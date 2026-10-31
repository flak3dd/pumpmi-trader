"""Bounded public pump.fun data collection for the optimizer.

There is intentionally no fixture fallback, no LunarCrush request without a key,
and no use of the blocked pump.fun v2 API. Network errors are surfaced to the
worker status rather than converted into market data.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from ..curve import parse_curve_account_data, sol_price_usd


PUMP_FRONTEND_V3 = "https://frontend-api-v3.pump.fun"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
USER_AGENT = {"User-Agent": "PumpmiOptimizer/1.0 paper-only", "Accept": "application/json"}


class SourceError(RuntimeError):
    """A source request failed; callers must keep this visible in health."""


@dataclass
class SourceBatch:
    observations: list[dict[str, Any]]
    source_name: str
    social: str
    error: str | None = None


def _url_json(url: str, *, timeout: float = 4.0) -> Any:
    request = urllib.request.Request(url, headers=USER_AGENT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise SourceError(f"{type(exc).__name__}: {str(exc)[:160]}") from exc


def _rpc_json(payload: dict[str, Any], *, timeout: float = 4.0) -> Any:
    endpoint = os.environ.get("SOLANA_RPC_URL", "").strip() or SOLANA_RPC
    if not endpoint.startswith("https://"):
        raise SourceError("holder RPC endpoint must use HTTPS")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={**USER_AGENT, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SourceError(f"holder RPC HTTP {exc.code}; provider access/rate limit") from exc
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # A private provider may embed a key in its URL; do not echo exception text.
        raise SourceError(f"holder RPC unavailable ({type(exc).__name__})") from exc


def _rpc_value(payload: Any, method: str, label: str = "holder") -> Any:
    """Validate a JSON-RPC success envelope before consuming its value."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise SourceError(f"{label} {method} returned an invalid JSON-RPC envelope")
    if payload.get("error") is not None:
        # Provider-supplied error text can unexpectedly contain request context.
        # Never place it in holder-risk telemetry where a private RPC URL/key
        # could be exposed through a provider echo.
        raise SourceError(f"{label} {method} RPC error")
    result = payload.get("result")
    if not isinstance(result, dict) or "value" not in result:
        raise SourceError(f"{label} {method} returned no result value")
    return result["value"]


def _quote_sol(value: Any) -> float | None:
    """SOL market-cap quote. Missing or non-positive is not a price."""
    if isinstance(value, bool):
        return None
    try:
        quote = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(quote) or quote <= 0:
        return None
    return quote


def _ui_amount(value: Any, label: str) -> float:
    """Accept only finite non-negative RPC ``uiAmount`` values, never null-as-zero."""
    if isinstance(value, bool):
        raise SourceError(f"holder {label} uiAmount is invalid")
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"holder {label} uiAmount is unavailable") from exc
    if not math.isfinite(amount) or amount < 0:
        raise SourceError(f"holder {label} uiAmount is invalid")
    return amount


def _known_curve_accounts(coin: dict[str, Any]) -> set[str]:
    """Read real token-account addresses supplied by the public coin record.

    The old "largest account is probably the curve" proxy could hide a holder.
    Exclusion now requires a known bonding-curve token-account address that matches
    the RPC response exactly; missing or mismatched metadata remains unknown.
    """
    addresses: set[str] = set()
    for key in (
        "associated_bonding_curve", "associatedBondingCurve", "curve_token_account",
        "curveTokenAccount", "bonding_curve", "bondingCurve",
    ):
        value = coin.get(key)
        if isinstance(value, str) and value.strip():
            addresses.add(value.strip())
    return addresses


class HolderRiskCache:
    """TTL/backoff holder checks; unknown values are never coerced to zero."""

    def __init__(
        self,
        *,
        rpc: Callable[[dict[str, Any]], Any] = _rpc_json,
        ttl_seconds: float = 300.0,
        failure_backoff_seconds: float = 60.0,
    ) -> None:
        self.rpc = rpc
        self.ttl_seconds = ttl_seconds
        self.failure_backoff_seconds = failure_backoff_seconds
        self.cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.backoff_until = 0.0

    def get(self, coin: dict[str, Any], now: float) -> dict[str, Any]:
        mint = str(coin.get("mint") or "").strip()
        if not mint:
            return {"status": "unknown", "reason": "token mint unavailable"}
        cached = self.cache.get(mint)
        if cached and cached[0] > now:
            return dict(cached[1])
        if now < self.backoff_until:
            return {"status": "unknown", "reason": "holder RPC backoff"}
        try:
            largest_value = _rpc_value(self.rpc({
                "jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint],
            }), "getTokenLargestAccounts")
            if not isinstance(largest_value, list):
                raise SourceError("holder getTokenLargestAccounts result value is not a list")
            largest: list[tuple[str, float]] = []
            for item in largest_value:
                if not isinstance(item, dict) or not isinstance(item.get("address"), str) or not item["address"].strip():
                    raise SourceError("holder getTokenLargestAccounts account shape is invalid")
                largest.append((item["address"].strip(), _ui_amount(item.get("uiAmount"), "largest account")))

            supply_value = _rpc_value(self.rpc({
                "jsonrpc": "2.0", "id": 2, "method": "getTokenSupply", "params": [mint],
            }), "getTokenSupply")
            if not isinstance(supply_value, dict):
                raise SourceError("holder getTokenSupply result value is not an object")
            supply = _ui_amount(supply_value.get("uiAmount"), "token supply")
            if supply <= 0:
                raise SourceError("holder token supply is unavailable")

            curve_accounts = _known_curve_accounts(coin)
            if not curve_accounts:
                raise SourceError("holder known curve account unavailable")
            matching_curve = {address for address, _ in largest if address in curve_accounts}
            if not matching_curve:
                raise SourceError("holder known curve account was not present in largest accounts")
            non_curve = [amount for address, amount in largest if address not in matching_curve]
            top10 = 100.0 * sum(sorted(non_curve, reverse=True)[:10]) / supply
            bundle = 100.0 * sum(sorted(non_curve, reverse=True)[:5]) / supply

            creator = coin.get("creator")
            if not isinstance(creator, str) or not creator.strip():
                raise SourceError("holder creator unavailable")
            owners_value = _rpc_value(self.rpc({
                "jsonrpc": "2.0", "id": 3, "method": "getTokenAccountsByOwner",
                "params": [creator.strip(), {"mint": mint}, {"encoding": "jsonParsed"}],
            }), "getTokenAccountsByOwner")
            if not isinstance(owners_value, list):
                raise SourceError("holder getTokenAccountsByOwner result value is not a list")
            dev_amount = 0.0
            for item in owners_value:
                if not isinstance(item, dict):
                    raise SourceError("holder creator account shape is invalid")
                account = item.get("account")
                if not isinstance(account, dict):
                    raise SourceError("holder creator account data is unavailable")
                data = account.get("data")
                if not isinstance(data, dict):
                    raise SourceError("holder creator parsed data is unavailable")
                parsed = data.get("parsed")
                if not isinstance(parsed, dict):
                    raise SourceError("holder creator parsed payload is unavailable")
                info = parsed.get("info")
                if not isinstance(info, dict):
                    raise SourceError("holder creator token info is unavailable")
                token_amount = info.get("tokenAmount")
                if not isinstance(token_amount, dict):
                    raise SourceError("holder creator token amount is unavailable")
                dev_amount += _ui_amount(token_amount.get("uiAmount"), "creator account")
            result = {
                "status": "known",
                "top10_pct": round(top10, 3),
                "bundle_pct": round(bundle, 3),
                "dev_pct": round(100.0 * dev_amount / supply, 3),
                "reason": None,
            }
        except SourceError as exc:
            self.backoff_until = now + self.failure_backoff_seconds
            result = {"status": "unknown", "reason": str(exc)}
        # Failed checks are retryable after the existing provider backoff. They
        # must not occupy the successful-data TTL (normally five minutes).
        ttl = self.ttl_seconds if result.get("status") == "known" else self.failure_backoff_seconds
        self.cache[mint] = (now + ttl, result)
        return dict(result)


class PumpFunSource:
    """Collect at most one 30-row listing plus a bounded tracked-token refresh."""

    name = "pump.fun frontend-api-v3"
    social_label = "unavailable (no LunarCrush API key; no sentiment used)"

    def __init__(
        self,
        *,
        http_get: Callable[[str], Any] | None = None,
        holder_cache: HolderRiskCache | None = None,
        rpc: Callable[[dict[str, Any]], Any] | None = None,
        max_listing: int = 30,
        max_tracked: int = 64,
        max_tracked_refresh: int = 8,
        max_holder_checks: int = 2,
    ) -> None:
        # At most 4s listing + 4s curve batch + 8×4s detail + 2×(3×4s) holder
        # RPC = 64s, below the 75-second lease. The hard cap prevents a
        # constructor override from turning bounded work into a lease loss.
        self.http_get = http_get or (lambda url: _url_json(url, timeout=4.0))
        self.rpc = rpc or (lambda payload: _rpc_json(payload, timeout=4.0))
        self.holder_cache = holder_cache or HolderRiskCache(rpc=lambda payload: _rpc_json(payload, timeout=4.0))
        self.max_listing = min(30, max(1, max_listing))
        self.max_tracked = min(64, max(1, max_tracked))
        self.max_tracked_refresh = min(8, self.max_tracked, max(1, max_tracked_refresh))
        self.max_holder_checks = min(2, max(1, max_holder_checks))
        self._tracked_cursor = 0
        self._listing_holder_cursor = 0
        self._holder_lane = 0
        self._holder_attempt_sequence = 0
        self._holder_attempts: dict[str, int] = {}

    @staticmethod
    def _market_cap(coin: dict[str, Any]) -> float:
        for key in ("usd_market_cap", "market_cap"):
            try:
                value = float(coin.get(key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _curve_address(coin: dict[str, Any]) -> str:
        for key in ("bonding_curve", "bondingCurve"):
            value = coin.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _reserve_accounts(self, coins: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """One getMultipleAccounts read of bonding-curve reserves.

        An empty map means the read failed. Callers must not substitute
        market-cap / 69_000. This is a paper label, not an order.
        """
        addresses: list[str] = []
        for coin in coins:
            address = self._curve_address(coin)
            if address and address not in addresses:
                addresses.append(address)
        addresses = addresses[:100]
        if not addresses:
            return {}
        try:
            payload = self.rpc({
                "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                "params": [addresses, {"encoding": "base64", "commitment": "confirmed"}],
            })
            values = _rpc_value(payload, "getMultipleAccounts", label="curve")
        except SourceError:
            return {}
        if not isinstance(values, list):
            return {}
        found: dict[str, dict[str, Any]] = {}
        for address, value in zip(addresses, values):
            if not isinstance(value, dict):
                continue
            parsed = parse_curve_account_data(value.get("data"))
            if parsed is not None:
                found[address] = parsed
        return found

    def _listing(self) -> list[dict[str, Any]]:
        url = f"{PUMP_FRONTEND_V3}/coins?offset=0&limit={self.max_listing}&sort=last_trade_timestamp&order=DESC&includeNsfw=false"
        payload = self.http_get(url)
        if not isinstance(payload, list):
            raise SourceError("frontend-api-v3 listing returned a non-list payload")
        return [item for item in payload[: self.max_listing] if isinstance(item, dict) and item.get("mint")]

    def _coin(self, mint: str) -> dict[str, Any]:
        payload = self.http_get(f"{PUMP_FRONTEND_V3}/coins-v2/{mint}")
        if not isinstance(payload, dict):
            raise SourceError("frontend-api-v3 coin detail returned a non-object payload")
        return payload

    def _holder_priority(
        self,
        listing_candidates: list[dict[str, Any]],
        refreshed_tracked: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Interleave two bounded lanes so refreshed watches are not starved.

        The first two uncached candidates are one listing and one refreshed watch
        whenever both lanes exist. Each following poll flips the leading lane and
        rotates the listing start, while detail refreshes retain their own durable
        cursor. This changes selection only; it cannot increase RPC or detail
        budgets.
        """
        listing: list[dict[str, Any]] = []
        if listing_candidates:
            start = self._listing_holder_cursor % len(listing_candidates)
            listing = listing_candidates[start:] + listing_candidates[:start]
            self._listing_holder_cursor = (start + 1) % len(listing_candidates)
        # A rotating detail chunk still has the same first mint each time it
        # returns. Select the least recently attempted member, including when
        # failures expire before the chunk's next visit.
        tracked = sorted(
            refreshed_tracked,
            key=lambda coin: self._holder_attempts.get(str(coin.get("mint")), -1),
        )
        lanes = [listing, tracked] if self._holder_lane == 0 else [tracked, listing]
        self._holder_lane = 1 - self._holder_lane
        priority: list[dict[str, Any]] = []
        seen: set[str] = set()
        index = 0
        while any(index < len(lane) for lane in lanes):
            for lane in lanes:
                if index >= len(lane):
                    continue
                coin = lane[index]
                mint = str(coin.get("mint") or "")
                if mint and mint not in seen:
                    priority.append(coin)
                    seen.add(mint)
            index += 1
        return priority

    def collect(self, tracked_mints: list[str], *, now: float | None = None) -> SourceBatch:
        # An explicit timestamp is a deterministic-test clock. Production leaves it
        # unset, so every observation is stamped only once the response that
        # supplied its data has actually been received.
        def receipt_time() -> float:
            if now is not None:
                return float(now)
            return time.time()

        try:
            listing = self._listing()
            listing_received_at = receipt_time()
            # Listing values are a legitimate live observation. Detail fetches are
            # bounded and rotated across the durable 64-token state. This avoids
            # turning a failed endpoint into an unbounded sequential network loop.
            by_mint = {str(coin["mint"]): coin for coin in listing}
            coin_received_at = {str(coin["mint"]): listing_received_at for coin in listing}
            bounded_tracked = tracked_mints[: self.max_tracked]
            if bounded_tracked:
                start = self._tracked_cursor % len(bounded_tracked)
                selected = (bounded_tracked[start:] + bounded_tracked[:start])[: self.max_tracked_refresh]
                self._tracked_cursor = (start + len(selected)) % len(bounded_tracked)
            else:
                selected = []
            refreshed_tracked: list[dict[str, Any]] = []
            for mint in selected:
                try:
                    detail = self._coin(mint)
                except SourceError:
                    # Do not make one stale token conceal a successful listing batch.
                    continue
                if detail.get("mint"):
                    detail_mint = str(detail["mint"])
                    by_mint[detail_mint] = detail
                    coin_received_at[detail_mint] = receipt_time()
                    refreshed_tracked.append(detail)
            # Reserve progress for this poll. A failed batch leaves the map empty
            # and every row curve_known false; prices are still recorded.
            accounts_by_curve = self._reserve_accounts(list(by_mint.values()))
            # A parsed reserve is compound data: it cannot be labelled earlier than
            # the reserve response even when its listing/detail price arrived first.
            reserve_received_at = receipt_time() if accounts_by_curve else None
            all_coins = list(by_mint.values())[: self.max_listing + self.max_tracked]
            refreshed_mints = {str(coin.get("mint")) for coin in refreshed_tracked if coin.get("mint")}
            listing_candidates = [coin for coin in listing if str(coin.get("mint")) not in refreshed_mints]
            retained_mints = set(bounded_tracked) | {str(coin.get("mint")) for coin in listing}
            self._holder_attempts = {
                mint: sequence for mint, sequence in self._holder_attempts.items()
                if mint in retained_mints
            }
            priority = self._holder_priority(listing_candidates, refreshed_tracked)
            cache = getattr(self.holder_cache, "cache", {})
            holder_now = receipt_time()
            holder_check_mints: set[str] = set()
            for coin in priority:
                mint = str(coin.get("mint") or "")
                cached = cache.get(mint) if isinstance(cache, dict) else None
                if not mint or (cached and cached[0] > holder_now):
                    continue
                if self._market_cap(coin) <= 0 or coin.get("complete"):
                    continue
                holder_check_mints.add(mint)
                if len(holder_check_mints) >= self.max_holder_checks:
                    break
            observations: list[dict[str, Any]] = []
            holder_checks = 0
            for coin in all_coins:
                market_cap = self._market_cap(coin)
                if market_cap <= 0 or coin.get("complete"):
                    continue
                mint = str(coin["mint"])
                cached = cache.get(mint) if isinstance(cache, dict) else None
                if cached and cached[0] > holder_now:
                    risk = dict(cached[1])
                elif mint in holder_check_mints and holder_checks < self.max_holder_checks:
                    holder_checks += 1
                    self._holder_attempt_sequence += 1
                    self._holder_attempts[mint] = self._holder_attempt_sequence
                    risk = self.holder_cache.get(coin, holder_now)
                else:
                    # This is deliberately not a zero concentration. The policy
                    # records and conservatively skips it until a later poll checks it.
                    risk = {"status": "unknown", "reason": "holder check budget; awaiting later poll"}
                try:
                    replies = int(coin.get("reply_count") or 0)
                except (TypeError, ValueError):
                    replies = 0
                address = self._curve_address(coin)
                parsed = accounts_by_curve.get(address)
                known = parsed is not None
                observed_at = coin_received_at.get(mint, listing_received_at)
                if known and reserve_received_at is not None:
                    observed_at = max(observed_at, reserve_received_at)
                observation: dict[str, Any] = {
                    "mint": mint,
                    "symbol": str(coin.get("symbol") or "?")[:32],
                    "name": str(coin.get("name") or "")[:120],
                    "observed_at": observed_at,
                    "market_cap": round(market_cap, 6),
                    # Unknown reserves stay numeric for the NOT NULL column and are
                    # flagged so the 25–85% band cannot treat 0 as a real print.
                    "curve_pct": round(float(parsed["curve_pct"]), 4) if known else 0.0,
                    "curve_known": known,
                    "created_at": float(coin.get("created_timestamp") or 0) / 1000.0 if coin.get("created_timestamp") else None,
                    "reply_count": replies,
                    "risk": risk,
                    "social": {"status": "unavailable", "sentiment": None, "source": "none"},
                    "source": self.name,
                }
                if known and parsed is not None:
                    observation["virtual_sol"] = int(parsed["virtual_sol"])
                    observation["virtual_token"] = int(parsed["virtual_token"])
                    quote = _quote_sol(coin.get("market_cap_quote"))
                    if quote is not None and quote > 0:
                        observation["sol_usd"] = round(market_cap / quote, 6)
                    else:
                        implied = sol_price_usd(market_cap, int(parsed["virtual_sol"]), int(parsed["virtual_token"]))
                        if implied is not None:
                            observation["sol_usd"] = round(implied, 6)
                observations.append(observation)
            return SourceBatch(observations=observations, source_name=self.name, social=self.social_label)
        except SourceError as exc:
            return SourceBatch(observations=[], source_name=self.name, social=self.social_label, error=str(exc))
