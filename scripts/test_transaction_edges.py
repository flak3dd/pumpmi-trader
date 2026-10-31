#!/usr/bin/env python3
"""Isolated transaction/lease edge regressions; never open the managed database."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pump-board-engine"))
from src.optimizer.runtime import OptimizerRuntime  # noqa: E402
from src.optimizer.source import SourceBatch  # noqa: E402
from src.optimizer.store import DatabaseStore, LeaseLost, StoreUnavailable  # noqa: E402


class TransactionEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pumpmi-transaction-edge-")
        self.url = f"sqlite:///{Path(self.temp.name) / 'test.sqlite'}"
        self.a, self.b = DatabaseStore(self.url), DatabaseStore(self.url)
        seed = {"version": 1, "revision": 7, "watchPeriods": {"short": 30, "medium": 45, "long": 60}, "max_open": 6}
        self.a.migrate(seed, now=0)
        self.a.initialize_state_if_missing({"events": []}, now=0)

    def tearDown(self) -> None:
        self.a.close()
        self.b.close()
        self.temp.cleanup()

    def test_failed_begin_restores_depth_and_next_transaction(self) -> None:
        self.b._connection_for_transaction().execute("PRAGMA busy_timeout=0")
        self.a._connection_for_transaction().execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with self.b._transaction(immediate=True):
                    self.fail("A locked BEGIN must not enter the body")
            self.assertEqual(self.b._tx_state().depth, 0)
            self.assertFalse(self.b._connection_for_transaction().in_transaction)
        finally:
            self.a._connection_for_transaction().rollback()
        self.b.set_state("events", ["recovered"], now=1)
        self.assertEqual(self.a.get_state("events", fresh=True), ["recovered"])

    def test_failed_savepoint_restores_parent_depth_and_allows_recovery(self) -> None:
        actual_execute = self.a._execute

        def fail_savepoint(cursor: Any, statement: str, parameters: tuple = ()) -> Any:
            if statement.startswith("SAVEPOINT "):
                raise sqlite3.OperationalError("injected savepoint setup failure")
            return actual_execute(cursor, statement, parameters)

        with self.a.transaction():
            self.a.set_state("events", ["before failure"], now=0)
            with patch.object(self.a, "_execute", side_effect=fail_savepoint):
                with self.assertRaises(sqlite3.OperationalError):
                    self.a.get_state("events", fresh=True)
            self.assertEqual(self.a._tx_state().depth, 1)
            self.assertTrue(self.a._connection_for_transaction().in_transaction)
            self.a.set_state("events", ["after recovery"], now=1)
        self.assertEqual(self.a._tx_state().depth, 0)
        self.assertEqual(self.b.get_state("events", fresh=True), ["after recovery"])

    def test_nested_writes_do_not_commit_before_root_rollback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "outer fault"):
            with self.a.transaction():
                self.a.set_state("events", ["must not become durable"], now=1)
                self.assertTrue(self.a._connection_for_transaction().in_transaction)
                self.assertEqual(self.b.get_state("events", fresh=True), [])
                raise RuntimeError("outer fault")
        self.assertEqual(self.a.get_state("events", fresh=True), [])
        self.assertEqual(self.b.get_state("events", fresh=True), [])

    def test_worker_fence_inside_unfenced_root_is_rejected_before_writes(self) -> None:
        self.assertTrue(self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=10, now=0))
        with self.a.transaction():
            with self.assertRaisesRegex(StoreUnavailable, "root transaction"):
                with self.a.lease_fence("optimizer-worker", "worker-a", now=0):
                    self.a.set_state("events", ["bad nesting"], now=0)
            self.assertTrue(self.a._connection_for_transaction().in_transaction)
        self.assertFalse(self.a._connection_for_transaction().in_transaction)
        self.assertTrue(self.b.acquire_lease("optimizer-worker", "worker-b", ttl_seconds=75, now=11))
        with self.assertRaises(LeaseLost):
            with self.a.lease_fence("optimizer-worker", "worker-a", now=11):
                self.a.set_state("events", ["stale writer"], now=11)
        self.assertEqual(self.b.get_state("events", fresh=True), [])

    def test_post_collection_renewal_does_not_reacquire_own_expired_lease(self) -> None:
        self.assertTrue(self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=10, now=0))
        self.assertFalse(self.a.renew_lease("optimizer-worker", "worker-a", ttl_seconds=75, now=11))
        # A new cycle may acquire authority, but the expired cycle must not renew it.
        self.assertTrue(self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=75, now=11))
        self.assertTrue(self.a.renew_lease("optimizer-worker", "worker-a", ttl_seconds=75, now=12))

    def test_post_collection_renewal_does_not_take_over_expired_successor(self) -> None:
        self.assertTrue(self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=10, now=0))
        self.assertTrue(self.b.acquire_lease("optimizer-worker", "worker-b", ttl_seconds=10, now=11))
        self.assertFalse(self.a.renew_lease("optimizer-worker", "worker-a", ttl_seconds=75, now=22))
        with self.b.transaction() as cursor:
            row = self.b._execute(cursor, "SELECT holder FROM optimizer_leases WHERE lease_name=?", ("optimizer-worker",)).fetchone()
        self.assertEqual(row["holder"], "worker-b")

    def test_old_batch_is_discarded_even_after_successor_also_expires(self) -> None:
        clock = [0.0]
        outer = self

        class HandoffSource:
            name = "isolated-no-network-source"
            social = "unavailable"

            def collect(self, _tracked: list[str], *, now: float) -> SourceBatch:
                clock[0] = 11.0
                outer.assertTrue(outer.b.acquire_lease("optimizer-worker", "worker-b", ttl_seconds=10, now=11))
                outer.b.set_state("events", ["successor state"], now=11)
                clock[0] = 22.0
                return SourceBatch([{
                    "mint": "OLD-BATCH", "symbol": "OLD", "name": "test-only",
                    "observed_at": now, "market_cap": 30_000, "curve_pct": 40,
                    "reply_count": 2,
                    "risk": {"status": "known", "top10_pct": 10, "dev_pct": 1, "bundle_pct": 10},
                }], self.name, self.social)

        seed = {"version": 1, "revision": 7, "watchPeriods": {"short": 30, "medium": 45, "long": 60}, "max_open": 6}
        runtime = OptimizerRuntime(self.a, source=HandoffSource(), seed_document=seed, enabled=True, boot_id="worker-a")
        runtime.lease_owned = self.a.acquire_lease("optimizer-worker", "worker-a", ttl_seconds=10, now=0)
        with patch("src.optimizer.runtime.time.time", side_effect=lambda: clock[0]):
            self.assertFalse(runtime.tick())
        self.assertFalse(runtime.lease_owned)
        self.assertEqual(self.b.observations(), [])
        self.assertEqual(self.b.get_state("events", fresh=True), ["successor state"])
        self.assertEqual(self.b.get_state("tracked", fresh=True), {})
        self.assertEqual(len(self.b.get_state("portfolio", fresh=True)["positions"]), 0)

    def test_frame_freshness_clock_is_sampled_after_delayed_reads(self) -> None:
        seed = {"version": 1, "revision": 7, "watchPeriods": {"short": 30, "medium": 45, "long": 60}, "max_open": 6}
        runtime = OptimizerRuntime(self.a, seed_document=seed, enabled=True, boot_id="frame-reader")
        self.a.set_state("source", {"lastObservation": 1004.0, "observations": 1, "error": None})
        clock = [1000.0]
        actual_get_state = self.a.get_state

        def delayed_read(key: str, *args, **kwargs):
            if key == "source":
                clock[0] = 1005.0
            return actual_get_state(key, *args, **kwargs)

        with patch("src.optimizer.runtime.time.time", side_effect=lambda: clock[0]), \
             patch.object(self.a, "get_state", side_effect=delayed_read):
            self.assertEqual(runtime.snapshot_payload()["meta"]["kind"], "live")
            # An explicitly supplied historical reference still rejects future data.
            self.assertEqual(runtime.snapshot_payload(now=1000)["meta"]["kind"], "stale")


if __name__ == "__main__":
    unittest.main(verbosity=2)
