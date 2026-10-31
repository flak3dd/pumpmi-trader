"""Runtime coordinator for the durable, always-on, paper-only optimizer."""
from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .policy import (
    BANKROLL,
    FEE_BPS,
    MAX_OPEN,
    SLIPPAGE_BPS,
    apply_observation,
    entry_reason,
    initial_portfolio,
    make_watch_state,
    open_position,
    portfolio_equity,
    record_watch_observation,
    refresh_equity,
    watch_checkpoint_state,
    watch_started_at,
)
from .replay import validate_candidates
from .source import PumpFunSource
from .store import DatabaseStore, RevisionConflict, StoreUnavailable


INTERVAL_SECONDS = 1800
POLL_SECONDS = 25
LEASE_TTL_SECONDS = 75
MAX_TRACKED = 64
MAX_DONE_CARDS = 80
OBSERVATION_READ_LIMIT = 30_000


def _now_iso(timestamp: float | None) -> str | None:
    return None if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _next_boundary(now: float) -> float:
    return (int(now // INTERVAL_SECONDS) + 1) * INTERVAL_SECONDS


def _empty_validation() -> dict[str, Any]:
    return {
        "state": "waiting",
        "reason": "awaiting fresh chronological forward-paper evidence",
        "trainTrades": 0,
        "testTrades": 0,
        "testPF": None,
        "testNet": None,
        "maxDrawdown": None,
    }


def _empty_source() -> dict[str, Any]:
    return {
        "name": PumpFunSource.name,
        "lastObservation": None,
        "error": None,
        "observations": 0,
        "trackedTokens": 0,
        "social": PumpFunSource.social_label,
    }


def _empty_board_stats() -> dict[str, Any]:
    return {"ticks": 0, "scanned": 0, "buys": 0, "wins": 0, "losses": 0, "skips": 0, "realized": 0.0}


def _display_symbol(symbol: Any, mint: Any) -> str:
    """Use the same unique key in cards and charts when symbols collide."""
    raw_symbol = str(symbol or "?")
    raw_mint = str(mint or "")
    return f"{raw_symbol} · {raw_mint}" if raw_mint else raw_symbol


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def disabled_status(document: dict[str, Any] | None = None, *, error: str | None = None) -> dict[str, Any]:
    document = document or {"revision": 0, "watchPeriods": {"short": 90, "medium": 180, "long": 300}}
    source = _empty_source()
    if error:
        source["error"] = error
    return {
        "enabled": False,
        "runtime": "disabled",
        "paperOnly": True,
        "intervalSeconds": INTERVAL_SECONDS,
        "lastTick": None,
        "lastReview": None,
        "nextReview": None,
        "bootId": "disabled",
        "leaseOwned": False,
        "source": source,
        "champion": {"watchPeriods": dict(document["watchPeriods"]), "revision": int(document["revision"])},
        "portfolio": {"equity": BANKROLL, "cash": BANKROLL, "bankroll": BANKROLL, "grossRealized": 0.0, "modeledCosts": 0.0, "netRealized": 0.0, "openPositions": 0, "drawdownPct": 0.0, "maxDrawdownPct": 0.0, "closedTrades": 0},
        "costModel": {"feeBps": FEE_BPS, "slippageBps": SLIPPAGE_BPS, "label": "Model assumption: 100 bps fee + 100 bps slippage per side"},
        "windows": [],
        "validation": _empty_validation(),
        "reviews": [],
    }


class OptimizerRuntime:
    """One durable forward-paper namespace, safe to reconstruct after restart."""

    def __init__(
        self,
        store: DatabaseStore,
        *,
        source: Any | None = None,
        seed_document: dict[str, Any],
        enabled: bool = True,
        boot_id: str | None = None,
    ) -> None:
        self.store = store
        self.source = source or PumpFunSource()
        self.seed_document = deepcopy(seed_document)
        self.enabled = enabled
        self.boot_id = boot_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        self.lease_owned = False
        self.local_error: str | None = None
        self.store.migrate(self.seed_document)
        if self.store.get_state("portfolio") is None:
            self.store.set_many_state({
                "portfolio": initial_portfolio(),
                "tracked": {},
                "source": _empty_source(),
                "board_stats": _empty_board_stats(),
                "validation": _empty_validation(),
                "events": [],
                "done_cards": [],
                "last_review_boundary": None,
                "last_validated_champion": deepcopy(self.store.get_config() or self.seed_document),
                "active_promotion": None,
                "health": {"runtime": "warming_up", "lastTick": None, "lastReview": None, "nextReview": None, "error": None},
            })

    @classmethod
    def from_env(cls, *, source: Any | None = None, enabled: bool | None = None) -> "OptimizerRuntime":
        # Read the existing document only as a seed input. It is never written here.
        from src.strategy_config import load_strategy_config  # type: ignore

        document = load_strategy_config()
        store = DatabaseStore.from_env()
        actual_enabled = os.environ.get("PUMPMI_OPTIMIZER_ENABLED", "").strip() == "1" if enabled is None else enabled
        return cls(store, source=source, seed_document=document, enabled=actual_enabled)

    def config(self) -> dict[str, Any]:
        document = self.store.get_config()
        if document is None:
            raise StoreUnavailable("optimizer strategy configuration is not seeded")
        return document

    def update_config(self, update: dict[str, Any]) -> dict[str, Any]:
        """Durable user update with the same strict schema as the adapter endpoint."""
        from src.strategy_config import StrategyConfigError, validate_watch_periods  # type: ignore

        if not isinstance(update, dict) or set(update) != {"watchPeriods", "revision"}:
            raise StrategyConfigError("strategy update accepts only watchPeriods and revision")
        revision = update["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise StrategyConfigError("revision must be a non-negative integer")
        periods = validate_watch_periods(update["watchPeriods"])
        current = self.config()
        if revision != current["revision"]:
            raise RevisionConflict("strategy revision conflict")
        document = {"version": 1, "revision": int(current["revision"]) + 1, "watchPeriods": periods, "max_open": MAX_OPEN}
        self.store.update_config(document, int(revision), source="user")
        self.store.audit("user_strategy_update", {"previous": current, "next": document})
        return document

    def _health(self) -> dict[str, Any]:
        return self.store.get_state("health", {"runtime": "warming_up", "lastTick": None, "lastReview": None, "nextReview": None, "error": None})

    def _set_health(self, *, runtime: str, now: float, error: str | None = None, last_review: float | None = None) -> None:
        old = self._health()
        self.store.set_state("health", {
            "runtime": runtime,
            "lastTick": now,
            "lastReview": old.get("lastReview") if last_review is None else last_review,
            "nextReview": _next_boundary(now),
            "error": error,
        }, now=now)

    def _renew_lease(self, *, now: float) -> bool:
        """Fence durable writes after potentially slow collection or a worker error."""
        self.lease_owned = self.store.acquire_lease(
            "optimizer-worker", self.boot_id, ttl_seconds=LEASE_TTL_SECONDS, now=now,
        )
        return self.lease_owned

    def _events(self) -> list[str]:
        value = self.store.get_state("events", [])
        return value if isinstance(value, list) else []

    def _event(self, message: str, *, now: float) -> list[str]:
        values = self._events()
        values.append(f"{_now_iso(now)} {message}")
        return values[-60:]

    @staticmethod
    def _watch_card(track: dict[str, Any], *, as_of: float | None = None) -> dict[str, Any]:
        try:
            checkpoints = watch_checkpoint_state(track, as_of=as_of)
            started = watch_started_at(track)
        except (TypeError, ValueError, KeyError):
            # Legacy persisted watches cannot be upgraded into invented samples.
            checkpoints = {}
            started = track.get("watch_started_ts", track.get("started_at"))
        return {
            "symbol": _display_symbol(track.get("symbol"), track.get("mint")), "mint": track.get("mint"), "name": track.get("name", ""),
            "column": "checking", "verdict": "WATCH", "mcap": round(float(track.get("last_mcap") or 0)),
            "curve_pct": track.get("last_curve"), "top10_pct": (track.get("risk") or {}).get("top10_pct"),
            "dev_pct": (track.get("risk") or {}).get("dev_pct"), "bundle_pct": (track.get("risk") or {}).get("bundle_pct"),
            "ts_seen": started, "watch_started_ts": started, "watch_periods": checkpoints,
            "reason": track.get("reason", "watching actual market-cap path"),
        }

    def _trim_tracks(self, tracked: dict[str, dict[str, Any]], portfolio: dict[str, Any]) -> dict[str, dict[str, Any]]:
        if len(tracked) <= MAX_TRACKED:
            return tracked
        protected = {position["mint"] for position in portfolio.get("positions", {}).values()}
        removable = sorted(
            (track for track in tracked.values() if track.get("mint") not in protected and track.get("entry_decided")),
            key=lambda track: float(track.get("last_seen", 0)),
        )
        for track in removable:
            if len(tracked) <= MAX_TRACKED:
                break
            tracked.pop(str(track["mint"]), None)
        return tracked

    def _portfolio_status(self, portfolio: dict[str, Any]) -> dict[str, Any]:
        equity = refresh_equity(portfolio)
        return {
            "equity": round(equity, 2), "cash": round(float(portfolio["cash"]), 2), "bankroll": round(float(portfolio["bankroll"]), 2),
            "grossRealized": round(float(portfolio["gross_realized"]), 2), "modeledCosts": round(float(portfolio["modeled_costs"]), 2),
            "netRealized": round(float(portfolio["net_realized"]), 2), "openPositions": len(portfolio["positions"]),
            "drawdownPct": round(float(portfolio["drawdown_pct"]), 4), "maxDrawdownPct": round(float(portfolio.get("max_drawdown_pct", 0.0)), 4),
            "closedTrades": int(portfolio["closed_trades"]),
        }

    def _windows(self, now: float) -> list[dict[str, Any]]:
        trades = self.store.closed_trades()
        current = int(now // INTERVAL_SECONDS) * INTERVAL_SECONDS
        windows: list[dict[str, Any]] = []
        for bucket in range(current - 5 * INTERVAL_SECONDS, current + INTERVAL_SECONDS, INTERVAL_SECONDS):
            items = [trade for trade in trades if bucket <= float(trade["exit_at"]) < bucket + INTERVAL_SECONDS]
            gross = sum(float(trade["gross"]) for trade in items)
            costs = sum(float(trade["costs"]) for trade in items)
            net = sum(float(trade["net"]) for trade in items)
            windows.append({"start": bucket, "end": bucket + INTERVAL_SECONDS, "gross": round(gross, 2), "costs": round(costs, 2), "net": round(net, 2), "closedTrades": len(items), "hasTrades": bool(items)})
        return windows

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        if not self.enabled:
            return disabled_status(self.config())
        now = time.time() if now is None else now
        source = self.store.get_state("source", _empty_source())
        portfolio = self.store.get_state("portfolio", initial_portfolio())
        validation = self.store.get_state("validation", _empty_validation())
        health = self._health()
        document = self.config()
        runtime = health.get("runtime", "warming_up")
        if self.local_error:
            runtime = "degraded"
            source = dict(source)
            source["error"] = self.local_error
        return {
            "enabled": True,
            "runtime": runtime,
            "paperOnly": True,
            "intervalSeconds": INTERVAL_SECONDS,
            "lastTick": health.get("lastTick"),
            "lastReview": health.get("lastReview"),
            "nextReview": health.get("nextReview") or _next_boundary(now),
            "bootId": self.boot_id,
            "leaseOwned": bool(self.lease_owned),
            "source": source,
            "champion": {"watchPeriods": dict(document["watchPeriods"]), "revision": int(document["revision"])},
            "portfolio": self._portfolio_status(portfolio),
            "hourlyReports": self.store.get_state("hourly_reports", []),
            "costModel": {"feeBps": FEE_BPS, "slippageBps": SLIPPAGE_BPS, "label": "Model assumption: 100 bps fee + 100 bps slippage per side"},
            "windows": self._windows(now),
            "validation": validation,
            "reviews": self.store.reviews(),
        }

    def _record_closed(self, trade: dict[str, Any], done: list[dict[str, Any]]) -> None:
        trade = dict(trade)
        trade["status"] = "closed"
        self.store.upsert_trade(trade)
        initial_notional = _number(trade.get("entry_notional"))
        done.append({
            "symbol": _display_symbol(trade["symbol"], trade["mint"]), "mint": trade["mint"], "name": trade.get("name", ""), "column": "done", "verdict": "EXIT", "reason": trade["reason"],
            "pnl": round(float(trade["net"]), 2),
            "pnl_pct": None if not initial_notional else round(100 * float(trade["net"]) / initial_notional, 1),
            "mcap": round(float(trade["exit_mcap"])), "ts_seen": trade["entry_at"], "exit_at": trade["exit_at"],
            "gross": round(float(trade["gross"]), 2), "costs": round(float(trade["costs"]), 2), "social": "unavailable",
            "watch_started_ts": trade.get("watch_started_ts"), "watch_periods": deepcopy(trade.get("watch_periods") or {}),
        })

    def _run_rollback_if_needed(self, *, now: float, portfolio: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Roll back only a promoted strategy after durable forward DD exceeds 10%."""
        active = self.store.get_state("active_promotion")
        observed_drawdown = max(
            float(portfolio.get("max_drawdown_pct", 0.0)),
            float(portfolio.get("drawdown_pct", 0.0)),
        )
        if not active or observed_drawdown <= 10.0:
            return portfolio, None
        current = self.config()
        previous = active.get("previous")
        if not isinstance(previous, dict) or int(previous.get("revision", -1)) >= int(current["revision"]):
            return portfolio, None
        rollback = {"version": 1, "revision": int(current["revision"]) + 1, "watchPeriods": previous["watchPeriods"], "max_open": MAX_OPEN}
        try:
            self.store.promote_config_with_audit(rollback, int(current["revision"]), {"action": "rollback", "previous": current, "restored": rollback, "reason": "forward paper drawdown exceeded 10%"}, now=now)
        except RevisionConflict:
            return portfolio, None
        drawdown = observed_drawdown
        validation = {**_empty_validation(), "state": "rolled_back", "reason": "forward paper drawdown exceeded 10%; restored last validated champion", "maxDrawdown": round(drawdown, 8)}
        self.store.set_many_state({"validation": validation, "active_promotion": None}, now=now)
        return portfolio, validation

    def tick(self, *, now: float | None = None) -> bool:
        """Run one bounded cycle, renewing the lease before any post-collect commit.

        Direct tests can supply deterministic ``now``. Production calls leave it
        unset so the post-collection fence uses a fresh wall-clock timestamp.
        """
        if not self.enabled or not self.lease_owned:
            return False
        explicit_now = now is not None
        now = time.time() if now is None else now
        tracked = self.store.get_state("tracked", {})
        portfolio = self.store.get_state("portfolio", initial_portfolio())
        source_status = self.store.get_state("source", _empty_source())
        config = self.config()  # User saves are read every cycle; existing watches stay immutable.
        batch = self.source.collect(list(tracked)[:MAX_TRACKED], now=now)
        fence_now = now if explicit_now else time.time()
        if not self._renew_lease(now=fence_now):
            # A newer worker owns the lease. This stale collector must write nothing.
            return False
        if batch.error:
            source_status.update({"name": batch.source_name, "error": batch.error, "social": batch.social, "trackedTokens": len(tracked)})
            self.store.set_many_state({"source": source_status, "portfolio": portfolio, "events": self._event(f"source failure: {batch.error}", now=now)}, now=now)
            self._set_health(runtime="degraded", now=now, error=batch.error)
            return True

        self.local_error = None
        self.store.record_observations(batch.observations)
        done = self.store.get_state("done_cards", [])
        events = self._events()
        stats = self.store.get_state("board_stats", _empty_board_stats())
        if not isinstance(stats, dict):
            stats = _empty_board_stats()
        for key, value in _empty_board_stats().items():
            stats.setdefault(key, value)
        stats["ticks"] = int(stats["ticks"]) + 1
        stats["scanned"] = int(stats["scanned"]) + len(batch.observations)
        observed_by_mint = {str(item["mint"]): item for item in batch.observations}
        # Existing open positions consume only their own actual mcap observations.
        for position_id, position in list(portfolio["positions"].items()):
            observation = observed_by_mint.get(str(position["mint"]))
            if not observation:
                continue
            event = apply_observation(portfolio, position_id, observation)
            if event and event["status"] == "closed":
                self._record_closed(event, done)
                stats["realized"] = round(float(stats["realized"]) + float(event["net"]), 8)
                if float(event["net"]) > 0:
                    stats["wins"] = int(stats["wins"]) + 1
                elif float(event["net"]) < 0:
                    stats["losses"] = int(stats["losses"]) + 1
                events.append(f"{_now_iso(now)} EXIT {event['symbol']} {event['reason']} net ${event['net']:+.2f}")
            elif event and event["status"] == "scaled":
                events.append(f"{_now_iso(now)} SCALE {position['symbol']} {event['reason']}")

        for observation in batch.observations:
            mint = str(observation["mint"])
            track = tracked.get(mint)
            if track is None:
                if len(tracked) >= MAX_TRACKED:
                    self._trim_tracks(tracked, portfolio)
                if len(tracked) >= MAX_TRACKED:
                    continue
                track = {
                    "mint": mint, "symbol": observation["symbol"], "name": observation.get("name", ""),
                    **make_watch_state(config["watchPeriods"], started_at=float(observation["observed_at"]), start_curve=float(observation["curve_pct"])),
                    "entry_decided": False, "reason": "watching actual market-cap path", "last_seen": float(observation["observed_at"]),
                }
                tracked[mint] = track
            track.update({
                "symbol": observation["symbol"], "name": observation.get("name", ""), "last_seen": float(observation["observed_at"]),
                "last_mcap": observation["market_cap"], "last_curve": observation["curve_pct"], "risk": observation.get("risk"),
            })
            try:
                record_watch_observation(track, observation)
                checkpoints = watch_checkpoint_state(track, as_of=float(observation["observed_at"]))
            except (TypeError, ValueError, KeyError):
                track["entry_decided"] = True
                track["reason"] = "SKIP checkpoint state unavailable"
                done.append({**self._watch_card(track, as_of=now), "column": "done", "verdict": "SKIP", "reason": track["reason"]})
                stats["skips"] = int(stats["skips"]) + 1
                continue
            if track.get("entry_decided"):
                continue
            if checkpoints["long"]["status"] != "observed":
                continue
            track["entry_decided"] = True
            reason = entry_reason(observation, track)
            if reason is None and len(portfolio["positions"]) >= MAX_OPEN:
                reason = "max 6 open positions"
            if reason is not None:
                track["reason"] = f"SKIP {reason}"
                done.append({**self._watch_card(track, as_of=now), "column": "done", "verdict": "SKIP", "reason": track["reason"]})
                stats["skips"] = int(stats["skips"]) + 1
                events.append(f"{_now_iso(now)} SKIP {track['symbol']} {reason}")
                continue
            position = open_position(portfolio, observation, strategy_revision=int(config["revision"]), watch=track)
            if position is None:
                track["reason"] = "SKIP insufficient paper cash"
                done.append({**self._watch_card(track, as_of=now), "column": "done", "verdict": "SKIP", "reason": track["reason"]})
                stats["skips"] = int(stats["skips"]) + 1
                events.append(f"{_now_iso(now)} SKIP {track['symbol']} insufficient paper cash")
            else:
                track["reason"] = "entered after three actual immutable watch checkpoints; social unavailable and unused"
                stats["buys"] = int(stats["buys"]) + 1
                events.append(f"{_now_iso(now)} BUY {track['symbol']} ${position['initial_notional']:.2f} actual mcap ${position['entry_mcap']:,.0f}")

        self._trim_tracks(tracked, portfolio)
        done = done[-MAX_DONE_CARDS:]
        source_status.update({
            "name": batch.source_name, "lastObservation": now if batch.observations else source_status.get("lastObservation"), "error": None,
            "observations": int(source_status.get("observations", 0)) + len(batch.observations), "trackedTokens": len(tracked), "social": batch.social,
        })
        events = events[-60:]
        self.store.set_many_state({"tracked": tracked, "portfolio": portfolio, "source": source_status, "done_cards": done, "events": events, "board_stats": stats}, now=now)
        portfolio, _ = self._run_rollback_if_needed(now=now, portfolio=portfolio)
        self._set_health(runtime="running", now=now)
        self.maybe_review(now=now)
        return True

    def maybe_review(self, *, now: float) -> None:
        boundary = int(now // INTERVAL_SECONDS) * INTERVAL_SECONDS
        previous = self.store.get_state("last_review_boundary")
        if previous is not None and float(previous) >= boundary:
            return
        source = self.store.get_state("source", _empty_source())
        tracked = self.store.get_state("tracked", {})
        fresh = source.get("lastObservation") is not None and now - float(source["lastObservation"]) <= max(120.0, 4 * POLL_SECONDS)
        if not fresh:
            validation = {**_empty_validation(), "reason": "source is not fresh; review held"}
        elif len(tracked) < 30:
            validation = {**_empty_validation(), "reason": "coverage below 30 tracked forward market-cap paths"}
        else:
            # Bound memory/read cost without deleting optimizer-owned historical data.
            observations = self.store.observations(limit=OBSERVATION_READ_LIMIT)
            validation = validate_candidates(observations, self.config()["watchPeriods"])
        document = self.config()
        decision = validation["state"]
        if validation["state"] == "promoted":
            promoted = {"version": 1, "revision": int(document["revision"]) + 1, "watchPeriods": validation["candidate"], "max_open": MAX_OPEN}
            try:
                self.store.promote_config_with_audit(promoted, int(document["revision"]), {"action": "promotion", "previous": document, "next": promoted, "validation": validation}, now=now)
                self.store.set_many_state({"last_validated_champion": document, "active_promotion": {"previous": document, "promoted": promoted, "at": now}}, now=now)
                validation = dict(validation)
                validation["reason"] = "heldout criteria met; promoted atomically"
                decision = "promoted"
            except RevisionConflict:
                validation = {**validation, "state": "waiting", "reason": "user configuration changed during review; no promotion"}
                decision = "waiting"
        review = {"id": f"review-{boundary}", "at": now, "boundary": boundary, "decision": decision, "reason": validation["reason"], "metrics": validation}
        self.store.record_review(review)
        self.store.set_many_state({"validation": validation, "last_review_boundary": boundary, "events": self._event(f"review {decision}: {validation['reason']}", now=now)}, now=now)
        self._set_health(runtime="running", now=now, last_review=now)

    @staticmethod
    def _buy_gates(trade: dict[str, Any]) -> list[dict[str, Any]]:
        risk_raw = trade.get("risk")
        risk = risk_raw if isinstance(risk_raw, dict) else {}
        risk_ok = risk.get("status") == "known"
        risk_detail = "actual sampled holder risk unavailable" if not risk_ok else (
            f"actual sampled top10 {risk.get('top10_pct')}%, dev {risk.get('dev_pct')}%, bundle {risk.get('bundle_pct')}%"
        )
        checkpoints_raw = trade.get("watch_periods")
        checkpoints = checkpoints_raw if isinstance(checkpoints_raw, dict) else {}
        checkpoint_ok = all(isinstance(checkpoints.get(name), dict) and checkpoints[name].get("status") == "observed" for name in ("short", "medium", "long"))
        checkpoint_detail = "actual checkpoint samples unavailable"
        if checkpoint_ok:
            checkpoint_detail = "; ".join(
                f"{name} Δ{float(checkpoints[name].get('curve_delta', 0.0)):+.2f} at {_now_iso(_number(checkpoints[name].get('sampled_at')))}"
                for name in ("short", "medium", "long")
            )
        replies = _number(trade.get("entry_reply_count"))
        long_delta = _number((checkpoints.get("long") or {}).get("curve_delta"))
        flow_ok = (replies is not None and replies >= 2) or (long_delta is not None and long_delta >= 1)
        flow_detail = f"actual entry replies {replies if replies is not None else 'unavailable'}; long Δ {long_delta if long_delta is not None else 'unavailable'}"
        return [
            {"name": "Risk check", "detail": risk_detail, "pass": risk_ok},
            {"name": "Checkpoint check 3/3", "detail": checkpoint_detail, "pass": checkpoint_ok},
            {"name": "Flow check", "detail": flow_detail, "pass": flow_ok},
        ]

    def snapshot_payload(self, *, now: float | None = None) -> dict[str, Any]:
        """Build a frontend-compatible board from durable forward data only."""
        now = time.time() if now is None else now
        source = self.store.get_state("source", _empty_source())
        tracked = self.store.get_state("tracked", {})
        portfolio = self.store.get_state("portfolio", initial_portfolio())
        done = self.store.get_state("done_cards", [])
        as_of = _number(source.get("lastObservation"))
        holdings = []
        for position in portfolio.get("positions", {}).values():
            mcap = float(position.get("last_mcap", position["entry_mcap"]))
            change = 100.0 * (mcap / float(position["entry_mcap"]) - 1.0)
            holdings.append({
                "symbol": _display_symbol(position["symbol"], position["mint"]), "mint": position["mint"], "name": position.get("name", ""), "column": "holding", "verdict": "BUY", "mcap": round(mcap),
                "pnl": round(float(position["remaining_basis"]) * change / 100.0, 2), "pnl_pct": round(change, 2),
                "reason": "forward paper position; modeled execution costs shown in optimizer status", "ts_seen": position["entry_at"],
                "watch_started_ts": position.get("watch_started_ts"), "watch_periods": deepcopy(position.get("watch_periods") or {}), "social": "unavailable",
            })
        checking = [self._watch_card(track, as_of=as_of) for track in tracked.values() if not track.get("entry_decided")]
        checking.sort(key=lambda card: float(card.get("ts_seen") or 0), reverse=True)
        equity = refresh_equity(portfolio)
        stats = self.store.get_state("board_stats", _empty_board_stats())
        if not isinstance(stats, dict):
            stats = _empty_board_stats()
        board_stats = {key: stats.get(key, default) for key, default in _empty_board_stats().items()}
        board_stats["realized"] = round(float(board_stats["realized"]), 2)
        # Additional factual counters retain compatibility with existing consumers;
        # the required tick/scanned/buy/win/loss/skip/realized fields remain present.
        board_stats["observations"] = int(source.get("observations", 0))
        board_stats["tracked"] = len(tracked)
        board_stats["closedTrades"] = int(portfolio.get("closed_trades", 0))
        board = {
            "ts": source.get("lastObservation"), "mode": "paper", "bankroll": BANKROLL, "equity": round(equity, 2),
            "multiple": round(equity / BANKROLL, 3), "columns": {"checking": checking[:MAX_TRACKED], "holding": holdings, "done": done[-MAX_DONE_CARDS:]},
            "source": "LIVE frontend-api-v3 · FORWARD PAPER ONLY", "social": PumpFunSource.social_label,
            "stats": board_stats,
            "log": self._events(), "updated": _now_iso(_number(source.get("lastObservation"))),
        }
        paths = self.store.observations(limit=OBSERVATION_READ_LIMIT)
        by_mint: dict[str, list[dict[str, Any]]] = {}
        for observation in paths:
            by_mint.setdefault(str(observation["mint"]), []).append(observation)
        charts: dict[str, Any] = {}
        for trade in self.store.closed_trades()[-24:]:
            entry_notional = _number(trade.get("entry_notional"))
            exit_proceeds = _number(trade.get("exit_proceeds"))
            if entry_notional is None or entry_notional <= 0 or exit_proceeds is None:
                # Do not invent an execution amount for a legacy/incomplete trade.
                continue
            points = [point for point in by_mint.get(str(trade["mint"]), []) if float(trade["entry_at"]) <= float(point["observed_at"]) <= float(trade["exit_at"])]
            if not points:
                continue
            def closest_index(timestamp: float) -> int:
                return min(range(len(points)), key=lambda index: abs(float(points[index]["observed_at"]) - timestamp))
            entry_index = closest_index(float(trade["entry_at"]))
            exit_index = closest_index(float(trade["exit_at"]))
            key = _display_symbol(trade["symbol"], trade["mint"])
            charts[key] = {
                "metric": "market_cap",
                "series": [{"label": _now_iso(float(point["observed_at"])), "value": float(point["market_cap"])} for point in points],
                "signals": [
                    {"id": "buy", "kind": "buy", "at": entry_index, "label": "Actual sampled paper entry", "t": _now_iso(float(trade["entry_at"])), "mcap": float(trade["entry_mcap"]), "usd": entry_notional, "pnl": None, "reason": "actual observed market-cap entry", "rule": None},
                    {"id": "sell", "kind": "sell", "at": exit_index, "label": "Actual sampled paper exit", "t": _now_iso(float(trade["exit_at"])), "mcap": float(trade["exit_mcap"]), "usd": exit_proceeds, "pnl": float(trade["net"]), "reason": trade["reason"], "rule": "paper exit"},
                ],
                "gatesBuy": self._buy_gates(trade),
                "gatesSell": [{"name": "Paper exit", "detail": f"actual sampled exit: {trade['reason']}", "pass": True}],
                "entry": float(trade["entry_mcap"]), "exitPnl": round(float(trade["net"]), 2),
                "exitPp": round(100.0 * float(trade["net"]) / entry_notional, 2),
                "dur": round((float(trade["exit_at"]) - float(trade["entry_at"])) / 60.0, 2), "_exitTs": float(trade["exit_at"]),
            }
        observed = _number(source.get("lastObservation"))
        verified = observed is not None and not source.get("error") and 0 <= now - observed <= 180
        kind = "live" if verified else "awaiting" if observed is None else "stale"
        label = "Verified live-market paper board" if verified else "Awaiting first verified live-market observation" if observed is None else "Live-market collection interrupted or stale"
        board["source"] = label
        return {"board": board, "trades": charts, "meta": {"kind": kind, "label": label, "updated": board["updated"], "simulationAvailable": True}}


class Worker(threading.Thread):
    """One bounded polling worker; a DB lease prevents overlap during deployments."""

    def __init__(self, runtime: OptimizerRuntime, *, poll_seconds: float = POLL_SECONDS, lease_ttl_seconds: float = LEASE_TTL_SECONDS) -> None:
        super().__init__(name="pumpmi-paper-optimizer", daemon=True)
        self.runtime = runtime
        self.poll_seconds = poll_seconds
        self.lease_ttl_seconds = lease_ttl_seconds
        self.stop_event = threading.Event()
        self.backoff_seconds = poll_seconds

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            now = time.time()
            try:
                owned = self.runtime.store.acquire_lease("optimizer-worker", self.runtime.boot_id, ttl_seconds=self.lease_ttl_seconds, now=now)
                self.runtime.lease_owned = owned
                if owned:
                    # tick() renews again after collection, fencing stale collectors.
                    self.runtime.tick()
                    if self.runtime._renew_lease(now=time.time()):
                        from .hourly import hourly_monitor_tick
                        hourly_monitor_tick(self.runtime.store, now=time.time())
                    self.runtime.local_error = None
                # A standby process must not write shared health: an active process
                # could otherwise be overwritten as "standby" during a deployment.
                self.backoff_seconds = self.poll_seconds
            except Exception as exc:  # Defensive worker boundary; HTTP remains responsive.
                self.runtime.local_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                try:
                    # Only the current lease holder may mutate shared global health.
                    if self.runtime._renew_lease(now=time.time()):
                        self.runtime._set_health(runtime="degraded", now=time.time(), error=self.runtime.local_error)
                except Exception:
                    pass
                self.backoff_seconds = min(max(self.poll_seconds, self.backoff_seconds * 2), 300.0)
            self.stop_event.wait(self.backoff_seconds)
        if self.runtime.lease_owned:
            try:
                self.runtime.store.release_lease("optimizer-worker", self.runtime.boot_id)
            except Exception:
                pass
            self.runtime.lease_owned = False
