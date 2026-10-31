"""Read-only swap-flow summaries for the paper entry gate. No orders.

A curve trade is the bonding-curve account's SOL delta: SOL in is a buy,
SOL out is a sell. Missing or incomplete tapes do not produce a passing
summary. Thresholds are expected-edge filters, not a claim that the gate
is profitable.

Two distribution gates sit on top of the unique-buyer and single-wallet caps.
Top-3 share rejects a book that clears the single-wallet cap because two or
three wallets still own the buy SOL. Meaningful buyers reject a unique count
inflated by dust wallets. A top-3 cap of 100 or more disables that test. A
meaningful-buyer minimum of 0 disables that test. Neither gate changes fees.
"""
from __future__ import annotations

FLOW_DUST_SOL = 0.01
FLOW_MEANINGFUL_SOL = 0.05


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


def summarize(trades: list, meaningful_sol: float = FLOW_MEANINGFUL_SOL) -> dict:
    """Signed quote flow plus distribution: top-3 share and economically real buyers.

    ``meaningful_buyers`` counts wallets whose aggregated buy SOL is at least
    ``meaningful_sol``. Several dust prints from one wallet add up before that test.
    """
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
    ranked = sorted(buyers.values(), reverse=True)
    top = ranked[0] if ranked else 0.0
    top_pct = (100.0 * top / buy_sol) if buy_sol > 0 else 100.0
    top3_pct = (100.0 * sum(ranked[:3]) / buy_sol) if buy_sol > 0 else 100.0
    floor = float(meaningful_sol)
    meaningful = sum(1 for amount in ranked if amount + 1e-12 >= floor)
    return {
        "signed_quote_sol": buy_sol - sell_sol,
        "buy_sol": buy_sol,
        "sell_sol": sell_sol,
        "imbalance": imbalance,
        "unique_buyers": len(buyers),
        "top_buyer_pct": top_pct,
        "top3_buyer_pct": top3_pct,
        "meaningful_buyers": meaningful,
        "meaningful_sol": floor,
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
    top3 = summary.get("top3_buyer_pct")
    if top3 is None:
        return False, "flow top3 unavailable"
    top3_cap = float(strat["flow_max_top3_pct"])
    # A cap of 100 or more turns the test off. Anything at or under the cap passes.
    if top3_cap < 100 and float(top3) > top3_cap:
        return False, f"flow top3 {float(top3):.1f}% > {top3_cap:g}%"
    meaningful = summary.get("meaningful_buyers")
    if meaningful is None:
        return False, "flow meaningful buyers unavailable"
    need_meaningful = int(strat["flow_strong_meaningful_buyers"] if strong else strat["flow_min_meaningful_buyers"])
    if int(meaningful) < need_meaningful:
        return False, f"flow meaningful buyers {int(meaningful)} < {need_meaningful}"
    breadth = "strong breadth, " if strong else ""
    return True, (
        f"flow {breadth}signed {signed:+.2f} SOL, imbalance {imbalance:.2f}, "
        f"buyers {buyers}, top {whale:.0f}%, top3 {float(top3):.0f}%, meaningful {int(meaningful)}"
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
    """0 at the floor, 1 when breadth, imbalance, concentration, and acceleration are all clearly stronger.

    Top-3 slack and meaningful-buyer slack participate equally with the older four
    terms. Missing distribution fields contribute 0, so an unmeasured tape cannot
    size up. The result is still clamped to 0..1 for ``size_for_signal``.
    """
    need = strat["flow_strong_buyers"] if strong else strat["flow_min_buyers"]
    buyers = max(0.0, (summary["unique_buyers"] - need) / max(need, 1))
    imbalance = max(0.0, (summary["imbalance"] - strat["flow_min_bs_ratio"]) / max(strat["flow_min_bs_ratio"], 1e-9))
    whale = max(0.0, (strat["flow_max_whale_pct"] - summary["top_buyer_pct"]) / max(strat["flow_max_whale_pct"], 1e-9))
    accel = 0.0
    if slices and slices[0] and slices[0] > 0:
        accel = max(0.0, slices[-1] / slices[0] - 1.0)
    top3_cap = float(strat["flow_max_top3_pct"])
    top3 = summary.get("top3_buyer_pct")
    if top3 is None:
        top3_score = 0.0
    elif top3_cap >= 100:
        top3_score = 1.0
    else:
        top3_score = max(0.0, (top3_cap - float(top3)) / max(top3_cap, 1e-9))
    need_meaningful = float(strat["flow_strong_meaningful_buyers"] if strong else strat["flow_min_meaningful_buyers"])
    meaningful = summary.get("meaningful_buyers")
    if meaningful is None:
        meaningful_score = 0.0
    elif need_meaningful <= 0:
        meaningful_score = 1.0
    else:
        meaningful_score = max(0.0, (float(meaningful) - need_meaningful) / max(need_meaningful, 1.0))
    parts = (
        min(buyers, 1), min(imbalance, 1), min(whale, 1), min(accel, 1),
        min(top3_score, 1), min(meaningful_score, 1),
    )
    return max(0.0, min(1.0, sum(parts) / len(parts)))


def size_for_signal(strength: float, strat: dict) -> float:
    """Map strength into the existing size band. Does not leave ``size_min``..``size_max``."""
    lo, hi = float(strat["size_min"]), float(strat["size_max"])
    usd = lo + max(0.0, min(1.0, float(strength))) * (hi - lo)
    return max(lo, min(hi, usd))
