"""init_logging 幂等性与默认目录回归（评审 入口 M4）。

已复现缺陷
----------
- ``_inited`` 被置位却**从不读取**，文档称"幂等"但每次调用都重建 handler；
- 嵌入式宿主先 ``configure(for_test(...))`` 再走 CLI 入口时，配置会被**整个
  替换**（实测 ``log_dir`` 与 ``retention_days`` 都被改写）；
- 目录不可写时 ``init_logging`` 抛错而调用方通常 ``except`` 掉 →
  ``_configured`` 保持 None，**全部埋点与自动分析悄无声息地失效**；
- 默认目录 ``Path(__file__).resolve().parents[2] / "logs"`` 在 pip 安装后解析成
  ``<prefix>/logs``（如 /usr/lib/python3.12/logs），与 ``VIBE_TRADING_HOME``
  的约定不一致。
"""

from __future__ import annotations

import logging
import os
import pathlib

import pytest


@pytest.fixture()
def clean(monkeypatch):
    """把 logsystem 与 bootstrap 的初始化状态都复位。"""
    import src.logsystem.logger as _logger
    import src.logsystem_bootstrap as _boot

    monkeypatch.setattr(_logger, "_configured", None)
    monkeypatch.setattr(_boot, "_inited", False)
    yield _logger, _boot


def test_default_dir_follows_runtime_root(clean, monkeypatch, tmp_path):
    """默认目录必须是运行时根下的 logs/（遵循 VIBE_TRADING_HOME）。"""
    import src.logsystem_bootstrap as boot

    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path / "rt"))
    monkeypatch.delenv("VIBE_LOG_DIR", raising=False)

    assert boot._default_log_dir() == tmp_path / "rt" / "logs"

    boot.init_logging()
    assert str(tmp_path / "rt" / "logs") == str(clean[0]._configured.config.log_dir)


def test_vibe_log_dir_wins_over_runtime_root(clean, monkeypatch, tmp_path):
    import src.logsystem_bootstrap as boot

    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path / "rt"))
    monkeypatch.setenv("VIBE_LOG_DIR", str(tmp_path / "explicit"))

    boot.init_logging()
    assert str(tmp_path / "explicit") == str(clean[0]._configured.config.log_dir)


def test_init_is_idempotent(clean, monkeypatch, tmp_path):
    """重复调用不得重建 handler / 改变配置。"""
    import src.logsystem_bootstrap as boot

    monkeypatch.setenv("VIBE_LOG_DIR", str(tmp_path / "a"))
    boot.init_logging()
    first = clean[0]._configured
    handlers_first = list(first.logger.handlers)

    monkeypatch.setenv("VIBE_LOG_DIR", str(tmp_path / "b"))
    boot.init_logging()  # 应直接返回

    assert clean[0]._configured is first, "重复初始化重建了日志系统"
    assert list(clean[0]._configured.logger.handlers) == handlers_first
    assert str(tmp_path / "a") == str(clean[0]._configured.config.log_dir)


def test_host_config_is_not_clobbered(clean, monkeypatch, tmp_path):
    """宿主已 configure 时，init_logging 必须保留其配置。"""
    from src.logsystem import LogConfig, configure
    import src.logsystem_bootstrap as boot

    configure(LogConfig(log_dir=str(tmp_path / "host"), retention_days=0))
    monkeypatch.setenv("VIBE_LOG_DIR", str(tmp_path / "cli"))

    boot.init_logging()

    cfg = clean[0]._configured.config
    assert str(tmp_path / "host") == str(cfg.log_dir), "宿主 log_dir 被覆盖"
    assert cfg.retention_days == 0, "宿主 retention_days 被覆盖"


def test_unwritable_dir_warns_instead_of_failing_silently(clean, tmp_path, caplog):
    """目录不可用必须**可见**（此前调用方 except 掉后现场毫无线索）。"""
    import src.logsystem_bootstrap as boot

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="src.logsystem_bootstrap"):
        with pytest.raises(Exception):
            boot.init_logging(log_dir=str(blocker / "sub"))

    assert "初始化失败" in caplog.text, caplog.text


def test_inited_flag_is_actually_used(clean, monkeypatch, tmp_path):
    """`_inited` 必须真正被读取（此前是死变量）。"""
    import src.logsystem_bootstrap as boot

    monkeypatch.setenv("VIBE_LOG_DIR", str(tmp_path / "x"))
    boot.init_logging()
    assert boot._inited is True

    # 手工把 _configured 清空但保留 _inited：仍应短路（说明读了 _inited）
    clean[0]._configured = None
    monkeypatch.setenv("VIBE_LOG_DIR", str(tmp_path / "y"))
    boot.init_logging()
    assert clean[0]._configured is None, "_inited 未被读取（幂等只依赖 _configured）"
