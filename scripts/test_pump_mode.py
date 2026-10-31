#!/usr/bin/env python3
"""Isolated pump-capture mode tests. No environment database, RPC, network or worker is used.

A deterministic fake chain serves real byte layouts (bonding-curve and token accounts)
and listing rows; the runtime runs on an in-memory SQLite store.
"""
from __future__ import annotations

import base64
import hashlib
import math
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from src.optimizer import pump  # noqa: E402
from src.optimizer.policy import initial_portfolio  # noqa: E402
from src.optimizer.pump import FallbackRpc, PumpMarket  # noqa: E402
from src.optimizer.runtime import OptimizerRuntime  # noqa: E402
from src.optimizer.signals import build_signal_analysis, empty_signal_analysis  # noqa: E402
from src.optimizer.source import SourceBatch  # noqa: E402
from src.optimizer.store import DatabaseStore  # noqa: E402
from src.solana_pda import TOKEN_2022_PROGRAM_ID, associated_token_address, b58encode  # noqa: E402

SOL_USD = 150.0
SUPPLY = 1_000_000_000_000_000
K = 30_000_000_000 * 1_073_000_000_000_000
OFFSET = 1_073_000_000_000_000 - 793_100_000_000_000
T0 = 1_800_000_000.0


def key(label: str) -> str:
    return b58encode(hashlib.sha256(label.encode()).digest())


def curve_blob(vs: float, *, complete: bool = False) -> list[str]:
    vs_i = int(vs)
    vt_i = int(K / vs)
    rt = vt_i - OFFSET
    raw = b"\x00" * 8 + struct.pack("<QQQQQ", vt_i, vs_i, rt, max(0, vs_i - 30_000_000_000), SUPPLY) + bytes([1 if complete else 0])
    return [base64.b64encode(raw).decode(), "base64"]


def token_blob(amount: int) -> list[str]:
    raw = b"\x00" * 64 + struct.pack("<Q", amount) + b"\x00" * 93
    return [base64.b64encode(raw).decode(), "base64"]


def mcap_usd(vs: float) -> float:
    vt = K / vs
    return vs * SUPPLY / (vt * 1e9) * SOL_USD


class FakeChain:
    """One pump token whose SOL reserve follows a scripted path; creator balance is scriptable."""

    def __init__(self, label: str = "PUMPA", *, created_at: float = T0, program: str | None = TOKEN_2022_PROGRAM_ID,
                 creator_amount: int = 20_000_000_000_000, launch_vs: float = 33e9) -> None:
        self.mint = key(f"mint-{label}")
        self.curve = key(f"curve-{label}")
        self.creator = key(f"creator-{label}")
        self.program = program
        self.ata = associated_token_address(self.creator, self.mint, program) if program else None
        self.symbol = label
        self.created_at = created_at
        self.vs = launch_vs
        self.creator_amount = creator_amount
        self.complete = False
        self.rpc_down = False
        self.listing_down = False
        self.listing_error: str | None = None
        self.blocked_calls = 0

    def listing(self, url: str) -> list[dict]:
        if self.listing_error:
            self.blocked_calls += 1
            raise pump.SourceError(self.listing_error)
        if self.listing_down:
            raise pump.SourceError("listing down (test)")
        mc = mcap_usd(self.vs)
        return [{"mint": self.mint, "symbol": self.symbol, "name": f"Test {self.symbol}", "usd_market_cap": mc,
                 "market_cap": mc / SOL_USD, "bonding_curve": self.curve, "creator": self.creator,
                 "token_program": self.program, "created_timestamp": self.created_at * 1000, "complete": self.complete,
                 "quote_mint": None}]

    def rpc(self, payload: dict) -> dict:
        if self.rpc_down:
            raise pump.SourceError("public RPC HTTP 429")
        values = []
        for address in payload["params"][0]:
            if address == self.curve:
                values.append({"data": curve_blob(self.vs, complete=self.complete), "owner": key("pump-program")})
            elif address == self.ata:
                values.append(None if self.creator_amount is None else {"data": token_blob(self.creator_amount), "owner": self.program})
            else:
                values.append(None)
        return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": values}}

    def market(self) -> PumpMarket:
        return PumpMarket(http_get=self.listing, rpc=FallbackRpc(primary=None, public=self.rpc, clock=lambda: T0))

    def scale_price(self, factor: float) -> None:
        self.vs *= math.sqrt(factor)


class NoLegacySource:
    name = "isolated-pump-test"
    social = "unavailable (isolated test)"

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []

    def collect(self, _tracked: list[str], *, now: float | None) -> SourceBatch:
        return SourceBatch([{**row, "observed_at": now} for row in self.rows], self.name, self.social)


def document() -> dict:
    return {"version": 1, "revision": 3, "watchPeriods": {"short": 30, "medium": 45, "long": 60}, "max_open": 6}


class PumpModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DatabaseStore("sqlite:///:memory:")
        self.boot = "pump-test"

    def tearDown(self) -> None:
        self.store.close()

    def runtime(self, chain: FakeChain, *, mode: str = "paper", variant: dict | None = None) -> OptimizerRuntime:
        rt = OptimizerRuntime(self.store, source=NoLegacySource(), seed_document=document(), enabled=True, boot_id=self.boot,
                              pump_mode=mode, pump_variant=variant or {"entry": "E2", "exit": "X2"}, pump_market=chain.market())
        return rt

    def cycle(self, rt: OptimizerRuntime, at: float) -> bool:
        rt.lease_owned = self.store.acquire_lease("optimizer-worker", self.boot, ttl_seconds=75, now=at)
        return rt.pump_tick(now=at)

    def warm_then_pump(self, rt: OptimizerRuntime, chain: FakeChain, *, flat_cycles: int = 18, factor: float = 1.18) -> float:
        """Flat price long enough for the 180 s E2 window, then one +18% jump. Returns the jump time."""
        at = T0 + 2
        for _ in range(flat_cycles):
            self.cycle(rt, at)
            at += 4
        chain.scale_price(factor)
        self.cycle(rt, at)
        return at

    def state(self) -> dict:
        return self.store.get_state("pump_state", None, fresh=True)

    def portfolio(self) -> dict:
        return self.store.get_state("portfolio", None, fresh=True)

    # ------------------------------------------------------------ maths
    def test_round_trip_costs_fee_and_impact_only(self) -> None:
        vs = 40e9
        vt = K / vs
        tokens = pump.buy_tokens(65.0, vs, vt, SOL_USD)
        vs2 = vs + 65.0 / SOL_USD * 1e9 / (1 + pump.FEE)
        vt2 = K / vs2
        back = pump.sell_value(tokens, vs2, vt2, SOL_USD)
        loss = 1 - back / 65.0
        self.assertGreater(loss, 2 * pump.FEE * 0.98)
        self.assertLess(loss, 0.04)

    # ------------------------------------------------------------ entry and fill
    def test_entry_fills_at_next_verified_read_with_costs(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        jump = self.warm_then_pump(rt, chain)
        state = self.state()
        self.assertEqual([o["kind"] for o in state["pending"]], ["entry"])
        self.assertEqual(self.portfolio()["positions"], {})
        decided_mcap = mcap_usd(chain.vs)
        chain.scale_price(1.03)
        self.cycle(rt, jump + 4)
        portfolio = self.portfolio()
        self.assertEqual(len(portfolio["positions"]), 1)
        position = next(iter(portfolio["positions"].values()))
        self.assertEqual(position["strategy"], pump.PUMP_STRATEGY)
        self.assertAlmostEqual(position["entry_mcap"], mcap_usd(chain.vs), delta=1.0)
        self.assertGreater(position["entry_mcap"], decided_mcap)  # filled at the next read, not the trigger price
        self.assertAlmostEqual(portfolio["cash"], 1500.0 - 65.0 - pump.NETWORK_USD, places=6)
        self.assertAlmostEqual(position["costs"], 65.0 - 65.0 / (1 + pump.FEE) + pump.NETWORK_USD, places=6)
        self.assertEqual(position["pump"]["fill_latency_s"], 4.0)
        self.assertIn("substituted", position["risk"]["holderGate"])
        self.assertEqual(position["pump"]["meta"]["curve"], chain.curve)
        actions = [e["action"] for e in self.state()["events"]]
        self.assertIn("BUY QUEUED", actions)
        self.assertIn("BUY", actions)

    def test_stop_exit_fills_next_read_and_records_closed_trade(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        jump = self.warm_then_pump(rt, chain)
        self.cycle(rt, jump + 4)
        chain.scale_price(0.80)
        self.cycle(rt, jump + 8)
        self.assertEqual([o["kind"] for o in self.state()["pending"]], ["exit"])
        self.assertEqual(len(self.portfolio()["positions"]), 1)
        chain.scale_price(0.95)
        self.cycle(rt, jump + 12)
        portfolio = self.portfolio()
        self.assertEqual(portfolio["positions"], {})
        self.assertEqual(portfolio["closed_trades"], 1)
        trades = self.store.closed_trades()
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade["strategy"], pump.PUMP_STRATEGY)
        self.assertTrue(trade["reason"].startswith("pump: stop"))
        self.assertLess(trade["net"], 0)
        self.assertAlmostEqual(portfolio["cash"], 1500.0 + trade["net"], places=5)
        self.assertAlmostEqual(trade["net"], trade["gross"] - trade["costs"], places=6)
        self.assertEqual(self.state()["book"]["closed"], 1)
        self.assertEqual(self.state()["book"]["streak"], 1)
        signals = self.store.get_state("signal_analysis", None, fresh=True)
        self.assertIn("SELL", [e.get("action") for e in signals["events"]])
        self.assertEqual(len(self.store.get_state("done_cards", [], fresh=True)), 1)

    def test_scale_half_then_trail(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        jump = self.warm_then_pump(rt, chain)
        self.cycle(rt, jump + 4)
        chain.scale_price(1.40)
        self.cycle(rt, jump + 8)
        self.assertEqual([o["kind"] for o in self.state()["pending"]], ["scale"])
        self.cycle(rt, jump + 12)
        position = next(iter(self.portfolio()["positions"].values()))
        self.assertTrue(position["scaled"])
        self.assertGreater(position["pump"]["realized_usd"], 0)
        chain.scale_price(0.85)
        self.cycle(rt, jump + 16)
        self.assertEqual([o["kind"] for o in self.state()["pending"]], ["exit"])
        self.assertIn("trail", self.state()["pending"][0]["reason"])

    # ------------------------------------------------------------ modes and gates
    def test_radar_mode_never_opens_a_position(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain, mode="radar")
        jump = self.warm_then_pump(rt, chain)
        self.cycle(rt, jump + 4)
        self.assertEqual(self.portfolio()["positions"], {})
        self.assertEqual(self.state()["pending"], [])
        self.assertIn("RADAR", [e["action"] for e in self.state()["events"]])
        radar = self.state()["radar"]
        self.assertEqual(radar[0]["mint"], chain.mint)
        self.assertTrue(all(g["status"] == "pass" for g in radar[0]["gates"] if g["key"] != "signal"))

    def test_unreadable_creator_fails_closed(self) -> None:
        chain = FakeChain(program=None)  # unknown token program: creator account cannot be derived
        rt = self.runtime(chain)
        self.warm_then_pump(rt, chain)
        self.assertEqual(self.state()["pending"], [])
        skip = [e for e in self.state()["events"] if e["action"] == "SKIP"]
        self.assertTrue(skip and "Creator" in skip[-1]["reason"])

    def test_unseen_launch_fails_closed(self) -> None:
        chain = FakeChain(created_at=T0 - 3600)
        rt = self.runtime(chain)
        self.warm_then_pump(rt, chain)
        self.assertEqual(self.state()["pending"], [])
        gates = {g["key"]: g for g in self.state()["radar"][0]["gates"]}
        self.assertEqual(gates["launch_take"]["status"], "unknown")

    def test_heavy_launch_take_blocks(self) -> None:
        chain = FakeChain(launch_vs=60e9)  # about 30% of supply bought at launch
        rt = self.runtime(chain)
        self.warm_then_pump(rt, chain)
        self.assertEqual(self.state()["pending"], [])
        gates = {g["key"]: g for g in self.state()["radar"][0]["gates"]}
        self.assertEqual(gates["launch_take"]["status"], "fail")

    def test_creator_over_five_percent_blocks(self) -> None:
        chain = FakeChain(creator_amount=80_000_000_000_000)  # 8% of supply
        rt = self.runtime(chain)
        self.warm_then_pump(rt, chain)
        self.assertEqual(self.state()["pending"], [])
        gates = {g["key"]: g for g in self.state()["radar"][0]["gates"]}
        self.assertEqual(gates["creator_share"]["status"], "fail")

    def test_creator_sale_exits_open_position(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        jump = self.warm_then_pump(rt, chain)
        self.cycle(rt, jump + 4)
        chain.creator_amount = 5_000_000_000_000
        self.cycle(rt, jump + 8)
        pending = self.state()["pending"]
        self.assertEqual(pending[0]["kind"], "exit")
        self.assertIn("creator sold", pending[0]["reason"])

    def test_missed_entry_when_no_read_within_20_seconds(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        jump = self.warm_then_pump(rt, chain)
        chain.rpc_down = True
        for n in range(1, 7):
            self.cycle(rt, jump + 4 * n)
        self.assertEqual(self.portfolio()["positions"], {})
        self.assertEqual(self.state()["book"]["missed"], 1)
        self.assertIn("MISSED", [e["action"] for e in self.state()["events"]])
        self.assertIn("verified reads unavailable", self.state()["source"]["error"])

    def test_six_position_cap_counts_legacy_positions(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        portfolio = initial_portfolio()
        for n in range(6):
            portfolio["positions"][f"legacy-{n}"] = {"id": f"legacy-{n}", "mint": key(f"legacy-{n}"), "symbol": f"L{n}", "entry_at": T0,
                                                     "entry_mcap": 10_000.0, "last_mcap": 10_000.0, "peak_mcap": 10_000.0,
                                                     "initial_notional": 10.0, "remaining_quantity": 0.001, "remaining_basis": 10.0,
                                                     "total_exit_proceeds": 0.0, "costs": 0.1, "scaled": False}
        portfolio["cash"] = 1500.0 - 60.6
        self.store.set_state("portfolio", portfolio)
        self.warm_then_pump(rt, chain)
        self.assertEqual(self.state()["pending"], [])
        gates = {g["key"]: g for g in self.state()["radar"][0]["gates"]}
        self.assertEqual(gates["capacity"]["status"], "fail")
        self.assertEqual(len(self.portfolio()["positions"]), 6)

    def test_book_drawdown_pauses_new_entries(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        state = pump.empty_pump_state("paper")
        state["book"].update({"net": -160.0, "peakNet": 0.0})
        pump._refresh_book(state)
        self.store.set_state("pump_state", state)
        self.warm_then_pump(rt, chain)
        self.assertEqual(self.state()["pending"], [])
        self.assertIn("paused", self.state()["book"]["paused"])

    def test_listing_budget_is_one_request_per_cycle_at_most(self) -> None:
        chain = FakeChain()
        calls: list[str] = []
        listing = chain.listing

        def counted(url: str) -> list[dict]:
            calls.append("created" if "created_timestamp" in url else "trades")
            return listing(url)

        chain.listing = counted  # type: ignore[method-assign]
        rt = self.runtime(chain)
        for n in range(12):  # 48 s
            self.cycle(rt, T0 + 2 + 4 * n)
        self.assertEqual(calls.count("created"), 4)  # every ~12 s
        self.assertEqual(calls.count("trades"), 2)   # every ~24 s
        self.assertLessEqual(len(calls) / 48 * 60, 7.5)

    def test_listing_403_backs_off_and_blocks_new_entries_but_keeps_reading_held(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        jump = self.warm_then_pump(rt, chain)
        self.cycle(rt, jump + 4)  # entry filled
        self.assertEqual(len(self.portfolio()["positions"]), 1)
        chain.listing_error = "HTTPError: HTTP Error 403: Forbidden"
        at = jump + 8
        for _ in range(60):  # 4 minutes of cycles
            self.cycle(rt, at)
            at += 4
        # 60 s, then 120 s, then 240 s backoff: three attempts in four minutes, not one per cycle
        self.assertEqual(chain.blocked_calls, 3)
        state = self.state()
        self.assertIn("listing paused 240 s", state["source"]["error"])
        self.assertAlmostEqual(state["source"]["lastRead"], at - 4, places=3)  # verified reserve reads continue
        gates = {g["key"]: g for g in state["radar"][0]["gates"]}
        self.assertEqual(gates["listing_fresh"]["status"], "unknown")
        self.assertIn("listing paused", gates["listing_fresh"]["detail"])
        # recovery: a successful listing clears the pause and the gate passes again
        chain.listing_error = None
        for _ in range(70):
            self.cycle(rt, at)
            at += 4
        state = self.state()
        self.assertIsNone(state["source"]["error"])
        gates = {g["key"]: g for g in state["radar"][0]["gates"]} if state["radar"] else {}
        if gates:
            self.assertEqual(gates["listing_fresh"]["status"], "pass")

    def test_three_losses_halve_the_ticket(self) -> None:
        self.assertEqual(pump.ticket_for(0), 65.0)
        self.assertEqual(pump.ticket_for(3), 32.5)

    # ------------------------------------------------------------ isolation and fencing
    def test_legacy_tick_and_sell_diagnostics_ignore_pump_positions(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        jump = self.warm_then_pump(rt, chain)
        self.cycle(rt, jump + 4)
        position = next(iter(self.portfolio()["positions"].values()))
        crash = {"mint": chain.mint, "symbol": chain.symbol, "name": "x", "market_cap": position["entry_mcap"] * 0.3, "curve_pct": 40.0,
                 "curve_known": True, "reply_count": 0, "risk": {"status": "unknown"}, "source": "isolated-test-only"}
        rt.source = NoLegacySource([crash])
        rt.lease_owned = self.store.acquire_lease("optimizer-worker", self.boot, ttl_seconds=75, now=jump + 6)
        rt.tick(now=jump + 6)
        self.assertEqual(len(self.portfolio()["positions"]), 1)
        analysis = build_signal_analysis(empty_signal_analysis(), observations=[crash], tracked={}, portfolio=self.portfolio(),
                                         strategy_revision=3, now=jump + 6)
        self.assertFalse([c for c in analysis["candidates"] if c.get("side") == "sell"])

    def test_lost_lease_writes_nothing(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        self.assertTrue(self.store.acquire_lease("optimizer-worker", "other-worker", ttl_seconds=75, now=T0))
        rt.lease_owned = True
        self.assertFalse(rt.pump_tick(now=T0 + 2))
        self.assertFalse(rt.lease_owned)
        self.assertEqual(self.state()["asOf"], None)

    def test_off_mode_does_nothing(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain, mode="off")
        self.assertFalse(self.cycle(rt, T0 + 2))

    def test_status_exposes_labelled_pump_state(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain, mode="radar")
        self.warm_then_pump(rt, chain)
        status = rt.status(now=T0 + 100)
        pm = status["pumpMode"]
        self.assertEqual(pm["mode"], "radar")
        self.assertFalse(pm["calibrated"])
        self.assertIn("not a probability", pm["disclaimer"])
        self.assertIn("substituted", pm["holderGate"])
        self.assertTrue(pm["radar"])

    def test_restart_rehydrates_held_token_without_listing(self) -> None:
        chain = FakeChain()
        rt = self.runtime(chain)
        jump = self.warm_then_pump(rt, chain)
        self.cycle(rt, jump + 4)
        restarted = self.runtime(chain)  # new process: empty market memory
        chain.listing_down = True
        chain.scale_price(0.80)
        self.cycle(restarted, jump + 8)
        self.assertEqual([o["kind"] for o in self.state()["pending"]], ["exit"])


if __name__ == "__main__":
    unittest.main()
