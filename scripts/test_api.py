#!/usr/bin/env python3
"""Stdlib tests for the adapter's endpoint dispatcher and fixture isolation."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import serve  # noqa: E402

RAW_DATASETS = (
    ROOT / "pump-board-engine" / "out" / "board.json",
    ROOT / "pump-board-engine" / "out" / "trade_charts.json",
)
FIXTURE_DATASETS = (
    ROOT / "pump-board-engine" / "fixtures" / "coins.json",
    ROOT / "pump-board-engine" / "fixtures" / "known_rugs.json",
    ROOT / "pump-board-engine" / "fixtures" / "posts.json",
)
IMMUTABLE_DATASETS = RAW_DATASETS + FIXTURE_DATASETS


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="pumpmi-api-strategy-")
        self.strategy_path = Path(self.temporary_directory.name) / "strategy.json"
        self.strategy_path.write_bytes((ROOT / "pump-board-engine" / "strategy.json").read_bytes())
        self.initial_document = json.loads(self.strategy_path.read_text(encoding="utf-8"))
        self.initial_revision = int(self.initial_document["revision"])
        self.initial_periods = dict(self.initial_document["watchPeriods"])

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def dispatch(self, method: str, target: str, body: bytes = b""):
        return serve.dispatch(method, target, body, strategy_config_path=self.strategy_path)

    def test_health_endpoint(self) -> None:
        status, payload = self.dispatch("GET", "/health")
        self.assertEqual(int(status), 200)
        self.assertEqual(payload, {"ok": True})

    def test_live_feed_without_worker_never_reads_saved_files(self) -> None:
        for route in ("/api/live-feed", "/api/snapshot"):
            with patch.object(serve, "_read_json", side_effect=AssertionError("saved snapshot was loaded")):
                with self.assertRaises(serve.ApiError) as caught:
                    self.dispatch("GET", route)
            self.assertEqual(int(caught.exception.status), 503)
            self.assertIn("No historical fallback", caught.exception.message)

    def test_live_feed_returns_only_enabled_runtime_frames(self) -> None:
        runtime = Mock(enabled=True)
        runtime.snapshot_payload.return_value = {
            "board": {"ts": 1, "columns": {"checking": [], "holding": [], "done": []}}, "trades": {},
            "meta": {"kind": "awaiting", "label": "Awaiting live prices", "simulationAvailable": True}}
        with patch.object(serve, "_runtime_for_payload", return_value=runtime), patch.object(serve, "strategy_payload", return_value={"revision":7}), patch.object(serve, "_read_json", side_effect=AssertionError("saved data loaded")):
            status, frame = self.dispatch("GET", "/api/live-feed")
        self.assertEqual(int(status), 200)
        self.assertEqual(frame["meta"]["kind"], "awaiting")
        self.assertEqual(frame["meta"]["transport"], "server-sent-events")
        self.assertFalse(frame["meta"]["simulationAvailable"])

    def test_active_strategy_reports_actual_durable_limits(self) -> None:
        runtime = Mock(enabled=True)
        runtime.config.return_value = self.initial_document
        with patch.object(serve, "_runtime_for_payload", return_value=runtime):
            values = serve.strategy_payload(self.strategy_path)
        from src.optimizer.policy import RISK_LIMITS
        self.assertEqual(values["strategy"]["min_curve_pct"], RISK_LIMITS["min_curve_pct"])
        self.assertEqual(values["strategy"]["max_open"], 6)
        self.assertEqual(values["watchPeriods"], self.initial_periods)

    def test_strategy_endpoint_is_paper_only_and_exact(self) -> None:
        status, payload = self.dispatch("GET", "/api/strategy")
        self.assertEqual(int(status), 200)
        from src.live_feed import STRAT  # type: ignore

        expected_strategy = dict(STRAT)
        expected_strategy.update(
            max_open=self.initial_document["max_open"],
            watch_short_s=self.initial_periods["short"],
            watch_medium_s=self.initial_periods["medium"],
            watch_long_s=self.initial_periods["long"],
        )
        self.assertEqual(payload, {
            "strategy": expected_strategy,
            "watchPeriods": self.initial_periods,
            "revision": self.initial_revision,
            "editable": True,
            "paperOnly": True,
        })
        self.assertEqual(payload["strategy"]["max_open"], 6)

    def test_strategy_put_persists_only_valid_slider_values_with_revision_conflicts(self) -> None:
        status, payload = self.dispatch(
            "PUT",
            "/api/strategy",
            json.dumps({"watchPeriods": {"short": 120, "medium": 240, "long": 360}, "revision": self.initial_revision}).encode(),
        )
        self.assertEqual(int(status), 200)
        self.assertEqual(payload["revision"], self.initial_revision + 1)
        self.assertEqual(payload["watchPeriods"], {"short": 120, "medium": 240, "long": 360})
        self.assertEqual(payload["strategy"]["watch_short_s"], 120)
        self.assertEqual(payload["strategy"]["max_open"], 6)

        with self.assertRaises(serve.ApiError) as stale:
            self.dispatch(
                "PUT",
                "/api/strategy",
                json.dumps({"watchPeriods": {"short": 135, "medium": 255, "long": 375}, "revision": self.initial_revision}).encode(),
            )
        self.assertEqual(int(stale.exception.status), 409)
        with self.assertRaises(serve.ApiError) as invalid:
            self.dispatch(
                "PUT",
                "/api/strategy",
                json.dumps({"watchPeriods": {"short": 120, "medium": 240, "long": 360}, "revision": self.initial_revision + 1, "max_open": 99}).encode(),
            )
        self.assertEqual(int(invalid.exception.status), 400)

    def test_injected_strategy_path_bypasses_optimizer_database_with_database_url_set(self) -> None:
        """Temp strategy tests must not seed/query the managed optimizer database."""
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "mysql://user:secret@managed.example/optimizer?ssl=true"
        try:
            with patch.object(serve, "optimizer_runtime", side_effect=AssertionError("injected API test touched optimizer DB")):
                strategy = serve.strategy_payload(self.strategy_path)
                with self.assertRaises(serve.ApiError) as unavailable:
                    serve.live_feed_payload(strategy_config_path=self.strategy_path)
                self.assertEqual(int(unavailable.exception.status), 503)
                updated = serve.update_strategy_payload(
                    {"watchPeriods": {"short": 120, "medium": 240, "long": 360}, "revision": self.initial_revision},
                    self.strategy_path,
                )
                simulation = serve.simulate_payload(self.strategy_path)
            self.assertEqual(strategy["revision"], self.initial_revision)
            self.assertEqual(updated["revision"], self.initial_revision + 1)
            self.assertEqual(simulation["strategyConfig"]["revision"], self.initial_revision + 1)
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous

    def test_simulation_is_fixture_only_and_preserves_recorded_datasets(self) -> None:
        before = {path: digest(path) for path in IMMUTABLE_DATASETS}
        status, payload = self.dispatch("POST", "/api/simulate", b"{}")
        after = {path: digest(path) for path in IMMUTABLE_DATASETS}

        self.assertEqual(int(status), 200)
        self.assertEqual(before, after, "simulation must not overwrite recorded or fixture datasets")
        self.assertEqual(payload["meta"]["kind"], "fixture")
        self.assertEqual(payload["meta"]["label"], "Fixture simulation · paper trades")
        self.assertTrue(payload["meta"]["simulationAvailable"])
        self.assertEqual(payload["meta"]["updated"], payload["board"]["updated"])
        self.assertEqual(payload["board"]["mode"], "paper")
        self.assertEqual(payload["board"]["source"], "OFFLINE FIXTURES · PAPER TRADES")
        self.assertEqual(payload["strategyConfig"]["strategy"]["max_open"], 6)
        self.assertEqual(payload["strategyConfig"]["watchPeriods"], self.initial_periods)
        self.assertIn("stats", payload["board"])
        self.assertIn("log", payload["board"])
        self.assertTrue(any(payload["trades"].values()), "closed fixture fills should produce engine chart data")
        self.assertTrue(all(line[2] == ":" and line[5] == ":" for line in payload["board"]["log"]))
        self.assertTrue(
            any(
                card.get("fixture")
                for cards in payload["board"]["columns"].values()
                for card in cards
            )
        )
        zeph = payload["trades"]["ZEPH"]
        self.assertEqual(zeph["metric"], "unit_price")
        self.assertEqual(zeph["entry"], 1.0)
        self.assertEqual(zeph["series"][-1]["value"], 2.04)
        self.assertEqual([point["value"] for point in zeph["series"]], [1.0, 2.04])
        self.assertEqual([signal["at"] for signal in zeph["signals"]], [0, 1])

    def test_simulate_rejects_parameters_and_nonempty_bodies(self) -> None:
        for body in (b"", b"[]", b'{"position_usd":1}'):
            with self.subTest(body=body), self.assertRaises(serve.ApiError) as caught:
                self.dispatch("POST", "/api/simulate", body)
            self.assertEqual(int(caught.exception.status), 400)
        with self.assertRaises(serve.ApiError) as caught:
            self.dispatch("POST", "/api/simulate?anything=1", b"{}")
        self.assertEqual(int(caught.exception.status), 400)


ENGINE = ROOT / "pump-board-engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from src.file_feed import (  # noqa: E402
    command_is_live_feed,
    process_is_live_feed,
    publish_pid,
    read_pid,
    release_pid,
)


def write_board(out: Path, *, now: float, ts, equity: float = 1108.36, source: str = "LIVE MARKET DATA · PAPER TRADES", mode: str = "paper") -> None:
    board = {
        "ts": ts,
        "mode": mode,
        "bankroll": 1500,
        "equity": equity,
        "multiple": round(equity / 1500, 3) if equity else 0,
        "columns": {
            "checking": [{"symbol": "PAPER", "mint": "MintPaper111", "column": "checking", "mcap": 12000}],
            "holding": [{"symbol": "HELD", "mint": "MintHeld222", "column": "holding", "mcap": 18000}],
            "done": [],
        },
        "source": source,
        "updated": "2026-10-31 14:14:00 UTC",
    }
    out.mkdir(parents=True, exist_ok=True)
    path = out / "board.json"
    path.write_text(json.dumps(board), encoding="utf-8")
    os.utime(path, (now, now))


@contextlib.contextmanager
def running_file_worker(out: Path):
    """A sleep process whose command line is src/live_feed.py, named by the pid file."""
    stub_root = Path(tempfile.mkdtemp(prefix="pumpmi-feed-stub-"))
    script_dir = stub_root / "src"
    script_dir.mkdir()
    script = script_dir / "live_feed.py"
    script.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "live_feed.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
        deadline = time.time() + 3
        while time.time() < deadline and not process_is_live_feed(proc.pid):
            time.sleep(0.02)
        if not process_is_live_feed(proc.pid):
            raise RuntimeError("stub live_feed process was not visible")
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)
        shutil.rmtree(stub_root, ignore_errors=True)


class FileBackedLiveFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="pumpmi-api-strategy-")
        self.strategy_path = Path(self.temporary_directory.name) / "strategy.json"
        self.strategy_path.write_bytes((ENGINE / "strategy.json").read_bytes())
        self.out = Path(self.temporary_directory.name) / "out"
        self.out.mkdir()
        self.now = time.time()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def dispatch(self, method: str, target: str, body: bytes = b""):
        return serve.dispatch(method, target, body, strategy_config_path=self.strategy_path)

    def test_command_line_matches_only_the_file_worker(self) -> None:
        self.assertTrue(command_is_live_feed("python3 -m src.live_feed"))
        self.assertTrue(command_is_live_feed("/usr/bin/python3 /tmp/src/live_feed.py"))
        self.assertFalse(command_is_live_feed("python3 scripts/serve.py"))
        self.assertFalse(command_is_live_feed("python3 scripts/test_live_feed.py"))
        self.assertFalse(process_is_live_feed(os.getpid()))

    def test_fresh_worker_serves_board_equity_and_lanes(self) -> None:
        write_board(self.out, now=self.now, ts=self.now, equity=1108.36)
        (self.out / "trade_charts.json").write_text(
            json.dumps({"PAPER": {"entry": 30199, "exitPnl": -24.25, "series": [{"label": "t", "value": 30199}]}}),
            encoding="utf-8",
        )
        with running_file_worker(self.out), patch.dict(os.environ, {"DATABASE_URL": ""}), patch.object(serve, "OUT_DIR", self.out), patch.object(
            serve, "simulate_payload", side_effect=AssertionError("simulate used as live feed")
        ), patch.object(serve, "_read_json", side_effect=AssertionError("archived snapshot loader used")):
            for route in ("/api/live-feed", "/api/snapshot"):
                status, payload = self.dispatch("GET", route)
                self.assertEqual(int(status), 200, route)
                self.assertEqual(payload["meta"]["kind"], "live")
                self.assertEqual(payload["meta"]["label"], "Verified live-market paper board")
                self.assertFalse(payload["meta"]["simulationAvailable"])
                self.assertEqual(payload["meta"]["transport"], "server-sent-events")
                self.assertEqual(payload["board"]["equity"], 1108.36)
                self.assertEqual(payload["board"]["bankroll"], 1500)
                self.assertEqual(payload["board"]["mode"], "paper")
                self.assertEqual(payload["board"]["columns"]["checking"][0]["mint"], "MintPaper111")
                self.assertEqual(payload["board"]["columns"]["holding"][0]["symbol"], "HELD")
                self.assertEqual(payload["trades"]["PAPER"]["exitPnl"], -24.25)
                self.assertTrue(payload["strategyConfig"]["paperOnly"])
            self.assertIsNone(serve.live_stream_block_reason())

    def test_missing_timestamp_is_awaiting_and_future_timestamp_is_stale(self) -> None:
        with running_file_worker(self.out), patch.object(serve, "OUT_DIR", self.out):
            write_board(self.out, now=self.now, ts=None)
            _, awaiting = self.dispatch("GET", "/api/live-feed")
            self.assertEqual(awaiting["meta"]["kind"], "awaiting")
            self.assertFalse(awaiting["meta"]["simulationAvailable"])
            self.assertEqual(awaiting["board"]["equity"], 1108.36)
            write_board(self.out, now=self.now, ts=self.now + 30)
            _, stale = self.dispatch("GET", "/api/live-feed")
            self.assertEqual(stale["meta"]["kind"], "stale")
            self.assertFalse(stale["meta"]["simulationAvailable"])
            self.assertEqual(stale["board"]["columns"]["checking"][0]["mint"], "MintPaper111")

    def test_stopped_worker_and_stale_board_stay_503(self) -> None:
        write_board(self.out, now=self.now, ts=self.now, equity=424242.42)
        (self.out / "live_feed.pid").write_text("999999\n", encoding="utf-8")
        tempting = {"board": {"equity": 1, "ts": self.now, "bankroll": 1, "columns": {"checking": [], "holding": [], "done": []}}, "trades": {}, "meta": {"kind": "live", "label": "archived"}}
        with patch.dict(os.environ, {"DATABASE_URL": ""}), patch.object(serve, "OUT_DIR", self.out), patch.object(serve, "_read_json", return_value=tempting):
            with self.assertRaises(serve.ApiError) as stopped:
                self.dispatch("GET", "/api/live-feed")
            self.assertEqual(int(stopped.exception.status), 503)
            self.assertIn("not running", stopped.exception.message)
            self.assertIn("No historical fallback", stopped.exception.message)
            self.assertNotIn("424242.42", stopped.exception.message)
            self.assertIsNotNone(serve.live_stream_block_reason())
            self.assertIn("not running", serve.live_stream_block_reason() or "")

            with running_file_worker(self.out):
                os.utime(self.out / "board.json", (self.now - 181, self.now - 181))
                with self.assertRaises(serve.ApiError) as old_file:
                    self.dispatch("GET", "/api/live-feed")
                self.assertIn("missing or stale", old_file.exception.message)
                self.assertNotIn("424242.42", old_file.exception.message)

                write_board(self.out, now=self.now, ts=self.now - 181, equity=424242.42)
                with self.assertRaises(serve.ApiError) as old_ts:
                    self.dispatch("GET", "/api/live-feed")
                self.assertIn("stale", old_ts.exception.message)
                self.assertNotIn("424242.42", old_ts.exception.message)

    def test_unrelated_live_process_is_not_the_worker(self) -> None:
        write_board(self.out, now=self.now, ts=self.now, equity=424242.42)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            (self.out / "live_feed.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
            with patch.object(serve, "OUT_DIR", self.out):
                with self.assertRaises(serve.ApiError) as caught:
                    self.dispatch("GET", "/api/live-feed")
            self.assertIn("not running", caught.exception.message)
        finally:
            proc.kill()
            proc.wait(timeout=2)

    def test_fixtures_and_non_paper_boards_are_not_a_live_feed(self) -> None:
        with running_file_worker(self.out), patch.object(serve, "OUT_DIR", self.out):
            write_board(self.out, now=self.now, ts=self.now, source="OFFLINE FIXTURES · PAPER TRADES")
            with self.assertRaises(serve.ApiError) as fixture:
                self.dispatch("GET", "/api/live-feed")
            self.assertIn("Fixture", fixture.exception.message)
            write_board(self.out, now=self.now, ts=self.now, mode="live")
            with self.assertRaises(serve.ApiError) as trading:
                self.dispatch("GET", "/api/live-feed")
            self.assertIn("non-paper", trading.exception.message)

    def test_stale_or_fixture_trade_charts_are_omitted(self) -> None:
        write_board(self.out, now=self.now, ts=self.now)
        charts = self.out / "trade_charts.json"
        charts.write_text(json.dumps({"OLD": {"entry": 1}}), encoding="utf-8")
        os.utime(charts, (self.now - 181, self.now - 181))
        with running_file_worker(self.out), patch.object(serve, "OUT_DIR", self.out):
            _, payload = self.dispatch("GET", "/api/live-feed")
            self.assertEqual(payload["trades"], {})
            charts.write_text(json.dumps({"ZEPH": {"metric": "unit_price", "entry": 1}}), encoding="utf-8")
            _, fixture_charts = self.dispatch("GET", "/api/live-feed")
            self.assertEqual(fixture_charts["trades"], {})
            self.assertEqual(fixture_charts["meta"]["kind"], "live")

    def test_enabled_optimizer_is_preferred_over_a_fresh_file_worker(self) -> None:
        write_board(self.out, now=self.now, ts=self.now, equity=424242.42)
        runtime = Mock(enabled=True)
        runtime.snapshot_payload.return_value = {
            "board": {"ts": 1, "bankroll": 1500, "equity": 1500, "columns": {"checking": [], "holding": [], "done": []}},
            "trades": {},
            "meta": {"kind": "awaiting", "label": "Awaiting live prices", "simulationAvailable": True},
        }
        with running_file_worker(self.out), patch.object(serve, "OUT_DIR", self.out), patch.object(
            serve, "_runtime_for_payload", return_value=runtime
        ), patch.object(serve, "strategy_payload", return_value={"revision": 7}), patch.object(
            serve, "_file_backed_live_payload", side_effect=AssertionError("file worker used while optimizer is enabled")
        ):
            status, frame = self.dispatch("GET", "/api/live-feed")
        self.assertEqual(int(status), 200)
        self.assertEqual(frame["meta"]["kind"], "awaiting")
        self.assertEqual(frame["board"]["equity"], 1500)
        self.assertFalse(frame["meta"]["simulationAvailable"])

    def test_configured_database_does_not_fall_back_to_files(self) -> None:
        write_board(self.out, now=self.now, ts=self.now, equity=424242.42)
        with running_file_worker(self.out), patch.object(serve, "OUT_DIR", self.out), patch.object(
            serve, "_runtime_for_payload", return_value=None
        ), patch.object(serve, "_durable_optimizer_requested", return_value=True):
            with self.assertRaises(serve.ApiError) as caught:
                self.dispatch("GET", "/api/live-feed")
            self.assertIn("durable database", caught.exception.message)
            self.assertEqual(serve.live_stream_block_reason(), "Live market worker is unavailable; no snapshot fallback.")

    def test_checked_in_board_is_not_served_without_a_worker(self) -> None:
        archived = json.loads((ENGINE / "out" / "board.json").read_text(encoding="utf-8"))
        pid = read_pid(serve.OUT_DIR / "live_feed.pid")
        if pid and process_is_live_feed(pid):
            self.skipTest("a file-backed worker is already running")
        previous = os.environ.get("DATABASE_URL")
        os.environ.pop("DATABASE_URL", None)
        try:
            with patch.object(serve, "_read_json", side_effect=AssertionError("archived snapshot loader used")):
                with self.assertRaises(serve.ApiError) as caught:
                    serve.live_feed_payload()
            self.assertEqual(int(caught.exception.status), 503)
            self.assertNotIn(str(archived["equity"]), caught.exception.message)
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous

    def test_worker_write_publishes_board_charts_and_pid(self) -> None:
        import src.live_feed as live_feed
        from src.live_feed import Feed

        work = Path(self.temporary_directory.name) / "worker"
        work.mkdir()
        strategy = work / "strategy.json"
        strategy.write_bytes((ENGINE / "strategy.json").read_bytes())
        previous = os.getcwd()
        with patch.object(live_feed, "Ledger"), patch.object(live_feed, "LunarCrushClient") as lunar, patch.object(Feed, "restore", lambda self: None):
            lunar.return_value.mode = "off"
            os.chdir(work)
            try:
                feed = Feed({"mode": "paper", "bankroll_usd": 1500}, strategy_config_path=strategy)
                feed.write()
            finally:
                os.chdir(previous)
        board = json.loads((work / "out" / "board.json").read_text(encoding="utf-8"))
        charts = json.loads((work / "out" / "trade_charts.json").read_text(encoding="utf-8"))
        self.assertEqual(board["mode"], "paper")
        self.assertEqual(board["source"], "LIVE MARKET DATA · PAPER TRADES")
        self.assertEqual(board["bankroll"], 1500)
        self.assertIsInstance(board["equity"], (int, float))
        self.assertIsInstance(board["ts"], float)
        self.assertEqual(set(board["columns"]), {"checking", "holding", "done"})
        self.assertIsInstance(charts, dict)
        self.assertEqual(read_pid(work / "out" / "live_feed.pid"), os.getpid())

    def test_publish_pid_refuses_to_replace_a_live_worker(self) -> None:
        with running_file_worker(self.out):
            owner = read_pid(self.out / "live_feed.pid")
            with self.assertRaises(RuntimeError):
                publish_pid(self.out, pid=os.getpid())
            self.assertEqual(read_pid(self.out / "live_feed.pid"), owner)
            release_pid(self.out, pid=os.getpid())
            self.assertEqual(read_pid(self.out / "live_feed.pid"), owner)
            release_pid(self.out, pid=owner)
            self.assertIsNone(read_pid(self.out / "live_feed.pid"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
