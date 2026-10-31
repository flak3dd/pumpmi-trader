#!/usr/bin/env python3
"""Deterministic durable-worker concurrency tests.

Every test uses two independent ``DatabaseStore`` connections to one temporary
SQLite file and mock observations only. No test calls ``from_env`` or makes a
provider request, so this suite cannot touch the live MySQL paper portfolio.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from src.optimizer.hourly import hourly_monitor_tick  # noqa: E402
from src.optimizer.policy import MAX_OPEN, initial_portfolio, make_watch_state, open_position  # noqa: E402
from src.optimizer.runtime import OptimizerRuntime  # noqa: E402
from src.optimizer.source import SourceBatch  # noqa: E402
from src.optimizer.store import DatabaseStore, LeaseLost, RevisionConflict  # noqa: E402


def document(revision: int = 7, periods: dict | None = None) -> dict:
    return {
        "version": 1,
        "revision": revision,
        "watchPeriods": periods or {"short": 30, "medium": 45, "long": 60},
        "max_open": 6,
    }


def observation(mint: str, at: float, mcap: float = 30_000.0) -> dict:
    return {
        "mint": mint,
        "symbol": mint[:8],
        "name": f"Mock {mint[:8]}",
        "observed_at": at,
        "market_cap": mcap,
        "curve_pct": 100 * mcap / 69_000,
        "reply_count": 2,
        "risk": {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0},
        "social": {"status": "unavailable", "sentiment": None, "source": "isolated-test"},
        "source": "isolated-test-only",
    }


class QueueSource:
    """No-network source that copies the cycle clock into every sample."""

    name = "isolated-test-source"
    social = "unavailable (isolated test)"

    def __init__(self, batches: list[list[dict]], before_collect=None) -> None:
        self.batches = batches
        self.before_collect = before_collect
        self.index = 0

    def collect(self, _tracked: list[str], *, now: float) -> SourceBatch:
        if self.before_collect is not None:
            callback, self.before_collect = self.before_collect, None
            callback()
        batch = self.batches[min(self.index, len(self.batches) - 1)] if self.batches else []
        self.index += 1
        return SourceBatch([{**item, "observed_at": now} for item in batch], self.name, self.social)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pumpmi-concurrency-")
        self.path = Path(self.temp.name) / "optimizer.sqlite"
        self.url = f"sqlite:///{self.path}"
        self.a = DatabaseStore(self.url)
        self.b = DatabaseStore(self.url)
        self.ra = OptimizerRuntime(self.a, source=QueueSource([]), seed_document=document(), enabled=True, boot_id="worker-a")
        self.rb = OptimizerRuntime(self.b, source=QueueSource([]), seed_document=document(), enabled=True, boot_id="worker-b")

    def tearDown(self) -> None:
        self.a.close()
        self.b.close()
        self.temp.cleanup()

    @staticmethod
    def _audit_count(store: DatabaseStore) -> int:
        with store.transaction() as cur:
            row = store._execute(cur, "SELECT COUNT(*) AS count FROM optimizer_audits").fetchone()
        return int(row["count"])

    def own(self, runtime: OptimizerRuntime, now: float) -> None:
        runtime.lease_owned = runtime.store.acquire_lease("optimizer-worker", runtime.boot_id, ttl_seconds=75, now=now)
        self.assertTrue(runtime.lease_owned)

    def test_stale_holder_write_is_rejected_after_expiry_and_replacement(self) -> None:
        self.assertTrue(self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=10, now=0))
        self.assertTrue(self.b.acquire_lease("optimizer-worker", "worker-b", ttl_seconds=75, now=11))
        with self.assertRaises(LeaseLost):
            with self.a.lease_fence("optimizer-worker", "worker-a", now=11):
                self.a.set_state("events", ["stale write"], now=11)
        self.assertEqual(self.b.get_state("events", fresh=True), [])

    def test_processing_expiry_rolls_back_observations_portfolio_and_counters(self) -> None:
        self.assertTrue(self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=10, now=0))
        with patch("src.optimizer.store.time.time", side_effect=[0.0, 11.0]):
            with self.assertRaises(LeaseLost):
                with self.a.lease_fence("optimizer-worker", "worker-a"):
                    self.a.record_observations([observation("EXPIRED", 1)])
                    self.a.set_many_state({
                        "portfolio": {**initial_portfolio(), "cash": 1.0},
                        "board_stats": {"ticks": 999},
                    }, now=0)
        self.assertEqual(self.a.observations(), [])
        self.assertEqual(self.a.get_state("portfolio", fresh=True)["cash"], 1500.0)
        self.assertEqual(self.a.get_state("board_stats", fresh=True)["ticks"], 0)
        self.assertTrue(self.b.acquire_lease("optimizer-worker", "worker-b", ttl_seconds=75, now=11))

    def test_acquisition_invalidates_stale_telemetry_cache_on_handoff(self) -> None:
        old_source = self.a.get_state("source")  # Populate worker A's local cache.
        self.assertTrue(self.b.acquire_lease("optimizer-worker", "worker-b", ttl_seconds=75, now=0))
        replacement = {"name": "handoff", "lastObservation": 1, "error": None, "observations": 44, "trackedTokens": 2, "social": "unavailable"}
        with self.b.lease_fence("optimizer-worker", "worker-b", now=1):
            self.b.set_state("source", replacement, now=1)
        self.assertEqual(self.a.get_state("source"), old_source, "pre-handoff telemetry cache may be stale")
        self.assertTrue(self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=75, now=76))
        self.assertEqual(self.a.get_state("source"), replacement, "new owner must invalidate and reload trading state")

    def test_six_position_cap_survives_restart_and_denies_seventh(self) -> None:
        seven = [observation(f"MINT-{index}", 0) for index in range(7)]
        self.ra.source = QueueSource([seven, seven])
        self.own(self.ra, 0)
        self.assertTrue(self.ra.tick(now=1))
        self.assertTrue(self.ra.tick(now=62))
        self.assertEqual(len(self.a.get_state("portfolio", fresh=True)["positions"]), MAX_OPEN)

        self.a.release_lease("optimizer-worker", "worker-a")
        eighth = [observation("EIGHTH", 0)]
        self.rb.source = QueueSource([eighth, eighth])
        self.own(self.rb, 63)
        self.assertTrue(self.rb.tick(now=63))
        self.assertTrue(self.rb.tick(now=124))
        portfolio = self.b.get_state("portfolio", fresh=True)
        self.assertEqual(len(portfolio["positions"]), MAX_OPEN)
        self.assertIn("max 6 open positions", self.b.get_state("tracked", fresh=True)["EIGHTH"]["reason"])

    def test_closed_trade_observation_and_state_roll_back_together_on_fault(self) -> None:
        portfolio = initial_portfolio()
        position = open_position(portfolio, observation("CLOSEME", 0), strategy_revision=7)
        assert position is not None
        self.a.set_state("portfolio", portfolio)
        self.ra.source = QueueSource([[observation("CLOSEME", 0)]])
        # Worker.run acquires authority at the start of this later timeout cycle.
        self.own(self.ra, 2401)
        original_set_many = self.a.set_many_state

        def fail_final_state(values, **kwargs):
            if "tracked" in values:
                raise RuntimeError("injected final state failure")
            return original_set_many(values, **kwargs)

        with patch.object(self.a, "set_many_state", side_effect=fail_final_state):
            with self.assertRaisesRegex(RuntimeError, "injected final state failure"):
                self.ra.tick(now=2401)
        self.assertEqual(self.a.observations(), [])
        self.assertEqual(self.a.closed_trades(), [])
        self.assertIn(position["id"], self.a.get_state("portfolio", fresh=True)["positions"])
        self.assertEqual(self.a.get_state("board_stats", fresh=True)["ticks"], 0)

    def test_competing_initialization_is_insert_only_and_never_resets_existing_state(self) -> None:
        path = Path(self.temp.name) / "competing.sqlite"
        url = f"sqlite:///{path}"
        gate = threading.Barrier(2)
        failures: list[Exception] = []
        runtimes: list[OptimizerRuntime] = []
        runtime_lock = threading.Lock()

        def start(name: str) -> None:
            try:
                store = DatabaseStore(url)
                gate.wait(timeout=2)
                runtime = OptimizerRuntime(store, source=QueueSource([]), seed_document=document(), enabled=True, boot_id=name)
                with runtime_lock:
                    runtimes.append(runtime)
            except Exception as exc:  # pragma: no cover - assertion below provides detail
                failures.append(exc)

        threads = [threading.Thread(target=start, args=(f"start-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(failures, failures)
        self.assertEqual(len(runtimes), 2)
        first = runtimes[0]
        changed = first.update_config({"revision": 7, "watchPeriods": {"short": 45, "medium": 60, "long": 75}})
        custom = initial_portfolio()
        custom["cash"] = 1234.0
        first.store.set_state("portfolio", custom)
        restarted = OptimizerRuntime(runtimes[1].store, source=QueueSource([]), seed_document=document(1), enabled=True, boot_id="restart")
        self.assertEqual(restarted.config(), changed)
        self.assertEqual(restarted.store.get_state("portfolio", fresh=True)["cash"], 1234.0)
        for runtime in runtimes:
            runtime.store.close()

    def test_conflicting_user_cas_and_audit_are_atomic(self) -> None:
        next_document = self.ra.update_config({"revision": 7, "watchPeriods": {"short": 45, "medium": 60, "long": 75}})
        with self.assertRaises(RevisionConflict):
            self.rb.update_config({"revision": 7, "watchPeriods": {"short": 60, "medium": 75, "long": 90}})
        self.assertEqual(self.ra.config(), next_document)
        self.assertEqual(self._audit_count(self.a), 1)
        with patch.object(self.a, "_write_audit", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.ra.update_config({"revision": 8, "watchPeriods": {"short": 60, "medium": 75, "long": 90}})
        self.assertEqual(self.ra.config(), next_document)
        self.assertEqual(self._audit_count(self.a), 1)

    def test_save_during_collection_reloads_config_for_new_watch_only(self) -> None:
        updated_periods = {"short": 45, "medium": 60, "long": 75}
        previous_periods = document()["watchPeriods"]
        existing = {
            "mint": "EXISTING", "symbol": "EXISTING", "name": "Existing immutable watch",
            **make_watch_state(previous_periods, started_at=0, start_curve=40),
            "entry_decided": False, "reason": "watching", "last_seen": 0,
        }
        self.a.set_state("tracked", {"EXISTING": existing})
        self.ra.source = QueueSource(
            [[observation("NEW-WATCH", 0)]],
            before_collect=lambda: self.rb.update_config({"revision": 7, "watchPeriods": updated_periods}),
        )
        self.own(self.ra, 0)
        self.assertTrue(self.ra.tick(now=1))
        track = self.a.get_state("tracked", fresh=True)["NEW-WATCH"]
        self.assertEqual(track["watch_durations"], updated_periods)
        self.assertEqual(self.a.get_state("tracked", fresh=True)["EXISTING"]["watch_durations"], previous_periods)
        self.assertEqual(self.ra.config()["revision"], 8)

    def test_save_during_validation_rejects_promotion_at_captured_revision(self) -> None:
        now = 100.0
        self.a.set_many_state({
            "source": {"name": "test", "lastObservation": now, "error": None, "observations": 100, "trackedTokens": 30, "social": "unavailable"},
            "tracked": {f"T{index}": {"mint": f"T{index}"} for index in range(30)},
        }, now=now)
        self.own(self.ra, now)
        manual_periods = {"short": 45, "medium": 60, "long": 75}
        promoted_result = {"state": "promoted", "candidate": {"short": 30, "medium": 45, "long": 75}, "reason": "mock candidate"}

        def validate_after_manual_save(_observations, _periods):
            self.rb.update_config({"revision": 7, "watchPeriods": manual_periods})
            return promoted_result

        with patch("src.optimizer.runtime.validate_candidates", side_effect=validate_after_manual_save):
            self.ra.maybe_review(now=now)
        self.assertEqual(self.ra.config()["revision"], 8)
        self.assertEqual(self.ra.config()["watchPeriods"], manual_periods)
        self.assertIsNone(self.a.get_state("active_promotion", fresh=True))
        self.assertEqual(self.a.get_state("validation", fresh=True)["state"], "waiting")
        self.assertEqual(len(self.a.reviews()), 1)

    def test_promotion_and_rollback_faults_leave_atomic_state(self) -> None:
        original_write_state = self.a._write_state
        promoted = document(8, {"short": 30, "medium": 45, "long": 75})
        active = {"previous": document(), "promoted": promoted, "at": 1}

        def fail_marker(cur, key, value, **kwargs):
            if key == "active_promotion":
                raise RuntimeError("marker write fault")
            return original_write_state(cur, key, value, **kwargs)

        self.own(self.ra, 0)
        with self.a.lease_fence("optimizer-worker", "worker-a", now=1):
            with patch.object(self.a, "_write_state", side_effect=fail_marker):
                with self.assertRaisesRegex(RuntimeError, "marker write fault"):
                    self.a.update_config_with_audit_and_state(
                        promoted,
                        7,
                        {"action": "promotion", "previous": document(), "next": promoted},
                        state_values={"last_validated_champion": document(), "active_promotion": active},
                        now=1,
                    )
        self.assertEqual(self.ra.config()["revision"], 7)
        self.assertIsNone(self.a.get_state("active_promotion", fresh=True))
        self.assertEqual(self._audit_count(self.a), 0)

        with self.a.lease_fence("optimizer-worker", "worker-a", now=2):
            self.a.update_config_with_audit_and_state(
                promoted,
                7,
                {"action": "promotion", "previous": document(), "next": promoted},
                state_values={"last_validated_champion": document(), "active_promotion": active},
                now=2,
            )
        with patch.object(self.a, "_write_audit", side_effect=RuntimeError("rollback audit fault")):
            with self.a.lease_fence("optimizer-worker", "worker-a", now=3):
                with self.assertRaisesRegex(RuntimeError, "rollback audit fault"):
                    self.ra._run_rollback_if_needed(now=3, portfolio={**initial_portfolio(), "drawdown_pct": 11.0, "max_drawdown_pct": 11.0})
        self.assertEqual(self.ra.config(), promoted)
        self.assertEqual(self.a.get_state("active_promotion", fresh=True), active)
        self.assertEqual(self._audit_count(self.a), 1)

    def test_manual_save_clears_marker_so_rollback_cannot_undo_it(self) -> None:
        promoted = document(8, {"short": 30, "medium": 45, "long": 75})
        active = {"previous": document(), "promoted": promoted, "at": 1}
        self.own(self.ra, 0)
        with self.a.lease_fence("optimizer-worker", "worker-a", now=1):
            self.a.update_config_with_audit_and_state(
                promoted,
                7,
                {"action": "promotion", "previous": document(), "next": promoted},
                state_values={"last_validated_champion": document(), "active_promotion": active},
                now=1,
            )
        manual = self.ra.update_config({"revision": 8, "watchPeriods": {"short": 45, "medium": 60, "long": 75}})
        self.assertIsNone(self.a.get_state("active_promotion", fresh=True))
        with self.a.lease_fence("optimizer-worker", "worker-a", now=2):
            _, validation = self.ra._run_rollback_if_needed(
                now=2,
                portfolio={**initial_portfolio(), "drawdown_pct": 11.0, "max_drawdown_pct": 11.0},
            )
        self.assertIsNone(validation)
        self.assertEqual(self.ra.config(), manual)

    def test_hourly_writes_are_rejected_for_a_stale_worker(self) -> None:
        self.assertTrue(self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=75, now=0))
        self.assertIsNone(hourly_monitor_tick(self.a, now=1, holder="worker-a", fence_now=1))
        before = self.a.get_state("hourly_samples", [], fresh=True)
        self.assertTrue(self.b.acquire_lease("optimizer-worker", "worker-b", ttl_seconds=75, now=76))
        with self.assertRaises(LeaseLost):
            hourly_monitor_tick(self.a, now=76, holder="worker-a", fence_now=76)
        self.assertEqual(self.a.get_state("hourly_samples", [], fresh=True), before)

    def test_admission_at_full_track_capacity_evicts_one_decided_nonheld_watch_only(self) -> None:
        portfolio = initial_portfolio()
        held = open_position(portfolio, observation("HELD", 0), strategy_revision=7)
        assert held is not None
        tracked = {
            "HELD": {"mint": "HELD", "entry_decided": True, "last_seen": -1},
            **{f"OLD-{index}": {"mint": f"OLD-{index}", "entry_decided": True, "last_seen": float(index)} for index in range(63)},
        }
        self.assertEqual(len(tracked), 64)
        history = observation("HISTORY", -10)
        self.a.record_observations([history])
        self.a.set_many_state({"portfolio": portfolio, "tracked": tracked})
        self.ra.source = QueueSource([[observation("ADMITTED", 0)]])
        self.own(self.ra, 0)
        self.assertTrue(self.ra.tick(now=1))
        after_tracks = self.a.get_state("tracked", fresh=True)
        self.assertEqual(len(after_tracks), 64)
        self.assertIn("HELD", after_tracks)
        self.assertIn("ADMITTED", after_tracks)
        self.assertNotIn("OLD-0", after_tracks)
        self.assertEqual(len(self.a.get_state("portfolio", fresh=True)["positions"]), 1)
        self.assertLessEqual(len(self.a.get_state("portfolio", fresh=True)["positions"]), MAX_OPEN)
        self.assertEqual({item["mint"] for item in self.a.observations()}, {"HISTORY", "ADMITTED"})

    def test_over_cap_persisted_portfolio_is_rejected_without_reset_or_liquidation(self) -> None:
        corrupt = initial_portfolio()
        for index in range(MAX_OPEN):
            self.assertIsNotNone(open_position(corrupt, observation(f"TOO-MANY-{index}", 0), strategy_revision=7))
        injected = dict(next(iter(corrupt["positions"].values())))
        injected["id"] = "persisted-corruption-seventh"
        injected["mint"] = "persisted-corruption-seventh"
        corrupt["positions"][injected["id"]] = injected
        self.a.set_state("portfolio", corrupt)
        self.ra.source = QueueSource([[observation("IGNORED", 0)]])
        self.own(self.ra, 0)
        with self.assertRaisesRegex(RuntimeError, "exceeds fixed maximum"):
            self.ra.tick(now=1)
        after = self.a.get_state("portfolio", fresh=True)
        self.assertEqual(len(after["positions"]), MAX_OPEN + 1)
        self.assertEqual(self.a.observations(), [])

    def test_mysql_lease_guard_select_uses_row_locking_syntax(self) -> None:
        fake = object.__new__(DatabaseStore)
        fake.dialect = "mysql"
        self.assertTrue(fake._lease_select_sql().endswith("FOR UPDATE"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
