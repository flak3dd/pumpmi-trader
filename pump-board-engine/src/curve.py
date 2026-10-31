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


def curve_progress_from_account_data(data: bytes | str | list | None) -> float | None:
    """Reserve progress from a base64 or raw account blob. None when it cannot be read.

    A missing or truncated account is not market-cap / 69_000. Callers must skip
    the entry rather than invent a curve percent.
    """
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, str):
        try:
            data = base64.b64decode(data)
        except Exception:
            return None
    if not isinstance(data, (bytes, bytearray)):
        return None
    parsed = parse_curve_account(bytes(data))
    if not parsed:
        return None
    return float(parsed["curve_pct"])


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
