"""constants.py - 关键字字典表与默认脱敏键定义。

关键字字典表是日志结构化字段的事实标准，文本以 KEYWORD_DICT 提供结构化说明。
"""

from __future__ import annotations

# 默认敏感键（小写子串匹配）；可被 LogConfig 白名单/自定义规则覆盖。
SENSITIVE_KEYS_DEFAULT: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "key",
    "id_card",
    "bank_card",
    "authorization",
    "cookie",
    "apikey",
    "api_key",
    "access_token",
    "refresh_token",
    "session",
)

# 关键字字典表：每个关键字的 name/type/required/description/example。
KEYWORD_DICT: tuple[dict, ...] = (
    {
        "key": "business_id",
        "type": "str",
        "required": True,
        "description": "业务唯一标识，贯穿全链路追踪（订单/任务/策略/请求 ID 等）。",
        "example": "order_20260925_000123",
    },
    {
        "key": "trace_id",
        "type": "str",
        "required": False,
        "description": "一次端到端分布式调用链的唯一标识，多个 span 共享。",
        "example": "t-6f9a2c1b3d4e5f60",
    },
    {
        "key": "span_id",
        "type": "str",
        "required": False,
        "description": "调用链中某个调用片段（函数/步骤）的唯一标识。",
        "example": "s-00c1a2b3",
    },
    {
        "key": "step",
        "type": "str|int",
        "required": False,
        "description": "业务步骤或阶段名称/序号，用于标记流转位置。",
        "example": "fetch_quote",
    },
    {
        "key": "status",
        "type": "str",
        "required": False,
        "description": "业务状态：success/failed/running/unknown 等。",
        "example": "success",
    },
    {
        "key": "cost_ms",
        "type": "float",
        "required": False,
        "description": "耗时（毫秒），由装饰器/上下文管理器自动写入。",
        "example": 12.34,
    },
    {
        "key": "error_code",
        "type": "str|int",
        "required": False,
        "description": "错误码，异常时写入。",
        "example": "E_HTTP_502",
    },
    {
        "key": "error_msg",
        "type": "str",
        "required": False,
        "description": "错误信息（已脱敏）。",
        "example": "upstream timeout",
    },
    {
        "key": "timestamp",
        "type": "str",
        "required": True,
        "description": "ISO8601 时间戳（自动写入）。",
        "example": "2026-09-25T10:00:00.123456Z",
    },
    {
        "key": "pid",
        "type": "int",
        "required": True,
        "description": "进程 ID（自动写入）。",
        "example": 12345,
    },
    {
        "key": "level",
        "type": "str",
        "required": True,
        "description": "日志级别：DEBUG/INFO/WARNING/ERROR/CRITICAL。",
        "example": "INFO",
    },
    {
        "key": "module",
        "type": "str",
        "required": False,
        "description": "模块名（自动写入）。",
        "example": "app.service.quote",
    },
    {
        "key": "function",
        "type": "str",
        "required": False,
        "description": "函数名（自动写入）。",
        "example": "fetch_quote",
    },
    {
        "key": "line",
        "type": "int",
        "required": False,
        "description": "源码行号（自动写入）。",
        "example": 42,
    },
    {
        "key": "args",
        "type": "str",
        "required": False,
        "description": "函数入参（脱敏后，由装饰器写入）。",
        "example": "symbol=AAPL, limit=10",
    },
    {
        "key": "result",
        "type": "str",
        "required": False,
        "description": "函数返回值摘要（脱敏后，由装饰器写入）。",
        "example": "<list len=10>",
    },
    {
        "key": "exception_type",
        "type": "str",
        "required": False,
        "description": "异常类型全名。",
        "example": "ValueError",
    },
    {
        "key": "stack_trace",
        "type": "str",
        "required": False,
        "description": "异常堆栈（脱敏后）。",
        "example": "Traceback (most recent call last): ...",
    },
)