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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from src.optimizer.policy import (  # noqa: E402
    apply_observation,
    initial_portfolio,
    make_watch_state,
    open_position,
    position_size,
    record_watch_observation,
    watch_checkpoint_state,
    watch_entry_reason,
)
from src.optimizer.replay import chronological_split, replay, validate_candidates  # noqa: E402
from src.optimizer.runtime import OptimizerRuntime, Worker  # noqa: E402
from src.optimizer.source import HolderRiskCache, PumpFunSource, SourceBatch  # noqa: E402
from src.optimizer.store import DatabaseStore, RevisionConflict  # noqa: E402


def document(revision: int = 5) -> dict:
    return {"version": 1, "revision": revision, "watchPeriods": {"short": 30, "medium": 45, "long": 60}, "max_open": 6}


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
            known_observation("PATH", 60, 31_000),
            known_observation("PATH", 2401, 31_000),  # hard exit only after cutoff
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
            observations.extend([
                known_observation(mint, base, 25_000),
                known_observation(mint, base + 60, 26_000),
                known_observation(mint, base + 75, 25_500),
                known_observation(mint, base + 2476, final),
            ])
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
        self.assertIsNone(watch_entry_reason(observed, watch), "one-point long rise satisfies original flow fallback")

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
        self.assertIn("flow check 2/3", watch_entry_reason(flat, flat_watch) or "")
        self.assertEqual(position_size(1500, losing_streak=0), 130.0)
        self.assertEqual(position_size(1500, losing_streak=3), 65.0)

    def test_peak_drawdown_remains_after_recovery_and_dev_balance_drop_exits(self) -> None:
        portfolio = initial_portfolio()
        entry = known_observation("DD", 0, 30_000)
        position = open_position(portfolio, entry, strategy_revision=1)
        self.assertIsNotNone(position)
        assert position is not None
        # Rise, then recoverable drawdown, then recovery: max must not become zero.
        apply_observation(portfolio, position["id"], known_observation("DD", 60, 36_000))
        apply_observation(portfolio, position["id"], known_observation("DD", 120, 30_000))
        peak_drawdown = portfolio["max_drawdown_pct"]
        apply_observation(portfolio, position["id"], known_observation("DD", 180, 36_000))
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
        source = MockSource([
            [known_observation("MINT-A", 0, 25_000, symbol="DUP"), known_observation("MINT-B", 0, 25_000, symbol="DUP")],
            [known_observation("MINT-A", 0, 30_000, symbol="DUP"), known_observation("MINT-B", 0, 30_000, symbol="DUP")],
            [known_observation("MINT-A", 0, 30_000, symbol="DUP"), known_observation("MINT-B", 0, 30_000, symbol="DUP")],
        ])
        runtime = self.runtime(source)
        self.own_lease(runtime, 0)
        runtime.tick(now=1)
        runtime.tick(now=62)
        # Model Worker.run acquiring a new cycle lease after the long time jump.
        self.own_lease(runtime, 2463)
        runtime.tick(now=2463)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
