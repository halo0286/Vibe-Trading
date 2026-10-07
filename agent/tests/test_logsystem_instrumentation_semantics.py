"""埋点状态语义契约（评审 C-3 / C-4 / H-7 / M-2 回归）。

背景（均为实测缺陷）
--------------------
1. **C-3** 分类器用的是 3 个词的**否定清单**（error/failed/rejected），于是
   ``{"status": "blocked"}``（kill-switch / mandate 拒单）被记成
   ``trading.place_order.success``（INFO），分析器报 ``error_count=0`` ——
   最安全敏感的熔断拒单在日志里读起来是一笔成功的实盘下单。
2. **C-4** ``log_business_event`` 先写规范字段再 ``ctx.update(extra)``，而
   摘要器把载荷自身的 ``status`` 放进 extra，于是**规范 status 被覆盖**：
   ``trading.get_account`` 认证失败落盘 ``status=not_authorized``，成功的调用
   落盘 ``status=ok``（永远不是规范值 ``success``），按 status 聚合的
   ``error_count`` / ``status_distribution`` 全部失真。
3. **H-7** 原先的 ``"rejected"`` 不是分析器认识的失败值
   （``analyzer`` 只把 ``failed`` 计为错误）→ 拒单对所有分析隐形。
4. **M-2** ``_tool_outcome`` 只认 4 个词，``timeout`` / ``budget_exceeded`` /
   ``quote_not_bounded`` / ``empty`` / ``fail`` / ``500`` 全被判为 success。

本文件锁定：**允许清单分类** + **规范字段不可被 extra 覆盖**。
"""

from __future__ import annotations

import glob
import logging
import os

import pytest


def _configure(tmp_path):
    from src.logsystem import LogConfig, configure

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))


def _flush():
    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _read(tmp_path) -> str:
    _flush()
    return "".join(
        open(p, encoding="utf-8").read()
        for p in glob.glob(os.path.join(str(tmp_path), "*.log"))
    )


def _fields(line: str) -> dict:
    import re

    out = {}
    for key, value in re.findall(r"(\w+)=(\"[^\"]*\"|\S+)", line):
        out[key] = value[1:-1] if value.startswith('"') and value.endswith('"') else value
    return out


def _line_for(text: str, step: str) -> dict:
    for line in text.splitlines():
        if f"step={step}" in line:
            return _fields(line)
    raise AssertionError(f"未找到 step={step} 的日志行:\n{text}")


# --------------------------------------------------------------------------- #
# C-3：允许清单分类
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "verdict",
    ["blocked", "not_authorized", "timeout", "budget_exceeded",
     "quote_not_bounded", "empty", "fail", "error", "failed", "rejected", 500],
)
def test_failure_verdicts_are_not_success(verdict):
    """所有非成功状态值都必须判为 failed（否定清单会漏掉它们）。"""
    from src.logsystem_bootstrap import business_outcome

    assert business_outcome({"status": verdict}) == "failed", verdict


@pytest.mark.parametrize("verdict", ["success", "ok", "completed", "done", "SUCCESS", " Ok "])
def test_success_verdicts(verdict):
    from src.logsystem_bootstrap import business_outcome

    assert business_outcome({"status": verdict}) == "success", verdict


def test_missing_status_is_success():
    """没有 status 字段（以及非 dict）说明调用未抛异常 → success。"""
    from src.logsystem_bootstrap import business_outcome

    assert business_outcome({"order_id": "O1"}) == "success"
    assert business_outcome("raw string") == "success"
    assert business_outcome(None) == "success"


def test_kill_switch_blocked_order_logged_as_failed(tmp_path):
    """C-3 核心：kill-switch 拒单必须记 failed，并带上拒单原因。"""
    _configure(tmp_path)
    from src.trading.service import _traced_order

    @_traced_order("trading.place_order")
    def blocked_order():
        return {
            "status": "blocked",
            "reason": "live trading halted by kill switch",
            "symbol": "AAPL",
        }

    blocked_order()
    fields = _line_for(_read(tmp_path), "trading.place_order")

    assert fields["status"] == "failed", fields
    assert fields.get("error_code") == "blocked", fields
    assert "kill switch" in (fields.get("error_msg") or ""), fields
    # 载荷自身的状态仍可观测（改名为 order_status，不覆盖规范字段）
    assert fields.get("order_status") == "blocked", fields


# --------------------------------------------------------------------------- #
# C-4：规范字段不可被 extra 覆盖
# --------------------------------------------------------------------------- #

def test_extra_cannot_clobber_canonical_status(tmp_path):
    """log_business_event 的规范字段必须最终生效，extra 无权覆盖。"""
    _configure(tmp_path)
    from src.logsystem import log_event

    log_event(logging.INFO, "probe", step="s", status="success",
              extra={"status": "not_authorized", "read_status": "not_authorized"})

    fields = _line_for(_read(tmp_path), "s")
    assert fields["status"] == "success", fields
    assert fields.get("read_status") == "not_authorized", fields


def test_not_authorized_read_logged_as_failed(tmp_path):
    """OAuth 失效的只读调用必须是 failed，且规范 status 不被 read_status 顶掉。"""
    _configure(tmp_path)
    from src.logsystem_bootstrap import traced_step
    from src.trading.service import _read_summary

    @traced_step("trading.get_account", summarize=_read_summary)
    def not_authorized():
        return {"status": "not_authorized", "error": "token expired", "positions": [1]}

    not_authorized()
    fields = _line_for(_read(tmp_path), "trading.get_account")

    assert fields["status"] == "failed", fields
    assert fields.get("read_status") == "not_authorized", fields


def test_successful_read_keeps_canonical_success(tmp_path):
    """成功的只读调用必须是规范值 success，而不是载荷的 ok。"""
    _configure(tmp_path)
    from src.logsystem_bootstrap import traced_step
    from src.trading.service import _read_summary

    @traced_step("trading.get_positions", summarize=_read_summary)
    def positions():
        return {"status": "ok", "positions": [1, 2, 3]}

    positions()
    fields = _line_for(_read(tmp_path), "trading.get_positions")

    assert fields["status"] == "success", fields
    assert fields.get("read_status") == "ok", fields
    assert fields.get("positions_count") == "3", fields


# --------------------------------------------------------------------------- #
# H-7：分析器必须把拒单计为错误
# --------------------------------------------------------------------------- #

def test_analyzer_counts_refusals_as_errors(tmp_path):
    """端到端：熔断拒单 + 认证失败 → error_count=2，dist 里没有垃圾状态值。"""
    _configure(tmp_path)
    from src.logsystem import log_event
    from src.logsystem.analyzer import LogAnalyzer
    from src.logsystem_bootstrap import traced_step
    from src.trading.service import _read_summary, _traced_order

    @_traced_order("trading.place_order")
    def blocked_order():
        return {"status": "blocked", "reason": "halted", "symbol": "AAPL"}

    @traced_step("trading.get_account", summarize=_read_summary)
    def not_authorized():
        return {"status": "not_authorized", "error": "token expired"}

    blocked_order()
    not_authorized()
    log_event(logging.INFO, "ok", step="trading.cancel_order", status="success")

    report = LogAnalyzer(str(tmp_path)).analyze()
    assert report["error_count"] == 2, report
    dist = report.get("status_distribution") or {}
    assert dist.get("failed") == 2, dist
    assert dist.get("success") == 1, dist
    # 载荷自带的状态值绝不能出现在规范 status 字段里
    assert "blocked" not in dist, f"载荷状态泄漏进规范 status 字段: {dist}"
    assert "not_authorized" not in dist, f"载荷状态泄漏进规范 status 字段: {dist}"


# --------------------------------------------------------------------------- #
# M-2 / M-3：工具埋点分类与有界解析
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "verdict",
    ["error", "failed", "rejected", "timeout", "budget_exceeded",
     "quote_not_bounded", "empty", "fail", "blocked", 500],
)
def test_tool_outcome_catches_real_failure_verdicts(verdict):
    """真实工具使用的失败状态词都必须判为 failed（M-2）。"""
    import json as _json

    from src.agent.tools import _tool_outcome

    assert _tool_outcome(_json.dumps({"status": verdict})) == "failed", verdict


@pytest.mark.parametrize("verdict", ["ok", "success", "completed"])
def test_tool_outcome_success_verdicts(verdict):
    import json as _json

    from src.agent.tools import _tool_outcome

    assert _tool_outcome(_json.dumps({"status": verdict})) == "success", verdict


def test_tool_outcome_is_robust_to_non_json():
    from src.agent.tools import _tool_outcome

    assert _tool_outcome("not json") == "success"
    assert _tool_outcome({"status": "error"}) == "success"  # 非 str：不伪判
    assert _tool_outcome(None) == "success"


def test_tool_outcome_large_payload_is_bounded():
    """超过阈值的载荷只做有界判定，代价不随体积线性增长（M-3）。"""
    import json as _json
    import time

    from src.agent.tools import _tool_outcome

    small = _json.dumps({"status": "ok", "data": [{"a": 1}] * 50})
    huge = _json.dumps({"status": "ok", "data": [{"a": 1, "b": "x" * 40}] * 400000})
    assert len(huge) > 20_000_000

    t0 = time.perf_counter()
    assert _tool_outcome(huge) == "success"
    elapsed = time.perf_counter() - t0
    # 全量解析 20MB 需数十毫秒；有界判定应远低于此
    assert elapsed < 0.05, f"超大载荷判定耗时 {elapsed * 1000:.1f}ms，疑似全量解析"

    assert _tool_outcome(small) == "success"


def test_tool_outcome_large_payload_prefix_failure():
    """超大载荷的前缀若已表明失败，也要判 failed（尽力判定）。"""
    import json as _json

    from src.agent.tools import _tool_outcome

    huge = _json.dumps({"status": "timeout", "data": [{"a": 1}] * 400000})
    assert len(huge) > 1_000_000
    assert _tool_outcome(huge) == "failed"
