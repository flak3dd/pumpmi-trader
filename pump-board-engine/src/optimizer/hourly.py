"""Hourly capability reports over the same continuously running paper portfolio.

Reports are observation summaries, not a claim that a strategy enhancement caused
profit. The monitor never executes orders, changes settings, or resets bankroll.
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from .policy import initial_portfolio, portfolio_equity

HOUR = 3600
MAX_SAMPLES = 1800


def hourly_monitor_tick(
    store: Any,
    *,
    now: float,
    lease_name: str = "optimizer-worker",
    holder: str | None = None,
    fence_now: float | None = None,
) -> dict[str, Any] | None:
    """Record one hourly sample/report.

    The worker supplies ``holder`` and gets one durable lease-fenced atomic
    transaction for the report, its audit, and all session/sample state. The
    optional unfenced branch remains for the deliberately in-memory analytical
    unit tests; the production worker never uses it.
    """
    transaction = store.lease_fence(lease_name, holder, now=fence_now) if holder is not None else nullcontext()
    with transaction:
        portfolio = store.get_state("portfolio", initial_portfolio())
        source = store.get_state("source", {})
        config = store.get_config() or {"revision": 0}
        sample = {
            "at": now,
            "dataAt": source.get("lastObservation"),
            "equity": round(portfolio_equity(portfolio), 8),
            "drawdownPct": float(portfolio.get("drawdown_pct", 0)),
            "observations": int(source.get("observations", 0)),
            "revision": int(config["revision"]),
            "error": source.get("error"),
        }
        session = store.get_state("hourly_session")
        samples = store.get_state("hourly_samples", [])
        samples.append(sample)
        samples = samples[-MAX_SAMPLES:]
        if session is None:
            store.set_many_state({"hourly_session": {"start": now, "startSample": sample}, "hourly_samples": samples}, now=now)
            return None
        start = float(session["start"])
        end = start + HOUR
        if now < end:
            store.set_state("hourly_samples", samples, now=now)
            return None
        points = [item for item in samples if start <= float(item["at"]) < end]
        fresh = [item for item in points if item.get("dataAt") is not None and 0 <= float(item["at"]) - float(item["dataAt"]) <= 120 and not item.get("error")]
        elapsed_span = 0.0 if len(fresh) < 2 else float(fresh[-1]["at"]) - float(fresh[0]["at"])
        coverage = len(fresh) / max(1, len(points))
        checkpoints = [start] + [float(item["at"]) for item in fresh] + [end]
        max_gap = max((b - a for a, b in zip(checkpoints, checkpoints[1:])), default=HOUR)
        complete = elapsed_span >= 3300 and coverage >= 0.9 and len(fresh) >= 30 and max_gap <= 120
        trades = [item for item in store.closed_trades() if start <= float(item["exit_at"]) < end]
        gross = sum(float(item["gross"]) for item in trades)
        costs = sum(float(item["costs"]) for item in trades)
        net = sum(float(item["net"]) for item in trades)
        winners = sum(1 for item in trades if float(item["net"]) > 0)
        ending = points[-1] if points else session["startSample"]
        beginning = session["startSample"]
        classification = "insufficient_data" if not complete else "no_completed_trades" if not trades else "profitable" if net > 0 else "loss" if net < 0 else "flat"
        report = {
            "id": f"hour-{int(start * 1000)}",
            "start": start, "end": end, "durationSeconds": HOUR,
            "paperOnly": True, "classification": classification,
            "gross": round(gross, 2), "modeledCosts": round(costs, 2), "net": round(net, 2),
            "completedTrades": len(trades), "wins": winners,
            "losses": sum(1 for item in trades if float(item["net"]) < 0),
            "winRatePct": None if not trades else round(100 * winners / len(trades), 2),
            "equityStart": beginning["equity"], "equityEnd": ending["equity"],
            "startValuationAt": beginning.get("dataAt"),
            "equityChange": round(float(ending["equity"]) - float(beginning["equity"]), 2),
            "valuationAt": ending["dataAt"],
            "maxObservedDrawdownPct": max((float(item["drawdownPct"]) for item in points), default=0.0),
            "freshSampleFraction": round(coverage, 4), "freshSpanSeconds": round(elapsed_span), "maxFreshSampleGapSeconds": round(max_gap),
            "completeMarketCoverage": complete,
            "sourceObservations": max(0, int(ending["observations"]) - int(beginning["observations"])),
            "strategyRevisions": sorted({int(item["revision"]) for item in points}),
            "note": "Modeled paper results only. Entry costs are attributed to completed trades; equity includes open positions. Mixed strategy revisions are not a controlled before/after experiment. No future-profit guarantee.",
        }
        reports = store.get_state("hourly_reports", [])
        if not any(item["id"] == report["id"] for item in reports):
            reports.append(report)
            store.audit("hourly_performance", report)
        # Keep hourly windows contiguous. A large outage is reported as insufficient
        # evidence on subsequent ticks rather than skipped or called a profitable run.
        store.set_many_state({
            "hourly_reports": reports[-24:],
            "hourly_session": {"start": end, "startSample": next((item for item in samples if float(item["at"]) >= end), sample)},
            "hourly_samples": samples,
        }, now=now)
        return report
