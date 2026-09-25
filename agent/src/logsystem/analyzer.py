"""analyzer.py - 业务完成后的自动日志分析闭环。

- run_analysis / run_analysis_async：对指定 business_id 或 trace_id 关联的日志做结构化分析。
- LogAnalyzer：从日志文件（JSON Lines / key=value）提取字段，统计耗时/异常/错误码分布/慢步骤/高频日志，输出报告。
- 分析结果可写回日志、写入 JSON 文件、发消息队列（预留接口）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .logger import get_logger, log_business_event

_analysis_tasks: Dict[str, threading.Thread] = {}


class LogAnalyzer:
    """解析日志文件并产出结构化分析结果。"""

    def __init__(self, log_dir: str, file_format: str = "kv") -> None:
        self.log_dir = Path(log_dir)
        self.file_format = file_format

    def _parse_kv(self, line: str) -> Dict[str, Any]:
        # 简单 key=value 解析（含引号包裹值）
        fields: Dict[str, Any] = {}
        pattern = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:\\.|[^"])*"|\S+)')
        for m in pattern.finditer(line):
            k, v = m.group(1), m.group(2)
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            fields[k] = self._coerce(v)
        return fields

    def _parse_json(self, line: str) -> Dict[str, Any]:
        try:
            obj = json.loads(line)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _coerce(v: str) -> Any:
        try:
            return int(v)
        except ValueError:
            pass
        try:
            return float(v)
        except ValueError:
            pass
        return v

    def _read_fields(self, business_id: Optional[str], trace_id: Optional[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for f in sorted(self.log_dir.glob("*.log")):
            try:
                with open(f, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        rec = self._parse_json(line) if self.file_format == "json" else self._parse_kv(line)
                        if not rec:
                            continue
                        if business_id and rec.get("business_id") != business_id:
                            continue
                        if trace_id and rec.get("trace_id") != trace_id:
                            continue
                        rows.append(rec)
            except OSError:
                continue
        return rows

    def analyze(self, business_id: Optional[str] = None, trace_id: Optional[str] = None) -> Dict[str, Any]:
        rows = self._read_fields(business_id, trace_id)

        total = len(rows)
        if total == 0:
            return {
                "scope": {"business_id": business_id, "trace_id": trace_id},
                "total_logs": 0,
                "status": "empty",
                "summary": "未找到匹配日志",
            }

        # 耗时统计
        costs = [r["cost_ms"] for r in rows if isinstance(r.get("cost_ms"), (int, float))]
        # 异常统计
        errors = [r for r in rows if r.get("level") in ("ERROR", "CRITICAL") or r.get("status") == "failed"]
        # 错误码分布
        err_codes = Counter(r.get("error_code") for r in errors if r.get("error_code"))
        # 状态分布
        statuses = Counter(r.get("status") for r in rows if r.get("status"))
        # 步骤耗时（每 step 聚合）
        step_costs: Dict[str, List[float]] = defaultdict(list)
        for r in rows:
            step = r.get("step")
            c = r.get("cost_ms")
            if step and isinstance(c, (int, float)):
                step_costs[str(step)].append(c)
        slow_steps = [
            {"step": k, "avg_ms": round(sum(v) / len(v), 3), "max_ms": round(max(v), 3), "count": len(v)}
            for k, v in step_costs.items()
            if (sum(v) / len(v)) > 100  # 慢步骤阈值 100ms
        ]
        slow_steps.sort(key=lambda x: x["max_ms"], reverse=True)
        # 高频日志（message 出现次数）
        msg_counter = Counter(r.get("message", "") for r in rows)
        high_freq = [{"message": m, "count": c} for m, c in msg_counter.most_common(10) if c > 5]

        max_cost = max(costs) if costs else 0.0
        avg_cost = (sum(costs) / len(costs)) if costs else 0.0

        # 优化建议
        suggestions = []
        top_err_codes = err_codes.most_common(5)
        if errors:
            suggestions.append(f"存在 {len(errors)} 条异常日志，建议优先处理错误码分布：{dict(top_err_codes)}")
        if slow_steps:
            suggestions.append(f"发现 {len(slow_steps)} 个慢步骤（>100ms），建议优化：{', '.join(s['step'] for s in slow_steps[:3])}")
        if high_freq:
            suggestions.append(f"存在高频日志（重复>5次），建议采样/聚合，如：{high_freq[0]['message'][:40]}")
        if not suggestions:
            suggestions.append("本次业务执行正常，无明显异常/慢步骤/高频日志。")

        report = {
            "scope": {"business_id": business_id, "trace_id": trace_id},
            "total_logs": total,
            "status": "failed" if errors else "success",
            "summary": (
                f"共 {total} 条日志，异常 {len(errors)} 条，"
                f"平均耗时 {round(avg_cost, 3)}ms，最大耗时 {round(max_cost, 3)}ms"
            ),
            "cost_stats": {
                "avg_ms": round(avg_cost, 3),
                "max_ms": round(max_cost, 3),
                "min_ms": round(min(costs), 3) if costs else 0.0,
            },
            "error_count": len(errors),
            "error_codes": dict(err_codes),
            "status_distribution": dict(statuses),
            "slow_steps": slow_steps[:20],
            "high_freq_logs": high_freq,
            "suggestions": suggestions,
            # D-8 fix: utcnow() deprecated in 3.12+; use timezone-aware now()
            "analyzed_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        return report


def run_analysis(
    log_dir: str,
    business_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    file_format: str = "kv",
    *,
    report_path: Optional[str] = None,
    sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """同步执行分析并输出报告。

    - report_path: 若提供，将报告写入该 JSON 文件。
    - sink: 可选回调（写数据库/消息队列），返回 None。
    """
    analyzer = LogAnalyzer(log_dir, file_format)
    report = analyzer.analyze(business_id=business_id, trace_id=trace_id)
    # D-3 fix: wrap analysis log in trace_scope so it carries business_id
    from .trace import trace_scope

    logger = get_logger()
    with trace_scope(business_id=business_id, trace_id=trace_id):
        log_business_event(
            logger, logging.INFO,
            f"[analysis] {report['summary']}",
            step="auto_analysis", status=report["status"],
            extra={"analysis_report": json.dumps(report, ensure_ascii=False)},
        )
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if sink is not None:
        try:
            sink(report)
        except Exception as e:  # 分析结果落库失败不影响主流程
            logger.exception("analysis sink failed: %s", e)
    return report


def run_analysis_async(
    log_dir: str,
    business_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    file_format: str = "kv",
    *,
    report_path: Optional[str] = None,
    sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> threading.Thread:
    """异步（后台线程）执行分析，避免阻塞主业务。

    返回 threading.Thread；若已存在相同 scope 的任务则复用。
    """
    key = business_id or trace_id or "_all_"
    if key in _analysis_tasks and _analysis_tasks[key].is_alive():
        return _analysis_tasks[key]

    def _run() -> None:
        run_analysis(log_dir, business_id, trace_id, file_format, report_path=report_path, sink=sink)

    t = threading.Thread(target=_run, name=f"loganalysis-{key}", daemon=True)
    _analysis_tasks[key] = t
    t.start()
    return t