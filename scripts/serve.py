#!/usr/bin/env python3
"""Pumpmi's deliberately small HTTP adapter.

The normal dashboard consumes a running live-market paper feed. Offline fixture
simulation remains an isolated test utility, never a live-feed fallback.
It does not expose wallets, keys, live orders, or arbitrary engine configuration.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import json
import mimetypes
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "pump-board-engine"
OUT_DIR = ENGINE_ROOT / "out"
BOARD_PATH = OUT_DIR / "board.json"
TRADES_PATH = OUT_DIR / "trade_charts.json"
FIXTURES_DIR = ENGINE_ROOT / "fixtures"
DIST_DIR = ROOT / "dist"
MAX_BODY_BYTES = 1024

# Serialising the complete fixture run keeps every request and its temporary ledger
# isolated. The adapter does not call the engine's legacy chart builder.
SIMULATION_LOCK = threading.Lock()
OPTIMIZER_LOCK = threading.Lock()
OPTIMIZER_RUNTIME: Any | None = None
OPTIMIZER_RUNTIME_ERROR: str | None = None
LIVE_FEED_LOCK = threading.RLock()
LIVE_FEED_CACHE: tuple[float, dict[str, Any]] | None = None
LIVE_STREAM_SLOTS = threading.BoundedSemaphore(8)


def _durable_optimizer_requested(strategy_config_path: Path | None = None) -> bool:
    """An injected strategy path is an isolated test/dev mode, never production DB."""
    return strategy_config_path is None and bool(os.environ.get("DATABASE_URL"))


def _runtime_for_payload(strategy_config_path: Path | None = None) -> Any | None:
    """Resolve durable runtime only for the process-default strategy path.

    Tests inject a temporary strategy file while DATABASE_URL can still be set by
    the hosting environment. This guard keeps every adapter payload on that path
    from even attempting an optimizer database lookup.
    """
    if strategy_config_path is not None:
        return None
    return optimizer_runtime() if _durable_optimizer_requested() else None


def optimizer_runtime() -> Any | None:
    """Construct the durable runtime once; never turn a DB outage into fixture data."""
    global OPTIMIZER_RUNTIME, OPTIMIZER_RUNTIME_ERROR
    if not _durable_optimizer_requested():
        return None
    with OPTIMIZER_LOCK:
        if OPTIMIZER_RUNTIME is not None or OPTIMIZER_RUNTIME_ERROR is not None:
            return OPTIMIZER_RUNTIME
        try:
            _add_engine_to_path()
            from src.optimizer.runtime import OptimizerRuntime  # type: ignore

            OPTIMIZER_RUNTIME = OptimizerRuntime.from_env()
        except Exception as exc:
            OPTIMIZER_RUNTIME_ERROR = f"{type(exc).__name__}: {str(exc)[:180]}"
        return OPTIMIZER_RUNTIME


def optimizer_status_payload(strategy_config_path: Path | None = None) -> dict[str, Any]:
    """Return status even when the durable store is temporarily unavailable."""
    _add_engine_to_path()
    from src.optimizer.runtime import disabled_status  # type: ignore
    from src.strategy_config import load_strategy_config  # type: ignore

    runtime = _runtime_for_payload(strategy_config_path)
    if runtime is not None:
        try:
            return runtime.status()
        except Exception as exc:
            document = load_strategy_config(strategy_config_path)
            payload = disabled_status(document, error=f"optimizer database unavailable: {type(exc).__name__}: {str(exc)[:160]}")
            payload["enabled"] = True
            payload["runtime"] = "degraded"
            return payload
    if _durable_optimizer_requested(strategy_config_path) and OPTIMIZER_RUNTIME_ERROR:
        payload = disabled_status(load_strategy_config(strategy_config_path), error=f"optimizer database unavailable: {OPTIMIZER_RUNTIME_ERROR}")
        payload["enabled"] = True
        payload["runtime"] = "degraded"
        return payload
    return disabled_status(load_strategy_config(strategy_config_path))


class ApiError(Exception):
    """An expected client-facing API error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _read_json(path: Path) -> Any:
    """Read a committed JSON data file without attempting to repair or rewrite it."""
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def live_feed_payload(*, strategy_config_path: Path | None = None) -> dict[str, Any]:
    """Only current live-engine data; a missing worker never exposes saved snapshots."""
    global LIVE_FEED_CACHE
    runtime = _runtime_for_payload(strategy_config_path)
    if runtime is None or not runtime.enabled:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Live market feed unavailable: enable the paper worker and its durable database. No historical fallback is served.")
    with LIVE_FEED_LOCK:
        if strategy_config_path is None and LIVE_FEED_CACHE is not None and time.monotonic() - LIVE_FEED_CACHE[0] < 5:
            return deepcopy(LIVE_FEED_CACHE[1])
        try:
            payload = runtime.snapshot_payload()
            if payload.get("meta", {}).get("kind") not in {"live", "awaiting", "stale"}:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Live feed returned an invalid source state.")
            payload["strategyConfig"] = strategy_payload(strategy_config_path)
            payload["meta"]["simulationAvailable"] = False
            payload["meta"]["transport"] = "server-sent-events"
            if strategy_config_path is None:
                LIVE_FEED_CACHE = (time.monotonic(), deepcopy(payload))
            return payload
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Live market feed is temporarily disconnected; no saved data fallback is served.") from exc


def snapshot_payload(*, simulation_available: bool = False, strategy_config_path: Path | None = None) -> dict[str, Any]:
    """Legacy compatibility alias for live-only data, never archived board files."""
    return live_feed_payload(strategy_config_path=strategy_config_path)


def strategy_payload(strategy_config_path: Path | None = None) -> dict[str, Any]:
    """Return the effective numeric paper strategy without constructing a Feed."""
    _add_engine_to_path()
    # Importing live_feed defines STRAT and classes. Feed is instantiated only by its
    # explicit main() path, which this adapter never calls.
    from src.live_feed import STRAT  # type: ignore
    from src.strategy_config import effective_strategy, load_strategy_config  # type: ignore

    runtime = _runtime_for_payload(strategy_config_path)
    if runtime is not None:
        document = runtime.config()
        from src.optimizer.policy import RISK_LIMITS, EXIT_RULES, MAX_OPEN  # type: ignore
        strategy = {
            "min_curve_pct": RISK_LIMITS["min_curve_pct"], "max_curve_pct": RISK_LIMITS["max_curve_pct"],
            "max_mcap_usd": RISK_LIMITS["max_mcap_usd"], "p0_top10_max": RISK_LIMITS["top10_max"],
            "p0_dev_max": RISK_LIMITS["dev_max"], "p0_bundle_max": RISK_LIMITS["bundle_max"],
            "size_pct_equity": 1.5, "size_min": 130, "size_max": 160, "loss_streak_halve": 3,
            "stop_loss_pct": EXIT_RULES["stop_loss_pct"], "scale_out_pct": EXIT_RULES["scale_out_pct"],
            "trail_giveback_pct": EXIT_RULES["trail_giveback_pct"],
            "flat_exit_s": EXIT_RULES["soft_exit_seconds"], "hard_exit_s": EXIT_RULES["hard_exit_seconds"],
        }
        strategy.update(
            max_open=MAX_OPEN,
            watch_short_s=document["watchPeriods"]["short"],
            watch_medium_s=document["watchPeriods"]["medium"],
            watch_long_s=document["watchPeriods"]["long"],
        )
    elif _durable_optimizer_requested(strategy_config_path) and OPTIMIZER_RUNTIME_ERROR:
        # A configured durable deployment must not silently serve the local file.
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"optimizer database unavailable: {OPTIMIZER_RUNTIME_ERROR}")
    else:
        document = load_strategy_config(strategy_config_path)
        strategy = effective_strategy(STRAT, strategy_config_path)
    numeric_strategy = {
        name: value for name, value in strategy.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return {
        "strategy": numeric_strategy,
        "watchPeriods": document["watchPeriods"],
        "revision": document["revision"],
        "editable": True,
        "paperOnly": True,
    }


def _add_engine_to_path() -> None:
    engine_path = str(ENGINE_ROOT)
    if engine_path not in sys.path:
        sys.path.insert(0, engine_path)


def _fixture_config(temp_root: Path, strategy_config_path: Path | None = None) -> dict[str, Any]:
    """Construct a fixed paper-only config without reading environment configuration."""
    strategy_config = strategy_payload(strategy_config_path)
    return {
        "mode": "paper",
        "bankroll_usd": 1500,
        "position_usd": 320,
        "goal_multiple": 4.0,
        "take_profit_pct": 100,
        "stop_loss_pct": -30,
        # Fixture Engine has no wall-clock watch loop, but it carries the same
        # immutable capacity and tier metadata that the live feed exposes.
        "max_open": strategy_config["strategy"]["max_open"],
        "watch_periods": strategy_config["watchPeriods"],
        "rules": {
            "max_dev_pct": 10,
            "max_bundle_pct": 25,
            "min_posts_15m": 20,
            "min_sentiment": 0.1,
        },
        "social": {
            "primary": "fixture",
            "fixture_path": str(FIXTURES_DIR / "posts.json"),
        },
        "holders": {
            "source": "fixture",
            "fixture_path": str(FIXTURES_DIR / "coins.json"),
        },
        "paths": {
            "ledger_dir": str(temp_root / "ledger"),
            "board_json": str(temp_root / "board.json"),
            "known_rugs": str(FIXTURES_DIR / "known_rugs.json"),
        },
    }


def _ledger_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _fixture_stats(fills: list[dict[str, str]], skips: list[dict[str, str]], scanned: int) -> dict[str, Any]:
    buy_rows = [row for row in fills if row.get("side") == "BUY"]
    sell_rows = [row for row in fills if row.get("side") == "SELL"]
    pnl_values = [float(row.get("pnl") or 0) for row in sell_rows]
    return {
        "scanned": scanned,
        "buys": len(buy_rows),
        "sells": len(sell_rows),
        "wins": sum(pnl > 0 for pnl in pnl_values),
        "losses": sum(pnl < 0 for pnl in pnl_values),
        "skips": len(skips),
        "realized": round(sum(pnl_values), 2),
    }


def _request_time(timestamp: float) -> str:
    """Format a timestamp captured during this adapter request, in UTC."""
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%H:%M:%S")


def _fixture_log(engine_log: list[tuple[float, str]], fills: list[dict[str, str]]) -> list[str]:
    """Timestamp this request's decisions and factual ledger fills, not market history."""
    decision_log = [f"{_request_time(timestamp)} {message}" for timestamp, message in engine_log]
    ledger_log = [
        "{timestamp} fixture ledger {side} {symbol} price={price} usd={usd} pnl={pnl}".format(
            timestamp=_request_time(float(row["ts"])),
            side=row.get("side", "?"),
            symbol=row.get("symbol", "?"),
            price=row.get("price", "?"),
            usd=row.get("usd", "?"),
            pnl=row.get("pnl", "?"),
        )
        for row in fills
    ]
    return (decision_log + ledger_log)[-60:]


def _annotate_fixture_cards(board: dict[str, Any], holders: Any) -> None:
    """Add an explicit fixture label without treating unit price paths as market caps."""
    coins_by_mint = getattr(holders, "by_mint", {})
    for cards in board.get("columns", {}).values():
        for card in cards:
            coin = coins_by_mint.get(card.get("mint"))
            if not coin:
                continue
            # The fixtures identify coins by symbol, not a market-facing name. The
            # label makes that provenance clear rather than claiming a real token name.
            card.setdefault("name", f"{coin.get('symbol', card.get('symbol', 'coin'))} (fixture)")
            card["fixture"] = True
            # Only copy a market-cap value if a future fixture declares one explicitly.
            # price_path values are unit prices and are never converted to USD mcap.
            for key in ("usd_market_cap", "mcap"):
                value = coin.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    card.setdefault("mcap", value)
                    break


def _fixture_trade_charts(fills_path: Path) -> dict[str, Any]:
    """Serialize completed fixture trades from exact temporary-ledger executions.

    The frontend retains its historic ``mcap`` signal key, but fixture entries mark
    it as a unit-price chart and never synthesize market-cap points or timestamps.
    """
    if not fills_path.exists():
        return {}
    _add_engine_to_path()
    # Reuse only the stable reason-formatting helpers. In particular, do not call
    # trade_charts.build(), which interpolates fixed-size market-cap series.
    from src.trade_charts import gates_buy, label_of, rule_for  # type: ignore

    by_symbol: dict[str, list[dict[str, str]]] = {}
    for row in _ledger_rows(fills_path):
        by_symbol.setdefault(row.get("symbol", ""), []).append(row)

    trades: dict[str, Any] = {}
    for symbol, rows in by_symbol.items():
        buy_index = max((index for index, row in enumerate(rows) if row.get("side") == "BUY"), default=None)
        if buy_index is None:
            continue
        buy = rows[buy_index]
        sells = [row for row in rows[buy_index + 1 :] if row.get("side") == "SELL"]
        # A scale-out alone is an open position, matching the existing closed-trade
        # behavior without adding a made-up closing event.
        if not sells or sells[-1].get("reason", "").startswith("scale-out"):
            continue

        events: list[dict[str, Any]] = []
        for index, row in enumerate([buy, *sells]):
            is_buy = row.get("side") == "BUY"
            kind = "buy" if is_buy else ("scale" if row.get("reason", "").startswith("scale-out") else "sell")
            events.append(
                {
                    "id": "b" if is_buy else f"s{index - 1}",
                    "kind": kind,
                    "ts": float(row["ts"]),
                    "price": float(row["price"]),
                    "usd": float(row["usd"]),
                    "pnl": float(row["pnl"] or 0),
                    "reason": row.get("reason", ""),
                }
            )

        entry = events[0]
        exit_event = events[-1]
        exit_pnl = round(sum(event["pnl"] for event in events[1:]), 2)
        entry_usd = entry["usd"]
        trades[symbol] = {
            "metric": "unit_price",
            # Each series point is one actual ledger fill. Labels and values are not
            # interpolated from fixture price paths or padded to a fixed tick count.
            "series": [{"label": _request_time(event["ts"]), "value": event["price"]} for event in events],
            "signals": [
                {
                    "id": event["id"],
                    "kind": event["kind"],
                    "at": index,
                    "label": label_of(event["kind"], event["reason"]),
                    "t": _request_time(event["ts"]),
                    # Kept for the existing frontend schema; metric declares that
                    # this numeric value is a unit price, not a market cap.
                    "mcap": event["price"],
                    "usd": event["usd"],
                    "pnl": event["pnl"],
                    "reason": event["reason"],
                    "rule": None if event["kind"] == "buy" else rule_for(event["reason"]),
                }
                for index, event in enumerate(events)
            ],
            "gatesBuy": gates_buy(entry["reason"]),
            "gatesSell": [
                {"name": rule_for(event["reason"]), "detail": event["reason"], "pass": True}
                for event in events[1:]
            ],
            "entry": entry["price"],
            "exitPnl": exit_pnl,
            "exitPp": round(exit_pnl / entry_usd * 100, 1) if entry_usd else 0.0,
            "dur": round(max(0.0, exit_event["ts"] - entry["ts"]) / 60, 1),
            "_exitTs": exit_event["ts"],
        }
    return trades


def simulate_payload(strategy_config_path: Path | None = None) -> dict[str, Any]:
    """Run one finite offline Engine pass using only checked-in fixture sources."""
    with SIMULATION_LOCK:
        _add_engine_to_path()
        # These imports intentionally exclude make_holders/make_source and live_feed:
        # FixtureHolders and FixtureSource have no network behavior, and paper mode
        # makes Engine._order persist a fill rather than invoke execute_live.
        from src.engine import Engine  # type: ignore
        from src.holders import FixtureHolders  # type: ignore
        from src.social_crawl import FixtureSource  # type: ignore

        with tempfile.TemporaryDirectory(prefix="pumpmi-fixture-") as temp_dir:
            temp_root = Path(temp_dir)
            strategy_config = strategy_payload(strategy_config_path)
            cfg = _fixture_config(temp_root, strategy_config_path)
            holders = FixtureHolders(cfg["holders"]["fixture_path"])
            social = FixtureSource(cfg["social"]["fixture_path"])
            engine = Engine(cfg, holders, social)
            engine_log: list[tuple[float, str]] = []

            def capture_decision(message: str) -> None:
                engine_log.append((time.time(), message))

            engine.run_feed(holders.new_coins(), log=capture_decision)
            board_path = engine.write_board()

            board = _read_json(board_path)
            fills = _ledger_rows(temp_root / "ledger" / "fills.csv")
            skips = _ledger_rows(temp_root / "ledger" / "skips.csv")
            _annotate_fixture_cards(board, holders)
            board["source"] = "OFFLINE FIXTURES · PAPER TRADES"
            board["stats"] = _fixture_stats(fills, skips, scanned=len(engine.board.cards))
            board["log"] = _fixture_log(engine_log, fills)
            board["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            trades = _fixture_trade_charts(temp_root / "ledger" / "fills.csv")
            return {
                "board": board,
                "trades": trades,
                "meta": {
                    "kind": "fixture",
                    "label": "Fixture simulation · paper trades",
                    "updated": board["updated"],
                    "simulationAvailable": True,
                },
                "strategyConfig": strategy_config,
            }


def _parse_simulate_body(body: bytes) -> None:
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "simulate requires JSON body {}") from exc
    if not isinstance(parsed, dict) or parsed:
        raise ApiError(HTTPStatus.BAD_REQUEST, "simulate accepts only an empty JSON object")


def _parse_strategy_body(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_BODY_BYTES:
        raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "strategy update requires JSON") from exc
    if not isinstance(parsed, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "strategy update requires a JSON object")
    return parsed


def update_strategy_payload(update: dict[str, Any], strategy_config_path: Path | None = None) -> dict[str, Any]:
    """Persist only valid slider values, reporting stale UI writes as conflicts."""
    _add_engine_to_path()
    from src.optimizer.store import RevisionConflict  # type: ignore
    from src.strategy_config import StrategyConfigError, StrategyRevisionConflict, update_strategy_config  # type: ignore

    try:
        runtime = _runtime_for_payload(strategy_config_path)
        if runtime is not None:
            runtime.update_config(update)
        elif _durable_optimizer_requested(strategy_config_path) and OPTIMIZER_RUNTIME_ERROR:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, f"optimizer database unavailable: {OPTIMIZER_RUNTIME_ERROR}")
        else:
            update_strategy_config(update, strategy_config_path)
    except (StrategyRevisionConflict, RevisionConflict) as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    except StrategyConfigError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    return strategy_payload(strategy_config_path)


def dispatch(
    method: str,
    target: str,
    body: bytes = b"",
    *,
    strategy_config_path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """Dispatch API requests independently of HTTP sockets for unit tests."""
    parsed = urlsplit(target)
    if parsed.query:
        raise ApiError(HTTPStatus.BAD_REQUEST, "query parameters are not supported")
    path = parsed.path
    if method == "GET" and path in {"/api/live-feed", "/api/snapshot"}:
        return HTTPStatus.OK, live_feed_payload(strategy_config_path=strategy_config_path)
    if method == "GET" and path == "/api/optimizer":
        return HTTPStatus.OK, optimizer_status_payload(strategy_config_path)
    if method == "GET" and path == "/api/strategy":
        return HTTPStatus.OK, strategy_payload(strategy_config_path)
    if method == "GET" and path == "/health":
        if _durable_optimizer_requested(strategy_config_path):
            # Liveness must not block behind market-data DB writes. Full worker
            # readiness and data freshness remain available at /api/optimizer.
            return HTTPStatus.OK, {"ok": True, "optimizer": "requested" if os.environ.get("PUMPMI_OPTIMIZER_ENABLED") == "1" else "disabled"}
        return HTTPStatus.OK, {"ok": True}
    if method == "POST" and path == "/api/simulate":
        _parse_simulate_body(body)
        return HTTPStatus.OK, simulate_payload(strategy_config_path)
    if method == "PUT" and path == "/api/strategy":
        return HTTPStatus.OK, update_strategy_payload(_parse_strategy_body(body), strategy_config_path)
    raise ApiError(HTTPStatus.NOT_FOUND, "not found")


def _static_path(request_path: str) -> Path | None:
    """Find an asset in dist, otherwise return the SPA entry point without traversal."""
    if not DIST_DIR.is_dir():
        return None
    root = DIST_DIR.resolve()
    requested = unquote(request_path).lstrip("/")
    candidate = (root / requested).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.is_file():
        return candidate
    index = root / "index.html"
    return index if index.is_file() else None


class PumpmiHandler(BaseHTTPRequestHandler):
    server_version = "PumpmiAdapter/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep normal request logs useful while avoiding request bodies or environment data.
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _send_json(self, status: int, payload: dict[str, Any], *, head_only: bool = False) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _send_api(self, *, head_only: bool = False) -> None:
        try:
            body = self._read_request_body() if self.command in ("POST", "PUT") else b""
            status, payload = dispatch(self.command, self.path, body)
            self._send_json(status, payload, head_only=head_only)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message}, head_only=head_only)
        except Exception:
            self.log_error("unhandled adapter error")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"}, head_only=head_only)

    def _send_live_stream(self) -> None:
        if not LIVE_STREAM_SLOTS.acquire(blocking=False):
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Live stream capacity reached. Reconnect shortly."})
            return
        try:
            runtime = _runtime_for_payload()
            if runtime is None or not runtime.enabled:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Live market worker is unavailable; no snapshot fallback."})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n: live feed connected; awaiting verified price frame\n\n")
            self.wfile.flush()
            previous = None
            while True:
                try:
                    frame = live_feed_payload()
                    encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
                    if encoded != previous:
                        self.wfile.write(f"event: feed\ndata: {encoded}\n\n".encode("utf-8"))
                        previous = encoded
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except ApiError:
                    self.wfile.write(b'event: feed-error\ndata: {"error":"Live feed disconnected; reconnecting without historical data."}\n\n')
                    self.wfile.flush()
                    return
                time.sleep(5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            LIVE_STREAM_SLOTS.release()

    def _read_request_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "chunked request bodies are not supported")
        value = self.headers.get("Content-Length")
        if value is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        try:
            size = int(value)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from exc
        if size < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if size > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
        return self.rfile.read(size)

    def _send_static(self, *, head_only: bool = False) -> None:
        request_path = urlsplit(self.path).path
        asset = _static_path(request_path)
        if asset is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head_only=head_only)
            return
        data = asset.read_bytes()
        content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/live-feed/events":
            self._send_live_stream()
        elif path.startswith("/api/") or path == "/health":
            self._send_api()
        else:
            self._send_static()

    def do_POST(self) -> None:  # noqa: N802
        self._send_api()

    def do_PUT(self) -> None:  # noqa: N802
        self._send_api()

    def do_HEAD(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/api/") or path == "/health":
            self._send_api(head_only=True)
        else:
            self._send_static(head_only=True)


def development_port() -> int:
    """Use 3001 without a built SPA; a built dist is the production start path."""
    if os.environ.get("PUMPMI_ENV", "").lower() == "production" or DIST_DIR.is_dir():
        return 3000
    return 3001


def worker_enabled() -> bool:
    """Separate a live dashboard replica from its continuous worker host."""
    return os.environ.get("PUMPMI_WORKER_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", str(development_port())))
    except ValueError as exc:
        raise SystemExit("PORT must be an integer") from exc
    worker = None
    runtime = optimizer_runtime()
    if runtime is not None and runtime.enabled and worker_enabled():
        from src.optimizer.runtime import Worker  # type: ignore

        worker = Worker(runtime)
        worker.start()
        print(f"Pumpmi optimizer worker requested (boot={runtime.boot_id})", flush=True)
    elif runtime is not None and runtime.enabled:
        print("Pumpmi dashboard replica reading the shared live paper feed", flush=True)
    elif OPTIMIZER_RUNTIME_ERROR:
        print(f"Pumpmi optimizer unavailable: {OPTIMIZER_RUNTIME_ERROR}", file=sys.stderr, flush=True)
    server = ThreadingHTTPServer((host, port), PumpmiHandler)
    print(f"Pumpmi adapter listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if worker is not None:
            worker.stop()
            worker.join(timeout=5)


if __name__ == "__main__":
    main()
