"""logsystem_bootstrap.py - Vibe-Trading 集成 task2 logsystem 的统一初始化入口。

把 task2 生产级日志系统（agent/src/logsystem/，纯标准库）接入本项目：
- 终端 + 文件双输出，文件命名 {pid}_{YYYY-MM-DD}_{序号}.log，100MB 滚动
- business_id / trace_id / span_id 全链路追踪（contextvars）
- @log_call 装饰器自动记录入参/出参/异常/耗时
- 敏感信息脱敏、高频限流、业务完成后自动分析闭环

用法：在 api_server / mcp_server 的启动入口（preflight / main）调用一次 init_logging()。
"""

from __future__ import annotations

import functools
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from src.logsystem import (
    LogConfig,
    configure,
    get_logger,
    get_business_id,
    log_call,
    log_event,
    log_business_event,
    trace,
    trace_scope,
    run_analysis,
    run_analysis_async,
    drain_analysis_tasks,
    copy_trace_context,
    register_business_id_resolver,
    resolve_business_id,
)

_inited = False


def init_logging(
    log_dir: Optional[str] = None,
    level: str = "INFO",
    enable_async_analysis: bool = True,
) -> None:
    """初始化日志系统（幂等，可重复调用）。

    log_dir 默认放到项目 data 目录下的 logs/，避免污染源码仓库；
    可用环境变量 VIBE_LOG_DIR 覆盖。
    """
    global _inited
    root = log_dir or os.environ.get("VIBE_LOG_DIR") or str(
        Path(__file__).resolve().parents[2] / "logs"
    )
    configure(
        LogConfig(
            log_dir=root,
            level=level,
            file_format="kv",  # key=value 结构化，便于后续自动分析
            extra_fields={"project": "vibe-trading"},
            enable_async_analysis=enable_async_analysis,
        )
    )
    _inited = True


def project_logger(name: Optional[str] = None) -> logging.Logger:
    return get_logger(f"vibe-trading.{name}" if name else "vibe-trading")


# --------------------------------------------------------------------------- #
# business_id 统一来源（P0-2）
# --------------------------------------------------------------------------- #

_BUSINESS_ID_PROVIDERS: list = []


def register_business_id_provider(provider) -> None:
    """注册 business_id 兜底来源（按注册顺序依次尝试，重复注册幂等）。"""
    if provider not in _BUSINESS_ID_PROVIDERS:
        _BUSINESS_ID_PROVIDERS.append(provider)


def _llm_session_business_id() -> Optional[str]:
    """兜底来源：LLM 会话 ID（一次研究会话 == 一个 business_id）。"""
    try:
        from src.providers.session_context import current_llm_session_id

        return current_llm_session_id() or None
    except Exception:
        return None


register_business_id_provider(_llm_session_business_id)


def current_business_id() -> Optional[str]:
    """统一的 business_id 解析入口（业务代码只应调用本函数）。

    解析顺序：
    1. ``trace_scope(business_id=...)`` 显式设置的值（最高优先级）
    2. 已注册的兜底 provider（默认含 LLM session_id）

    与 ``logsystem.resolve_business_id()`` 等价：provider 链已通过
    ``register_business_id_resolver`` 注册进 logsystem 本体，因此
    **日志落盘路径自身**也走同一套兜底逻辑 —— 埋点无需在每处外层手动
    包 ``trace_scope``，Agent 会话内自动获得 business_id。
    """
    return resolve_business_id()


def _resolve_from_providers() -> Optional[str]:
    """供 logsystem 内部日志路径调用的兜底解析器。"""
    for provider in _BUSINESS_ID_PROVIDERS:
        try:
            value = provider()
        except Exception:
            continue
        if value:
            return value
    return None


# 关键：把 provider 链注册进 logsystem 本体，使 log_business_event /
# log_event / 终端格式化都会自动带上 business_id。
register_business_id_resolver(_resolve_from_providers)


def finish_and_analyze(
    business_id: Optional[str] = None,
    report_path: Optional[str] = None,
    sync: bool = False,
):
    """业务完成后触发自动分析（见 task2 analyzer 闭环）。"""
    bid = business_id or resolve_business_id()
    _dir = os.environ.get("VIBE_LOG_DIR") or str(
        Path(__file__).resolve().parents[2] / "logs"
    )
    report = report_path or os.path.join(_dir, f"analysis_{bid or 'all'}.json")
    if sync:
        return run_analysis(_dir, business_id=bid, report_path=report)
    return run_analysis_async(_dir, business_id=bid, report_path=report)


# --------------------------------------------------------------------------- #
# 埋点原语（P2）
# --------------------------------------------------------------------------- #


def safe_log_event(
    level: int,
    message: str,
    *,
    step: Optional[str] = None,
    status: Optional[str] = None,
    error_code: Optional[str] = None,
    error_msg: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """埋点专用入口：日志失败绝不影响业务主流程。

    交易下单、IM 推送这类关键路径上，日志只能是**旁路**。本函数吞掉一切
    异常（logsystem 未初始化、字段无法序列化、磁盘只读等），保证调用方
    逻辑与返回值完全不受日志影响。

    **被动性约束（重要）**：日志系统未显式初始化时直接丢弃事件，绝不因
    埋点而触发 ``configure()``。否则一次库调用就会改变进程全局状态 ——
    创建 ``./logs``、挂上文件 handler、并激活 ``finish_and_analyze`` 的
    自动分析线程。实测该副作用会污染宿主程序：在测试中表现为与被埋点
    业务无关的随机失败（后台分析线程扫描日志目录产生 I/O 与资源竞争）。

    与 ``log_event`` 的区别：``log_event`` 会正常抛出配置类错误，便于
    开发期发现误用；``safe_log_event`` 用于生产业务路径。
    """
    try:
        from src.logsystem.logger import _configured as _ls_configured

        if _ls_configured is None:
            return  # 未显式初始化 —— 保持被动，不产生任何全局副作用
        log_event(
            level,
            message,
            step=step,
            status=status,
            error_code=error_code,
            error_msg=error_msg,
            extra=extra,
        )
    except Exception:
        pass


#: 明确表示「业务成功」的状态值。**必须用允许清单，而不是否定清单。**
#:
#: 历史缺陷：埋点用 3~4 个词的否定清单（error/failed/rejected）判断成败，
#: 于是 `blocked`（kill-switch / mandate 拒单）、`not_authorized`（OAuth 过期）、
#: `timeout`、`budget_exceeded`、`quote_not_bounded`、`empty`、`fail`、`500`
#: 全部被记成 success —— 最安全敏感的熔断拒单在日志里读起来是成功的实盘下单。
SUCCESS_VERDICTS = frozenset({"success", "ok", "completed", "done"})


def business_outcome(result: Any) -> str:
    """判定业务成败，返回 ``"success"`` 或 ``"failed"``（**唯一**共享分类器）。

    全项目只有这一处分类逻辑，避免各埋点各写一份否定清单而漂移。

    规则：
    - 非 dict，或 dict 中没有 ``status`` 字段：调用未抛异常 → ``success``。
    - 有 ``status`` 字段：仅当取值命中 :data:`SUCCESS_VERDICTS` 才算
      ``success``，其余一律 ``failed``。
    """
    if not isinstance(result, dict):
        return "success"
    raw = result.get("status")
    if raw is None:
        return "success"
    return "success" if str(raw).strip().lower() in SUCCESS_VERDICTS else "failed"


def result_error(result: Any) -> tuple:
    """从错误载荷抽取 ``(error_code, error_msg)``，让失败记录带上原因。"""
    if not isinstance(result, dict):
        return None, None
    code = result.get("error_code") or result.get("status")
    if code is not None:
        code = str(code)
    msg = result.get("error") or result.get("reason") or result.get("error_msg") or ""
    return code, (str(msg)[:200] or None)


def traced_step(step: str, *, summarize=None):
    """通用步骤埋点装饰器（同步函数）。

    场景全覆盖时对「每个场景的核心计算步骤」补埋点，用它避免把
    try/except + 计时 + safe_log_event 这段样板抄 N 遍。

    Args:
        step: 步骤标识，如 ``"factor.bench"``、``"backtest.run"``。
        summarize: 可选 ``(args, kwargs, result) -> dict``，抽取业务摘要字段；
            抛异常时忽略（摘要失败不能影响埋点，更不能影响业务）。

    用法::

        @traced_step("factor.bench", summarize=lambda a, k, r: {"zoo": k.get("zoo")})
        def run_bench(...): ...
    """

    def deco(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 记录后原样抛出
                safe_log_event(
                    logging.WARNING,
                    f"{step}.failed",
                    step=step,
                    status="failed",
                    error_code=type(exc).__name__,
                    error_msg=str(exc)[:200],
                    extra={"cost_ms": int((time.monotonic() - t0) * 1000)},
                )
                raise
            extra = {"cost_ms": int((time.monotonic() - t0) * 1000)}
            if summarize is not None:
                try:
                    extra.update(summarize(args, kwargs, result) or {})
                except Exception:
                    pass
            # 返回错误载荷 ≠ 成功：这类函数（alpha bench / 交易查询等）以
            # dict 形式返回 {"status": ...} 而不抛异常。分类必须走共享的
            # **允许清单**：否定清单会漏掉 blocked / not_authorized / timeout
            # 等真实失败词，把业务失败统计成成功。
            status = business_outcome(result)
            error_code, error_msg = (None, None)
            if status == "failed":
                error_code, error_msg = result_error(result)
            safe_log_event(
                logging.INFO if status == "success" else logging.WARNING,
                f"{step}.{status}",
                step=step,
                status=status,
                error_code=error_code,
                error_msg=error_msg,
                extra=extra,
            )
            return result

        return wrapper

    return deco


def drain_analysis() -> int:
    """等待本进程发起的异步日志分析完成（一次性进程退出前必须调用）。

    CLI 等短命进程若不调用，daemon 分析线程会被进程退出直接杀死，
    「业务完成后自动分析闭环」形同虚设。
    """
    try:
        return drain_analysis_tasks()
    except Exception:
        return 0


__all__ = [
    "init_logging",
    "project_logger",
    "finish_and_analyze",
    "current_business_id",
    "register_business_id_provider",
    "safe_log_event",
    "traced_step",
    "business_outcome",
    "SUCCESS_VERDICTS",
    "result_error",
    "drain_analysis",
    "log_call",
    "log_event",
    "log_business_event",
    "trace_scope",
    "copy_trace_context",
    "LogConfig",
    "configure",
    "get_logger",
]