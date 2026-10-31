"""Bonding-curve progress and paper fill adjustments. No network, no signing.

Progress comes from real token reserves, not a USD market-cap / 69_000 proxy.
Paper fills worsen the entry and the exit by the configured fee and, when
virtual reserves and a SOL price are known, by constant-product impact.
This is an expected-cost model for paper marks, not a live fee schedule and
not evidence of profitability.
"""
from __future__ import annotations

import base64
import struct

# Classic pump.fun curve constants. Token-side deltas on live accounts still match these.
INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000


def curve_progress_pct(real_token_reserves: int, initial: int = INITIAL_REAL_TOKEN_RESERVES) -> float:
    """Percent of the real token inventory already sold. 100 means the curve is through."""
    if initial <= 0:
        return 0.0
    sold = initial - int(real_token_reserves)
    return max(0.0, min(100.0, 100.0 * sold / initial))


def parse_curve_account(data: bytes) -> dict | None:
    """Parse the leading reserve words of a pump bonding-curve account.

    Layout (after the 8-byte discriminator): virtual token, virtual SOL lamports,
    real token, real SOL lamports. Later bytes (completion flag, creator) are ignored;
    graduation is taken from the coin payload's ``complete`` flag.
    """
    if not data or len(data) < 40:
        return None
    virtual_token, virtual_sol, real_token, real_sol = struct.unpack_from("<QQQQ", data, 8)
    if virtual_token <= 0 or virtual_sol <= 0:
        return None
    return {
        "virtual_token": int(virtual_token),
        "virtual_sol": int(virtual_sol),
        "real_token": int(real_token),
        "real_sol": int(real_sol),
        "curve_pct": round(curve_progress_pct(real_token), 2),
    }


# Pump.fun standard: 1e9 tokens at 6 decimals. Used only to imply a paper SOL price
# from reserves and a USD market cap when the payload has no SOL quote.
PUMP_TOKEN_SUPPLY_RAW = 1_000_000_000_000_000


def parse_curve_account_data(data: bytes | str | list | None) -> dict | None:
    """Full reserve words from a base64 or raw account blob. None when unreadable."""
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, str):
        try:
            data = base64.b64decode(data)
        except Exception:
            return None
    if not isinstance(data, (bytes, bytearray)):
        return None
    return parse_curve_account(bytes(data))


def curve_progress_from_account_data(data: bytes | str | list | None) -> float | None:
    """Reserve progress from a base64 or raw account blob. None when it cannot be read.

    A missing or truncated account is not market-cap / 69_000. Callers must skip
    the entry rather than invent a curve percent.
    """
    parsed = parse_curve_account_data(data)
    if not parsed:
        return None
    return float(parsed["curve_pct"])


def sol_price_usd(usd_mcap: float, virtual_sol: int | None, virtual_token: int | None,
                  supply_raw: int = PUMP_TOKEN_SUPPLY_RAW) -> float | None:
    """USD per SOL implied by the curve spot and a USD market cap.

    ``mcap_sol = virtual_sol_human * supply_raw / virtual_token`` is the constant-product
    spot when supply and the virtual token reserve share raw decimals. An explicit
    SOL quote should win over this. None means the paper price cannot be formed.
    """
    try:
        usd = float(usd_mcap)
        v_sol = int(virtual_sol) / 1e9 if virtual_sol is not None else 0.0
        v_tok = int(virtual_token) if virtual_token is not None else 0
        supply = int(supply_raw)
    except (TypeError, ValueError):
        return None
    if usd <= 0 or v_sol <= 0 or v_tok <= 0 or supply <= 0:
        return None
    mcap_sol = v_sol * supply / v_tok
    if mcap_sol <= 0:
        return None
    price = usd / mcap_sol
    if price < 1.0 or price > 100_000.0:
        return None
    return price


def _fee_rate(fee_bps: float) -> float:
    fee = max(0.0, float(fee_bps)) / 10_000.0
    return min(fee, 0.99)


def buy_fill(spot_mcap: float, usd: float, sol_px: float | None, virtual_sol: int | None,
             virtual_token: int | None, fee_bps: float) -> dict:
    """Worse paper entry. ``entry_eff`` is the spot mcap marked up by fee and impact.

    ``tokens`` is the raw-token amount the curve would deliver for ``usd``. It is
    None when reserves or the SOL price are missing; the fill is then fee-only.
    """
    spot = float(spot_mcap) if spot_mcap else 1.0
    fee = _fee_rate(fee_bps)
    fee_ratio = 1.0 / (1.0 - fee)
    sol_px = float(sol_px or 0)
    if usd <= 0 or sol_px <= 0 or not virtual_sol or not virtual_token:
        return {"entry_eff": spot * fee_ratio, "tokens": None, "sol_in": (usd / sol_px) if sol_px else None,
                "impact": False}
    sol_in = float(usd) / sol_px
    net = sol_in * (1.0 - fee)
    v_sol = virtual_sol / 1e9
    tokens = virtual_token * net / (v_sol + net)
    spot_tokens = virtual_token * sol_in / v_sol
    ratio = (spot_tokens / tokens) if tokens > 0 else fee_ratio
    return {"entry_eff": spot * ratio, "tokens": tokens, "sol_in": sol_in, "impact": True}


def sell_eff(spot_mcap: float, tokens: float | None, virtual_sol: int | None, virtual_token: int | None,
             fee_bps: float) -> float:
    """Spot mcap marked down by the fee and, when ``tokens`` and reserves exist, by impact."""
    spot = float(spot_mcap) if spot_mcap else 1.0
    fee = _fee_rate(fee_bps)
    if not tokens or tokens <= 0 or not virtual_sol or not virtual_token:
        return spot * (1.0 - fee)
    v_sol = virtual_sol / 1e9
    gross = v_sol * float(tokens) / (virtual_token + float(tokens))
    sol_out = gross * (1.0 - fee)
    spot_sol = float(tokens) * v_sol / virtual_token
    ratio = (sol_out / spot_sol) if spot_sol > 0 else (1.0 - fee)
    return spot * ratio


def flat_round_trip_pct(spot_mcap: float, usd: float, sol_px: float | None, virtual_sol: int | None,
                        virtual_token: int | None, fee_bps: float) -> float | None:
    """Net percent of a paper round trip if the spot market cap does not move.

    None when fee-only is the best model available (missing SOL price or reserves).
    A largely negative result means this size pays for the curve before any adverse move.
    """
    spot = float(spot_mcap or 0)
    if spot <= 0:
        return None
    filled = buy_fill(spot, float(usd), sol_px, virtual_sol, virtual_token, fee_bps)
    if not filled.get("impact") or not filled.get("tokens"):
        return None
    exited = sell_eff(spot, filled["tokens"], virtual_sol, virtual_token, fee_bps)
    entry = float(filled["entry_eff"])
    if entry <= 0:
        return None
    return 100.0 * (exited / entry - 1.0)
