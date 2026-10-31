"""Conservative shared rules for forward paper trading and chronological replay.

The forward runtime and replay deliberately use the same watch checkpoint helpers.
A checkpoint is recorded only when an actual observation arrives at or after its due
period; elapsed wall-clock time alone is never presented as market coverage.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any


FEE_BPS = 100
SLIPPAGE_BPS = 100
SIDE_COST_RATE = (FEE_BPS + SLIPPAGE_BPS) / 10_000.0
BANKROLL = 1500.0
MAX_OPEN = 6
WATCH_TIERS = ("short", "medium", "long")
RISK_LIMITS = {
    "min_curve_pct": 25.0,
    "max_curve_pct": 85.0,
    "max_mcap_usd": 250_000.0,
    "top10_max": 30.0,
    "dev_max": 5.0,
    "bundle_max": 25.0,
}
EXIT_RULES = {
    "stop_loss_pct": -15.0,
    "scale_out_pct": 25.0,
    "trail_giveback_pct": 12.0,
    "soft_exit_seconds": 12 * 60,
    "soft_exit_below_pct": 5.0,
    "hard_exit_seconds": 40 * 60,
    "flatten_curve_pct": 95.0,
}


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def paper_cost(notional: float) -> float:
    """Model 100 bps fee + 100 bps slippage for one side, separately."""
    return round(max(0.0, notional) * SIDE_COST_RATE, 8)


def position_size(equity: float, *, losing_streak: int = 0) -> float:
    """Use the fixed USD 130..160 base, halving it after three net losses.

    The half-size rule is deliberately applied *after* the conservative base clamp:
    it can reduce risk but can never increase it or exceed the six-position cap.
    """
    base = max(130.0, min(160.0, max(0.0, equity) * 0.015))
    return round(base * (0.5 if int(losing_streak) >= 3 else 1.0), 2)


def risk_reason(observation: dict[str, Any]) -> str | None:
    """Return a conservative gate failure without treating missing risk as zero."""
    market_cap = _finite_number(observation.get("market_cap")) or 0.0
    curve = _finite_number(observation.get("curve_pct")) or 0.0
    if not (RISK_LIMITS["min_curve_pct"] <= curve <= RISK_LIMITS["max_curve_pct"]):
        return "curve outside 25-85%"
    if market_cap > RISK_LIMITS["max_mcap_usd"]:
        return "market cap above $250,000"
    risk = observation.get("risk") or {}
    if risk.get("status") != "known":
        # Explicit conservative policy; unknown never becomes a zero concentration.
        return f"risk metadata unknown: {risk.get('reason') or 'unavailable'}"
    tests = (("top10_pct", "top10_max", "top10"), ("dev_pct", "dev_max", "developer"), ("bundle_pct", "bundle_max", "bundle"))
    for key, limit, label in tests:
        value = _finite_number(risk.get(key))
        if value is None:
            return f"risk metadata unknown: {label}"
        if value > RISK_LIMITS[limit]:
            return f"{label} concentration above {RISK_LIMITS[limit]:g}%"
    return None


def normalize_periods(periods: dict[str, Any]) -> dict[str, int]:
    result = {name: int(periods[name]) for name in WATCH_TIERS}
    short, medium, long = result["short"], result["medium"], result["long"]
    if any(value % 15 for value in result.values()) or not (30 <= short < medium < long <= 1800):
        raise ValueError("watch periods must satisfy 30 <= short < medium < long <= 1800 in 15 second steps")
    return result


def neighbor_periods(champion: dict[str, Any]) -> list[dict[str, int]]:
    """At most seven deterministic ±15 second watch candidates, incl. champion."""
    base = normalize_periods(champion)
    candidates = [base]
    for key in WATCH_TIERS:
        for delta in (-15, 15):
            candidate = dict(base)
            candidate[key] += delta
            try:
                candidate = normalize_periods(candidate)
            except ValueError:
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates[:7]


def make_watch_state(periods: dict[str, Any], *, started_at: float, start_curve: float) -> dict[str, Any]:
    """Create immutable watch durations plus an initially empty actual sample set."""
    return {
        "watch_started_ts": float(started_at),
        "watch_durations": normalize_periods(periods),
        "watch_start_curve": float(start_curve),
        "watch_samples": {},
    }


def _watch_durations(watch: dict[str, Any]) -> dict[str, int]:
    """Read current or pre-checkpoint persisted watch state without changing it."""
    raw = watch.get("watch_durations")
    if not isinstance(raw, dict):
        raw = watch.get("watch_periods")
    if not isinstance(raw, dict):
        raise ValueError("watch durations are unavailable")
    durations: dict[str, int] = {}
    for name in WATCH_TIERS:
        value = raw.get(name)
        if isinstance(value, dict):
            value = value.get("duration_s")
        number = _finite_number(value)
        if number is None:
            raise ValueError("watch duration is unavailable")
        durations[name] = int(number)
    return normalize_periods(durations)


def watch_started_at(watch: dict[str, Any]) -> float:
    value = watch.get("watch_started_ts", watch.get("started_at"))
    number = _finite_number(value)
    if number is None:
        raise ValueError("watch start time is unavailable")
    return number


def _watch_start_curve(watch: dict[str, Any]) -> float:
    value = _finite_number(watch.get("watch_start_curve", watch.get("start_curve")))
    if value is None:
        raise ValueError("watch start curve is unavailable")
    return value


def record_watch_observation(watch: dict[str, Any], observation: dict[str, Any]) -> None:
    """Retain one real sample for every due checkpoint.

    If one late observation crosses multiple due times, each retained checkpoint
    carries the same ``sampled_at`` value. This is truthful: it is one sample that
    satisfied multiple elapsed durations, not invented independent coverage.
    """
    observed_at = _finite_number(observation.get("observed_at"))
    curve = _finite_number(observation.get("curve_pct"))
    if observed_at is None or curve is None:
        return
    started_at = watch_started_at(watch)
    start_curve = _watch_start_curve(watch)
    durations = _watch_durations(watch)
    samples = watch.setdefault("watch_samples", {})
    if not isinstance(samples, dict):
        samples = {}
        watch["watch_samples"] = samples
    elapsed = max(0.0, observed_at - started_at)
    for name in WATCH_TIERS:
        if name in samples or elapsed < durations[name]:
            continue
        samples[name] = {
            "sampled_at": observed_at,
            "elapsed_s": elapsed,
            "curve_delta": curve - start_curve,
        }


def watch_checkpoint_state(watch: dict[str, Any], *, as_of: float | None = None) -> dict[str, dict[str, Any]]:
    """Return the frontend checkpoint schema without claiming a missing sample exists."""
    durations = _watch_durations(watch)
    started_at = watch_started_at(watch)
    samples_raw = watch.get("watch_samples")
    samples = samples_raw if isinstance(samples_raw, dict) else {}
    reference = _finite_number(as_of)
    if reference is None:
        reference = _finite_number(watch.get("last_seen")) or started_at
    elapsed = max(0.0, reference - started_at)
    result: dict[str, dict[str, Any]] = {}
    for name in WATCH_TIERS:
        sample = samples.get(name)
        if isinstance(sample, dict):
            sample_elapsed = _finite_number(sample.get("elapsed_s"))
            sampled_at = _finite_number(sample.get("sampled_at"))
            delta = _finite_number(sample.get("curve_delta"))
            item: dict[str, Any] = {
                "duration_s": durations[name],
                "elapsed_s": round(max(0.0, sample_elapsed if sample_elapsed is not None else elapsed), 6),
                "status": "observed",
            }
            if delta is not None:
                item["curve_delta"] = round(delta, 6)
            if sampled_at is not None:
                item["sampled_at"] = sampled_at
            result[name] = item
        else:
            result[name] = {
                "duration_s": durations[name],
                "elapsed_s": round(elapsed, 6),
                "status": "expired" if elapsed >= durations[name] else "watching",
            }
    return result


def watch_entry_reason(observation: dict[str, Any], watch: dict[str, Any]) -> str | None:
    """Return a truthful watch/flow failure shared by forward and replay paths.

    Check 3 documents the three actual checkpoint rule: short and medium must be
    non-negative, while long preserves the original no-decline condition. Check 2
    preserves the original flow rule of at least two replies *or* a one-point long
    curve rise. These requirements only add conservative filters.
    """
    observed_at = _finite_number(observation.get("observed_at"))
    if observed_at is None:
        return "checkpoint check 3/3: observation timestamp unavailable"
    checkpoints = watch_checkpoint_state(watch, as_of=observed_at)
    for name in WATCH_TIERS:
        checkpoint = checkpoints[name]
        if checkpoint["status"] != "observed":
            return f"checkpoint check 3/3: {name} duration has no actual observation"
        delta = _finite_number(checkpoint.get("curve_delta"))
        if delta is None:
            return f"checkpoint check 3/3: {name} curve delta unavailable"
        if name in ("short", "medium") and delta < 0:
            return f"checkpoint check 3/3: {name} curve declined ({delta:.2f} points)"
        if name == "long" and delta < 0:
            return f"checkpoint check 3/3: long curve did not rise ({delta:.2f} points)"
    long_delta = float(checkpoints["long"]["curve_delta"])
    reply_count = _finite_number(observation.get("reply_count")) or 0.0
    if reply_count < 2.0 and long_delta < 1.0:
        return "flow check 2/3: replies below 2 and long curve rise below 1 point"
    return None


def entry_reason(observation: dict[str, Any], watch: dict[str, Any]) -> str | None:
    """Apply unchanged risk caps plus shared flow/checkpoint entry requirements."""
    reason = risk_reason(observation)
    if reason is not None:
        return f"risk check 1/3: {reason}"
    return watch_entry_reason(observation, watch)


def initial_portfolio() -> dict[str, Any]:
    return {
        "bankroll": BANKROLL,
        "cash": BANKROLL,
        "gross_realized": 0.0,
        "modeled_costs": 0.0,
        "net_realized": 0.0,
        "peak_equity": BANKROLL,
        "drawdown_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "closed_trades": 0,
        "losing_streak": 0,
        "positions": {},
    }


def portfolio_equity(portfolio: dict[str, Any]) -> float:
    gross_open = sum(
        float(position["remaining_quantity"]) * float(position.get("last_mcap", position["entry_mcap"]))
        for position in portfolio.get("positions", {}).values()
    )
    return round(float(portfolio["cash"]) + gross_open, 8)


def refresh_equity(portfolio: dict[str, Any]) -> float:
    equity = portfolio_equity(portfolio)
    portfolio["peak_equity"] = max(float(portfolio.get("peak_equity", equity)), equity)
    peak = max(float(portfolio["peak_equity"]), 0.000001)
    current_drawdown = round(max(0.0, 100.0 * (peak - equity) / peak), 6)
    portfolio["drawdown_pct"] = current_drawdown
    portfolio["max_drawdown_pct"] = max(float(portfolio.get("max_drawdown_pct", 0.0)), current_drawdown)
    return equity


def open_position(
    portfolio: dict[str, Any],
    observation: dict[str, Any],
    *,
    strategy_revision: int,
    watch: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if len(portfolio["positions"]) >= MAX_OPEN:
        return None
    equity = refresh_equity(portfolio)
    notional = position_size(equity, losing_streak=int(portfolio.get("losing_streak", 0)))
    cost = paper_cost(notional)
    if float(portfolio["cash"]) + 1e-9 < notional + cost:
        return None
    price = float(observation["market_cap"])
    if price <= 0:
        return None
    entry_at = float(observation["observed_at"])
    position_id = f"{observation['mint']}:{int(entry_at * 1000)}"
    position = {
        "id": position_id,
        "mint": observation["mint"],
        "symbol": observation.get("symbol", "?"),
        "name": observation.get("name", ""),
        "entry_at": entry_at,
        "entry_mcap": price,
        "last_mcap": price,
        "peak_mcap": price,
        "initial_notional": notional,
        "remaining_quantity": notional / price,
        "remaining_basis": notional,
        "total_exit_proceeds": 0.0,
        "costs": cost,
        "scaled": False,
        "strategy_revision": strategy_revision,
        "risk": deepcopy(observation.get("risk") or {}),
        "entry_reply_count": observation.get("reply_count"),
        "entry_source": observation.get("source"),
        "social": {"status": "unavailable", "sentiment": None},
    }
    if watch is not None:
        position["watch_started_ts"] = watch_started_at(watch)
        position["watch_periods"] = watch_checkpoint_state(watch, as_of=entry_at)
    portfolio["cash"] = round(float(portfolio["cash"]) - notional - cost, 8)
    portfolio["positions"][position_id] = position
    refresh_equity(portfolio)
    return position


def exit_reason(position: dict[str, Any], observation: dict[str, Any]) -> tuple[str, float] | None:
    current = float(observation["market_cap"])
    entry = float(position["entry_mcap"])
    change = 100.0 * (current / entry - 1.0)
    age = float(observation["observed_at"]) - float(position["entry_at"])
    curve = float(observation["curve_pct"])
    if change <= EXIT_RULES["stop_loss_pct"]:
        return (f"stop loss {change:.1f}%", 1.0)
    initial_dev = _finite_number((position.get("risk") or {}).get("dev_pct"))
    current_risk = observation.get("risk") or {}
    current_dev = _finite_number(current_risk.get("dev_pct")) if current_risk.get("status") == "known" else None
    if initial_dev is not None and current_dev is not None and current_dev + 1e-9 < initial_dev:
        return (f"developer balance decreased {initial_dev:.2f}%→{current_dev:.2f}%", 1.0)
    if curve >= EXIT_RULES["flatten_curve_pct"]:
        return (f"flatten curve {curve:.1f}%", 1.0)
    if not position.get("scaled") and change >= EXIT_RULES["scale_out_pct"]:
        return (f"scale half {change:.1f}%", 0.5)
    if position.get("scaled") and current < float(position["peak_mcap"]) * (1.0 - EXIT_RULES["trail_giveback_pct"] / 100.0):
        return (f"trail 12% {change:.1f}%", 1.0)
    if not position.get("scaled") and age >= EXIT_RULES["soft_exit_seconds"] and change < EXIT_RULES["soft_exit_below_pct"]:
        return (f"soft 12m {change:.1f}%", 1.0)
    if age >= EXIT_RULES["hard_exit_seconds"]:
        return (f"hard 40m {change:.1f}%", 1.0)
    return None


def apply_observation(portfolio: dict[str, Any], position_id: str, observation: dict[str, Any]) -> dict[str, Any] | None:
    """Apply an actual market-cap sample. Pending positions are never force-closed."""
    position = portfolio["positions"].get(position_id)
    if not position:
        return None
    price = float(observation["market_cap"])
    if price <= 0:
        return None
    position["last_mcap"] = price
    position["peak_mcap"] = max(float(position["peak_mcap"]), price)
    event = exit_reason(position, observation)
    if event is None:
        refresh_equity(portfolio)
        return None
    reason, fraction = event
    quantity = float(position["remaining_quantity"]) * fraction
    basis = float(position["remaining_basis"]) * fraction
    proceeds = quantity * price
    cost = paper_cost(proceeds)
    position["remaining_quantity"] = round(float(position["remaining_quantity"]) - quantity, 12)
    position["remaining_basis"] = round(float(position["remaining_basis"]) - basis, 8)
    position["total_exit_proceeds"] = round(float(position["total_exit_proceeds"]) + proceeds, 8)
    position["costs"] = round(float(position["costs"]) + cost, 8)
    portfolio["cash"] = round(float(portfolio["cash"]) + proceeds - cost, 8)
    if fraction < 1.0:
        position["scaled"] = True
        refresh_equity(portfolio)
        return {
            "status": "scaled", "id": position_id, "reason": reason, "at": float(observation["observed_at"]),
            "proceeds": round(proceeds, 8), "cost": round(cost, 8),
        }
    portfolio["positions"].pop(position_id, None)
    gross = float(position["total_exit_proceeds"]) - float(position["initial_notional"])
    costs = float(position["costs"])
    net = gross - costs
    portfolio["gross_realized"] = round(float(portfolio["gross_realized"]) + gross, 8)
    portfolio["modeled_costs"] = round(float(portfolio["modeled_costs"]) + costs, 8)
    portfolio["net_realized"] = round(float(portfolio["net_realized"]) + net, 8)
    portfolio["closed_trades"] = int(portfolio["closed_trades"]) + 1
    portfolio["losing_streak"] = int(portfolio.get("losing_streak", 0)) + 1 if net < 0 else 0
    refresh_equity(portfolio)
    return {
        "status": "closed",
        "id": position_id,
        "mint": position["mint"],
        "symbol": position["symbol"],
        "name": position.get("name", ""),
        "entry_at": position["entry_at"],
        "exit_at": float(observation["observed_at"]),
        "entry_mcap": position["entry_mcap"],
        "exit_mcap": price,
        "entry_notional": round(float(position["initial_notional"]), 8),
        "exit_proceeds": round(float(position["total_exit_proceeds"]), 8),
        "gross": round(gross, 8),
        "costs": round(costs, 8),
        "net": round(net, 8),
        "reason": reason,
        "strategy_revision": position["strategy_revision"],
        "risk": position["risk"],
        "entry_reply_count": position.get("entry_reply_count"),
        "entry_source": position.get("entry_source"),
        "social": position["social"],
        "watch_started_ts": position.get("watch_started_ts"),
        "watch_periods": deepcopy(position.get("watch_periods") or {}),
    }
