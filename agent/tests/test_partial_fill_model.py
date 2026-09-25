"""Tests for PartialFillSchedule data model (T1).

Covers construction, derived properties, validation, and factory helpers.
No business logic — pure data container tests.
"""

from __future__ import annotations

import numpy as np
import pytest

from backtest.engines.partial_fill import PartialFillSchedule, build_partial_fill_schedule


# ── Construction ────────────────────────────────────────────────────────


class TestConstruction:
    def test_basic_construction(self):
        sched = PartialFillSchedule(
            total_shares=1000,
            fill_schedule=np.array([300, 300, 300]),
            fill_prices=np.array([10.0, 10.1, 10.2]),
            unfilled_shares=100,
            direction=1,
            reference_price=10.0,
        )
        assert sched.total_shares == 1000
        assert sched.n_bars == 3
        assert sched.direction == 1

    def test_frozen(self):
        sched = PartialFillSchedule(
            total_shares=100,
            fill_schedule=np.array([100.0]),
            fill_prices=np.array([10.0]),
            unfilled_shares=0.0,
        )
        with pytest.raises(AttributeError):
            sched.total_shares = 200  # type: ignore[misc]


# ── Derived properties ──────────────────────────────────────────────────


class TestProperties:
    @pytest.fixture()
    def sample(self) -> PartialFillSchedule:
        return PartialFillSchedule(
            total_shares=1000,
            fill_schedule=np.array([400.0, 350.0, 200.0]),
            fill_prices=np.array([10.0, 10.1, 10.2]),
            unfilled_shares=50.0,
            direction=1,
            reference_price=10.0,
        )

    def test_filled_shares(self, sample: PartialFillSchedule):
        assert sample.filled_shares == pytest.approx(950.0)

    def test_fill_ratio(self, sample: PartialFillSchedule):
        assert sample.fill_ratio == pytest.approx(0.95)

    def test_fill_ratio_zero_total(self):
        sched = PartialFillSchedule(
            total_shares=0,
            fill_schedule=np.array([]),
            fill_prices=np.array([]),
            unfilled_shares=0.0,
        )
        assert sched.fill_ratio == 1.0

    def test_avg_fill_price(self, sample: PartialFillSchedule):
        expected = np.average(
            [10.0, 10.1, 10.2], weights=[400.0, 350.0, 200.0]
        )
        assert sample.avg_fill_price == pytest.approx(expected)

    def test_avg_fill_price_empty(self):
        sched = PartialFillSchedule.empty(1000)
        assert sched.avg_fill_price == 0.0

    def test_total_cost(self, sample: PartialFillSchedule):
        expected = 400 * 10.0 + 350 * 10.1 + 200 * 10.2
        assert sample.total_cost == pytest.approx(expected)

    def test_implementation_shortfall_buy(self, sample: PartialFillSchedule):
        avg = sample.avg_fill_price
        expected = (avg - 10.0) * sample.filled_shares
        assert sample.implementation_shortfall == pytest.approx(expected)

    def test_implementation_shortfall_sell(self):
        sched = PartialFillSchedule(
            total_shares=500,
            fill_schedule=np.array([500.0]),
            fill_prices=np.array([9.9]),
            unfilled_shares=0.0,
            direction=-1,
            reference_price=10.0,
        )
        # Sell at 9.9 vs reference 10.0 → shortfall = (9.9-10.0)*(-1)*500 = 50
        assert sched.implementation_shortfall == pytest.approx(50.0)

    def test_implementation_shortfall_zero_filled(self):
        sched = PartialFillSchedule.empty(1000)
        assert sched.implementation_shortfall == 0.0

    def test_n_bars(self, sample: PartialFillSchedule):
        assert sample.n_bars == 3

    def test_participation_rates_placeholder(self, sample: PartialFillSchedule):
        rates = sample.participation_rates
        assert len(rates) == 3
        assert np.all(rates == 0.0)

    def test_compute_participation_rates(self, sample: PartialFillSchedule):
        rates = sample.compute_participation_rates(adv_per_bar=1000.0)
        np.testing.assert_allclose(rates, [0.4, 0.35, 0.2])

    def test_compute_participation_rates_zero_adv(self, sample: PartialFillSchedule):
        rates = sample.compute_participation_rates(adv_per_bar=0.0)
        assert np.all(np.isinf(rates))


# ── Validation ──────────────────────────────────────────────────────────


class TestValidation:
    def test_valid_schedule_passes(self):
        sched = PartialFillSchedule(
            total_shares=100,
            fill_schedule=np.array([60.0, 40.0]),
            fill_prices=np.array([10.0, 10.1]),
            unfilled_shares=0.0,
        )
        sched.validate()  # should not raise

    def test_shape_mismatch_raises(self):
        sched = PartialFillSchedule(
            total_shares=100,
            fill_schedule=np.array([60.0, 40.0]),
            fill_prices=np.array([10.0]),  # wrong length
            unfilled_shares=0.0,
        )
        with pytest.raises(ValueError, match="shape mismatch"):
            sched.validate()

    def test_negative_fill_raises(self):
        sched = PartialFillSchedule(
            total_shares=100,
            fill_schedule=np.array([-10.0, 110.0]),
            fill_prices=np.array([10.0, 10.1]),
            unfilled_shares=0.0,
        )
        with pytest.raises(ValueError, match="negative"):
            sched.validate()

    def test_negative_unfilled_raises(self):
        sched = PartialFillSchedule(
            total_shares=100,
            fill_schedule=np.array([100.0]),
            fill_prices=np.array([10.0]),
            unfilled_shares=-1.0,
        )
        with pytest.raises(ValueError, match="unfilled_shares"):
            sched.validate()

    def test_invalid_direction_raises(self):
        sched = PartialFillSchedule(
            total_shares=100,
            fill_schedule=np.array([100.0]),
            fill_prices=np.array([10.0]),
            unfilled_shares=0.0,
            direction=2,
        )
        with pytest.raises(ValueError, match="direction"):
            sched.validate()

    def test_total_mismatch_raises(self):
        sched = PartialFillSchedule(
            total_shares=100,
            fill_schedule=np.array([60.0]),
            fill_prices=np.array([10.0]),
            unfilled_shares=30.0,  # 60+30=90 != 100
        )
        with pytest.raises(ValueError, match="!="):
            sched.validate()


# ── Factory helpers ─────────────────────────────────────────────────────


class TestFactories:
    def test_empty(self):
        sched = PartialFillSchedule.empty(500, direction=-1, reference_price=20.0)
        assert sched.total_shares == 500
        assert sched.filled_shares == 0.0
        assert sched.unfilled_shares == 500.0
        assert sched.n_bars == 0
        assert sched.direction == -1
        assert sched.reference_price == 20.0
        sched.validate()

    def test_instant(self):
        sched = PartialFillSchedule.instant(300, price=15.5, direction=1)
        assert sched.total_shares == 300
        assert sched.filled_shares == 300.0
        assert sched.unfilled_shares == 0.0
        assert sched.n_bars == 1
        assert sched.avg_fill_price == 15.5
        assert sched.fill_ratio == 1.0
        assert sched.implementation_shortfall == 0.0  # ref == fill price
        sched.validate()

    def test_instant_with_reference_price(self):
        sched = PartialFillSchedule.instant(
            300, price=15.5, direction=1, reference_price=15.0
        )
        assert sched.reference_price == 15.0
        assert sched.implementation_shortfall > 0  # paid more than ref

    def test_instant_sell(self):
        sched = PartialFillSchedule.instant(200, price=9.8, direction=-1,
                                            reference_price=10.0)
        assert sched.direction == -1
        # Sold at 9.8 vs ref 10.0 → shortfall = (9.8-10.0)*(-1)*200 = 40
        assert sched.implementation_shortfall == pytest.approx(40.0)


# ── build_partial_fill_schedule (T2) ───────────────────────────────────


class TestBuildSchedule:
    """Tests for the core scheduling algorithm."""

    def test_basic_sqrt_schedule(self):
        sched = build_partial_fill_schedule(
            total_shares=1000, price=100.0, adv_per_bar=5000,
            direction=1, max_participation=0.1, max_bars=5,
            impact_model="sqrt", volatility=0.02,
        )
        # max per bar = 5000 * 0.1 = 500; need 1000 → 2 bars
        assert sched.n_bars == 2
        assert sched.filled_shares == pytest.approx(1000.0)
        assert sched.unfilled_shares == pytest.approx(0.0)
        assert sched.fill_ratio == pytest.approx(1.0)
        sched.validate()

    def test_unfilled_when_max_bars_insufficient(self):
        sched = build_partial_fill_schedule(
            total_shares=3000, price=100.0, adv_per_bar=5000,
            direction=1, max_participation=0.1, max_bars=3,
            impact_model="sqrt", volatility=0.02,
        )
        # max per bar = 500; 3 bars = 1500; unfilled = 1500
        assert sched.n_bars == 3
        assert sched.filled_shares == pytest.approx(1500.0)
        assert sched.unfilled_shares == pytest.approx(1500.0)
        assert sched.fill_ratio == pytest.approx(0.5)
        sched.validate()

    def test_zero_adv_returns_empty(self):
        sched = build_partial_fill_schedule(
            total_shares=1000, price=100.0, adv_per_bar=0,
            direction=1, impact_model="sqrt", volatility=0.02,
        )
        assert sched.n_bars == 0
        assert sched.filled_shares == 0.0
        assert sched.unfilled_shares == 1000.0
        sched.validate()

    def test_small_order_single_bar(self):
        sched = build_partial_fill_schedule(
            total_shares=100, price=50.0, adv_per_bar=10000,
            direction=-1, max_participation=0.1, max_bars=5,
            impact_model="sqrt", volatility=0.01,
        )
        # 100 < 1000 → single bar
        assert sched.n_bars == 1
        assert sched.filled_shares == pytest.approx(100.0)
        assert sched.unfilled_shares == pytest.approx(0.0)
        sched.validate()

    def test_linear_impact_model(self):
        sched = build_partial_fill_schedule(
            total_shares=500, price=100.0, adv_per_bar=5000,
            direction=1, max_participation=0.1, max_bars=3,
            impact_model="linear", impact_coefficient=0.1,
        )
        assert sched.n_bars == 1  # 500 < 500*0.1=500... exactly 1 bar
        assert sched.filled_shares == pytest.approx(500.0)
        sched.validate()

    def test_fixed_impact_model(self):
        sched = build_partial_fill_schedule(
            total_shares=200, price=100.0, adv_per_bar=5000,
            direction=1, max_participation=0.1, max_bars=3,
            impact_model="fixed", slippage_bps=10.0,
        )
        assert sched.n_bars == 1
        # Fixed slippage: 10 bps = 0.001 → price = 100 + 0.1 = 100.1
        assert sched.fill_prices[0] == pytest.approx(100.1, rel=1e-3)
        sched.validate()

    def test_buy_price_above_reference(self):
        sched = build_partial_fill_schedule(
            total_shares=1000, price=100.0, adv_per_bar=5000,
            direction=1, impact_model="sqrt", volatility=0.02,
        )
        assert sched.avg_fill_price > 100.0  # buying pushes price up

    def test_sell_price_below_reference(self):
        sched = build_partial_fill_schedule(
            total_shares=1000, price=100.0, adv_per_bar=5000,
            direction=-1, impact_model="sqrt", volatility=0.02,
        )
        assert sched.avg_fill_price < 100.0  # selling pushes price down

    def test_implementation_shortfall_positive(self):
        sched = build_partial_fill_schedule(
            total_shares=1000, price=100.0, adv_per_bar=5000,
            direction=1, impact_model="sqrt", volatility=0.02,
        )
        assert sched.implementation_shortfall > 0

    # ── Validation errors ───────────────────────────────────────────────

    def test_zero_shares_raises(self):
        with pytest.raises(ValueError, match="total_shares"):
            build_partial_fill_schedule(0, 100.0, 5000, 1,
                                        impact_model="sqrt", volatility=0.02)

    def test_negative_price_raises(self):
        with pytest.raises(ValueError, match="price"):
            build_partial_fill_schedule(100, -1.0, 5000, 1,
                                        impact_model="sqrt", volatility=0.02)

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            build_partial_fill_schedule(100, 100.0, 5000, 0,
                                        impact_model="sqrt", volatility=0.02)

    def test_invalid_participation_raises(self):
        with pytest.raises(ValueError, match="max_participation"):
            build_partial_fill_schedule(100, 100.0, 5000, 1,
                                        max_participation=0,
                                        impact_model="sqrt", volatility=0.02)

    def test_zero_max_bars_raises(self):
        with pytest.raises(ValueError, match="max_bars"):
            build_partial_fill_schedule(100, 100.0, 5000, 1,
                                        max_bars=0,
                                        impact_model="sqrt", volatility=0.02)

    def test_unknown_impact_model_raises(self):
        with pytest.raises(ValueError, match="unknown impact_model"):
            build_partial_fill_schedule(100, 100.0, 5000, 1,
                                        impact_model="cubic")

    def test_sqrt_without_volatility_raises(self):
        with pytest.raises(ValueError, match="volatility"):
            build_partial_fill_schedule(100, 100.0, 5000, 1,
                                        impact_model="sqrt", volatility=None)
