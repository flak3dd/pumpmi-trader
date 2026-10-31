#!/usr/bin/env python3
"""Isolated software checks; values below are not profitability evidence."""
from pathlib import Path
import sys
import unittest
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pump-board-engine"))
from src.optimizer.hourly import hourly_monitor_tick
from src.optimizer.policy import initial_portfolio

class MemoryStore:
    def __init__(self):
        self.data = {"portfolio": initial_portfolio(), "source": {"lastObservation": None, "observations": 0}}
        self.trades = []
        self.audits = []
        self.revision = 7
    def get_state(self, key, default=None): return deepcopy(self.data.get(key, default))
    def set_state(self, key, value, **kwargs): self.data[key] = deepcopy(value)
    def set_many_state(self, values, **kwargs): self.data.update(deepcopy(values))
    def get_config(self): return {"revision": self.revision}
    def closed_trades(self): return deepcopy(self.trades)
    def audit(self, action, report): self.audits.append((action, deepcopy(report)))

class HourlyTests(unittest.TestCase):
    def test_complete_hour_and_cost_attribution(self):
        store = MemoryStore()
        store.trades = [{"exit_at": 3500, "gross": 10., "costs": 4., "net": 6.}]
        report = None
        for t in range(0, 3601, 30):
            store.data["source"] = {"lastObservation": t, "observations": t // 30 * 10}
            report = hourly_monitor_tick(store, now=t)
        assert report is not None
        self.assertEqual(report["durationSeconds"], 3600)
        self.assertTrue(report["completeMarketCoverage"])
        self.assertEqual((report["gross"], report["modeledCosts"], report["net"]), (10., 4., 6.))
        self.assertEqual(report["classification"], "profitable")
        self.assertEqual(len(store.audits), 1)
        self.assertEqual(store.data["hourly_session"]["start"], 3600)
        self.assertEqual(store.data["portfolio"]["cash"], 1500)
        self.assertIsNone(hourly_monitor_tick(store, now=3601))
    def test_outage_not_called_profitable(self):
        store = MemoryStore()
        hourly_monitor_tick(store, now=0)
        report = hourly_monitor_tick(store, now=3600)
        assert report is not None
        self.assertEqual(report["classification"], "insufficient_data")
        self.assertFalse(report["completeMarketCoverage"])
    def test_two_fresh_samples_do_not_hide_a_long_outage(self):
        store = MemoryStore()
        store.trades = [{"exit_at": 3500, "gross": 10., "costs": 4., "net": 6.}]
        for t in (0, 3500, 3600):
            store.data["source"] = {"lastObservation": t, "observations": 1}
            report = hourly_monitor_tick(store, now=t)
        assert report is not None
        self.assertEqual(report["classification"], "insufficient_data")
        self.assertFalse(report["completeMarketCoverage"])
        self.assertGreater(report["maxFreshSampleGapSeconds"], 120)
        self.assertEqual(store.data["hourly_session"]["startSample"]["at"], 3600)

    def test_full_hour_without_completed_trades_not_a_win(self):
        store = MemoryStore()
        report = None
        for t in range(0, 3601, 30):
            store.data["source"] = {"lastObservation": t, "observations": t}
            if t > 1800: store.revision = 8
            report = hourly_monitor_tick(store, now=t)
        assert report is not None
        self.assertEqual(report["classification"], "no_completed_trades")
        self.assertEqual(report["strategyRevisions"], [7, 8])
        self.assertIsNone(report["winRatePct"])

if __name__ == "__main__": unittest.main(verbosity=2)
