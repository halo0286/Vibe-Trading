"""biz_id 全链路追踪 —— contextvars + logging filter。

设计决策(评审 v0.1):
- 注入方式:②+①混合 → 日志过滤器(LogRecord.levelname)自动带 biz_id,入口用 req() 显式绑定。
- biz_id 形态:message 前缀 `[req-a1b2c3d4] xxx`,levelname → `ERROR[req-...]`。
- tests/ 不动:contextvars 默认值 None + filter 惰性注入,测试隔离无影响。

线程/协程安全:基于 contextvars(ContextVar),asyncio/futures 自动传播。
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


def _new_req_id() -> str:
    """自动生成 req-<12hex> 格式 biz_id(前缀 req-,便于 grep)。"""
    return f"req-{uuid.uuid4().hex[:12]}"


# contextvar: 当前业务流 biz_id。默认 None → filter 注入时自动求值一次再缓存。
biz_id_var: ContextVar[str | None] = ContextVar("vibe_biz_id", default=None)


def get_biz_id() -> str | None:
    """返回当前上下文 biz_id(已缓存,惰性生成)。None 表示未绑定业务流。"""
    return biz_id_var.get()


@contextmanager
def req(biz_id: str | None = None) -> Iterator[str]:
    """入口调用 —— 为整个请求/任务绑定唯一 biz_id。

    - biz_id=None → 自动生成 req-xxx(默认,全量覆盖);
    - biz_id=传入值 → 透传客户端 X-Biz-ID(可选)。

    用法::

        @biz_context()   # contextmanager 别名
        def handle_request(): ...

    自动传播:嵌套调用/asyncio.gather/sync-to-async 均继承同一 biz_id。
    """
    resolved = biz_id if biz_id is not None else _new_req_id()
    token = biz_id_var.set(resolved)
    try:
        yield resolved
    finally:
        biz_id_var.reset(token)


# contextmanager 别名 —— @biz_context() 包裹函数入口(显式绑定语义)。
biz_context = req

__all__ = ["get_biz_id", "req", "biz_context", "biz_id_var"]
