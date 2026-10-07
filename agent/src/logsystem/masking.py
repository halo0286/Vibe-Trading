"""masking.py - 敏感信息脱敏（统一脱敏函数 + logging.Filter）。"""

from __future__ import annotations

import logging
import re
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
    """P0 fix: 用非正则的逐字符扫描替代原正则，消除 ReDoS 风险。

    原正则 (["\']?key["\']?\\s*=\\s*)([^,\\s\\}\\];]+) 中 \\s* 与 [^,\\s...]+
    的组合在恶意输入下可导致指数级回溯。改为线性扫描：找到 key+sep 后，
    从 sep 之后第一个非空白字符开始，到下一个分隔符为止即为值。
    """
    key_lower = key.lower()
    result = []
    i = 0
    n = len(text)
    while i < n:
        # 尝试在位置 i 匹配 key（大小写不敏感，允许前后引号）
        j = i
        # 跳过可选前引号
        if j < n and text[j] in ('"', "'"):
            j += 1
        # 匹配 key，且该 key 必须是键名里的**完整词元**：
        #   前一个字符不能是字母/数字（否则 `monkey` 的 `key` 会误命中）；
        #   后一个字符也不能是字母/数字（否则 `keynote` 会误命中）。
        # 命中后跳过同一标识符串的剩余部分，以支持复合键名
        # （`session_id` 命中 `session` 后需跳过 `_id`）。
        if (j + len(key) <= n
                and text[j:j + len(key)].lower() == key_lower
                and (j == 0 or not text[j - 1].isalnum())
                and (j + len(key) >= n or not text[j + len(key)].isalnum())):
            j += len(key)
            while j < n and (text[j].isalnum() or text[j] in ('_', '-')):
                j += 1
            # 跳过可选后引号
            if j < n and text[j] in ('"', "'"):
                j += 1
            # 跳过空白
            while j < n and text[j] in (' ', '\t'):
                j += 1
            # 匹配 sep
            if j < n and text[j:j + len(sep)] == sep:
                j += len(sep)
                # 跳过 sep 后空白
                while j < n and text[j] in (' ', '\t'):
                    j += 1
                # 保留 key+sep 部分
                result.append(text[i:j])
                # 扫描值：到分隔符为止
                v_start = j
                while j < n and text[j] not in (',', ' ', '\t', '}', ']', ';', '\n', '\r'):
                    j += 1
                result.append(placeholder)
                i = j
                continue
        result.append(text[i])
        i += 1
    return ''.join(result)


def _mask_json(text: str, key: str, placeholder: str) -> str:
    """P0 fix: 用非正则的逐字符扫描替代原正则，消除 ReDoS 风险。

    匹配 "key": "value" 或 "key": value 形态。
    """
    key_lower = key.lower()
    result = []
    i = 0
    n = len(text)
    while i < n:
        # 尝试匹配 "key" / 'key' / key。
        # 必须同时支持单引号：Python 的 dict repr（{'password': 'x'}）用单引号，
        # 而 @log_call 会把位置参数 repr 后送进来 —— 历史缺陷是只认双引号，
        # 于是 `{'password': 'hunter2'}` 完全不被脱敏。
        j = i
        has_quote = False
        quote_char = ""
        if j < n and text[j] in ('"', "'"):
            has_quote = True
            quote_char = text[j]
            j += 1
        if (j + len(key) <= n
                and text[j:j + len(key)].lower() == key_lower
                and (j == 0 or not text[j - 1].isalnum())
                and (j + len(key) >= n or not text[j + len(key)].isalnum())):
            j += len(key)
            # 跳过同一标识符串的剩余部分（复合键名：`user_token` / `session_id`）
            while j < n and (text[j].isalnum() or text[j] in ('_', '-')):
                j += 1
            # 跳过可选后引号
            if j < n and text[j] in ('"', "'"):
                j += 1
            # 跳过空白
            while j < n and text[j] in (' ', '\t'):
                j += 1
            # 匹配冒号
            if j < n and text[j] == ':':
                j += 1
                # 跳过冒号后空白
                while j < n and text[j] in (' ', '\t'):
                    j += 1
                # 保留 key: 部分
                result.append(text[i:j])
                # 值：引号字符串（单/双引号）或非引号到分隔符
                if j < n and text[j] in ('"', "'"):
                    value_quote = text[j]
                    j += 1
                    while j < n:
                        if text[j] == '\\' and j + 1 < n:
                            j += 2
                        elif text[j] == value_quote:
                            j += 1
                            break
                        else:
                            j += 1
                    result.append(placeholder)
                else:
                    # 非引号值：到 , } ] 为止
                    while j < n and text[j] not in (',', '}', ']', '\n', '\r'):
                        j += 1
                    result.append(placeholder)
                i = j
                continue
        result.append(text[i])
        i += 1
    return ''.join(result)


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

#: 承载**自由文本**的字段：可能是异常消息、堆栈或第三方返回体，必须做值级脱敏。
_FREETEXT_FIELDS = frozenset({
    "error_msg", "stack_trace", "exc_text", "reason", "result", "error", "detail",
})


def is_sensitive_key(name: Any, keyset: Any) -> bool:
    """字段名是否敏感：**整体命中，或任一词元整体命中**。

    为什么不用裸子串匹配：``monkey`` 含 ``key`` 会被误脱敏（破坏无关字段），
    而 ``session_id`` / ``api_key_v2`` 这类「敏感词 + 前缀/后缀」反而漏掉。

    为什么**不**把敏感键也切词：``id_card`` 若切出 ``id``，会连
    ``business_id`` / ``order_id`` / ``trade_id`` 一起脱敏，直接摧毁链路追踪。
    因此只切**字段名**，拿切出的词元去和**完整的**敏感键比较。
    """
    if isinstance(keyset, (set, frozenset)):
        lowered = str(name).lower()
        if lowered in keyset:
            return True
        return any(t in keyset for t in _TOKEN_SPLIT.split(lowered) if t)
    keys = {str(k).lower() for k in keyset}
    return is_sensitive_key(name, frozenset(keys))


class MaskFilter(logging.Filter):
    """logging.Filter：对每条日志消息在写入前后动脱敏。

    - 对 msg（str）套用 mask()。
    - 对 record.args 中的敏感映射做键级脱敏。
    """

    def __init__(self, sensitive_keys: Iterable[str], placeholder: str = "***"):
        super().__init__()
        self.keys = list(sensitive_keys)
        self.placeholder = placeholder
        #: 预计算的敏感键集合（避免每条记录、每个处理器重复重建）
        self._keyset = frozenset(str(k).lower() for k in self.keys)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask(record.msg, self.keys, self.placeholder)
        elif record.msg is not None:
            # 非 str 的 msg（dict/list/异常对象）此前完全绕过掩码。
            try:
                record.msg = mask(str(record.msg), self.keys, self.placeholder)
            except Exception:
                pass
        if record.args:
            try:
                record.args = self._mask_args(record.args)
            except Exception:
                pass
        self._mask_exception(record)
        self._mask_extra(record)
        return True

    def _mask_exception(self, record: logging.LogRecord) -> None:
        """异常文本按文本规则脱敏，并写入 ``record.exc_text``。

        修复两个历史缺陷：
        1. ``Formatter.format`` 会把 ``record.exc_text`` **原样打到控制台** ——
           异常消息里的 token/密码因此泄露到 stdout / journald / CI 日志；
        2. ``_record_fields`` 里没有 ``exc_text``，堆栈在**文件**里反而丢失。
        现在统一为「脱敏后的异常文本」，既进控制台也进文件。
        """
        try:
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if isinstance(record.exc_text, str) and record.exc_text:
                record.exc_text = mask(record.exc_text, self.keys, self.placeholder)
        except Exception:
            pass

    def _mask_extra(self, record: logging.LogRecord) -> None:
        """对 extra 注入的字段做键级脱敏。

        必须同时改写两处，否则会漏脱敏：
        1. ``record._logsystem_extra`` 原始字典 —— ``_record_fields`` 直接读它
           来还原任意额外字段（只改 record 属性对此路径无效）。
        2. record 上的属性本身（含 ``ls_`` 前缀别名）—— 命中
           ``_record_fields`` 的固定字段元组时走这条路。
        """
        keyset = self._keyset

        def _hit(name: Any) -> bool:
            return is_sensitive_key(name, keyset)

        extra = getattr(record, "_logsystem_extra", None)
        if isinstance(extra, dict):
            for field in list(extra):
                if not _hit(field):
                    continue
                extra[field] = self.placeholder
                for attr in (field, f"ls_{field}"):
                    if hasattr(record, attr):
                        try:
                            setattr(record, attr, self.placeholder)
                        except Exception:
                            pass
            # 值级脱敏：这些字段承载**自由文本**（异常信息、堆栈、第三方返回体），
            # 键级匹配对它们无效（"error_msg" 本身不是敏感键名），必须按文本
            # 规则把值里的 token/password 等打码。
            for field in list(extra):
                if field not in _FREETEXT_FIELDS or _hit(field):
                    continue
                value = extra[field]
                if isinstance(value, str) and value:
                    masked_value = mask(value, self.keys, self.placeholder)
                    extra[field] = masked_value
                    for attr in (field, f"ls_{field}"):
                        if hasattr(record, attr):
                            try:
                                setattr(record, attr, masked_value)
                            except Exception:
                                pass
        # 兜底：未登记在 _logsystem_extra 里、但直接挂在 record 上的敏感属性
        for field in list(getattr(record, "__dict__", ())):
            if field == "_logsystem_extra" or not _hit(field):
                continue
            try:
                setattr(record, field, self.placeholder)
            except Exception:
                pass

    def _mask_args(self, args: Any) -> Any:
        if isinstance(args, dict):
            return {
                k: (self.placeholder if is_sensitive_key(k, self._keyset) else v)
                for k, v in args.items()
            }
        if isinstance(args, tuple):
            # 注意：元组元素是**值**不是键名，只在它恰好等于某个敏感键名时
            # 才替换（保持既有语义）。真正的 %s 参数泄露见 C-5 的值级脱敏。
            return tuple(
                self.placeholder
                if isinstance(a, str) and is_sensitive_key(a, self._keyset)
                else a
                for a in args
            )
        return args