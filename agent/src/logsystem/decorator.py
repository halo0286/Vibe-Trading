"""decorator.py - 函数级自动日志：入参/出参/异常/耗时的装饰器与上下文管理器。

支持同步函数与 async 函数（自动识别协程）。
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import time
import traceback
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from .logger import log_business_event
from .masking import mask, mask_value
from .trace import get_span_id, new_span, _span_id, _new_span_id

# 当前调用栈（用于记录嵌套调用层级）
_current_call: contextvars.ContextVar[Optional["CallInfo"]] = contextvars.ContextVar(
    "logsystem_current_call", default=None
)


class CallInfo:
    __slots__ = ("name", "parent", "depth", "started_at")

    def __init__(self, name: str, parent: Optional["CallInfo"]) -> None:
        self.name = name
        self.parent = parent
        self.depth = (parent.depth + 1) if parent else 0
        self.started_at = time.perf_counter()


def current_call() -> Optional[CallInfo]:
    return _current_call.get()


def _serialize_args(args: tuple, kwargs: dict, max_len: int, sensitive_keys, placeholder: str) -> str:
    """序列化入参，并对敏感键名的 kwarg 值直接脱敏。"""
    parts = [repr(mask_value(a)) for a in args]
    skey_set = {k.lower() for k in sensitive_keys}
    for k, v in kwargs.items():
        if k.lower() in skey_set:
            parts.append(f"{k}='{placeholder}'")
        else:
            parts.append(f"{k}={mask_value(v)!r}")
    s = ", ".join(parts)
    s = mask(s, sensitive_keys, placeholder)
    if len(s) > max_len:
        s = s[:max_len] + "...<truncated>"
    return s


def log_call(
    logger: Optional[logging.Logger] = None,
    *,
    step: Optional[str] = None,
    log_args: bool = True,
    log_result: bool = True,
    level: int = logging.INFO,
):
    """装饰器：自动记录函数入参、出参、异常、耗时。

    可装饰同步函数与 async 函数。用法：
        @log_call(logger, step="挡板")
        def foo(a, b): ...

    内部的中间状态请使用 log_business_event 或 logger.info 记录，
    它们会自动携带当前 business_id/trace_id/span_id。
    """

    def decorator(func: Callable) -> Callable:
        is_async = inspect.iscoroutinefunction(func)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            loggr = logger or logging.getLogger("logsystem")
            _cfg = _config_of(loggr)
            fn_name = f"{func.__module__}.{func.__qualname__}"
            started = time.perf_counter()
            # P0 fix: 用 token/reset 确保嵌套 @log_call 时外层 span_id 正确恢复
            _token_span = _span_id.set(_new_span_id())
            try:
                args_s = _serialize_args(
                    args, kwargs,
                    getattr(_cfg, "max_args_length", 2048),
                    _cfg.effective_sensitive_keys() if _cfg else (),
                    getattr(_cfg, "mask_placeholder", "***") if _cfg else "***",
                ) if log_args else ""
                if log_args:
                    log_business_event(loggr, logging.DEBUG, f"{fn_name} in", step=step or fn_name, status="start", extra={"args": args_s})
                s = step or fn_name
                try:
                    result = func(*args, **kwargs)
                except Exception as e:
                    cost = (time.perf_counter() - started) * 1000
                    log_business_event(
                        loggr, logging.ERROR,
                        f"{fn_name} failed: {type(e).__name__}: {e}",
                        step=s, status="failed",
                        error_code=getattr(e, "code", None) or type(e).__name__,
                        error_msg=str(e),
                        extra={"cost_ms": round(cost, 3), "exception_type": type(e).__name__,
                               "stack_trace": traceback.format_exc()[:4000]},
                    )
                    raise
                cost = (time.perf_counter() - started) * 1000
                extra = {"cost_ms": round(cost, 3)}
                if log_result:
                    extra["result"] = repr(mask_value(result, 512))
                log_business_event(
                    loggr, level, f"{fn_name} out", step=s, status="success", extra=extra,
                )
                return result
            finally:
                _span_id.reset(_token_span)

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            loggr = logger or logging.getLogger("logsystem")
            _cfg = _config_of(loggr)
            fn_name = f"{func.__module__}.{func.__qualname__}"
            started = time.perf_counter()
            # P0 fix: 用 token/reset 确保嵌套 @log_call 时外层 span_id 正确恢复
            _token_span = _span_id.set(_new_span_id())
            try:
                args_s = _serialize_args(
                    args, kwargs,
                    getattr(_cfg, "max_args_length", 2048),
                    _cfg.effective_sensitive_keys() if _cfg else (),
                    getattr(_cfg, "mask_placeholder", "***") if _cfg else "***",
                ) if log_args else ""
                s = step or fn_name
                if log_args:
                    log_business_event(loggr, logging.DEBUG, f"{fn_name} in", step=s, status="start", extra={"args": args_s})
                try:
                    result = await func(*args, **kwargs)
                except Exception as e:
                    cost = (time.perf_counter() - started) * 1000
                    log_business_event(
                        loggr, logging.ERROR,
                        f"{fn_name} failed: {type(e).__name__}: {e}",
                        step=s, status="failed",
                        error_code=getattr(e, "code", None) or type(e).__name__,
                        error_msg=str(e),
                        extra={"cost_ms": round(cost, 3), "exception_type": type(e).__name__,
                               "stack_trace": traceback.format_exc()[:4000]},
                    )
                    raise
                cost = (time.perf_counter() - started) * 1000
                extra = {"cost_ms": round(cost, 3)}
                if log_result:
                    extra["result"] = repr(mask_value(result, 512))
                log_business_event(
                    loggr, level, f"{fn_name} out", step=s, status="success", extra=extra,
                )
                return result
            finally:
                _span_id.reset(_token_span)

        return async_wrapper if is_async else sync_wrapper

    return decorator


def _config_of(logger: logging.Logger) -> Any:
    """从全局日志系统拿到当前使用的 LogConfig（尽力而为）。"""
    from .logger import _configured

    if _configured is not None:
        return _configured.config
    return None


@contextmanager
def LoggedContext(
    logger: Optional[logging.Logger] = None,
    *,
    step: str = "",
    business_id: Optional[str] = None,
) -> Iterator[dict]:
    """上下文管理器：记录一段业务逻辑的耗时与状态。

    with LoggedContext(logger, step="下单") as ctx:
        ...
        ctx["status"] = "success"
    结束时自动写入耗时与状态。
    """
    from .trace import trace_scope

    loggr = logger or logging.getLogger("logsystem")
    started = time.perf_counter()
    new_span()
    state: dict = {"status": "running"}
    try:
        with trace_scope(business_id=business_id) if business_id else _null():
            yield state
    except Exception as e:
        cost = (time.perf_counter() - started) * 1000
        log_business_event(
            loggr, logging.ERROR,
            f"{step or 'block'} failed: {type(e).__name__}: {e}",
            step=step or "block", status="failed",
            error_code=type(e).__name__, error_msg=str(e),
            extra={"cost_ms": round(cost, 3), "stack_trace": traceback.format_exc()[:4000]},
        )
        raise
    else:
        cost = (time.perf_counter() - started) * 1000
        log_business_event(
            loggr, logging.INFO,
            f"{step or 'block'} finished",
            step=step or "block", status=state.get("status", "success"),
            extra={"cost_ms": round(cost, 3)},
        )


from contextlib import nullcontext as _null