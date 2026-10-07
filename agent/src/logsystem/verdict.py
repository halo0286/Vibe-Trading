"""verdict.py - 埋点状态判定与安全字符串化（logsystem 领域，不依赖应用层）。

这些助手原先定义在应用层的 ``logsystem_bootstrap`` 里，导致**只用 logsystem
或只用 ToolRegistry 的嵌入方**被迫导入应用层模块（后者还会注册全局
business_id 解析器）。状态判定属于日志领域，放在这里即可解除该耦合。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

__all__ = ["SUCCESS_VERDICTS", "business_outcome", "result_error", "safe_str"]




#: 明确表示「业务成功」的状态值。**必须用允许清单，而不是否定清单。**
#:
#: 历史缺陷：埋点用 3~4 个词的否定清单（error/failed/rejected）判断成败，
#: 于是 `blocked`（kill-switch / mandate 拒单）、`not_authorized`（OAuth 过期）、
#: `timeout`、`budget_exceeded`、`quote_not_bounded`、`empty`、`fail`、`500`
#: 全部被记成 success —— 最安全敏感的熔断拒单在日志里读起来是成功的实盘下单。
SUCCESS_VERDICTS = frozenset({"success", "ok", "completed", "done"})


def business_outcome(result: Any) -> str:
    """判定业务成败，返回 ``"success"`` 或 ``"failed"``（**唯一**共享分类器）。

    全项目只有这一处分类逻辑，避免各埋点各写一份否定清单而漂移。

    规则：
    - 非 dict，或 dict 中没有 ``status`` 字段：调用未抛异常 → ``success``。
    - 有 ``status`` 字段：仅当取值命中 :data:`SUCCESS_VERDICTS` 才算
      ``success``，其余一律 ``failed``。
    """
    if not isinstance(result, dict):
        return "success"
    raw = result.get("status")
    if raw is None:
        return "success"
    return "success" if str(raw).strip().lower() in SUCCESS_VERDICTS else "failed"


def result_error(result: Any) -> tuple:
    """从错误载荷抽取 ``(error_code, error_msg)``，让失败记录带上原因。"""
    if not isinstance(result, dict):
        return None, None
    code = result.get("error_code") or result.get("status")
    if code is not None:
        code = str(code)
    msg = result.get("error") or result.get("reason") or result.get("error_msg") or ""
    return code, (str(msg)[:200] or None)


def safe_str(value: Any, limit: int = 200) -> str:
    """把任意对象安全转成字符串（**绝不抛异常**）。

    埋点里直接 ``str(exc)`` 是危险的：异常类可以重写 ``__str__`` 并让它抛错，
    于是**日志代码会顶替掉真正的业务异常**（实测：``Bad.__str__`` 抛错时，
    调用方收到的是 ``str() exploded`` 而不是 ``Bad``）。埋点必须只能旁路。
    """
    try:
        return str(value)[:limit]
    except Exception:
        try:
            return type(value).__name__
        except Exception:
            return "<unstringifiable>"
