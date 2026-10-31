from src.flow import (
    accelerating,
    flow_allows,
    flow_reversed,
    signal_strength,
    size_for_signal,
    summarize,
    trade_from_tx,
)
from src.live_feed import STRAT


def _tx(curve, pre, post, loaded=False, signer="buyer-1"):
    keys = [{"pubkey": signer, "signer": True, "writable": True}, {"pubkey": "other", "signer": False}]
    meta = {"err": None, "preBalances": pre, "postBalances": post, "logMessages": ["Program log: Instruction: Buy"]}
    if loaded:
        meta["loadedAddresses"] = {"writable": [curve], "readonly": []}
    else:
        keys.append({"pubkey": curve, "signer": False})
    return {"meta": meta, "transaction": {"message": {"accountKeys": keys}}, "blockTime": 10}


def test_trade_from_loaded_curve_account_is_a_buy():
    curve = "CURVE"
    # static keys then loaded writable. Curve delta +1 SOL at the loaded index.
    trade = trade_from_tx(_tx(curve, [2_000_000_000, 1_000_000_000, 5_000_000_000],
                              [1_000_000_000, 1_000_000_000, 6_000_000_000], loaded=True), curve)
    assert trade["side"] == "buy" and abs(trade["quote_sol"] - 1) < 1e-9 and trade["user"] == "buyer-1"


def test_sell_and_dust_are_classified():
    curve = "CURVE"
    sell = trade_from_tx(_tx(curve, [1_000_000_000, 0, 5_000_000_000], [2_000_000_000, 0, 4_000_000_000]), curve)
    assert sell["side"] == "sell" and abs(sell["quote_sol"] - 1) < 1e-9
    dust = trade_from_tx(_tx(curve, [0, 0, 1_000_000_000], [0, 0, 1_000_001_000]), curve)
    assert dust is None


def test_summary_and_gate_never_passes_when_missing():
    ok, reason = flow_allows(None, STRAT)
    assert not ok and reason == "flow unavailable"
    empty = summarize([])
    ok, reason = flow_allows(empty, STRAT)
    assert not ok and "buyers" in reason
    trades = [{"side": "buy", "quote_sol": 0.1, "user": f"u{i}"} for i in range(16)]
    trades += [{"side": "sell", "quote_sol": 0.05, "user": "s"}]
    summary = summarize(trades)
    assert summary["unique_buyers"] == 16
    assert summary["signed_quote_sol"] > 0
    assert summary["top_buyer_pct"] < 20
    assert flow_allows(summary, STRAT)[0]
    assert not flow_allows(summary, STRAT, strong=True)[0]
    whale = summarize([{"side": "buy", "quote_sol": 5, "user": "solo"}] + trades)
    assert not flow_allows(whale, STRAT)[0]
    heavy_sell = summarize(trades + [{"side": "sell", "quote_sol": 5, "user": "s2"}])
    rejected, why = flow_allows(heavy_sell, STRAT)
    assert not rejected and "signed" in why
    thin_edge = summarize(
        [{"side": "buy", "quote_sol": 0.1, "user": f"b{i}"} for i in range(16)]
        + [{"side": "sell", "quote_sol": 1.4, "user": "seller"}]
    )
    rejected, why = flow_allows(thin_edge, STRAT)
    assert not rejected and "imbalance" in why


def test_acceleration_and_reversal():
    assert accelerating([0.2, 0.4, 0.7], 0.01)[0]
    assert not accelerating([0.4, 0.3, 0.8], 0.01)[0]
    assert not accelerating([0.2, 0.4], 0.01)[0]
    assert not accelerating(None, 0.01)[0]


def test_rate_acceleration_rejects_a_fade_absolute_sol_accepts():
    """Saved watches are 30s / 150s / 885s. Absolute SOL rises while the rate collapses."""
    slices = [0.60, 0.75, 0.885]
    durations = [30, 150, 885]
    assert accelerating(slices, 0.01)[0]
    rejected, why = accelerating(slices, 0.01, durations)
    assert not rejected and "SOL/s" in why
    # 0.01, 0.02, 0.03 SOL/s across those same windows.
    rising = [0.30, 3.0, 26.55]
    assert accelerating(rising, 0.01, durations)[0]
    flat = [0.30, 1.50, 8.85]
    assert accelerating(flat, 0.01)[0]
    assert not accelerating(flat, 0.01, durations)[0]
    assert not accelerating(rising, 0.01, [30, 0, 885])[0]
    entry = 10_000_000_000
    assert not flow_reversed(entry, entry - 10_000_000, 1.0, dust_sol=0.05, giveback=0.25)
    assert flow_reversed(entry, entry - 400_000_000, 1.0, dust_sol=0.05, giveback=0.25)
    assert not flow_reversed(None, entry, 1.0)


def test_signal_size_stays_inside_the_band():
    assert size_for_signal(0, STRAT) == STRAT["size_min"]
    assert size_for_signal(1, STRAT) == STRAT["size_max"]
    assert size_for_signal(5, STRAT) == STRAT["size_max"]
    summary = summarize([{"side": "buy", "quote_sol": 0.1, "user": f"u{i}"} for i in range(40)]
                        + [{"side": "sell", "quote_sol": 0.2, "user": "s"}])
    strength = signal_strength(summary, [0.2, 0.5, 1.0], STRAT)
    usd = size_for_signal(strength, STRAT)
    assert STRAT["size_min"] <= usd <= STRAT["size_max"]
    assert strength > 0.5


def _distributed_base():
    """Fifteen economically real buyers plus a smaller sell. Clears the v8 tape gate."""
    trades = [{"side": "buy", "quote_sol": 0.1, "user": f"u{i}"} for i in range(16)]
    trades.append({"side": "sell", "quote_sol": 0.4, "user": "s"})
    return trades


def test_top3_rejects_a_book_the_single_whale_cap_allows():
    """Three wallets under 20% each can still own the tape. The previous gate bought that."""
    trades = [
        {"side": "buy", "quote_sol": 1.9, "user": "w1"},
        {"side": "buy", "quote_sol": 1.8, "user": "w2"},
        {"side": "buy", "quote_sol": 1.7, "user": "w3"},
    ]
    rest = 4.6
    trades += [{"side": "buy", "quote_sol": rest / 12, "user": f"d{i}"} for i in range(12)]
    trades.append({"side": "sell", "quote_sol": 4.0, "user": "seller"})
    summary = summarize(trades)
    assert summary["unique_buyers"] == 15
    assert summary["top_buyer_pct"] <= STRAT["flow_max_whale_pct"]
    assert summary["top3_buyer_pct"] > STRAT["flow_max_top3_pct"]
    assert summary["meaningful_buyers"] >= STRAT["flow_min_meaningful_buyers"]
    loose = {**STRAT, "flow_max_top3_pct": 100}
    assert flow_allows(summary, loose)[0]
    rejected, why = flow_allows(summary, STRAT)
    assert not rejected and why.startswith("flow top3")


def test_meaningful_buyers_reject_dust_that_inflates_the_unique_count():
    """Sixteen dust wallets clear the unique-buyer and whale caps and still are not distributed demand."""
    trades = [{"side": "buy", "quote_sol": 0.02, "user": f"u{i}"} for i in range(16)]
    trades.append({"side": "sell", "quote_sol": 0.1, "user": "s"})
    summary = summarize(trades)
    assert summary["unique_buyers"] >= STRAT["flow_min_buyers"]
    assert summary["top_buyer_pct"] < STRAT["flow_max_whale_pct"]
    assert summary["top3_buyer_pct"] < STRAT["flow_max_top3_pct"]
    assert summary["meaningful_buyers"] == 0
    loose = {**STRAT, "flow_min_meaningful_buyers": 0, "flow_strong_meaningful_buyers": 0}
    assert flow_allows(summary, loose)[0]
    rejected, why = flow_allows(summary, STRAT)
    assert not rejected and "meaningful" in why
    # Two dust prints from the same wallet add up. Seven extra dust wallets keep the unique count legal.
    repeated = [{"side": "buy", "quote_sol": 0.03, "user": f"u{i}"} for i in range(8) for _ in range(2)]
    repeated += [{"side": "buy", "quote_sol": 0.02, "user": f"d{i}"} for i in range(7)]
    repeated.append({"side": "sell", "quote_sol": 0.2, "user": "s"})
    rolled = summarize(repeated)
    assert rolled["unique_buyers"] == 15
    assert rolled["meaningful_buyers"] == 8
    assert flow_allows(rolled, STRAT)[0]


def test_upper_band_requires_more_meaningful_buyers_and_threshold_is_configurable():
    trades = _distributed_base()
    summary = summarize(trades)
    assert summary["meaningful_buyers"] == 16
    assert flow_allows(summary, STRAT)[0]
    # Enough unique wallets for the upper curve band, but only eight are economically real.
    wide = summarize(
        [{"side": "buy", "quote_sol": 0.1, "user": f"b{i}"} for i in range(8)]
        + [{"side": "buy", "quote_sol": 0.02, "user": f"d{i}"} for i in range(20)]
        + [{"side": "sell", "quote_sol": 0.3, "user": "s"}]
    )
    assert wide["unique_buyers"] >= STRAT["flow_strong_buyers"]
    assert wide["meaningful_buyers"] == 8
    assert flow_allows(wide, STRAT)[0]
    rejected, why = flow_allows(wide, STRAT, strong=True)
    assert not rejected and "meaningful buyers 8 < 12" in why
    finer = summarize(trades, meaningful_sol=0.2)
    assert finer["meaningful_buyers"] == 0
    assert summarize(trades, meaningful_sol=0.05)["meaningful_buyers"] == 16


def test_cleaner_distribution_sizes_larger_without_leaving_the_band():
    slices = [0.2, 0.4, 0.7]
    broad = dict(
        unique_buyers=24, imbalance=2.5, top_buyer_pct=8, top3_buyer_pct=18,
        meaningful_buyers=20, buy_sol=2.4, sell_sol=1.0, signed_quote_sol=1.4, trades=24,
    )
    tight = dict(broad, top3_buyer_pct=44, meaningful_buyers=8)
    high = signal_strength(broad, slices, STRAT)
    low = signal_strength(tight, slices, STRAT)
    assert high > low
    assert STRAT["size_min"] <= size_for_signal(low, STRAT) <= size_for_signal(high, STRAT) <= STRAT["size_max"]
    # An unmeasured distribution cannot take the high-size credit.
    unmeasured = dict(broad)
    unmeasured.pop("top3_buyer_pct")
    unmeasured.pop("meaningful_buyers")
    assert signal_strength(unmeasured, slices, STRAT) < high


def test_longer_window_does_not_take_the_acceleration_size_credit():
    summary = dict(
        unique_buyers=24, imbalance=2.5, top_buyer_pct=8, top3_buyer_pct=18,
        meaningful_buyers=20, buy_sol=2.4, sell_sol=1.0, signed_quote_sol=1.4, trades=24,
    )
    # Flat 0.01 SOL/s. Absolute SOL makes the last window look like a surge.
    flat = [0.30, 1.50, 8.85]
    durations = [30, 150, 885]
    rated = signal_strength(summary, flat, STRAT, durations=durations)
    absolute = signal_strength(summary, flat, STRAT)
    assert rated < absolute
    rising = [0.30, 3.0, 26.55]
    assert signal_strength(summary, rising, STRAT, durations=durations) > rated
    assert STRAT["size_min"] <= size_for_signal(rated, STRAT) <= size_for_signal(absolute, STRAT) <= STRAT["size_max"]
