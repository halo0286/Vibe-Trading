"""Execution algorithms for multi-bar order scheduling (T6).

Three classic algorithms that determine *how* a large order is distributed
across bars, complementing the participation-rate cap in
:func:`build_partial_fill_schedule`.

========================= ===================================================
Algorithm                 Best when
========================= ===================================================
:func:`twap_schedule`     Low urgency, uniform liquidity across the window
:func:`vwap_schedule`     Liquidity varies predictably (e.g. U-shaped volume)
:func:`is_schedule`       Minimizing implementation shortfall vs decision price
========================= ===================================================

All three return a :class:`PartialFillSchedule` and accept the same core
parameters so they are interchangeable via :func:`select_algorithm`.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from backtest.engines.partial_fill import PartialFillSchedule, build_partial_fill_schedule


def twap_schedule(
    total_shares: float,
    price: float,
    adv_per_bar: float,
    direction: int,
    *,
    n_bars: int = 5,
    max_participation: float = 0.1,
    impact_model: str = "sqrt",
    volatility: float | None = None,
    impact_coefficient: float = 0.5,
    slippage_bps: float = 5.0,
) -> PartialFillSchedule:
    """Time-Weighted Average Price: distribute evenly across bars.

    Each bar gets ``total_shares / n_bars`` shares, capped by the
    participation-rate constraint.  If the cap binds, remaining shares
    spill into additional bars up to ``max_bars = n_bars``.
    """
    # TWAP = uniform distribution, let build_partial_fill_schedule handle caps
    return build_partial_fill_schedule(
        total_shares=total_shares,
        price=price,
        adv_per_bar=adv_per_bar,
        direction=direction,
        max_participation=max_participation,
        max_bars=n_bars,
        impact_model=impact_model,
        volatility=volatility,
        impact_coefficient=impact_coefficient,
        slippage_bps=slippage_bps,
    )


def vwap_schedule(
    total_shares: float,
    price: float,
    adv_series: np.ndarray,
    direction: int,
    *,
    max_participation: float = 0.1,
    impact_model: str = "sqrt",
    volatility: float | None = None,
    impact_coefficient: float = 0.5,
    slippage_bps: float = 5.0,
) -> PartialFillSchedule:
    """Volume-Weighted Average Price: allocate proportional to historical volume.

    Bars with higher ADV get more shares; bars with lower ADV get fewer.
    The participation-rate cap still applies per bar.

    Parameters
    ----------
    adv_series : np.ndarray
        Per-bar ADV values for the execution window, shape ``(n_bars,)``.
        All values must be ≥ 0.
    """
    adv_arr = np.asarray(adv_series, dtype=np.float64)
    if len(adv_arr) == 0 or np.sum(adv_arr) == 0:
        return PartialFillSchedule.empty(total_shares, direction, price)

    # VWAP weights: proportional to ADV
    weights = adv_arr / np.sum(adv_arr)
    target_per_bar = weights * abs(total_shares)

    fills: list[float] = []
    prices: list[float] = []
    cumulative = 0.0

    from src.quantlib.impact import fixed_slippage, linear_impact, sqrt_impact

    for i in range(len(adv_arr)):
        bar_adv = adv_arr[i]
        bar_target = target_per_bar[i]
        max_fill = bar_adv * max_participation if bar_adv > 0 else 0.0
        bar_fill = min(bar_target, max_fill)

        if bar_fill < 1e-12:
            continue

        # Impact on this bar's fill
        if impact_model == "sqrt" and volatility is not None and bar_adv > 0:
            fp = sqrt_impact(price, direction, cumulative + bar_fill,
                             bar_adv, volatility, eta=impact_coefficient)
        elif impact_model == "linear" and bar_adv > 0:
            fp = linear_impact(price, direction, cumulative + bar_fill,
                               bar_adv, impact_coeff=impact_coefficient)
        else:
            fp = fixed_slippage(price, direction, bps=slippage_bps)

        if impact_model != "fixed":
            slip = price * slippage_bps / 10_000.0
            fp += direction * slip

        fills.append(bar_fill)
        prices.append(float(fp))
        cumulative += bar_fill

    filled = sum(fills)
    return PartialFillSchedule(
        total_shares=abs(total_shares),
        fill_schedule=np.array(fills, dtype=np.float64),
        fill_prices=np.array(prices, dtype=np.float64),
        unfilled_shares=max(abs(total_shares) - filled, 0.0),
        direction=direction,
        reference_price=price,
    )


def is_schedule(
    total_shares: float,
    price: float,
    adv_per_bar: float,
    direction: int,
    *,
    n_bars: int = 5,
    urgency: float = 1.0,
    max_participation: float = 0.1,
    impact_model: str = "sqrt",
    volatility: float | None = None,
    impact_coefficient: float = 0.5,
    slippage_bps: float = 5.0,
) -> PartialFillSchedule:
    """Implementation Shortfall: front-load to minimize decision-to-fill gap.

    Allocates more shares to earlier bars (when the price is closer to the
    decision price) and fewer to later bars.  ``urgency`` controls the
    front-loading intensity: 1.0 = moderate, 2.0 = aggressive, 0.5 = patient.

    The per-bar allocation follows a geometric decay:
    ``weight[i] ∝ (1 - decay)^i`` where ``decay = urgency / n_bars``.
    """
    if n_bars < 1:
        raise ValueError(f"n_bars must be >= 1, got {n_bars}")

    decay = min(urgency / n_bars, 0.99)  # cap to avoid degenerate distributions
    raw_weights = np.array([(1.0 - decay) ** i for i in range(n_bars)])
    weights = raw_weights / np.sum(raw_weights)

    # Convert to per-bar ADV series (uniform ADV but weighted targets)
    adv_series = np.full(n_bars, adv_per_bar)
    target_per_bar = weights * abs(total_shares)

    fills: list[float] = []
    prices: list[float] = []
    cumulative = 0.0

    from src.quantlib.impact import fixed_slippage, linear_impact, sqrt_impact

    for i in range(n_bars):
        bar_target = target_per_bar[i]
        max_fill = adv_per_bar * max_participation
        bar_fill = min(bar_target, max_fill)

        if bar_fill < 1e-12:
            continue

        if impact_model == "sqrt" and volatility is not None and adv_per_bar > 0:
            fp = sqrt_impact(price, direction, cumulative + bar_fill,
                             adv_per_bar, volatility, eta=impact_coefficient)
        elif impact_model == "linear" and adv_per_bar > 0:
            fp = linear_impact(price, direction, cumulative + bar_fill,
                               adv_per_bar, impact_coeff=impact_coefficient)
        else:
            fp = fixed_slippage(price, direction, bps=slippage_bps)

        if impact_model != "fixed":
            slip = price * slippage_bps / 10_000.0
            fp += direction * slip

        fills.append(bar_fill)
        prices.append(float(fp))
        cumulative += bar_fill

    filled = sum(fills)
    return PartialFillSchedule(
        total_shares=abs(total_shares),
        fill_schedule=np.array(fills, dtype=np.float64),
        fill_prices=np.array(prices, dtype=np.float64),
        unfilled_shares=max(abs(total_shares) - filled, 0.0),
        direction=direction,
        reference_price=price,
    )


def select_algorithm(
    order_size: float,
    adv: float,
    urgency: str = "normal",
) -> str:
    """Auto-select an execution algorithm based on order characteristics.

    Parameters
    ----------
    order_size : float
        Absolute order size in shares.
    adv : float
        Average daily volume in shares.
    urgency : str
        One of ``"low"``, ``"normal"``, ``"high"``.

    Returns
    -------
    str
        Algorithm name: ``"twap"``, ``"vwap"``, or ``"is"``.
    """
    if adv <= 0:
        return "twap"  # fallback

    participation = abs(order_size) / adv

    if urgency == "high" or participation > 0.05:
        return "is"
    elif participation > 0.01:
        return "vwap"
    else:
        return "twap"


__all__ = [
    "is_schedule",
    "select_algorithm",
    "twap_schedule",
    "vwap_schedule",
]
