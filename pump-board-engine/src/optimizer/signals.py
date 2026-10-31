"""Deterministic, policy-backed readiness diagnostics for the paper optimizer.

This module deliberately does not make market requests or change trading state.  It
summarises actual captured observations and invokes the authoritative policy only
on deep copies.  The result is a diagnostic aid, not a prediction of profit or
execution.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import re
from typing import Any, Iterable

from ..exits import failed_impulse_hit, profit_lock_hit
from .policy import (
    EXIT_RULES,
    MAX_OPEN,
    curve_rate_reason,
    entry_reason,
    exit_reason,
    net_of_costs_pct,
    open_position,
    watch_checkpoint_state,
)


MODEL = "gate-readiness-v1"
DISCLAIMER = (
    "Readiness is strategy-gate progress for current sampled data (buy) or nearest "
    "applicable exit-trigger proximity (sell). It is not a calibrated probability "
    "of profit or execution."
)
MAX_CANDIDATES = 64
MAX_EVENTS = 120
FRESH_SECONDS = 120.0
_ALLOWED_STATES = {"pass", "fail", "unknown", "pending"}
_ALLOWED_STATUS = {"ready", "approaching", "blocked", "waiting", "stale"}
_ALLOWED_ACTIONS = {"WAIT", "BLOCK", "READY", "BUY", "SCALE", "SELL", "SKIP", "EXPIRE"}
_URL = re.compile(r"(?i)https?://\S+")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _percent(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _text(value: Any, *, fallback: str = "unavailable", limit: int = 280) -> str:
    if value is None:
        return fallback
    rendered = _URL.sub("[provider URL omitted]", str(value)).replace("\n", " ").strip()
    return (rendered or fallback)[:limit]


def _identifier(value: Any, *, fallback: str) -> str:
    rendered = _text(value, fallback=fallback, limit=160)
    return rendered if rendered else fallback


def _gate(key: Any, label: Any, state: Any, actual: Any, required: Any, progress: float = 0.0) -> dict[str, Any]:
    return {
        "key": _text(key),
        "label": _text(label),
        "state": state if isinstance(state, str) and state in _ALLOWED_STATES else "unknown",
        "actual": _text(actual),
        "required": _text(required),
        "progressPct": _percent(progress),
    }


def _is_fresh(observed_at: float | None, now: float) -> bool:
    return observed_at is not None and observed_at <= now and now - observed_at <= FRESH_SECONDS


def _quote_gate(observation: dict[str, Any] | None, *, now: float) -> tuple[dict[str, Any], bool, float | None, float | None]:
    if not isinstance(observation, dict):
        return _gate("quote", "Fresh positive market-cap quote", "unknown", "no actual sampled quote", "fresh actual market cap > $0"), False, None, None
    observed_at = _number(observation.get("observed_at"))
    market_cap = _number(observation.get("market_cap"))
    if observed_at is None:
        return _gate("quote", "Fresh positive market-cap quote", "unknown", "sample timestamp unavailable", "fresh actual market cap > $0"), False, None, market_cap
    if not _is_fresh(observed_at, now):
        return _gate("quote", "Fresh positive market-cap quote", "unknown", f"sampled at {observed_at:.3f}; stale", "fresh actual market cap > $0"), False, observed_at, market_cap
    if market_cap is None or market_cap <= 0:
        return _gate("quote", "Fresh positive market-cap quote", "fail", "market cap unavailable or non-positive", "fresh actual market cap > $0"), False, observed_at, market_cap
    return _gate("quote", "Fresh positive market-cap quote", "pass", f"actual ${market_cap:,.2f} at {observed_at:.3f}", "fresh actual market cap > $0", 100), True, observed_at, market_cap


def _risk_gates(observation: dict[str, Any]) -> list[dict[str, Any]]:
    raw_risk = observation.get("risk")
    risk: dict[str, Any] = raw_risk if isinstance(raw_risk, dict) else {}
    if risk.get("status") != "known":
        return [
            _gate("top10", "Top-10 concentration", "unknown", "actual holder concentration unavailable", "top10 ≤ 30%"),
            _gate("developer", "Developer concentration", "unknown", "actual developer concentration unavailable", "developer ≤ 5%"),
            _gate("bundle", "Bundle concentration", "unknown", "actual bundle concentration unavailable", "bundle ≤ 25%"),
        ]
    result: list[dict[str, Any]] = []
    for key, label, limit in (
        ("top10_pct", "Top-10 concentration", 30.0),
        ("dev_pct", "Developer concentration", 5.0),
        ("bundle_pct", "Bundle concentration", 25.0),
    ):
        output_key = {"top10_pct": "top10", "dev_pct": "developer", "bundle_pct": "bundle"}[key]
        value = _number(risk.get(key))
        if value is None:
            result.append(_gate(output_key, label, "unknown", "actual concentration unavailable", f"{label.lower()} ≤ {limit:g}%"))
        elif value <= limit:
            result.append(_gate(output_key, label, "pass", f"actual {value:.2f}%", f"≤ {limit:g}%", 100))
        else:
            result.append(_gate(output_key, label, "fail", f"actual {value:.2f}%", f"≤ {limit:g}%", _percent(limit / max(value, 0.000001) * 100)))
    return result


def _curve_gates(observation: dict[str, Any]) -> list[dict[str, Any]]:
    curve_known = observation.get("curve_known")
    curve = _number(observation.get("curve_pct"))
    market_cap = _number(observation.get("market_cap"))
    if curve_known is not True or curve is None:
        curve_gate = _gate("curve", "Verified bonding-curve range", "unknown", "verified reserve curve unavailable", "verified 25–85% curve")
    elif 25.0 <= curve <= 85.0:
        curve_gate = _gate("curve", "Verified bonding-curve range", "pass", f"verified actual {curve:.2f}%", "25–85%", 100)
    else:
        distance = 25.0 - curve if curve < 25.0 else curve - 85.0
        curve_gate = _gate("curve", "Verified bonding-curve range", "fail", f"verified actual {curve:.2f}%", "25–85%", _percent(100 - distance * 4))
    if market_cap is None or market_cap <= 0:
        mcap_gate = _gate("mcap", "Market-cap ceiling", "unknown", "positive market cap unavailable", "market cap ≤ $250,000")
    elif market_cap <= 250_000.0:
        mcap_gate = _gate("mcap", "Market-cap ceiling", "pass", f"actual ${market_cap:,.2f}", "≤ $250,000", 100)
    else:
        mcap_gate = _gate("mcap", "Market-cap ceiling", "fail", f"actual ${market_cap:,.2f}", "≤ $250,000", _percent(250_000.0 / market_cap * 100))
    return [curve_gate, mcap_gate]


def _watch_gates(track: dict[str, Any] | None, observation: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not isinstance(track, dict):
        return [
            _gate("checkpoints", "Three actual nondeclining checkpoints", "unknown", "token was not admitted to a captured watch", "3 distinct actual samples with nondeclining curve"),
            _gate("curve_rate", "Accelerating curve rate", "unknown", "captured checkpoints unavailable", "three distinct increasing point-per-second rates"),
            _gate("flow", "Replies or long-window curve flow", "unknown", "captured long checkpoint unavailable", "replies ≥ 2 OR long curve Δ ≥ 1 point"),
        ], None
    observed_at = _number(observation.get("observed_at"))
    if observed_at is None:
        return [
            _gate("checkpoints", "Three actual nondeclining checkpoints", "unknown", "observation timestamp unavailable", "3 distinct actual samples with nondeclining curve"),
            _gate("curve_rate", "Accelerating curve rate", "unknown", "captured checkpoints unavailable", "three distinct increasing point-per-second rates"),
            _gate("flow", "Replies or long-window curve flow", "unknown", "captured long checkpoint unavailable", "replies ≥ 2 OR long curve Δ ≥ 1 point"),
        ], None
    try:
        checkpoints = watch_checkpoint_state(deepcopy(track), as_of=observed_at)
    except (TypeError, ValueError, KeyError):
        return [
            _gate("checkpoints", "Three actual nondeclining checkpoints", "unknown", "captured checkpoint state malformed", "3 distinct actual samples with nondeclining curve"),
            _gate("curve_rate", "Accelerating curve rate", "unknown", "captured checkpoints unavailable", "three distinct increasing point-per-second rates"),
            _gate("flow", "Replies or long-window curve flow", "unknown", "captured long checkpoint unavailable", "replies ≥ 2 OR long curve Δ ≥ 1 point"),
        ], None

    names = ("short", "medium", "long")
    observed: list[dict[str, Any]] = []
    for name in names:
        raw_checkpoint = checkpoints.get(name)
        if isinstance(raw_checkpoint, dict) and raw_checkpoint.get("status") == "observed":
            observed.append(raw_checkpoint)
    checkpoint_details: list[str] = []
    valid_deltas = True
    nondeclining = True
    times: list[float] = []
    for name in names:
        raw_checkpoint = checkpoints.get(name)
        item: dict[str, Any] = raw_checkpoint if isinstance(raw_checkpoint, dict) else {}
        delta = _number(item.get("curve_delta"))
        sampled_at = _number(item.get("sampled_at"))
        if item.get("status") != "observed" or delta is None or sampled_at is None:
            valid_deltas = False
            checkpoint_details.append(f"{name}: no actual sample")
        else:
            nondeclining = nondeclining and delta >= 0
            times.append(sampled_at)
            checkpoint_details.append(f"{name} Δ{delta:+.2f} at {sampled_at:.3f}")
    if len(observed) < 3 or not valid_deltas:
        checkpoint_gate = _gate("checkpoints", "Three actual nondeclining checkpoints", "pending", f"{len(observed)}/3 actual checkpoints; " + "; ".join(checkpoint_details), "3 distinct actual samples with nondeclining curve", len(observed) / 3 * 75)
    elif len(set(times)) < 3 or not (times[0] < times[1] < times[2]):
        checkpoint_gate = _gate("checkpoints", "Three actual nondeclining checkpoints", "fail", "; ".join(checkpoint_details), "3 distinct actual samples", len(set(times)) / 3 * 65)
    elif not nondeclining:
        checkpoint_gate = _gate("checkpoints", "Three actual nondeclining checkpoints", "fail", "; ".join(checkpoint_details), "all checkpoint curve deltas ≥ 0", 40)
    else:
        checkpoint_gate = _gate("checkpoints", "Three actual nondeclining checkpoints", "pass", "; ".join(checkpoint_details), "3 distinct actual samples with nondeclining curve", 100)

    if checkpoint_gate["state"] == "pending":
        rate_gate = _gate("curve_rate", "Accelerating curve rate", "pending", "awaiting three actual checkpoints", "three distinct increasing point-per-second rates", checkpoint_gate["progressPct"])
    elif checkpoint_gate["state"] != "pass":
        rate_gate = _gate("curve_rate", "Accelerating curve rate", "fail", "checkpoint prerequisite did not pass", "three distinct increasing point-per-second rates", checkpoint_gate["progressPct"])
    else:
        try:
            rate_reason = curve_rate_reason(checkpoints)
        except (TypeError, ValueError, KeyError):
            rate_reason = "flow rate unavailable"
        if rate_reason is None:
            rate_gate = _gate("curve_rate", "Accelerating curve rate", "pass", "three distinct actual curve rates accelerate", "increasing point-per-second rates", 100)
        else:
            rate_gate = _gate("curve_rate", "Accelerating curve rate", "fail", rate_reason, "increasing point-per-second rates", 45)

    reply_count = _number(observation.get("reply_count"))
    long_delta = _number((checkpoints.get("long") or {}).get("curve_delta"))
    if (reply_count is not None and reply_count >= 2) or (long_delta is not None and long_delta >= 1):
        flow_gate = _gate("flow", "Replies or long-window curve flow", "pass", f"actual replies {reply_count if reply_count is not None else 'unavailable'}; long Δ {long_delta if long_delta is not None else 'unavailable'}", "replies ≥ 2 OR long curve Δ ≥ 1 point", 100)
    elif reply_count is None or long_delta is None:
        flow_gate = _gate("flow", "Replies or long-window curve flow", "unknown", f"actual replies {reply_count if reply_count is not None else 'unavailable'}; long Δ {long_delta if long_delta is not None else 'unavailable'}", "replies ≥ 2 OR long curve Δ ≥ 1 point")
    else:
        flow_gate = _gate("flow", "Replies or long-window curve flow", "fail", f"actual replies {reply_count:.0f}; long Δ {long_delta:.2f}", "replies ≥ 2 OR long curve Δ ≥ 1 point", _percent(max(reply_count / 2 * 100, long_delta * 100)))
    return [checkpoint_gate, rate_gate, flow_gate], checkpoints


def _impact_gate(observation: dict[str, Any]) -> dict[str, Any]:
    # The policy deliberately permits older/no-reserve observations to use the flat
    # cost model. Calling it here keeps the explanation exactly aligned with entry.
    try:
        from .policy import impact_entry_reason

        reason = impact_entry_reason(deepcopy(observation))
    except (TypeError, ValueError, KeyError, OverflowError):
        return _gate("impact", "Existing impact filter", "unknown", "impact inputs malformed", "must not breach existing impact threshold")
    if reason is None:
        return _gate("impact", "Existing impact filter", "pass", "no existing-policy impact rejection", "must not breach existing impact threshold", 100)
    return _gate("impact", "Existing impact filter", "fail", reason, "must not breach existing impact threshold", 0)


def _entry_preview(
    portfolio: dict[str, Any],
    observation: dict[str, Any],
    track: dict[str, Any] | None,
    *,
    strategy_revision: int,
) -> tuple[dict[str, Any], dict[str, Any], str | None, bool]:
    positions = portfolio.get("positions") if isinstance(portfolio, dict) else None
    if not isinstance(positions, dict):
        slot_gate = _gate("slots", "Open-position capacity", "unknown", "portfolio positions unavailable", f"fewer than {MAX_OPEN} open positions")
    elif len(positions) >= MAX_OPEN:
        slot_gate = _gate("slots", "Open-position capacity", "fail", f"actual {len(positions)} open positions", f"fewer than {MAX_OPEN} open positions", _percent(MAX_OPEN / max(1, len(positions)) * 100))
    else:
        slot_gate = _gate("slots", "Open-position capacity", "pass", f"actual {len(positions)} open positions", f"fewer than {MAX_OPEN} open positions", 100)

    authority_reason: str | None = None
    if isinstance(track, dict):
        try:
            authority_reason = entry_reason(deepcopy(observation), deepcopy(track))
        except (TypeError, ValueError, KeyError, OverflowError):
            authority_reason = "policy inputs unavailable"
    else:
        authority_reason = "captured watch unavailable"

    if authority_reason is None:
        policy_gate = _gate("policy", "Authoritative entry policy", "pass", "existing entry_reason accepted current copied data", "existing entry_reason must accept", 100)
    elif "unavailable" in authority_reason or "unknown" in authority_reason or "no actual observation" in authority_reason:
        policy_gate = _gate("policy", "Authoritative entry policy", "unknown", authority_reason, "existing entry_reason must accept")
    elif "windows incomplete" in authority_reason or "checkpoint check" in authority_reason:
        policy_gate = _gate("policy", "Authoritative entry policy", "pending", authority_reason, "existing entry_reason must accept", 45)
    else:
        policy_gate = _gate("policy", "Authoritative entry policy", "fail", authority_reason, "existing entry_reason must accept")

    preview_ok = False
    if slot_gate["state"] == "pass":
        try:
            preview_portfolio = deepcopy(portfolio)
            preview_position = open_position(
                preview_portfolio,
                deepcopy(observation),
                strategy_revision=strategy_revision,
                watch=deepcopy(track) if isinstance(track, dict) else None,
            )
            preview_ok = preview_position is not None
        except (TypeError, ValueError, KeyError, OverflowError):
            preview_ok = False
    if preview_ok:
        cash_gate = _gate("cash", "Paper cash for policy-sized entry", "pass", f"actual cash ${_number(portfolio.get('cash')) or 0.0:,.2f} covers copied policy-sized entry plus modeled cost", "cash covers policy-sized entry plus modeled cost", 100)
    elif slot_gate["state"] != "pass":
        cash_gate = _gate("cash", "Paper cash for policy-sized entry", "unknown", "not evaluated because no slot is available", "cash covers policy-sized entry plus modeled cost")
    else:
        cash_value = _number(portfolio.get("cash")) if isinstance(portfolio, dict) else None
        if cash_value is None:
            cash_gate = _gate("cash", "Paper cash for policy-sized entry", "unknown", "actual cash unavailable", "cash covers policy-sized entry plus modeled cost")
        else:
            cash_gate = _gate("cash", "Paper cash for policy-sized entry", "fail", f"actual cash ${cash_value:,.2f} cannot open copied policy-sized entry", "cash covers policy-sized entry plus modeled cost")
    return slot_gate, cash_gate, authority_reason, preview_ok


def buy_candidate(
    observation: dict[str, Any] | None,
    track: dict[str, Any] | None,
    portfolio: dict[str, Any],
    *,
    strategy_revision: int,
    now: float,
) -> dict[str, Any]:
    observation = observation if isinstance(observation, dict) else {}
    mint = _identifier(observation.get("mint") or (track or {}).get("mint"), fallback="unknown-mint")
    symbol = _identifier(observation.get("symbol") or (track or {}).get("symbol"), fallback="?")
    quote_gate, quote_current, observed_at, _market_cap = _quote_gate(observation, now=now)
    gates: list[dict[str, Any]] = [quote_gate]
    gates.extend(_curve_gates(observation))
    gates.extend(_risk_gates(observation))
    watch_gates, _checkpoints = _watch_gates(track, observation)
    gates.extend(watch_gates)
    gates.append(_impact_gate(observation))
    slot_gate, cash_gate, authority_reason, preview_ok = _entry_preview(portfolio, observation, track, strategy_revision=strategy_revision)
    gates.extend([slot_gate, cash_gate])
    if authority_reason is None:
        gates.append(_gate("policy", "Authoritative entry policy", "pass", "existing entry_reason accepted current copied data", "existing entry_reason must accept", 100))
    elif "unavailable" in authority_reason or "unknown" in authority_reason or "no actual observation" in authority_reason:
        gates.append(_gate("policy", "Authoritative entry policy", "unknown", authority_reason, "existing entry_reason must accept"))
    elif "windows incomplete" in authority_reason or "checkpoint check" in authority_reason:
        gates.append(_gate("policy", "Authoritative entry policy", "pending", authority_reason, "existing entry_reason must accept", 45))
    else:
        gates.append(_gate("policy", "Authoritative entry policy", "fail", authority_reason, "existing entry_reason must accept"))

    if not quote_current:
        status = "stale" if observed_at is None or not _is_fresh(observed_at, now) else "blocked"
        reason = "stale sampled quote; readiness unavailable" if status == "stale" else quote_gate["actual"]
        readiness: float | None = None
    else:
        all_pass = all(gate["state"] == "pass" for gate in gates)
        policy_ready = authority_reason is None and preview_ok and all_pass
        if policy_ready:
            status = "ready"
            readiness = 100.0
            reason = "all current strategy gates pass on copied authoritative policy inputs; diagnostic only, not an execution"
        else:
            readiness = _percent(sum(float(gate["progressPct"]) for gate in gates) / max(1, len(gates)))
            failed = next((gate for gate in gates if gate["state"] == "fail"), None)
            unknown = next((gate for gate in gates if gate["state"] == "unknown"), None)
            pending = next((gate for gate in gates if gate["state"] == "pending"), None)
            if failed is not None:
                status, reason = "blocked", f"{failed['label']}: {failed['actual']}"
            elif unknown is not None:
                status, reason = "blocked", f"{unknown['label']}: {unknown['actual']}"
            elif pending is not None:
                status = "approaching" if readiness >= 35.0 else "waiting"
                reason = f"{pending['label']}: {pending['actual']}"
            else:
                status, reason = "blocked", "authoritative copied policy did not produce an entry"
            # A policy block must never be presented as all the way ready.
            readiness = min(readiness, 99.0)
    return {
        "mint": mint,
        "symbol": symbol,
        "side": "buy",
        "readinessPct": readiness,
        "status": status if status in _ALLOWED_STATUS else "blocked",
        "observedAt": observed_at,
        "reason": _text(reason),
        "gates": gates,
    }


def _exit_trigger_gates(position: dict[str, Any], observation: dict[str, Any]) -> tuple[list[dict[str, Any]], tuple[str, float] | None]:
    entry = _number(position.get("entry_mcap"))
    current = _number(observation.get("market_cap"))
    observed_at = _number(observation.get("observed_at"))
    entry_at = _number(position.get("entry_at"))
    if entry is None or entry <= 0 or current is None or current <= 0 or observed_at is None or entry_at is None:
        unknown = _gate("exit_policy", "Authoritative exit policy", "unknown", "position or quote inputs unavailable", "existing exit_reason must evaluate")
        return [unknown], None

    copied = deepcopy(position)
    copied["last_mcap"] = current
    peak = _number(copied.get("peak_mcap"))
    copied["peak_mcap"] = max(current, peak if peak is not None and peak > 0 else current)
    try:
        authoritative = exit_reason(copied, deepcopy(observation))
    except (TypeError, ValueError, KeyError, OverflowError):
        authoritative = None
    net = net_of_costs_pct(entry, current)
    change = 100.0 * (current / entry - 1.0)
    age = max(0.0, observed_at - entry_at)
    gates: list[dict[str, Any]] = []

    if net <= EXIT_RULES["stop_loss_pct"]:
        gates.append(_gate("stop", "Net stop loss", "pass", f"actual net {net:.2f}% after modeled entry+exit costs", f"net ≤ {EXIT_RULES['stop_loss_pct']:.0f}%", 100))
    else:
        gates.append(_gate("stop", "Net stop loss", "pending", f"actual net {net:.2f}% after modeled entry+exit costs", f"net ≤ {EXIT_RULES['stop_loss_pct']:.0f}%", _percent(max(0.0, -net) / abs(EXIT_RULES["stop_loss_pct"]) * 100)))

    initial_dev = _number((position.get("risk") or {}).get("dev_pct")) if isinstance(position.get("risk"), dict) else None
    raw_current_risk = observation.get("risk")
    current_risk: dict[str, Any] = raw_current_risk if isinstance(raw_current_risk, dict) else {}
    current_dev = _number(current_risk.get("dev_pct")) if current_risk.get("status") == "known" else None
    if initial_dev is None or current_dev is None:
        gates.append(_gate("dev_drop", "Developer-balance drop", "unknown", "actual entry/current developer comparison unavailable", "current developer balance below entry balance"))
    elif current_dev + 1e-9 < initial_dev:
        gates.append(_gate("dev_drop", "Developer-balance drop", "pass", f"actual {initial_dev:.2f}% → {current_dev:.2f}%", "current developer balance below entry balance", 100))
    else:
        gates.append(_gate("dev_drop", "Developer-balance drop", "pending", f"actual {initial_dev:.2f}% → {current_dev:.2f}%", "current developer balance below entry balance", 0))

    curve = _number(observation.get("curve_pct"))
    if observation.get("curve_known") is not True or curve is None:
        gates.append(_gate("curve_exit", "Curve flatten", "unknown", "verified current curve unavailable", f"verified curve ≥ {EXIT_RULES['flatten_curve_pct']:.0f}%"))
    elif curve >= EXIT_RULES["flatten_curve_pct"]:
        gates.append(_gate("curve_exit", "Curve flatten", "pass", f"verified actual {curve:.2f}%", f"verified curve ≥ {EXIT_RULES['flatten_curve_pct']:.0f}%", 100))
    else:
        gates.append(_gate("curve_exit", "Curve flatten", "pending", f"verified actual {curve:.2f}%", f"verified curve ≥ {EXIT_RULES['flatten_curve_pct']:.0f}%", _percent(curve / EXIT_RULES["flatten_curve_pct"] * 100)))

    scaled = bool(position.get("scaled"))
    peak_mcap = _number(copied.get("peak_mcap")) or current
    if scaled:
        gates.append(_gate("failed_impulse", "Failed impulse", "pending", "not applicable after partial scale", "unscaled path: ≥8% spot drop before a >2% peak", 0))
    elif failed_impulse_hit(entry, current, peak_mcap, EXIT_RULES["fail_impulse_drop_pct"], EXIT_RULES["fail_impulse_max_peak_pct"]):
        gates.append(_gate("failed_impulse", "Failed impulse", "pass", f"actual spot {change:.2f}%; peak {100.0 * (peak_mcap / entry - 1.0):.2f}%", "≥8% drop before a >2% peak", 100))
    else:
        peak_change = 100.0 * (peak_mcap / entry - 1.0)
        state = "fail" if peak_change > EXIT_RULES["fail_impulse_max_peak_pct"] else "pending"
        progress = 0.0 if state == "fail" else _percent(max(0.0, -change) / EXIT_RULES["fail_impulse_drop_pct"] * 100)
        gates.append(_gate("failed_impulse", "Failed impulse", state, f"actual spot {change:.2f}%; peak {peak_change:.2f}%", "≥8% drop before a >2% peak", progress))

    if scaled:
        gates.append(_gate("scale", "Net scale-out", "pending", "already scaled; this one-time trigger is consumed", f"unscaled net ≥ {EXIT_RULES['scale_out_pct']:.0f}%", 0))
        giveback = 100.0 * max(0.0, peak_mcap - current) / max(peak_mcap, 0.000001)
        if current < peak_mcap * (1.0 - EXIT_RULES["trail_giveback_pct"] / 100.0):
            gates.append(_gate("trail", "Post-scale trail", "pass", f"actual spot giveback {giveback:.2f}% from peak", f"giveback > {EXIT_RULES['trail_giveback_pct']:.0f}%", 100))
        else:
            gates.append(_gate("trail", "Post-scale trail", "pending", f"actual spot giveback {giveback:.2f}% from peak", f"giveback > {EXIT_RULES['trail_giveback_pct']:.0f}%", _percent(giveback / EXIT_RULES["trail_giveback_pct"] * 100)))
        gates.append(_gate("profit_lock", "Net profit lock", "pending", "not applicable after partial scale", "unscaled path only", 0))
    else:
        if net >= EXIT_RULES["scale_out_pct"]:
            gates.append(_gate("scale", "Net scale-out", "pass", f"actual net {net:.2f}% after modeled entry+exit costs", f"net ≥ {EXIT_RULES['scale_out_pct']:.0f}%", 100))
        else:
            gates.append(_gate("scale", "Net scale-out", "pending", f"actual net {net:.2f}% after modeled entry+exit costs", f"net ≥ {EXIT_RULES['scale_out_pct']:.0f}%", _percent(max(0.0, net) / EXIT_RULES["scale_out_pct"] * 100)))
        gates.append(_gate("trail", "Post-scale trail", "pending", "activates only after partial scale", f"giveback > {EXIT_RULES['trail_giveback_pct']:.0f}% after scale", 0))
        peak_net = net_of_costs_pct(entry, peak_mcap)
        lock_hit = net < EXIT_RULES["scale_out_pct"] and profit_lock_hit(peak_net, net, EXIT_RULES["profit_lock_arm_pct"], EXIT_RULES["profit_lock_giveback_pct"])
        if lock_hit:
            gates.append(_gate("profit_lock", "Net profit lock", "pass", f"actual net {net:.2f}%; peak net {peak_net:.2f}% after modeled costs", f"peak net ≥ {EXIT_RULES['profit_lock_arm_pct']:.0f}% then giveback ≥ {EXIT_RULES['profit_lock_giveback_pct']:.0f}%", 100))
        elif peak_net < EXIT_RULES["profit_lock_arm_pct"]:
            gates.append(_gate("profit_lock", "Net profit lock", "pending", f"actual peak net {peak_net:.2f}% after modeled costs", f"arm at peak net {EXIT_RULES['profit_lock_arm_pct']:.0f}%", _percent(max(0.0, peak_net) / EXIT_RULES["profit_lock_arm_pct"] * 100)))
        else:
            giveback_net = max(0.0, peak_net - net)
            gates.append(_gate("profit_lock", "Net profit lock", "pending", f"actual peak-to-current net giveback {giveback_net:.2f}%", f"giveback ≥ {EXIT_RULES['profit_lock_giveback_pct']:.0f}%", _percent(giveback_net / EXIT_RULES["profit_lock_giveback_pct"] * 100)))

    if not scaled and age >= EXIT_RULES["soft_exit_seconds"] and net < EXIT_RULES["soft_exit_below_pct"]:
        gates.append(_gate("soft_time", "Soft time exit", "pass", f"actual age {age:.0f}s; net {net:.2f}%", f"age ≥ {EXIT_RULES['soft_exit_seconds']:.0f}s and net < {EXIT_RULES['soft_exit_below_pct']:.0f}%", 100))
    elif scaled:
        gates.append(_gate("soft_time", "Soft time exit", "pending", "not applicable after partial scale", "unscaled path only", 0))
    else:
        gates.append(_gate("soft_time", "Soft time exit", "pending", f"actual age {age:.0f}s; net {net:.2f}%", f"age ≥ {EXIT_RULES['soft_exit_seconds']:.0f}s and net < {EXIT_RULES['soft_exit_below_pct']:.0f}%", _percent(age / EXIT_RULES["soft_exit_seconds"] * 100) if net < EXIT_RULES["soft_exit_below_pct"] else 0))

    if age >= EXIT_RULES["hard_exit_seconds"]:
        gates.append(_gate("hard_time", "Hard time exit", "pass", f"actual age {age:.0f}s", f"age ≥ {EXIT_RULES['hard_exit_seconds']:.0f}s", 100))
    else:
        gates.append(_gate("hard_time", "Hard time exit", "pending", f"actual age {age:.0f}s", f"age ≥ {EXIT_RULES['hard_exit_seconds']:.0f}s", _percent(age / EXIT_RULES["hard_exit_seconds"] * 100)))

    if authoritative is None:
        gates.append(_gate("exit_policy", "Authoritative exit policy", "pending", "existing exit_reason has no trigger on copied current peak", "existing exit_reason trigger", 0))
    else:
        reason, fraction = authoritative
        gates.append(_gate("exit_policy", "Authoritative exit policy", "pass", f"{reason}; copied exit fraction {fraction:.2f}", "existing exit_reason trigger", 100))
    return gates, authoritative


def sell_candidate(position: dict[str, Any], observation: dict[str, Any] | None, *, now: float) -> dict[str, Any]:
    observation = observation if isinstance(observation, dict) else {}
    mint = _identifier(position.get("mint"), fallback="unknown-mint")
    symbol = _identifier(position.get("symbol"), fallback="?")
    quote_gate, quote_current, observed_at, _market_cap = _quote_gate(observation, now=now)
    if not quote_current:
        status = "stale" if observed_at is None or not _is_fresh(observed_at, now) else "blocked"
        return {
            "mint": mint,
            "symbol": symbol,
            "side": "sell",
            "readinessPct": None,
            "status": status,
            "observedAt": observed_at,
            "reason": "stale sampled quote; readiness unavailable" if status == "stale" else quote_gate["actual"],
            "gates": [quote_gate, _gate("exit_policy", "Authoritative exit policy", "unknown", "fresh positive quote unavailable", "existing exit_reason must evaluate")],
        }
    gates, authoritative = _exit_trigger_gates(position, observation)
    all_gates = [quote_gate, *gates]
    if authoritative is not None:
        reason, fraction = authoritative
        return {
            "mint": mint,
            "symbol": symbol,
            "side": "sell",
            "readinessPct": 100.0,
            "status": "ready",
            "observedAt": observed_at,
            "reason": _text(f"existing exit policy ready: {reason}; copied fraction {fraction:.2f}. Diagnostic only, not an execution"),
            "gates": all_gates,
        }
    trigger_gates = [gate for gate in gates if gate["key"] != "exit_policy"]
    nearest = max(trigger_gates, key=lambda gate: float(gate["progressPct"]), default=None)
    readiness = _percent(float(nearest["progressPct"]) if nearest is not None else 0.0)
    unknown = next((gate for gate in trigger_gates if gate["state"] == "unknown"), None)
    if unknown is not None and readiness <= 0:
        status = "blocked"
        reason = f"{unknown['label']}: {unknown['actual']}"
    elif readiness > 0:
        status = "approaching"
        reason = f"nearest applicable trigger: {nearest['label']}: {nearest['actual']} (requires {nearest['required']})" if nearest else "no current exit trigger"
    else:
        status = "waiting"
        reason = "no current exit trigger is near on actual sampled data"
    return {
        "mint": mint,
        "symbol": symbol,
        "side": "sell",
        "readinessPct": min(readiness, 99.0),
        "status": status,
        "observedAt": observed_at,
        "reason": _text(reason),
        "gates": all_gates,
    }


def _safe_observation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    mint = value.get("mint")
    if not isinstance(mint, str) or not mint.strip():
        return None
    result: dict[str, Any] = {
        "mint": mint.strip()[:160],
        "symbol": _identifier(value.get("symbol"), fallback="?")[:80],
        "observed_at": _number(value.get("observed_at")),
        "market_cap": _number(value.get("market_cap")),
        "curve_pct": _number(value.get("curve_pct")),
        "reply_count": _number(value.get("reply_count")),
    }
    if value.get("curve_known") is False:
        result["curve_known"] = False
    elif result["curve_pct"] is not None:
        # An actual numeric curve from the existing source is verified unless that
        # source expressly marks its reserve read unavailable.
        result["curve_known"] = True
    else:
        result["curve_known"] = None
    risk = value.get("risk")
    if isinstance(risk, dict) and risk.get("status") == "known":
        result["risk"] = {
            "status": "known",
            "top10_pct": _number(risk.get("top10_pct")),
            "dev_pct": _number(risk.get("dev_pct")),
            "bundle_pct": _number(risk.get("bundle_pct")),
        }
    else:
        result["risk"] = {"status": "unknown"}
    for key in ("virtual_sol", "virtual_token"):
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            result[key] = raw
    sol_usd = _number(value.get("sol_usd"))
    if sol_usd is not None and sol_usd > 0:
        result["sol_usd"] = sol_usd
    return result


def _fallback_observation(track: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(track, dict):
        return None
    return _safe_observation({
        "mint": track.get("mint"),
        "symbol": track.get("symbol"),
        "observed_at": track.get("last_seen"),
        "market_cap": track.get("last_mcap"),
        "curve_pct": track.get("last_curve"),
        "curve_known": track.get("curve_known"),
        "risk": track.get("risk"),
    })


def _position_fallback(position: dict[str, Any]) -> dict[str, Any] | None:
    return _safe_observation({
        "mint": position.get("mint"),
        "symbol": position.get("symbol"),
        "observed_at": position.get("entry_at"),
        "market_cap": position.get("last_mcap", position.get("entry_mcap")),
        "risk": position.get("risk"),
        # A position fallback must not fabricate a current verified curve.
        "curve_known": False,
    })


def _normal_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        at = _number(item.get("at"))
        side = item.get("side")
        action = item.get("action")
        if at is None or side not in {"buy", "sell"} or action not in _ALLOWED_ACTIONS:
            continue
        readiness = _number(item.get("readinessPct"))
        result.append({
            "id": _identifier(item.get("id"), fallback="signal-event"),
            "at": at,
            "mint": _identifier(item.get("mint"), fallback="unknown-mint"),
            "symbol": _identifier(item.get("symbol"), fallback="?"),
            "side": side,
            "action": action,
            "readinessPct": None if readiness is None else _percent(readiness),
            "reason": _text(item.get("reason")),
        })
    return result[-MAX_EVENTS:]


def empty_signal_analysis() -> dict[str, Any]:
    """Return the durable analysis state, including a private bounded quote cache."""
    return {
        "asOf": None,
        "model": MODEL,
        "calibrated": False,
        "disclaimer": DISCLAIMER,
        "candidates": [],
        "events": [],
        "_latest": {},
    }


def signal_events(state: Any) -> list[dict[str, Any]]:
    return _normal_events(state.get("events")) if isinstance(state, dict) else []


def _event_id(*, at: float, mint: str, side: str, action: str, reason: str, sequence: int) -> str:
    digest = hashlib.sha256(f"{at:.6f}|{mint}|{side}|{action}|{reason}|{sequence}".encode("utf-8")).hexdigest()[:14]
    return f"signal-{int(at * 1000)}-{digest}"


def record_signal_event(
    events: list[dict[str, Any]],
    *,
    at: float,
    mint: Any,
    symbol: Any,
    side: str,
    action: str,
    readiness_pct: float | None,
    reason: Any,
) -> list[dict[str, Any]]:
    """Append a real decision record; operational records use ``record_operational``."""
    timestamp = _number(at)
    if timestamp is None or side not in {"buy", "sell"} or action not in _ALLOWED_ACTIONS:
        return _normal_events(events)
    safe_reason = _text(reason)
    normalized = _normal_events(events)
    normalized.append({
        "id": _event_id(at=timestamp, mint=_identifier(mint, fallback="unknown-mint"), side=side, action=action, reason=safe_reason, sequence=len(normalized)),
        "at": timestamp,
        "mint": _identifier(mint, fallback="unknown-mint"),
        "symbol": _identifier(symbol, fallback="?"),
        "side": side,
        "action": action,
        "readinessPct": None if readiness_pct is None else _percent(readiness_pct),
        "reason": safe_reason,
    })
    return normalized[-MAX_EVENTS:]


def _readiness_band(value: float | None) -> str:
    if value is None:
        return "null"
    return str(int(max(0.0, min(99.0, value)) // 10) * 10)


def record_operational_event(events: list[dict[str, Any]], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Record WAIT/BLOCK/READY only when its action, reason, or score band changed."""
    observed_at = _number(candidate.get("observedAt"))
    if observed_at is None:
        return _normal_events(events)
    raw_status = candidate.get("status")
    status = raw_status if isinstance(raw_status, str) else ""
    action = {"ready": "READY", "blocked": "BLOCK"}.get(status, "WAIT")
    readiness = _number(candidate.get("readinessPct"))
    reason = _text(candidate.get("reason"))
    mint = _identifier(candidate.get("mint"), fallback="unknown-mint")
    side = candidate.get("side")
    if side not in {"buy", "sell"}:
        return _normal_events(events)
    normalized = _normal_events(events)
    prior = next((item for item in reversed(normalized) if item["mint"] == mint and item["side"] == side and item["action"] in {"WAIT", "BLOCK", "READY"}), None)
    if prior is not None and prior["action"] == action and prior["reason"] == reason and _readiness_band(_number(prior.get("readinessPct"))) == _readiness_band(readiness):
        return normalized
    return record_signal_event(
        normalized,
        at=observed_at,
        mint=mint,
        symbol=candidate.get("symbol"),
        side=side,
        action=action,
        readiness_pct=readiness,
        reason=reason,
    )


def _latest_from_state(state: Any) -> dict[str, dict[str, Any]]:
    raw = state.get("_latest") if isinstance(state, dict) else None
    result: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for mint, item in raw.items():
            observation = _safe_observation(item)
            if observation is not None:
                result[str(mint)] = observation
    return result


def _trim_latest(
    latest: dict[str, dict[str, Any]],
    *,
    held_mints: set[str],
) -> dict[str, dict[str, Any]]:
    protected = [item for mint, item in latest.items() if mint in held_mints]
    others = [item for mint, item in latest.items() if mint not in held_mints]
    protected.sort(key=lambda item: _number(item.get("observed_at")) or -1.0, reverse=True)
    others.sort(key=lambda item: _number(item.get("observed_at")) or -1.0, reverse=True)
    kept = (protected + others)[:MAX_CANDIDATES]
    return {str(item["mint"]): item for item in kept}


def build_signal_analysis(
    previous: Any,
    *,
    tracked: dict[str, Any],
    portfolio: dict[str, Any],
    observations: Iterable[dict[str, Any]],
    strategy_revision: int,
    now: float,
    operational_mints: Iterable[str] = (),
) -> dict[str, Any]:
    """Merge actual samples into bounded diagnostics without any policy mutation."""
    prior = previous if isinstance(previous, dict) else empty_signal_analysis()
    latest = _latest_from_state(prior)
    tracks: dict[str, Any] = tracked if isinstance(tracked, dict) else {}
    positions_raw = portfolio.get("positions") if isinstance(portfolio, dict) else {}
    positions: dict[str, Any] = positions_raw if isinstance(positions_raw, dict) else {}
    for item in observations:
        clean = _safe_observation(item)
        if clean is not None:
            latest[str(clean["mint"])] = clean
    for mint, track in tracks.items():
        fallback = _fallback_observation(track) if isinstance(track, dict) else None
        if fallback is None:
            continue
        existing = latest.get(str(fallback["mint"]))
        if existing is None or (_number(fallback.get("observed_at")) or -1.0) > (_number(existing.get("observed_at")) or -1.0):
            latest[str(fallback["mint"])] = fallback
    for position in positions.values():
        if not isinstance(position, dict):
            continue
        mint = str(position.get("mint") or "")
        if mint and mint not in latest:
            fallback = _position_fallback(position)
            if fallback is not None:
                latest[mint] = fallback
    held_mints = {str(position.get("mint")) for position in positions.values() if isinstance(position, dict) and position.get("mint")}
    latest = _trim_latest(latest, held_mints=held_mints)

    candidates: list[dict[str, Any]] = []
    for position in positions.values():
        if not isinstance(position, dict):
            continue
        if position.get("strategy") == "pump-capture-v1":
            # Pump positions use their own exit set; the pump radar reports them.
            continue
        mint = str(position.get("mint") or "")
        if not mint:
            continue
        candidates.append(sell_candidate(position, latest.get(mint), now=now))
    buy_observations = [item for mint, item in latest.items() if mint not in held_mints]
    buy_observations.sort(key=lambda item: _number(item.get("observed_at")) or -1.0, reverse=True)
    for observation in buy_observations:
        mint = str(observation["mint"])
        raw_track = tracks.get(mint)
        candidates.append(buy_candidate(observation, raw_track if isinstance(raw_track, dict) else None, portfolio, strategy_revision=strategy_revision, now=now))
        if len(candidates) >= MAX_CANDIDATES:
            break
    candidates = candidates[:MAX_CANDIDATES]

    events = signal_events(prior)
    sampled = {str(mint) for mint in operational_mints}
    for candidate in candidates:
        if candidate["mint"] in sampled and candidate.get("observedAt") is not None:
            events = record_operational_event(events, candidate)
    observed_values = [value for item in latest.values() if (value := _number(item.get("observed_at"))) is not None]
    as_of = max(observed_values) if observed_values else None
    return {
        "asOf": as_of,
        "model": MODEL,
        "calibrated": False,
        "disclaimer": DISCLAIMER,
        "candidates": candidates,
        "events": events[-MAX_EVENTS:],
        "_latest": latest,
    }


def public_signal_analysis(state: Any, *, now: float) -> dict[str, Any]:
    """Return the API contract and invalidate stale candidate percentages at read time."""
    raw = state if isinstance(state, dict) else empty_signal_analysis()
    raw_candidates_value = raw.get("candidates")
    raw_candidates: list[Any] = raw_candidates_value if isinstance(raw_candidates_value, list) else []
    candidates: list[dict[str, Any]] = []
    for item in raw_candidates[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        observed_at = _number(item.get("observedAt"))
        readiness = _number(item.get("readinessPct"))
        raw_gates = item.get("gates")
        gates: list[Any] = raw_gates if isinstance(raw_gates, list) else []
        safe_gates = [
            _gate(gate.get("key"), gate.get("label"), gate.get("state"), gate.get("actual"), gate.get("required"), _number(gate.get("progressPct")) or 0.0)
            for gate in gates if isinstance(gate, dict)
        ]
        stale = not _is_fresh(observed_at, now)
        if stale:
            replaced = False
            for gate in safe_gates:
                if gate["key"] == "quote":
                    gate.update({"state": "unknown", "actual": "sampled quote is stale or unavailable", "required": "fresh actual market cap > $0", "progressPct": 0.0})
                    replaced = True
                    break
            if not replaced:
                safe_gates.insert(0, _gate("quote", "Fresh positive market-cap quote", "unknown", "sampled quote is stale or unavailable", "fresh actual market cap > $0"))
        candidates.append({
            "mint": _identifier(item.get("mint"), fallback="unknown-mint"),
            "symbol": _identifier(item.get("symbol"), fallback="?"),
            "side": item.get("side") if item.get("side") in {"buy", "sell"} else "buy",
            "readinessPct": None if stale else (None if readiness is None else _percent(readiness)),
            "status": "stale" if stale else (item.get("status") if item.get("status") in _ALLOWED_STATUS else "blocked"),
            "observedAt": observed_at,
            "reason": "stale sampled quote; readiness unavailable" if stale else _text(item.get("reason")),
            "gates": safe_gates,
        })
    as_of = _number(raw.get("asOf"))
    return {
        "asOf": as_of,
        "model": MODEL,
        "calibrated": False,
        "disclaimer": DISCLAIMER,
        "candidates": candidates,
        "events": signal_events(raw)[-MAX_EVENTS:],
    }
