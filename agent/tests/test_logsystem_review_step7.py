"""评审步 7 的 M/L 级修复回归。

覆盖：
- **M16** ``resolve_business_id`` 无重入保护 → resolver 内写日志会指数放大
  （实测一次 ``log_event`` 触发 199 次 resolver / 198 行日志，靠吞掉
  ``RecursionError`` 才停）。
- **S23** resolver 返回值不做类型校验（返回 list 会让分析任务 key 变成
  unhashable 而抛 TypeError）。
- **M2** ``_traced_llm_call`` 的后置探针无保护 → ``usage_metadata`` 非 dict 时
  ``AttributeError`` 逸出装饰器，把**成功**的 provider 调用变成异常。
- **L1** 埋点里裸 ``str(exc)`` → 重写 ``__str__`` 的异常会**顶替真正的业务异常**。
- **L19** ``_apply_overrides`` 用 ``hasattr`` → 方法也能被覆盖。
- **M17** ``from_env`` 遇未知 ``LOG_PROFILE`` 直接崩，而不是回落。
- **M14** ``for_test()`` 每次调用泄漏一个临时目录。
- **L18** ``__all__`` 实为 10 项而文档称 8。
- **agent.session H2**（入口评审）失败会话被记为 success。
- **L3** ``agent.session.started`` 被记为 WARNING。
- **H1** ``_fetch_log`` 的未解析检测是死代码（顶层 ``_unresolved`` 是 list）。
- **M5** ``chat_id`` 明文（部分渠道即手机号）。
"""

from __future__ import annotations

import glob
import logging
import os
import pathlib

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


# --------------------------------------------------------------------------- #
# M16 / S23：resolver 重入与类型
# --------------------------------------------------------------------------- #

def test_resolver_is_not_reentered(tmp_path):
    """resolver 内部写日志不得导致指数级重入（199 → 个位数）。"""
    from src.logsystem import LogConfig, configure, log_event
    from src.logsystem import trace as _trace

    configure(LogConfig(log_dir=str(tmp_path), level="DEBUG",
                        enable_async_analysis=False))
    calls = []

    def noisy():
        calls.append(1)
        logging.getLogger("logsystem").debug("resolver side log")
        return None

    original = list(_trace._BUSINESS_ID_RESOLVERS)
    try:
        _trace._BUSINESS_ID_RESOLVERS[:] = [noisy]
        log_event(logging.INFO, "one event")
    finally:
        _trace._BUSINESS_ID_RESOLVERS[:] = original

    assert len(calls) <= 3, f"resolver 被调用 {len(calls)} 次（重入未阻断）"
    assert len(_lines(tmp_path)) < 10, "日志行数异常放大"


@pytest.mark.parametrize("bad", [12345, ["unhashable"], {"a": 1}, object(), "   "])
def test_resolver_non_string_values_are_rejected(bad):
    """非字符串/空白返回值必须被忽略（否则 key 可能 unhashable）。

    注意必须**先清空 contextvar**：resolve_business_id 优先返回 contextvar，
    而全量测试里前面的用例可能已经设过 business_id —— 不清空就测不到
    resolver 的返回值校验（本用例第一版正是因此只在隔离运行时通过）。
    """
    from src.logsystem import trace as _trace

    original_resolvers = list(_trace._BUSINESS_ID_RESOLVERS)
    original_bid = _trace.get_business_id()
    try:
        _trace.set_trace_context(business_id="")  # 空串为假 → 走 resolver
        _trace._BUSINESS_ID_RESOLVERS[:] = [lambda: bad]
        assert _trace.resolve_business_id() is None
    finally:
        _trace._BUSINESS_ID_RESOLVERS[:] = original_resolvers
        _trace.set_trace_context(business_id=original_bid or "")


# --------------------------------------------------------------------------- #
# M2 / L1：埋点绝不能伤害业务
# --------------------------------------------------------------------------- #

def test_llm_decorator_survives_non_dict_usage(tmp_path):
    """usage_metadata 非 dict 时装饰器必须仍正常返回。"""
    from src.logsystem import LogConfig, configure
    from src.providers.chat import _traced_llm_call

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))

    class Resp:
        usage_metadata = ["not", "a", "dict"]
        response_model = "m"
        finish_reason = "stop"
        tool_calls = ()
        content = "hi"
        content_filter_triggered = False

    class Client:
        @_traced_llm_call
        def chat(self, messages):
            return Resp()

    out = Client().chat([{"role": "user", "content": "x"}])
    assert out is not None


def test_safe_str_handles_raising_str():
    from src.logsystem_bootstrap import safe_str

    class Bad(Exception):
        def __str__(self):
            raise RuntimeError("str() exploded")

    assert safe_str(Bad()) == "Bad"


def test_raising_str_does_not_replace_business_exception(tmp_path):
    """埋点不得把业务异常顶替掉。"""
    from src.logsystem import LogConfig, configure
    from src.logsystem_bootstrap import traced_step

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))

    class Bad(Exception):
        def __str__(self):
            raise RuntimeError("str() exploded")

    @traced_step("biz.step")
    def boom():
        raise Bad()

    with pytest.raises(Bad):
        boom()


# --------------------------------------------------------------------------- #
# L19 / M17 / M14 / L18：配置契约
# --------------------------------------------------------------------------- #

def test_overrides_cannot_shadow_methods():
    from src.logsystem import LogConfig

    with pytest.raises(TypeError):
        LogConfig.for_dev(to_dict=1)
    with pytest.raises(TypeError):
        LogConfig.for_dev(ensure_dir="oops")
    with pytest.raises(TypeError):
        LogConfig.for_dev(typo_field=1)


def test_unknown_profile_falls_back(monkeypatch):
    from src.logsystem import LogConfig

    monkeypatch.setenv("LOG_PROFILE", "nope")
    cfg = LogConfig.from_env()  # 不得抛异常
    assert cfg.log_dir


def test_known_profile_still_applies(monkeypatch):
    from src.logsystem import LogConfig

    monkeypatch.setenv("LOG_PROFILE", "prod")
    assert LogConfig.from_env().file_format == "json"


def test_for_test_reuses_single_temp_dir():
    from src.logsystem import LogConfig

    assert LogConfig.for_test().log_dir == LogConfig.for_test().log_dir


def test_all_is_exactly_the_core_api():
    import src.logsystem as L

    assert list(L.__all__) == list(L.CORE_API)
    assert len(L.__all__) == 8


# --------------------------------------------------------------------------- #
# agent.session：失败会话不得记为 success
# --------------------------------------------------------------------------- #

def _agent_fields(tmp_path, status, result):
    from src.agent.loop import _agent_log
    from src.logsystem import LogConfig, configure

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))
    _agent_log(status, "sess", "hi", 0.0, result=result)
    line = _lines(tmp_path)[0]
    import re

    out = {}
    for k, v in re.findall(r"(\w+)=(\"[^\"]*\"|\S+)", line):
        out[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v
    return out


def test_failed_agent_run_is_failed_not_success(tmp_path):
    """入口评审 H2：_run_bound 返回 {"status":"failed"} 时必须记 failed。"""
    fields = _agent_fields(tmp_path, "success",
                           {"status": "failed", "error_code": "agent_loop_error"})
    assert fields["status"] == "failed", fields
    assert fields["message"] == "agent.session.failed", fields
    assert fields.get("level") == "WARNING", fields


def test_started_agent_run_is_info(tmp_path):
    """L3：正常开始不应记为 WARNING。"""
    fields = _agent_fields(tmp_path, "started", None)
    assert fields["status"] == "started", fields
    assert fields.get("level") == "INFO", fields


def test_successful_agent_run_is_info(tmp_path):
    fields = _agent_fields(tmp_path, "success", {"status": "success"})
    assert fields["status"] == "success", fields
    assert fields.get("level") == "INFO", fields


# --------------------------------------------------------------------------- #
# H1：_fetch_log 未解析检测
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "result,expected_resolved,expected_unresolved",
    [
        ({"AAPL": object(), "_unresolved": ["BAD1"]}, 1, 1),
        ({"AAPL": object(), "MSFT": object(), "_unresolved": []}, 2, 0),
        ({"_unresolved": ["A", "B"]}, 0, 2),
        ({"_unresolved": ["A"], "_provenance": {}}, 0, 1),
    ],
)
def test_fetch_log_counts_unresolved(tmp_path, result, expected_resolved, expected_unresolved):
    """真实返回是 {symbol: DataFrame} + 顶层 _unresolved(list)。"""
    import re

    from src.logsystem import LogConfig, configure
    from src.market_data import _fetch_log

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))
    _fetch_log("success", {"codes": list(result)}, result, 0.0)

    line = _lines(tmp_path)[0]
    fields = {}
    for k, v in re.findall(r"(\w+)=(\"[^\"]*\"|\S+)", line):
        fields[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v

    assert fields.get("resolved") == str(expected_resolved), fields
    assert fields.get("unresolved_count") == str(expected_unresolved), fields


def test_fetch_log_warns_when_nothing_resolved(tmp_path):
    """全部未解析时必须 WARNING（此前该分支是死代码）。"""
    from src.logsystem import LogConfig, configure
    from src.market_data import _fetch_log

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))
    _fetch_log("success", {"codes": ["A", "B"]}, {"_unresolved": ["A", "B"]}, 0.0)

    assert "level=WARNING" in _lines(tmp_path)[0]


# --------------------------------------------------------------------------- #
# 收尾两个一致性缺陷（C-4 同类 + H-8 同类）
# --------------------------------------------------------------------------- #

def test_auto_analysis_reports_its_own_status(tmp_path):
    """分析这一步的成败是"分析是否完成"，不是"被分析的业务是否失败"。

    此前直接落 report["status"]：分析成功产出报告却记 `status=failed`
    （level 硬编码 INFO，自相矛盾），并把业务失败**重复计入** auto_analysis，
    使 error_count 虚高。
    """
    import re

    from src.logsystem import LogConfig, configure, log_event, run_analysis

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))
    log_event(logging.INFO, "biz", step="order", status="failed",
              error_code="X", error_msg="boom")
    run_analysis(str(tmp_path))

    line = [l for l in _lines(tmp_path) if "auto_analysis" in l][0]
    fields = {}
    for k, v in re.findall(r'(\w+)=("[^"]*"|\S+)', line):
        fields[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v

    assert fields["status"] == "success", fields
    assert fields.get("analyzed_status") == "failed", fields


def test_auto_analysis_not_double_counted_as_error(tmp_path):
    """auto_analysis 不应把被分析业务的失败重复计成自己的错误。"""
    from src.logsystem import LogConfig, configure, log_event, run_analysis
    from src.logsystem.analyzer import LogAnalyzer

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))
    log_event(logging.INFO, "biz", step="order", status="failed", error_msg="boom")
    run_analysis(str(tmp_path))

    report = LogAnalyzer(str(tmp_path)).analyze()
    # 只有那一条业务失败算错误；auto_analysis 自身是 success
    assert report["error_count"] == 1, report


def test_agent_session_payload_failure_has_reason(tmp_path):
    """载荷式失败（如 empty_model_response）必须带出原因。"""
    fields = _agent_fields(tmp_path, "success",
                           {"status": "failed", "reason": "empty_model_response: x"})
    assert fields["status"] == "failed", fields
    assert "empty_model_response" in (fields.get("error_msg") or ""), fields
