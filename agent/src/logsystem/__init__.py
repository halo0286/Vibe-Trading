"""logsystem - 生产级日志系统（纯标准库实现）。

提供：
- 终端 + 文件双输出，文件命名 `{pid}_{YYYY-MM-DD}_{序号}.log`，100MB 滚动
- 业务 ID / trace_id / span_id 全链路追踪（基于 contextvars）
- 函数入参/出参/中间状态/异常/耗时自动记录装饰器与上下文管理器
- 敏感信息脱敏（过滤器 + 统一脱敏函数）
- 高频日志采样/限流
- 业务完成后自动日志分析闭环

API 分层（P1 收敛）
------------------
``__all__`` 只暴露 **8 个核心 API**（``CORE_API``），这是业务接入需要的全部内容::

    from logsystem import LogConfig, configure, get_logger, log_event, \\
        log_call, trace_scope, get_business_id, run_analysis

其余为 ``ADVANCED_API``（高级/底层）：**仍然完全可导入**（``from logsystem import mask``
照常工作），只是不再出现在 ``import *`` 与推荐用法中。它们服务于二次开发、
自定义 handler、单元测试等场景，普通业务不需要。

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
    register_business_id_resolver,
    resolve_business_id,
)
from .logger import (
    get_logger,
    configure,
    GlobalLogger,
    log_event,
    log_business_event,
)
from .decorator import log_call, LoggedContext, current_call
from .analyzer import run_analysis, run_analysis_async, drain_analysis_tasks, LogAnalyzer

#: 核心 API —— 业务接入只需这 8 个。
#: 覆盖「配置 → 取 logger → 记事件 → 记调用 → 串链路 → 读业务 ID → 出分析报告」闭环。
CORE_API = (
    "LogConfig",       # 配置对象（含 dev/prod/test 预设）
    "configure",       # 初始化 / 重配置全局日志系统
    "get_logger",      # 获取命名 logger
    "log_event",       # 记录业务事件（免传 logger，推荐入口）
    "log_call",        # 装饰器：自动记录入参/出参/异常/耗时
    "trace_scope",     # 上下文管理器：绑定 business_id / trace_id
    "get_business_id", # 读取当前业务 ID（用于回填到响应/产物）
    "run_analysis",    # 业务完成后产出分析报告
)

#: 高级 API —— 完全可导入，供二次开发/测试使用，不进入 ``__all__``。
ADVANCED_API = (
    # 兼容入口（历史项目在用，保留可导入）
    "log_business_event",
    # 底层 trace 控制
    "register_business_id_resolver",
    "resolve_business_id",
    "TraceContext",
    "get_trace_id",
    "get_span_id",
    "set_trace_context",
    "new_span",
    "copy_trace_context",
    "run_with_context",
    # 脱敏原语
    "mask",
    "mask_value",
    "MaskFilter",
    "SENSITIVE_KEYS_DEFAULT",
    "KEYWORD_DICT",
    # 内部实现
    "GlobalLogger",
    "LoggedContext",
    "current_call",
    "run_analysis_async",
    "drain_analysis_tasks",
    "LogAnalyzer",
)

__all__ = list(CORE_API) + ["CORE_API", "ADVANCED_API"]

__version__ = "1.0.0"