#!/usr/bin/env python3
"""Isolated liveness regressions for the durable paper-only optimizer.

Every test uses in-memory SQLite plus injected sources/RPC stubs. It never calls
``from_env``, starts a worker, or contacts a production database/provider.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from src.optimizer.policy import initial_portfolio, make_watch_state, open_position, record_watch_observation  # noqa: E402
from src.optimizer.runtime import (  # noqa: E402
    MAX_TRACKED,
    WATCH_EXPIRY_GRACE_SECONDS,
    OptimizerRuntime,
)
from src.optimizer.source import HolderRiskCache, PumpFunSource, SourceBatch  # noqa: E402
from src.optimizer.store import DatabaseStore  # noqa: E402


PERIODS = {"short": 30, "medium": 45, "long": 60}


def document(revision: int = 7, periods: dict | None = None) -> dict:
    return {"version": 1, "revision": revision, "watchPeriods": periods or PERIODS, "max_open": 6}


def observation(mint: str, at: float, *, curve_known: bool = True) -> dict:
    return {
        "mint": mint,
        "symbol": mint[:8],
        "name": f"Mock {mint[:8]}",
        "observed_at": at,
        "market_cap": 30_000.0,
        "curve_pct": 40.0 if curve_known else 0.0,
        "curve_known": curve_known,
        "reply_count": 2,
        "risk": {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0},
        "social": {"status": "unavailable", "sentiment": None, "source": "isolated-test"},
        "source": "isolated-test-only",
    }


def pending_track(
    mint: str,
    *,
    started: float = 0.0,
    durations: dict | None = None,
    risk: dict | None = None,
    curve_known: bool = True,
) -> dict:
    return {
        "mint": mint,
        "symbol": mint[:8],
        "name": f"Watch {mint[:8]}",
        **make_watch_state(durations or PERIODS, started_at=started, start_curve=40.0),
        "entry_decided": False,
        "reason": "watching actual market-cap path",
        "last_seen": started,
        "last_mcap": 30_000.0,
        "last_curve": 40.0,
        "curve_known": curve_known,
        "risk": risk if risk is not None else {"status": "unknown", "reason": "test holder unavailable"},
    }


class BatchSource:
    name = "isolated-liveness-source"
    social = "unavailable (isolated liveness test)"

    def __init__(self, *, rows: list[dict] | None = None, error: str | None = None) -> None:
        self.rows = rows or []
        self.error = error

    def collect(self, _tracked: list[str], *, now: float) -> SourceBatch:
        if self.error:
            return SourceBatch([], self.name, self.social, self.error)
        return SourceBatch([{**row, "observed_at": now} for row in self.rows], self.name, self.social)


class WatchLivenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DatabaseStore("sqlite:///:memory:")

    def runtime(self, source: BatchSource | None = None, *, boot: str = "liveness") -> OptimizerRuntime:
        return OptimizerRuntime(self.store, source=source or BatchSource(), seed_document=document(), enabled=True, boot_id=boot)

    def own(self, runtime: OptimizerRuntime, now: float) -> None:
        runtime.lease_owned = self.store.acquire_lease("optimizer-worker", runtime.boot_id, ttl_seconds=75, now=now)
        self.assertTrue(runtime.lease_owned)

    def test_expiry_without_observations_is_single_skip_without_trade_or_pnl(self) -> None:
        runtime = self.runtime(BatchSource())
        self.store.set_state("tracked", {"STALE": pending_track("STALE")})
        due = PERIODS["long"] + WATCH_EXPIRY_GRACE_SECONDS + 1
        self.own(runtime, due)
        self.assertTrue(runtime.tick(now=due))

        track = self.store.get_state("tracked", fresh=True)["STALE"]
        stats = self.store.get_state("board_stats", fresh=True)
        portfolio = self.store.get_state("portfolio", fresh=True)
        done = self.store.get_state("done_cards", fresh=True)
        self.assertTrue(track["entry_decided"])
        self.assertIn("watch expired", track["reason"])
        self.assertEqual(stats["expiredWatches"], 1)
        self.assertEqual(stats["skips"], 1)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["verdict"], "SKIP")
        self.assertEqual(self.store.observations(), [])
        self.assertEqual(self.store.closed_trades(), [])
        self.assertEqual(portfolio["closed_trades"], 0)
        self.assertEqual(portfolio["net_realized"], 0.0)
        self.assertEqual(runtime.snapshot_payload(now=due)["board"]["stats"]["expiredWatches"], 1)

        self.assertTrue(runtime.tick(now=due + 1))
        self.assertEqual(self.store.get_state("board_stats", fresh=True)["expiredWatches"], 1)
        self.assertEqual(len(self.store.get_state("done_cards", fresh=True)), 1)

    def test_expiry_with_unknown_reserve_observation_preserves_anchor_and_samples(self) -> None:
        runtime = self.runtime(BatchSource(rows=[observation("UNKNOWN", 0, curve_known=False)]))
        self.store.set_state("tracked", {"UNKNOWN": pending_track("UNKNOWN")})
        due = PERIODS["long"] + WATCH_EXPIRY_GRACE_SECONDS + 1
        self.own(runtime, due)
        self.assertTrue(runtime.tick(now=due))

        track = self.store.get_state("tracked", fresh=True)["UNKNOWN"]
        self.assertTrue(track["entry_decided"])
        self.assertFalse(track["curve_known"])
        self.assertEqual(track["last_curve"], 40.0, "unknown reserve placeholder must not replace anchor")
        self.assertEqual(track["watch_samples"], {}, "expiry must not invent checkpoints")
        self.assertEqual(len(self.store.observations()), 1, "actual unknown-reserve price remains a recorded observation")
        self.assertEqual(self.store.get_state("board_stats", fresh=True)["expiredWatches"], 1)
        self.assertEqual(self.store.closed_trades(), [])

    def test_expiry_on_source_failure_is_durable_and_never_creates_a_trade(self) -> None:
        runtime = self.runtime(BatchSource(error="holder RPC unavailable (OSError)"))
        self.store.set_state("tracked", {"FAILED": pending_track("FAILED")})
        due = PERIODS["long"] + WATCH_EXPIRY_GRACE_SECONDS + 1
        self.own(runtime, due)
        self.assertTrue(runtime.tick(now=due))

        self.assertTrue(self.store.get_state("tracked", fresh=True)["FAILED"]["entry_decided"])
        self.assertEqual(self.store.get_state("board_stats", fresh=True)["expiredWatches"], 1)
        self.assertEqual(self.store.get_state("done_cards", fresh=True)[0]["verdict"], "SKIP")
        self.assertEqual(self.store.closed_trades(), [])
        self.assertEqual(self.store.get_state("portfolio", fresh=True)["net_realized"], 0.0)
        self.assertIn("holder RPC unavailable", runtime.status(now=due)["source"]["error"])

    def test_held_path_is_never_expired(self) -> None:
        runtime = self.runtime(BatchSource())
        portfolio = initial_portfolio()
        position = open_position(portfolio, observation("HELD", 0), strategy_revision=7)
        assert position is not None
        self.store.set_many_state({"portfolio": portfolio, "tracked": {"HELD": pending_track("HELD")}})
        due = PERIODS["long"] + WATCH_EXPIRY_GRACE_SECONDS + 1
        self.own(runtime, due)
        self.assertTrue(runtime.tick(now=due))

        self.assertFalse(self.store.get_state("tracked", fresh=True)["HELD"]["entry_decided"])
        self.assertEqual(self.store.get_state("board_stats", fresh=True)["expiredWatches"], 0)
        self.assertEqual(self.store.get_state("done_cards", fresh=True), [])
        self.assertIn(position["id"], self.store.get_state("portfolio", fresh=True)["positions"])

    def test_expiry_uses_captured_duration_after_config_changes(self) -> None:
        runtime = self.runtime(BatchSource())
        self.store.set_state("tracked", {"CAPTURED": pending_track("CAPTURED", durations=PERIODS)})
        runtime.update_config({"revision": 7, "watchPeriods": {"short": 30, "medium": 60, "long": 75}})
        # 181 is after this path's captured 60+120 deadline and before the new
        # configuration's hypothetical 75+120 deadline.
        self.own(runtime, 181)
        self.assertTrue(runtime.tick(now=181))
        self.assertTrue(self.store.get_state("tracked", fresh=True)["CAPTURED"]["entry_decided"])
        self.assertEqual(runtime.config()["watchPeriods"]["long"], 75)

    def test_expired_decisions_free_an_admission_slot_at_capacity(self) -> None:
        runtime = self.runtime(BatchSource(rows=[observation("ADMITTED", 0)]))
        tracked = {f"STALE-{index}": pending_track(f"STALE-{index}") for index in range(MAX_TRACKED)}
        self.store.set_state("tracked", tracked)
        due = PERIODS["long"] + WATCH_EXPIRY_GRACE_SECONDS + 1
        self.own(runtime, due)
        self.assertTrue(runtime.tick(now=due))

        after = self.store.get_state("tracked", fresh=True)
        self.assertEqual(len(after), MAX_TRACKED)
        self.assertIn("ADMITTED", after)
        self.assertEqual(self.store.get_state("board_stats", fresh=True)["expiredWatches"], MAX_TRACKED)
        self.assertTrue(any(mint.startswith("STALE-") and mint not in after for mint in tracked))
        self.assertEqual(self.store.closed_trades(), [])

    def test_lease_loss_rolls_back_expiry_decision_and_counter(self) -> None:
        runtime = self.runtime(BatchSource())
        self.store.set_state("tracked", {"ROLLBACK": pending_track("ROLLBACK")})
        due = PERIODS["long"] + WATCH_EXPIRY_GRACE_SECONDS + 1
        self.own(runtime, due)
        # tick() uses the wall clock for both fence checks when `now` is omitted.
        # The final 257 exceeds the lease renewed at 181 plus its 75-second TTL.
        with patch("src.optimizer.runtime.time.time", side_effect=[181.0, 181.0, 181.0, 257.0]):
            self.assertFalse(runtime.tick())
        self.assertFalse(runtime.lease_owned)
        self.assertFalse(self.store.get_state("tracked", fresh=True)["ROLLBACK"]["entry_decided"])
        self.assertEqual(self.store.get_state("board_stats", fresh=True)["expiredWatches"], 0)
        self.assertEqual(self.store.get_state("done_cards", fresh=True), [])

    def test_collection_crossing_deadline_expires_before_a_stale_row_can_enter(self) -> None:
        """The post-collection fence clock, not tick start, owns expiry decisions."""
        known = {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0}
        track = pending_track("CROSSED", risk=known)
        # These two real prior samples, plus the delayed third, would clear all
        # checkpoint/flow/risk gates if the old 170 tick-start timestamp were used.
        for timestamp, curve in ((30.0, 40.3), (45.0, 41.2)):
            prior = observation("CROSSED", timestamp)
            prior["curve_pct"] = curve
            record_watch_observation(track, prior)
        track["last_seen"] = 45.0
        delayed = observation("CROSSED", 170.0)
        delayed["curve_pct"] = 50.0
        clock = [170.0]
        case = self

        class DeadlineCrossingSource:
            name = "deadline-crossing-source"
            social = "unavailable (isolated deadline test)"

            def collect(self, _tracked: list[str], *, now: float | None = None) -> SourceBatch:
                case.assertIsNone(now, "production must not pass tick-start time into a real source")
                clock[0] = 220.0  # Captured long 60s + 120s grace deadline was 180.
                return SourceBatch([delayed], self.name, self.social)

        runtime = self.runtime(DeadlineCrossingSource())  # type: ignore[arg-type]
        self.store.set_state("tracked", {"CROSSED": track})
        self.own(runtime, 170.0)
        with patch("src.optimizer.runtime.time.time", side_effect=lambda: clock[0]):
            self.assertTrue(runtime.tick())

        after_track = self.store.get_state("tracked", fresh=True)["CROSSED"]
        stats = self.store.get_state("board_stats", fresh=True)
        portfolio = self.store.get_state("portfolio", fresh=True)
        self.assertTrue(after_track["entry_decided"])
        self.assertIn("watch expired", after_track["reason"])
        self.assertEqual(stats["expiredWatches"], 1)
        self.assertEqual(stats["buys"], 0)
        self.assertEqual(self.store.get_state("done_cards", fresh=True)[0]["verdict"], "SKIP")
        self.assertEqual(self.store.closed_trades(), [])
        self.assertEqual(portfolio["positions"], {})
        self.assertEqual(portfolio["net_realized"], 0.0)
        # Accepted source data retains its sample timestamp rather than advertising
        # either the old tick start or the later mutation clock as observation time.
        self.assertEqual(self.store.get_state("source", fresh=True)["lastObservation"], 170.0)

    def test_entry_readiness_counts_nonheld_pending_paths_with_exact_contract(self) -> None:
        runtime = self.runtime(BatchSource())
        known = {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0}
        portfolio = initial_portfolio()
        held = open_position(portfolio, observation("HELD-READINESS", 0), strategy_revision=7)
        assert held is not None
        self.store.set_many_state({
            "portfolio": portfolio,
            "tracked": {
                "HOLDER": pending_track("HOLDER", started=100, risk={"status": "unknown", "reason": "backoff"}),
                "CURVE": pending_track("CURVE", started=100, risk=known, curve_known=False),
                "OVERDUE": pending_track("OVERDUE", started=0, risk=known),
                "HELD-READINESS": pending_track("HELD-READINESS", started=0, risk={"status": "unknown"}, curve_known=False),
            },
            "board_stats": {"ticks": 0, "scanned": 0, "buys": 0, "wins": 0, "losses": 0, "skips": 0, "expiredWatches": 7, "realized": 0.0},
        })
        readiness = runtime.status(now=181)["entryReadiness"]
        self.assertEqual(set(readiness), {"pending", "holderUnknown", "curveUnknown", "overdue", "expiredTotal", "blocker"})
        self.assertEqual(readiness, {
            "pending": 3,
            "holderUnknown": 1,
            "curveUnknown": 1,
            "overdue": 1,
            "expiredTotal": 7,
            "blocker": "holder risk data blocked",
        })

        state = self.store.get_state("tracked", fresh=True)
        state["HOLDER"]["risk"] = known
        self.store.set_state("tracked", state)
        self.assertEqual(runtime.status(now=181)["entryReadiness"]["blocker"], "reserve data blocked")
        state = self.store.get_state("tracked", fresh=True)
        state["CURVE"]["curve_known"] = True
        self.store.set_state("tracked", state)
        self.assertEqual(runtime.status(now=181)["entryReadiness"]["blocker"], "watch awaiting checkpoints")


class SourceBoundTests(unittest.TestCase):
    def test_real_source_uses_response_receipts_or_the_provided_explicit_clock(self) -> None:
        import base64
        import struct

        from src.curve import INITIAL_REAL_TOKEN_RESERVES

        coin = {
            "mint": "RECEIPT", "symbol": "RECEIPT", "name": "Receipt test",
            "usd_market_cap": 30_000, "bonding_curve": "curve-receipt", "creator": "creator",
        }
        account = base64.b64encode(
            b"\x00" * 8 + struct.pack(
                "<QQQQ", 1_000_000_000_000_000, 30_000_000_000,
                int(INITIAL_REAL_TOKEN_RESERVES * 0.6), 1,
            )
        ).decode("ascii")

        class CachedHolders:
            def __init__(self) -> None:
                self.cache = {
                    "RECEIPT": (9999.0, {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0}),
                }

        clock = [100.0]

        def http_get(_url: str) -> list[dict]:
            clock[0] = 110.0
            return [coin]

        def reserve_rpc(payload: dict) -> dict:
            self.assertEqual(payload["method"], "getMultipleAccounts")
            clock[0] = 120.0
            return {"jsonrpc": "2.0", "result": {"value": [{"data": [account, "base64"]}]}}

        source = PumpFunSource(http_get=http_get, rpc=reserve_rpc, holder_cache=CachedHolders())  # type: ignore[arg-type]
        with patch("src.optimizer.source.time.time", side_effect=lambda: clock[0]):
            live = source.collect([])
        self.assertIsNone(live.error)
        self.assertEqual(live.observations[0]["observed_at"], 120.0, "compound reserve data cannot predate its receipt")

        explicit = PumpFunSource(http_get=lambda _url: [coin], rpc=lambda _payload: {
            "jsonrpc": "2.0", "result": {"value": [{"data": [account, "base64"]}]},
        }, holder_cache=CachedHolders())  # type: ignore[arg-type]
        with patch("src.optimizer.source.time.time", side_effect=AssertionError("explicit test clock must avoid wall time")):
            deterministic = explicit.collect([], now=7.0)
        self.assertIsNone(deterministic.error)
        self.assertEqual(deterministic.observations[0]["observed_at"], 7.0)

    def test_failed_holder_risk_cache_uses_short_backoff_not_success_ttl(self) -> None:
        failed = HolderRiskCache(
            rpc=lambda _payload: {"jsonrpc": "2.0", "error": {"message": "https://private.example/key=secret"}},
            ttl_seconds=300,
            failure_backoff_seconds=11,
        )
        unknown = failed.get({"mint": "BAD", "creator": "C", "associated_bonding_curve": "curve"}, now=10)
        self.assertEqual(unknown["status"], "unknown")
        self.assertNotIn("secret", unknown["reason"])
        self.assertEqual(failed.cache["BAD"][0], 21)

        def success_rpc(payload: dict) -> dict:
            method = payload["method"]
            if method == "getTokenLargestAccounts":
                return {"jsonrpc": "2.0", "result": {"value": [
                    {"address": "curve", "uiAmount": 70.0}, {"address": "holder", "uiAmount": 30.0},
                ]}}
            if method == "getTokenSupply":
                return {"jsonrpc": "2.0", "result": {"value": {"uiAmount": 100.0}}}
            return {"jsonrpc": "2.0", "result": {"value": []}}

        positive = HolderRiskCache(rpc=success_rpc, ttl_seconds=300, failure_backoff_seconds=11)
        known = positive.get({"mint": "GOOD", "creator": "C", "associated_bonding_curve": "curve"}, now=10)
        self.assertEqual(known["status"], "known")
        self.assertEqual(positive.cache["GOOD"][0], 310)

    def test_fair_holder_lanes_keep_hard_two_check_eight_detail_and_one_reserve_batch_limits(self) -> None:
        def coin(mint: str) -> dict:
            return {
                "mint": mint, "symbol": mint, "name": mint, "usd_market_cap": 30_000,
                "bonding_curve": f"curve-{mint}", "creator": "creator", "reply_count": 2,
            }

        listings = [coin(f"LIST-{index}") for index in range(3)]
        details = {f"TRACK-{index}": coin(f"TRACK-{index}") for index in range(9)}
        detail_calls: list[str] = []
        reserve_calls: list[dict] = []

        def http_get(url: str):
            if "/coins-v2/" in url:
                mint = url.rsplit("/", 1)[-1]
                detail_calls.append(mint)
                return details[mint]
            return listings

        def reserve_rpc(payload: dict) -> dict:
            reserve_calls.append(payload)
            self.assertEqual(payload["method"], "getMultipleAccounts")
            return {"jsonrpc": "2.0", "result": {"value": [None] * len(payload["params"][0])}}

        class RecordingHolders:
            def __init__(self) -> None:
                self.cache: dict = {}
                self.calls: list[str] = []

            def get(self, item: dict, _now: float) -> dict:
                self.calls.append(str(item["mint"]))
                return {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0, "reason": None}

        holders = RecordingHolders()
        source = PumpFunSource(
            http_get=http_get,
            rpc=reserve_rpc,
            holder_cache=holders,  # type: ignore[arg-type]
            max_tracked_refresh=99,
            max_holder_checks=99,
        )
        batch = source.collect(list(details), now=1)

        self.assertIsNone(batch.error)
        self.assertEqual(source.max_holder_checks, 2)
        self.assertEqual(source.max_tracked_refresh, 8)
        self.assertEqual(len(holders.calls), 2)
        self.assertIn("LIST-0", holders.calls)
        self.assertIn("TRACK-0", holders.calls, "a refreshed tracked candidate receives a holder slot")
        self.assertEqual(len(detail_calls), 8)
        self.assertEqual(len(reserve_calls), 1)

    def test_all_64_tracks_receive_a_holder_slot_across_repeated_failed_chunks(self) -> None:
        def coin(mint: str) -> dict:
            return {"mint": mint, "symbol": mint, "usd_market_cap": 30_000,
                    "bonding_curve": f"curve-{mint}", "creator": "creator"}

        details = {f"TRACK-{index}": coin(f"TRACK-{index}") for index in range(64)}
        class FailedHolders:
            def __init__(self) -> None:
                self.cache: dict = {}
                self.calls: list[str] = []
            def get(self, item: dict, _now: float) -> dict:
                self.calls.append(str(item["mint"]))
                return {"status": "unknown", "reason": "mock failure"}
        holders = FailedHolders()
        source = PumpFunSource(
            http_get=lambda url: details[url.rsplit("/", 1)[-1]] if "/coins-v2/" in url else [coin("NEW")],
            rpc=lambda payload: {"jsonrpc": "2.0", "result": {"value": [None] * len(payload["params"][0])}},
            holder_cache=holders,  # type: ignore[arg-type]
        )
        for cycle in range(64):
            before = len(holders.calls)
            source.collect(list(details), now=float(cycle * 70))
            self.assertLessEqual(len(holders.calls) - before, 2)
        self.assertTrue(set(details).issubset(set(holders.calls)), "no repeated chunk may starve its later members")
        self.assertLessEqual(len(source._holder_attempts), 65, "scheduler metadata remains bounded")

if __name__ == "__main__":
    unittest.main(verbosity=2)
