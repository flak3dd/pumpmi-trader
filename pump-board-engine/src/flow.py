"""Read-only swap-flow summaries for the paper entry gate. No orders.

A curve trade is the bonding-curve account's SOL delta: SOL in is a buy,
SOL out is a sell. Missing or incomplete tapes do not produce a passing
summary. Thresholds are expected-edge filters, not a claim that the gate
is profitable.
"""
from __future__ import annotations

FLOW_DUST_SOL = 0.01


def account_keys(tx: dict) -> list:
    """Static keys, then address-lookup writable, then readonly. Matches balance indexes."""
    message = ((tx.get("transaction") or {}).get("message") or {})
    pubs = []
    for key in message.get("accountKeys") or []:
        pubs.append(key.get("pubkey") if isinstance(key, dict) else key)
    loaded = ((tx.get("meta") or {}).get("loadedAddresses") or {})
    pubs.extend(loaded.get("writable") or [])
    pubs.extend(loaded.get("readonly") or [])
    return pubs


def trade_from_tx(tx: dict, curve: str, dust_sol: float = FLOW_DUST_SOL) -> dict | None:
    """One curve swap, or None if this transaction is not a material curve SOL move."""
    meta = tx.get("meta") or {}
    if not meta or meta.get("err"):
        return None
    pubs = account_keys(tx)
    if curve not in pubs:
        return None
    index = pubs.index(curve)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if index >= len(pre) or index >= len(post):
        return None
    delta_sol = (post[index] - pre[index]) / 1e9
    if abs(delta_sol) < dust_sol:
        return None
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []
    user = None
    for key in keys:
        if isinstance(key, dict) and key.get("signer"):
            user = key.get("pubkey")
            break
    if user is None and keys:
        user = keys[0] if isinstance(keys[0], str) else (keys[0].get("pubkey") if isinstance(keys[0], dict) else None)
    return {
        "side": "buy" if delta_sol > 0 else "sell",
        "quote_sol": abs(delta_sol),
        "user": user or "unknown",
        "ts": tx.get("blockTime"),
    }


def summarize(trades: list) -> dict:
    """Signed quote flow, buy/sell imbalance, unique buyers, and top-buyer share of buy SOL."""
    buy_sol = 0.0
    sell_sol = 0.0
    buyers: dict[str, float] = {}
    for trade in trades:
        quote = float(trade["quote_sol"])
        if trade["side"] == "buy":
            buy_sol += quote
            buyers[trade["user"]] = buyers.get(trade["user"], 0.0) + quote
        else:
            sell_sol += quote
    if sell_sol > 0:
        imbalance = buy_sol / sell_sol
    else:
        imbalance = 99.0 if buy_sol > 0 else 0.0
    top = max(buyers.values()) if buyers else 0.0
    top_pct = (100.0 * top / buy_sol) if buy_sol > 0 else 100.0
    return {
        "signed_quote_sol": buy_sol - sell_sol,
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "imbalance": imbalance,
        "unique_buyers": len(buyers),
        "top_buyer_pct": top_pct,
        "trades": len(trades),
    }


def flow_allows(summary: dict | None, strat: dict, strong: bool = False) -> tuple[bool, str]:
    """Enforce P2. A missing summary never passes."""
    if not summary:
        return False, "flow unavailable"
    need = strat["flow_strong_buyers"] if strong else strat["flow_min_buyers"]
    buyers = int(summary["unique_buyers"])
    signed = float(summary["signed_quote_sol"])
    imbalance = float(summary["imbalance"])
    whale = float(summary["top_buyer_pct"])
    if buyers < need:
        return False, f"flow buyers {buyers} < {need}"
    if signed <= 0 or float(summary["buy_sol"]) <= 0:
        return False, f"flow signed quote {signed:+.3f} SOL"
    if imbalance < strat["flow_min_bs_ratio"]:
        return False, f"flow imbalance {imbalance:.2f} < {strat['flow_min_bs_ratio']}"
    if whale > strat["flow_max_whale_pct"]:
        return False, f"flow top buyer {whale:.0f}% > {strat['flow_max_whale_pct']}%"
    breadth = "strong breadth, " if strong else ""
    return True, (
        f"flow {breadth}signed {signed:+.2f} SOL, imbalance {imbalance:.2f}, "
        f"buyers {buyers}, top {whale:.0f}%"
    )


def accelerating(slices: list | None, dust_sol: float) -> tuple[bool, str]:
    """Three successive watch windows must each be positive and strictly larger than the previous."""
    if not slices or len(slices) != 3 or any(item is None for item in slices):
        return False, "flow windows incomplete"
    short, medium, long = (float(item) for item in slices)
    if short < dust_sol or medium <= short or long <= medium:
        return False, f"flow not accelerating {short:+.3f}/{medium:+.3f}/{long:+.3f} SOL"
    return True, f"flow accel {short:+.3f}/{medium:+.3f}/{long:+.3f} SOL"


def flow_reversed(entry_lamports: int | None, now_lamports: int | None, entry_flow_sol: float,
                  dust_sol: float = 0.05, giveback: float = 0.25) -> bool:
    """Post-entry net SOL has given back a material share of the inflow that qualified the entry.

    A single dust sell is not treated as a reversal. The threshold is the larger of
    ``dust_sol`` and ``giveback`` times the watch-window net inflow.
    """
    if entry_lamports is None or now_lamports is None:
        return False
    net = (int(now_lamports) - int(entry_lamports)) / 1e9
    tolerance = max(float(dust_sol), float(giveback) * max(float(entry_flow_sol), 0.0))
    return net < -tolerance


def signal_strength(summary: dict, slices: list, strat: dict, strong: bool = False) -> float:
    """0 at the P2 floor, 1 when breadth, imbalance, concentration, and acceleration are all clearly stronger."""
    need = strat["flow_strong_buyers"] if strong else strat["flow_min_buyers"]
    buyers = max(0.0, (summary["unique_buyers"] - need) / max(need, 1))
    imbalance = max(0.0, (summary["imbalance"] - strat["flow_min_bs_ratio"]) / max(strat["flow_min_bs_ratio"], 1e-9))
    whale = max(0.0, (strat["flow_max_whale_pct"] - summary["top_buyer_pct"]) / max(strat["flow_max_whale_pct"], 1e-9))
    accel = 0.0
    if slices and slices[0] and slices[0] > 0:
        accel = max(0.0, slices[-1] / slices[0] - 1.0)
    return max(0.0, min(1.0, (min(buyers, 1) + min(imbalance, 1) + min(whale, 1) + min(accel, 1)) / 4))


def size_for_signal(strength: float, strat: dict) -> float:
    """Map strength into the existing size band. Does not leave ``size_min``..``size_max``."""
    lo, hi = float(strat["size_min"]), float(strat["size_max"])
    usd = lo + max(0.0, min(1.0, float(strength))) * (hi - lo)
    return max(lo, min(hi, usd))
