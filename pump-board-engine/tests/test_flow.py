import pytest

from src.curve import buy_fill, flat_round_trip_pct, sell_eff, sol_price_usd
from src.flow import (
    accelerating,
    flow_allows,
    flow_reversed,
    last_window_allows,
    signal_strength,
    size_for_signal,
    summarize,
    trade_from_tx,
    with_last_window,
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


def test_last_window_rejects_one_wallet_the_whole_tape_caps_allow():
    """Sixteen early buyers clear the tape. One wallet then buys the entry window."""
    early = [{"side": "buy", "quote_sol": 0.2, "user": f"u{i}", "ts": 100} for i in range(16)]
    early.append({"side": "sell", "quote_sol": 0.4, "user": "s", "ts": 110})
    late = [{"side": "buy", "quote_sol": 0.6, "user": "chaser", "ts": 250}]
    summary = with_last_window(summarize(early + late), early + late, 200)
    assert flow_allows(summary, STRAT)[0]
    assert summary["top_buyer_pct"] <= STRAT["flow_max_whale_pct"]
    assert summary["last_window_buyers"] == 1
    assert summary["last_window_top_pct"] == 100
    rejected, why = last_window_allows(summary, STRAT)
    assert not rejected and why.startswith("flow last window buyers")
    whale_window = dict(
        summary, last_window_buyers=4, last_window_top_pct=80, last_window_buy_sol=1.0,
        last_window_signed_sol=0.8, last_window_imbalance=2.0,
    )
    rejected, why = last_window_allows(whale_window, STRAT)
    assert not rejected and "top buyer" in why
    loose = {**STRAT, "flow_last_window_min_buyers": 0, "flow_last_window_max_whale_pct": 100}
    assert last_window_allows(summary, loose)[0]
    assert last_window_allows({}, STRAT)[1] == "flow last window unavailable"
    undated = with_last_window(summarize(early + late), early + late, None)
    assert last_window_allows(undated, STRAT)[1] == "flow last window unavailable"


def test_broader_last_window_sizes_larger_without_leaving_the_band():
    slices = [0.2, 0.4, 0.7]
    base = dict(
        unique_buyers=24, imbalance=2.5, top_buyer_pct=8, top3_buyer_pct=18,
        meaningful_buyers=20, buy_sol=2.4, sell_sol=1.0, signed_quote_sol=1.4, trades=24,
    )
    broad = dict(
        base, last_window_buyers=9, last_window_top_pct=15, last_window_buy_sol=1.2,
        last_window_signed_sol=1.0, last_window_imbalance=2.6, last_window_round_trip_pct=0.0,
    )
    thin = dict(
        base, last_window_buyers=3, last_window_top_pct=49, last_window_buy_sol=0.4,
        last_window_signed_sol=0.3, last_window_imbalance=1.4, last_window_round_trip_pct=0.0,
    )
    assert last_window_allows(broad, STRAT)[0]
    assert last_window_allows(thin, STRAT)[0]
    high = signal_strength(broad, slices, STRAT)
    low = signal_strength(thin, slices, STRAT)
    assert high > low
    assert STRAT["size_min"] <= size_for_signal(low, STRAT) <= size_for_signal(high, STRAT) <= STRAT["size_max"]


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


def _distributed_then_sold_window():
    """Whole tape clears the older caps. The entry window is four small bids into a larger sell."""
    early = [{"side": "buy", "quote_sol": 0.2, "user": f"u{i}", "ts": 100} for i in range(16)]
    early.append({"side": "sell", "quote_sol": 0.5, "user": "s", "ts": 110})
    late = [{"side": "buy", "quote_sol": 0.15, "user": f"b{i}", "ts": 250} for i in range(4)]
    late.append({"side": "sell", "quote_sol": 1.2, "user": "dumper", "ts": 260})
    return with_last_window(summarize(early + late), early + late, 200)


def test_last_window_net_sell_skips_a_tape_the_breadth_caps_allow():
    summary = _distributed_then_sold_window()
    assert flow_allows(summary, STRAT)[0]
    assert summary["last_window_buyers"] == 4
    assert summary["last_window_top_pct"] == 25
    assert summary["last_window_signed_sol"] < 0
    rejected, why = last_window_allows(summary, STRAT)
    assert not rejected and why.startswith("flow last window imbalance")
    # The same breadth with the sell removed is a net bid and still passes.
    bids = [{"side": "buy", "quote_sol": 0.2, "user": f"u{i}", "ts": 100} for i in range(16)]
    bids.append({"side": "sell", "quote_sol": 0.5, "user": "s", "ts": 110})
    late = [{"side": "buy", "quote_sol": 0.2, "user": f"b{i}", "ts": 250} for i in range(4)]
    healthy = with_last_window(summarize(bids + late), bids + late, 200)
    assert last_window_allows(healthy, STRAT)[0]
    disabled = {**STRAT, "flow_last_window_min_bs_ratio": 0}
    assert last_window_allows(summary, disabled)[0]
    assert last_window_allows(dict(healthy, last_window_signed_sol=None, last_window_imbalance=None), STRAT)[1] == (
        "flow last window imbalance unavailable"
    )
    # $130 flat round trip on the feed's test curve. Taking the tape pays this; skipping pays 0.
    spot, usd, sol_px = 30_000.0, STRAT["size_min"], 150.0
    virtual_sol, virtual_token = 40_000_000_000, 1_000_000_000_000_000
    filled = buy_fill(spot, usd, sol_px, virtual_sol, virtual_token, STRAT["curve_fee_bps"])
    flat = sell_eff(spot, filled["tokens"], virtual_sol, virtual_token, STRAT["curve_fee_bps"])
    flat_pnl = usd * flat / filled["entry_eff"] - usd
    assert flat_pnl == pytest.approx(-7.828171445, abs=1e-6)
    # An exact -15% net stop on the same $130 books -19.50. Skipping books 0.
    assert usd * -0.15 == -19.5


def test_stronger_last_window_bid_sizes_larger_without_leaving_the_band():
    base = dict(
        unique_buyers=24, imbalance=2.5, top_buyer_pct=8, top3_buyer_pct=18,
        meaningful_buyers=20, buy_sol=2.4, sell_sol=1.0, signed_quote_sol=1.4, trades=24,
        last_window_buyers=6, last_window_top_pct=20, last_window_buy_sol=0.8,
        last_window_round_trip_pct=0.0,
    )
    weak = dict(base, last_window_signed_sol=0.2, last_window_imbalance=1.3)
    strong = dict(base, last_window_signed_sol=0.8, last_window_imbalance=2.6)
    assert last_window_allows(weak, STRAT)[0]
    assert last_window_allows(strong, STRAT)[0]
    assert signal_strength(strong, [0.2, 0.4, 0.7], STRAT) > signal_strength(weak, [0.2, 0.4, 0.7], STRAT)
    low = size_for_signal(signal_strength(weak, [0.2, 0.4, 0.7], STRAT), STRAT)
    high = size_for_signal(signal_strength(strong, [0.2, 0.4, 0.7], STRAT), STRAT)
    assert STRAT["size_min"] <= low <= high <= STRAT["size_max"]


def test_same_wallet_round_trip_skips_a_tape_the_imbalance_cap_allows():
    """Sixteen wallets each buy and sell. Buy/sell still clears 1.3. The buyers are the sellers."""
    trades = []
    for index in range(16):
        trades.append({"side": "buy", "quote_sol": 0.40, "user": f"u{index}"})
        trades.append({"side": "sell", "quote_sol": 0.22, "user": f"u{index}"})
    summary = summarize(trades)
    assert summary["unique_buyers"] == 16
    assert summary["imbalance"] >= STRAT["flow_min_bs_ratio"]
    assert summary["top_buyer_pct"] < STRAT["flow_max_whale_pct"]
    assert summary["top3_buyer_pct"] < STRAT["flow_max_top3_pct"]
    assert summary["meaningful_buyers"] >= STRAT["flow_min_meaningful_buyers"]
    assert summary["round_trip_pct"] > STRAT["flow_max_round_trip_pct"]
    rejected, why = flow_allows(summary, STRAT)
    assert not rejected and why.startswith("flow round trip")
    external = []
    for index in range(16):
        external.append({"side": "buy", "quote_sol": 0.40, "user": f"u{index}"})
        external.append({"side": "sell", "quote_sol": 0.22, "user": f"s{index}"})
    clean = summarize(external)
    assert clean["round_trip_pct"] == 0
    assert flow_allows(clean, STRAT)[0]
    disabled = {**STRAT, "flow_max_round_trip_pct": 100}
    assert flow_allows(summary, disabled)[0]
    assert flow_allows(dict(clean, round_trip_pct=None), STRAT)[1] == "flow round trip unavailable"
    # Same SOL, but the entry window is the churn. The whole tape stays under the cap.
    early = [{"side": "buy", "quote_sol": 0.30, "user": f"u{i}", "ts": 100} for i in range(16)]
    early.append({"side": "sell", "quote_sol": 0.50, "user": "outsider", "ts": 110})
    late = []
    for index in range(4):
        late.append({"side": "buy", "quote_sol": 0.40, "user": f"b{index}", "ts": 250})
        late.append({"side": "sell", "quote_sol": 0.25, "user": f"b{index}", "ts": 260})
    window = with_last_window(summarize(early + late), early + late, 200)
    assert flow_allows(window, STRAT)[0]
    assert window["last_window_imbalance"] >= STRAT["flow_last_window_min_bs_ratio"]
    assert window["last_window_round_trip_pct"] > STRAT["flow_max_round_trip_pct"]
    rejected, why = last_window_allows(window, STRAT)
    assert not rejected and why.startswith("flow last window round trip")
    recycled = dict(clean, round_trip_pct=45.0)
    fresh = dict(clean, round_trip_pct=0.0)
    assert signal_strength(fresh, [0.2, 0.4, 0.7], STRAT) > signal_strength(recycled, [0.2, 0.4, 0.7], STRAT)
    low = size_for_signal(signal_strength(recycled, [0.2, 0.4, 0.7], STRAT), STRAT)
    high = size_for_signal(signal_strength(fresh, [0.2, 0.4, 0.7], STRAT), STRAT)
    assert STRAT["size_min"] <= low <= high <= STRAT["size_max"]
    # $2,000 spot on the test reserves. Taking it pays this flat loss; skipping pays 0.
    spot, usd = 2_000.0, STRAT["size_min"]
    virtual_sol, virtual_token = 40_000_000_000, 1_000_000_000_000_000
    sol_px = sol_price_usd(spot, virtual_sol, virtual_token)
    assert sol_px is not None
    drag = flat_round_trip_pct(spot, usd, sol_px, virtual_sol, virtual_token, STRAT["curve_fee_bps"])
    assert drag is not None and drag < STRAT["impact_max_drag_pct"]
    filled = buy_fill(spot, usd, sol_px, virtual_sol, virtual_token, STRAT["curve_fee_bps"])
    flat = sell_eff(spot, filled["tokens"], virtual_sol, virtual_token, STRAT["curve_fee_bps"])
    assert usd * flat / filled["entry_eff"] - usd < 0
