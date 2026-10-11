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


# --------------------------------------------------------------------------- #
# 字段级契约：生产端（logger.py）与消费端（analyzer.py）必须共用同一份清单
#
# 已复现缺陷（改造前）
# --------------------
# - ``logger.py`` 内联一个 14 项业务字段元组赋给 ``for k in (...)``；
#   ``analyzer.py`` 另写 8 个字符串字面量读取字段。两边各写一遍：
#   **改名字段时没有任何编译期/测试期报错，消费端会静默忽略该字段。**
# - ``constants.KEYWORD_DICT``（注释自称"日志结构化字段的事实标准"）漏收了
#   ``exc_text`` / ``business_done`` —— **标准本身已与生产端漂移**。
#
# 现在：两边都从 ``constants`` 派生；下面三条测试守住契约。
# --------------------------------------------------------------------------- #

def test_analyzed_fields_are_subset_of_produced_fields():
    """消费端声明消费的字段，必须都能被生产端写出。"""
    from src.logsystem.constants import ANALYZED_FIELDS, PRODUCED_FIELDS

    orphan = sorted(set(ANALYZED_FIELDS) - set(PRODUCED_FIELDS))
    assert not orphan, (
        f"分析器要读这些字段，但生产端不会写出：{orphan}；"
        f"请同步 constants.BUSINESS_FIELDS / BASE_FIELDS"
    )


def test_keyword_dict_covers_all_produced_fields():
    """KEYWORD_DICT（字段事实标准）必须覆盖生产端写出的全部字段。"""
    from src.logsystem.constants import KEYWORD_FIELDS, PRODUCED_FIELDS

    undocumented = sorted(set(PRODUCED_FIELDS) - set(KEYWORD_FIELDS))
    assert not undocumented, (
        f"生产端会写出这些字段，但 KEYWORD_DICT 里没有登记：{undocumented}"
    )


def test_producer_writes_only_contract_fields(tmp_path):
    """端到端：生产端实际写出的字段必须落在契约内，且基础字段齐全。"""
    import json as _json

    from src.logsystem import LogConfig, configure, log_event, trace_scope
    from src.logsystem.constants import BASE_FIELDS, MESSAGE_FIELD, PRODUCED_FIELDS

    configure(LogConfig(log_dir=str(tmp_path), file_format="json",
                        enable_async_analysis=False))
    with trace_scope(business_id="b-1", trace_id="t-1"):
        log_event(logging.INFO, "order placed", step="order", status="success",
                  extra={"cost_ms": 12.5, "business_done": True})

    lines = _lines(tmp_path)
    assert lines, "生产端没有写出任何日志"
    rec = _json.loads(lines[-1])

    outside = sorted(set(rec) - set(PRODUCED_FIELDS))
    assert not outside, f"生产端写出了契约外的字段：{outside}（请登记到 constants）"

    must_have = set(BASE_FIELDS) | {MESSAGE_FIELD}
    missing = sorted(must_have - set(rec))
    assert not missing, f"基础字段缺失：{missing}"

    # 业务字段确实按契约里的名字写出（两侧名字一致才可能命中）
    for key in ("business_id", "trace_id", "step", "status", "cost_ms", "business_done"):
        assert key in rec, f"契约字段 {key} 未被生产端写出"


def test_report_consumes_error_detail_fields(tmp_path):
    """排障字段（error_msg / exception_type / stack_trace）必须进入分析报告。

    改造前：分析器只读 8 个字段，这三个**产出但从不被分析消费** ——
    报告里只有 error_code 分布，排障时还得手工抠原始日志。
    """
    from src.logsystem import LogConfig, configure, log_event, run_analysis, trace_scope

    configure(LogConfig(log_dir=str(tmp_path), file_format="json",
                        enable_async_analysis=False))
    with trace_scope(business_id="b-err"):
        log_event(logging.ERROR, "order failed", step="order", status="failed",
                  error_code="E_BOOM", error_msg="upstream timeout",
                  extra={"exception_type": "TimeoutError",
                         "stack_trace": "Traceback (most recent call last):\n  ...\nTimeoutError"})

    report = run_analysis(str(tmp_path))
    assert report["total_logs"] == 1, report
    assert report["error_count"] == 1
    assert report["exception_types"] == {"TimeoutError": 1}, report["exception_types"]

    detail = report["error_details"][0]
    assert detail["exception_type"] == "TimeoutError"
    assert detail["error_code"] == "E_BOOM"
    assert detail["count"] == 1
    assert detail["sample_error_msg"] == "upstream timeout"
    assert "Traceback" in detail["sample_stack_trace"]

    # 契约可见性：这几个字段确实被观测到 → 不应出现在缺失清单里
    observed = set(report["fields_observed"])
    assert {"error_msg", "exception_type", "stack_trace"} <= observed, report["fields_observed"]


def test_report_flags_missing_contract_fields(tmp_path):
    """只写最小日志时，契约中未出现的字段必须显式列出（漂移在运行时可见）。"""
    from src.logsystem import LogConfig, configure, log_event, run_analysis

    configure(LogConfig(log_dir=str(tmp_path), file_format="json",
                        enable_async_analysis=False))
    log_event(logging.INFO, "minimal")

    report = run_analysis(str(tmp_path))
    missing = set(report["missing_contract_fields"])
    # 最小日志不会带这些排障字段
    assert {"exception_type", "stack_trace", "error_msg"} <= missing, missing
    # 但基础字段必须已观测到
    assert {"level", "message"} <= set(report["fields_observed"])
    assert any("契约字段缺失" in s for s in report["suggestions"])


def test_stack_trace_sample_is_clipped(tmp_path):
    """超长 stack_trace 必须被截断，不能把报告撑爆。"""
    from src.logsystem import LogConfig, configure, log_event, run_analysis, trace_scope

    configure(LogConfig(log_dir=str(tmp_path), file_format="json",
                        enable_async_analysis=False))
    huge = "x" * 50_000
    with trace_scope(business_id="b-big"):
        log_event(logging.ERROR, "boom", status="failed",
                  extra={"exception_type": "Boom", "stack_trace": huge})

    report = run_analysis(str(tmp_path))
    sample = report["error_details"][0]["sample_stack_trace"]
    assert "已截断" in sample, "超长 stack_trace 未被截断"
    assert len(sample) < 3_000, f"截断后仍过长：{len(sample)}"
