"""编排与调度埋点的 business_id 与状态判定回归（评审 M1 / L2）。

已复现缺陷
----------
- **L2** 调度事件跑在服务启动上下文里，不继承任何业务身份 → 全部
  ``scheduled.*`` 落成 ``business_id=-``，无法按业务关联。
- **M1** ``swarm.started`` 在 ``start_run`` 的**调用者上下文**里、且 ``thread.start()``
  **之后**记录：既没有 business_id，又可能晚于 worker 内的 ``running``
  （实测顺序 ``running, started, success``），使
  ``finish_and_analyze(run.id)`` 看不到首个事件。
- **同类 C-3** ``_traced_execute_run`` 在 ``_execute_run`` 正常返回后**无条件**记
  ``success``，而业务失败（failed/cancelled）是被内部吞掉后正常返回的 →
  失败/取消的编排在日志里读起来是成功。
"""

from __future__ import annotations

import glob
import logging
import os
import pathlib
import re
import threading
from types import SimpleNamespace

import pytest


def _lines(log_dir) -> list:
    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass
    out = []
    for path in glob.glob(os.path.join(str(log_dir), "*.log")):
        out += [l for l in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    return out


def _fields(line: str) -> dict:
    out = {}
    for k, v in re.findall(r'(\w+)=("[^"]*"|\S+)', line):
        out[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v
    return out


def _configure(tmp_path):
    from src.logsystem import LogConfig, configure

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))


# --------------------------------------------------------------------------- #
# 调度事件带 business_id
# --------------------------------------------------------------------------- #

def test_scheduled_events_carry_business_id(tmp_path):
    from src.scheduled_research.executor import _sched_log

    _configure(tmp_path)
    _sched_log("tick", {"due": 1}, business_id="sched-tick")
    _sched_log("job_done", {"job_id": "j1"}, 0.0, business_id="sched-j1")

    rows = [_fields(l) for l in _lines(tmp_path)]
    assert rows[0]["business_id"] == "sched-tick", rows
    assert rows[1]["business_id"] == "sched-j1", rows
    assert all(r.get("business_id") not in (None, "-") for r in rows), rows


def test_scheduled_events_without_business_id_still_log(tmp_path):
    """未传 business_id 时仍要落盘（不得因 scope 缺失而丢日志）。"""
    from src.scheduled_research.executor import _sched_log

    _configure(tmp_path)
    _sched_log("tick", {"due": 0})
    assert _lines(tmp_path), "未记录日志"


# --------------------------------------------------------------------------- #
# swarm 状态判定
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "status,expected",
    [("completed", "success"), ("succeeded", "success"),
     ("failed", "failed"), ("cancelled", "failed"),
     ("running", "failed"), ("pending", "failed")],
)
def test_swarm_outcome_mapping(status, expected):
    from src.swarm.runtime import _swarm_outcome

    assert _swarm_outcome(SimpleNamespace(status=status)) == expected


def _run_swarm(tmp_path, final_status):
    from src.swarm.runtime import SwarmRuntime

    _configure(tmp_path)
    rt = SwarmRuntime.__new__(SwarmRuntime)
    run = SimpleNamespace(
        id="swarm-z1", status="pending", preset_name="p", agents=[], tasks=[],
        total_input_tokens=0, total_output_tokens=0, provider="p", model="m",
    )
    rt._execute_run = lambda r, c, i, rf: setattr(r, "status", final_status)
    rt._traced_execute_run(run, threading.Event())
    return [_fields(l) for l in _lines(tmp_path)]


def test_failed_swarm_run_is_logged_failed(tmp_path):
    """业务失败不抛异常 —— 必须由 run.status 判定。"""
    rows = _run_swarm(tmp_path, "failed")
    assert rows[-1]["status"] == "failed", rows
    assert rows[-1]["run_status"] == "failed", rows


def test_cancelled_swarm_run_is_logged_failed(tmp_path):
    rows = _run_swarm(tmp_path, "cancelled")
    assert rows[-1]["status"] == "failed", rows


def test_completed_swarm_run_is_logged_success(tmp_path):
    rows = _run_swarm(tmp_path, "completed")
    assert rows[-1]["status"] == "success", rows


def test_swarm_started_is_first_and_correlated(tmp_path):
    """started 必须带 business_id，且排在任何其它事件之前。"""
    rows = _run_swarm(tmp_path, "completed")

    assert rows[0]["status"] == "started", rows
    assert rows[0]["business_id"] == "swarm-z1", rows
    assert all(r["business_id"] == "swarm-z1" for r in rows), rows


def test_swarm_events_all_share_business_id(tmp_path):
    rows = _run_swarm(tmp_path, "failed")
    ids = {r["business_id"] for r in rows}
    assert ids == {"swarm-z1"}, ids
