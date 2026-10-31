#!/usr/bin/env python3
"""Isolated software tests for the durable paper optimizer.

These tests use only injected SQLite and controlled mock market observations.
Their mock source is expressly not market evidence and is never used by the
production worker.
"""
from __future__ import annotations

import ssl
import sys
import time
import types
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from src.optimizer.policy import (  # noqa: E402
    apply_observation,
    exit_reason,
    initial_portfolio,
    net_of_costs_pct,
    make_watch_state,
    open_position,
    position_size,
    record_watch_observation,
    risk_reason,
    watch_checkpoint_state,
    entry_reason,
    watch_entry_reason,
)
from src.optimizer.replay import chronological_split, replay, validate_candidates  # noqa: E402
from src.optimizer.runtime import OptimizerRuntime, Worker  # noqa: E402
from src.optimizer.source import HolderRiskCache, PumpFunSource, SourceBatch  # noqa: E402
from src.optimizer.store import DatabaseStore, RevisionConflict  # noqa: E402


def document(revision: int = 5) -> dict:
    return {"version": 1, "revision": revision, "watchPeriods": {"short": 30, "medium": 45, "long": 60}, "max_open": 6}


def _watch_through(rows: list[dict]) -> dict:
    """Record every sample after the first onto a 30/180/1065 watch. Paper only."""
    first = rows[0]
    watch = make_watch_state(
        {"short": 30, "medium": 180, "long": 1065},
        started_at=float(first["observed_at"]),
        start_curve=float(first["curve_pct"]),
    )
    for item in rows[1:]:
        record_watch_observation(watch, item)
    return watch


def known_observation(mint: str, timestamp: float, mcap: float, *, symbol: str | None = None) -> dict:
    return {
        "mint": mint, "symbol": symbol or mint[:8], "name": f"Mock {mint[:8]}", "observed_at": timestamp,
        "market_cap": mcap, "curve_pct": 100 * mcap / 69000,
        "reply_count": 2,
        "risk": {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0},
        "social": {"status": "unavailable", "sentiment": None, "source": "none"}, "source": "mock-test-only",
    }


class MockSource:
    """Test-only source; never used by runtime construction in production."""
    name = "mock-test-only"
    social = "unavailable (test source; no sentiment)"

    def __init__(self, batches: list[list[dict]] | None = None, *, error: str | None = None) -> None:
        self.batches = batches or []
        self.error = error
        self.index = 0

    def collect(self, _tracked: list[str], *, now: float) -> SourceBatch:
        if self.error:
            return SourceBatch([], self.name, self.social, self.error)
        batch = self.batches[min(self.index, len(self.batches) - 1)] if self.batches else []
        self.index += 1
        # Fresh timestamp supplied by the runtime is copied into test samples so the
        # mock never sneaks future timestamps into a production-like tick.
        copied = [{**item, "observed_at": now} for item in batch]
        return SourceBatch(copied, self.name, self.social)


class OptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DatabaseStore("sqlite:///:memory:")

    def runtime(self, source: MockSource | None = None, *, boot: str = "test") -> OptimizerRuntime:
        return OptimizerRuntime(self.store, source=source or MockSource(), seed_document=document(), enabled=True, boot_id=boot)

    def own_lease(self, runtime: OptimizerRuntime, now: float = 0) -> None:
        runtime.lease_owned = self.store.acquire_lease("optimizer-worker", runtime.boot_id, ttl_seconds=75, now=now)
        self.assertTrue(runtime.lease_owned)

    def test_migration_seed_is_idempotent_and_never_resets_config(self) -> None:
        runtime = self.runtime()
        updated = runtime.update_config({"revision": 5, "watchPeriods": {"short": 45, "medium": 60, "long": 75}})
        self.assertEqual(updated["revision"], 6)
        # A recreated runtime performs migration again but must preserve user state.
        second = OptimizerRuntime(self.store, seed_document=document(5), enabled=True, boot_id="restart")
        self.assertEqual(second.config()["revision"], 6)
        self.assertEqual(second.config()["watchPeriods"], {"short": 45, "medium": 60, "long": 75})

    def test_durable_optimistic_config_conflict(self) -> None:
        runtime = self.runtime()
        runtime.update_config({"revision": 5, "watchPeriods": {"short": 45, "medium": 60, "long": 75}})
        with self.assertRaises(RevisionConflict):
            runtime.update_config({"revision": 5, "watchPeriods": {"short": 60, "medium": 75, "long": 90}})

    def test_lease_is_exclusive_and_recovers_after_ttl(self) -> None:
        self.store.migrate(document())
        self.assertTrue(self.store.acquire_lease("optimizer-worker", "one", ttl_seconds=30, now=100))
        self.assertFalse(self.store.acquire_lease("optimizer-worker", "two", ttl_seconds=30, now=101))
        self.assertTrue(self.store.acquire_lease("optimizer-worker", "two", ttl_seconds=30, now=131))

    def test_costs_are_per_side_and_max_six_positions(self) -> None:
        portfolio = initial_portfolio()
        positions = []
        for index in range(7):
            position = open_position(portfolio, known_observation(f"M{index}", 0, 30_000), strategy_revision=5)
            if position:
                positions.append(position)
        self.assertEqual(len(positions), 6)
        # A flat hard exit has zero gross P&L and 2% modeled cost on each side.
        outcome = apply_observation(portfolio, positions[0]["id"], known_observation("M0", 2401, 30_000))
        assert outcome is not None
        self.assertEqual(outcome["status"], "closed")
        self.assertAlmostEqual(outcome["gross"], 0.0, places=6)
        self.assertAlmostEqual(outcome["costs"], 5.2, places=6)  # 130 * (2% entry + 2% exit)
        self.assertAlmostEqual(outcome["net"], -5.2, places=6)

    def test_replay_excludes_future_exit_from_training_cutoff(self) -> None:
        observations = [
            known_observation("PATH", 0, 30_000),
            known_observation("PATH", 30, 30_400),
            known_observation("PATH", 45, 31_000),
            known_observation("PATH", 60, 31_800),
            known_observation("PATH", 2401, 31_800),  # soft exit only after cutoff
        ]
        before = replay(observations, document()["watchPeriods"], cutoff=60)
        after = replay(observations, document()["watchPeriods"])
        self.assertEqual(before["metrics"]["closed_trades"], 0)
        self.assertEqual(before["metrics"]["pending_trades"], 1)
        self.assertEqual(after["metrics"]["closed_trades"], 1)

    def test_heldout_minimum_keeps_waiting_when_fewer_than_thirty_closed(self) -> None:
        observations: list[dict] = []
        # 40 cohorts -> 12 heldout; all paths can close, but min 30 is strict.
        for index in range(40):
            base = float(index * 3000)
            mint = f"C{index}"
            observations.extend([
                known_observation(mint, base, 25_000),
                known_observation(mint, base + 60, 30_000),
                known_observation(mint, base + 2461, 30_000),
            ])
        result = validate_candidates(observations, document()["watchPeriods"])
        self.assertEqual(result["state"], "waiting")
        self.assertLess(result["testTrades"], 30)

    def test_heldout_promotion_is_atomic_and_uses_same_cohorts(self) -> None:
        runtime = self.runtime()
        observations: list[dict] = []
        # 101 chronological cohorts yield 70 train / 31 test. The valid +15s
        # long-watch neighbor gets a lower actual entry mcap; one late loss keeps
        # PF finite, so this tests all thresholds rather than a zero-loss shortcut.
        for index in range(101):
            base = float(index * 3000)
            mint = f"P{index}"
            final = 20_000 if index == 100 else 30_000
            # Curve points per second rise through both the 60s and 75s long windows.
            # The longer watch still enters at the cheaper 25_500 print.
            for offset, mcap, curve in (
                (0, 25_000, 36.00),
                (30, 25_200, 36.30),
                (45, 25_400, 36.60),
                (60, 26_000, 37.00),
                (75, 25_500, 37.50),
                (2476, final, 40.00),
            ):
                item = known_observation(mint, base + offset, mcap)
                item["curve_pct"] = curve
                observations.append(item)
        self.store.record_observations(observations)
        now = 302_500.0
        self.store.set_many_state({
            "source": {"name": "mock-test-only", "lastObservation": now, "error": None, "observations": len(observations), "trackedTokens": 30, "social": "unavailable"},
            "tracked": {f"live-{index}": {"mint": f"live-{index}"} for index in range(30)},
        }, now=now)
        self.own_lease(runtime, now=now)
        runtime.maybe_review(now=now)
        validation = runtime.status(now=now)["validation"]
        self.assertEqual(validation["state"], "promoted")
        self.assertEqual(validation["candidate"], {"short": 30, "medium": 45, "long": 75})
        self.assertGreater(validation["testTrades"], 30 - 1)
        self.assertGreater(validation["testPF"], 1.1)
        self.assertEqual(runtime.config()["revision"], 6)
        self.assertEqual(runtime.config()["watchPeriods"]["long"], 75)
        self.assertEqual(len(self.store.reviews()), 1)

    def test_rollback_restores_last_validated_champion_only_after_ten_percent_drawdown(self) -> None:
        runtime = self.runtime()
        previous = runtime.config()
        promoted = {"version": 1, "revision": 6, "watchPeriods": {"short": 30, "medium": 45, "long": 75}, "max_open": 6}
        self.store.update_config(promoted, 5, source="optimizer", now=10)
        portfolio = initial_portfolio()
        portfolio["drawdown_pct"] = 10.01
        self.store.set_many_state({"active_promotion": {"previous": previous, "promoted": promoted, "at": 10}, "portfolio": portfolio})
        _, validation = runtime._run_rollback_if_needed(now=20, portfolio=portfolio)
        self.assertIsNotNone(validation)
        assert validation is not None
        self.assertEqual(validation["state"], "rolled_back")
        self.assertEqual(runtime.config()["revision"], 7)
        self.assertEqual(runtime.config()["watchPeriods"], previous["watchPeriods"])

    def test_snapshot_persists_across_restart_and_social_is_never_fixture(self) -> None:
        source = MockSource([[known_observation("LIVE", 0, 25_000)], [known_observation("LIVE", 0, 30_000)]])
        first = self.runtime(source)
        self.own_lease(first, 0)
        first.tick(now=1)
        first.tick(now=62)
        self.assertEqual(first.status(now=62)["source"]["social"], source.social)
        self.assertNotIn("fixture", first.status(now=62)["source"]["social"].lower())
        restarted = OptimizerRuntime(self.store, source=source, seed_document=document(), enabled=True, boot_id="restarted")
        payload = restarted.snapshot_payload(now=62)
        self.assertEqual(payload["meta"]["kind"], "live")
        self.assertEqual(payload["board"]["ts"], 62)
        self.assertGreaterEqual(payload["board"]["stats"]["observations"], 2)

    def test_live_watch_path_contains_prices_not_fake_executions(self) -> None:
        runtime = self.runtime(MockSource([[known_observation("WATCH", 0, 25_000)]]))
        self.own_lease(runtime, 0)
        runtime.tick(now=1)
        frame = runtime.snapshot_payload(now=1)
        self.assertTrue(frame["trades"])
        watch = next(iter(frame["trades"].values()))
        self.assertTrue(watch["isMarketWatch"])
        self.assertEqual(watch["signals"], [])
        self.assertEqual(watch["entry"], 0)
        self.assertEqual(watch["series"][0]["value"], 25_000)
        self.assertEqual(frame["board"]["stats"]["buys"], 0)
        self.assertFalse(watch["gatesBuy"][-1]["pass"])

    def test_unverified_and_failed_source_are_not_advertised_fresh(self) -> None:
        runtime = self.runtime(MockSource([[known_observation("LIVE", 0, 25_000)]]))
        waiting = runtime.snapshot_payload(now=0)
        self.assertEqual(waiting["meta"]["kind"], "awaiting")
        self.assertIsNone(waiting["board"]["ts"])
        self.assertNotIn("Fresh", waiting["meta"]["label"])
        self.own_lease(runtime, 0)
        runtime.tick(now=1)
        active = runtime.snapshot_payload(now=1)
        self.assertEqual(active["meta"]["kind"], "live")
        self.assertIsInstance(active["meta"]["updated"], str)
        source = self.store.get_state("source")
        source["error"] = "source connection interrupted"
        self.store.set_state("source", source)
        self.assertEqual(runtime.snapshot_payload(now=2)["meta"]["kind"], "stale")

    def test_source_failure_is_visible_then_recovery_returns_running(self) -> None:
        failing = self.runtime(MockSource(error="test network timeout"))
        self.own_lease(failing, 0)
        failing.tick(now=1)
        self.assertEqual(failing.status(now=1)["runtime"], "degraded")
        self.assertIn("timeout", failing.status(now=1)["source"]["error"])
        healthy = OptimizerRuntime(self.store, source=MockSource([[known_observation("OK", 0, 25_000)]]), seed_document=document(), enabled=True, boot_id="healthy")
        self.store.release_lease("optimizer-worker", "test")
        self.own_lease(healthy, 2)
        healthy.tick(now=2)
        self.assertEqual(healthy.status(now=2)["runtime"], "running")

    def test_holder_risk_uses_validated_token_supply_and_known_curve_account(self) -> None:
        calls: list[str] = []

        def rpc(payload: dict) -> dict:
            calls.append(payload["method"])
            if payload["method"] == "getTokenLargestAccounts":
                return {"jsonrpc": "2.0", "result": {"value": [
                    {"address": "curve-token-account", "uiAmount": 70.0},
                    {"address": "holder-one", "uiAmount": 20.0},
                    {"address": "holder-two", "uiAmount": 10.0},
                ]}}
            if payload["method"] == "getTokenSupply":
                return {"jsonrpc": "2.0", "result": {"value": {"uiAmount": 100.0}}}
            return {"jsonrpc": "2.0", "result": {"value": [
                {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": 1.0}}}}}},
            ]}}

        risk = HolderRiskCache(rpc=rpc).get({
            "mint": "MINT", "creator": "CREATOR", "associated_bonding_curve": "curve-token-account",
            # Deliberately nonsense: source must not derive risk from feed raw supply.
            "total_supply": 1, "base_decimals": 9,
        }, now=1)
        self.assertEqual(calls, ["getTokenLargestAccounts", "getTokenSupply", "getTokenAccountsByOwner"])
        self.assertEqual(risk["status"], "known")
        self.assertEqual(risk["top10_pct"], 30.0)
        self.assertEqual(risk["bundle_pct"], 30.0)
        self.assertEqual(risk["dev_pct"], 1.0)
        self.assertEqual(PumpFunSource().max_holder_checks, 2)

    def test_holder_risk_invalid_rpc_or_missing_curve_stays_unknown(self) -> None:
        rpc_error = HolderRiskCache(rpc=lambda _payload: {"jsonrpc": "2.0", "error": {"message": "rate limited"}})
        error_risk = rpc_error.get({"mint": "MINT", "creator": "C", "associated_bonding_curve": "curve"}, now=1)
        self.assertEqual(error_risk["status"], "unknown")
        self.assertIn("RPC error", error_risk["reason"])

        def valid_without_curve(payload: dict) -> dict:
            if payload["method"] == "getTokenLargestAccounts":
                return {"jsonrpc": "2.0", "result": {"value": [{"address": "largest", "uiAmount": 100.0}]}}
            return {"jsonrpc": "2.0", "result": {"value": {"uiAmount": 100.0}}}

        missing_curve = HolderRiskCache(rpc=valid_without_curve).get({"mint": "MINT", "creator": "C"}, now=1)
        self.assertEqual(missing_curve["status"], "unknown")
        self.assertIn("curve account", missing_curve["reason"])

    def test_mysql_ssl_query_uses_verified_default_context(self) -> None:
        captured: dict = {}

        def connect(**kwargs):
            captured.update(kwargs)
            return object()

        fake_pymysql = types.SimpleNamespace(connect=connect, cursors=types.SimpleNamespace(DictCursor=object))
        with patch.dict(sys.modules, {"pymysql": fake_pymysql}):
            DatabaseStore("mysql://user:secret@managed.example:4000/optimizer?ssl=true")._mysql_connection()
        context = captured.get("ssl")
        self.assertIsInstance(context, ssl.SSLContext)
        assert isinstance(context, ssl.SSLContext)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_watch_checkpoints_are_actual_and_share_one_overdue_sample_truthfully(self) -> None:
        watch = make_watch_state({"short": 30, "medium": 45, "long": 60}, started_at=0, start_curve=40.0)
        observed = known_observation("WATCH", 75, 29_670)  # curve 43.0, one real late sample
        observed["reply_count"] = 0
        record_watch_observation(watch, observed)
        checkpoints = watch_checkpoint_state(watch, as_of=75)
        self.assertEqual(set(checkpoints), {"short", "medium", "long"})
        self.assertTrue(all(item["status"] == "observed" for item in checkpoints.values()))
        self.assertEqual({item["sampled_at"] for item in checkpoints.values()}, {75.0})
        # One late print still fills every tier with the same timestamp. It is not three windows.
        self.assertIn("one sample", watch_entry_reason(observed, watch) or "")

        no_sample = make_watch_state({"short": 30, "medium": 45, "long": 60}, started_at=0, start_curve=40.0)
        expired = watch_checkpoint_state(no_sample, as_of=75)
        self.assertTrue(all(item["status"] == "expired" for item in expired.values()))
        self.assertIn("checkpoint check 3/3", watch_entry_reason(observed, no_sample) or "")

    def test_checkpoint_decline_flow_gate_and_losing_streak_only_reduce_risk(self) -> None:
        watch = make_watch_state({"short": 30, "medium": 45, "long": 60}, started_at=0, start_curve=40.0)
        short_down = known_observation("WATCH", 30, 26_910)  # curve 39.0
        later_up = known_observation("WATCH", 60, 28_980)  # curve 42.0
        record_watch_observation(watch, short_down)
        record_watch_observation(watch, later_up)
        self.assertIn("short curve declined", watch_entry_reason(later_up, watch) or "")

        flat_watch = make_watch_state({"short": 30, "medium": 45, "long": 60}, started_at=0, start_curve=40.0)
        flat = known_observation("FLAT", 60, 27_600)  # curve exactly 40
        flat["reply_count"] = 0
        record_watch_observation(flat_watch, flat)
        self.assertIn("one sample", watch_entry_reason(flat, flat_watch) or "")
        # Three rising rates with a sub-point total and no replies still hit the older reply gate.
        paced = make_watch_state({"short": 30, "medium": 45, "long": 60}, started_at=0, start_curve=40.0)
        for stamp, mcap in ((30, 27_634.5), (45, 27_738.0), (60, 27_945.0)):
            sample = known_observation("PACE", stamp, mcap)
            sample["curve_pct"] = 40.0 + (0.05 if stamp == 30 else 0.20 if stamp == 45 else 0.50)
            sample["reply_count"] = 0
            record_watch_observation(paced, sample)
        self.assertIn("flow check 2/3", watch_entry_reason(sample, paced) or "")
        self.assertEqual(position_size(1500, losing_streak=0), 130.0)
        self.assertEqual(position_size(1500, losing_streak=3), 65.0)

    def test_peak_drawdown_remains_after_recovery_and_dev_balance_drop_exits(self) -> None:
        portfolio = initial_portfolio()
        entry = known_observation("DD", 0, 30_000)
        position = open_position(portfolio, entry, strategy_revision=1)
        self.assertIsNotNone(position)
        assert position is not None
        # Rise, then recoverable drawdown, then recovery: max must not become zero.
        # +10% spot is under the +8% net profit-lock arm, so this position stays open.
        apply_observation(portfolio, position["id"], known_observation("DD", 60, 33_000))
        apply_observation(portfolio, position["id"], known_observation("DD", 120, 30_000))
        peak_drawdown = portfolio["max_drawdown_pct"]
        apply_observation(portfolio, position["id"], known_observation("DD", 180, 33_000))
        self.assertGreater(peak_drawdown, 0)
        self.assertEqual(portfolio["max_drawdown_pct"], peak_drawdown)

        dev_portfolio = initial_portfolio()
        dev_position = open_position(dev_portfolio, known_observation("DEV", 0, 30_000), strategy_revision=1)
        self.assertIsNotNone(dev_position)
        assert dev_position is not None
        dev_sample = known_observation("DEV", 60, 30_000)
        dev_sample["risk"] = {"status": "known", "top10_pct": 10.0, "dev_pct": 0.5, "bundle_pct": 10.0}
        event = apply_observation(dev_portfolio, dev_position["id"], dev_sample)
        self.assertEqual(event and event["status"], "closed")
        self.assertIn("developer balance decreased", event["reason"] if event else "")

    def test_profit_lock_banks_a_round_trip_the_hard_stop_would_give_back(self) -> None:
        portfolio = initial_portfolio()
        position = open_position(portfolio, known_observation("LOCK", 0, 30_000), strategy_revision=1)
        self.assertIsNotNone(position)
        assert position is not None
        # +20% spot is about +15.6% net after both sides of 100 bps fee + 100 bps slippage.
        self.assertIsNone(apply_observation(portfolio, position["id"], known_observation("LOCK", 60, 36_000)))
        # +30% spot still scales half. The lock must not flatten a print at the scale target.
        scaled = initial_portfolio()
        runner = open_position(scaled, known_observation("RUN", 0, 30_000), strategy_revision=1)
        self.assertIsNotNone(runner)
        assert runner is not None
        half = apply_observation(scaled, runner["id"], known_observation("RUN", 60, 39_000))
        self.assertEqual(half and half["status"], "scaled")
        self.assertIn("scale half", half["reason"] if half else "")

        locked = apply_observation(portfolio, position["id"], known_observation("LOCK", 120, 33_000))
        self.assertEqual(locked and locked["status"], "closed")
        assert locked is not None
        self.assertTrue((locked or {}).get("reason", "").startswith("profit lock"))
        self.assertGreater(float((locked or {}).get("net") or 0), 0)
        # The same path used to ride 30k -> 36k -> 33k -> 24k into a -20% stop, net about -30.68.
        self.assertGreater(float(locked["net"]), -30.68)

    def test_dying_curve_rate_and_one_sample_do_not_buy_the_stop(self) -> None:
        """Saved 30/180/1065 watches. Cumulative points rise; points per second do not.

        Before this gate the same -20% path entered and stopped at net -30.68.
        An accelerating path that hard-exits +10% still closes at net +7.54.
        """
        periods = {"short": 30, "medium": 180, "long": 1065}

        def row(mint: str, stamp: float, mcap: float, curve: float, replies: int = 0) -> dict:
            item = known_observation(mint, stamp, mcap)
            item["curve_pct"] = curve
            item["reply_count"] = replies
            return item

        fade = [
            row("FADE", 0, 30_000, 40.0),
            row("FADE", 30, 32_000, 42.0),
            row("FADE", 180, 32_500, 42.4),
            row("FADE", 1065, 32_800, 42.8),
            row("FADE", 1200, 26_240, 42.0),
        ]
        faded = replay(fade, periods)
        self.assertEqual(faded["metrics"]["closed_trades"], 0)
        self.assertEqual(faded["metrics"]["net"], 0)
        self.assertIn("not accelerating", watch_entry_reason(fade[3], _watch_through(fade)) or "")
        portfolio = initial_portfolio()
        opened = open_position(portfolio, fade[3], strategy_revision=0)
        self.assertIsNotNone(opened)
        assert opened is not None
        stopped = apply_observation(portfolio, opened["id"], fade[4])
        self.assertIsNotNone(stopped)
        assert stopped is not None
        self.assertAlmostEqual(float(stopped["net"]), -30.68, places=2)
        self.assertAlmostEqual(float(stopped["gross"]), -26.0, places=2)
        self.assertAlmostEqual(float(stopped["costs"]), 4.68, places=2)

        one = [
            row("ONE", 0, 30_000, 40.0),
            row("ONE", 1065, 33_000, 43.0),
            row("ONE", 1200, 26_400, 43.0),
        ]
        skipped = replay(one, periods)
        self.assertEqual(skipped["metrics"]["closed_trades"], 0)
        self.assertEqual(skipped["metrics"]["net"], 0)
        self.assertIn("one sample", watch_entry_reason(one[1], _watch_through(one[:2])) or "")

        good = [
            row("GOOD", 0, 28_000, 40.0, replies=2),
            row("GOOD", 30, 28_400, 40.30, replies=2),
            row("GOOD", 180, 30_000, 43.30, replies=2),
            row("GOOD", 1065, 30_000, 63.30, replies=2),
            row("GOOD", 3465, 33_000, 64.0, replies=2),
        ]
        kept = replay(good, periods)
        self.assertEqual(kept["metrics"]["closed_trades"], 1)
        self.assertAlmostEqual(kept["metrics"]["net"], 7.54, places=2)
        self.assertIsNone(watch_entry_reason(good[3], _watch_through(good[:4])))

    def test_expensive_curve_depth_does_not_buy_the_stop(self) -> None:
        """A $2,000 book on these reserves loses more than 10% if spot does not move.

        The same accelerating path without reserve words still enters and stops.
        Before the drag gate that stop is net -30.68. After it, the trade is not opened.
        A $30,000 book on the same reserves still clears the gate.
        """
        periods = {"short": 30, "medium": 180, "long": 1065}
        reserves = {"virtual_sol": 40_000_000_000, "virtual_token": 10**15}

        def row(stamp: float, mcap: float, curve: float) -> dict:
            item = known_observation("THIN", stamp, mcap)
            item["curve_pct"] = curve
            item["reply_count"] = 2
            return item

        bare = [
            row(0, 1_866.67, 40.0),
            row(30, 1_893.33, 40.30),
            row(180, 2_000.0, 43.30),
            row(1065, 2_000.0, 63.30),
            row(1200, 1_600.0, 60.0),
        ]
        priced = [dict(item) for item in bare]
        priced[3].update(reserves)
        watch = _watch_through(priced)
        self.assertIsNone(watch_entry_reason(priced[3], watch))
        self.assertIn("curve impact", entry_reason(priced[3], watch) or "")
        deep = dict(priced[3], market_cap=30_000)
        self.assertIsNone(entry_reason(deep, watch))
        skipped = replay(priced, periods)
        self.assertEqual(skipped["metrics"]["closed_trades"], 0)
        self.assertEqual(skipped["metrics"]["net"], 0)
        taken = replay(bare, periods)
        self.assertEqual(taken["metrics"]["closed_trades"], 1)
        self.assertAlmostEqual(taken["metrics"]["net"], -30.68, places=2)

    def test_net_exits_and_depth_size_keep_the_flat_cost_model(self) -> None:
        """A -12% spot print is already a -15% paper loss. A gross stop used to hold it.

        Riding that print on to -20% spot is the old -30.68. Stopping here is about -20.49.
        A +6% spot print is only about +1.9% net, so the 12-minute soft exit takes it.
        +30% spot is still a scale, not a lock. On an $8k book a $160 ticket is cut to
        the $130 floor; a $60k book keeps $160. Neither path is a profitability claim.
        """
        portfolio = initial_portfolio()
        position = open_position(portfolio, known_observation("STOP", 0, 30_000), strategy_revision=1)
        self.assertIsNotNone(position)
        assert position is not None
        early = known_observation("STOP", 60, 26_400)
        self.assertGreater(100.0 * (26_400 / 30_000 - 1.0), -15.0)
        self.assertLess(net_of_costs_pct(30_000, 26_400), -15.0)
        stopped = apply_observation(portfolio, position["id"], early)
        self.assertEqual(stopped and stopped["status"], "closed")
        self.assertIn("stop loss", stopped["reason"] if stopped else "")
        self.assertAlmostEqual(float(stopped["net"]) if stopped else 0, -20.488, places=2)
        self.assertGreater(float(stopped["net"]) if stopped else 0, -30.68)

        soft_book = initial_portfolio()
        soft_position = open_position(soft_book, known_observation("SOFT", 0, 30_000), strategy_revision=1)
        self.assertIsNotNone(soft_position)
        assert soft_position is not None
        # +6% spot is above the old 5% gross soft line and still under +5% net.
        self.assertLess(net_of_costs_pct(30_000, 31_800), 5.0)
        softened = apply_observation(soft_book, soft_position["id"], known_observation("SOFT", 800, 31_800))
        self.assertEqual(softened and softened["status"], "closed")
        self.assertTrue((softened or {}).get("reason", "").startswith("soft 12m"))
        self.assertGreater(float(softened["net"]) if softened else 0, 0)

        scale_book = initial_portfolio()
        runner = open_position(scale_book, known_observation("RUN2", 0, 30_000), strategy_revision=1)
        self.assertIsNotNone(runner)
        assert runner is not None
        half = apply_observation(scale_book, runner["id"], known_observation("RUN2", 60, 39_000))
        self.assertEqual(half and half["status"], "scaled")

        rich = initial_portfolio()
        rich["cash"] = 12_000
        rich["peak_equity"] = 12_000
        reserves = {"virtual_sol": 40_000_000_000, "virtual_token": 10**15, "curve_pct": 40.0}
        thin = known_observation("CUT", 0, 8_000)
        thin.update(reserves)
        cut = open_position(rich, thin, strategy_revision=1)
        self.assertIsNotNone(cut)
        assert cut is not None
        self.assertEqual(cut["initial_notional"], 130.0)
        deep_book = initial_portfolio()
        deep_book["cash"] = 12_000
        deep_book["peak_equity"] = 12_000
        deep = known_observation("DEEPSIZE", 0, 60_000)
        deep.update(reserves)
        kept = open_position(deep_book, deep, strategy_revision=1)
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertEqual(kept["initial_notional"], 160.0)

    def test_chronological_split_never_divides_same_timestamp_cohort(self) -> None:
        observations: list[dict] = []
        for mint in ("A", "B", "C"):
            observations.append(known_observation(mint, 0, 30_000))
        for mint in ("D", "E", "F", "G"):
            observations.append(known_observation(mint, 100, 30_000))
        train, test, split_at = chronological_split(observations)
        self.assertEqual(split_at, 0.0)
        self.assertEqual(train, {"A", "B", "C"})
        self.assertEqual(test, {"D", "E", "F", "G"})
        tied = [known_observation(mint, 0, 30_000) for mint in ("X", "Y")]
        self.assertEqual(chronological_split(tied), (set(), set(), None))

    def test_post_collection_lease_fence_prevents_stale_commit(self) -> None:
        store = self.store
        case = self

        class LeaseStealingSource:
            name = "lease-test-source"
            social = "unavailable"

            def collect(self, _tracked: list[str], *, now: float) -> SourceBatch:
                # Simulate a rolling deployment taking over while collection is slow.
                case.assertTrue(store.acquire_lease("optimizer-worker", "new-worker", ttl_seconds=75, now=76))
                return SourceBatch([known_observation("STALE", now, 30_000)], self.name, self.social)

        runtime = OptimizerRuntime(store, source=LeaseStealingSource(), seed_document=document(), enabled=True, boot_id="old-worker")
        self.own_lease(runtime, 0)
        self.assertFalse(runtime.tick(now=1))
        self.assertFalse(runtime.lease_owned)
        self.assertEqual(store.observations(), [])
        self.assertEqual(store.get_state("source", {"observations": 0})["observations"], 0)

    def test_standby_worker_does_not_overwrite_active_health_and_success_clears_local_error(self) -> None:
        active_now = time.time()
        standby = self.runtime(MockSource(), boot="standby")
        self.store.set_state("health", {"runtime": "running", "lastTick": 11, "lastReview": None, "nextReview": 12, "error": None})
        self.assertTrue(self.store.acquire_lease("optimizer-worker", "active", ttl_seconds=75, now=active_now))
        worker = Worker(standby, poll_seconds=0.01)
        worker.start()
        time.sleep(0.04)
        worker.stop()
        worker.join(timeout=1)
        self.assertEqual(self.store.get_state("health")["runtime"], "running")

        self.store.release_lease("optimizer-worker", "active")
        recovering = self.runtime(MockSource([[known_observation("RECOVER", 0, 30_000)]]), boot="recover")
        self.own_lease(recovering, 0)
        recovering.local_error = "prior worker exception"
        self.assertTrue(recovering.tick(now=1))
        self.assertIsNone(recovering.local_error)

    def test_snapshot_stats_trade_chart_and_watch_metadata_use_actual_values(self) -> None:
        # Two duplicate symbols prove card/chart trade keys remain mint-specific.
        # Samples at 1/31/46/61 are three distinct rising curve-rate windows, then a flat exit.
        def pair(mcap: float) -> list[dict]:
            return [
                known_observation("MINT-A", 0, mcap, symbol="DUP"),
                known_observation("MINT-B", 0, mcap, symbol="DUP"),
            ]
        source = MockSource([pair(25_000), pair(25_400), pair(26_000), pair(26_800), pair(26_800)])
        runtime = self.runtime(source)
        self.own_lease(runtime, 0)
        runtime.tick(now=1)
        runtime.tick(now=31)
        runtime.tick(now=46)
        runtime.tick(now=61)
        # Model Worker.run acquiring a new cycle lease after the long time jump.
        self.own_lease(runtime, 2462)
        runtime.tick(now=2462)
        payload = runtime.snapshot_payload()
        stats = payload["board"]["stats"]
        self.assertTrue({"ticks", "scanned", "buys", "wins", "losses", "skips", "realized"}.issubset(stats))
        self.assertEqual(stats["buys"], 2)
        self.assertEqual(len(payload["trades"]), 2)
        self.assertIn("DUP · MINT-A", payload["trades"])
        self.assertIn("DUP · MINT-B", payload["trades"])
        for key, chart in payload["trades"].items():
            self.assertTrue(chart["gatesBuy"], key)
            self.assertTrue(chart["gatesSell"], key)
            self.assertIsInstance(chart["exitPp"], float)
            self.assertEqual(chart["signals"][0]["usd"], 130.0)
            self.assertEqual(chart["signals"][1]["usd"], 130.0)
            self.assertIsNone(chart["signals"][0]["pnl"])
            self.assertEqual(chart["signals"][0]["label"], "Actual sampled paper entry")
            self.assertIn("_exitTs", chart)
        done = payload["board"]["columns"]["done"]
        self.assertTrue(done)
        self.assertTrue(all(card.get("watch_started_ts") is not None for card in done))
        self.assertTrue(all(set(card.get("watch_periods", {})) == {"short", "medium", "long"} for card in done))

    def test_half_hour_buckets_keep_no_trade_windows_at_zero_and_review_deduplicates(self) -> None:
        runtime = self.runtime(MockSource())
        trade = {
            "id": "closed", "status": "closed", "mint": "M", "symbol": "M", "entry_at": 100,
            "exit_at": 3601, "entry_mcap": 30_000, "exit_mcap": 31_000, "gross": 4.0, "costs": 5.2, "net": -1.2,
            "reason": "hard", "strategy_revision": 5, "risk": {}, "social": {"status": "unavailable"},
        }
        self.store.upsert_trade(trade)
        windows = runtime._windows(7201)
        self.assertTrue(any(not window["hasTrades"] and window["net"] == 0 for window in windows))
        self.assertTrue(all(window["net"] <= 0 or window["hasTrades"] for window in windows))
        # With no coverage, a boundary review waits; repeated same boundary creates one audit row.
        self.own_lease(runtime, now=7201)
        runtime.maybe_review(now=7201)
        runtime.maybe_review(now=7210)
        self.assertEqual(len(self.store.reviews()), 1)
        self.assertEqual(runtime.status(now=7210)["validation"]["state"], "waiting")
        self.assertEqual(runtime.config()["revision"], 5, "insufficient evidence must not promote")

    def test_reserve_curve_replaces_the_usd_proxy_and_unknown_does_not_enter_or_flatten(self) -> None:
        import base64
        import struct

        from src.curve import INITIAL_REAL_TOKEN_RESERVES

        def account(sold_fraction: float) -> str:
            real_token = int(INITIAL_REAL_TOKEN_RESERVES * (1.0 - sold_fraction))
            blob = b"\x00" * 8 + struct.pack("<QQQQ", 1_073_000_000_000_000, 30_000_000_000, real_token, 1)
            return base64.b64encode(blob).decode()

        late = account(0.915)  # real ~91.5%. $25,402 / 69_000 is ~36.8% and used to pass.
        mid = account(0.40)    # real ~40%. $10,000 / 69_000 is ~14.5% and used to fail.
        coins = {
            "LATE": {"mint": "LATE", "symbol": "LATE", "usd_market_cap": 25402, "bonding_curve": "curve-late", "reply_count": 4},
            "MID": {"mint": "MID", "symbol": "MID", "usd_market_cap": 10000, "bonding_curve": "curve-mid", "reply_count": 4},
            "MISS": {"mint": "MISS", "symbol": "MISS", "usd_market_cap": 30000, "bonding_curve": "curve-miss", "reply_count": 4},
        }

        class _Holders:
            cache: dict = {}

            def get(self, _coin, _now):
                return {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0, "reason": None}

        def rpc(payload):
            self.assertEqual(payload["method"], "getMultipleAccounts")
            blobs = {"curve-late": late, "curve-mid": mid}
            values = []
            for address in payload["params"][0]:
                raw = blobs.get(address)
                values.append({"data": [raw, "base64"]} if raw else None)
            return {"jsonrpc": "2.0", "result": {"value": values}}

        source = PumpFunSource(http_get=lambda _url: list(coins.values()), holder_cache=cast(HolderRiskCache, _Holders()), rpc=rpc)
        batch = source.collect([], now=10)
        self.assertIsNone(batch.error)
        by_mint = {row["mint"]: row for row in batch.observations}
        self.assertGreater(by_mint["LATE"]["curve_pct"], 90)
        self.assertTrue(by_mint["LATE"]["curve_known"])
        self.assertIn("curve outside", risk_reason(by_mint["LATE"]) or "")
        self.assertAlmostEqual(by_mint["MID"]["curve_pct"], 40.0, delta=0.2)
        self.assertEqual(by_mint["MID"]["virtual_sol"], 30_000_000_000)
        self.assertGreater(by_mint["MID"]["sol_usd"], 1)
        self.assertIsNone(risk_reason(by_mint["MID"]))
        # The old proxy would have rejected this $10k print (about 14%) and accepted the late one.
        self.assertFalse(25 <= 100 * 10000 / 69000 <= 85)
        self.assertTrue(25 <= 100 * 25402 / 69000 <= 85)
        self.assertFalse(by_mint["MISS"]["curve_known"])
        self.assertEqual(by_mint["MISS"]["curve_pct"], 0.0)
        self.assertEqual(by_mint["MISS"]["market_cap"], 30000)
        self.assertIn("reserves unavailable", risk_reason(by_mint["MISS"]) or "")

        portfolio = initial_portfolio()
        position = open_position(portfolio, known_observation("HELD", 0, 30_000), strategy_revision=1)
        self.assertIsNotNone(position)
        assert position is not None
        quiet = known_observation("HELD", 30, 30_000)
        quiet["curve_pct"] = 0.0
        quiet["curve_known"] = False
        self.assertIsNone(exit_reason(position, quiet))
        stopped = dict(quiet, market_cap=24_000, observed_at=60)
        event = apply_observation(portfolio, position["id"], stopped)
        self.assertEqual(event and event["status"], "closed")
        self.assertIn("stop loss", event["reason"] if event else "")

        runtime = self.runtime(MockSource([[quiet]]))
        self.own_lease(runtime, 0)
        runtime.tick(now=5)
        self.assertEqual(self.store.get_state("tracked"), {})
        self.assertEqual(runtime.status(now=5)["portfolio"]["openPositions"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
