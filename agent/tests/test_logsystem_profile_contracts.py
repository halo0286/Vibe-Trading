"""配置档契约回归（评审 H-4 / H-5）。

已复现缺陷
----------
- **H-4** ``for_prod()`` 设 ``sample_rate=1.0``，而限流器超限分支是
  ``random.random() < sample_rate`` —— 恒真，于是"限流"从不丢弃任何记录。
  实测阈值 5/60s、写入 200 条 → **落盘 200 行**。
- **H-5** ``for_prod()`` 用 ``file_format="json"``，但 ``run_analysis`` 默认
  ``"kv"`` —— 生产档下分析器按 kv 解析 json 行，一条也读不出，报告恒为
  "未找到匹配日志"，即自动分析闭环在生产档静默失效。
"""

from __future__ import annotations

import glob
import logging
import os

import pytest


def _lines(log_dir) -> list:
    import pathlib

    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass
    out = []
    for path in glob.glob(os.path.join(str(log_dir), "*.log")):
        out += [l for l in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


# --------------------------------------------------------------------------- #
# H-4：prod 档必须真的限流
# --------------------------------------------------------------------------- #

def test_for_prod_sample_rate_is_below_one():
    """契约：sample_rate 是超限后的放行比例，1.0 等于不限流。"""
    from src.logsystem import LogConfig

    assert LogConfig.for_prod().sample_rate < 1.0


def test_rate_limiter_actually_drops(tmp_path):
    """超限后必须丢弃记录（不能只靠 sample_rate 控制）。"""
    from src.logsystem import LogConfig, configure, log_event

    configure(LogConfig.for_prod(log_dir=str(tmp_path),
                                 rate_limit_max=5,
                                 rate_limit_window_seconds=60,
                                 enable_async_analysis=False))
    for _ in range(200):
        log_event(logging.INFO, "storm", step="hot")

    written = len(_lines(tmp_path))
    assert written < 100, f"限流未生效：阈值 5 却写入 {written} 行"


def test_rate_limiter_records_dropped_count(tmp_path):
    from src.logsystem import LogConfig, configure, log_event
    from src.logsystem import logger as _logger_module

    configure(LogConfig(log_dir=str(tmp_path), rate_limit_max=3,
                        rate_limit_window_seconds=60, sample_rate=0.0,
                        enable_async_analysis=False))
    for _ in range(50):
        log_event(logging.INFO, "storm", step="hot")

    limiter = _logger_module._configured.rate_limiter
    assert limiter.dropped > 0, "丢弃计数未累计"


def test_rate_limit_zero_means_unlimited(tmp_path):
    from src.logsystem import LogConfig, configure, log_event

    configure(LogConfig(log_dir=str(tmp_path), rate_limit_max=0,
                        enable_async_analysis=False))
    for _ in range(30):
        log_event(logging.INFO, "spam", step="x")
    assert len(_lines(tmp_path)) == 30


# --------------------------------------------------------------------------- #
# H-5：分析格式跟随配置
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fmt", ["kv", "json"])
def test_run_analysis_follows_configured_format(tmp_path, fmt):
    from src.logsystem import LogConfig, configure, log_event, run_analysis

    configure(LogConfig(log_dir=str(tmp_path), file_format=fmt,
                        enable_async_analysis=False))
    log_event(logging.INFO, "a", step="order", status="success")
    log_event(logging.INFO, "b", step="order", status="success")

    report = run_analysis(str(tmp_path))  # 不传 file_format
    assert report["total_logs"] == 2, f"{fmt} 档下分析器读不到日志: {report}"


def test_run_analysis_on_prod_profile(tmp_path):
    """生产档（json）下自动分析闭环必须真的读到日志。"""
    from src.logsystem import LogConfig, configure, log_event, run_analysis

    configure(LogConfig.for_prod(log_dir=str(tmp_path),
                                 enable_async_analysis=False))
    log_event(logging.INFO, "order placed", step="order", status="success")

    report = run_analysis(str(tmp_path))
    assert report["total_logs"] == 1, report


def test_explicit_format_wins(tmp_path):
    """显式指定优先于配置。"""
    from src.logsystem import LogConfig, configure, log_event, run_analysis

    configure(LogConfig(log_dir=str(tmp_path), file_format="json",
                        enable_async_analysis=False))
    log_event(logging.INFO, "a", step="order", status="success")

    assert run_analysis(str(tmp_path), file_format="json")["total_logs"] == 1
    # 显式指定错的格式 → 读不到（证明显式参数确实生效）
    assert run_analysis(str(tmp_path), file_format="kv")["total_logs"] == 0


def test_resolve_file_format_defaults_to_kv_without_config(monkeypatch):
    from src.logsystem import analyzer

    monkeypatch.setattr(analyzer._logger_module, "_configured", None)
    assert analyzer.resolve_file_format() == "kv"
    assert analyzer.resolve_file_format("json") == "json"
