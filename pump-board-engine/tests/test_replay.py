import json

from src.replay import fee_only_pnl, report


def test_fee_only_flat_spot_is_a_loss_and_replay_splits_the_curve_band(tmp_path):
    assert fee_only_pnl(0, 100, 100) < 0
    fills = [
        {"symbol": "KEEP", "side": "BUY", "usd": "130", "pnl": "0", "ts": "10", "price": "1", "reason": "buy"},
        {"symbol": "KEEP", "side": "SELL", "usd": "150", "pnl": "20", "ts": "20", "price": "1", "reason": "time exit"},
        {"symbol": "EARLY", "side": "BUY", "usd": "130", "pnl": "0", "ts": "11", "price": "1", "reason": "buy"},
        {"symbol": "EARLY", "side": "SELL", "usd": "100", "pnl": "-30", "ts": "21", "price": "1", "reason": "stop loss"},
        {"symbol": "LATE", "side": "BUY", "usd": "130", "pnl": "0", "ts": "12", "price": "1", "reason": "buy"},
        {"symbol": "LATE", "side": "SELL", "usd": "140", "pnl": "10", "ts": "22", "price": "1", "reason": "time exit"},
    ]
    history = [
        {"key": "KEEP", "side": "BUY", "curve_pct": 40, "ts": 10, "sentiment_buy": False},
        {"key": "EARLY", "side": "BUY", "curve_pct": 22, "ts": 11, "sentiment_buy": False},
        {"key": "LATE", "side": "BUY", "curve_pct": 78, "ts": 12, "sentiment_buy": True, "mint": "late-mint",
         "sentiment_0_100": 80, "mention_volume": 100, "mcap": 40000, "replies": 1, "top10_pct": 10,
         "dev_pct": 1, "bundle_pct": 1, "rise_pts": 1, "size_pct_equity": 1, "age_s": 100},
    ]
    # Twenty-nine prior wins are still short of the hardened sample, so this buy is not boosted.
    for index in range(29):
        history.append({
            "key": f"OLD{index}", "side": "EXIT", "mint": f"old-{index}", "ts": index,
            "pnl_pct": 10, "sentiment_0_100": 80, "mcap": 40000, "replies": 10, "top10_pct": 10,
            "dev_pct": 1, "bundle_pct": 1, "rise_pts": 4, "mention_volume": 500, "size_pct_equity": 1,
            "age_s": 400, "curve_pct": 40,
        })
    summary = report(fills, history)
    assert summary["paper_only"] is True and summary["live_trading"] is False
    assert summary["gross_pnl"] == 0  # +20 -30 +10
    assert summary["fee_only_pnl"] < summary["gross_pnl"]
    assert summary["curve_band"]["keep"] == 1
    assert summary["curve_band"]["reject"] == 1
    assert summary["curve_band"]["reject_needs_breadth"] == 1
    assert summary["similarity"]["boost_new"] == 0
    assert "swap tape" in summary["not_replayable"][0]
    path = tmp_path / "report.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    assert json.loads(path.read_text())["round_trips"] == 3
