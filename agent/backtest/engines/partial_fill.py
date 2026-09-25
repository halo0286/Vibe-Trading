"""Partial fill schedule data model for realistic backtest execution.

When a target position change exceeds what can be filled in a single bar
under a participation-rate constraint, the order must be split across
multiple bars.  This module defines the *result* of that splitting —
a :class:`PartialFillSchedule` — without prescribing *how* the split is
computed (that logic lives in ``build_partial_fill_schedule`` and the
execution-algorithm modules).

Design decisions
----------------
* **Frozen dataclass** — schedules are immutable value objects; once built
  they are passed around, stored in run artifacts, and compared.  Mutation
  would make debugging nightmarish.
* **NumPy arrays for per-bar data** — avoids pandas overhead in hot paths;
  callers that need Series/DataFrame alignment can wrap externally.
* **All costs are positive drags** — consistent with ``factor_costs.py``
  convention.  Signed adjustments get added somewhere by mistake.
* **Zero-safe properties** — ``avg_fill_price`` returns 0.0 when nothing
  was filled rather than raising ZeroDivisionError.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from numpy.typing import ArrayLike


@dataclass(frozen=True)
class PartialFillSchedule:
    """Immutable record of a multi-bar partial-fill execution plan.

    Attributes
    ----------
    total_shares : float
        Absolute number of shares the order intended to trade.
    fill_schedule : np.ndarray
        Shares filled in each bar, shape ``(n_bars,)``.  All values ≥ 0.
    fill_prices : np.ndarray
        Execution price in each bar (after impact), shape ``(n_bars,)``.
    unfilled_shares : float
        Shares that could not be filled within the allowed window.
        Always ≥ 0.
    direction : int
        +1 for buy, −1 for sell.  Stored so downstream cost calculations
        know which side without re-deriving it.
    reference_price : float
        The decision-time price before any impact.  Used to compute
        implementation shortfall.
    """

    total_shares: float
    fill_schedule: np.ndarray
    fill_prices: np.ndarray
    unfilled_shares: float
    direction: int = 1
    reference_price: float = 0.0

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def n_bars(self) -> int:
        """Number of bars over which the order was executed."""
        return len(self.fill_schedule)

    @property
    def filled_shares(self) -> float:
        """Total shares actually filled across all bars."""
        return float(np.sum(self.fill_schedule))

    @property
    def fill_ratio(self) -> float:
        """Fraction of the intended order that was filled (0–1)."""
        if self.total_shares == 0:
            return 1.0  # nothing to fill → trivially complete
        return min(self.filled_shares / abs(self.total_shares), 1.0)

    @property
    def avg_fill_price(self) -> float:
        """Volume-weighted average execution price.

        Returns 0.0 when no shares were filled (avoids ZeroDivisionError).
        """
        total = self.filled_shares
        if total == 0:
            return 0.0
        return float(np.average(self.fill_prices, weights=self.fill_schedule))

    @property
    def total_cost(self) -> float:
        """Total cash consumed by fills (shares × price per bar)."""
        return float(np.sum(self.fill_schedule * self.fill_prices))

    @property
    def implementation_shortfall(self) -> float:
        """Price deviation from decision price, in currency units.

        Positive means the trader paid more (buy) or received less (sell)
        than the reference price.  Always ≥ 0 by construction.
        """
        if self.filled_shares == 0 or self.reference_price == 0:
            return 0.0
        avg = self.avg_fill_price
        shortfall = (avg - self.reference_price) * self.direction
        return max(shortfall, 0.0) * self.filled_shares

    @property
    def participation_rates(self) -> np.ndarray:
        """Per-bar fill as fraction of ADV (placeholder — needs ADV input).

        Returns zeros; callers should use ``compute_participation_rates(adv)``
        for actual values.  Kept as a property for interface symmetry.
        """
        return np.zeros_like(self.fill_schedule)

    def compute_participation_rates(self, adv_per_bar: float) -> np.ndarray:
        """Per-bar fill as fraction of average daily volume.

        Parameters
        ----------
        adv_per_bar : float
            Average daily volume in shares for the relevant period.

        Returns
        -------
        np.ndarray
            Shape ``(n_bars,)``, values in [0, ∞).  Values > 1 mean the
            fill exceeded ADV (possible when max_participation > 1 or
            ADV is underestimated).
        """
        if adv_per_bar <= 0:
            return np.full_like(self.fill_schedule, np.inf)
        return self.fill_schedule / adv_per_bar

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Raise ValueError if internal consistency checks fail.

        Intended for test assertions and post-construction sanity checks,
        not for hot-path enforcement.
        """
        if self.fill_schedule.shape != self.fill_prices.shape:
            raise ValueError(
                f"shape mismatch: fill_schedule {self.fill_schedule.shape} "
                f"vs fill_prices {self.fill_prices.shape}"
            )
        if np.any(self.fill_schedule < 0):
            raise ValueError("fill_schedule contains negative values")
        if self.unfilled_shares < 0:
            raise ValueError(f"unfilled_shares must be ≥ 0, got {self.unfilled_shares}")
        if self.direction not in (1, -1):
            raise ValueError(f"direction must be +1 or -1, got {self.direction}")
        expected_total = self.filled_shares + self.unfilled_shares
        if abs(expected_total - abs(self.total_shares)) > 1e-9:
            raise ValueError(
                f"filled ({self.filled_shares}) + unfilled ({self.unfilled_shares}) "
                f"!= total ({abs(self.total_shares)})"
            )

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @classmethod
    def empty(cls, total_shares: float, direction: int = 1,
              reference_price: float = 0.0) -> "PartialFillSchedule":
        """Create a schedule where nothing was filled."""
        return cls(
            total_shares=total_shares,
            fill_schedule=np.array([], dtype=np.float64),
            fill_prices=np.array([], dtype=np.float64),
            unfilled_shares=abs(total_shares),
            direction=direction,
            reference_price=reference_price,
        )

    @classmethod
    def instant(cls, shares: float, price: float, direction: int = 1,
                reference_price: float | None = None) -> "PartialFillSchedule":
        """Create a single-bar instant fill (no partial execution)."""
        ref = reference_price if reference_price is not None else price
        return cls(
            total_shares=abs(shares),
            fill_schedule=np.array([abs(shares)], dtype=np.float64),
            fill_prices=np.array([price], dtype=np.float64),
            unfilled_shares=0.0,
            direction=direction,
            reference_price=ref,
        )


def build_partial_fill_schedule(
    total_shares: float,
    price: float,
    adv_per_bar: float,
    direction: int,
    *,
    max_participation: float = 0.1,
    max_bars: int = 5,
    impact_model: str = "sqrt",
    volatility: float | None = None,
    impact_coefficient: float = 0.5,
    slippage_bps: float = 5.0,
) -> PartialFillSchedule:
    """Build a multi-bar partial-fill schedule under participation-rate constraint.

    Each bar fills at most ``adv_per_bar × max_participation`` shares.  The fill
    price includes both fixed slippage and size-dependent market impact computed
    by the chosen model from :mod:`src.quantlib.impact`.

    Parameters
    ----------
    total_shares : float
        Absolute number of shares to trade (always positive).
    price : float
        Decision-time reference price (before impact).
    adv_per_bar : float
        Average daily volume in shares for the relevant period.
    direction : int
        +1 for buy, −1 for sell.
    max_participation : float
        Maximum fraction of ADV that can be traded per bar (default 10%).
    max_bars : int
        Maximum number of bars to spread the order across (default 5).
    impact_model : str
        One of ``"sqrt"``, ``"linear"``, ``"fixed"`` (default ``"sqrt"``).
    volatility : float or None
        Daily return volatility (required for ``"sqrt"`` model).
    impact_coefficient : float
        Coefficient for ``"sqrt"`` (eta) or ``"linear"`` model.
    slippage_bps : float
        Fixed half-spread slippage in basis points, applied on every bar.

    Returns
    -------
    PartialFillSchedule
        Immutable schedule with per-bar fills, prices, and unfilled remainder.

    Raises
    ------
    ValueError
        If ``total_shares <= 0``, ``price <= 0``, ``adv_per_bar < 0``,
        ``direction`` not in {+1, −1}, ``max_participation`` not in (0, 1],
        ``max_bars < 1``, unknown ``impact_model``, or ``"sqrt"`` without
        ``volatility``.
    """
    # ── Input validation ────────────────────────────────────────────────
    if total_shares <= 0:
        raise ValueError(f"total_shares must be > 0, got {total_shares}")
    if price <= 0:
        raise ValueError(f"price must be > 0, got {price}")
    if adv_per_bar < 0:
        raise ValueError(f"adv_per_bar must be >= 0, got {adv_per_bar}")
    if direction not in (1, -1):
        raise ValueError(f"direction must be +1 or -1, got {direction}")
    if not (0 < max_participation <= 1):
        raise ValueError(f"max_participation must be in (0, 1], got {max_participation}")
    if max_bars < 1:
        raise ValueError(f"max_bars must be >= 1, got {max_bars}")
    if impact_model not in ("sqrt", "linear", "fixed"):
        raise ValueError(f"unknown impact_model {impact_model!r}")
    if impact_model == "sqrt" and (volatility is None or volatility < 0):
        raise ValueError("sqrt impact requires non-negative volatility")

    # ── Edge cases ──────────────────────────────────────────────────────
    if adv_per_bar == 0:
        # No liquidity → nothing can be filled
        return PartialFillSchedule.empty(total_shares, direction, price)

    # ── Build schedule ──────────────────────────────────────────────────
    from src.quantlib.impact import fixed_slippage, linear_impact, sqrt_impact

    max_shares_per_bar = adv_per_bar * max_participation
    remaining = abs(total_shares)
    fills: list[float] = []
    prices: list[float] = []
    cumulative_filled = 0.0

    for _ in range(max_bars):
        if remaining <= 1e-12:
            break
        bar_fill = min(remaining, max_shares_per_bar)

        # Compute fill price with impact
        if impact_model == "sqrt":
            fill_price = sqrt_impact(
                price, direction, cumulative_filled + bar_fill,
                adv_per_bar, volatility, eta=impact_coefficient,
            )
        elif impact_model == "linear":
            fill_price = linear_impact(
                price, direction, cumulative_filled + bar_fill,
                adv_per_bar, impact_coeff=impact_coefficient,
            )
        else:  # fixed
            fill_price = fixed_slippage(price, direction, bps=slippage_bps)

        # Add fixed slippage on top of size-dependent impact (except for
        # "fixed" model which already includes it)
        if impact_model != "fixed":
            slip = price * slippage_bps / 10_000.0
            fill_price += direction * slip

        fills.append(bar_fill)
        prices.append(float(fill_price))
        cumulative_filled += bar_fill
        remaining -= bar_fill

    return PartialFillSchedule(
        total_shares=abs(total_shares),
        fill_schedule=np.array(fills, dtype=np.float64),
        fill_prices=np.array(prices, dtype=np.float64),
        unfilled_shares=max(remaining, 0.0),
        direction=direction,
        reference_price=price,
    )


__all__ = ["PartialFillSchedule", "build_partial_fill_schedule"]
