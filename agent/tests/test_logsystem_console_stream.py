"""控制台日志流回归（评审 H-2）。

缺陷（已复现）
--------------
console handler 挂在 ``sys.stdout``，导致 ``cli run --json`` 的 stdout 变成
「若干人类可读日志行 + JSON」，``json.loads(stdout)`` 直接 JSONDecodeError ——
破坏了机器可读输出契约。日志应走 stderr，业务/机器输出才走 stdout。

本文件锁定：控制台日志默认写 stderr；stdout 保持干净；文件日志不受影响；
并保留 ``VIBE_LOG_CONSOLE_STREAM=stdout`` 的显式回退。
"""

from __future__ import annotations

import io
import logging
import os
import sys

import pytest


def _reconfigure(log_dir: str):
    from src.logsystem import LogConfig, configure

    return configure(LogConfig(log_dir=log_dir, level="INFO",
                               enable_async_analysis=False))


def test_console_handler_writes_to_stderr(tmp_path, monkeypatch):
    monkeypatch.delenv("VIBE_LOG_CONSOLE_STREAM", raising=False)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _reconfigure(str(tmp_path))
    logging.getLogger("logsystem").info("hello console")

    assert "hello console" in err.getvalue(), "控制台日志未写 stderr"
    assert "hello console" not in out.getvalue(), "控制台日志污染了 stdout"


def test_console_stream_can_be_forced_to_stdout(tmp_path, monkeypatch):
    """显式回退开关：VIBE_LOG_CONSOLE_STREAM=stdout。"""
    monkeypatch.setenv("VIBE_LOG_CONSOLE_STREAM", "stdout")
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _reconfigure(str(tmp_path))
    logging.getLogger("logsystem").info("forced stdout")

    assert "forced stdout" in out.getvalue()


def test_stdout_stays_clean_for_machine_output(tmp_path, monkeypatch):
    """业务只往 stdout 写 JSON 时，stdout 必须能直接 json.loads。"""
    import json

    monkeypatch.delenv("VIBE_LOG_CONSOLE_STREAM", raising=False)
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    _reconfigure(str(tmp_path))
    logging.getLogger("logsystem").warning("noise that used to break --json")
    print(json.dumps({"status": "success", "run_id": "r-1"}))

    payload = json.loads(out.getvalue())  # 不应抛异常
    assert payload["status"] == "success"


def test_file_logging_unaffected_by_stream_change(tmp_path, monkeypatch):
    monkeypatch.delenv("VIBE_LOG_CONSOLE_STREAM", raising=False)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    _reconfigure(str(tmp_path))
    logging.getLogger("logsystem").info("goes to file")
    for handler in logging.getLogger("logsystem").handlers:
        try:
            handler.flush()
        except Exception:
            pass

    files = list(tmp_path.glob("*.log"))
    assert files, "未生成文件日志"
    assert "goes to file" in files[0].read_text(encoding="utf-8")
