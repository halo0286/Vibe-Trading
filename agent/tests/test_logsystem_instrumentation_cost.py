"""埋点开销回归（评审 M3）。

缺陷（已量化）
--------------
所有装饰器都在 ``safe_log_event`` 的被动短路**之前**就把工作做完了：
算耗时、``_effective_args`` 合并默认参数、``_fetch_log`` 扫描整个结果字典、
``_prompt_shape`` 遍历 messages、``summarize`` 构造 extra —— 日志未初始化时
这些成果必然被丢弃，纯属浪费。

独立评审实测：``fetch_market_data`` 装饰后端到端 **+9~11%**；
``_effective_args`` 每次调用 ``inspect.signature``（约 9~13 µs，**未缓存**，
``signature(f) is signature(f)`` 为 False）。

本文件锁定：日志关闭时装饰器**不做埋点工作**（但仍必须正确调用被装饰函数）；
日志开启时行为不变；``inspect.signature`` 只算一次。
"""

from __future__ import annotations

import glob
import inspect
import logging
import os
import pathlib

import pytest


@pytest.fixture()
def inactive(monkeypatch):
    """把 logsystem 置为未初始化状态。"""
    import src.logsystem.logger as _logger

    monkeypatch.setattr(_logger, "_configured", None)
    return _logger


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


# --------------------------------------------------------------------------- #
# logsystem_active
# --------------------------------------------------------------------------- #

def test_logsystem_active_reflects_state(inactive, tmp_path):
    from src.logsystem import LogConfig, configure, logsystem_active

    assert logsystem_active() is False
    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))
    assert logsystem_active() is True


def test_logsystem_active_is_advanced_not_core():
    """它是埋点内部短路用的工具，不应挤占"8 个核心 API"的契约。"""
    import src.logsystem as L

    assert "logsystem_active" in L.ADVANCED_API
    assert "logsystem_active" not in L.CORE_API
    assert len(L.CORE_API) == 8


def test_logsystem_active_is_importable(inactive):
    """库内部与外部都可按名导入（不依赖 __all__）。"""
    from src.logsystem import logsystem_active  # noqa: F401
    from src.logsystem_bootstrap import logsystem_active as boot  # noqa: F401


# --------------------------------------------------------------------------- #
# 装饰器短路：不做埋点工作
# --------------------------------------------------------------------------- #

def test_fetch_decorator_skips_work_when_inactive(inactive, monkeypatch):
    """日志关闭时不得调用 _effective_args / _fetch_log。"""
    from src import market_data

    used = []
    monkeypatch.setattr(market_data, "_effective_args",
                        lambda *a, **k: used.append("args") or {})
    monkeypatch.setattr(market_data, "_fetch_log",
                        lambda *a, **k: used.append("log"))

    @market_data._traced_fetch
    def fake(symbols, source=None, interval="1d"):
        return {s: {"close": 1} for s in symbols}

    out = fake(["AAPL"])
    assert list(out) == ["AAPL"], "短路后仍必须正确返回业务结果"
    assert used == [], f"日志关闭时仍做了埋点工作: {used}"


def test_llm_decorator_skips_work_when_inactive(inactive, monkeypatch):
    import src.providers.chat as providers_chat

    providers = type("P", (), {"chat": providers_chat})()

    used = []
    monkeypatch.setattr(providers.chat, "_prompt_shape",
                        lambda m: used.append("shape") or (0, 0))
    monkeypatch.setattr(providers.chat, "_llm_log",
                        lambda *a, **k: used.append("log"))

    class C:
        @providers.chat._traced_llm_call
        def chat(self, messages):
            return "RESP"

    assert C().chat([{"role": "user", "content": "x"}]) == "RESP"
    assert used == [], f"日志关闭时仍做了埋点工作: {used}"


def test_traced_step_skips_summarize_when_inactive(inactive):
    from src.logsystem_bootstrap import traced_step

    used = []

    @traced_step("s", summarize=lambda a, k, r: used.append("sum") or {})
    def fn(x):
        return x + 1

    assert fn(1) == 2
    assert used == [], "日志关闭时仍调用了 summarize"


# --------------------------------------------------------------------------- #
# 日志开启时行为不变
# --------------------------------------------------------------------------- #

def test_fetch_decorator_still_logs_when_active(tmp_path):
    from src.logsystem import LogConfig, configure
    from src.market_data import _traced_fetch

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))

    @_traced_fetch
    def fake(symbols, source=None, interval="1d"):
        return {s: {"close": 1} for s in symbols}

    fake(["AAPL", "MSFT"])
    line = _lines(tmp_path)[0]
    assert "step=market_data.fetch" in line
    assert "status=success" in line
    assert "resolved=2" in line


def test_traced_step_still_logs_when_active(tmp_path):
    from src.logsystem import LogConfig, configure
    from src.logsystem_bootstrap import traced_step

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))

    @traced_step("s", summarize=lambda a, k, r: {"n": r})
    def fn(x):
        return x + 1

    assert fn(1) == 2
    line = _lines(tmp_path)[0]
    assert "step=s" in line and "status=success" in line and "n=2" in line


# --------------------------------------------------------------------------- #
# inspect.signature 缓存
# --------------------------------------------------------------------------- #

def test_signature_is_cached(monkeypatch):
    """inspect.signature 每次约 9~13 µs 且不返回同一对象 —— 必须缓存。"""
    from src import market_data

    market_data._signature_of.cache_clear()
    calls = []
    real = inspect.signature

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(market_data.inspect, "signature", counting)

    def fn(x, y=1, *, z=2):
        return x

    for _ in range(20):
        market_data._effective_args(fn, (1,), {})
    assert len(calls) == 1, f"signature 被计算了 {len(calls)} 次（未缓存）"


def test_effective_args_still_merges_defaults():
    from src.market_data import _effective_args

    def fn(symbols, source=None, interval="1d"):
        return symbols

    merged = _effective_args(fn, (["A"],), {})
    assert merged["interval"] == "1d", merged
    assert merged["symbols"] == ["A"], merged
