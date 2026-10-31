"""Pump-capture paper mode (``pump-capture-v1``). Paper only: no orders, no wallet, no signing.

A fast loop (about every 4 s) reads the public pump.fun listing and one batched
``getMultipleAccounts`` of verified bonding-curve reserves and creator token
accounts for a bounded hot set. Entry and exit rules mirror the pre-registered
research backtest (``pump-capture-spec.md``) so live paper results can be
compared with the recorded test. Decisions made on one read fill at the *next*
verified read, against that read's reserves with constant-product impact, the
1.25% bonding-curve fee and a network cost. A decision never fills at the price
that triggered it.

Pump mode substitutes the unavailable top-10 holder check (user-selected
option B) with verified on-chain checks: the creator's own balance, whether the
creator has sold, and how much supply was taken at launch. That substitution is
labelled everywhere it appears. Missing data fails closed.
"""
from __future__ import annotations

import base64
import http.client
import json
import math
import os
import re
import struct
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from ..curve import flat_round_trip_pct
from ..solana_pda import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, associated_token_address
from .source import PUMP_FRONTEND_V3, SOLANA_RPC, USER_AGENT, SourceError, _rpc_json, _rpc_value, _url_json

PUMP_STRATEGY = "pump-capture-v1"
PUMP_MODES = ("off", "radar", "paper")
PUMP_SECONDS = 4.0
DISCLAIMER = (
    "Paper only. Pump readiness is the share of pump-mode gates currently passing, not a probability of a "
    "pump, a fill or a profit. Holder concentration is substituted by verified creator and launch checks."
)
HOLDER_SUBSTITUTE_LABEL = "Top-10 holder check substituted in pump mode: creator balance, creator selling and launch take (verified on-chain)"

# Costs and sizing (spec v1 + addendum A)
FEE = 0.0125                 # pump.fun bonding-curve fee per side
NETWORK_USD = 0.05           # per transaction
TICKET_USD = 65.0            # half of Pumpmi's $130 base
IMPACT_CHECK_USD = 160.0     # Pumpmi's existing impact check size
IMPACT_MAX_DRAG_PCT = -10.0
MISS_SECONDS = 20.0          # an entry with no verified read this long after decision is missed
FRESH_SECONDS = 10.0         # a decision needs a verified read at most this old
MAX_OPEN = 6
BOOK_BANKROLL = 1500.0
BOOK_MAX_DRAWDOWN_PCT = 10.0

# Hot set (mirrors the research recorder)
LISTING_LIMIT = 50
HOT_MAX = 30
ADMIT_MIN_USD = 4_000.0
IDLE_EVICT_SECONDS = 90.0
HISTORY_SECONDS = 600.0
CHAIN_PREFERENCE_SECONDS = 10.0
LAUNCH_AGE_MAX_SECONDS = 20.0
LAUNCH_READS_PER_CYCLE = 20
PRIMARY_COOLDOWN_SECONDS = 300.0
# pump.fun listing budget: shared with the main optimizer's feed from the same IP, so keep it small.
# Created coins every 3rd cycle (~12 s, keeps launches inside the 20 s window), latest trades every
# 6th cycle (~24 s); never both in one cycle: ~7.5 requests/minute. Verified reserve reads stay every cycle.
LISTING_CREATED_EVERY = 3
LISTING_TRADES_EVERY = 6
LISTING_FRESH_SECONDS = 120.0   # entries need a listing (and so SOL/USD) at most this old
LISTING_BACKOFF_BASE_SECONDS = 60.0   # after HTTP 403/429: 60 s, doubling ...
LISTING_BACKOFF_MAX_SECONDS = 900.0   # ... up to 15 minutes
LISTING_ERROR_PAUSE_SECONDS = 30.0    # other listing errors

# Option-B substitutes for the holder gate
CREATOR_MAX_PCT = 5.0
CREATOR_SELL_PCT = 10.0
LAUNCH_TAKE_MAX_PCT = 25.0

SUPPLY_DEFAULT = 1_000_000_000_000_000
REAL_INIT = 793_100_000_000_000
K_DEFAULT = 30_000_000_000 * 1_073_000_000_000_000
OFFSET_DEFAULT = 1_073_000_000_000_000 - REAL_INIT
SOL_QUOTES = (None, "", "11111111111111111111111111111111", "So11111111111111111111111111111111111111112")

EXIT_SETS: dict[str, dict[str, Any]] = {
    "X1": dict(stop=-15.0, fail_drop=8.0, fail_peak=2.0, lock_arm=8.0, lock_give=6.0, scale_at=25.0, trail=12.0,
               soft_s=12 * 60, soft_below=5.0, hard_s=40 * 60, flatten=95.0, flow_flip=False),
    "X2": dict(stop=-12.0, fail_drop=None, fail_peak=None, lock_arm=None, lock_give=None, scale_at=20.0, trail=8.0,
               soft_s=4 * 60, soft_below=5.0, hard_s=10 * 60, flatten=95.0, flow_flip=True),
}
ENTRY_SIGNALS = ("E1", "E2", "E3")
DEFAULT_VARIANT = {"entry": "E2", "exit": "X1"}  # pre-registered selection rule (5 s latency holdout), see README


# ---------------------------------------------------------------- maths
def buy_tokens(usd: float, vs: float, vt: float, sol_usd: float, fee: float = FEE) -> float:
    """Raw tokens out for ``usd`` spent (fee taken from the SOL input)."""
    to_curve = usd / sol_usd * 1e9 / (1.0 + fee)
    return vt * to_curve / (vs + to_curve)


def sell_value(tokens: float, vs: float, vt: float, sol_usd: float, fee: float = FEE) -> float:
    """USD received for selling ``tokens`` into the curve, after the fee."""
    gross = vs * tokens / (vt + tokens)
    return gross * (1.0 - fee) / 1e9 * sol_usd


def sell_gross(tokens: float, vs: float, vt: float, sol_usd: float) -> float:
    return vs * tokens / (vt + tokens) / 1e9 * sol_usd


def creator_sold(amount: int | None, peak: int | None) -> bool | None:
    if amount is None or peak is None:
        return None
    return peak > 0 and amount < (1.0 - CREATOR_SELL_PCT / 100.0) * peak


# ---------------------------------------------------------------- signals (mirror pump_backtest.signal)
def _at_or_before(times: list[float], t: float) -> int:
    lo, hi = 0, len(times)
    while lo < hi:
        mid = (lo + hi) // 2
        if times[mid] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def _first_at_or_after(times: list[float], t: float) -> int:
    lo, hi = 0, len(times)
    while lo < hi:
        mid = (lo + hi) // 2
        if times[mid] < t:
            lo = mid + 1
        else:
            hi = mid
    return lo


def entry_signal(times: list[float], prices: list[float], curves: list[float], i: int, name: str) -> bool:
    """Exactly the pre-registered E1/E2/E3 rules on a price path ending at index ``i``."""
    t, p, c = times[i], prices[i], curves[i]
    if c > 85.0:
        return False
    j30 = _at_or_before(times, t - 30)
    lo30 = _first_at_or_after(times, t - 30)
    hi30 = max(prices[lo30:i + 1])
    if name == "E1":
        if j30 < 0 or not (5_000 <= p <= 60_000):
            return False
        ups = sum(1 for k in range(max(j30 + 1, 1), i + 1) if prices[k] > prices[k - 1])
        return p >= 1.20 * prices[j30] and ups >= 3 and p >= 0.92 * hi30
    if name == "E2":
        if j30 < 0 or not (5_000 <= p <= 150_000):
            return False
        j180 = _first_at_or_after(times, t - 180)
        if j180 > j30:
            return False
        prior_high = max(prices[j180:j30 + 1])
        return p >= 1.10 * prior_high and p >= 1.10 * prices[j30]
    if name == "E3":
        j60 = _at_or_before(times, t - 60)
        if j60 < 0 or not (50.0 <= c <= 85.0):
            return False
        return c - curves[j60] >= 8.0 and p >= 0.92 * hi30
    raise ValueError(f"unknown pump entry signal {name}")


def momentum_30s(times: list[float], prices: list[float]) -> float | None:
    if not times:
        return None
    j30 = _at_or_before(times, times[-1] - 30)
    if j30 < 0 or prices[j30] <= 0:
        return None
    return 100.0 * (prices[-1] / prices[j30] - 1.0)


def exit_step(pos: dict[str, Any], point: dict[str, Any], prices: list[float], k: int, rules: dict[str, Any],
              sold: bool | None, fee: float = FEE) -> tuple[str, str] | None:
    """One exit evaluation on a verified point; mirrors pump_backtest.simulate_trade's loop body.

    Mutates the position's running peaks. Returns ("exit"|"scale", reason) or None.
    """
    state = pos["pump"]
    age = float(point["t"]) - float(state["entry_t"])
    value = sell_value(float(state["tokens"]), point["vs"], point["vt"], point["sol"], fee)
    cost = float(state["cost_usd"])
    net = 100.0 * (float(state["realized_usd"]) + value - NETWORK_USD - cost) / cost
    spot = 100.0 * (float(point["mcap"]) / float(state["entry_spot"]) - 1.0)
    state["peak_net"] = max(float(state["peak_net"]), net)
    state["peak_spot"] = max(float(state["peak_spot"]), spot)
    state["last_net"] = net
    peak_net, peak_spot, scaled = float(state["peak_net"]), float(state["peak_spot"]), bool(pos.get("scaled"))
    why = None
    if sold:
        why = "creator sold ≥10% of peak balance"
    elif net <= rules["stop"]:
        why = f"stop {net:.1f}% net"
    elif rules["fail_drop"] is not None and peak_spot < rules["fail_peak"] and spot <= -rules["fail_drop"]:
        why = f"failed impulse {spot:.1f}% spot"
    elif rules["lock_arm"] is not None and not scaled and peak_net >= rules["lock_arm"] and net <= peak_net - rules["lock_give"] \
            and peak_net < rules["scale_at"]:
        why = f"profit lock {net:.1f}% (peak {peak_net:.1f}%)"
    elif scaled and net <= peak_net - rules["trail"]:
        why = f"trail {rules['trail']:g} pts {net:.1f}% (peak {peak_net:.1f}%)"
    elif float(point["curve"]) >= rules["flatten"] or point.get("complete"):
        why = f"flatten curve {float(point['curve']):.1f}%"
    elif age >= rules["hard_s"]:
        why = f"hard time {age / 60:.0f}m {net:.1f}%"
    elif age >= rules["soft_s"] and net < rules["soft_below"]:
        why = f"soft time {age / 60:.0f}m {net:.1f}%"
    elif rules["flow_flip"] and k >= 3 and all(prices[m] < prices[m - 1] for m in (k, k - 1, k - 2)) \
            and prices[k] <= 0.94 * prices[k - 3]:
        why = f"flow flip {100 * (prices[k] / prices[k - 3] - 1):.1f}% over 3 moves"
    if why is None and not scaled and net >= rules["scale_at"]:
        return ("scale", f"scale half {net:.1f}% net")
    return ("exit", why) if why else None


# ---------------------------------------------------------------- account parsing
def _b64(data: Any) -> bytes | None:
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, str):
        return None
    try:
        return base64.b64decode(data)
    except Exception:
        return None


def parse_curve_full(data: Any) -> dict[str, Any] | None:
    raw = _b64(data)
    if raw is None or len(raw) < 49:
        return None
    vt, vs, rt, rs, supply = struct.unpack_from("<QQQQQ", raw, 8)
    if vt <= 0 or vs <= 0:
        return None
    return {"vt": int(vt), "vs": int(vs), "rt": int(rt), "rs": int(rs), "supply": int(supply) or SUPPLY_DEFAULT,
            "complete": bool(raw[48])}


def parse_token_amount(data: Any) -> int | None:
    raw = _b64(data)
    if raw is None or len(raw) < 72:
        return None
    return int.from_bytes(raw[64:72], "little")


# ---------------------------------------------------------------- RPC with public fallback
def _public_rpc_json(payload: dict[str, Any], *, timeout: float = 6.0) -> Any:
    request = urllib.request.Request(SOLANA_RPC, data=json.dumps(payload).encode("utf-8"),
                                     headers={**USER_AGENT, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"public RPC HTTP {exc.code}") from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise SourceError(f"public RPC unavailable ({type(exc).__name__})") from exc


class FallbackRpc:
    """Primary (configured) RPC with a cooldown breaker and the public endpoint as fallback.

    Errors are reduced to labels; provider text is never surfaced.
    """

    def __init__(self, *, primary: Callable[[dict[str, Any]], Any] | None, public: Callable[[dict[str, Any]], Any],
                 clock: Callable[[], float] = time.time) -> None:
        self.primary = primary
        self.public = public
        self.clock = clock
        self.primary_down_until = 0.0
        self.last_route = "none"
        self.last_error: str | None = None

    def call(self, payload: dict[str, Any], method: str) -> Any:
        errors = []
        if self.primary is not None and self.clock() >= self.primary_down_until:
            try:
                value = _rpc_value(self.primary(payload), method, label="pump")
                self.last_route, self.last_error = "primary", None
                return value
            except SourceError as exc:
                self.primary_down_until = self.clock() + PRIMARY_COOLDOWN_SECONDS
                errors.append(f"primary: {str(exc)[:80]}")
        try:
            value = _rpc_value(self.public(payload), method, label="pump")
            self.last_route = "public fallback" if self.primary is not None else "public"
            self.last_error = "; ".join(errors) or None
            return value
        except SourceError as exc:
            errors.append(f"public: {str(exc)[:80]}")
            self.last_route, self.last_error = "failed", "; ".join(errors)
            raise SourceError("pump verified read failed on every route") from exc


# ---------------------------------------------------------------- market state
@dataclass
class TokenMeta:
    mint: str
    curve: str
    symbol: str
    name: str
    creator: str | None
    token_program: str | None
    created_ms: float | None
    quote_mint: str | None
    creator_ata: str | None = None


@dataclass
class PumpBatch:
    at: float
    points: dict[str, dict[str, Any]] = field(default_factory=dict)     # verified reads this cycle
    error: str | None = None
    route: str = "none"
    listing_rows: int = 0
    hot: int = 0
    launch_reads: int = 0


class PumpMarket:
    """In-memory observational state for the fast loop (rebuilds within minutes after a restart)."""

    def __init__(self, *, http_get: Callable[[str], Any] | None = None, rpc: FallbackRpc | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.http_get = http_get or (lambda url: _url_json(url, timeout=4.0))
        primary = (lambda payload: _rpc_json(payload, timeout=4.0)) if os.environ.get("SOLANA_RPC_URL", "").strip() else None
        self.rpc = rpc or FallbackRpc(primary=primary, public=lambda payload: _public_rpc_json(payload, timeout=6.0), clock=clock)
        self.clock = clock
        self.meta: dict[str, TokenMeta] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.k: dict[str, float] = {}
        self.offset: dict[str, float] = {}
        self.supply: dict[str, int] = {}
        self.creator: dict[str, dict[str, Any]] = {}
        self.launch: dict[str, dict[str, Any]] = {}
        self.hot: "OrderedDict[str, float]" = OrderedDict()
        self.queue: "OrderedDict[str, float]" = OrderedDict()
        self.retired: "OrderedDict[str, float]" = OrderedDict()
        self.last_change: dict[str, float] = {}
        self.sol_usd: float | None = None
        self.cycle = 0
        self.listing_ok_at: float | None = None
        self.listing_failures = 0
        self.listing_pause_until = 0.0
        self.listing_note: str | None = None
        self.listing_requests = 0

    # -- listing
    def _listing(self, sort: str) -> list[dict[str, Any]]:
        url = f"{PUMP_FRONTEND_V3}/coins?offset=0&limit={LISTING_LIMIT}&sort={sort}&order=DESC&includeNsfw=false"
        self.listing_requests += 1
        payload = self.http_get(url)
        if not isinstance(payload, list):
            raise SourceError("pump listing returned a non-list payload")
        return [row for row in payload if isinstance(row, dict) and row.get("mint")]

    def _listing_due(self, now: float) -> str | None:
        """At most one listing request per cycle, inside the budget and outside any backoff."""
        if now < self.listing_pause_until:
            return None
        if self.listing_ok_at is None or self.cycle % LISTING_TRADES_EVERY == 1:
            return "last_trade_timestamp"
        if self.cycle % LISTING_CREATED_EVERY == 0:
            return "created_timestamp"
        return None

    def listing_age(self, now: float) -> float | None:
        return None if self.listing_ok_at is None else max(0.0, now - self.listing_ok_at)

    def _absorb_listing(self, rows: list[dict[str, Any]], now: float, sort: str) -> None:
        for row in rows:
            mint = str(row["mint"])
            try:
                usd = float(row.get("usd_market_cap") or 0)
                sol_mcap = float(row.get("market_cap") or 0)
            except (TypeError, ValueError):
                continue
            if usd > 0 and sol_mcap > 0:
                self.sol_usd = usd / sol_mcap
            if mint not in self.meta and row.get("bonding_curve") and not row.get("complete"):
                program = row.get("token_program")
                program = program if program in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID) else None
                meta = TokenMeta(mint=mint, curve=str(row["bonding_curve"]), symbol=str(row.get("symbol") or "")[:24],
                                 name=str(row.get("name") or "")[:64], creator=row.get("creator"), token_program=program,
                                 created_ms=float(row["created_timestamp"]) if row.get("created_timestamp") else None,
                                 quote_mint=row.get("quote_mint"))
                if meta.creator and meta.token_program:
                    try:
                        meta.creator_ata = associated_token_address(str(meta.creator), mint, meta.token_program)
                    except ValueError:
                        meta.creator_ata = None
                self.meta[mint] = meta
            meta = self.meta.get(mint)
            if meta is None:
                continue
            if sort == "created_timestamp" and meta.created_ms and mint not in self.launch:
                age = now - meta.created_ms / 1000.0
                if 0 <= age <= LAUNCH_AGE_MAX_SECONDS:
                    self.launch[mint] = {"status": "queued", "queued_at": now}
            if sort == "last_trade_timestamp" and sol_mcap > 0 and not row.get("complete") and self.sol_usd:
                self._api_point(mint, now, sol_mcap)
            if (not row.get("complete") and usd >= ADMIT_MIN_USD and meta.quote_mint in SOL_QUOTES
                    and mint not in self.hot and mint not in self.queue and mint not in self.retired):
                self.queue[mint] = now

    def _api_point(self, mint: str, now: float, sol_mcap: float) -> None:
        pts = self.history.setdefault(mint, [])
        if pts and any(p["src"] == "chain" and now - p["t"] < CHAIN_PREFERENCE_SECONDS for p in pts[-4:]):
            return
        k = self.k.get(mint, K_DEFAULT)
        offset = self.offset.get(mint, OFFSET_DEFAULT)
        supply = self.supply.get(mint, SUPPLY_DEFAULT)
        price = sol_mcap * 1e9 / supply
        vs, vt = math.sqrt(k * price), math.sqrt(k / price)
        self._append(mint, {"t": now, "vs": vs, "vt": vt, "mcap": sol_mcap * float(self.sol_usd or 0), "sol": float(self.sol_usd or 0),
                            "curve": max(0.0, min(100.0, 100.0 * (REAL_INIT - (vt - offset)) / REAL_INIT)),
                            "complete": False, "src": "api"})

    def _append(self, mint: str, point: dict[str, Any]) -> None:
        pts = self.history.setdefault(mint, [])
        if pts and point["t"] <= pts[-1]["t"]:
            return
        pts.append(point)
        cutoff = point["t"] - HISTORY_SECONDS
        while pts and pts[0]["t"] < cutoff:
            pts.pop(0)

    # -- hot set
    def _maintain_hot(self, now: float, held: set[str]) -> None:
        for mint in list(self.hot):
            if mint in held:
                continue
            last = self.last_change.get(mint, self.hot[mint])
            pts = self.history.get(mint) or []
            if now - last > IDLE_EVICT_SECONDS or (pts and pts[-1].get("complete")):
                self.hot.pop(mint, None)
                self.retired[mint] = now
        for mint in held:
            if mint in self.meta and mint not in self.hot:
                self.hot[mint] = now
        while self.queue and len(self.hot) < HOT_MAX:
            mint, _ = self.queue.popitem(last=False)
            if mint not in self.retired:
                self.hot[mint] = now
                self.last_change.setdefault(mint, now)
        while len(self.retired) > 5000:
            self.retired.popitem(last=False)
        while len(self.queue) > 200:
            self.queue.popitem(last=False)

    def _gc(self, now: float) -> None:
        if self.cycle % 30:
            return
        keep = set(self.hot) | set(self.queue)
        for mint in list(self.history):
            pts = self.history[mint]
            if mint not in keep and (not pts or now - pts[-1]["t"] > HISTORY_SECONDS):
                self.history.pop(mint, None)
        for mint in list(self.launch):
            if now - float(self.launch[mint].get("queued_at", now)) > 7200:
                self.launch.pop(mint, None)
        if len(self.meta) > 8000:
            for mint in list(self.meta)[: len(self.meta) - 8000]:
                if mint not in keep:
                    self.meta.pop(mint, None)

    # -- one cycle
    def ensure_meta(self, mint: str, info: dict[str, Any]) -> None:
        """Rehydrate a held token's addresses from its durable position after a restart."""
        if mint in self.meta or not info.get("curve"):
            return
        program = info.get("token_program")
        self.meta[mint] = TokenMeta(mint=mint, curve=str(info["curve"]), symbol=str(info.get("symbol") or "")[:24],
                                    name=str(info.get("name") or "")[:64], creator=info.get("creator"),
                                    token_program=program if program in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID) else None,
                                    created_ms=info.get("created_ms"), quote_mint=info.get("quote_mint"),
                                    creator_ata=info.get("creator_ata"))

    def collect(self, held: set[str], *, now: float | None = None) -> PumpBatch:
        explicit = now is not None
        now = self.clock() if now is None else float(now)
        self.cycle += 1
        batch = PumpBatch(at=now)
        sort = self._listing_due(now)
        if sort is not None:
            try:
                rows = self._listing(sort)
                self._absorb_listing(rows, now, sort)
                batch.listing_rows += len(rows)
                self.listing_ok_at = now
                self.listing_failures = 0
                self.listing_note = None
            except SourceError as exc:
                text = str(exc)
                if re.search(r"\b(403|429)\b", text):
                    self.listing_failures += 1
                    pause = min(LISTING_BACKOFF_MAX_SECONDS, LISTING_BACKOFF_BASE_SECONDS * 2 ** (self.listing_failures - 1))
                else:
                    pause = LISTING_ERROR_PAUSE_SECONDS
                self.listing_pause_until = now + pause
                self.listing_note = f"pump.fun listing paused {pause:.0f} s after: {text[:90]}"
        if self.listing_note:
            batch.error = self.listing_note
        self._maintain_hot(now, held)
        batch.hot = len(self.hot)
        accounts: list[tuple[str, str, str]] = []
        for mint in self.hot:
            meta = self.meta.get(mint)
            if meta is None:
                continue
            accounts.append(("curve", mint, meta.curve))
            if meta.creator_ata:
                accounts.append(("creator", mint, meta.creator_ata))
        launch_mints = [m for m, v in self.launch.items() if v.get("status") == "queued" and m in self.meta][:LAUNCH_READS_PER_CYCLE]
        for mint in launch_mints:
            if ("curve", mint, self.meta[mint].curve) not in accounts:
                accounts.append(("launch", mint, self.meta[mint].curve))
        accounts = accounts[:100]
        if not accounts or not self.sol_usd:
            self._gc(now)
            return batch
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                   "params": [[a[2] for a in accounts], {"encoding": "base64", "commitment": "confirmed"}]}
        try:
            values = self.rpc.call(payload, "getMultipleAccounts")
        except SourceError:
            batch.error = (batch.error + "; " if batch.error else "") + "verified reads unavailable on every route"
            batch.route = self.rpc.last_route
            self._gc(now)
            return batch
        batch.route = self.rpc.last_route
        read_at = now if explicit else self.clock()
        if not isinstance(values, list) or len(values) != len(accounts):
            batch.error = "verified read returned an unexpected shape"
            return batch
        for (kind, mint, address), value in zip(accounts, values):
            meta = self.meta.get(mint)
            if meta is None:
                continue
            if kind in ("curve", "launch"):
                parsed = parse_curve_full(value.get("data")) if isinstance(value, dict) else None
                if parsed is None:
                    continue
                supply = parsed["supply"]
                self.supply[mint] = supply
                self.k[mint] = float(parsed["vs"]) * float(parsed["vt"])
                self.offset[mint] = float(parsed["vt"] - parsed["rt"])
                launch = self.launch.get(mint)
                if launch is not None and launch.get("status") == "queued" and meta.created_ms:
                    age = read_at - meta.created_ms / 1000.0
                    if age <= LAUNCH_AGE_MAX_SECONDS + PUMP_SECONDS:
                        taken = max(0, REAL_INIT - parsed["rt"])
                        launch.update({"status": "read", "take_pct": round(100.0 * taken / supply, 3), "age_s": round(age, 1), "read_at": read_at})
                    else:
                        launch.update({"status": "late", "age_s": round(age, 1)})
                    batch.launch_reads += 1
                if kind == "launch":
                    continue
                mcap = parsed["vs"] * supply / (parsed["vt"] * 1e9) * float(self.sol_usd)
                point = {"t": read_at, "vs": float(parsed["vs"]), "vt": float(parsed["vt"]), "mcap": mcap, "sol": float(self.sol_usd),
                         "curve": max(0.0, min(100.0, 100.0 * (REAL_INIT - parsed["rt"]) / REAL_INIT)) if self.offset[mint] == OFFSET_DEFAULT
                         else max(0.0, min(100.0, 100.0 * (REAL_INIT - (parsed["vt"] - self.offset[mint])) / REAL_INIT)),
                         "complete": parsed["complete"], "src": "chain", "supply": supply}
                pts = self.history.get(mint) or []
                last_chain = next((p for p in reversed(pts) if p["src"] == "chain"), None)
                if last_chain is None or last_chain["vs"] != point["vs"] or last_chain["vt"] != point["vt"]:
                    self.last_change[mint] = read_at
                self._append(mint, point)
                batch.points[mint] = point
            elif kind == "creator":
                owner = value.get("owner") if isinstance(value, dict) else None
                prior = self.creator.get(mint) or {}
                if value is None:
                    amount = 0
                    exists = False
                elif owner in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID) and owner == meta.token_program:
                    amount = parse_token_amount(value.get("data"))
                    exists = True
                    if amount is None:
                        continue
                else:
                    continue
                peak = max(int(prior.get("peak", 0)), int(amount))
                self.creator[mint] = {"amount": int(amount), "peak": peak, "exists": exists, "read_at": read_at,
                                      "first_seen": prior.get("first_seen", read_at)}
        for mint, point in batch.points.items():
            c = self.creator.get(mint)
            point["creator_read"] = c is not None and c.get("read_at") == read_at
        self._gc(now)
        return batch

    def arrays(self, mint: str) -> tuple[list[float], list[float], list[float]]:
        pts = self.history.get(mint) or []
        return [p["t"] for p in pts], [p["mcap"] for p in pts], [p["curve"] for p in pts]


# ---------------------------------------------------------------- durable decision cycle
def empty_pump_state(mode: str = "off", variant: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "version": 1, "strategy": PUMP_STRATEGY, "mode": mode, "variant": dict(variant or DEFAULT_VARIANT),
        "pending": [], "entered": {}, "events": [], "radar": [], "asOf": None,
        "book": {"closed": 0, "wins": 0, "losses": 0, "net": 0.0, "peakNet": 0.0, "drawdownPct": 0.0, "maxDrawdownPct": 0.0,
                 "streak": 0, "paused": None, "missed": 0, "buys": 0},
        "source": {"route": "none", "error": None, "hot": 0, "lastRead": None, "listingRows": 0, "launchReads": 0},
    }


def _event(state: dict[str, Any], at: float, action: str, mint: str, symbol: str, reason: str) -> None:
    events = state.setdefault("events", [])
    events.append({"at": round(at, 3), "action": action, "mint": mint, "symbol": symbol, "reason": reason[:200]})
    del events[:-80]


def ticket_for(streak: int) -> float:
    return TICKET_USD * (0.5 if int(streak) >= 3 else 1.0)


def gate_report(market: PumpMarket, mint: str, point: dict[str, Any] | None, now: float, variant: dict[str, str],
                state: dict[str, Any], portfolio: dict[str, Any], *, open_count: int) -> tuple[list[dict[str, Any]], bool]:
    """Every pump-mode entry gate with pass/fail/unknown. Unknown fails closed."""
    gates: list[dict[str, Any]] = []

    def gate(key: str, label: str, status: str, detail: str) -> None:
        gates.append({"key": key, "label": label, "status": status, "detail": detail[:160]})

    times, prices, curves = market.arrays(mint)
    signal_on = bool(times) and entry_signal(times, prices, curves, len(times) - 1, variant["entry"])
    gate("signal", f"Entry signal {variant['entry']}", "pass" if signal_on else "fail",
         "pump signal active on the latest point" if signal_on else "signal conditions not met")
    fresh = point is not None and point.get("src") == "chain" and now - float(point["t"]) <= FRESH_SECONDS
    gate("verified_read", "Verified curve read ≤10 s", "pass" if fresh else "unknown",
         f"read {now - float(point['t']):.1f}s ago" if point is not None else "no verified read this cycle")
    creator = market.creator.get(mint)
    meta = market.meta.get(mint)
    supply = market.supply.get(mint, SUPPLY_DEFAULT)
    if creator is None or meta is None or not meta.creator_ata:
        gate("creator_share", f"Creator holds ≤{CREATOR_MAX_PCT:g}%", "unknown", "creator account not readable")
        gate("creator_selling", "Creator has not sold", "unknown", "creator account not readable")
    else:
        share = 100.0 * int(creator["amount"]) / supply
        gate("creator_share", f"Creator holds ≤{CREATOR_MAX_PCT:g}%", "pass" if share <= CREATOR_MAX_PCT else "fail",
             f"creator holds {share:.2f}%" + ("" if creator.get("exists") else " (no creator token account)"))
        sold = creator_sold(int(creator["amount"]), int(creator["peak"]))
        gate("creator_selling", "Creator has not sold", "fail" if sold else "pass",
             "creator balance fell ≥10% from its high" if sold else "no creator sale seen since first read")
    launch = market.launch.get(mint)
    if launch and launch.get("status") == "read":
        take = float(launch["take_pct"])
        gate("launch_take", f"Launch take ≤{LAUNCH_TAKE_MAX_PCT:g}%", "pass" if take <= LAUNCH_TAKE_MAX_PCT else "fail",
             f"{take:.1f}% of supply sold by {launch['age_s']}s after creation")
    else:
        gate("launch_take", f"Launch take ≤{LAUNCH_TAKE_MAX_PCT:g}%", "unknown", "launch not read within 20 s of creation")
    listing_age = market.listing_age(now)
    if listing_age is not None and listing_age <= LISTING_FRESH_SECONDS:
        gate("listing_fresh", f"Market listing ≤{LISTING_FRESH_SECONDS:.0f} s old", "pass", f"listing {listing_age:.0f}s old")
    else:
        gate("listing_fresh", f"Market listing ≤{LISTING_FRESH_SECONDS:.0f} s old", "unknown",
             market.listing_note or ("no listing yet" if listing_age is None else f"listing {listing_age:.0f}s old"))
    entered = mint in (state.get("entered") or {})
    gate("one_entry", "One entry per token", "fail" if entered else "pass", "already traded" if entered else "not traded yet")
    gate("capacity", f"Fewer than {MAX_OPEN} open", "pass" if open_count < MAX_OPEN else "fail", f"{open_count}/{MAX_OPEN} open or pending")
    if point is not None and point.get("sol"):
        drag = flat_round_trip_pct(point["mcap"], IMPACT_CHECK_USD, point["sol"], int(point["vs"]), int(point["vt"]), FEE * 1e4)
        if drag is None:
            gate("impact", "Curve impact", "unknown", "impact cannot be priced")
        else:
            gate("impact", "Curve impact", "pass" if drag >= IMPACT_MAX_DRAG_PCT else "fail", f"$160 flat round trip {drag:.1f}%")
    else:
        gate("impact", "Curve impact", "unknown", "no verified reserves")
    ticket = ticket_for(int(state["book"].get("streak", 0)))
    cash_ok = float(portfolio.get("cash", 0)) >= ticket + NETWORK_USD
    gate("cash", "Paper cash", "pass" if cash_ok else "fail", f"${float(portfolio.get('cash', 0)):,.2f} for a ${ticket:.2f} ticket")
    paused = state["book"].get("paused")
    gate("book_drawdown", "Pump book drawdown ≤10%", "fail" if paused else "pass", paused or f"{state['book'].get('drawdownPct', 0):.1f}%")
    ok = all(g["status"] == "pass" for g in gates)
    return gates, ok


def _pump_positions(portfolio: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {pid: p for pid, p in (portfolio.get("positions") or {}).items() if isinstance(p, dict) and p.get("strategy") == PUMP_STRATEGY}


def _refresh_book(state: dict[str, Any]) -> None:
    book = state["book"]
    book["peakNet"] = max(float(book.get("peakNet", 0.0)), float(book["net"]))
    dd = 100.0 * (float(book["peakNet"]) - float(book["net"])) / BOOK_BANKROLL
    book["drawdownPct"] = round(max(0.0, dd), 4)
    book["maxDrawdownPct"] = round(max(float(book.get("maxDrawdownPct", 0.0)), book["drawdownPct"]), 4)
    book["paused"] = (f"pump book drawdown {book['drawdownPct']:.1f}% > {BOOK_MAX_DRAWDOWN_PCT:g}%; new pump entries paused"
                      if book["drawdownPct"] > BOOK_MAX_DRAWDOWN_PCT else None)


def run_cycle(market: PumpMarket, batch: PumpBatch, state: dict[str, Any], portfolio: dict[str, Any], *, now: float,
              mode: str, strategy_revision: int) -> list[dict[str, Any]]:
    """Fill pending orders on this batch, then decide exits and entries. Returns closed/scale/buy events.

    Mutates ``state`` and ``portfolio`` only; the caller commits both inside the lease fence.
    """
    from .policy import refresh_equity  # local import keeps module import light

    out: list[dict[str, Any]] = []
    variant = state.get("variant") or dict(DEFAULT_VARIANT)
    rules = EXIT_SETS[variant["exit"]]
    positions = _pump_positions(portfolio)
    by_mint = {p["mint"]: (pid, p) for pid, p in positions.items()}
    pending_left: list[dict[str, Any]] = []

    # 1. fills at the next verified read
    for order in state.get("pending") or []:
        mint = order["mint"]
        point = batch.points.get(mint)
        if order["kind"] == "entry":
            if point is None:
                if now - float(order["decided_at"]) > MISS_SECONDS:
                    state["book"]["missed"] = int(state["book"].get("missed", 0)) + 1
                    _event(state, now, "MISSED", mint, order["symbol"], "no verified read within 20 s of the decision")
                else:
                    pending_left.append(order)
                continue
            if len(portfolio["positions"]) >= MAX_OPEN or mint in by_mint:
                _event(state, now, "SKIP", mint, order["symbol"], "capacity filled before the entry read")
                continue
            ticket = float(order["ticket"])
            if float(portfolio["cash"]) + 1e-9 < ticket + NETWORK_USD:
                _event(state, now, "SKIP", mint, order["symbol"], "insufficient paper cash at fill")
                continue
            tokens = buy_tokens(ticket, point["vs"], point["vt"], point["sol"])
            to_curve = ticket / (1.0 + FEE)
            entry_fee = ticket - to_curve
            supply = int(point.get("supply") or SUPPLY_DEFAULT)
            spot = float(point["mcap"])
            avg_mcap = to_curve / tokens * supply
            pid = f"{mint}:{int(point['t'] * 1000)}"
            position = {
                "id": pid, "mint": mint, "symbol": order["symbol"], "name": order.get("name", ""), "strategy": PUMP_STRATEGY,
                "entry_at": float(point["t"]), "entry_mcap": spot, "last_mcap": spot, "peak_mcap": spot,
                "initial_notional": round(to_curve, 8), "remaining_quantity": tokens / supply, "remaining_basis": round(to_curve, 8),
                "total_exit_proceeds": 0.0, "costs": round(entry_fee + NETWORK_USD, 8), "scaled": False,
                "strategy_revision": strategy_revision,
                "risk": {"status": "pump_substitutes", "holderGate": HOLDER_SUBSTITUTE_LABEL, **(order.get("risk") or {})},
                "entry_reply_count": None, "entry_source": "pump-capture-v1 verified reserves (next-read fill)",
                "social": {"status": "unavailable", "sentiment": None},
                "pump": {"tokens": tokens, "cost_usd": ticket + NETWORK_USD, "realized_usd": 0.0, "peak_net": -1e9, "peak_spot": 0.0,
                         "last_net": None, "entry_spot": spot, "entry_t": float(point["t"]), "signal": variant["entry"],
                         "exit_set": variant["exit"], "decided_at": float(order["decided_at"]),
                         "fill_latency_s": round(float(point["t"]) - float(order["decided_at"]), 2),
                         "impact_pct": round(100.0 * (avg_mcap / spot - 1.0), 3), "ticket": ticket,
                         "creator_peak": int((order.get("risk") or {}).get("creator_peak") or 0),
                         "meta": dict(order.get("meta") or {})},
            }
            portfolio["cash"] = round(float(portfolio["cash"]) - to_curve - entry_fee - NETWORK_USD, 8)
            portfolio["positions"][pid] = position
            by_mint[mint] = (pid, position)
            state["book"]["buys"] = int(state["book"].get("buys", 0)) + 1
            refresh_equity(portfolio)
            _event(state, now, "BUY", mint, order["symbol"],
                   f"filled ${ticket:.2f} at ${spot:,.0f} mcap, impact {position['pump']['impact_pct']:.2f}%, {position['pump']['fill_latency_s']:.1f}s after decision")
            out.append({"status": "bought", "position": position})
            continue
        # exit / scale
        held = by_mint.get(mint)
        if held is None:
            continue
        if point is None:
            stale_for = now - float(order["decided_at"])
            last = next((p for p in reversed(market.history.get(mint) or []) if p["src"] == "chain" and not p.get("complete")), None)
            if stale_for <= 120 or last is None:
                pending_left.append(order)
                continue
            point = last
            order = {**order, "reason": order["reason"] + "; no newer verified read for 120 s, filled at last verified reserves"}
        pid, position = held
        st = position["pump"]
        fraction = 0.5 if order["kind"] == "scale" else 1.0
        note = ""
        if point.get("complete"):
            final = next((p for p in reversed(market.history.get(mint) or []) if p["src"] == "chain" and not p.get("complete")), None)
            if final is not None:
                point = final
                note = "; graduated, valued at final curve price"
        tokens = float(st["tokens"]) * fraction
        gross = sell_gross(tokens, point["vs"], point["vt"], point["sol"])
        fee = gross * FEE
        st["tokens"] = float(st["tokens"]) - tokens
        st["realized_usd"] = float(st["realized_usd"]) + gross - fee - NETWORK_USD
        position["remaining_quantity"] = st["tokens"] / int(point.get("supply") or SUPPLY_DEFAULT)
        basis = float(position["remaining_basis"]) * fraction
        position["remaining_basis"] = round(float(position["remaining_basis"]) - basis, 8)
        position["total_exit_proceeds"] = round(float(position["total_exit_proceeds"]) + gross, 8)
        position["costs"] = round(float(position["costs"]) + fee + NETWORK_USD, 8)
        position["last_mcap"] = float(point["mcap"])
        portfolio["cash"] = round(float(portfolio["cash"]) + gross - fee - NETWORK_USD, 8)
        if fraction < 1.0:
            position["scaled"] = True
            refresh_equity(portfolio)
            _event(state, now, "SCALE", mint, position["symbol"], f"{order['reason']}; filled at ${point['mcap']:,.0f}")
            out.append({"status": "scaled", "id": pid, "reason": order["reason"], "at": float(point["t"]), "mint": mint, "symbol": position["symbol"]})
            continue
        portfolio["positions"].pop(pid, None)
        by_mint.pop(mint, None)
        gross_pnl = float(position["total_exit_proceeds"]) - float(position["initial_notional"])
        costs = float(position["costs"])
        net = gross_pnl - costs
        portfolio["gross_realized"] = round(float(portfolio["gross_realized"]) + gross_pnl, 8)
        portfolio["modeled_costs"] = round(float(portfolio["modeled_costs"]) + costs, 8)
        portfolio["net_realized"] = round(float(portfolio["net_realized"]) + net, 8)
        portfolio["closed_trades"] = int(portfolio["closed_trades"]) + 1
        portfolio["losing_streak"] = int(portfolio.get("losing_streak", 0)) + 1 if net < 0 else 0
        refresh_equity(portfolio)
        book = state["book"]
        book["closed"] = int(book["closed"]) + 1
        book["net"] = round(float(book["net"]) + net, 8)
        book["wins" if net > 0 else "losses"] = int(book["wins" if net > 0 else "losses"]) + (1 if net != 0 else 0)
        book["streak"] = int(book.get("streak", 0)) + 1 if net < 0 else 0
        _refresh_book(state)
        _event(state, now, "SELL", mint, position["symbol"], f"{order['reason']}{note}; net ${net:+.2f}")
        out.append({
            "status": "closed", "id": pid, "mint": mint, "symbol": position["symbol"], "name": position.get("name", ""),
            "entry_at": position["entry_at"], "exit_at": float(point["t"]), "entry_mcap": position["entry_mcap"], "exit_mcap": float(point["mcap"]),
            "entry_notional": round(float(position["initial_notional"]), 8), "exit_proceeds": round(float(position["total_exit_proceeds"]), 8),
            "gross": round(gross_pnl, 8), "costs": round(costs, 8), "net": round(net, 8), "reason": f"pump: {order['reason']}",
            "strategy_revision": position["strategy_revision"], "strategy": PUMP_STRATEGY, "risk": position["risk"],
            "entry_reply_count": None, "entry_source": position["entry_source"], "social": position["social"],
            "watch_started_ts": None, "watch_periods": {}, "pump": dict(position["pump"]),
        })
    state["pending"] = pending_left
    pending_mints = {o["mint"] for o in pending_left}

    # 2. exits on open pump positions (always, in radar or paper mode)
    for mint, (pid, position) in list(by_mint.items()):
        point = batch.points.get(mint)
        if point is None or mint in pending_mints:
            continue
        position["last_mcap"] = float(point["mcap"])
        position["peak_mcap"] = max(float(position["peak_mcap"]), float(point["mcap"]))
        times, prices, _ = market.arrays(mint)
        k = len(prices) - 1
        c = market.creator.get(mint)
        sold = creator_sold(c["amount"], max(int(c["peak"]), int(position["pump"].get("creator_peak") or 0))) if c else None
        decision = exit_step(position, point, prices, k, rules, sold)
        if decision:
            kind, reason = decision
            state["pending"].append({"kind": kind, "mint": mint, "symbol": position["symbol"], "decided_at": float(point["t"]), "reason": reason})
            pending_mints.add(mint)
            _event(state, now, "EXIT QUEUED" if kind == "exit" else "SCALE QUEUED", mint, position["symbol"], reason + " — fills at next verified read")
    refresh_equity(portfolio)

    # 3. entries and radar
    open_count = len(portfolio["positions"]) + sum(1 for o in state["pending"] if o["kind"] == "entry")
    radar = []
    skip_log = state.setdefault("_skip_log", {})
    for mint in list(market.hot):
        meta = market.meta.get(mint)
        point = batch.points.get(mint)
        if meta is None:
            continue
        gates, ok = gate_report(market, mint, point, now, variant, state, portfolio, open_count=open_count)
        times, prices, curves = market.arrays(mint)
        passing = sum(1 for g in gates if g["status"] == "pass")
        held = mint in by_mint
        signals = {name: bool(times) and entry_signal(times, prices, curves, len(times) - 1, name) for name in ENTRY_SIGNALS}
        entry: dict[str, Any] = {
            "mint": mint, "symbol": meta.symbol or mint[:6], "mcap": round(prices[-1]) if prices else None,
            "curvePct": round(curves[-1], 1) if curves else None, "momentum30sPct": None if not times else
            (round(m, 2) if (m := momentum_30s(times, prices)) is not None else None),
            "signals": signals, "gates": gates, "readinessPct": round(100.0 * passing / len(gates), 1), "held": held,
            "readAgeS": None if point is None else round(now - float(point["t"]), 1),
            "netPct": round(float(by_mint[mint][1]["pump"]["last_net"]), 2) if held and by_mint[mint][1]["pump"].get("last_net") is not None else None,
            "pending": mint in pending_mints,
        }
        radar.append(entry)
        if held or mint in pending_mints or not signals.get(variant["entry"]):
            continue
        if not ok or point is None:
            failing = [g for g in gates if g["status"] != "pass"]
            last = float(skip_log.get(mint, 0))
            if now - last >= 60:
                skip_log[mint] = now
                _event(state, now, "SKIP", mint, entry["symbol"],
                       f"{variant['entry']} active; blocked by " + "; ".join(f"{g['label']} ({g['detail']})" for g in failing[:3]))
            continue
        if mode != "paper":
            last = float(skip_log.get(mint, 0))
            if now - last >= 60:
                skip_log[mint] = now
                _event(state, now, "RADAR", mint, entry["symbol"], f"{variant['entry']} active and every gate passes; radar mode places no paper order")
            continue
        creator = market.creator.get(mint) or {}
        launch = market.launch.get(mint) or {}
        supply = market.supply.get(mint, SUPPLY_DEFAULT)
        ticket = ticket_for(int(state["book"].get("streak", 0)))
        order_meta = {"curve": meta.curve, "creator": meta.creator, "token_program": meta.token_program,
                      "creator_ata": meta.creator_ata, "symbol": meta.symbol, "name": meta.name,
                      "created_ms": meta.created_ms, "quote_mint": meta.quote_mint}
        state["pending"].append({"kind": "entry", "mint": mint, "symbol": entry["symbol"], "name": meta.name, "decided_at": float(point["t"]),
                                 "meta": order_meta,
                                 "ticket": ticket, "reason": f"{variant['entry']} pump signal",
                                 "risk": {"creator_pct": round(100.0 * int(creator.get("amount", 0)) / supply, 4),
                                          "creator_peak": int(creator.get("peak", 0)), "launch_take_pct": launch.get("take_pct"),
                                          "dev_pct": round(100.0 * int(creator.get("amount", 0)) / supply, 4)}})
        state.setdefault("entered", {})[mint] = round(now, 3)
        open_count += 1
        pending_mints.add(mint)
        _event(state, now, "BUY QUEUED", mint, entry["symbol"],
               f"{variant['entry']} pump signal, all {len(gates)} gates pass; ${ticket:.2f} fills at next verified read")
    for mint, (pid, position) in by_mint.items():
        if market.creator.get(mint):
            position["pump"]["creator_peak"] = max(int(position["pump"].get("creator_peak") or 0), int(market.creator[mint]["peak"]))
    radar.sort(key=lambda r: (not r["held"], not r["pending"], not r["signals"].get(variant["entry"]), -r["readinessPct"],
                              -(r["momentum30sPct"] or -1e9)))
    state["radar"] = radar[:30]
    cutoff = now - 7200
    state["entered"] = {m: t for m, t in (state.get("entered") or {}).items() if float(t) >= cutoff}
    state["_skip_log"] = {m: t for m, t in skip_log.items() if now - float(t) < 600}
    state["asOf"] = round(now, 3)
    state["mode"] = mode
    if market.sol_usd:
        state["solUsd"] = {"value": float(market.sol_usd), "at": round(now, 3)}
    state["source"] = {"route": batch.route, "error": batch.error, "hot": batch.hot, "lastRead": round(batch.at, 3) if batch.points else
                       (state.get("source") or {}).get("lastRead"), "listingRows": batch.listing_rows, "launchReads": batch.launch_reads,
                       "verifiedPoints": len(batch.points)}
    return out


def public_pump_state(state: Any, *, now: float) -> dict[str, Any]:
    if not isinstance(state, dict):
        state = empty_pump_state()
    as_of = state.get("asOf")
    fresh = as_of is not None and 0 <= now - float(as_of) <= 30
    return {
        "strategy": PUMP_STRATEGY, "mode": state.get("mode", "off"), "variant": state.get("variant") or DEFAULT_VARIANT,
        "asOf": as_of, "fresh": bool(fresh), "disclaimer": DISCLAIMER, "holderGate": HOLDER_SUBSTITUTE_LABEL,
        "calibrated": False, "cadenceSeconds": PUMP_SECONDS, "ticketUsd": TICKET_USD,
        "costModel": {"feePct": FEE * 100, "networkUsd": NETWORK_USD, "impact": "constant-product on verified reserves",
                      "fill": "next verified read after the decision"},
        "book": state.get("book") or {}, "source": state.get("source") or {},
        "pending": [{k: o.get(k) for k in ("kind", "mint", "symbol", "decided_at", "reason")} for o in state.get("pending") or []],
        "radar": state.get("radar") or [], "events": list(state.get("events") or [])[-60:],
    }
