"""埋点被动性契约：埋点不得改变宿主进程的全局状态。

背景（实测缺陷）
----------------
``logsystem_bootstrap.safe_log_event`` 早期实现直接调用 ``log_event``，而后者经
``get_logger()`` 在未配置时会自动 ``configure(LogConfig())``。副作用包括：

1. 在工作目录创建 ``logs/`` 并挂上文件 handler；
2. 使 ``AgentLoop.run`` 收尾的 ``finish_and_analyze`` 判定为「已初始化」，
   从而启动后台分析守护线程（``analyzer.py`` 中的 ``loganalysis-*``），
   扫描日志目录并写 ``analysis_*.json``。

后果：只要任何被埋点的业务函数被调用一次，就改变了整个进程的日志全局状态。
在测试套件中表现为与被埋点业务**无关的随机失败**（后台线程 I/O 竞争），
排查成本极高。曾观测到仓库根目录被自动生成 ``logs/``（61 个文件）。

本测试锁定契约：**未显式初始化时，埋点必须完全被动。**
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def test_safe_log_event_is_passive_before_configure(tmp_path, monkeypatch):
    """未初始化时 safe_log_event 不得触发 configure，也不得创建日志目录。"""
    monkeypatch.chdir(tmp_path)
    # 重置全局配置，模拟「宿主程序尚未初始化日志系统」
    import src.logsystem.logger as ls_logger

    monkeypatch.setattr(ls_logger, "_configured", None, raising=False)

    from src.logsystem_bootstrap import safe_log_event

    safe_log_event(logging.INFO, "probe", step="probe", status="success")

    assert ls_logger._configured is None, "埋点不应触发 configure()"
    assert not (tmp_path / "logs").exists(), "埋点不应创建 logs/ 目录"


def test_safe_log_event_logs_once_configured(tmp_path, monkeypatch):
    """显式初始化后，埋点必须照常落盘（被动 ≠ 失效）。"""
    monkeypatch.chdir(tmp_path)
    from src.logsystem import LogConfig, configure

    configure(LogConfig(log_dir=str(tmp_path / "logs"), level="INFO"))

    from src.logsystem_bootstrap import safe_log_event

    safe_log_event(logging.INFO, "after-init", step="probe2", status="success")

    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass

    written = "".join(
        p.read_text(encoding="utf-8") for p in (tmp_path / "logs").glob("*.log")
    )
    assert "after-init" in written, written


def test_safe_log_event_never_raises(monkeypatch):
    """埋点异常必须被吞掉：即使内部解析炸了也不能影响业务调用方。"""
    import src.logsystem_bootstrap as boot

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated logsystem failure")

    monkeypatch.setattr(boot, "log_event", _boom)
    # 未初始化路径
    boot.safe_log_event(logging.INFO, "x")
    # 已初始化路径
    import src.logsystem.logger as ls_logger

    monkeypatch.setattr(ls_logger, "_configured", object(), raising=False)
    boot.safe_log_event(logging.INFO, "x")  # 不应抛出
