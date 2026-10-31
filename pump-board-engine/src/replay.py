"""Offline replay of the recorded paper book. No network, no wallet, no orders.

The ledger does not store swap tapes or bonding-curve reserves, so flow, acceleration,
holder, and intrabar emergency rules cannot be re-simulated here. What can be
recomputed is reported: a fee-only remark of historical gross P&L, the reserve-style
curve band using the curve percent stored on each buy, and whether the hardened
similarity rule would still boost. This is an expected-edge diagnostic, not a
profitability result.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from . import similarity as SIM
from .live_feed import STRAT


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def round_trips(fills: list[dict]) -> list[dict]:
    """Pair each BUY with later SELL legs until the position is flat."""
    open_legs: dict[str, dict] = {}
    trips = []
    for row in fills:
        key = row.get("symbol") or ""
        side = row.get("side")
        try:
            usd = float(row.get("usd") or 0)
            pnl = float(row.get("pnl") or 0)
            ts = float(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if side == "BUY":
            open_legs[key] = {"key": key, "basis": usd, "gross": 0.0, "buy_ts": ts, "open": True}
        elif side == "SELL" and key in open_legs:
            leg = open_legs[key]
            leg["gross"] += pnl
            reason = row.get("reason") or ""
            if not reason.startswith("scale-out"):
                leg["open"] = False
                leg["exit_ts"] = ts
                trips.append(leg)
                open_legs.pop(key, None)
    return trips


def fee_only_pnl(gross: float, basis: float, fee_bps: float) -> float:
    """Remark a raw mcap P&L as if both sides had paid ``fee_bps`` and no price impact.

    Flat spot (gross == 0) becomes a loss of about two fees. Impact is not included.
    """
    fee = min(0.99, max(0.0, float(fee_bps)) / 10_000.0)
    multiple = (1.0 + (gross / basis if basis else 0.0)) * (1.0 - fee) ** 2
    return basis * (multiple - 1.0)


def _curve_by_key(history: list[dict]) -> dict[str, float | None]:
    found: dict[str, float | None] = {}
    for row in history:
        if row.get("side") != "BUY" or not row.get("key"):
            continue
        value = row.get("curve_pct")
        found[row["key"]] = float(value) if isinstance(value, (int, float)) else None
    return found


def _band(curve: float | None, strat: dict) -> str:
    if curve is None:
        return "unknown"
    if curve < strat["min_curve_pct"] or curve > strat["max_curve_pct"]:
        return "reject"
    if curve > strat["curve_breadth_from_pct"]:
        return "reject_needs_breadth"
    return "keep"


def similarity_replay(history: list[dict], strat: dict) -> dict:
    """Walk buys in time order and judge each one only with exits already on the book."""
    rows = sorted(history, key=lambda row: float(row.get("ts") or 0))
    exits: list[dict] = []
    relaxed = {**SIM.SIM, "min_labeled": 5}
    hardened = {**SIM.SIM, "min_labeled": strat["v6_min_hist"]}
    out = {"buys": 0, "boost_old": 0, "boost_new": 0, "denied_by_new_rule": 0}
    for row in rows:
        if row.get("side") == "EXIT" and row.get("pnl_pct") is not None and row.get("sentiment_0_100") is not None:
            exits.append(row)
            continue
        if row.get("side") != "BUY" or not row.get("sentiment_buy"):
            continue
        out["buys"] += 1
        now = float(row.get("ts") or 0)
        old = SIM.decide(row, exits, relaxed, now=now)["decision"]
        new = SIM.decide(row, exits, hardened, now=now)["decision"]
        if old == "boost":
            out["boost_old"] += 1
        if new == "boost":
            out["boost_new"] += 1
        if old == "boost" and new != "boost":
            out["denied_by_new_rule"] += 1
    return out


def report(fills: list[dict], history: list[dict], strat: dict | None = None) -> dict:
    strat = dict(strat or STRAT)
    curves = _curve_by_key(history)
    trips = round_trips(fills)
    gross = 0.0
    adjusted = 0.0
    bands = {"keep": 0, "reject": 0, "reject_needs_breadth": 0, "unknown": 0}
    kept_gross = 0.0
    rejected_gross = 0.0
    for trip in trips:
        gross += trip["gross"]
        adjusted += fee_only_pnl(trip["gross"], trip["basis"], strat["curve_fee_bps"])
        band = _band(curves.get(trip["key"]), strat)
        bands[band] += 1
        if band == "keep":
            kept_gross += trip["gross"]
        elif band.startswith("reject"):
            rejected_gross += trip["gross"]
    return {
        "paper_only": True,
        "live_trading": False,
        "round_trips": len(trips),
        "gross_pnl": round(gross, 2),
        "fee_only_pnl": round(adjusted, 2),
        "fee_bps": strat["curve_fee_bps"],
        "curve_band": bands,
        "kept_gross_pnl": round(kept_gross, 2),
        "rejected_gross_pnl": round(rejected_gross, 2),
        "similarity": similarity_replay(history, strat),
        "not_replayable": [
            "on-chain swap tape (buyers, imbalance, top buyer)",
            "reserve-delta acceleration across the three watch windows",
            "holder concentration when the ledger stored nulls",
            "intrabar emergency and volatility stops without a price path",
        ],
        "note": "Fee-only remark and gate counts are expected-edge diagnostics, not a profitability claim.",
    }


def report_paths(fills_path: str | Path, history_path: str | Path, strat: dict | None = None) -> dict:
    fills = _rows(Path(fills_path))
    history = []
    history_file = Path(history_path)
    if history_file.exists():
        for line in history_file.read_text(encoding="utf-8").splitlines():
            try:
                history.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return report(fills, history, strat)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    summary = report_paths(root / "out" / "ledger_live" / "fills.csv", root / "out" / "feature_history.jsonl")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
