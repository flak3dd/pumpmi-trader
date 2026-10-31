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
from .pump import (
    DEFAULT_VARIANT,
    ENTRY_SIGNALS,
    EXIT_SETS,
    PUMP_MODES,
    PUMP_SECONDS,
    PUMP_STRATEGY,
    PumpMarket,
    empty_pump_state,
    public_pump_state,
    run_cycle as run_pump_cycle,
)
from .replay import validate_candidates
from .signals import (
    build_signal_analysis,
    empty_signal_analysis,
    public_signal_analysis,
    record_signal_event,
    signal_events,
)
from .source import PumpFunSource
from .store import DatabaseStore, LeaseLost, RevisionConflict, StoreUnavailable


INTERVAL_SECONDS = 1800
POLL_SECONDS = 25
LEASE_TTL_SECONDS = 75
MAX_TRACKED = 64
MAX_DONE_CARDS = 80
OBSERVATION_READ_LIMIT = 30_000
# A watch needs actual samples, not elapsed time. This finite grace leaves room
# for an ordinary delayed poll without allowing a missing path to occupy a slot
# indefinitely.
WATCH_EXPIRY_GRACE_SECONDS = 120


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
    return {
        "ticks": 0, "scanned": 0, "buys": 0, "wins": 0, "losses": 0,
        "skips": 0, "expiredWatches": 0, "realized": 0.0,
    }


def _empty_entry_readiness() -> dict[str, int | str | None]:
    """Keep the frontend's optional readiness object deliberately exact."""
    return {
        "pending": 0,
        "holderUnknown": 0,
        "curveUnknown": 0,
        "overdue": 0,
        "expiredTotal": 0,
        "blocker": None,
    }


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
        "entryReadiness": _empty_entry_readiness(),
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
        pump_mode: str = "off",
        pump_variant: dict[str, str] | None = None,
        pump_market: Any | None = None,
    ) -> None:
        self.store = store
        self.pump_mode = pump_mode if pump_mode in PUMP_MODES else "off"
        variant = dict(pump_variant or DEFAULT_VARIANT)
        if variant.get("entry") not in ENTRY_SIGNALS or variant.get("exit") not in EXIT_SETS:
            variant = dict(DEFAULT_VARIANT)
        self.pump_variant = variant
        self.pump_market = pump_market
        self.source = source or PumpFunSource()
        self.seed_document = deepcopy(seed_document)
        self.enabled = enabled
        self.boot_id = boot_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        self.lease_owned = False
        self.local_error: str | None = None
        self.store.migrate(self.seed_document)
        # Every row is insert-if-missing in one transaction. Competing deployment
        # starts cannot turn a momentary missing read into a reset of portfolio,
        # user state, or promotion markers owned by the other process.
        champion = deepcopy(self.store.get_config() or self.seed_document)
        self.store.initialize_state_if_missing({
            "portfolio": initial_portfolio(),
            "tracked": {},
            "source": _empty_source(),
            "board_stats": _empty_board_stats(),
            "validation": _empty_validation(),
            "events": [],
            "signal_analysis": empty_signal_analysis(),
            "pump_state": empty_pump_state(self.pump_mode, self.pump_variant),
            "done_cards": [],
            "last_review_boundary": None,
            "last_validated_champion": champion,
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
        pump_mode = os.environ.get("PUMPMI_PUMP_MODE", "off").strip().lower()
        entry, _, exit_set = os.environ.get("PUMPMI_PUMP_VARIANT", "").strip().upper().partition("-")
        variant = {"entry": entry, "exit": exit_set} if entry and exit_set else None
        return cls(store, source=source, seed_document=document, enabled=actual_enabled, pump_mode=pump_mode, pump_variant=variant)

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
        self.store.update_user_config_with_audit(
            document,
            int(revision),
            {"action": "user_strategy_update", "previous": current, "next": document},
        )
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
        """Retain continuous authority for this batch; do not reacquire after loss."""
        self.lease_owned = self.store.renew_lease(
            "optimizer-worker", self.boot_id, ttl_seconds=LEASE_TTL_SECONDS, now=now,
        )
        return self.lease_owned

    @staticmethod
    def _assert_portfolio_capacity(portfolio: dict[str, Any]) -> None:
        """Refuse corrupt over-cap state; never reset it or silently liquidate it."""
        positions = portfolio.get("positions")
        if not isinstance(positions, dict):
            raise RuntimeError("portfolio positions state is invalid")
        if len(positions) > MAX_OPEN:
            raise RuntimeError("portfolio exceeds fixed maximum of 6 open positions; refusing mutation")

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
        curve_known = track.get("curve_known") is True
        return {
            "symbol": _display_symbol(track.get("symbol"), track.get("mint")), "mint": track.get("mint"), "name": track.get("name", ""),
            "column": "checking", "verdict": "WATCH", "mcap": round(float(track.get("last_mcap") or 0)),
            # Never display the numeric 0 reserve placeholder as a current curve
            # reading. The last known anchor remains persisted for checkpoints.
            "curve_pct": track.get("last_curve") if curve_known else None,
            "curve_known": curve_known,
            "top10_pct": (track.get("risk") or {}).get("top10_pct"),
            "dev_pct": (track.get("risk") or {}).get("dev_pct"), "bundle_pct": (track.get("risk") or {}).get("bundle_pct"),
            "ts_seen": started, "watch_started_ts": started, "watch_periods": checkpoints,
            "reason": track.get("reason", "watching actual market-cap path"),
        }

    def _trim_tracks(
        self,
        tracked: dict[str, dict[str, Any]],
        portfolio: dict[str, Any],
        *,
        reserve_slots: int = 0,
    ) -> dict[str, dict[str, Any]]:
        """Retain bounded watches while protecting held and undecided paths.

        Normal end-of-cycle trimming leaves up to ``MAX_TRACKED`` entries. During
        admission a caller can reserve one slot, safely retiring the oldest
        entry-decided non-position watch at an exactly full 64-track set.
        """
        target = max(0, MAX_TRACKED - max(0, int(reserve_slots)))
        if len(tracked) <= target:
            return tracked
        protected = {position["mint"] for position in portfolio.get("positions", {}).values()}
        removable = sorted(
            (track for track in tracked.values() if track.get("mint") not in protected and track.get("entry_decided")),
            key=lambda track: float(track.get("last_seen", 0)),
        )
        for track in removable:
            if len(tracked) <= target:
                break
            tracked.pop(str(track["mint"]), None)
        return tracked

    @staticmethod
    def _held_mints(portfolio: dict[str, Any]) -> set[str]:
        positions = portfolio.get("positions", {})
        if not isinstance(positions, dict):
            return set()
        return {
            str(position.get("mint"))
            for position in positions.values()
            if isinstance(position, dict) and position.get("mint") is not None
        }

    @staticmethod
    def _watch_expiry_deadline(track: dict[str, Any]) -> float | None:
        """Return the persisted long-window deadline without creating a sample."""
        try:
            started = watch_started_at(track)
            checkpoints = watch_checkpoint_state(track, as_of=started)
            long_duration = _number((checkpoints.get("long") or {}).get("duration_s"))
        except (TypeError, ValueError, KeyError):
            return None
        if long_duration is None or long_duration <= 0:
            return None
        return started + long_duration + WATCH_EXPIRY_GRACE_SECONDS

    def _expire_pending_watches(
        self,
        tracked: dict[str, dict[str, Any]],
        portfolio: dict[str, Any],
        done: list[dict[str, Any]],
        stats: dict[str, Any],
        events: list[str],
        signal_events_out: list[dict[str, Any]],
        *,
        now: float,
    ) -> None:
        """Mark stale undecided paths once, inside the caller's lease fence.

        This intentionally operates from durable captured watch durations and the
        wall clock only. It never synthesizes an observation, checkpoint, trade,
        or P&L value, and held paths remain immune even if their tracking card is
        malformed or unexpectedly undecided.
        """
        held = self._held_mints(portfolio)
        for key, track in list(tracked.items()):
            if not isinstance(track, dict) or track.get("entry_decided"):
                continue
            mint = str(track.get("mint") or key)
            if mint in held:
                continue
            deadline = self._watch_expiry_deadline(track)
            if deadline is None or now < deadline:
                continue
            track["entry_decided"] = True
            track["expired_at"] = now
            track["reason"] = (
                "SKIP watch expired: no actual long checkpoint before captured "
                f"long duration + {WATCH_EXPIRY_GRACE_SECONDS}s grace"
            )
            done.append({
                **self._watch_card(track, as_of=now), "column": "done",
                "verdict": "SKIP", "reason": track["reason"],
            })
            stats["skips"] = int(stats.get("skips", 0)) + 1
            stats["expiredWatches"] = int(stats.get("expiredWatches", 0)) + 1
            events.append(f"{_now_iso(now)} SKIP {track.get('symbol', mint)} watch expired")
            signal_events_out[:] = record_signal_event(
                signal_events_out,
                at=now,
                mint=mint,
                symbol=track.get("symbol"),
                side="buy",
                action="EXPIRE",
                readiness_pct=None,
                reason=track["reason"],
            )

    def _entry_readiness(
        self,
        tracked: dict[str, Any],
        portfolio: dict[str, Any],
        stats: dict[str, Any],
        *,
        now: float,
    ) -> dict[str, int | str | None]:
        """Describe factual entry blockers without implying entry eligibility."""
        readiness = _empty_entry_readiness()
        held = self._held_mints(portfolio)
        for key, raw_track in tracked.items():
            if not isinstance(raw_track, dict) or raw_track.get("entry_decided"):
                continue
            mint = str(raw_track.get("mint") or key)
            if mint in held:
                continue
            readiness["pending"] = int(readiness["pending"] or 0) + 1
            risk = raw_track.get("risk")
            if not isinstance(risk, dict) or risk.get("status") != "known":
                readiness["holderUnknown"] = int(readiness["holderUnknown"] or 0) + 1
            if raw_track.get("curve_known") is not True:
                readiness["curveUnknown"] = int(readiness["curveUnknown"] or 0) + 1
            deadline = self._watch_expiry_deadline(raw_track)
            if deadline is not None and now >= deadline:
                readiness["overdue"] = int(readiness["overdue"] or 0) + 1
        expired_total = _number(stats.get("expiredWatches")) if isinstance(stats, dict) else None
        readiness["expiredTotal"] = max(0, int(expired_total or 0))
        if readiness["holderUnknown"]:
            readiness["blocker"] = "holder risk data blocked"
        elif readiness["curveUnknown"]:
            readiness["blocker"] = "reserve data blocked"
        elif readiness["pending"]:
            readiness["blocker"] = "watch awaiting checkpoints"
        return readiness

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
        use_wall_clock = now is None
        now = time.time() if now is None else now
        source = self.store.get_state("source", _empty_source())
        # Entry readiness is factual, durable state rather than a five-second
        # telemetry-cache estimate. It is not an assertion that entry is allowed.
        portfolio = self.store.get_state("portfolio", initial_portfolio(), fresh=True)
        tracked = self.store.get_state("tracked", {}, fresh=True)
        stats = self.store.get_state("board_stats", _empty_board_stats(), fresh=True)
        validation = self.store.get_state("validation", _empty_validation())
        signal_state = self.store.get_state("signal_analysis", empty_signal_analysis())
        health = self._health()
        document = self.config()
        runtime = health.get("runtime", "warming_up")
        if self.local_error:
            runtime = "degraded"
            source = dict(source)
            source["error"] = self.local_error
        hourly_reports = self.store.get_state("hourly_reports", [])
        windows = self._windows(now)
        reviews = self.store.reviews()
        if use_wall_clock:
            # Reads can wait for a worker commit with a quote newer than request
            # start. Score freshness at response time, not before those reads.
            now = time.time()
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
            "hourlyReports": hourly_reports,
            "costModel": {"feeBps": FEE_BPS, "slippageBps": SLIPPAGE_BPS, "label": "Model assumption: 100 bps fee + 100 bps slippage per side"},
            "windows": windows,
            "validation": validation,
            "reviews": reviews,
            "entryReadiness": self._entry_readiness(tracked, portfolio, stats, now=now),
            "signalAnalysis": public_signal_analysis(signal_state, now=now),
            "pumpMode": public_pump_state(self.store.get_state("pump_state", empty_pump_state()), now=now),
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
        """Roll back only the still-active promoted strategy after DD exceeds 10%.

        This method is called inside ``lease_fence``. The config CAS, audit,
        rollback marker clear, validation result, and champion state therefore
        either all commit with the portfolio cycle or all roll back together.
        """
        active = self.store.get_state("active_promotion", fresh=True)
        observed_drawdown = max(
            float(portfolio.get("max_drawdown_pct", 0.0)),
            float(portfolio.get("drawdown_pct", 0.0)),
        )
        if not active or observed_drawdown <= 10.0:
            return portfolio, None
        current = self.config()
        previous = active.get("previous")
        promoted = active.get("promoted")
        if (
            not isinstance(previous, dict)
            or not isinstance(promoted, dict)
            or int(previous.get("revision", -1)) >= int(current["revision"])
            or int(promoted.get("revision", -1)) != int(current["revision"])
        ):
            return portfolio, None
        rollback = {"version": 1, "revision": int(current["revision"]) + 1, "watchPeriods": previous["watchPeriods"], "max_open": MAX_OPEN}
        drawdown = observed_drawdown
        validation = {**_empty_validation(), "state": "rolled_back", "reason": "forward paper drawdown exceeded 10%; restored last validated champion", "maxDrawdown": round(drawdown, 8)}
        try:
            self.store.update_config_with_audit_and_state(
                rollback,
                int(current["revision"]),
                {"action": "rollback", "previous": current, "restored": rollback, "reason": "forward paper drawdown exceeded 10%"},
                state_values={
                    "validation": validation,
                    "active_promotion": None,
                    "last_validated_champion": rollback,
                },
                now=now,
            )
        except RevisionConflict:
            return portfolio, None
        return portfolio, validation

    def tick(self, *, now: float | None = None) -> bool:
        """Run one bounded cycle, renewing the lease before any post-collect commit.

        Direct tests can supply deterministic ``now``. Production calls leave it
        unset so collection receives no fixed clock and every decision inside the
        post-collection fence uses a fresh wall-clock timestamp.
        """
        if not self.enabled or not self.lease_owned:
            return False
        explicit_now = now is not None
        # Collection is intentionally outside a database transaction. This first
        # snapshot only tells the source which mints to ask for; all trading state
        # is reloaded authoritatively after the post-collection lease fence.
        collect_tracked = self.store.get_state("tracked", {})
        # ``None`` explicitly means that a production source must sample actual
        # receipt times itself, rather than inheriting a pre-collection tick time.
        batch = self.source.collect(list(collect_tracked)[:MAX_TRACKED], now=now if explicit_now else None)
        renew_now = float(now) if explicit_now else time.time()
        if not self._renew_lease(now=renew_now):
            # A newer worker owns the lease. This stale collector must write nothing.
            return False
        fence_now = float(now) if explicit_now else None
        try:
            with self.store.lease_fence("optimizer-worker", self.boot_id, now=fence_now):
                # The fence owns the authoritative transaction and has already
                # checked the live lease clock. Capture this only after collection
                # and after entering the fence: a row cannot fill checkpoints or
                # enter after its durable watch deadline passed while collecting.
                mutation_now = float(now) if explicit_now else time.time()
                # Fresh bypasses the telemetry cache. Existing watches retain their
                # persisted durations; only a just-created watch uses this freshly
                # re-read post-collection document.
                tracked = self.store.get_state("tracked", {}, fresh=True)
                portfolio = self.store.get_state("portfolio", initial_portfolio(), fresh=True)
                source_status = self.store.get_state("source", _empty_source(), fresh=True)
                self._assert_portfolio_capacity(portfolio)
                config = self.config()
                if not isinstance(source_status, dict):
                    source_status = _empty_source()
                done = self.store.get_state("done_cards", [], fresh=True)
                events = self.store.get_state("events", [], fresh=True)
                signal_state = self.store.get_state("signal_analysis", empty_signal_analysis(), fresh=True)
                structured_signal_events = signal_events(signal_state)
                stats = self.store.get_state("board_stats", _empty_board_stats(), fresh=True)
                if not isinstance(done, list):
                    done = []
                if not isinstance(events, list):
                    events = []
                if not isinstance(stats, dict):
                    stats = _empty_board_stats()
                for key, value in _empty_board_stats().items():
                    stats.setdefault(key, value)
                # Wall-clock expiry must run even when collection supplied no
                # usable reserve quote or failed before it produced a batch.
                self._expire_pending_watches(
                    tracked, portfolio, done, stats, events, structured_signal_events, now=mutation_now,
                )
                if batch.error:
                    source_status.update({"name": batch.source_name, "error": batch.error, "social": batch.social, "trackedTokens": len(tracked)})
                    events.append(f"{_now_iso(mutation_now)} source failure: {batch.error}")
                    signal_state = {
                        **(signal_state if isinstance(signal_state, dict) else empty_signal_analysis()),
                        "events": structured_signal_events[-120:],
                    }
                    self.store.set_many_state({
                        "tracked": tracked, "portfolio": portfolio, "source": source_status,
                        "done_cards": done[-MAX_DONE_CARDS:], "events": events[-60:], "board_stats": stats,
                        "signal_analysis": signal_state,
                    }, now=mutation_now)
                    self._set_health(runtime="degraded", now=mutation_now, error=batch.error)
                    return True

                # A source row that predates a durable track cannot rewrite its
                # current reserve state or fill a checkpoint retroactively.
                accepted_observations: list[dict[str, Any]] = []
                for item in batch.observations:
                    if not isinstance(item, dict):
                        continue
                    mint = str(item.get("mint") or "")
                    observed_at = _number(item.get("observed_at"))
                    if not mint or observed_at is None or observed_at > mutation_now:
                        continue
                    existing = tracked.get(mint)
                    previous_seen = _number(existing.get("last_seen")) if isinstance(existing, dict) else None
                    if previous_seen is not None and observed_at <= previous_seen:
                        continue
                    accepted_observations.append(item)

                self.store.record_observations(accepted_observations)
                stats["ticks"] = int(stats["ticks"]) + 1
                stats["scanned"] = int(stats["scanned"]) + len(accepted_observations)
                observed_by_mint = {str(item["mint"]): item for item in accepted_observations}
                # Existing open positions consume only their own actual mcap observations.
                for position_id, position in list(portfolio["positions"].items()):
                    if position.get("strategy") == PUMP_STRATEGY:
                        # The pump loop owns its own exits on verified reserves.
                        continue
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
                        events.append(f"{_now_iso(mutation_now)} EXIT {event['symbol']} {event['reason']} net ${event['net']:+.2f}")
                        structured_signal_events = record_signal_event(
                            structured_signal_events,
                            at=float(event["exit_at"]),
                            mint=event["mint"],
                            symbol=event["symbol"],
                            side="sell",
                            action="SELL",
                            readiness_pct=100.0,
                            reason=event["reason"],
                        )
                    elif event and event["status"] == "scaled":
                        events.append(f"{_now_iso(mutation_now)} SCALE {position['symbol']} {event['reason']}")
                        structured_signal_events = record_signal_event(
                            structured_signal_events,
                            at=float(event["at"]),
                            mint=position.get("mint"),
                            symbol=position.get("symbol"),
                            side="sell",
                            action="SCALE",
                            readiness_pct=100.0,
                            reason=event["reason"],
                        )

                for observation in accepted_observations:
                    mint = str(observation["mint"])
                    # Preserve main's reserve-data truth inside the atomic fence.
                    curve_known = observation.get("curve_known") is not False
                    track = tracked.get(mint)
                    if track is None:
                        if not curve_known:
                            continue
                        if len(tracked) >= MAX_TRACKED:
                            self._trim_tracks(tracked, portfolio, reserve_slots=1)
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
                        "last_mcap": observation["market_cap"], "risk": observation.get("risk"), "curve_known": bool(curve_known),
                    })
                    if curve_known:
                        track["last_curve"] = observation["curve_pct"]
                    else:
                        continue
                    try:
                        record_watch_observation(track, observation)
                        checkpoints = watch_checkpoint_state(track, as_of=float(observation["observed_at"]))
                    except (TypeError, ValueError, KeyError):
                        track["entry_decided"] = True
                        track["reason"] = "SKIP checkpoint state unavailable"
                        done.append({**self._watch_card(track, as_of=mutation_now), "column": "done", "verdict": "SKIP", "reason": track["reason"]})
                        stats["skips"] = int(stats["skips"]) + 1
                        structured_signal_events = record_signal_event(
                            structured_signal_events,
                            at=float(observation["observed_at"]),
                            mint=mint,
                            symbol=track.get("symbol"),
                            side="buy",
                            action="SKIP",
                            readiness_pct=None,
                            reason=track["reason"],
                        )
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
                        done.append({**self._watch_card(track, as_of=mutation_now), "column": "done", "verdict": "SKIP", "reason": track["reason"]})
                        stats["skips"] = int(stats["skips"]) + 1
                        events.append(f"{_now_iso(mutation_now)} SKIP {track['symbol']} {reason}")
                        structured_signal_events = record_signal_event(
                            structured_signal_events,
                            at=float(observation["observed_at"]),
                            mint=mint,
                            symbol=track.get("symbol"),
                            side="buy",
                            action="SKIP",
                            readiness_pct=None,
                            reason=reason,
                        )
                        continue
                    position = open_position(portfolio, observation, strategy_revision=int(config["revision"]), watch=track)
                    if position is None:
                        track["reason"] = "SKIP insufficient paper cash"
                        done.append({**self._watch_card(track, as_of=mutation_now), "column": "done", "verdict": "SKIP", "reason": track["reason"]})
                        stats["skips"] = int(stats["skips"]) + 1
                        events.append(f"{_now_iso(mutation_now)} SKIP {track['symbol']} insufficient paper cash")
                        structured_signal_events = record_signal_event(
                            structured_signal_events,
                            at=float(observation["observed_at"]),
                            mint=mint,
                            symbol=track.get("symbol"),
                            side="buy",
                            action="SKIP",
                            readiness_pct=None,
                            reason="insufficient paper cash",
                        )
                    else:
                        track["reason"] = "entered after three actual immutable watch checkpoints; social unavailable and unused"
                        stats["buys"] = int(stats["buys"]) + 1
                        events.append(f"{_now_iso(mutation_now)} BUY {track['symbol']} ${position['initial_notional']:.2f} actual mcap ${position['entry_mcap']:,.0f}")
                        structured_signal_events = record_signal_event(
                            structured_signal_events,
                            at=float(observation["observed_at"]),
                            mint=mint,
                            symbol=track.get("symbol"),
                            side="buy",
                            action="BUY",
                            readiness_pct=100.0,
                            reason="existing entry policy opened a paper position",
                        )

                self._trim_tracks(tracked, portfolio)
                done = done[-MAX_DONE_CARDS:]
                accepted_times = [_number(item.get("observed_at")) for item in accepted_observations]
                last_observed = max((value for value in accepted_times if value is not None), default=None)
                prior_observed = _number(source_status.get("lastObservation"))
                source_status.update({
                    "name": batch.source_name,
                    "lastObservation": max((value for value in (prior_observed, last_observed) if value is not None), default=None),
                    "error": None,
                    "observations": int(source_status.get("observations", 0)) + len(accepted_observations), "trackedTokens": len(tracked), "social": batch.social,
                })
                signal_state = build_signal_analysis(
                    {**(signal_state if isinstance(signal_state, dict) else empty_signal_analysis()), "events": structured_signal_events},
                    tracked=tracked,
                    portfolio=portfolio,
                    observations=accepted_observations,
                    strategy_revision=int(config["revision"]),
                    now=mutation_now,
                    operational_mints=(str(item["mint"]) for item in accepted_observations),
                )
                self.store.set_many_state({
                    "tracked": tracked, "portfolio": portfolio, "source": source_status,
                    "done_cards": done, "events": events[-60:], "board_stats": stats,
                    "signal_analysis": signal_state,
                }, now=mutation_now)
                self._run_rollback_if_needed(now=mutation_now, portfolio=portfolio)
                self._set_health(runtime="running", now=mutation_now)
        except LeaseLost:
            self.lease_owned = False
            return False

        self.local_error = None
        review_now = float(now) if explicit_now else time.time()
        self.maybe_review(now=review_now, use_wall_clock=not explicit_now)
        return True

    def pump_tick(self, *, now: float | None = None) -> bool:
        """One fast pump-capture cycle: collect outside the transaction, decide inside the lease fence.

        Paper only. Exits for open pump positions run in radar and paper modes; entries only in paper mode.
        """
        if not self.enabled or not self.lease_owned or self.pump_mode == "off":
            return False
        explicit_now = now is not None
        if self.pump_market is None:
            self.pump_market = PumpMarket()
        market = self.pump_market
        snapshot = self.store.get_state("portfolio", initial_portfolio())
        prior_state = self.store.get_state("pump_state", empty_pump_state(self.pump_mode, self.pump_variant))
        held: dict[str, dict[str, Any]] = {}
        for position in (snapshot.get("positions") or {}).values():
            if isinstance(position, dict) and position.get("strategy") == PUMP_STRATEGY:
                held[str(position["mint"])] = dict((position.get("pump") or {}).get("meta") or {}, symbol=position.get("symbol"))
        for order in (prior_state.get("pending") or []) if isinstance(prior_state, dict) else []:
            if isinstance(order, dict) and order.get("meta"):
                held.setdefault(str(order["mint"]), dict(order["meta"]))
        for mint, info in held.items():
            market.ensure_meta(mint, info)
        if market.sol_usd is None and isinstance(prior_state, dict):
            # After a restart the listing may be down; a recent durable SOL/USD keeps held tokens readable.
            saved = prior_state.get("solUsd") or {}
            seed_now = float(now) if explicit_now else time.time()
            if _number(saved.get("value")) and _number(saved.get("at")) is not None and seed_now - float(saved["at"]) <= 600:
                market.sol_usd = float(saved["value"])
        batch = market.collect(set(held), now=now if explicit_now else None)
        renew_now = float(now) if explicit_now else time.time()
        if not self._renew_lease(now=renew_now):
            return False
        fence_now = float(now) if explicit_now else None
        try:
            with self.store.lease_fence("optimizer-worker", self.boot_id, now=fence_now):
                mutation_now = float(now) if explicit_now else time.time()
                portfolio = self.store.get_state("portfolio", initial_portfolio(), fresh=True)
                self._assert_portfolio_capacity(portfolio)
                state = self.store.get_state("pump_state", None, fresh=True)
                if not isinstance(state, dict) or state.get("version") != 1:
                    state = empty_pump_state(self.pump_mode, self.pump_variant)
                state["variant"] = dict(self.pump_variant)
                config = self.config()
                had_positions = any(isinstance(p, dict) and p.get("strategy") == PUMP_STRATEGY for p in portfolio["positions"].values())
                results = run_pump_cycle(market, batch, state, portfolio, now=mutation_now, mode=self.pump_mode,
                                         strategy_revision=int(config["revision"]))
                self._assert_portfolio_capacity(portfolio)
                values: dict[str, Any] = {"pump_state": state}
                has_positions = any(isinstance(p, dict) and p.get("strategy") == PUMP_STRATEGY for p in portfolio["positions"].values())
                if results or had_positions or has_positions:
                    values["portfolio"] = portfolio
                if results:
                    done = self.store.get_state("done_cards", [], fresh=True)
                    events = self.store.get_state("events", [], fresh=True)
                    stats = self.store.get_state("board_stats", _empty_board_stats(), fresh=True)
                    signal_state = self.store.get_state("signal_analysis", empty_signal_analysis(), fresh=True)
                    done = done if isinstance(done, list) else []
                    events = events if isinstance(events, list) else []
                    stats = stats if isinstance(stats, dict) else _empty_board_stats()
                    for key, value in _empty_board_stats().items():
                        stats.setdefault(key, value)
                    structured = signal_events(signal_state)
                    for event in results:
                        if event["status"] == "closed":
                            self._record_closed(event, done)
                            stats["realized"] = round(float(stats["realized"]) + float(event["net"]), 8)
                            if float(event["net"]) > 0:
                                stats["wins"] = int(stats["wins"]) + 1
                            elif float(event["net"]) < 0:
                                stats["losses"] = int(stats["losses"]) + 1
                            events.append(f"{_now_iso(mutation_now)} PUMP EXIT {event['symbol']} {event['reason']} net ${event['net']:+.2f}")
                            structured = record_signal_event(structured, at=float(event["exit_at"]), mint=event["mint"], symbol=event["symbol"],
                                                             side="sell", action="SELL", readiness_pct=100.0, reason=event["reason"])
                        elif event["status"] == "scaled":
                            events.append(f"{_now_iso(mutation_now)} PUMP SCALE {event['symbol']} {event['reason']}")
                            structured = record_signal_event(structured, at=float(event["at"]), mint=event["mint"], symbol=event["symbol"],
                                                             side="sell", action="SCALE", readiness_pct=100.0, reason=f"pump: {event['reason']}")
                        elif event["status"] == "bought":
                            position = event["position"]
                            stats["buys"] = int(stats["buys"]) + 1
                            events.append(f"{_now_iso(mutation_now)} PUMP BUY {position['symbol']} ${position['pump']['ticket']:.2f} at mcap ${position['entry_mcap']:,.0f}")
                            structured = record_signal_event(structured, at=float(position["entry_at"]), mint=position["mint"], symbol=position["symbol"],
                                                             side="buy", action="BUY", readiness_pct=100.0,
                                                             reason=f"pump-capture-v1 {position['pump']['signal']} signal; next-read paper fill")
                    signal_state = {**(signal_state if isinstance(signal_state, dict) else empty_signal_analysis()), "events": structured[-120:]}
                    values.update({"done_cards": done[-MAX_DONE_CARDS:], "events": events[-60:], "board_stats": stats, "signal_analysis": signal_state})
                self.store.set_many_state(values, now=mutation_now)
        except LeaseLost:
            self.lease_owned = False
            return False
        return True

    def maybe_review(self, *, now: float, use_wall_clock: bool = False) -> None:
        """Compute candidates outside locks then fence the short durable decision."""
        if not self.lease_owned:
            return
        boundary = int(now // INTERVAL_SECONDS) * INTERVAL_SECONDS
        previous = self.store.get_state("last_review_boundary", fresh=True)
        if previous is not None and float(previous) >= boundary:
            return
        # Capture exactly one document before validation. The promotion CAS later
        # uses this same revision, so a slider save can only reject promotion.
        document = self.config()
        source = self.store.get_state("source", _empty_source(), fresh=True)
        tracked = self.store.get_state("tracked", {}, fresh=True)
        fresh = source.get("lastObservation") is not None and now - float(source["lastObservation"]) <= max(120.0, 4 * POLL_SECONDS)
        if not fresh:
            validation = {**_empty_validation(), "reason": "source is not fresh; review held"}
        elif len(tracked) < 30:
            validation = {**_empty_validation(), "reason": "coverage below 30 tracked forward market-cap paths"}
        else:
            # Bound memory/read cost without deleting optimizer-owned historical data.
            observations = self.store.observations(limit=OBSERVATION_READ_LIMIT)
            validation = validate_candidates(observations, document["watchPeriods"])
        decision = validation["state"]
        try:
            # Production validation can be comparatively expensive; use a fresh
            # wall-clock expiry check at commit rather than the tick-start time.
            with self.store.lease_fence("optimizer-worker", self.boot_id, now=None if use_wall_clock else now):
                durable_boundary = self.store.get_state("last_review_boundary", fresh=True)
                if durable_boundary is not None and float(durable_boundary) >= boundary:
                    return
                if validation["state"] == "promoted":
                    promoted = {"version": 1, "revision": int(document["revision"]) + 1, "watchPeriods": validation["candidate"], "max_open": MAX_OPEN}
                    try:
                        promoted_validation = {**validation, "reason": "heldout criteria met; promoted atomically"}
                        self.store.update_config_with_audit_and_state(
                            promoted,
                            int(document["revision"]),
                            {"action": "promotion", "previous": document, "next": promoted, "validation": validation},
                            state_values={
                                "last_validated_champion": document,
                                "active_promotion": {"previous": document, "promoted": promoted, "at": now},
                            },
                            now=now,
                        )
                        validation = promoted_validation
                        decision = "promoted"
                    except RevisionConflict:
                        validation = {**validation, "state": "waiting", "reason": "user configuration changed during review; no promotion"}
                        decision = "waiting"
                review = {"id": f"review-{boundary}", "at": now, "boundary": boundary, "decision": decision, "reason": validation["reason"], "metrics": validation}
                self.store.record_review(review)
                self.store.set_many_state({
                    "validation": validation,
                    "last_review_boundary": boundary,
                    "events": self._event(f"review {decision}: {validation['reason']}", now=now),
                }, now=now)
                self._set_health(runtime="running", now=now, last_review=now)
        except LeaseLost:
            self.lease_owned = False

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
        use_wall_clock = now is None
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
                "reason": ("pump-capture-v1 paper position: next-read fills, 1.25% fee + curve impact" if position.get("strategy") == PUMP_STRATEGY
                           else "forward paper position; modeled execution costs shown in optimizer status"), "ts_seen": position["entry_at"],
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
        # Active watched/held tokens have live price paths even before a closed
        # paper execution exists. They are not journal trades or realized returns.
        for track in tracked.values():
            mint = str(track["mint"])
            key = _display_symbol(track["symbol"], mint)
            if key in charts:
                continue
            started = _number(track.get("watch_started_ts", track.get("started_at")))
            if started is None:
                continue
            points = [point for point in by_mint.get(mint, []) if float(point["observed_at"]) >= started]
            if not points:
                continue
            checkpoints = watch_checkpoint_state(track, as_of=as_of)
            risk = track.get("risk") or {}
            risk_known = risk.get("status") == "known"
            limits = (("top10_pct", 30), ("dev_pct", 5), ("bundle_pct", 25))
            risk_safe = risk_known and all(_number(risk.get(name)) is not None and float(risk[name]) <= cap for name, cap in limits)
            observed_windows = sum(1 for item in checkpoints.values() if item["status"] == "observed")
            charts[key] = {
                "isMarketWatch": True, "metric": "market_cap",
                "series": [{"label": _now_iso(float(point["observed_at"])), "value": float(point["market_cap"])} for point in points],
                "signals": [], "entry": 0, "exitPnl": 0, "exitPp": 0,
                "dur": round((float(points[-1]["observed_at"]) - started) / 60, 2),
                "gatesBuy": [
                    {"name": "Holder risk", "detail": "Validated concentrations within risk caps" if risk_safe else str(risk.get("reason") or "Holder risk unavailable or outside limits; entry blocked"), "pass": bool(risk_safe)},
                    {"name": "Watch checkpoints", "detail": f"{observed_windows}/3 actual observations; pending entry gates", "pass": observed_windows == 3},
                    {"name": "Paper execution", "detail": "Live market watch path, not a completed paper execution", "pass": False},
                ],
                "gatesSell": [],
            }
        if use_wall_clock:
            # A read can wait for a worker transaction and then return a quote newer
            # than request-start time. Do not mislabel that current quote as future.
            now = time.time()
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
        self.pump_worker = PumpWorker(runtime, self.stop_event) if runtime.pump_mode != "off" else None

    def start(self) -> None:
        super().start()
        if self.pump_worker is not None:
            self.pump_worker.start()

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
                    completed = self.runtime.tick()
                    if completed and self.runtime.lease_owned:
                        from .hourly import hourly_monitor_tick
                        hourly_monitor_tick(
                            self.runtime.store,
                            now=time.time(),
                            holder=self.runtime.boot_id,
                            # Omit fence_now in production so the commit check uses
                            # the actual wall clock rather than tick-start time.
                        )
                    self.runtime.local_error = None
                # A standby process must not write shared health: an active process
                # could otherwise be overwritten as "standby" during a deployment.
                self.backoff_seconds = self.poll_seconds
            except Exception as exc:  # Defensive worker boundary; HTTP remains responsive.
                self.runtime.local_error = f"{type(exc).__name__}: {str(exc)[:180]}"
                try:
                    # Do not reacquire after a failed/stale cycle. A degradation
                    # write is allowed only when this process still owns a live
                    # lease at its own fenced commit.
                    if self.runtime.lease_owned:
                        error_now = time.time()
                        with self.runtime.store.lease_fence("optimizer-worker", self.runtime.boot_id):
                            self.runtime._set_health(runtime="degraded", now=error_now, error=self.runtime.local_error)
                except LeaseLost:
                    self.runtime.lease_owned = False
                except Exception:
                    # Keep the local diagnostic but never write an unfenced error.
                    pass
                self.backoff_seconds = min(max(self.poll_seconds, self.backoff_seconds * 2), 300.0)
            self.stop_event.wait(self.backoff_seconds)
        if self.pump_worker is not None and self.pump_worker.is_alive():
            self.pump_worker.join(timeout=15)
        if self.runtime.lease_owned:
            try:
                self.runtime.store.release_lease("optimizer-worker", self.runtime.boot_id)
            except Exception:
                pass
            self.runtime.lease_owned = False


class PumpWorker(threading.Thread):
    """Fast pump-capture loop sharing the main worker's lease (renew only, never acquire).

    The store serializes transactions on one connection under a process lock, and every
    fenced cycle re-reads portfolio state, so this thread and the main worker cannot
    overwrite each other or exceed the six-position cap.
    """

    def __init__(self, runtime: OptimizerRuntime, stop_event: threading.Event, *, cadence: float = PUMP_SECONDS) -> None:
        super().__init__(name="pumpmi-pump-capture", daemon=True)
        self.runtime = runtime
        self.stop_event = stop_event
        self.cadence = cadence
        self.backoff = cadence
        self.last_error: str | None = None

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.time()
            try:
                if self.runtime.lease_owned:
                    self.runtime.pump_tick()
                self.last_error = None
                self.backoff = self.cadence
            except Exception as exc:  # keep the main worker and HTTP alive
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self.backoff = min(max(self.cadence, self.backoff * 2), 60.0)
            elapsed = time.time() - started
            self.stop_event.wait(max(0.5, self.backoff - elapsed) if self.last_error is None else self.backoff)
