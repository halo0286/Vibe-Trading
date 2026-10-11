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
    {
        "key": "exc_text",
        "type": "str",
        "required": False,
        "description": "logging 格式化后的异常文本（由 LogRecord.exc_text 还原）。",
        "example": "ValueError: bad symbol",
    },
    {
        "key": "business_done",
        "type": "bool",
        "required": False,
        "description": "业务是否已结束（由 LoggedContext 在退出时写入），供分析器判断链路完整性。",
        "example": True,
    },
    {
        "key": "message",
        "type": "str",
        "required": True,
        "description": "日志正文（由 LogRecord.getMessage() 格式化，始终写入）。",
        "example": "fetch_quote done in 12.3ms",
    },
)

# --------------------------------------------------------------------------- #
# 字段契约：**生产端（logger.py）与消费端（analyzer.py）共用同一份清单**
#
# 为什么单列这组常量：此前生产端在 ``logger.py`` 里内联一个元组、消费端在
# ``analyzer.py`` 里散写若干字符串字面量，两边各写一遍 —— 改名字段时没有
# 任何编译期/测试期报错，消费端会**静默忽略**该字段。实测：生产端产出 14 个
# 业务字段，而分析器只读 8 个，且 KEYWORD_DICT 漏收了 ``exc_text`` /
# ``business_done``（事实标准已漂移）。
#
# 现在两边都从本文件派生，并由
# ``agent/tests/test_logsystem_profile_contracts.py`` 的字段级契约测试守住。
# --------------------------------------------------------------------------- #

#: 基础字段：由 ``_RotatingPidFileHandler._record_fields`` 自动写入。
BASE_FIELDS: tuple[str, ...] = (
    "timestamp",
    "pid",
    "level",
    "module",
    "function",
    "line",
)

#: 业务字段：由装饰器/上下文管理器或 ``log_business_event`` 写入。
#: ⚠️ 修改这里即修改**生产端实际写出**的字段（``logger.py`` 直接引用本常量）。
BUSINESS_FIELDS: tuple[str, ...] = (
    "business_id",
    "trace_id",
    "span_id",
    "step",
    "status",
    "cost_ms",
    "error_code",
    "error_msg",
    "args",
    "result",
    "exception_type",
    "stack_trace",
    "exc_text",
    "business_done",
)

#: 正文字段：始终存在。
MESSAGE_FIELD: str = "message"

#: 生产端会写出的**全部**字段。契约测试的基准集合。
PRODUCED_FIELDS: tuple[str, ...] = BASE_FIELDS + BUSINESS_FIELDS + (MESSAGE_FIELD,)

#: 消费端（``analyzer.py``）**声明消费**的字段。
#: 必须始终保持 ``set(ANALYZED_FIELDS) <= set(PRODUCED_FIELDS)`` —— 由契约测试强制。
#: 分析报告会对照本清单报告"实际读到 / 缺失"的字段，漂移在**运行时也可见**。
ANALYZED_FIELDS: tuple[str, ...] = (
    "level",
    "message",
    "business_id",
    "trace_id",
    "step",
    "status",
    "cost_ms",
    "error_code",
    "error_msg",
    "exception_type",
    "stack_trace",
)

#: ``KEYWORD_DICT`` 覆盖的字段名（事实标准的口径）。
KEYWORD_FIELDS: tuple[str, ...] = tuple(str(d["key"]) for d in KEYWORD_DICT)