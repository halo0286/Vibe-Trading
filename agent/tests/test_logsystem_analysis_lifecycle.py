"""分析任务并发 / 退出保证 / 会话身份回归（评审 H-6 / H-10 / H-9）。

已复现缺陷
----------
- **H-6** ``run_analysis_async`` 的「去重」是 check-then-act：8 个并发同键调用
  会启动 **8 个线程**，只有 1 个进 ``_analysis_tasks`` —— 其余 7 个**未被跟踪**，
  ``drain_analysis_tasks`` 等不到它们，且多个线程并发写同一个 ``report_path``。
- **H-10** ``drain_analysis_tasks`` 对每个线程各自 ``join(timeout)``，N 个卡住
  任务最坏等 N×timeout（实测 3 个 @1s → 3.00s）；报告用 ``write_text`` 非原子，
  进程被杀会留下截断 JSON；CLI 的 drain 不在 ``try/finally`` 里，
  ``SystemExit`` / 异常会跳过它；interactive / resume 路径根本不 drain。
- **H-9** 进程级 business_id 被注入到 ``chat`` / ``--session-chat`` /
  ``--continue`` 等**自带会话身份**的命令上，使同一会话跨调用拿到不同 id。
"""

from __future__ import annotations

import json
import threading
import time

import pytest


# --------------------------------------------------------------------------- #
# H-6：并发去重
# --------------------------------------------------------------------------- #

def test_concurrent_same_key_starts_one_thread(monkeypatch):
    """8 个并发同键调用只能启动 1 个分析线程。"""
    from src.logsystem import analyzer

    analyzer._analysis_tasks.clear()
    started = []

    def fake_run(*args, **kwargs):
        started.append(1)
        time.sleep(0.4)

    monkeypatch.setattr(analyzer, "run_analysis", fake_run)

    threads = [analyzer.run_analysis_async("/tmp/nope", business_id="same") for _ in range(8)]

    assert len({id(t) for t in threads}) == 1, "同键调用返回了多个线程"
    assert len(analyzer._analysis_tasks) == 1, "有未跟踪的分析线程"
    time.sleep(0.8)
    assert len(started) == 1, f"实际启动了 {len(started)} 次分析"


def test_finished_tasks_are_pruned(monkeypatch):
    """已完成的任务不应永久驻留（长驻服务下会按 business_id 无界增长）。"""
    from src.logsystem import analyzer

    analyzer._analysis_tasks.clear()
    monkeypatch.setattr(analyzer, "run_analysis", lambda *a, **k: None)

    for i in range(5):
        analyzer.run_analysis_async("/tmp/nope", business_id=f"bid-{i}")
    time.sleep(0.3)

    # 再次注册时应顺手回收已结束的任务
    analyzer.run_analysis_async("/tmp/nope", business_id="fresh")
    assert len(analyzer._analysis_tasks) == 1, analyzer._analysis_tasks


# --------------------------------------------------------------------------- #
# H-10：退出保证
# --------------------------------------------------------------------------- #

def test_drain_uses_shared_deadline(monkeypatch):
    """N 个卡住任务最坏等待 timeout，而不是 N × timeout。"""
    from src.logsystem import analyzer

    analyzer._analysis_tasks.clear()
    for i in range(3):
        t = threading.Thread(target=lambda: time.sleep(30), daemon=True)
        analyzer._analysis_tasks[f"k{i}"] = t
        t.start()

    t0 = time.monotonic()
    analyzer.drain_analysis_tasks(timeout=1.0)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.8, f"耗时 {elapsed:.2f}s，疑似串行 N×timeout"


def test_report_write_is_atomic(tmp_path):
    """报告必须原子落盘，且不留下临时文件。"""
    from src.logsystem.analyzer import run_analysis
    from src.logsystem import LogConfig, configure, log_event
    import logging

    configure(LogConfig(log_dir=str(tmp_path), level="INFO",
                        enable_async_analysis=False))
    log_event(logging.INFO, "event", step="order", status="success")
    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass

    report = tmp_path / "analysis_x.json"
    run_analysis(str(tmp_path), business_id=None, file_format="kv",
                 report_path=str(report))

    assert report.exists()
    json.loads(report.read_text(encoding="utf-8"))  # 必须是完整合法 JSON
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert not leftovers, f"残留临时文件: {leftovers}"


def test_cli_drain_runs_on_exception(monkeypatch, tmp_path):
    """`_legacy.main` 抛异常时也必须 drain（此前会被跳过）。"""
    import importlib

    m = importlib.import_module("cli.main")

    called = []
    monkeypatch.setattr(m, "_drain_analysis_best_effort", lambda: called.append(1))

    def boom(argv):
        raise SystemExit(3)

    legacy = importlib.import_module("cli._legacy")

    monkeypatch.setattr(legacy, "main", boom)
    monkeypatch.setattr(m, "main", m.main)  # 保持原函数

    with pytest.raises(SystemExit):
        m.main(["connector", "list"])

    assert called, "异常退出路径跳过了 drain"


# --------------------------------------------------------------------------- #
# H-9：会话身份不被覆盖
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "argv",
    [["serve"], ["dev"], ["chat"], ["resume", "SID"],
     ["--session-chat", "SID"], ["--continue", "R1", "prompt"]],
)
def test_session_bearing_invocations_are_not_injected(argv):
    from cli.main import _has_own_session_identity

    assert _has_own_session_identity(argv) is True


@pytest.mark.parametrize(
    "argv",
    [["run", "-p", "x"], ["connector", "list"], ["connector", "account"],
     ["alpha", "bench", "--universe", "csi300"]],
)
def test_stateless_invocations_are_injected(argv):
    from cli.main import _has_own_session_identity

    assert _has_own_session_identity(argv) is False
