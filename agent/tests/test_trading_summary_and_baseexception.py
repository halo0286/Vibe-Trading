"""交易摘要截断与 BaseException 埋点回归（评审 L4 / L5）。

已复现缺陷
----------
- **L4** ``_read_summary`` / ``_order_summary`` 对字符串字段**不做截断**，
  broker 返回的 ``error`` 原样落盘（实测单行 7500 字符、内嵌账号样式串），
  而 ``traced_step`` 自己的 ``error_msg`` 却截断到 200 —— 策略不一致。
  同时 ``_read_summary`` 的 docstring 称"不记任何财务数值"，但 broker 错误文本
  无法保证不含账号/金额，说法过于绝对。
- **L5** ``traced_step`` 只捕获 ``Exception``，而 ``BaseEngine.run_backtest`` 在
  无数据/空信号时走 ``sys.exit(1)``（抛 ``SystemExit``）—— 这类真实失败路径
  **什么日志都不产生**。
"""

from __future__ import annotations

import glob
import logging
import os
import pathlib
import re

import pytest


def _lines(log_dir) -> list:
    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass
    out = []
    for path in glob.glob(os.path.join(str(log_dir), "*.log")):
        out += [l for l in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def _configure(tmp_path):
    from src.logsystem import LogConfig, configure

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))


_LONG = "account 12345678 balance 999999.99 " * 250  # 8750 字符


# --------------------------------------------------------------------------- #
# L4
# --------------------------------------------------------------------------- #

def test_read_summary_clips_long_strings():
    from src.trading.service import _MAX_SUMMARY_CHARS, _read_summary

    out = _read_summary((), {}, {"status": "error", "reason": _LONG})
    assert len(out["reason"]) <= _MAX_SUMMARY_CHARS + 20, len(out["reason"])
    assert out["reason"].endswith("<truncated>")


def test_order_summary_clips_long_strings():
    from src.trading.service import _MAX_SUMMARY_CHARS, _order_summary

    out = _order_summary({"status": "ok", "order_id": "O1", "error": _LONG})
    assert len(out["error"]) <= _MAX_SUMMARY_CHARS + 20, len(out["error"])


def test_order_summary_keeps_audit_fields():
    """下单/撤单是审计事件：quantity/price 属合规留痕必需，刻意保留。"""
    from src.trading.service import _order_summary

    out = _order_summary({"status": "ok", "quantity": 100,
                          "average_price": 12.5, "limit_price": 13.0})
    assert out["quantity"] == 100
    assert out["average_price"] == 12.5
    assert out["limit_price"] == 13.0


def test_read_summary_omits_financial_values():
    """只读查询不得落盘余额/持仓数量等财务数值。"""
    from src.trading.service import _read_summary

    out = _read_summary((), {}, {
        "status": "ok", "balance": 123456.78, "equity": 999.0,
        "positions": [{"qty": 1}], "orders": [1, 2],
    })
    for banned in ("balance", "equity", "positions", "orders"):
        assert banned not in out, f"{banned} 不应落盘"
    assert out.get("positions_count") == 1
    assert out.get("orders_count") == 2


def test_log_line_length_is_bounded(tmp_path):
    """端到端：超长 broker error 不得把日志单行撑到几千字符。"""
    from src.trading.service import _traced_order

    _configure(tmp_path)

    @_traced_order("trading.place_order")
    def place():
        return {"status": "error", "error": _LONG, "order_id": "O1"}

    place()
    line = _lines(tmp_path)[0]
    assert len(line) < 2000, f"日志单行 {len(line)} 字符"


# --------------------------------------------------------------------------- #
# L5
# --------------------------------------------------------------------------- #

def test_systemexit_is_logged_and_propagated(tmp_path):
    """sys.exit(1) 是真实失败路径：必须记 failed 且原样传播。"""
    from src.logsystem_bootstrap import traced_step

    _configure(tmp_path)

    @traced_step("engine.run")
    def run():
        raise SystemExit(1)

    with pytest.raises(SystemExit) as excinfo:
        run()
    assert excinfo.value.code == 1, "SystemExit 被改写"

    line = _lines(tmp_path)[0]
    assert "step=engine.run" in line, line
    assert "status=failed" in line, line
    assert "SystemExit" in line, line


def test_keyboardinterrupt_is_logged_and_propagated(tmp_path):
    from src.logsystem_bootstrap import traced_step

    _configure(tmp_path)

    @traced_step("engine.kb")
    def run():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run()
    assert "status=failed" in _lines(tmp_path)[0]


def test_exception_semantics_unchanged(tmp_path):
    """普通异常仍然只记一次并原样传播（不能因放宽捕获而重复记录）。"""
    from src.logsystem_bootstrap import traced_step

    _configure(tmp_path)

    @traced_step("engine.err")
    def run():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run()
    rows = _lines(tmp_path)
    assert len(rows) == 1, rows
    assert "status=failed" in rows[0]
