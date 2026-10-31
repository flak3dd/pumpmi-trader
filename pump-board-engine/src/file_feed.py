"""Read-only view of the file-backed paper worker.

The worker is ``python -m src.live_feed``. It publishes ``out/board.json`` and
``out/trade_charts.json`` and records its pid in ``out/live_feed.pid``. This
module never starts that process, never reads fixtures, and never places orders.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

PID_FILENAME = "live_feed.pid"
BOARD_FILENAME = "board.json"
TRADES_FILENAME = "trade_charts.json"
# Same window the durable optimizer uses for a verified observation.
FRESH_SECONDS = 180
# A file or timestamp a couple of seconds ahead is clock skew, not a future quote.
CLOCK_SKEW_SECONDS = 5

_LANES = ("checking", "holding", "done")


class FileFeedUnavailable(Exception):
    """The file worker is not an acceptable live source. Callers should return 503."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def read_pid(path: Path) -> int | None:
    """Return a positive pid from a pid file, or None when the file is unusable."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text.split()[0])
    except (ValueError, IndexError):
        return None
    if pid <= 0:
        return None
    return pid


def command_line(pid: int) -> str:
    """Best-effort command line for a pid. Empty when it cannot be read."""
    if pid <= 0:
        return ""
    proc_path = Path(f"/proc/{pid}/cmdline")
    try:
        raw = proc_path.read_bytes()
    except OSError:
        raw = b""
    if raw:
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip()


def command_is_live_feed(command: str) -> bool:
    """True for ``python -m src.live_feed`` or an explicit ``live_feed.py`` entrypoint."""
    for token in command.split():
        normalized = token.replace("\\", "/").rstrip("/")
        if normalized in {"src.live_feed", "live_feed.py"}:
            return True
        if normalized.endswith("/live_feed.py") or normalized.endswith("/src.live_feed"):
            return True
        if normalized.endswith(".live_feed") and "src.live_feed" in normalized:
            return True
    return False


def process_is_live_feed(pid: int) -> bool:
    """True when ``pid`` is alive and its command line is the file-backed paper worker."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return command_is_live_feed(command_line(pid))


def publish_pid(out_dir: Path, *, pid: int | None = None) -> None:
    """Record this process as the file worker. Refuse to replace a different live worker."""
    pid = os.getpid() if pid is None else pid
    path = Path(out_dir) / PID_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_pid(path)
    if current and current != pid and process_is_live_feed(current):
        raise RuntimeError(f"file-backed paper feed already running (pid {current})")
    temporary = path.with_name(f"{PID_FILENAME}.tmp")
    temporary.write_text(f"{pid}\n", encoding="utf-8")
    temporary.replace(path)


def refresh_pid(out_dir: Path, *, pid: int | None = None) -> None:
    """Rewrite our pid file. A different live worker keeps its own file."""
    try:
        publish_pid(out_dir, pid=pid)
    except RuntimeError:
        return


def release_pid(out_dir: Path, *, pid: int | None = None) -> None:
    """Remove the pid file only when it still names this process."""
    pid = os.getpid() if pid is None else pid
    path = Path(out_dir) / PID_FILENAME
    if read_pid(path) != pid:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _age_seconds(path: Path, now: float) -> float | None:
    try:
        modified = path.stat().st_mtime
    except OSError:
        return None
    return now - modified


def _published_recently(path: Path, now: float) -> bool:
    age = _age_seconds(path, now)
    return age is not None and -CLOCK_SKEW_SECONDS <= age <= FRESH_SECONDS


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _looks_like_fixture(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    source = str(value.get("source") or "")
    upper = source.upper()
    if "FIXTURE" in upper or "OFFLINE" in upper:
        return True
    if value.get("fixture") is True or value.get("metric") == "unit_price":
        return True
    columns = value.get("columns")
    if isinstance(columns, dict):
        for cards in columns.values():
            if isinstance(cards, list) and any(isinstance(card, dict) and card.get("fixture") is True for card in cards):
                return True
    return False


def _load_trades(path: Path, now: float) -> dict[str, Any]:
    """Use trade charts only when that file was published as recently as the board."""
    if not _published_recently(path, now):
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or _looks_like_fixture(payload):
        return {}
    for trade in payload.values():
        if isinstance(trade, dict) and (trade.get("fixture") is True or trade.get("metric") == "unit_price"):
            return {}
    return payload


def _classify(timestamp: float | None, now: float) -> tuple[str, str]:
    if timestamp is None:
        return "awaiting", "Awaiting first verified live-market observation"
    age = now - timestamp
    if age > FRESH_SECONDS:
        raise FileFeedUnavailable("File-backed paper board is stale. No historical fallback is served.")
    if age < -CLOCK_SKEW_SECONDS:
        return "stale", "Live-market collection interrupted or stale"
    return "live", "Verified live-market paper board"


def load_live_payload(out_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Return a frontend live frame, or raise when the worker or board is not current.

    A stopped worker and a board older than 180 seconds are equally unavailable.
    The checked-in archive and fixture files are never substituted.
    """
    now = time.time() if now is None else float(now)
    root = Path(out_dir)
    pid = read_pid(root / PID_FILENAME)
    if pid is None or not process_is_live_feed(pid):
        raise FileFeedUnavailable("File-backed paper worker is not running. No historical fallback is served.")
    board_path = root / BOARD_FILENAME
    if not _published_recently(board_path, now):
        raise FileFeedUnavailable("File-backed paper board is missing or stale. No historical fallback is served.")
    try:
        board = json.loads(board_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FileFeedUnavailable("Live board could not be read. No historical fallback is served.") from exc
    if not isinstance(board, dict) or _looks_like_fixture(board):
        raise FileFeedUnavailable("Fixture data is not a live feed. No historical fallback is served.")
    if board.get("mode") != "paper":
        raise FileFeedUnavailable("Live feed refused a non-paper board. No historical fallback is served.")
    bankroll = _number(board.get("bankroll"))
    equity = _number(board.get("equity"))
    columns = board.get("columns")
    if bankroll is None or equity is None or not isinstance(columns, dict):
        raise FileFeedUnavailable("Live board is incomplete. No historical fallback is served.")
    if any(not isinstance(columns.get(lane), list) for lane in _LANES):
        raise FileFeedUnavailable("Live board is incomplete. No historical fallback is served.")
    raw_ts = board.get("ts")
    if raw_ts is not None and _number(raw_ts) is None:
        raise FileFeedUnavailable("Live board timestamp is invalid. No historical fallback is served.")
    kind, label = _classify(None if raw_ts is None else _number(raw_ts), now)
    trades = _load_trades(root / TRADES_FILENAME, now)
    frame = dict(board)
    frame["mode"] = "paper"
    frame["bankroll"] = bankroll
    frame["equity"] = equity
    frame["columns"] = {lane: list(columns[lane]) for lane in _LANES}
    return {
        "board": frame,
        "trades": trades,
        "meta": {
            "kind": kind,
            "label": label,
            "updated": board.get("updated"),
            "simulationAvailable": False,
        },
    }
