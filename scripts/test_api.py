#!/usr/bin/env python3
"""Stdlib tests for the adapter's endpoint dispatcher and fixture isolation."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

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

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def dispatch(self, method: str, target: str, body: bytes = b""):
        return serve.dispatch(method, target, body, strategy_config_path=self.strategy_path)

    def test_health_endpoint(self) -> None:
        status, payload = self.dispatch("GET", "/health")
        self.assertEqual(int(status), 200)
        self.assertEqual(payload, {"ok": True})

    def test_snapshot_endpoint_wraps_raw_datasets(self) -> None:
        status, payload = self.dispatch("GET", "/api/snapshot")
        self.assertEqual(int(status), 200)
        self.assertEqual(payload["board"], json.loads(RAW_DATASETS[0].read_text(encoding="utf-8")))
        self.assertEqual(payload["trades"], json.loads(RAW_DATASETS[1].read_text(encoding="utf-8")))
        self.assertEqual(payload["meta"]["kind"], "snapshot")
        self.assertEqual(payload["meta"]["label"], "Recorded market snapshot · paper trades")
        self.assertTrue(payload["meta"]["simulationAvailable"])
        self.assertEqual(payload["meta"]["updated"], payload["board"].get("updated"))
        self.assertEqual(payload["strategyConfig"]["watchPeriods"], {"short": 90, "medium": 180, "long": 300})

    def test_strategy_endpoint_is_paper_only_and_exact(self) -> None:
        status, payload = self.dispatch("GET", "/api/strategy")
        self.assertEqual(int(status), 200)
        from src.live_feed import STRAT  # type: ignore

        self.assertEqual(payload, {
            "strategy": STRAT,
            "watchPeriods": {"short": 90, "medium": 180, "long": 300},
            "revision": 0,
            "editable": True,
            "paperOnly": True,
        })
        self.assertEqual(payload["strategy"]["max_open"], 6)

    def test_strategy_put_persists_only_valid_slider_values_with_revision_conflicts(self) -> None:
        status, payload = self.dispatch(
            "PUT",
            "/api/strategy",
            json.dumps({"watchPeriods": {"short": 120, "medium": 240, "long": 360}, "revision": 0}).encode(),
        )
        self.assertEqual(int(status), 200)
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["watchPeriods"], {"short": 120, "medium": 240, "long": 360})
        self.assertEqual(payload["strategy"]["watch_short_s"], 120)
        self.assertEqual(payload["strategy"]["max_open"], 6)

        with self.assertRaises(serve.ApiError) as stale:
            self.dispatch(
                "PUT",
                "/api/strategy",
                b'{"watchPeriods":{"short":135,"medium":255,"long":375},"revision":0}',
            )
        self.assertEqual(int(stale.exception.status), 409)
        with self.assertRaises(serve.ApiError) as invalid:
            self.dispatch(
                "PUT",
                "/api/strategy",
                b'{"watchPeriods":{"short":120,"medium":240,"long":360},"revision":1,"max_open":99}',
            )
        self.assertEqual(int(invalid.exception.status), 400)

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
        self.assertEqual(payload["strategyConfig"]["watchPeriods"], {"short": 90, "medium": 180, "long": 300})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
