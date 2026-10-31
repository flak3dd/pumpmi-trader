#!/usr/bin/env python3
"""Isolated signal-analysis tests; never read DATABASE_URL or contact a provider."""
from __future__ import annotations

from copy import deepcopy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from src.optimizer.policy import (  # noqa: E402
    apply_observation,
    entry_reason,
    initial_portfolio,
    make_watch_state,
    open_position,
    record_watch_observation,
)
from src.optimizer.signals import (  # noqa: E402
    MAX_CANDIDATES,
    MAX_EVENTS,
    build_signal_analysis,
    buy_candidate,
    empty_signal_analysis,
    public_signal_analysis,
    record_operational_event,
    record_signal_event,
    sell_candidate,
)

PERIODS = {"short": 30, "medium": 45, "long": 60}
KNOWN_RISK = {"status": "known", "top10_pct": 10.0, "dev_pct": 1.0, "bundle_pct": 10.0}


def observation(mint: str, at: float, *, mcap: float = 30_000.0, curve: float = 40.0, risk: dict | None = None, replies: float | None = 2.0, curve_known: bool = True) -> dict:
    return {
        "mint": mint,
        "symbol": mint[:8],
        "name": f"Mock {mint[:8]}",
        "observed_at": at,
        "market_cap": mcap,
        "curve_pct": curve,
        "curve_known": curve_known,
        "reply_count": replies,
        "risk": deepcopy(KNOWN_RISK if risk is None else risk),
        "source": "isolated-test-only",
    }


def ready_path(mint: str = "READY") -> tuple[dict, dict]:
    first = observation(mint, 0, mcap=28_000, curve=40.0)
    watch = make_watch_state(PERIODS, started_at=0, start_curve=40.0)
    last = first
    # Distinct actual capture times have rising incremental curve point rates.
    for stamp, curve, mcap in ((30, 40.1, 28_200), (45, 40.5, 28_500), (60, 41.2, 30_000)):
        last = observation(mint, stamp, mcap=mcap, curve=curve)
        record_watch_observation(watch, last)
    return watch, last


class SignalAnalysisTests(unittest.TestCase):
    def test_score_bounds_and_all_required_buy_gate_facts(self) -> None:
        watch, current = ready_path()
        candidate = buy_candidate(current, watch, initial_portfolio(), strategy_revision=7, now=60)
        self.assertEqual(candidate["status"], "ready")
        self.assertEqual(candidate["readinessPct"], 100.0)
        required = {"quote", "curve", "mcap", "top10", "developer", "bundle", "checkpoints", "curve_rate", "flow", "impact", "slots", "cash", "policy"}
        self.assertTrue(required.issubset({gate["key"] for gate in candidate["gates"]}))
        for gate in candidate["gates"]:
            self.assertIn(gate["state"], {"pass", "fail", "unknown", "pending"})
            self.assertGreaterEqual(gate["progressPct"], 0.0)
            self.assertLessEqual(gate["progressPct"], 100.0)

    def test_full_ready_is_equivalent_to_copied_authoritative_entry_and_open(self) -> None:
        watch, current = ready_path("EQUIV")
        portfolio = initial_portfolio()
        before_portfolio = deepcopy(portfolio)
        before_watch = deepcopy(watch)
        candidate = buy_candidate(current, watch, portfolio, strategy_revision=7, now=60)
        self.assertEqual(candidate["readinessPct"], 100.0)
        self.assertIsNone(entry_reason(deepcopy(current), deepcopy(watch)))
        preview = deepcopy(portfolio)
        self.assertIsNotNone(open_position(preview, deepcopy(current), strategy_revision=7, watch=deepcopy(watch)))
        self.assertEqual(portfolio, before_portfolio)
        self.assertEqual(watch, before_watch)

    def test_incomplete_and_non_distinct_checkpoints_cannot_be_ready(self) -> None:
        first = observation("ONE", 0, curve=40.0)
        incomplete = make_watch_state(PERIODS, started_at=0, start_curve=40.0)
        pending = buy_candidate(first, incomplete, initial_portfolio(), strategy_revision=7, now=0)
        self.assertNotEqual(pending["status"], "ready")
        self.assertEqual(next(gate for gate in pending["gates"] if gate["key"] == "checkpoints")["state"], "pending")

        duplicate = make_watch_state(PERIODS, started_at=0, start_curve=40.0)
        late = observation("ONE", 60, curve=41.5)
        record_watch_observation(duplicate, late)  # One real sample fills all due periods.
        blocked = buy_candidate(late, duplicate, initial_portfolio(), strategy_revision=7, now=60)
        checkpoint = next(gate for gate in blocked["gates"] if gate["key"] == "checkpoints")
        self.assertEqual(checkpoint["state"], "fail")
        self.assertNotEqual(blocked["readinessPct"], 100.0)

    def test_unknown_and_stale_quotes_fail_closed_with_null_scores(self) -> None:
        watch, current = ready_path("UNKNOWN")
        current["curve_known"] = False
        current["risk"] = {"status": "unknown", "reason": "https://private-provider.example/key"}
        unknown = buy_candidate(current, watch, initial_portfolio(), strategy_revision=7, now=60)
        self.assertEqual(unknown["status"], "blocked")
        self.assertNotEqual(unknown["readinessPct"], 100.0)
        self.assertTrue(any(gate["state"] == "unknown" for gate in unknown["gates"]))
        self.assertNotIn("private-provider", unknown["reason"])

        stale = public_signal_analysis({**empty_signal_analysis(), "candidates": [unknown]}, now=181)["candidates"][0]
        self.assertEqual(stale["status"], "stale")
        self.assertIsNone(stale["readinessPct"])
        unavailable = buy_candidate({"mint": "BAD", "symbol": "BAD", "observed_at": 60, "market_cap": 0}, None, initial_portfolio(), strategy_revision=7, now=60)
        self.assertIsNone(unavailable["readinessPct"])

    def test_six_cap_and_cash_are_real_blockers(self) -> None:
        watch, current = ready_path("CAP")
        capped = initial_portfolio()
        for index in range(6):
            self.assertIsNotNone(open_position(capped, observation(f"HELD{index}", 0), strategy_revision=7))
        cap_candidate = buy_candidate(current, watch, capped, strategy_revision=7, now=60)
        self.assertEqual(next(gate for gate in cap_candidate["gates"] if gate["key"] == "slots")["state"], "fail")
        self.assertNotEqual(cap_candidate["readinessPct"], 100.0)

        no_cash = initial_portfolio()
        no_cash["cash"] = 0.0
        cash_candidate = buy_candidate(current, watch, no_cash, strategy_revision=7, now=60)
        self.assertEqual(next(gate for gate in cash_candidate["gates"] if gate["key"] == "cash")["state"], "fail")
        self.assertNotEqual(cash_candidate["readinessPct"], 100.0)

    def test_fee_aware_sell_triggers_partial_exit_and_no_phantom_sell(self) -> None:
        portfolio = initial_portfolio()
        entered = open_position(portfolio, observation("EXIT", 0, mcap=30_000, curve=40), strategy_revision=7)
        assert entered is not None
        before_position = deepcopy(entered)
        stop_quote = observation("EXIT", 30, mcap=26_000, curve=42)
        stopped = sell_candidate(entered, stop_quote, now=30)
        self.assertEqual(stopped["status"], "ready")
        self.assertEqual(stopped["readinessPct"], 100.0)
        self.assertIn("net", next(gate for gate in stopped["gates"] if gate["key"] == "stop")["actual"])
        self.assertEqual(entered, before_position, "diagnostics must not update the live position peak")

        scale_quote = observation("EXIT", 31, mcap=39_000, curve=43)
        scaled_candidate = sell_candidate(entered, scale_quote, now=31)
        self.assertEqual(scaled_candidate["status"], "ready")
        self.assertIn("scale", scaled_candidate["reason"])
        partial = apply_observation(portfolio, entered["id"], deepcopy(scale_quote))
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial["status"], "scaled")
        remaining = portfolio["positions"][entered["id"]]
        after_scale = sell_candidate(remaining, observation("EXIT", 32, mcap=39_000, curve=43), now=32)
        self.assertNotEqual(after_scale["status"], "ready", "a partial exit must not invent a second scale exit")

        no_holdings = build_signal_analysis(
            empty_signal_analysis(), tracked={}, portfolio=initial_portfolio(),
            observations=[observation("SCREEN", 10)], strategy_revision=7, now=10, operational_mints=["SCREEN"],
        )
        self.assertTrue(no_holdings["candidates"])
        self.assertFalse(any(item["side"] == "sell" for item in no_holdings["candidates"]))

    def test_bounded_operational_dedup_and_candidate_history(self) -> None:
        candidate = {
            "mint": "DEDUP", "symbol": "DEDUP", "side": "buy", "readinessPct": 12.0,
            "status": "waiting", "observedAt": 1.0, "reason": "actual checkpoint pending", "gates": [],
        }
        events: list[dict] = []
        events = record_operational_event(events, candidate)
        events = record_operational_event(events, candidate)
        self.assertEqual(len(events), 1)
        changed = {**candidate, "readinessPct": 25.0}
        events = record_operational_event(events, changed)
        self.assertEqual(len(events), 2)
        for index in range(MAX_EVENTS + 20):
            events = record_signal_event(events, at=float(index + 2), mint=f"M{index}", symbol="M", side="buy", action="SKIP", readiness_pct=None, reason="actual policy decision")
        self.assertEqual(len(events), MAX_EVENTS)

        observations = [observation(f"TOKEN-{index}", 100 + index) for index in range(MAX_CANDIDATES + 12)]
        analysis = build_signal_analysis(empty_signal_analysis(), tracked={}, portfolio=initial_portfolio(), observations=observations, strategy_revision=7, now=200, operational_mints=[])
        self.assertLessEqual(len(analysis["candidates"]), MAX_CANDIDATES)
        self.assertLessEqual(len(analysis["events"]), MAX_EVENTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
