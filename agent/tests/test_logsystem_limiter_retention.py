"""限流器记账与分析报告回收回归（评审 M15 / M8）。

已复现缺陷
----------
- **M15** ``_RateLimiter.allow`` 把 ``_total_entries += 1`` 与 compact 触发**只放在
  未超限分支**里：一旦某个 key 超限，计数就冻结、``_buckets`` 里的过期 key
  永不回收（长驻服务下无界增长），且该 key 的 bucket 也会一直变长。
  另外 compact 用「当前存活条目数」当触发条件，压缩后仍可能高于阈值 →
  **每次记录都全量扫描**（阈值 10 时实测 60 行触发 51 次压缩）。
- **M8** 保留期只清理 ``*.log``，而 ``analyzer`` 以
  ``analysis_<business_id>.json`` 命名报告，且 CLI 每次调用的 business_id
  都不同 → 报告**永不回收**、无界累积。
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest


def _limiter(**overrides):
    from src.logsystem import LogConfig
    from src.logsystem.logger import _RateLimiter

    cfg = LogConfig(log_dir="/tmp/unused_m15", enable_async_analysis=False, **overrides)
    return _RateLimiter(cfg)


# --------------------------------------------------------------------------- #
# M15
# --------------------------------------------------------------------------- #

def test_bookkeeping_continues_after_limit_is_exceeded():
    """超限后计数必须继续累加（此前冻结，导致过期 key 永不回收）。"""
    rl = _limiter(rate_limit_max=3, rate_limit_window_seconds=60, sample_rate=0.0)
    for _ in range(100):
        rl.allow("hot")
    assert rl._total_entries == 100, rl._total_entries
    assert rl.dropped == 97, rl.dropped


def test_bucket_is_capped_for_hot_key():
    """超限 key 的 bucket 必须封顶，不得随记录数无界增长。"""
    rl = _limiter(rate_limit_max=3, rate_limit_window_seconds=60, sample_rate=1.0)
    for _ in range(500):
        rl.allow("hot")
    assert len(rl._buckets["hot"]) <= 3, len(rl._buckets["hot"])


def test_compaction_is_not_run_per_record():
    """压缩频率必须与阈值挂钩，而不是每条记录都全量扫描。"""
    rl = _limiter(rate_limit_max=100, rate_limit_window_seconds=60)
    rl._COMPACT_THRESHOLD = 10
    calls = {"n": 0}
    original = rl._compact

    def counting(now, window):
        calls["n"] += 1
        return original(now, window)

    rl._compact = counting
    for i in range(60):
        rl.allow(f"k{i % 10}")

    assert calls["n"] <= 8, f"60 条记录触发了 {calls['n']} 次压缩（此前 51 次）"


def test_stale_keys_are_reclaimed():
    """窗口过去后，过期 key 应被 compact 回收。

    注意压缩是**按记录数节流**的（每 ``_COMPACT_THRESHOLD`` 条触发一次），
    所以这里必须再写够阈值条才能触发回收 —— 这是刻意的取舍：以「过期 key
    最多多留阈值条记录」换取「不再每条记录全量扫描」。
    """
    rl = _limiter(rate_limit_max=1000, rate_limit_window_seconds=0.01)
    rl._COMPACT_THRESHOLD = 5
    for i in range(20):
        rl.allow(f"k{i}")
    assert len(rl._buckets) == 20

    time.sleep(0.05)
    for i in range(6):  # 触发一次压缩
        rl.allow(f"later-{i}")

    assert len(rl._buckets) < 21, f"过期 key 未被回收: {len(rl._buckets)}"


def test_rate_limit_zero_is_unlimited():
    rl = _limiter(rate_limit_max=0)
    assert all(rl.allow("x") for _ in range(50))


# --------------------------------------------------------------------------- #
# M8
# --------------------------------------------------------------------------- #

def test_expired_analysis_reports_are_reclaimed(tmp_path):
    """过期的 analysis_*.json 必须按保留期回收（此前永不回收）。"""
    from src.logsystem import LogConfig, configure

    old = tmp_path / "analysis_bid-old.json"
    old.write_text("{}", encoding="utf-8")
    fresh = tmp_path / "analysis_bid-new.json"
    fresh.write_text("{}", encoding="utf-8")
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))

    configure(LogConfig(log_dir=str(tmp_path), retention_days=7,
                        enable_async_analysis=False))

    assert not old.exists(), "过期分析报告未被回收（无界累积）"
    assert fresh.exists(), "未过期的分析报告被误删"


def test_retention_never_deletes_user_json(tmp_path):
    """只删本系统自有命名（analysis_*.json），其它 JSON 一律不碰。"""
    from src.logsystem import LogConfig, configure

    keep = [
        tmp_path / "user-backup.json",
        tmp_path / "analysis_notes.json.bak",
        tmp_path / "my_analysis_x.json",
        tmp_path / "notes.txt",
    ]
    stale = time.time() - 60 * 86400
    for f in keep:
        f.write_text("KEEP", encoding="utf-8")
        os.utime(f, (stale, stale))

    configure(LogConfig(log_dir=str(tmp_path), retention_days=1,
                        enable_async_analysis=False))

    for f in keep:
        assert f.exists(), f"{f.name} 被误删"


def test_retention_disabled_keeps_analysis_reports(tmp_path):
    from src.logsystem import LogConfig, configure

    report = tmp_path / "analysis_keep.json"
    report.write_text("{}", encoding="utf-8")
    stale = time.time() - 365 * 86400
    os.utime(report, (stale, stale))

    configure(LogConfig(log_dir=str(tmp_path), retention_days=0,
                        enable_async_analysis=False))

    assert report.exists(), "retention_days=0 竟然执行了清理"
