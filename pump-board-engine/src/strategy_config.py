"""Validated, local-only strategy watch-period configuration.

This module deliberately has no environment, network, or current-working-directory
inputs.  The checked-in ``strategy.json`` beside the engine is the production
configuration; callers may pass a path explicitly for isolated tests.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator, Mapping

ENGINE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ENGINE_ROOT / "strategy.json"
WATCH_KEYS = ("short", "medium", "long")
DEFAULT_WATCH_PERIODS = {"short": 90, "medium": 180, "long": 300}
DEFAULT_DOCUMENT = {
    "version": 1,
    "revision": 0,
    "watchPeriods": DEFAULT_WATCH_PERIODS,
    "max_open": 6,
}


class StrategyConfigError(ValueError):
    """The on-disk document or a requested update is not valid."""


class StrategyRevisionConflict(StrategyConfigError):
    """The caller attempted to save against an older configuration revision."""


def config_path(path: str | Path | None = None) -> Path:
    """Resolve an optional injected path without consulting the current directory."""
    return Path(path) if path is not None else DEFAULT_CONFIG_PATH


def default_document() -> dict[str, Any]:
    """Return a new copy suitable for seeding an isolated test configuration."""
    return deepcopy(DEFAULT_DOCUMENT)


def _is_integer(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def validate_watch_periods(watch_periods: Any) -> dict[str, int]:
    """Validate the one intentionally editable part of the strategy."""
    if not isinstance(watch_periods, dict) or set(watch_periods) != set(WATCH_KEYS):
        raise StrategyConfigError("watchPeriods must contain only short, medium, and long")
    normalized: dict[str, int] = {}
    for tier in WATCH_KEYS:
        value = watch_periods[tier]
        if not _is_integer(value):
            raise StrategyConfigError(f"watchPeriods.{tier} must be an integer")
        value = int(value)
        if value % 15:
            raise StrategyConfigError(f"watchPeriods.{tier} must be a multiple of 15 seconds")
        normalized[tier] = value
    short, medium, long = (normalized[tier] for tier in WATCH_KEYS)
    if not (30 <= short < medium < long <= 1800):
        raise StrategyConfigError("watchPeriods must satisfy 30 <= short < medium < long <= 1800")
    return normalized


def _validate_document(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"version", "revision", "watchPeriods", "max_open"}:
        raise StrategyConfigError("strategy configuration has an invalid schema")
    if raw["version"] != 1:
        raise StrategyConfigError("strategy configuration version must be 1")
    if not _is_integer(raw["revision"]) or raw["revision"] < 0:
        raise StrategyConfigError("strategy configuration revision must be a non-negative integer")
    # Position sizing is deliberately not an API-editable parameter.  Keep the
    # invariant in the stored document too, so a bad file cannot silently lift it.
    if raw["max_open"] != 6:
        raise StrategyConfigError("strategy configuration max_open must be fixed at 6")
    return {
        "version": 1,
        "revision": int(raw["revision"]),
        "watchPeriods": validate_watch_periods(raw["watchPeriods"]),
        "max_open": 6,
    }


def load_strategy_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the strategy document from its deterministic location."""
    target = config_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StrategyConfigError(f"strategy configuration is missing: {target}") from exc
    except json.JSONDecodeError as exc:
        raise StrategyConfigError("strategy configuration is not valid JSON") from exc
    return _validate_document(raw)


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    """Replace one configuration file atomically without leaving partial JSON behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def _config_write_lock(target: Path) -> Iterator[None]:
    """Lock a stable sibling file, not the JSON inode replaced by os.replace.

    Separate handles contend across both threads and processes. Readers continue
    to see complete JSON; only the read/check/replace write sequence is serialized.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def update_strategy_config(update: Any, path: str | Path | None = None) -> dict[str, Any]:
    """Apply a strict optimistic-concurrency update and atomically persist it.

    Only the slider values and the revision are accepted.  The fixed six-position
    cap is never copied from a request body. A stable advisory lock makes the
    revision check plus replacement one cross-process compare-and-swap.
    """
    if not isinstance(update, dict) or set(update) != {"watchPeriods", "revision"}:
        raise StrategyConfigError("strategy update accepts only watchPeriods and revision")
    if not _is_integer(update["revision"]) or update["revision"] < 0:
        raise StrategyConfigError("revision must be a non-negative integer")
    watch_periods = validate_watch_periods(update["watchPeriods"])
    target = config_path(path)
    revision = int(update["revision"])
    with _config_write_lock(target):
        current = load_strategy_config(target)
        if revision != current["revision"]:
            raise StrategyRevisionConflict("strategy revision conflict")
        next_document = {
            "version": 1,
            "revision": current["revision"] + 1,
            "watchPeriods": watch_periods,
            "max_open": 6,
        }
        _atomic_write(target, next_document)
        return next_document


def effective_strategy(base_strategy: Mapping[str, Any], path: str | Path | None = None) -> dict[str, Any]:
    """Overlay persisted watch periods and fixed capacity onto numeric feed defaults."""
    document = load_strategy_config(path)
    effective = dict(base_strategy)
    effective.update(
        max_open=document["max_open"],
        watch_short_s=document["watchPeriods"]["short"],
        watch_medium_s=document["watchPeriods"]["medium"],
        watch_long_s=document["watchPeriods"]["long"],
    )
    return effective
