"""Software-only CAS regressions; every file is under pytest's temporary directory."""
import json
import multiprocessing
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.strategy_config import (
    StrategyConfigError,
    StrategyRevisionConflict,
    default_document,
    load_strategy_config,
    update_strategy_config,
    validate_watch_periods,
)


def _writer(path: str, ready: Any, start: Any, results: Any, index: int) -> None:
    import src.strategy_config as strategy_config

    actual_write = strategy_config._atomic_write

    def slow_write(target: Path, document: dict) -> None:
        time.sleep(0.15)
        actual_write(target, document)

    ready.put(index)
    if not start.wait(10):
        results.put(("error", "start timeout"))
        return
    try:
        with patch.object(strategy_config, "_atomic_write", side_effect=slow_write):
            saved = update_strategy_config(
                {"revision": 0, "watchPeriods": {"short": 30 + index * 15, "medium": 180 + index * 15, "long": 300 + index * 15}},
                path,
            )
        results.put(("saved", saved))
    except StrategyRevisionConflict:
        results.put(("conflict", index))
    except Exception as exc:
        results.put(("error", type(exc).__name__))


def test_six_processes_same_revision_have_exactly_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(default_document()), encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    processes = [context.Process(target=_writer, args=(str(path), ready, start, results, i)) for i in range(6)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            ready.get(timeout=15)
        start.set()
        outcomes = [results.get(timeout=15) for _ in processes]
        assert [kind for kind, _ in outcomes].count("saved") == 1
        assert [kind for kind, _ in outcomes].count("conflict") == 5
        assert not [item for item in outcomes if item[0] == "error"]
        winner = next(value for kind, value in outcomes if kind == "saved")
        assert load_strategy_config(path) == winner
        assert winner["revision"] == 1
        assert winner["max_open"] == 6
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        for process in processes:
            process.join(timeout=3)
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
        ready.close()
        results.close()


def test_replace_failure_preserves_json_and_releases_lock(tmp_path: Path) -> None:
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(default_document()), encoding="utf-8")
    original = path.read_bytes()
    update = {"revision": 0, "watchPeriods": {"short": 30, "medium": 45, "long": 60}}
    with patch("src.strategy_config.os.replace", side_effect=OSError("injected replace failure")):
        with pytest.raises(OSError):
            update_strategy_config(update, path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))
    assert update_strategy_config(update, path)["revision"] == 1


@pytest.mark.parametrize("periods", [
    {}, {"short": 30, "medium": 45},
    {"short": 30, "medium": 45, "long": 60, "extra": 90},
    {"short": 15, "medium": 45, "long": 60},
    {"short": 30, "medium": 45, "long": 1815},
    {"short": 30, "medium": 30, "long": 60},
    {"short": 30, "medium": 60, "long": 60},
    {"short": 60, "medium": 45, "long": 90},
    {"short": 31, "medium": 45, "long": 60},
    {"short": True, "medium": 45, "long": 60},
    {"short": "30", "medium": 45, "long": 60},
    {"short": 30.5, "medium": 45, "long": 60},
    {"short": float("nan"), "medium": 45, "long": 60},
    {"short": 30, "medium": float("inf"), "long": 60},
])
def test_invalid_watch_values_are_rejected_not_clamped(periods: Any) -> None:
    with pytest.raises(StrategyConfigError):
        validate_watch_periods(periods)


@pytest.mark.parametrize("periods", [
    {"short": 30, "medium": 45, "long": 60},
    {"short": 1770, "medium": 1785, "long": 1800},
    {"short": 30.0, "medium": 45.0, "long": 60.0},
])
def test_valid_boundaries_normalize_to_integer_seconds(periods: dict) -> None:
    normalized = validate_watch_periods(periods)
    assert normalized == periods
    assert all(type(value) is int for value in normalized.values())


def test_limit_cannot_be_raised_via_file_or_update(tmp_path: Path) -> None:
    path = tmp_path / "strategy.json"
    document = default_document()
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(StrategyConfigError):
        update_strategy_config({"revision": 0, "watchPeriods": document["watchPeriods"], "max_open": 7}, path)
    document["max_open"] = 7
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(StrategyConfigError):
        load_strategy_config(path)
