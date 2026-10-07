"""trace.py - 全链路追踪上下文（基于 contextvars，多线程/异步安全）。"""

from __future__ import annotations

import contextvars
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional, Callable, Any

# ---- contextvars（跨线程/异步任务自动隔离且可传播） ----
_business_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "logsystem_business_id", default=None
)
_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "logsystem_trace_id", default=None
)
_span_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "logsystem_span_id", default=None
)


@dataclass
class TraceContext:
    """一次业务调用链的追踪上下文快照。"""

    business_id: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        d = {}
        if self.business_id is not None:
            d["business_id"] = self.business_id
        if self.trace_id is not None:
            d["trace_id"] = self.trace_id
        if self.span_id is not None:
            d["span_id"] = self.span_id
        return d


def get_business_id() -> Optional[str]:
    return _business_id.get()


#: business_id 兜底解析器：contextvar 为空时按注册顺序尝试。
#: 存在的意义：``log_business_event`` 是**所有**业务日志的唯一落盘路径，
#: 若它只读 contextvar，则凡是没有显式 ``trace_scope`` 的场景
#: （如 Agent 会话自动绑定的 session_id）都会写成空 business_id，
#: 全链路追踪直接失效。把兜底能力下沉到 logsystem 内部后，
#: 使用方无需在每处埋点外层手动包 trace_scope。
_BUSINESS_ID_RESOLVERS: list = []


def register_business_id_resolver(resolver) -> None:
    """注册 business_id 兜底解析器（重复注册幂等）。"""
    if resolver not in _BUSINESS_ID_RESOLVERS:
        _BUSINESS_ID_RESOLVERS.append(resolver)


def resolve_business_id() -> Optional[str]:
    """解析 business_id：contextvar 优先，其次注册的兜底解析器。"""
    bid = _business_id.get()
    if bid:
        return bid
    for resolver in _BUSINESS_ID_RESOLVERS:
        try:
            value = resolver()
        except Exception:
            continue
        if value:
            return value
    return None


def get_trace_id() -> Optional[str]:
    return _trace_id.get()


def get_span_id() -> Optional[str]:
    return _span_id.get()


def current_trace() -> TraceContext:
    return TraceContext(
        business_id=_business_id.get(),
        trace_id=_trace_id.get(),
        span_id=_span_id.get(),
    )


def _new_trace_id() -> str:
    return "t-" + secrets.token_hex(16)


def _new_span_id() -> str:
    return "s-" + secrets.token_hex(4)


def set_trace_context(
    business_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> None:
    """显式设置追踪上下文（通常无需手动调用，由 trace_scope 管理）。"""
    if business_id is not None:
        _business_id.set(business_id)
    if trace_id is not None:
        _trace_id.set(trace_id)
    else:
        # 若未提供 trace_id 且当前为空，则新建
        if _trace_id.get() is None:
            _trace_id.set(_new_trace_id())
    if span_id is not None:
        _span_id.set(span_id)


@contextmanager
def trace_scope(
    business_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
) -> Iterator[TraceContext]:
    """进入一个业务追踪作用域。

    - 若 business_id 未提供，会尝试继承已有值（外层作用域）。
    - trace_id 未提供则继承或新建。
    - span_id 未提供则新建一个子 span。

    用法：
        with trace_scope(business_id="order_1") as tc:
            ...  # 内部日志自动携带 business_id/trace_id/span_id
    """
    # 保存旧值
    token_b = _business_id.set(business_id if business_id is not None else _business_id.get())
    new_trace = trace_id if trace_id is not None else (_trace_id.get() or _new_trace_id())
    token_t = _trace_id.set(new_trace)
    new_span = span_id if span_id is not None else _new_span_id()
    token_s = _span_id.set(new_span)
    try:
        yield current_trace()
    finally:
        _business_id.reset(token_b)
        _trace_id.reset(token_t)
        _span_id.reset(token_s)


def new_span(span_id: Optional[str] = None) -> str:
    """返回一个新的 span_id 并设置为当前 span（用于子步骤标识）。"""
    sid = span_id or _new_span_id()
    _span_id.set(sid)
    return sid


def copy_trace_context() -> contextvars.Context:
    """捕获当前上下文的一份快照（Context 对象）。

    用于在手动创建线程时传播追踪上下文。contextvars 不会自动向
    ``threading.Thread`` / ``concurrent.futures`` 的工作线程传播，
    需要显式用 ``ctx.run(target)`` 执行目标函数。

    用法：
        ctx = copy_trace_context()
        t = threading.Thread(target=ctx.run, args=(worker, arg))
        t.start()

    注意：``asyncio.Task`` 会自动继承创建时的上下文（无需手动复制）。
    """
    return contextvars.copy_context()


def run_with_context(ctx: contextvars.Context, target: Callable[..., Any], *args, **kwargs) -> Any:
    """在指定上下文 ctx 中执行 target。

    需先调用 ``copy_trace_context()`` 捕获上下文，再传入：
        ctx = copy_trace_context()
        t = threading.Thread(target=run_with_context, args=(ctx, worker, 1))
        t.start()

    等价于 ``ctx.run(target, *args, **kwargs)``，提供显式化语义。
    """
    return ctx.run(target, *args, **kwargs)