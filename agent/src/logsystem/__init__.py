"""logsystem - 生产级日志系统（纯标准库实现）。

提供：
- 终端 + 文件双输出，文件命名 `{pid}_{YYYY-MM-DD}_{序号}.log`，100MB 滚动
- 业务 ID / trace_id / span_id 全链路追踪（基于 contextvars）
- 函数入参/出参/中间状态/异常/耗时自动记录装饰器与上下文管理器
- 敏感信息脱敏（过滤器 + 统一脱敏函数）
- 高频日志采样/限流
- 业务完成后自动日志分析闭环

用法见 README 与 examples/ 目录。
"""

from .config import LogConfig
from .constants import KEYWORD_DICT, SENSITIVE_KEYS_DEFAULT
from .masking import mask, mask_value, MaskFilter
from .trace import (
    TraceContext,
    get_business_id,
    get_trace_id,
    get_span_id,
    set_trace_context,
    trace_scope,
    new_span,
    copy_trace_context,
    run_with_context,
)
from .logger import (
    get_logger,
    configure,
    GlobalLogger,
    log_business_event,
)
from .decorator import log_call, LoggedContext, current_call
from .analyzer import run_analysis, run_analysis_async, LogAnalyzer

__all__ = [
    "LogConfig",
    "KEYWORD_DICT",
    "SENSITIVE_KEYS_DEFAULT",
    "mask",
    "mask_value",
    "MaskFilter",
    "TraceContext",
    "get_business_id",
    "get_trace_id",
    "get_span_id",
    "set_trace_context",
    "trace_scope",
    "new_span",
    "copy_trace_context",
    "run_with_context",
    "get_logger",
    "configure",
    "GlobalLogger",
    "log_business_event",
    "log_call",
    "LoggedContext",
    "current_call",
    "run_analysis",
    "run_analysis_async",
    "LogAnalyzer",
]

__version__ = "1.0.0"