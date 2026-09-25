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
    log_call,
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


__all__ = [
    "init_logging",
    "project_logger",
    "finish_and_analyze",
    "log_call",
    "log_business_event",
    "trace_scope",
    "copy_trace_context",
    "LogConfig",
    "configure",
    "get_logger",
]