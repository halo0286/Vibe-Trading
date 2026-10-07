"""BaseTool + ToolRegistry: tool infrastructure."""

from __future__ import annotations

import json
import logging
import re
import time
import traceback
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# task3: 业务日志覆盖（收敛点）
# ---------------------------------------------------------------------------
# ToolRegistry.execute() 是所有 MCP 工具调用的唯一入口。在此处注入日志，
# 可以一次性覆盖全部 64 个工具业务的入参/出参/异常/耗时，符合 task3 对
# 超大框架"收敛点单点覆盖"的策略。所有日志调用都包裹在 try/except 中，
# 保证日志系统故障绝不影响业务执行。

_MAX_RESULT_LOG_CHARS = 512


def _summarize_tool_result(result: Any) -> str:
    """将工具返回值压缩为安全摘要，避免大对象全量 JSON 入日志。"""
    try:
        if result is None:
            return "<None>"
        if isinstance(result, str):
            if len(result) <= _MAX_RESULT_LOG_CHARS:
                return result
            return result[:_MAX_RESULT_LOG_CHARS] + "...<truncated>"
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) <= _MAX_RESULT_LOG_CHARS:
            return text
        return text[:_MAX_RESULT_LOG_CHARS] + "...<truncated>"
    except Exception:
        return f"<{type(result).__name__}>"


def _mask_tool_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """对工具入参做脱敏，密码/token/密钥等敏感键值替换为占位符。"""
    try:
        from src.logsystem.masking import mask_value
        from src.logsystem.constants import SENSITIVE_KEYS_DEFAULT

        sensitive = {k.lower() for k in SENSITIVE_KEYS_DEFAULT}
        out: Dict[str, Any] = {}
        for key, value in params.items():
            if str(key).lower() in sensitive:
                out[key] = "***"
            else:
                out[key] = mask_value(value, 256)
        return out
    except Exception:
        # 脱敏组件不可用时，宁可不记录参数也不要泄露
        return {"<redacted>": f"{len(params)} params"}


#: 超过该长度的工具结果不做全量解析：仅为判定成败而解析多 MB 载荷
#: 实测代价过高（1.15MB→3ms、64MB→351ms）。
_OUTCOME_PARSE_LIMIT = 64 * 1024

_STATUS_IN_TEXT = re.compile(r'"status"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"')


def _tool_outcome(result: Any) -> str:
    """判定工具调用业务成败，返回 ``"success"`` / ``"failed"``。

    复用 logsystem 的**共享允许清单**（``business_outcome``），不再自写一份
    否定清单 —— 历史缺陷是只认 error/failed/rejected/failure 四个词，于是
    ``timeout`` / ``budget_exceeded`` / ``quote_not_bounded`` / ``empty`` /
    ``fail`` / ``500`` 全部被记成 success。

    性能：结果不超过 64 KiB 时精确解析；超过时只对**有界前缀**做尽力判定
    （与落盘时使用的截断窗口一致），避免为读一个字段解析整个大载荷。
    解析失败一律按 success 处理 —— 埋点分类绝不能反过来影响业务判定。
    """
    try:
        from src.logsystem_bootstrap import SUCCESS_VERDICTS, business_outcome
    except Exception:  # pragma: no cover - 引导失败时退回保守判定
        return "success"

    if not isinstance(result, str):
        return "success"
    try:
        if len(result) <= _OUTCOME_PARSE_LIMIT:
            return business_outcome(json.loads(result))
    except Exception:
        return "success"
    # 超大结果：有界前缀尽力判定
    match = _STATUS_IN_TEXT.search(result[:_OUTCOME_PARSE_LIMIT])
    if match and match.group(1).strip().lower() not in SUCCESS_VERDICTS:
        return "failed"
    return "success"


def _log_tool_call(
    name: str,
    params: Dict[str, Any],
    started: float,
    status: str,
    *,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    """记录一次工具业务调用（入参/出参/异常/耗时）。失败时静默。"""
    try:
        # 注意：必须使用 logsystem 自己的 logger（其 handler 挂在 "logsystem"
        # 命名空间下）。若传入项目自有 logger，日志会走 root 而无法落到
        # {pid}_{date}_{seq}.log，造成静默丢失。
        from src.logsystem import (
            get_business_id,
            get_logger,
            log_business_event,
            trace_scope,
        )

        _logsystem_logger = get_logger("tools")
        cost_ms = (time.perf_counter() - started) * 1000.0
        extra: Dict[str, Any] = {
            "cost_ms": round(cost_ms, 3),
            "args": json.dumps(_mask_tool_params(params), ensure_ascii=False, default=str),
        }
        if status == "success":
            extra["result"] = _summarize_tool_result(result)

        error_code = None
        error_msg = None
        if error is not None:
            error_code = type(error).__name__
            error_msg = str(error)[:500]
            extra["exception_type"] = type(error).__name__
            extra["stack_trace"] = traceback.format_exc()[:4000]

        # P0-2：统一走 logsystem_bootstrap.current_business_id()
        # （trace_scope 优先，其次 LLM session_id 兜底），避免调用点各自兜底。
        try:
            from src.logsystem_bootstrap import current_business_id

            _bid = current_business_id()
        except Exception:
            _bid = get_business_id()

        import contextlib

        _scope = trace_scope(business_id=_bid) if _bid else contextlib.nullcontext()
        with _scope:
            log_business_event(
                _logsystem_logger,
                logging.ERROR if error is not None else logging.INFO,
                f"tool {name} {status}",
                step=f"tool.{name}",
                status=status,
                error_code=error_code,
                error_msg=error_msg,
                extra=extra,
            )
    except Exception:
        # task3 约束：日志系统故障不得影响业务执行
        pass


class BaseTool(ABC):
    """Tool base class.

    Attributes:
        name: Unique tool identifier.
        description: Tool description shown to the LLM.
        parameters: Parameter definition in JSON Schema format.
        repeatable: Whether the tool may be called more than once.
        replay_after_compaction: Whether an exact successful readonly call may
            restore its run-scoped result after compaction removed the visible
            payload, even when the tool is otherwise repeatable. Tools opting
            in must provide an explicit freshness argument when a caller needs
            to bypass replay (for example ``no_cache=True``).
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}
    repeatable: bool = False
    is_readonly: bool = True
    # Pure, side-effect-free computation: identical args always produce the
    # same result, so the loop may serve repeated identical calls from cache
    # instead of re-executing them (financial_rigor calc/verify, etc.).
    deterministic: bool = False
    # Mutable/read-through tools can stay repeatable during normal operation
    # while opting into replay only when compaction has removed an exact prior
    # successful result. The loop still bypasses replay for explicit freshness
    # requests such as ``no_cache=True``.
    replay_after_compaction: bool = False

    @classmethod
    def check_available(cls) -> bool:
        """Check if this tool's dependencies are met.

        Override in subclasses to check for API keys, packages, etc.
        Tools that return False are excluded from the registry.
        """
        return True

    @abstractmethod
    def execute(self, **kwargs: Any) -> str:
        """Execute the tool and return a JSON string."""

    def to_openai_schema(self) -> Dict[str, Any]:
        """Convert to OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}, "required": []},
            },
        }


class ToolRegistry:
    """Tool registry."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}
        self._import_failures: Dict[str, str] = {}
        self._registration_failures: Dict[str, str] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        """Retrieve a tool by name."""
        return self._tools.get(name)

    def get_definitions(self) -> List[Dict[str, Any]]:
        """Return all tools in OpenAI function calling format."""
        return [t.to_openai_schema() for t in self._tools.values()]

    def record_import_failure(self, module_name: str, reason: str) -> None:
        """Record a tool module that could not be imported during discovery."""
        self._import_failures[module_name] = reason

    def record_registration_failure(self, tool_name: str, reason: str) -> None:
        """Record a discovered tool that could not be instantiated."""
        self._registration_failures[tool_name] = reason

    def _unavailable_reason(self, name: str) -> tuple[str, str] | None:
        registration_reason = self._registration_failures.get(name)
        if registration_reason is not None:
            return name, registration_reason

        for module_name, reason in self._import_failures.items():
            expected_tool_name = module_name.removesuffix("_tool")
            if name in {module_name, expected_tool_name}:
                return module_name, reason
        return None

    def execute(self, name: str, params: Dict[str, Any]) -> str:
        """Execute a tool and guarantee a valid JSON return value."""
        tool = self._tools.get(name)
        if not tool:
            unavailable = self._unavailable_reason(name)
            if unavailable is not None:
                source, reason = unavailable
                return json.dumps(
                    {
                        "status": "error",
                        "error": (
                            f"Tool '{name}' is unavailable because '{source}' failed "
                            f"during registry construction: {reason}"
                        ),
                        "registry_incomplete": True,
                        "failed_source": source,
                    },
                    ensure_ascii=False,
                )

            payload: Dict[str, Any] = {
                "status": "error",
                "error": f"Tool '{name}' not found",
            }
            failure_count = len(self._import_failures) + len(
                self._registration_failures
            )
            if failure_count:
                payload["registry_incomplete"] = True
                payload["registry_failure_count"] = failure_count
                payload["error"] += (
                    f"; the registry is incomplete because {failure_count} tool "
                    "source(s) failed during startup"
                )
            return json.dumps(payload, ensure_ascii=False)
        try:
            # task3: 业务日志覆盖 —— 记录入参/异常/耗时
            _started = time.perf_counter()
            _result = tool.execute(**params)
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            _log_tool_call(name, params, _started, "failed", error=exc)
            return json.dumps({
                "status": "error", "tool": name,
                "error": str(exc),
            }, ensure_ascii=False)
        # task3: 业务日志覆盖 —— 记录出参/耗时
        #
        # 工具以 JSON 字符串返回业务结果，**不抛异常**也可能表示业务失败：
        # 例如 alpha_zoo 返回 {"status": "error", "error": "..."}。
        # 早期实现无条件记 success，会把业务失败统计成成功，使按 status
        # 聚合的分析/告警失真（task4 维度 2/4 会直接暴露这一点）。
        _log_tool_call(
            name, params, _started, _tool_outcome(_result), result=_result
        )
        return _result

    @property
    def tool_names(self) -> List[str]:
        return list(self._tools.keys())

    @property
    def import_failures(self) -> Dict[str, str]:
        """Return a copy of module import failures captured during discovery."""
        return dict(self._import_failures)

    @property
    def registration_failures(self) -> Dict[str, str]:
        """Return a copy of tool instantiation failures captured during build."""
        return dict(self._registration_failures)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
