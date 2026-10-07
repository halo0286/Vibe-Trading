"""logsystem_bootstrap.py - Vibe-Trading 集成 task2 logsystem 的统一初始化入口。

把 task2 生产级日志系统（agent/src/logsystem/，纯标准库）接入本项目：
- 终端 + 文件双输出，文件命名 {pid}_{YYYY-MM-DD}_{序号}.log，100MB 滚动
- business_id / trace_id / span_id 全链路追踪（contextvars）
- @log_call 装饰器自动记录入参/出参/异常/耗时
- 敏感信息脱敏、高频限流、业务完成后自动分析闭环

用法：在 api_server / mcp_server 的启动入口（preflight / main）调用一次 init_logging()。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from src.logsystem import (
    LogConfig,
    configure,
    get_logger,
    get_business_id,
    log_call,
    log_event,
    log_business_event,
    trace,
    trace_scope,
    run_analysis,
    run_analysis_async,
    copy_trace_context,
)

_inited = False


def init_logging(
    log_dir: Optional[str] = None,
    level: str = "INFO",
    enable_async_analysis: bool = True,
) -> None:
    """初始化日志系统（幂等，可重复调用）。

    log_dir 默认放到项目 data 目录下的 logs/，避免污染源码仓库；
    可用环境变量 VIBE_LOG_DIR 覆盖。
    """
    global _inited
    root = log_dir or os.environ.get("VIBE_LOG_DIR") or str(
        Path(__file__).resolve().parents[2] / "logs"
    )
    configure(
        LogConfig(
            log_dir=root,
            level=level,
            file_format="kv",  # key=value 结构化，便于后续自动分析
            extra_fields={"project": "vibe-trading"},
            enable_async_analysis=enable_async_analysis,
        )
    )
    _inited = True


def project_logger(name: Optional[str] = None) -> logging.Logger:
    return get_logger(f"vibe-trading.{name}" if name else "vibe-trading")


# --------------------------------------------------------------------------- #
# business_id 统一来源（P0-2）
# --------------------------------------------------------------------------- #

_BUSINESS_ID_PROVIDERS: list = []


def register_business_id_provider(provider) -> None:
    """注册 business_id 兜底来源（按注册顺序依次尝试，重复注册幂等）。"""
    if provider not in _BUSINESS_ID_PROVIDERS:
        _BUSINESS_ID_PROVIDERS.append(provider)


def _llm_session_business_id() -> Optional[str]:
    """兜底来源：LLM 会话 ID（一次研究会话 == 一个 business_id）。"""
    try:
        from src.providers.session_context import current_llm_session_id

        return current_llm_session_id() or None
    except Exception:
        return None


register_business_id_provider(_llm_session_business_id)


def current_business_id() -> Optional[str]:
    """统一的 business_id 解析入口（业务代码只应调用本函数）。

    解析顺序：
    1. ``trace_scope(business_id=...)`` 显式设置的值（最高优先级）
    2. 已注册的兜底 provider（默认含 LLM session_id）

    把「logsystem 原生上下文」与「本项目特有来源」收敛到一处，
    避免各调用点各自 import 私有模块、各自兜底。
    """
    bid = get_business_id()
    if bid:
        return bid
    for provider in _BUSINESS_ID_PROVIDERS:
        try:
            value = provider()
        except Exception:
            continue
        if value:
            return value
    return None


def finish_and_analyze(
    business_id: Optional[str] = None,
    report_path: Optional[str] = None,
    sync: bool = False,
):
    """业务完成后触发自动分析（见 task2 analyzer 闭环）。"""
    from src.logsystem import get_business_id

    bid = business_id or get_business_id()
    _dir = os.environ.get("VIBE_LOG_DIR") or str(
        Path(__file__).resolve().parents[2] / "logs"
    )
    report = report_path or os.path.join(_dir, f"analysis_{bid or 'all'}.json")
    if sync:
        return run_analysis(_dir, business_id=bid, report_path=report)
    return run_analysis_async(_dir, business_id=bid, report_path=report)


# --------------------------------------------------------------------------- #
# 埋点原语（P2）
# --------------------------------------------------------------------------- #


def safe_log_event(
    level: int,
    message: str,
    *,
    step: Optional[str] = None,
    status: Optional[str] = None,
    error_code: Optional[str] = None,
    error_msg: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """埋点专用入口：日志失败绝不影响业务主流程。

    交易下单、IM 推送这类关键路径上，日志只能是**旁路**。本函数吞掉一切
    异常（logsystem 未初始化、字段无法序列化、磁盘只读等），保证调用方
    逻辑与返回值完全不受日志影响。

    与 ``log_event`` 的区别：``log_event`` 会正常抛出配置类错误，便于
    开发期发现误用；``safe_log_event`` 用于生产业务路径。
    """
    try:
        log_event(
            level,
            message,
            step=step,
            status=status,
            error_code=error_code,
            error_msg=error_msg,
            extra=extra,
        )
    except Exception:
        pass


__all__ = [
    "init_logging",
    "project_logger",
    "finish_and_analyze",
    "current_business_id",
    "register_business_id_provider",
    "safe_log_event",
    "log_call",
    "log_event",
    "log_business_event",
    "trace_scope",
    "copy_trace_context",
    "LogConfig",
    "configure",
    "get_logger",
]