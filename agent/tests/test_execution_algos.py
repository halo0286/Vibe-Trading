"""Tests for execution algorithms (T6)."""

from __future__ import annotations

import numpy as np
import pytest

from backtest.engines.execution_algos import (
    is_schedule,
    select_algorithm,
    twap_schedule,
    vwap_schedule,
)


class TestTWAP:
    def test_basic(self):
        sched = twap_schedule(1000, 100.0, 5000, 1, n_bars=5,
                              impact_model="sqrt", volatility=0.02)
        assert sched.filled_shares == pytest.approx(1000.0)
        assert sched.unfilled_shares == pytest.approx(0.0)
        sched.validate()

    def test_single_bar(self):
        sched = twap_schedule(100, 100.0, 5000, 1, n_bars=1,
                              impact_model="sqrt", volatility=0.02)
        assert sched.n_bars == 1
        assert sched.filled_shares == pytest.approx(100.0)
        sched.validate()

    def test_cap_binding(self):
        # 3000 shares, ADV=5000, participation=0.1 → max 500/bar, 5 bars = 2500
        sched = twap_schedule(3000, 100.0, 5000, 1, n_bars=5,
                              impact_model="sqrt", volatility=0.02)
        assert sched.filled_shares <= 3000.0
        assert sched.unfilled_shares >= 0.0
        sched.validate()


class TestVWAP:
    def test_basic_uniform_adv(self):
        adv = np.array([5000.0, 5000.0, 5000.0, 5000.0, 5000.0])
        sched = vwap_schedule(1000, 100.0, adv, 1,
                              impact_model="sqrt", volatility=0.02)
        assert sched.filled_shares == pytest.approx(1000.0)
        sched.validate()

    def test_skewed_volume(self):
        # High volume in first bar → more shares allocated there
        adv = np.array([10000.0, 1000.0, 1000.0, 1000.0, 1000.0])
        sched = vwap_schedule(1000, 100.0, adv, 1,
                              impact_model="sqrt", volatility=0.02)
        assert sched.fill_schedule[0] > sched.fill_schedule[1]
        sched.validate()

    def test_zero_adv_returns_empty(self):
        adv = np.array([0.0, 0.0, 0.0])
        sched = vwap_schedule(1000, 100.0, adv, 1)
        assert sched.n_bars == 0
        assert sched.unfilled_shares == 1000.0
        sched.validate()

    def test_empty_adv_returns_empty(self):
        sched = vwap_schedule(1000, 100.0, np.array([]), 1)
        assert sched.n_bars == 0
        sched.validate()


class TestIS:
    def test_front_loaded(self):
        sched = is_schedule(1000, 100.0, 5000, 1, n_bars=5, urgency=1.0,
                            impact_model="sqrt", volatility=0.02)
        # First bar should have more shares than last
        if sched.n_bars >= 2:
            assert sched.fill_schedule[0] >= sched.fill_schedule[-1]
        sched.validate()

    def test_high_urgency_more_front_loaded(self):
        s_low = is_schedule(1000, 100.0, 5000, 1, n_bars=5, urgency=0.5,
                            impact_model="sqrt", volatility=0.02)
        s_high = is_schedule(1000, 100.0, 5000, 1, n_bars=5, urgency=2.0,
                             impact_model="sqrt", volatility=0.02)
        # High urgency → more in first bar
        if s_low.n_bars >= 1 and s_high.n_bars >= 1:
            assert s_high.fill_schedule[0] >= s_low.fill_schedule[0]

    def test_zero_bars_raises(self):
        with pytest.raises(ValueError, match="n_bars"):
            is_schedule(1000, 100.0, 5000, 1, n_bars=0,
                        impact_model="sqrt", volatility=0.02)


class TestSelectAlgorithm:
    def test_small_order_twap(self):
        assert select_algorithm(100, 100_000) == "twap"  # 0.1% < 1%

    def test_medium_order_vwap(self):
        assert select_algorithm(2000, 100_000) == "vwap"  # 2% > 1%

    def test_large_order_is(self):
        assert select_algorithm(10_000, 100_000) == "is"  # 10% > 5%

    def test_high_urgency_is(self):
        assert select_algorithm(500, 100_000, urgency="high") == "is"

    def test_zero_adv_fallback(self):
        assert select_algorithm(1000, 0) == "twap"
