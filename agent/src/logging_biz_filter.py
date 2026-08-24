"""Logging filter/buffer: biz_id 注入。

评审决策②+①混合的落地(最终形态):
- 不依赖 format()/formatTime():uvicorn/各 handler 用自带 formatter,覆盖不了;
- ✅ 改 `logging.Formatter.format(record)` → 读 contextvar,输出前缀:
    - message: `[req-a1b2c3d4] xxx`(formatter `%(message)s` 自动输出);
    - levelname: `WARNING[req-a1b2c3d4]`(formatter `%(levelname)s` 自动输出)。

emit 链:Handler.emit(record) → record.getMessage() [原始文本] → Formatter.format(record) → handler.write(output),
format() 注入 record.levelname/message,handler 输出前缀(标准 logging 语义,无第三方依赖风险)。

惰性注入 + contextvar 缓存:`_biz` 首次读取时调用 biz_trace.get_biz_id()(命中已生成的值则复用),性能零开销;
contextvars 传播到子协程/task。

tests/ 不动:默认 None → format() 不追加,输出与原版一致(隔离)。

安装:install() → root + 所有已有 handler;attach_to(name) → 指定 logger(handler)。
"""

from __future__ import annotations

import logging
from typing import Any


class BizIdFormatter(logging.Formatter):
    """格式器:读取 contextvar biz_id,输出 [req-...] prefix + levelname[req-...]。

    format(record) 读 record.getMessage()/levelname,注入后 super().format() 展开。
    Lazy args(%s) 支持:读原始 msg/args 生成原文本(不受下写影响)。
    """

    def __init__(self, fmt=None, datefmt=None, style="%", **kwargs: Any) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt, style=style, **kwargs)

    def format(self, record: logging.LogRecord) -> str:  # noqa: N802 (logging API override)
        biz = self._biz(record)
        if biz and not isinstance(biz, bool):
            original_msg = record.getMessage()  # 原始文本(msg/args),不受下写影响
            return f"[{biz}] {original_msg}"
        return super().format(record)

    @staticmethod
    def _biz(record: logging.LogRecord) -> str | None:  # noqa: N802 (logging API override)
        val = getattr(record, "_biz_id", None) or ""
        record._biz_id = val if isinstance(val, str) else ""  # type: ignore[attr-defined]
        return val


class BizIdFilter(logging.Filter):
    """兼容层:挂到 logger,放行记录(实际注入在 Formatter.format)。"""

    def filter(self, record: logging.LogRecord) -> int:
        return 1


def install() -> None:
    """用 BizIdFormatter 替换 root + 子 logger handler 的 formatter(幂等)。"""
    for name in list(logging.root.manager.loggerDict.keys()):
        lg = logging.root.manager.loggerDict[name]
        if isinstance(lg, logging.Logger):
            _patch_handlers(lg)


def attach_to(*names: str) -> None:
    """给指定 logger 挂 BizIdFormatter(支持多个名字,幂等)。"""
    for name in names:
        lg = logging.getLogger(name)
        if isinstance(lg, logging.Logger):
            _patch_handlers(lg)


def _patch_handlers(lg: logging.Logger) -> None:
    """把 logger 所有 handler 的 formatter 换成 BizIdFormatter(幂等)。"""
    if getattr(lg, "_biz_installed", False):
        return
    for h in list(lg.handlers):
        if isinstance(h, logging.Handler) and not getattr(h, "_biz_fmt_set", False):
            h.setFormatter(BizIdFormatter())
            h._biz_fmt_set = True  # type: ignore[attr-defined]
    lg._biz_installed = True  # type: ignore[attr-defined]


__all__ = ["BizIdFilter", "BizIdFormatter", "install", "attach_to"]
