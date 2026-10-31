"""Pure paper exit helpers: gap, volatility band, and fee-inclusive break-even.

The hard stop remains the widest loss. Volatility can only tighten it.
None of these functions place an order.
"""
from __future__ import annotations


def emergency_drop(prev_spot: float | None, spot: float | None, drop_pct: float = 10) -> bool:
    """True when spot fell by at least ``drop_pct`` versus the previous check."""
    if not prev_spot or not spot or prev_spot <= 0:
        return False
    change = (float(spot) / float(prev_spot) - 1.0) * 100.0
    # A tenth of a basis point of slack so an exact 10% print is a drop, not a float miss.
    return change <= -abs(drop_pct) + 1e-6


def net_change_pct(entry_eff: float, exit_eff: float) -> float:
    if not entry_eff:
        return 0.0
    return 100.0 * (float(exit_eff) / float(entry_eff) - 1.0)


def profit_lock_hit(peak_net: float, net: float, arm_pct: float = 8.0, giveback_pct: float = 6.0) -> bool:
    """True when a not-yet-scaled paper gain should be closed in full.

    The lock stays off until peak net reaches ``arm_pct``. The line is
    ``max(0, peak_net - giveback)``, so the threshold itself is never a loss.
    A later print can still gap through that line. Callers try the scale-out
    first: a print already at the scale target is a half sale, not a full lock.
    """
    if float(peak_net) + 1e-9 < abs(float(arm_pct)):
        return False
    floor = max(0.0, float(peak_net) - abs(float(giveback_pct)))
    return float(net) <= floor + 1e-6


def vol_stop_hit(entry_spot: float | None, spot: float | None, band: float, hard: float) -> bool:
    """True when spot itself has reached a band that is tighter than the hard stop.

    ``band`` comes from spot returns. Comparing it to fee-inclusive net treats the
    entry fee and curve impact as a price drop, so an unchanged spot on a thin
    book is already through the −4% floor. This check uses the spot move from
    the entry spot. The hard net stop stays with the caller. A band that is not
    tighter than ``hard`` does not fire here.
    """
    try:
        entry = float(entry_spot) if entry_spot is not None else 0.0
        mark = float(spot) if spot is not None else 0.0
        band_pct = float(band)
        hard_pct = float(hard)
    except (TypeError, ValueError):
        return False
    if entry <= 0 or mark <= 0:
        return False
    if band_pct <= hard_pct + 1e-9:
        return False
    change = (mark / entry - 1.0) * 100.0
    return change <= band_pct + 1e-6


def vol_stop_pct(returns: list, strat: dict) -> float:
    """Negative percent. Sample-starved paths keep the hard stop. Otherwise clamp to [hard, floor].

    ``floor`` is the tightest band (a -4% stop). The hard stop (typically -15%) is the widest.
    """
    hard = float(strat["stop_loss_pct"])
    floor = -abs(float(strat["vol_stop_floor_pct"]))
    need = int(strat["vol_stop_min_samples"])
    if len(returns) < need:
        return hard
    mean = sum(returns) / len(returns)
    var = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    sigma_pct = (var ** 0.5) * 100.0
    raw = -abs(float(strat["vol_stop_mult"])) * sigma_pct
    # min(raw, floor) pushes a too-tight raw (for example -1) out to the floor (-4).
    # max(..., hard) refuses to sit wider than the hard stop.
    return max(hard, min(raw, floor))
