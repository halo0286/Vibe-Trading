"""masking.py - 敏感信息脱敏（统一脱敏函数 + logging.Filter）。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len] + "...<truncated>"


def mask_value(value: Any, max_len: int = 512) -> Any:
    """对任意值做安全处理：截断大对象、脱标明敏感结构。

    - str/int/float/bool/None 直接返回（str 会被截断）。
    - bytes/bytearray 不打印二进制内容。
    - dict/list 递归摘要，避免大对象全量输出。
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>"
    if isinstance(value, bytearray):
        return f"<bytearray len={len(value)}>"
    if isinstance(value, str):
        return _truncate(value, max_len)
    if isinstance(value, dict):
        return {k: mask_value(v, max_len) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:50]
        return type(value).__name__ + "[" + ", ".join(
            str(mask_value(i, max_len)) for i in items
        ) + f"]<len={len(value)}>"
    # 其它对象：只记录类型与长度（避免 __str__ 泄露大对象）
    try:
        ln = len(value)
        return f"<{type(value).__name__} len={ln}>"
    except TypeError:
        return f"<{type(value).__name__}>"


def mask(text: str, sensitive_keys: Iterable[str], placeholder: str = "***") -> str:
    """对 key=value / key: value 形态文本中的敏感键对应值做脱敏。

    采用子串匹配键名（大小写不敏感），命中则把其后的值替换为 placeholder。
    覆盖 JSON、key=value、dict repr 等常见文本形态。
    """
    keys = [k.lower() for k in sensitive_keys]
    if not text or not keys:
        return text
    out = text
    for k in keys:
        # key=value
        out = _mask_kv(out, k, placeholder, sep="=")
        # "key": "value" 或 "key": value
        out = _mask_json(out, k, placeholder)
    return out


def _mask_kv(text: str, key: str, placeholder: str, sep: str) -> str:
    import re

    # 匹配 key（可选引号）= 值（直到空格/逗号/换行/右括号）
    pattern = re.compile(
        r'(?i)(["\']?' + re.escape(key) + r'["\']?\s*' + re.escape(sep)
        + r'\s*)([^,\s\}\];]+)'
    )
    return pattern.sub(lambda m: m.group(1) + placeholder, text)


def _mask_json(text: str, key: str, placeholder: str) -> str:
    import re

    # 匹配 "key": "value" 或 "key": value
    pattern = re.compile(
        r'(?i)("?' + re.escape(key) + r'"?\s*:\s*)("[^"]*"|[^,}\]]+)'
    )
    return pattern.sub(lambda m: m.group(1) + placeholder, text)


class MaskFilter(logging.Filter):
    """logging.Filter：对每条日志消息在写入前后动脱敏。

    - 对 msg（str）套用 mask()。
    - 对 record.args 中的敏感映射做键级脱敏。
    """

    def __init__(self, sensitive_keys: Iterable[str], placeholder: str = "***"):
        super().__init__()
        self.keys = list(sensitive_keys)
        self.placeholder = placeholder

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask(record.msg, self.keys, self.placeholder)
        if record.args:
            try:
                record.args = self._mask_args(record.args)
            except Exception:
                pass
        # 对 extra 注入的字段也做脱敏
        for field in list(getattr(record, "_logsystem_extra", {}) or {}):
            val = getattr(record, field, None)
            if isinstance(val, str) and field.lower() in {k.lower() for k in self.keys}:
                setattr(record, field, self.placeholder)
        return True

    def _mask_args(self, args: Any) -> Any:
        if isinstance(args, dict):
            return {
                k: (self.placeholder if str(k).lower() in {x.lower() for x in self.keys} else v)
                for k, v in args.items()
            }
        if isinstance(args, tuple):
            return tuple(
                self.placeholder if isinstance(a, str) and a.lower() in {x.lower() for x in self.keys} else a
                for a in args
            )
        return args