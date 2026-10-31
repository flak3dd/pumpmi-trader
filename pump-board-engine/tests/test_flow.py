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
