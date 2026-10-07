"""调度心跳 / CLI business_id 生命周期 / 工具层分层回归（评审 入口 L1 L4 L5 L6）。

已复现缺陷
----------
- **L1** 执行器被禁用时静默返回、空闲时不记任何事件 →
  「被禁用」「卡死」「一直没任务」在日志里完全一样。
- **L4** 注入条件写成 ``if raw_argv and ...``，于是**空 argv**
  （`echo hi | vibe-trading` 也会跑一次 agent）拿不到 business_id。
- **L5** 用 ``set_trace_context`` 注入，会在**进程级**留下永不重置的 contextvar：
  ``main()`` 返回后 ``get_business_id()`` 仍返回 CLI 的 id（``cli.main`` 是可
  被当库调用的，测试里就这么用）。
- **L6** 工具层每次调用都 import 应用层的 ``src.logsystem_bootstrap``，其导入
  会注册全局 business_id 解析器 —— 对"只用 ToolRegistry"的嵌入方是意外副作用。
"""

from __future__ import annotations

import asyncio
import glob
import importlib
import logging
import os
import pathlib
import re
import subprocess
import sys
import tempfile

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


def _messages(log_dir) -> list:
    return [re.search(r"message=(\S+)", l).group(1) for l in _lines(log_dir)]


def _configure(tmp_path):
    from src.logsystem import LogConfig, configure

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))


def _make_executor(tmp_path, **kwargs):
    from src.scheduled_research.executor import ScheduledResearchExecutor
    from src.scheduled_research.store import ScheduledResearchJobStore

    _configure(tmp_path)
    store = ScheduledResearchJobStore(path=pathlib.Path(tempfile.mkdtemp()) / "jobs.json")
    return ScheduledResearchExecutor(store=store, dispatch=lambda job: None, **kwargs)


# --------------------------------------------------------------------------- #
# L1
# --------------------------------------------------------------------------- #

def test_disabled_executor_emits_event(tmp_path):
    """被禁用必须显式记录，否则与"空闲"无法区分。"""
    ex = _make_executor(tmp_path, enabled=False)
    ex.start()
    assert "scheduled.disabled" in _messages(tmp_path), _messages(tmp_path)


def test_idle_ticks_emit_heartbeat(tmp_path):
    """空闲也必须周期心跳，否则"卡死"与"无任务"无法区分。"""
    ex = _make_executor(tmp_path, enabled=True, heartbeat_every_ticks=3)
    for i in range(7):
        asyncio.run(ex.tick(now_ms=1000 + i))

    assert _messages(tmp_path).count("scheduled.heartbeat") == 2, _messages(tmp_path)


def test_heartbeat_has_business_id(tmp_path):
    ex = _make_executor(tmp_path, enabled=True, heartbeat_every_ticks=1)
    asyncio.run(ex.tick(now_ms=1000))
    line = _lines(tmp_path)[0]
    assert "business_id=sched-executor" in line, line


# --------------------------------------------------------------------------- #
# L5 / L4
# --------------------------------------------------------------------------- #

def _run_main_with_stub(argv, tmp_path):
    """跑一次 cli.main，桩掉 legacy 分发，返回 (业务 id 于调用中, 调用后 id)。"""
    _configure(tmp_path)
    import cli.main as _cli_main_module
    import cli._legacy as legacy
    from src.logsystem import get_business_id

    cli_main = importlib.import_module("cli.main")
    seen = {}

    def stub(_argv):
        seen["during"] = get_business_id()
        return 0

    original = legacy.main
    legacy.main = stub
    try:
        cli_main.main(argv)
    finally:
        legacy.main = original
    return seen.get("during"), get_business_id()


def test_business_id_is_restored_after_main(tmp_path, monkeypatch):
    """L5：main() 返回后不得留下进程级 business_id。"""
    monkeypatch.setenv("VIBE_BUSINESS_ID", "t4-lifecycle")
    monkeypatch.delenv("VIBE_LOG_DIR", raising=False)

    during, after = _run_main_with_stub(["connector", "list"], tmp_path)

    assert during == "t4-lifecycle", during
    assert after != "t4-lifecycle", "business_id 未被还原（进程级残留）"


def test_empty_argv_still_gets_business_id(tmp_path, monkeypatch):
    """L4：空 argv（管道 stdin 会跑 agent）同样需要 business_id。"""
    monkeypatch.setenv("VIBE_BUSINESS_ID", "t4-empty-argv")
    monkeypatch.delenv("VIBE_LOG_DIR", raising=False)

    during, after = _run_main_with_stub([], tmp_path)

    assert during == "t4-empty-argv", during
    assert after != "t4-empty-argv", after


def test_session_bearing_command_is_not_injected(tmp_path, monkeypatch):
    """自带会话身份的命令不得被注入进程级 id。"""
    monkeypatch.setenv("VIBE_BUSINESS_ID", "t4-should-not-apply")
    monkeypatch.delenv("VIBE_LOG_DIR", raising=False)

    during, _ = _run_main_with_stub(["--session-chat", "SID"], tmp_path)

    assert during != "t4-should-not-apply", during


# --------------------------------------------------------------------------- #
# L6
# --------------------------------------------------------------------------- #

def test_tool_layer_does_not_pull_app_bootstrap():
    """只用 ToolRegistry 的嵌入方不应被迫加载应用层 bootstrap。"""
    code = (
        "import sys, json;"
        "sys.path.insert(0, 'agent');"
        "from src.agent.tools import ToolRegistry, BaseTool;"
        "cls = type('T',(BaseTool,),{'name':'t','description':'d',"
        "'parameters':{'type':'object','properties':{}},"
        "'execute':lambda self,**k: json.dumps({'status':'ok'})});"
        "r = ToolRegistry(); r.register(cls()); r.execute('t', {});"
        "print('BOOTSTRAP_LOADED' if 'src.logsystem_bootstrap' in sys.modules else 'CLEAN')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
    )
    assert proc.returncode == 0, proc.stderr[-400:]
    assert "CLEAN" in proc.stdout, proc.stdout
