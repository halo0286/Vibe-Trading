"""保留期清理与 CLI 初始化安全回归（评审 H-1）。

缺陷（已复现，数据丢失）
------------------------
``init_logging()`` 位于 ``cli/main.py::main`` 的最前面（**参数解析之前**），
而配置阶段会：① 急切创建日志目录（``config.ensure_dir``）；② 执行保留期清理。
清理用的是 ``root.glob("*.log")`` —— 删除该目录下**所有**过期 ``.log``。
``VIBE_LOG_DIR`` 可以指向任意目录，因此只要用户把别的日志放在同一目录，
一个只读的 ``cli --help`` 就会把它们删掉：

    实测：目录内放 30 天前的 user-precious.log → 执行 `cli --help` → 文件消失

本文件锁定两条约束：
1. 保留期清理**只**删除本系统自有文件名 ``{pid}_{YYYY-MM-DD}_{序号}.log``；
2. ``--help`` / ``--version`` **不初始化**日志系统（不创建目录、不触发清理）。
   同时反向锁定：保留期对自有文件仍然有效，业务命令仍然初始化。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_AGENT_DIR = str(Path(__file__).resolve().parents[1])
_REPO_ROOT = str(Path(__file__).resolve().parents[2])


def _age(path: Path, days: int) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _configure(log_dir: str, retention_days: int = 7):
    from src.logsystem import LogConfig, configure

    return configure(
        LogConfig(log_dir=log_dir, level="INFO", retention_days=retention_days,
                  enable_async_analysis=False)
    )


# --------------------------------------------------------------------------- #
# 保留期清理：只删自有文件
# --------------------------------------------------------------------------- #

def test_retention_deletes_only_own_expired_logs(tmp_path):
    d = Path(tmp_path)
    (d / "111_2026-01-01_1.log").write_text("own expired")
    (d / "222_2026-01-01_2.log").write_text("own expired 2")
    (d / "333_2026-01-01_1.log").write_text("own fresh")
    (d / "user-precious.log").write_text("USER DATA")
    (d / "notes.txt").write_text("not a log")
    for name in ("111_2026-01-01_1.log", "222_2026-01-01_2.log", "user-precious.log"):
        _age(d / name, 30)

    _configure(str(d), retention_days=7)

    assert not (d / "111_2026-01-01_1.log").exists(), "自有过期日志未被清理"
    assert not (d / "222_2026-01-01_2.log").exists(), "自有过期日志未被清理"
    assert (d / "333_2026-01-01_1.log").exists(), "未过期的自有日志被误删"
    assert (d / "user-precious.log").exists(), "**用户文件被删除（数据丢失）**"
    assert (d / "notes.txt").exists(), "非 .log 文件被删除"


@pytest.mark.parametrize(
    "name",
    ["user-precious.log", "app.log", "2026-01-01_1.log", "111_2026-01-01.log",
     "111_2026-01-01_1.log.bak", "audit.log", "my_111_2026-01-01_1.log"],
)
def test_retention_ignores_non_own_filenames(tmp_path, name):
    """任何不符自有命名模式的文件都不得删除。"""
    d = Path(tmp_path)
    target = d / name
    target.write_text("KEEP")
    _age(target, 90)

    _configure(str(d), retention_days=1)

    assert target.exists(), f"{name} 被误删"


def test_retention_disabled_when_zero(tmp_path):
    """retention_days=0 表示不清理：连自有文件也不删。"""
    d = Path(tmp_path)
    own = d / "111_2026-01-01_1.log"
    own.write_text("own")
    _age(own, 365)

    _configure(str(d), retention_days=0)

    assert own.exists(), "retention_days=0 竟然执行了清理"


# --------------------------------------------------------------------------- #
# CLI：信息型调用不得产生文件系统副作用
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--help"], True),
        (["-h"], True),
        (["--version"], True),
        (["-V"], True),
        (["connector", "--help"], True),
        ([], False),
        (["run", "-p", "hi"], False),
        (["connector", "list"], False),
        (["alpha", "bench", "--universe", "csi300"], False),
    ],
)
def test_informational_invocation_detection(argv, expected):
    from cli.main import _is_informational_invocation

    assert _is_informational_invocation(argv) is expected


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_help_does_not_create_log_dir(tmp_path, flag):
    """端到端：`--help` / `--version` 不得创建日志目录。"""
    log_dir = tmp_path / "should-not-exist"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(_REPO_ROOT, ".pylibs", "lib", "python3.12", "site-packages"), _AGENT_DIR]
    )
    env["VIBE_LOG_DIR"] = str(log_dir)
    env.setdefault("HOME", "/home/halo/workspace/.vt-home")
    env.setdefault("VIBE_TRADING_HOME", "/home/halo/workspace/.vt-home/.vibe-trading")

    proc = subprocess.run(
        [sys.executable, "-m", "cli", flag],
        cwd=_AGENT_DIR, env=env, capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert not log_dir.exists(), f"{flag} 创建了日志目录（副作用）"


def test_help_does_not_delete_user_files(tmp_path):
    """端到端回归：`--help` 不得删除同目录下用户的过期 .log（H-1 核心）。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    precious = log_dir / "user-precious.log"
    precious.write_text("USER PRECIOUS DATA")
    _age(precious, 30)

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.join(_REPO_ROOT, ".pylibs", "lib", "python3.12", "site-packages"), _AGENT_DIR]
    )
    env["VIBE_LOG_DIR"] = str(log_dir)
    env.setdefault("HOME", "/home/halo/workspace/.vt-home")
    env.setdefault("VIBE_TRADING_HOME", "/home/halo/workspace/.vt-home/.vibe-trading")

    proc = subprocess.run(
        [sys.executable, "-m", "cli", "--help"],
        cwd=_AGENT_DIR, env=env, capture_output=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    assert precious.exists(), "`cli --help` 删除了用户的日志文件（数据丢失）"
