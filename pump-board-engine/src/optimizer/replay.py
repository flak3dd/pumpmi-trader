"""Chronological, market-cap-only replay for conservative watch-period review."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .policy import (
    MAX_OPEN,
    apply_observation,
    entry_reason,
    initial_portfolio,
    make_watch_state,
    neighbor_periods,
    open_position,
    portfolio_equity,
    record_watch_observation,
    watch_checkpoint_state,
)


def grouped_paths(observations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        if isinstance(observation, dict) and observation.get("mint") and observation.get("market_cap"):
            groups[str(observation["mint"])].append(observation)
    for path in groups.values():
        path.sort(key=lambda item: float(item["observed_at"]))
    return dict(groups)


def cohort_anchors(observations: list[dict[str, Any]]) -> dict[str, float]:
    return {mint: float(path[0]["observed_at"]) for mint, path in grouped_paths(observations).items() if path}


def replay(
    observations: list[dict[str, Any]],
    watch_periods: dict[str, int],
    *,
    allowed_mints: set[str] | None = None,
    cutoff: float | None = None,
    strategy_revision: int = 0,
) -> dict[str, Any]:
    """Replay only recorded observations in chronological order.

    An open position at the end remains pending and is explicitly excluded from
    closed-trade profitability metrics. ``cutoff`` makes it impossible for a
    pre-split cohort to use an exit after the split. Entry timing uses the identical
    actual-checkpoint helper as the forward worker: one overdue sampled point may
    satisfy all three tiers, with the same retained sampled timestamp.
    """
    paths = grouped_paths(observations)
    if allowed_mints is not None:
        paths = {mint: path for mint, path in paths.items() if mint in allowed_mints}
    if cutoff is not None:
        paths = {mint: [item for item in path if float(item["observed_at"]) <= cutoff] for mint, path in paths.items()}
        paths = {mint: path for mint, path in paths.items() if path}

    planned_entries: dict[tuple[str, float], tuple[dict[str, Any], dict[str, Any]]] = {}
    for mint, path in paths.items():
        first = path[0]
        watch = make_watch_state(
            watch_periods,
            started_at=float(first["observed_at"]),
            start_curve=float(first["curve_pct"]),
        )
        # The first actual sample at/after long completes the immutable watch.
        # Forward runtime decides once at that point too; a failed gate is a skip,
        # not a future opportunity to select a better sample.
        for item in path[1:]:
            record_watch_observation(watch, item)
            checkpoints = watch_checkpoint_state(watch, as_of=float(item["observed_at"]))
            if checkpoints["long"]["status"] != "observed":
                continue
            if entry_reason(item, watch) is None:
                planned_entries[(mint, float(item["observed_at"]))] = (item, watch)
            break

    timeline = sorted(
        (item for path in paths.values() for item in path),
        key=lambda item: (float(item["observed_at"]), str(item["mint"])),
    )
    portfolio = initial_portfolio()
    closed: list[dict[str, Any]] = []
    entered_mints: set[str] = set()
    for observation in timeline:
        mint = str(observation["mint"])
        # First manage only a position whose path has an actual observation now.
        for position_id, position in list(portfolio["positions"].items()):
            if position["mint"] != mint:
                continue
            event = apply_observation(portfolio, position_id, observation)
            if event and event["status"] == "closed":
                closed.append(event)
        key = (mint, float(observation["observed_at"]))
        planned = planned_entries.get(key)
        if planned and mint not in entered_mints and len(portfolio["positions"]) < MAX_OPEN:
            _, watch = planned
            if open_position(portfolio, observation, strategy_revision=strategy_revision, watch=watch):
                entered_mints.add(mint)

    pending = list(portfolio["positions"].values())
    gains = sum(max(0.0, float(trade["net"])) for trade in closed)
    losses = sum(-min(0.0, float(trade["net"])) for trade in closed)
    metrics = {
        "closed_trades": len(closed),
        "pending_trades": len(pending),
        "gross": round(sum(float(trade["gross"]) for trade in closed), 8),
        "costs": round(sum(float(trade["costs"]) for trade in closed), 8),
        "net": round(sum(float(trade["net"]) for trade in closed), 8),
        "profit_factor": None if losses == 0 and gains == 0 else (None if losses == 0 else round(gains / losses, 8)),
        "expectancy": None if not closed else round(sum(float(trade["net"]) for trade in closed) / len(closed), 8),
        "max_drawdown": round(float(portfolio.get("max_drawdown_pct", 0.0)), 8),
        "equity": round(portfolio_equity(portfolio), 8),
    }
    return {"trades": closed, "pending": pending, "metrics": metrics}


def chronological_split(observations: list[dict[str, Any]]) -> tuple[set[str], set[str], float | None]:
    """Split cohorts at a distinct anchor timestamp, never through a tie group."""
    anchors = sorted(((started, mint) for mint, started in cohort_anchors(observations).items()), key=lambda item: (item[0], item[1]))
    if len(anchors) < 2:
        return set(), set(), None
    target = len(anchors) * 0.70
    # Only boundaries between distinct times are valid. Otherwise a test cohort at
    # the same timestamp could be treated as future evidence relative to training.
    candidates = [index for index in range(1, len(anchors)) if anchors[index - 1][0] < anchors[index][0]]
    if not candidates:
        return set(), set(), None
    split_index = min(candidates, key=lambda index: (abs(index - target), -index))
    split_at = float(anchors[split_index - 1][0])
    train = {mint for started, mint in anchors if started <= split_at}
    test = {mint for started, mint in anchors if started > split_at}
    if not train or not test:
        return set(), set(), None
    return train, test, split_at


def validate_candidates(observations: list[dict[str, Any]], champion_periods: dict[str, int]) -> dict[str, Any]:
    """Select on chronological training, then compare candidate/champion on heldout cohorts."""
    train_mints, test_mints, split_at = chronological_split(observations)
    base = {"short": int(champion_periods["short"]), "medium": int(champion_periods["medium"]), "long": int(champion_periods["long"])}
    if not train_mints or not test_mints or split_at is None:
        return {
            "state": "waiting", "reason": "insufficient chronological cohorts with a distinct time boundary", "trainTrades": 0, "testTrades": 0,
            "testPF": None, "testNet": None, "maxDrawdown": None, "candidate": base, "champion": base,
        }
    # Train is hard-cut at the cohort split; an exit after the split cannot leak in.
    training = {tuple(sorted(periods.items())): replay(observations, periods, allowed_mints=train_mints, cutoff=split_at) for periods in neighbor_periods(base)}
    # A candidate that only opens at the final observed point has zero closed
    # evidence. It must not win by avoiding every loss through pending positions.
    viable_training = {
        key: result for key, result in training.items()
        if int(result["metrics"]["closed_trades"]) > 0
    }
    if not viable_training:
        return {
            "state": "waiting", "reason": "no candidate has a closed chronological training trade", "trainTrades": 0,
            "testTrades": 0, "testPF": None, "testNet": None, "maxDrawdown": None,
            "candidate": base, "champion": base,
        }
    selected_key, selected_train = max(viable_training.items(), key=lambda item: float(item[1]["metrics"]["net"]))
    selected = dict(selected_key)
    selected_test = replay(observations, selected, allowed_mints=test_mints)
    champion_test = replay(observations, base, allowed_mints=test_mints)
    selected_metrics = selected_test["metrics"]
    champion_metrics = champion_test["metrics"]
    test_closed = int(selected_metrics["closed_trades"])
    test_pf = selected_metrics["profit_factor"]
    test_net = float(selected_metrics["net"])
    drawdown = float(selected_metrics["max_drawdown"])
    same_as_champion = selected == base
    valid = (
        not same_as_champion
        and test_closed >= 30
        and selected_metrics["expectancy"] is not None
        and float(selected_metrics["expectancy"]) > 0
        and test_pf is not None
        and float(test_pf) > 1.1
        and test_net > float(champion_metrics["net"])
        and drawdown <= 10.0
    )
    if valid:
        state, reason = "promoted", "heldout criteria met"
    elif test_closed < 30:
        state, reason = "waiting", "fewer than 30 closed heldout trades; pending trades excluded"
    elif same_as_champion:
        state, reason = "kept", "champion remains best on chronological training"
    elif selected_metrics["expectancy"] is None or float(selected_metrics["expectancy"]) <= 0:
        state, reason = "kept", "heldout modeled net expectancy is not positive"
    elif test_pf is None or float(test_pf) <= 1.1:
        state, reason = "kept", "heldout profit factor is not above 1.1"
    elif test_net <= float(champion_metrics["net"]):
        state, reason = "kept", "candidate did not improve net versus champion on the same holdout"
    else:
        state, reason = "kept", "candidate heldout peak drawdown exceeds 10%"
    return {
        "state": state,
        "reason": reason,
        "trainTrades": int(selected_train["metrics"]["closed_trades"]),
        "testTrades": test_closed,
        "testPF": test_pf,
        "testNet": round(test_net, 8),
        "maxDrawdown": drawdown,
        "candidate": selected,
        "champion": base,
        "championTestNet": round(float(champion_metrics["net"]), 8),
        "candidatePending": int(selected_metrics["pending_trades"]),
        "splitAt": split_at,
        "cohorts": {"train": len(train_mints), "test": len(test_mints)},
    }
