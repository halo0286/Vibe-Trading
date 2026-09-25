"""Tests for CostModel abstraction and CostBreakdown (T7)."""

from __future__ import annotations

import pytest

from backtest.cost_model import (
    ChinaAShareCostModel,
    CostBreakdown,
    CostModel,
    CryptoCostModel,
)


class TestCostBreakdown:
    def test_total(self):
        cb = CostBreakdown(commission=10, slippage=5, market_impact=3,
                           borrow_cost=2, opportunity_cost=1)
        assert cb.total == pytest.approx(21.0)

    def test_zero_default(self):
        cb = CostBreakdown()
        assert cb.total == 0.0

    def test_frozen(self):
        cb = CostBreakdown(commission=10)
        with pytest.raises(AttributeError):
            cb.commission = 20  # type: ignore[misc]

    def test_addition(self):
        a = CostBreakdown(commission=10, slippage=5)
        b = CostBreakdown(commission=3, market_impact=7)
        c = a + b
        assert c.commission == pytest.approx(13.0)
        assert c.slippage == pytest.approx(5.0)
        assert c.market_impact == pytest.approx(7.0)
        assert c.total == pytest.approx(25.0)

    def test_to_dict(self):
        cb = CostBreakdown(commission=10.123456789, slippage=5.0)
        d = cb.to_dict()
        assert d["commission"] == 10.123457  # rounded to 6 decimals
        assert "total" in d


class TestChinaAShareCostModel:
    @pytest.fixture()
    def model(self) -> ChinaAShareCostModel:
        return ChinaAShareCostModel()

    def test_buy_commission(self, model: ChinaAShareCostModel):
        # notional=100000, rate=0.00025 → 25, min=5 → 25 + transfer 1 = 26
        comm = model.calc_commission(100_000, direction=1, is_open=True)
        assert comm == pytest.approx(26.0)

    def test_sell_includes_stamp_tax(self, model: ChinaAShareCostModel):
        buy = model.calc_commission(100_000, direction=1, is_open=True)
        sell = model.calc_commission(100_000, direction=-1, is_open=False)
        # Sell has stamp_tax (50) extra
        assert sell > buy
        assert sell - buy == pytest.approx(50.0)  # 100000 * 0.0005

    def test_minimum_commission(self, model: ChinaAShareCostModel):
        # Small trade: notional=1000, rate*notional=0.25 < min=5
        comm = model.calc_commission(1_000, direction=1, is_open=True)
        assert comm >= 5.0

    def test_slippage(self, model: ChinaAShareCostModel):
        slip = model.calc_slippage(100.0, 1000, 50000, 1)
        # 100 * 10/10000 * 1000 = 100
        assert slip == pytest.approx(100.0)

    def test_total_cost(self, model: ChinaAShareCostModel):
        cb = model.total_cost(price=100.0, order_size=1000, adv=50000,
                              sigma=0.02, direction=1, is_open=True)
        assert cb.commission > 0
        assert cb.slippage > 0
        assert cb.market_impact >= 0
        assert cb.total > 0


class TestCryptoCostModel:
    @pytest.fixture()
    def model(self) -> CryptoCostModel:
        return CryptoCostModel()

    def test_open_taker_rate(self, model: CryptoCostModel):
        comm = model.calc_commission(10_000, direction=1, is_open=True)
        assert comm == pytest.approx(5.0)  # 10000 * 0.0005

    def test_close_maker_rate(self, model: CryptoCostModel):
        comm = model.calc_commission(10_000, direction=-1, is_open=False)
        assert comm == pytest.approx(2.0)  # 10000 * 0.0002

    def test_borrow_cost_default(self, model: CryptoCostModel):
        borrow = model.calc_borrow_cost(10_000, annual_rate=0.1, periods_per_year=365)
        assert borrow == pytest.approx(10_000 * 0.1 / 365)

    def test_total_cost_short_with_borrow(self, model: CryptoCostModel):
        cb = model.total_cost(price=50_000, order_size=1, adv=1000,
                              sigma=0.05, direction=-1, is_open=True,
                              borrow_annual_rate=0.1, periods_per_year=365)
        assert cb.borrow_cost > 0
        assert cb.commission > 0
        assert cb.total > 0
