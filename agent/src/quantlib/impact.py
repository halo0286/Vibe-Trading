"""Market-impact and slippage models for backtest execution.

Four models, ordered by how much of the order book they claim to know:

========================= ================================== =====================
Model                     Impact                             Use when
========================= ================================== =====================
:func:`fixed_slippage`    constant ``bps``                   order < 0.5% of ADV
:func:`linear_impact`     ``coeff * V / ADV``                order 0.5-5% of ADV
:func:`sqrt_impact`       ``eta * sigma * sqrt(V / ADV)``    order > 5% of ADV
:func:`delayed_execution` shifts a signal forward in time    always, for signal lag
========================= ================================== =====================

All three price models compute a non-negative impact and push the fill price
*against* the trader -- up when buying, down when selling.

The two size-aware models, :func:`linear_impact` and :func:`sqrt_impact`, are
additionally monotone non-decreasing in order size and return the untouched price
at zero size. :func:`fixed_slippage` takes no size at all: it charges its ``bps``
on every fill regardless of how small, which is the whole point of a fixed model
and the reason it is only appropriate below roughly 0.5% of ADV.

Every function here is scalar-only. Passing a numpy array or a pandas Series where
a ``float`` is documented raises rather than broadcasting.

These are backtest primitives. Nothing here places, routes or prices a live order.

Note on naming: the square-root model is frequently labelled "Almgren-Chriss" and
is indeed the impact term from that literature, but this module does **not**
implement Almgren-Chriss optimal execution -- there is no trading trajectory, no
permanent/temporary impact split and no risk-aversion parameter. Call it what it
is: a square-root impact function.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike

#: Public surface. `quantlib_call` dispatches on ``__all__`` alone, so a module
#: without it is unreachable from Web / API / MCP even when the tool allowlists
#: it — which is exactly what happened to this module until 0.1.13.
#: Default fixed slippage in basis points (1bp = 0.01%).
DEFAULT_SLIPPAGE_BPS = 5.0

#: Default linear-impact coefficient; 0.05-0.2 is the usual calibrated range.
DEFAULT_LINEAR_IMPACT_COEFF = 0.1

#: Default square-root impact elasticity; 0.3-0.8 is the usual calibrated range.
DEFAULT_SQRT_IMPACT_ETA = 0.5

#: Default execution lag in bars. 1 bar matches the China A-share T+1 rule.
DEFAULT_DELAY_BARS = 1

#: Basis points in one unit (100%).
_BPS_PER_UNIT = 10_000.0


def _check_order(price: ArrayLike, direction: int) -> np.ndarray:
    """Validate the price and side shared by every price-impact model.

    Args:
        price: Reference price before impact; scalar or array-like.
        direction: 1 to buy, -1 to sell.

    Returns:
        ``price`` as a float array (0-d for a scalar input).

    Raises:
        ValueError: If any ``price`` is not strictly positive, or if
            ``direction`` is anything other than 1 or -1. Direction multiplies
            the impact, so a value such as 2 would silently double the modelled
            cost.
    """
    prices = np.asarray(price, dtype=float)
    if not np.all(np.isfinite(prices)) or not np.all(prices > 0.0):
        raise ValueError(f"price must be strictly positive, got {price!r}")
    if direction not in (1, -1):
        raise ValueError(f"direction must be 1 (buy) or -1 (sell), got {direction!r}")
    return prices


def _participation_rate(volume_traded: ArrayLike, adv: ArrayLike) -> np.ndarray:
    """Return the order size as a fraction of average daily volume.

    Args:
        volume_traded: Order size, in the same unit as ``adv``.
        adv: Average daily volume, strictly positive.

    Returns:
        ``volume_traded / adv``, broadcast over both arguments.

    Raises:
        ValueError: If any ``volume_traded`` is negative or any ``adv`` is not
            strictly positive. A zero ``adv`` means the instrument does not
            trade, for which no impact model has an answer.
    """
    volumes = np.asarray(volume_traded, dtype=float)
    advs = np.asarray(adv, dtype=float)
    if not np.all(np.isfinite(volumes)) or not np.all(volumes >= 0.0):
        raise ValueError(f"volume_traded must be non-negative, got {volume_traded!r}")
    if not np.all(np.isfinite(advs)) or not np.all(advs > 0.0):
        raise ValueError(f"adv must be strictly positive, got {adv!r}")
    return volumes / advs


def _fill(prices: np.ndarray, direction: int, impact: np.ndarray) -> float | np.ndarray:
    """Apply a relative impact to a price and return the fill.

    Args:
        prices: Validated reference prices as a float array.
        direction: 1 to buy, -1 to sell.
        impact: Relative impact, same shape or broadcastable.

    Returns:
        The fill price -- a float for scalar input, an ndarray otherwise, so a
        scalar call never has to unwrap a 0-d array.

    Raises:
        ValueError: If the impact is large enough to drive a sell fill to zero
            or below. Both functions reject a non-positive input price, so
            returning one is incoherent: a sell filling at a negative price
            books a nonsensical profit. An order that large is outside the
            model's calibrated domain and the honest answer is a refusal, not
            an extrapolation.
    """
    fill = prices * (1.0 + direction * impact)
    if not np.all(fill > 0.0):
        raise ValueError(
            "impact drives the fill price to zero or below "
            f"(max relative impact {float(np.max(impact)):.4g}); the order is "
            "outside the model's domain -- split it or use a fill model that "
            "accounts for depth"
        )
    return float(fill) if fill.ndim == 0 else fill


def fixed_slippage(price: float, direction: int, bps: float = DEFAULT_SLIPPAGE_BPS) -> float:
    """Apply a constant basis-point slippage to a fill price.

    The cheapest model, and adequate whenever the order is small enough that it
    does not move the book -- roughly under 0.5% of average daily volume.

    Args:
        price: Reference price before slippage.
        direction: 1 to buy, -1 to sell.
        bps: Slippage in basis points. Defaults to :data:`DEFAULT_SLIPPAGE_BPS`.

    Returns:
        The fill price after slippage: worse than ``price`` for both sides.

    Raises:
        ValueError: If ``price`` is not strictly positive, ``direction`` is not 1
            or -1, or ``bps`` is negative.
    """
    prices = _check_order(price, direction)
    rates = np.asarray(bps, dtype=float)
    if not np.all(np.isfinite(rates)) or not np.all(rates >= 0.0):
        raise ValueError(f"bps must be non-negative, got {bps!r}")
    return _fill(prices, direction, rates / _BPS_PER_UNIT)


def linear_impact(
    price: float,
    direction: int,
    volume_traded: float,
    adv: float,
    impact_coeff: float = DEFAULT_LINEAR_IMPACT_COEFF,
) -> float:
    """Apply market impact proportional to the participation rate.

    Impact is ``impact_coeff * volume_traded / adv``. Marginal impact is constant,
    which overstates the cost of very large orders; prefer :func:`sqrt_impact`
    once the order exceeds roughly 5% of average daily volume.

    Args:
        price: Reference price before impact.
        direction: 1 to buy, -1 to sell.
        volume_traded: Order size, in the same unit as ``adv``.
        adv: Average daily volume, strictly positive.
        impact_coeff: Impact coefficient. Defaults to
            :data:`DEFAULT_LINEAR_IMPACT_COEFF`.

    Returns:
        The fill price after impact.

    Raises:
        ValueError: If ``price`` is not strictly positive, ``direction`` is not 1
            or -1, ``volume_traded`` is negative, ``adv`` is not strictly
            positive, or ``impact_coeff`` is negative.
    """
    prices = _check_order(price, direction)
    coeffs = np.asarray(impact_coeff, dtype=float)
    if not np.all(np.isfinite(coeffs)) or not np.all(coeffs >= 0.0):
        raise ValueError(f"impact_coeff must be non-negative, got {impact_coeff!r}")
    return _fill(prices, direction, coeffs * _participation_rate(volume_traded, adv))


def sqrt_impact(
    price: float,
    direction: int,
    volume_traded: float,
    adv: float,
    volatility: float,
    eta: float = DEFAULT_SQRT_IMPACT_ETA,
) -> float:
    """Apply square-root market impact, the best empirically supported form.

    Impact is ``eta * volatility * sqrt(volume_traded / adv)``. Marginal impact
    falls as the order grows, which matches observed execution data far better
    than the linear form.

    This is the impact term familiar from the Almgren-Chriss literature; it is not
    Almgren-Chriss optimal execution, which additionally solves for a trading
    trajectory under a risk-aversion penalty. No trajectory is computed here.

    Args:
        price: Reference price before impact.
        direction: 1 to buy, -1 to sell.
        volume_traded: Order size, in the same unit as ``adv``.
        adv: Average daily volume, strictly positive.
        volatility: Daily return volatility as a decimal fraction, non-negative.
        eta: Impact elasticity. Defaults to :data:`DEFAULT_SQRT_IMPACT_ETA`.

    Returns:
        The fill price after impact.

    Raises:
        ValueError: If ``price`` is not strictly positive, ``direction`` is not 1
            or -1, ``volume_traded`` is negative, ``adv`` is not strictly
            positive, or ``volatility`` or ``eta`` is negative.
    """
    prices = _check_order(price, direction)
    vols = np.asarray(volatility, dtype=float)
    etas = np.asarray(eta, dtype=float)
    if not np.all(np.isfinite(vols)) or not np.all(vols >= 0.0):
        raise ValueError(f"volatility must be non-negative, got {volatility!r}")
    if not np.all(np.isfinite(etas)) or not np.all(etas >= 0.0):
        raise ValueError(f"eta must be non-negative, got {eta!r}")
    impact = etas * vols * np.sqrt(_participation_rate(volume_traded, adv))
    return _fill(prices, direction, impact)


def delayed_execution(signal_series: pd.Series, delay_bars: int = DEFAULT_DELAY_BARS) -> pd.Series:
    """Shift a signal forward to model the lag between decision and fill.

    A signal computed from a bar's data cannot be traded on that same bar. Shifting
    it forward is what keeps a backtest honest.

    Args:
        signal_series: Signal indexed in chronological order.
        delay_bars: Bars to delay by. Defaults to :data:`DEFAULT_DELAY_BARS`
            (T+1). 0 means same-bar execution.

    Returns:
        The signal shifted forward by ``delay_bars``, with the leading positions
        set to NaN by the shift.

    Raises:
        TypeError: If ``signal_series`` is not a :class:`pandas.Series`.
        ValueError: If ``delay_bars`` is negative. A negative shift would pull
            future signal values into the past, which is look-ahead bias and
            silently inflates every backtest that contains it.
    """
    if not isinstance(signal_series, pd.Series):
        raise TypeError(f"signal_series must be a pandas Series, got {type(signal_series).__name__}")
    if delay_bars < 0:
        raise ValueError(f"delay_bars must be non-negative; a negative shift is look-ahead bias, got {delay_bars!r}")
    return signal_series.shift(delay_bars)


def decomposed_impact(
    price: float,
    direction: int,
    volume_traded: float,
    adv: float,
    volatility: float,
    *,
    permanent_fraction: float = 0.3,
    eta: float = DEFAULT_SQRT_IMPACT_ETA,
) -> tuple[float, float]:
    """Split market impact into temporary and permanent components (T4).

    The total impact follows the square-root model (``eta * sigma * sqrt(V/ADV)``),
    then is split into:

    * **Temporary**: dissipates after the current bar; represents the cost of
      consuming liquidity that replenishes.
    * **Permanent**: persists indefinitely; represents information leakage that
      moves the equilibrium price.

    Parameters
    ----------
    price : float
        Reference price before impact, strictly positive.
    direction : int
        +1 for buy, −1 for sell.
    volume_traded : float
        Order size in shares, non-negative.
    adv : float
        Average daily volume, strictly positive.
    volatility : float
        Daily return volatility as a decimal fraction, non-negative.
    permanent_fraction : float
        Fraction of total impact that is permanent (default 0.3).
        Must be in [0, 1].
    eta : float
        Square-root impact elasticity (default :data:`DEFAULT_SQRT_IMPACT_ETA`).

    Returns
    -------
    tuple[float, float]
        ``(temporary_impact, permanent_impact)`` in price units.
        Both values are ≥ 0.  Their sum equals the total sqrt impact.

    Raises
    ------
    ValueError
        If inputs violate constraints (same as :func:`sqrt_impact`, plus
        ``permanent_fraction`` not in [0, 1]).
    """
    if not (0.0 <= permanent_fraction <= 1.0):
        raise ValueError(
            f"permanent_fraction must be in [0, 1], got {permanent_fraction}"
        )

    # Compute total sqrt impact in price units
    fill_price = sqrt_impact(price, direction, volume_traded, adv, volatility, eta=eta)
    total_impact = abs(fill_price - price)

    permanent = total_impact * permanent_fraction
    temporary = total_impact - permanent

    return (temporary, permanent)


def estimate_book_depth(
    high: float,
    low: float,
    volume: float,
    tick_size: float,
) -> float:
    """Estimate effective order-book depth from OHLCV bar data (T5).

    Uses the intraday price range as a proxy for the number of price levels
    touched, and divides volume by that count to get an average per-level
    depth.  This is a *rough* proxy — real L2 data is always preferred — but
    it captures the key intuition that a wide-range, low-volume bar implies
    thin books while a narrow-range, high-volume bar implies thick books.

    Parameters
    ----------
    high : float
        Bar high price, strictly positive.
    low : float
        Bar low price, non-negative, ≤ high.
    volume : float
        Bar volume in shares, non-negative.
    tick_size : float
        Minimum price increment (e.g. 0.01 for A-shares, 0.01 for US equities),
        strictly positive.

    Returns
    -------
    float
        Estimated shares per price level.  Returns ``inf`` when the range is
        zero (single-price bar → effectively infinite depth at that level) or
        when tick_size is zero.

    Raises
    ------
    ValueError
        If any input is negative, high < low, or tick_size ≤ 0.
    """
    if high < 0 or low < 0 or volume < 0:
        raise ValueError("high, low, volume must be non-negative")
    if high < low:
        raise ValueError(f"high ({high}) must be >= low ({low})")
    if tick_size <= 0:
        raise ValueError(f"tick_size must be > 0, got {tick_size}")

    price_range = high - low
    if price_range < tick_size:
        # Single-tick or flat bar → treat as infinite depth
        return float("inf")

    n_levels = max(price_range / tick_size, 1.0)
    return volume / n_levels


def dynamic_slippage(
    order_size: float,
    estimated_depth: float,
    spread_bps: float,
    price: float,
) -> float:
    """Compute slippage that adapts to estimated book depth (T5).

    Small orders (below one level of depth) pay roughly half the spread.
    Larger orders pay the full spread plus a depth-relative impact term.

    Parameters
    ----------
    order_size : float
        Absolute order size in shares, non-negative.
    estimated_depth : float
        Shares per price level from :func:`estimate_book_depth`.  May be
        ``inf`` for flat bars.
    spread_bps : float
        Bid-ask spread in basis points, non-negative.
    price : float
        Reference price, strictly positive.

    Returns
    -------
    float
        Slippage in price units (always ≥ 0).

    Raises
    ------
    ValueError
        If inputs violate constraints.
    """
    if order_size < 0:
        raise ValueError(f"order_size must be >= 0, got {order_size}")
    if spread_bps < 0:
        raise ValueError(f"spread_bps must be >= 0, got {spread_bps}")
    if price <= 0:
        raise ValueError(f"price must be > 0, got {price}")

    half_spread = price * spread_bps / _BPS_PER_UNIT / 2.0

    if not np.isfinite(estimated_depth) or estimated_depth <= 0:
        # Infinite or unknown depth → just charge half spread
        return half_spread

    if order_size <= estimated_depth:
        # Small order: fits within one level → half spread
        return half_spread

    # Large order: full spread + sqrt of depth-relative size
    depth_ratio = order_size / estimated_depth
    extra = price * spread_bps / _BPS_PER_UNIT * 0.5 * np.sqrt(depth_ratio - 1.0)
    return half_spread + float(extra)


__all__ = [
    "DEFAULT_DELAY_BARS",
    "DEFAULT_LINEAR_IMPACT_COEFF",
    "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_SQRT_IMPACT_ETA",
    "decomposed_impact",
    "delayed_execution",
    "dynamic_slippage",
    "estimate_book_depth",
    "fixed_slippage",
    "linear_impact",
    "sqrt_impact",
]
