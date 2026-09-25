"""Unified cost model abstraction and structured cost breakdown (T7).

Provides a common interface for all market engines to compute trading costs,
and a :class:`CostBreakdown` dataclass that decomposes total cost into
attributable components for reporting and factor-level analysis.

Engines that implement :class:`CostModel` can be used interchangeably by
the backtest runner and factor-cost modules.  Existing engine-specific
``calc_commission`` / ``apply_slippage`` methods remain functional as a
compatibility layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CostBreakdown:
    """Structured decomposition of total trading cost.

    All fields are non-negative currency amounts representing a *drag* on
    returns (consistent with ``factor_costs.py`` convention).

    Attributes
    ----------
    commission : float
        Broker commissions, exchange fees, stamp taxes, transfer fees.
    slippage : float
        Bid-ask spread cost (half-spread × turnover).
    market_impact : float
        Price movement caused by the order itself (temporary + permanent).
    borrow_cost : float
        Securities lending fees for short positions.
    opportunity_cost : float
        Cost of unfilled/partially-filled orders (alpha decay from delay).
    """

    commission: float = 0.0
    slippage: float = 0.0
    market_impact: float = 0.0
    borrow_cost: float = 0.0
    opportunity_cost: float = 0.0

    @property
    def total(self) -> float:
        """Sum of all cost components."""
        return (
            self.commission
            + self.slippage
            + self.market_impact
            + self.borrow_cost
            + self.opportunity_cost
        )

    def to_dict(self) -> dict[str, float]:
        """Serialize to a plain dictionary for JSON/report output."""
        return {
            "commission": round(self.commission, 6),
            "slippage": round(self.slippage, 6),
            "market_impact": round(self.market_impact, 6),
            "borrow_cost": round(self.borrow_cost, 6),
            "opportunity_cost": round(self.opportunity_cost, 6),
            "total": round(self.total, 6),
        }

    def __add__(self, other: "CostBreakdown") -> "CostBreakdown":
        if not isinstance(other, CostBreakdown):
            return NotImplemented
        return CostBreakdown(
            commission=self.commission + other.commission,
            slippage=self.slippage + other.slippage,
            market_impact=self.market_impact + other.market_impact,
            borrow_cost=self.borrow_cost + other.borrow_cost,
            opportunity_cost=self.opportunity_cost + other.opportunity_cost,
        )


class CostModel(ABC):
    """Abstract base class for market-specific cost calculation.

    Subclasses implement the three cost components; :meth:`total_cost`
    combines them into a :class:`CostBreakdown`.
    """

    @abstractmethod
    def calc_commission(
        self, notional: float, direction: int, is_open: bool
    ) -> float:
        """Compute broker/exchange fees for a trade.

        Parameters
        ----------
        notional : float
            Trade notional value (shares × price), always positive.
        direction : int
            +1 for buy, −1 for sell.
        is_open : bool
            True for opening/increasing a position, False for closing/reducing.

        Returns
        -------
        float
            Non-negative commission amount in currency units.
        """

    @abstractmethod
    def calc_slippage(
        self, price: float, order_size: float, adv: float, direction: int
    ) -> float:
        """Compute bid-ask spread cost.

        Returns
        -------
        float
            Non-negative slippage amount in currency units.
        """

    @abstractmethod
    def calc_market_impact(
        self, price: float, order_size: float, adv: float,
        sigma: float, direction: int
    ) -> float:
        """Compute price impact from order execution.

        Returns
        -------
        float
            Non-negative impact amount in currency units.
        """

    def calc_borrow_cost(
        self, notional: float, annual_rate: float, periods_per_year: int
    ) -> float:
        """Compute securities lending cost for short positions.

        Default implementation: ``notional × annual_rate / periods_per_year``.
        Override for market-specific borrow fee structures.
        """
        if periods_per_year <= 0:
            return 0.0
        return notional * annual_rate / periods_per_year

    def total_cost(
        self,
        price: float,
        order_size: float,
        adv: float,
        sigma: float,
        direction: int,
        is_open: bool,
        borrow_annual_rate: float = 0.0,
        periods_per_year: int = 252,
    ) -> CostBreakdown:
        """Compute full cost breakdown for a single trade.

        Combines commission, slippage, market impact, and borrow cost into
        a structured :class:`CostBreakdown`.
        """
        notional = abs(order_size) * price
        comm = self.calc_commission(notional, direction, is_open)
        slip = self.calc_slippage(price, abs(order_size), adv, direction)
        impact = self.calc_market_impact(price, abs(order_size), adv, sigma, direction)
        borrow = 0.0
        if direction == -1 and borrow_annual_rate > 0:
            borrow = self.calc_borrow_cost(notional, borrow_annual_rate, periods_per_year)

        return CostBreakdown(
            commission=comm,
            slippage=slip,
            market_impact=impact,
            borrow_cost=borrow,
        )


# ── Concrete implementations ────────────────────────────────────────────


class ChinaAShareCostModel(CostModel):
    """Cost model for China A-share market.

    Commission: max(notional × rate, minimum) + transfer_fee
    Stamp tax: sell-side only
    Slippage: fixed bps or dynamic (configurable)
    Impact: sqrt model
    """

    def __init__(
        self,
        commission_rate: float = 0.00025,
        commission_min: float = 5.0,
        stamp_tax: float = 0.0005,
        transfer_fee: float = 0.00001,
        slippage_bps: float = 10.0,
        impact_eta: float = 0.5,
    ):
        self.commission_rate = commission_rate
        self.commission_min = commission_min
        self.stamp_tax = stamp_tax
        self.transfer_fee = transfer_fee
        self.slippage_bps = slippage_bps
        self.impact_eta = impact_eta

    def calc_commission(self, notional: float, direction: int, is_open: bool) -> float:
        comm = max(notional * self.commission_rate, self.commission_min)
        comm += notional * self.transfer_fee
        if direction == -1:  # sell-side stamp tax
            comm += notional * self.stamp_tax
        return comm

    def calc_slippage(self, price: float, order_size: float, adv: float, direction: int) -> float:
        return price * self.slippage_bps / 10_000.0 * order_size

    def calc_market_impact(self, price: float, order_size: float, adv: float,
                           sigma: float, direction: int) -> float:
        if adv <= 0 or sigma <= 0:
            return 0.0
        from src.quantlib.impact import sqrt_impact
        fill = sqrt_impact(price, direction, order_size, adv, sigma, eta=self.impact_eta)
        return abs(fill - price) * order_size


class CryptoCostModel(CostModel):
    """Cost model for cryptocurrency perpetual futures.

    Maker/taker fee separation + funding fee handled externally.
    """

    def __init__(
        self,
        maker_rate: float = 0.0002,
        taker_rate: float = 0.0005,
        slippage_bps: float = 5.0,
        impact_eta: float = 0.5,
    ):
        self.maker_rate = maker_rate
        self.taker_rate = taker_rate
        self.slippage_bps = slippage_bps
        self.impact_eta = impact_eta

    def calc_commission(self, notional: float, direction: int, is_open: bool) -> float:
        # Opening = taker, closing = maker (simplified)
        rate = self.taker_rate if is_open else self.maker_rate
        return notional * rate

    def calc_slippage(self, price: float, order_size: float, adv: float, direction: int) -> float:
        return price * self.slippage_bps / 10_000.0 * order_size

    def calc_market_impact(self, price: float, order_size: float, adv: float,
                           sigma: float, direction: int) -> float:
        if adv <= 0 or sigma <= 0:
            return 0.0
        from src.quantlib.impact import sqrt_impact
        fill = sqrt_impact(price, direction, order_size, adv, sigma, eta=self.impact_eta)
        return abs(fill - price) * order_size


__all__ = [
    "ChinaAShareCostModel",
    "CostBreakdown",
    "CostModel",
    "CryptoCostModel",
]
