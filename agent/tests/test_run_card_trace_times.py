"""``trace_timestamps()`` 回归测试：墙上时钟回调不应让 run card 校验失败。

背景：tool trace 的 ``started_at`` / ``ended_at`` 取自墙上时钟
（``datetime.now(timezone.utc)``）。NTP 步进或 VM/主机恢复会让
``ended_at < started_at``，run card 的 ``_normalize_tool_traces`` 随即抛
``ValueError: trace ends before it starts``，把一轮本身健康的回测判死
（2026-10-09 容器全量中实测触发过一次）。

修法：在**产生处**用 ``trace_timestamps()`` 成对产生并钳住顺序；
消费者侧的严格校验**保持不变**（见最后一个测试）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backtest.run_card import _normalize_tool_traces, trace_timestamps, write_run_card


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def test_normal_run_keeps_both_stamps():
    started = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    ended = started + timedelta(seconds=90)

    started_iso, ended_iso = trace_timestamps(started, ended)

    assert _parse(started_iso) == started
    assert _parse(ended_iso) == ended
    assert started_iso.endswith("Z")
    assert ended_iso.endswith("Z")


def test_backwards_clock_is_clamped_not_raised():
    """时钟回调（ended < started）应钳到起点，而不是抛异常。"""
    started = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    ended = started - timedelta(seconds=5)

    started_iso, ended_iso = trace_timestamps(started, ended)

    assert _parse(ended_iso) == started
    assert _parse(ended_iso) >= _parse(started_iso)


def test_ended_at_defaults_to_now_and_stays_ordered():
    started = datetime.now(timezone.utc) - timedelta(seconds=1)

    started_iso, ended_iso = trace_timestamps(started)

    assert _parse(ended_iso) >= _parse(started_iso)


def test_naive_datetimes_are_treated_as_utc():
    started = datetime(2026, 10, 9, 12, 0, 0)  # naive

    started_iso, _ = trace_timestamps(started)

    assert _parse(started_iso) == started.replace(tzinfo=timezone.utc)


def test_run_card_accepts_clamped_pair_after_backwards_clock(tmp_path):
    """核心回归：时钟回调时 run card 仍应成功写出，而不是抛 ValueError。"""
    started = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    started_iso, ended_iso = trace_timestamps(started, started - timedelta(seconds=3))

    card = write_run_card(
        tmp_path / "run",
        {"codes": ["AAA"], "engine": "global_equity"},
        {"sharpe": 1.0},
        tool_traces=[
            {
                "tool": "backtest",
                "args": {"codes": ["AAA"]},
                "started_at": started_iso,
                "ended_at": ended_iso,
                "status": "ok",
                "result": {"sharpe": 1.0},
            }
        ],
    )

    trace = card["tool_traces"][0]
    assert _parse(trace["ended_at"]) >= _parse(trace["started_at"])


def test_validator_still_rejects_reversed_pair():
    """消费者侧的严格校验保持不变：未经 helper 的逆序输入仍被拒绝。"""
    with pytest.raises(ValueError, match="trace ends before it starts"):
        _normalize_tool_traces(
            [
                {
                    "tool": "backtest",
                    "args": {},
                    "started_at": "2026-10-09T12:00:05Z",
                    "ended_at": "2026-10-09T12:00:00Z",
                    "status": "ok",
                    "result": {},
                }
            ]
        )
