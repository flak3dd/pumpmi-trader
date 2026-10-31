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
Acceleration compares SOL per second once window lengths are known, so a
longer checkpoint cannot pass as rising flow. The last watch window is gated
on its own: a whole-tape cap can pass while the window that triggers the
entry is a single wallet, or while that window itself is net selling. A
last-window imbalance ratio of 0 disables only that bid test.
Same-wallet round trip is separate from that bid: sells from other wallets
do not count, so a tape can clear 1.3 while the buyers have already sold
half of what they bought. A round-trip cap of 100 or more disables that test.
A last-window top-seller cap is separate again: the seller never has to have
bought. One wallet can be the entire offer while the bid still clears 1.3
and the round-trip share stays at zero. A cap of 100 or more disables that test.
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
    sellers: dict[str, float] = {}
    for trade in trades:
        quote = float(trade["quote_sol"])
        if trade["side"] == "buy":
            buy_sol += quote
            buyers[trade["user"]] = buyers.get(trade["user"], 0.0) + quote
        else:
            sell_sol += quote
            user = trade.get("user") or "unknown"
            sellers[user] = sellers.get(user, 0.0) + quote
    matched = sum(min(amount, sellers.get(user, 0.0)) for user, amount in buyers.items())
    round_trip_pct = (100.0 * matched / buy_sol) if buy_sol > 0 else 0.0
    if sell_sol > 0:
        imbalance = buy_sol / sell_sol
    else:
        imbalance = 99.0 if buy_sol > 0 else 0.0
    ranked = sorted(buyers.values(), reverse=True)
    top = ranked[0] if ranked else 0.0
    top_pct = (100.0 * top / buy_sol) if buy_sol > 0 else 100.0
    top3_pct = (100.0 * sum(ranked[:3]) / buy_sol) if buy_sol > 0 else 100.0
    sell_ranked = sorted(sellers.values(), reverse=True)
    top_sell = sell_ranked[0] if sell_ranked else 0.0
    # No sells is not a concentrated seller. Callers still fail closed when the field is missing.
    top_seller_pct = (100.0 * top_sell / sell_sol) if sell_sol > 0 else 0.0
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
        "round_trip_pct": round_trip_pct,
        "top_seller_pct": top_seller_pct,
        "trades": len(trades),
    }


def with_last_window(summary: dict, trades: list, since_ts: float | None) -> dict:
    """Attach the buy distribution at or after ``since_ts``.

    A missing cutoff or any undated trade leaves the fields empty. Callers
    treat that as unavailable and skip, rather than using the whole tape.
    """
    if since_ts is None or any(trade.get("ts") is None for trade in trades):
        summary["last_window_buyers"] = None
        summary["last_window_top_pct"] = None
        summary["last_window_buy_sol"] = None
        summary["last_window_signed_sol"] = None
        summary["last_window_imbalance"] = None
        summary["last_window_round_trip_pct"] = None
        summary["last_window_sell_sol"] = None
        summary["last_window_top_seller_pct"] = None
        return summary
    cutoff = float(since_ts)
    late = [trade for trade in trades if float(trade["ts"]) >= cutoff]
    late_summary = summarize(late, meaningful_sol=float(summary.get("meaningful_sol", FLOW_MEANINGFUL_SOL)))
    summary["last_window_buyers"] = late_summary["unique_buyers"]
    summary["last_window_top_pct"] = late_summary["top_buyer_pct"]
    summary["last_window_buy_sol"] = late_summary["buy_sol"]
    summary["last_window_signed_sol"] = late_summary["signed_quote_sol"]
    summary["last_window_imbalance"] = late_summary["imbalance"]
    summary["last_window_round_trip_pct"] = late_summary["round_trip_pct"]
    summary["last_window_sell_sol"] = late_summary["sell_sol"]
    summary["last_window_top_seller_pct"] = late_summary["top_seller_pct"]
    return summary


def last_window_allows(summary: dict | None, strat: dict) -> tuple[bool, str]:
    """The window that triggers the entry must itself be a distributed net bid.

    Whole-tape unique, top-buyer, and top-3 caps can all pass while the latest
    window is a single buyer, or while that window is net selling, or while one
    wallet that never bought is the offer. That window is the paper entry's
    counterparty. A buyer minimum of 0 and a top-buyer cap of 100 or more disable
    the breadth test. An imbalance ratio of 0 disables the bid test. A round-trip
    cap of 100 or more disables that test. A top-seller cap of 100 or more disables
    the offer test. Sells below the SOL floor are dust and do not count. Any
    control left on still fails closed when its fields are missing.
    """
    need = int(strat["flow_last_window_min_buyers"])
    cap = float(strat["flow_last_window_max_whale_pct"])
    ratio = float(strat.get("flow_last_window_min_bs_ratio", strat["flow_min_bs_ratio"]))
    round_trip_cap = float(strat.get("flow_max_round_trip_pct", 50))
    seller_cap = float(strat.get("flow_last_window_max_seller_pct", 60))
    seller_floor = float(strat.get("flow_last_window_min_sell_sol", 0.15))
    buyers_off = need <= 0 and cap >= 100
    imbalance_off = ratio <= 0
    round_trip_off = round_trip_cap >= 100
    seller_off = seller_cap >= 100
    if buyers_off and imbalance_off and round_trip_off and seller_off:
        return True, "flow last window disabled"
    if not summary:
        return False, "flow last window unavailable"
    buyers = summary.get("last_window_buyers")
    top = summary.get("last_window_top_pct")
    buy_sol = summary.get("last_window_buy_sol")
    signed = summary.get("last_window_signed_sol")
    imbalance = summary.get("last_window_imbalance")
    if not buyers_off and (buyers is None or top is None or buy_sol is None):
        return False, "flow last window unavailable"
    if not imbalance_off and (signed is None or imbalance is None):
        return False, "flow last window imbalance unavailable"
    if not buyers_off:
        if float(buy_sol) <= 0:
            return False, "flow last window has no buys"
        if int(buyers) < need:
            return False, f"flow last window buyers {int(buyers)} < {need}"
        if cap < 100 and float(top) > cap:
            return False, f"flow last window top buyer {float(top):.0f}% > {cap:g}%"
    if not imbalance_off and (float(signed) <= 0 or float(imbalance) < ratio):
        return False, f"flow last window imbalance {float(imbalance):.2f} < {ratio:g}"
    if not round_trip_off:
        round_trip = summary.get("last_window_round_trip_pct")
        if round_trip is None:
            return False, "flow last window round trip unavailable"
        if float(round_trip) > round_trip_cap:
            return False, f"flow last window round trip {float(round_trip):.0f}% > {round_trip_cap:g}%"
    if not seller_off:
        sell_sol = summary.get("last_window_sell_sol")
        top_seller = summary.get("last_window_top_seller_pct")
        if sell_sol is None or top_seller is None:
            return False, "flow last window seller unavailable"
        if float(sell_sol) + 1e-12 >= seller_floor and float(top_seller) > seller_cap:
            return False, f"flow last window top seller {float(top_seller):.0f}% > {seller_cap:g}%"
    if buyers_off:
        if imbalance is not None and not imbalance_off:
            return True, f"flow last window imbalance {float(imbalance):.2f}"
        if not round_trip_off:
            return True, f"flow last window round trip {float(summary.get('last_window_round_trip_pct')):.0f}%"
        return True, "flow last window"
    bid = "" if imbalance is None else f", imbalance {float(imbalance):.2f}"
    return True, f"flow last window buyers {int(buyers)}, top {float(top):.0f}%{bid}"


def unavailable_tape_fallback(reply_count, rise_pts: float, min_rise: float) -> tuple[bool, str]:
    """Paper P2 when the swap tape cannot be read. Not a claim that the rise is profitable.

    Reply counts do not decide this path. Missing, non-numeric, and zero counts are
    unavailable: the public reply field is 0 on every coin when that feed is down, so
    an AND of ``replies >= 2`` and the rise can never pass, and a hard skip on a
    missing tape has the same result. A positive count is ignored too. A missing
    tape must not soft-pass on replies alone. The curve has to rise by ``min_rise``
    points over the watch. Below that, the entry skips. Callers use this only when
    the tape is missing. A present tape stays on ``flow_allows``.
    """
    measured = reply_count if isinstance(reply_count, (int, float)) and not isinstance(reply_count, bool) else None
    if measured is None or float(measured) <= 0:
        note = "replies unavailable"
    else:
        note = f"replies {int(measured)} ignored"
    rise = float(rise_pts)
    need = float(min_rise)
    if rise + 1e-9 >= need:
        return True, f"flow n/a -> fallback ({note}, curve {rise:+.1f}pts)"
    return False, f"flow n/a -> fallback ({note}, curve {rise:+.1f}pts < {need:g})"


def flow_allows(summary: dict | None, strat: dict, strong: bool = False) -> tuple[bool, str]:
    """Enforce P2 when a swap tape exists. A missing summary never passes here."""
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
    round_trip_cap = float(strat.get("flow_max_round_trip_pct", 50))
    if round_trip_cap < 100:
        round_trip = summary.get("round_trip_pct")
        if round_trip is None:
            return False, "flow round trip unavailable"
        if float(round_trip) > round_trip_cap:
            return False, f"flow round trip {float(round_trip):.0f}% > {round_trip_cap:g}%"
    breadth = "strong breadth, " if strong else ""
    return True, (
        f"flow {breadth}signed {signed:+.2f} SOL, imbalance {imbalance:.2f}, "
        f"buyers {buyers}, top {whale:.0f}%, top3 {float(top3):.0f}%, meaningful {int(meaningful)}"
    )


def accelerating(slices: list | None, dust_sol: float, durations: list | None = None) -> tuple[bool, str]:
    """Three successive watch windows must each show rising buy flow.

    When ``durations`` is the length of each window in seconds, the test is
    SOL per second. Absolute SOL on a 30s / 150s / 885s split almost always
    rises while the rate is dying, and those entries are the ones this rejects.
    Omitting durations keeps the absolute comparison for a caller that already
    used equal windows.
    """
    if not slices or len(slices) != 3 or any(item is None for item in slices):
        return False, "flow windows incomplete"
    short, medium, long = (float(item) for item in slices)
    if durations is not None:
        if len(durations) != 3 or any(item is None or float(item) <= 0 for item in durations):
            return False, "flow windows incomplete"
        rates = [sol / float(dur) for sol, dur in zip((short, medium, long), durations)]
        if short < dust_sol or rates[1] <= rates[0] or rates[2] <= rates[1]:
            return False, f"flow not accelerating {rates[0]:+.4f}/{rates[1]:+.4f}/{rates[2]:+.4f} SOL/s"
        return True, f"flow accel {rates[0]:+.4f}/{rates[1]:+.4f}/{rates[2]:+.4f} SOL/s"
    if short < dust_sol or medium <= short or long <= medium:
        return False, f"flow not accelerating {short:+.3f}/{medium:+.3f}/{long:+.3f} SOL"
    return True, f"flow accel {short:+.3f}/{medium:+.3f}/{long:+.3f} SOL"


def _round_trip_score(pct, cap: float) -> float:
    """1 when the cap is off or the tape has no matched round trip. Missing is 0."""
    if pct is None:
        return 0.0
    if cap >= 100:
        return 1.0
    return max(0.0, (float(cap) - float(pct)) / max(float(cap), 1e-9))


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


def signal_strength(summary: dict, slices: list, strat: dict, strong: bool = False,
                    durations: list | None = None) -> float:
    """0 at the floor, 1 when breadth, imbalance, concentration, and acceleration are all clearly stronger.

    Top-3 slack, meaningful-buyer slack, last-window slack, and same-wallet
    round-trip slack participate with the older four terms. A measured
    last-window imbalance adds to that slack. A lower round-trip share sizes up.
    A measured last-window seller share sizes up when the offer is spread out.
    Missing distribution fields contribute 0, so an
    unmeasured tape cannot size up. A missing last-window seller share is left
    out of the average, so tapes recorded before that field do not shrink.
    When window lengths are passed, the
    acceleration term is the SOL/s ratio, so a longer last window does not by
    itself take the high-size credit. The result is still clamped to 0..1 for
    ``size_for_signal``.
    """
    need = strat["flow_strong_buyers"] if strong else strat["flow_min_buyers"]
    buyers = max(0.0, (summary["unique_buyers"] - need) / max(need, 1))
    imbalance = max(0.0, (summary["imbalance"] - strat["flow_min_bs_ratio"]) / max(strat["flow_min_bs_ratio"], 1e-9))
    whale = max(0.0, (strat["flow_max_whale_pct"] - summary["top_buyer_pct"]) / max(strat["flow_max_whale_pct"], 1e-9))
    accel = 0.0
    rate_windows = (
        isinstance(durations, (list, tuple)) and slices and len(durations) == len(slices) == 3
        and all(isinstance(item, (int, float)) and float(item) > 0 for item in durations)
    )
    if rate_windows:
        rate0 = float(slices[0]) / float(durations[0])
        rate_n = float(slices[-1]) / float(durations[-1])
        if rate0 > 0:
            accel = max(0.0, rate_n / rate0 - 1.0)
    elif slices and slices[0] and slices[0] > 0:
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
    late_buyers = summary.get("last_window_buyers")
    late_top = summary.get("last_window_top_pct")
    late_imbalance = summary.get("last_window_imbalance")
    need_late = int(strat.get("flow_last_window_min_buyers", 3))
    late_cap = float(strat.get("flow_last_window_max_whale_pct", 50))
    late_ratio = float(strat.get("flow_last_window_min_bs_ratio", strat.get("flow_min_bs_ratio", 1.3)))
    round_trip_cap = float(strat.get("flow_max_round_trip_pct", 50))
    if late_buyers is None or late_top is None:
        late_score = 0.0
    elif need_late <= 0 and late_cap >= 100 and (late_imbalance is None or late_ratio <= 0):
        late_score = 1.0
    else:
        buyer_score = 1.0 if need_late <= 0 else max(0.0, (float(late_buyers) - need_late) / max(need_late, 1))
        whale_score = 1.0 if late_cap >= 100 else max(0.0, (late_cap - float(late_top)) / max(late_cap, 1e-9))
        parts_late = [buyer_score, whale_score]
        # A missing bid does not dilute tapes recorded before this field existed.
        if late_imbalance is not None:
            if late_ratio <= 0:
                parts_late.append(1.0)
            else:
                parts_late.append(max(0.0, (float(late_imbalance) - late_ratio) / max(late_ratio, 1e-9)))
        late_rt = summary.get("last_window_round_trip_pct")
        # A missing last-window round trip does not dilute tapes recorded before the field.
        if late_rt is not None:
            parts_late.append(_round_trip_score(late_rt, round_trip_cap))
        late_score = sum(parts_late) / len(parts_late)
    round_trip_score = _round_trip_score(summary.get("round_trip_pct"), round_trip_cap)
    parts = [
        min(buyers, 1), min(imbalance, 1), min(whale, 1), min(accel, 1),
        min(top3_score, 1), min(meaningful_score, 1), min(late_score, 1), min(round_trip_score, 1),
    ]
    seller_pct = summary.get("last_window_top_seller_pct")
    if seller_pct is not None:
        seller_cap = float(strat.get("flow_last_window_max_seller_pct", 60))
        seller_floor = float(strat.get("flow_last_window_min_sell_sol", 0.15))
        seller_sol = summary.get("last_window_sell_sol")
        if seller_cap >= 100:
            seller_score = 1.0
        elif seller_sol is not None and float(seller_sol) + 1e-12 < seller_floor:
            seller_score = 1.0
        else:
            seller_score = max(0.0, (seller_cap - float(seller_pct)) / max(seller_cap, 1e-9))
        parts.append(min(seller_score, 1.0))
    return max(0.0, min(1.0, sum(parts) / len(parts)))


def size_for_signal(strength: float, strat: dict) -> float:
    """Map strength into the existing size band. Does not leave ``size_min``..``size_max``."""
    lo, hi = float(strat["size_min"]), float(strat["size_max"])
    usd = lo + max(0.0, min(1.0, float(strength))) * (hi - lo)
    return max(lo, min(hi, usd))
