"""Public pump.fun discovery for the paper live feed.

The feed used to list coins from ``frontend-api-v3.pump.fun``. That host now
answers with a Cloudflare challenge (HTTP 403) to this client, including with a
browser User-Agent, Origin, and Referer. LunarCrush is a social index and does
not carry bonding-curve reserves. ``api.pump.fun`` is a keyed profile/social
API, not a coin listing.

GeckoTerminal's public ``pump-fun`` pool list is the replacement. Those pool
addresses are the on-chain bonding-curve accounts (owner
``6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P``). Market cap is their FDV, which
for this curve is price times the full supply. No fixture and no synthetic book.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .curve import parse_curve_account
from .solana_pda import TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, associated_token_address, b58encode

GECKO_API = "https://api.geckoterminal.com/api/v2"
GECKO_HEADERS = {
    "User-Agent": "PumpBoardEngine/1.0 (paper; public market data)",
    "Accept": "application/json;version=20230203",
}
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
WSOL_MINT = "So11111111111111111111111111111111111111112"


def listing_url(page: int = 1) -> str:
    """One public page of live pump.fun bonding-curve pools."""
    number = int(page)
    if number < 1:
        number = 1
    return f"{GECKO_API}/networks/solana/dexes/pump-fun/pools?include=base_token&page={number}"


def _float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _mint_from_id(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("solana_"):
        return ""
    return value.split("_", 1)[1].strip()


def _timestamp_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _decimals(value: object) -> int:
    try:
        decimals = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 6
    if 0 <= decimals <= 18:
        return decimals
    return 6


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def coins_from_listing(payload: object) -> tuple[list[dict], float | None]:
    """Map one GeckoTerminal pool page into the coin records ``evaluate`` already reads.

    Returns ``(coins, sol_usd)``. ``sol_usd`` is the median SOL quote on pools
    quoted in wrapped SOL. A missing quote does not invent a price.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("geckoterminal listing returned no pool list")
    included: dict[str, dict] = {}
    for item in payload.get("included") or []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("attributes"), dict):
            included[item["id"]] = item["attributes"]
    coins: list[dict] = []
    sol_quotes: list[float] = []
    for pool in payload["data"]:
        if not isinstance(pool, dict):
            continue
        attributes = pool.get("attributes") if isinstance(pool.get("attributes"), dict) else {}
        relationships = pool.get("relationships") if isinstance(pool.get("relationships"), dict) else {}
        dex = ((relationships.get("dex") or {}).get("data") or {}).get("id")
        if dex not in (None, "pump-fun"):
            continue
        curve = attributes.get("address")
        if not isinstance(curve, str) or not curve.strip():
            continue
        base = (relationships.get("base_token") or {}).get("data") or {}
        base_id = base.get("id") if isinstance(base, dict) else None
        token = included.get(base_id) if isinstance(base_id, str) else {}
        if not isinstance(token, dict):
            token = {}
        mint = token.get("address") if isinstance(token.get("address"), str) else ""
        mint = mint.strip() or _mint_from_id(base_id)
        if not mint or mint == WSOL_MINT:
            continue
        quote_id = ((relationships.get("quote_token") or {}).get("data") or {}).get("id")
        quote_mint = _mint_from_id(quote_id)
        quote_px = _float(attributes.get("quote_token_price_usd"))
        quoted_sol = quote_mint == WSOL_MINT and quote_px is not None and 1.0 <= quote_px <= 100_000.0
        if quoted_sol and quote_px is not None:
            sol_quotes.append(quote_px)
        fdv = _float(attributes.get("market_cap_usd"))
        if fdv is None or fdv <= 0:
            fdv = _float(attributes.get("fdv_usd")) or 0.0
        name = attributes.get("name") if isinstance(attributes.get("name"), str) else ""
        symbol = token.get("symbol") if isinstance(token.get("symbol"), str) and token.get("symbol").strip() else ""
        if not symbol:
            symbol = name.split("/")[0].strip() or "?"
        token_name = token.get("name") if isinstance(token.get("name"), str) and token.get("name").strip() else symbol
        coin = {
            "mint": mint,
            "symbol": symbol,
            "name": token_name,
            "bonding_curve": curve.strip(),
            "usd_market_cap": float(fdv),
            "created_timestamp": _timestamp_ms(attributes.get("pool_created_at")),
            "base_decimals": _decimals(token.get("decimals")),
            "complete": False,
            "reply_count": None,
        }
        if quoted_sol and quote_px and fdv and fdv > 0:
            coin["market_cap_quote"] = float(fdv) / quote_px
        coins.append(coin)
    return coins, _median(sol_quotes)


def creator_pubkey(data: bytes) -> str | None:
    """Creator wallet at the end of the standard bonding-curve header.

    Borsh layout after the discriminator and five u64 words: one completion
    byte, then the 32-byte creator. A zero key is not a creator.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 81:
        return None
    raw = bytes(data[49:81])
    if raw == b"\x00" * 32:
        return None
    try:
        text = b58encode(raw)
    except Exception:
        return None
    return text or None


def mcap_from_reserves(sol_usd: float | None, virtual_sol: object, virtual_token: object, supply_raw: object):
    """USD and SOL market cap from curve reserves and a real SOL/USD quote.

    ``None`` when either input is missing. This is the constant-product spot
    ``virtual_sol * supply / virtual_token``, not a stand-in price.
    """
    quote = _float(sol_usd)
    if quote is None or quote <= 0:
        return None
    try:
        virtual_sol_i = int(virtual_sol)  # type: ignore[arg-type]
        virtual_token_i = int(virtual_token)  # type: ignore[arg-type]
        supply_i = int(supply_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if virtual_sol_i <= 0 or virtual_token_i <= 0 or supply_i <= 0:
        return None
    sol_mcap = (virtual_sol_i / 1e9) * supply_i / virtual_token_i
    usd = quote * sol_mcap
    if sol_mcap <= 0 or usd <= 0:
        return None
    return usd, sol_mcap


def apply_account_meta(coin: dict, accounts: dict) -> dict | None:
    """Fill creator, supply, completion, and the curve token account from RPC.

    An account that answered and is not the pump program is dropped. A missing
    account is left untouched so a later read can still skip closed.
    """
    if not isinstance(coin, dict):
        return None
    updated = dict(coin)
    curve_addr = updated.get("bonding_curve")
    curve = accounts.get(curve_addr) if isinstance(curve_addr, str) else None
    if isinstance(curve, dict):
        if curve.get("owner") != PUMP_PROGRAM_ID:
            return None
        blob = curve.get("data")
        if isinstance(blob, (bytes, bytearray)):
            parsed = parse_curve_account(bytes(blob))
            if parsed:
                if parsed.get("token_supply"):
                    updated["total_supply"] = parsed["token_supply"]
                if "complete" in parsed:
                    updated["complete"] = bool(parsed["complete"])
                creator = creator_pubkey(bytes(blob))
                if creator:
                    updated["creator"] = creator
    mint = updated.get("mint")
    mint_account = accounts.get(mint) if isinstance(mint, str) else None
    program = mint_account.get("owner") if isinstance(mint_account, dict) else None
    if program in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID) and isinstance(curve_addr, str) and isinstance(mint, str):
        try:
            updated["associated_bonding_curve"] = associated_token_address(curve_addr, mint, program)
        except ValueError:
            pass
    return updated
