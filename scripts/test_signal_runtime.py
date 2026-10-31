#!/usr/bin/env python3
"""Isolated runtime diagnostics tests; no environment DB, RPC, or worker is used."""
from __future__ import annotations

from copy import deepcopy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from src.optimizer.policy import make_watch_state  # noqa: E402
from src.optimizer.runtime import OptimizerRuntime, WATCH_EXPIRY_GRACE_SECONDS  # noqa: E402
from src.optimizer.source import SourceBatch  # noqa: E402
from src.optimizer.store import DatabaseStore  # noqa: E402


def document() -> dict:
    return {"version": 1, "revision": 7, "watchPeriods": {"short": 30, "medium": 45, "long": 60}, "max_open": 6}


def observation(mint: str, at: float, *, curve: float = 40.0, mcap: float = 30_000.0, curve_known: bool = True) -> dict:
    return {
        "mint": mint,
        "symbol": mint[:8],
        "name": f"Mock {mint[:8]}",
        "observed_at": at,
        "market_cap": mcap,
        "curve_pct": curve if curve_known else 0.0,
        "curve_known": curve_known,
        "reply_count": 2,
        "risk": {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0},
        "source": "isolated-test-only",
    }


class Batches:
    name = "isolated-signal-runtime"
    social = "unavailable (isolated test)"

    def __init__(self, rows: list[list[dict]] | None = None, *, error: str | None = None, preserve_times: bool = False) -> None:
        self.rows = rows or []
        self.error = error
        self.index = 0
        self.preserve_times = preserve_times

    def collect(self, _tracked: list[str], *, now: float | None) -> SourceBatch:
        if self.error:
            return SourceBatch([], self.name, self.social, self.error)
        selected = self.rows[min(self.index, len(self.rows) - 1)] if self.rows else []
        self.index += 1
        if self.preserve_times:
            return SourceBatch(deepcopy(selected), self.name, self.social)
        return SourceBatch([{**row, "observed_at": now} for row in selected], self.name, self.social)


class SignalRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DatabaseStore("sqlite:///:memory:")

    def tearDown(self) -> None:
        self.store.close()

    def runtime(self, source: Batches, *, boot: str = "signal-runtime") -> OptimizerRuntime:
        return OptimizerRuntime(self.store, source=source, seed_document=document(), enabled=True, boot_id=boot)

    def own(self, runtime: OptimizerRuntime, now: float) -> None:
        runtime.lease_owned = self.store.acquire_lease("optimizer-worker", runtime.boot_id, ttl_seconds=75, now=now)
        self.assertTrue(runtime.lease_owned)

    def test_unadmitted_unknown_curve_screen_is_retained_and_public(self) -> None:
        runtime = self.runtime(Batches([[observation("SCREENED", 0, curve_known=False)]]))
        self.own(runtime, 0)
        self.assertTrue(runtime.tick(now=1))
        self.assertEqual(self.store.get_state("tracked", fresh=True), {}, "unknown reserve sample must not be admitted as a watch")
        analysis = runtime.status(now=1)["signalAnalysis"]
        self.assertEqual(analysis["model"], "gate-readiness-v1")
        self.assertFalse(analysis["calibrated"])
        candidate = next(item for item in analysis["candidates"] if item["mint"] == "SCREENED")
        self.assertEqual(candidate["side"], "buy")
        self.assertEqual(candidate["status"], "blocked")
        self.assertTrue(any(gate["key"] == "curve" and gate["state"] == "unknown" for gate in candidate["gates"]))
        self.assertTrue(any(event["action"] == "BLOCK" for event in analysis["events"]))

    def test_real_buy_is_recorded_but_diagnostic_ready_is_not_an_execution_claim(self) -> None:
        rows = [
            [observation("BUYME", 0, curve=40.0, mcap=28_000)],
            [observation("BUYME", 0, curve=40.1, mcap=28_200)],
            [observation("BUYME", 0, curve=40.5, mcap=28_500)],
            [observation("BUYME", 0, curve=41.2, mcap=30_000)],
        ]
        runtime = self.runtime(Batches(rows))
        self.own(runtime, 0)
        for stamp in (0, 30, 45, 60):
            self.assertTrue(runtime.tick(now=stamp))
        state = self.store.get_state("signal_analysis", fresh=True)
        buys = [event for event in state["events"] if event["action"] == "BUY" and event["mint"] == "BUYME"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["readinessPct"], 100.0)
        self.assertIn("paper position", buys[0]["reason"])
        # A held token has only a sell diagnostic; the earlier buy event is history.
        public = runtime.status(now=60)["signalAnalysis"]
        self.assertFalse(any(item["mint"] == "BUYME" and item["side"] == "buy" for item in public["candidates"]))

    def test_expiry_and_source_failure_are_honest_and_lease_loss_rolls_back_records(self) -> None:
        runtime = self.runtime(Batches(error="isolated source failure"))
        pending = {
            "mint": "EXPIRE", "symbol": "EXPIRE", "name": "Expired test",
            **make_watch_state(document()["watchPeriods"], started_at=0, start_curve=40.0),
            "entry_decided": False, "reason": "watching", "last_seen": 0,
        }
        self.store.set_state("tracked", {"EXPIRE": pending})
        due = 60 + WATCH_EXPIRY_GRACE_SECONDS + 1
        self.own(runtime, due)
        self.assertTrue(runtime.tick(now=due))
        failed_status = runtime.status(now=due)
        self.assertIn("isolated source failure", failed_status["source"]["error"])
        self.assertTrue(any(event["action"] == "EXPIRE" for event in failed_status["signalAnalysis"]["events"]))

        rollback_store = DatabaseStore("sqlite:///:memory:")
        self.addCleanup(rollback_store.close)
        rollback_runtime = OptimizerRuntime(
            rollback_store,
            source=Batches([[observation("ROLLBACK", 1)]], preserve_times=True),
            seed_document=document(), enabled=True, boot_id="rollback-signal",
        )
        rollback_runtime.lease_owned = rollback_store.acquire_lease("optimizer-worker", rollback_runtime.boot_id, ttl_seconds=75, now=0)
        self.assertTrue(rollback_runtime.lease_owned)
        # renew, initial fence assertion, mutation clock, final fence assertion
        with patch("src.optimizer.runtime.time.time", side_effect=[0.0, 1.0, 1.0, 76.0]):
            self.assertFalse(rollback_runtime.tick())
        self.assertEqual(rollback_store.observations(), [])
        after = rollback_store.get_state("signal_analysis", fresh=True)
        self.assertEqual(after["candidates"], [])
        self.assertEqual(after["events"], [])

    def test_status_freshness_uses_response_clock_after_delayed_reads(self) -> None:
        runtime = self.runtime(Batches([[observation("NEWQUOTE", 0, curve_known=False)]]))
        self.own(runtime, 1005)
        self.assertTrue(runtime.tick(now=1005))
        get_state = self.store.get_state
        clock = [1000.0]

        def delayed_read(key: str, *args, **kwargs):
            result = get_state(key, *args, **kwargs)
            if key == "signal_analysis":
                clock[0] = 1005.0
            return result

        with patch.object(self.store, "get_state", side_effect=delayed_read):
            with patch("src.optimizer.runtime.time.time", side_effect=lambda: clock[0]):
                candidate = runtime.status()["signalAnalysis"]["candidates"][0]
        self.assertIsNotNone(candidate["readinessPct"])
        self.assertNotEqual(candidate["status"], "stale")
        # An intentionally earlier explicit clock still sees the quote as future.
        self.assertIsNone(runtime.status(now=1000)["signalAnalysis"]["candidates"][0]["readinessPct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
