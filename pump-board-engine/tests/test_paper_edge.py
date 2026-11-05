"""Paper-feed behavior for the v8 expected-edge rules. No network and no orders."""
import json
from pathlib import Path
from unittest.mock import patch

import src.live_feed as live_feed
from src.exits import vol_stop_hit, vol_stop_pct
from src.holders import (
    account_ui_amount, concentration, largest_account_rows,
    rpc_error_kind, rpc_failure_kind, rpc_payload_kind,
)
from src.trade_charts import gates_buy
from src.live_feed import STRAT, Feed, public_sentiment_tier, sentiment_size_margin


class _MemoryLedger:
    def __init__(self, _path: str) -> None:
        self.do_not_rebuy: set[str] = set()
        self.fills: list[tuple] = []
        self.skips: list[tuple] = []

    def fill(self, *args) -> None:
        self.fills.append(args)

    def skip(self, *args) -> None:
        self.skips.append(args)

    def add_dnr(self, dev: str) -> None:
        if dev:
            self.do_not_rebuy.add(dev)


class _NoNetworkLunar:
    mode = "test"


def _coin(**overrides):
    coin = {
        "symbol": "EDGE",
        "mint": "edge-mint-0001",
        "name": "Edge",
        "creator": "creator-1",
        "usd_market_cap": 30_000,
        "reply_count": 40,
        "bonding_curve": "curve-addr",
        "created_timestamp": 1_000_000,
    }
    coin.update(overrides)
    return coin


def _state(curve=40.0, sol=5_000_000_000):
    return {
        "curve_pct": curve,
        "virtual_sol": 40_000_000_000,
        "virtual_token": 1_000_000_000_000_000,
        "real_token": 400_000_000_000_000,
        "real_sol": sol,
    }


def _flow(buyers=16):
    return {
        "signed_quote_sol": 1.5, "buy_sol": 2.4, "sell_sol": 0.9, "imbalance": 2.6,
        "unique_buyers": buyers, "top_buyer_pct": 8.0, "top3_buyer_pct": 18.0,
        "meaningful_buyers": buyers, "trades": buyers + 2,
        "last_window_buyers": buyers, "last_window_top_pct": 12.0, "last_window_buy_sol": 0.8,
        "last_window_signed_sol": 0.8, "last_window_imbalance": 4.0,
        "last_window_sell_sol": 0.2, "last_window_top_seller_pct": 25.0,
        "round_trip_pct": 0.0, "last_window_round_trip_pct": 0.0,
    }


# 90s / 90s / 120s windows. Rates 0.002, 0.004, 0.006 SOL/s, so the rate gate still buys.
SOLS = {1000.0: 5_000_000_000, 1090.0: 5_180_000_000, 1180.0: 5_540_000_000, 1300.0: 6_260_000_000}


def _run_watch(feed, coin, now, curve=40.0):
    feed.curve_state = lambda _coin: _state(curve, SOLS.get(now[0], 5_000_000_000))
    feed.evaluate(coin)
    for stamp in (1090.0, 1180.0, 1300.0):
        now[0] = stamp
        feed.recheck()


def _feed(tmp_path: Path, now: list[float]):
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps({
        "version": 1, "revision": 0, "max_open": 6,
        "watchPeriods": {"short": 90, "medium": 180, "long": 300},
    }), encoding="utf-8")
    return path


def test_rules_stay_paper_only_and_keep_six_slots():
    source = Path(live_feed.__file__).read_text(encoding="utf-8")
    assert "PUMP_ENGINE_ENABLE_LIVE" not in source
    assert "sendTransaction" not in source
    assert STRAT["max_open"] == 6
    assert STRAT["v6_size_mult"] == 1.5
    assert STRAT["v6_min_hist"] == 30
    assert STRAT["pos_check_s"] == 2.5
    assert STRAT["min_curve_pct"] == 2
    assert STRAT["max_curve_pct"] == 85
    assert STRAT["early_curve_pct"] == 30
    assert STRAT["early_curve_size_mult"] == 0.45
    assert STRAT["holder_gap_size_mult"] == 0.65
    assert STRAT["p3_strong_sentiment"] == 65
    assert STRAT["p3_strong_mentions"] == 150
    assert STRAT["sentiment_border_size_mult"] == 1.0
    assert STRAT["sentiment_size_weight"] == 1.0
    assert sentiment_size_margin(65, STRAT) == 0.5
    assert sentiment_size_margin(100, STRAT) == 1.0
    assert STRAT["curve_fee_bps"] == 100
    assert STRAT["flow_max_top3_pct"] == 45
    assert STRAT["flow_meaningful_sol"] == 0.05
    assert STRAT["flow_min_meaningful_buyers"] == 8
    assert STRAT["flow_strong_meaningful_buyers"] == 12
    assert STRAT["flow_last_window_min_buyers"] == 3
    assert STRAT["flow_last_window_max_whale_pct"] == 50
    assert STRAT["flow_last_window_min_bs_ratio"] == 1.3
    assert STRAT["flow_max_round_trip_pct"] == 50
    assert STRAT["flow_fallback_rise_pts"] == 3
    assert STRAT["flow_last_window_max_seller_pct"] == 60
    assert STRAT["flow_last_window_min_sell_sol"] == 0.15
    assert STRAT["fail_impulse_drop_pct"] == 8
    assert STRAT["fail_impulse_max_peak_pct"] == 2
    assert STRAT["impact_max_drag_pct"] == -10
    assert STRAT["impact_size_budget_pct"] == -4
    assert STRAT["profit_lock_arm_pct"] == 8
    assert STRAT["profit_lock_giveback_pct"] == 6
    try:
        Feed({"mode": "live", "bankroll_usd": 100})
    except AssertionError:
        pass
    else:
        raise AssertionError("live mode must be refused")


def test_missing_flow_replies_and_holders_do_not_soft_pass(tmp_path: Path) -> None:
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    coin = _coin()
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(coin)):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: None
        _run_watch(feed, coin, now)
        assert not feed.pos
        assert feed.ledger.skips[-1][1] == "flow n/a -> fallback (replies 40 ignored, curve +0.0pts < 3)"

        now[0] = 1000.0
        feed.holders = lambda _coin: (None, None, None, None)
        feed.curve_state = lambda _coin: _state()
        feed.evaluate(_coin(mint="edge-mint-0002", symbol="HOLD"))
        assert feed.ledger.skips[-1][1] == "P0 holder data missing"
        assert "edge-mint-0002" not in feed.watch

        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.curve_state = lambda _coin: _state(curve=1)
        feed.evaluate(_coin(mint="edge-mint-0003", symbol="LOW"))
        assert "P1 curve" in feed.ledger.skips[-1][1]
        assert "edge-mint-0003" not in feed.watch


def test_upper_band_needs_stronger_breadth_and_boost_is_capped_at_one_and_a_half(tmp_path: Path) -> None:
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    coin = _coin(symbol="HIGH", mint="high-mint-0001")
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.SIM, "decide", return_value={"decision": "boost", "score": 0.2, "knn_win": 4, "knn_n": 5, "n_hist": 30, "nw": 16, "nl": 14}), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(coin)):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: {"mention_volume": 500, "sentiment_0_100": 80}
        feed.flow_for = lambda _coin, _since, _last=None: _flow(16)
        _run_watch(feed, coin, now, curve=80)
        assert not feed.pos
        assert "buyers 16 < 25" in feed.ledger.skips[-1][1]

        now[0] = 1000.0
        rich = _coin(symbol="BOOST", mint="boost-mint-001")
        feed.social = lambda _symbol: {"mention_volume": 500, "sentiment_0_100": 80}
        feed.flow_for = lambda _coin, _since, _last=None: _flow(30)
        feed.http = lambda *_a, **_k: dict(rich)  # noqa: unused, recheck uses the patched http
        with patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(rich)):
            _run_watch(feed, rich, now, curve=40)
        assert len(feed.pos) == 1
        usd = next(iter(feed.pos.values()))["usd"]
        assert usd <= STRAT["size_max"] * STRAT["v6_size_mult"]
        assert usd > STRAT["size_min"]
        assert usd < STRAT["size_max"] * 3


def test_exits_emergency_vol_breakeven_and_flow_reversal(tmp_path: Path) -> None:
    path = _feed(tmp_path, [1_000.0])
    now = [5_000.0]
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None

        def plant(key, spot=100.0):
            feed.pos[key] = dict(
                mint=key, usd=130, entry=spot / 0.99, entry_eff=spot / 0.99, entry_spot=spot,
                peak=spot, last_spot=spot, t=now[0] - 30, creator="c", dev0=None, sup=None,
                scaled=False, fee_bps=100, tokens=None, entry_usd=130, scaled_pnl=0, rets=[],
                be_armed=False, entry_real_sol=10_000_000_000, entry_flow_sol=1.0, trail_giveback_pct=12,
            )

        plant("GAP")
        feed.apply_mark("GAP", {"spot": 89})
        assert "GAP" not in feed.pos
        assert feed.ledger.fills[-1][5].startswith("emergency drop")

        plant("VOL")
        feed.pos["VOL"]["rets"] = [0.001] * 6
        feed.apply_mark("VOL", {"spot": 93})
        assert "VOL" not in feed.pos
        assert feed.ledger.fills[-1][5].startswith("vol stop")

        # Entry is marked 10% through the fee-only exit at the same spot, so net is
        # −10% while spot has not moved. That used to lock the spread as a vol stop.
        # $130 * (99 / 110 - 1) = −13.00. The position stays open. A later −4% spot
        # print is still the quiet band and does exit.
        plant("SPREAD")
        feed.pos["SPREAD"]["entry"] = 110
        feed.pos["SPREAD"]["entry_eff"] = 110
        feed.pos["SPREAD"]["entry_spot"] = 100
        feed.pos["SPREAD"]["peak"] = 100
        feed.pos["SPREAD"]["last_spot"] = 100
        feed.pos["SPREAD"]["rets"] = [0.001] * 6
        feed.apply_mark("SPREAD", {"spot": 100})
        assert "SPREAD" in feed.pos
        assert [row for row in feed.ledger.fills if row[0] == "SPREAD"] == []
        # Fee-only sell at the entry spot is 99 against an entry of 110, so $130 loses 13.
        assert round(130 * (99 / 110 - 1), 6) == -13
        feed.apply_mark("SPREAD", {"spot": 96})
        assert "SPREAD" not in feed.pos
        assert feed.ledger.fills[-1][0] == "SPREAD"
        assert feed.ledger.fills[-1][5].startswith("vol stop")
        assert feed.ledger.fills[-1][4] < -13.0

        plant("ARM")
        feed.apply_mark("ARM", {"spot": 120})
        assert feed.pos["ARM"]["be_armed"] is True

        # A peak under the profit-lock arm still uses break-even once that stop was armed.
        # The step is inside the 10% emergency gap.
        plant("BE")
        feed.pos["BE"]["be_armed"] = True
        feed.pos["BE"]["peak_net"] = 4.0
        feed.pos["BE"]["last_spot"] = 105
        feed.apply_mark("BE", {"spot": 100})
        assert "BE" not in feed.pos
        assert feed.ledger.fills[-1][5].startswith("break-even")

        plant("LOCK")
        feed.apply_mark("LOCK", {"spot": 112})
        assert "LOCK" in feed.pos
        assert not feed.pos["LOCK"].get("be_armed")
        feed.apply_mark("LOCK", {"spot": 105})
        assert "LOCK" not in feed.pos
        assert feed.ledger.fills[-1][5].startswith("profit lock")
        assert feed.ledger.fills[-1][4] > 0

        plant("SCALE")
        feed.apply_mark("SCALE", {"spot": 130})
        assert feed.pos["SCALE"].get("scaled") is True
        feed.apply_mark("SCALE", {"spot": 118})
        assert "SCALE" in feed.pos

        plant("FLOW", spot=100)
        feed.apply_mark("FLOW", {"spot": 100, "real_sol": 9_600_000_000})
        assert "FLOW" not in feed.pos
        assert feed.ledger.fills[-1][5].startswith("flow exit")

        # Wild returns widen the vol band to the hard stop, so −8% spot used to stay open
        # until net itself was −15% ($130 * −0.15 = −19.50). Fee-only at spot 92 is about −12.78.
        plant("IMPULSE")
        wild = [0.2, -0.2, 0.25, -0.25, 0.2, -0.2]
        feed.pos["IMPULSE"]["rets"] = list(wild)
        feed.pos["IMPULSE"]["peak"] = 100
        feed.pos["IMPULSE"]["last_spot"] = 100
        band = vol_stop_pct(wild, STRAT)
        assert band == STRAT["stop_loss_pct"]
        assert not vol_stop_hit(100, 92, band, STRAT["stop_loss_pct"])
        entry_eff = 100 / 0.99
        exit_at_92 = 92 * 0.99
        held_net = (exit_at_92 / entry_eff - 1.0) * 100.0
        assert STRAT["stop_loss_pct"] < held_net < 0
        feed.apply_mark("IMPULSE", {"spot": 92})
        assert "IMPULSE" not in feed.pos
        impulse = feed.ledger.fills[-1]
        assert impulse[0] == "IMPULSE"
        assert impulse[5].startswith("failed impulse")
        assert -19.50 < impulse[4] < 0

        # A +6% spot bounce is past the +2% ceiling, so the same −8% print stays open.
        plant("BOUNCE")
        feed.pos["BOUNCE"]["rets"] = list(wild)
        feed.pos["BOUNCE"]["peak"] = 106
        feed.pos["BOUNCE"]["last_spot"] = 100
        feed.apply_mark("BOUNCE", {"spot": 92})
        assert "BOUNCE" in feed.pos


def test_distribution_gates_skip_before_a_paper_buy(tmp_path: Path) -> None:
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    concentrated = {
        "signed_quote_sol": 6.0, "buy_sol": 10.0, "sell_sol": 4.0, "imbalance": 2.5,
        "unique_buyers": 16, "top_buyer_pct": 19.0, "top3_buyer_pct": 54.0,
        "meaningful_buyers": 16, "trades": 20,
    }
    dust = {
        "signed_quote_sol": 0.22, "buy_sol": 0.32, "sell_sol": 0.10, "imbalance": 3.2,
        "unique_buyers": 16, "top_buyer_pct": 6.0, "top3_buyer_pct": 19.0,
        "meaningful_buyers": 0, "trades": 17,
    }
    holder = {"coin": _coin(symbol="WHALES", mint="whale-mint-0001")}
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(holder["coin"])):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: concentrated
        _run_watch(feed, holder["coin"], now)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("flow top3")

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="DUST", mint="dust-mint-0001")
        feed.flow_for = lambda _coin, _since, _last=None: dust
        _run_watch(feed, holder["coin"], now)
        assert not feed.pos
        assert "meaningful" in feed.ledger.skips[-1][1]
        assert feed.ledger.fills == []
        assert STRAT["curve_fee_bps"] == 100


def test_position_check_runs_before_the_scan(tmp_path: Path) -> None:
    path = _feed(tmp_path, [0.0])
    now = [1_000.0]
    order = []
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]):
        feed = Feed({"mode": "paper", "bankroll_usd": 1_500}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.manage_fast = lambda: order.append("positions")
        feed.scan = lambda: order.append("scan")
        feed.write = lambda: order.append("write")
        assert feed.cycle(now=1_000) is True
        assert order == ["positions", "scan", "write"]
        order.clear()
        assert feed.cycle(now=1_005) is False
        assert order == ["positions", "write"]


def test_fading_rate_on_the_saved_watch_does_not_buy(tmp_path: Path) -> None:
    """30/180/1065 is the saved config. Absolute SOL rises; SOL per second does not."""
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps({
        "version": 1, "revision": 0, "max_open": 6,
        "watchPeriods": {"short": 30, "medium": 180, "long": 1065},
    }), encoding="utf-8")
    now = [1000.0]
    # +0.60 / 30s, +0.75 / 150s, +0.885 / 885s.
    sols = {
        1000.0: 5_000_000_000, 1030.0: 5_600_000_000,
        1180.0: 6_350_000_000, 2065.0: 7_235_000_000,
    }
    coin = _coin(symbol="FADE", mint="fade-mint-0001")
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(coin)):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: _flow(16)
        feed.curve_state = lambda _coin: _state(40.0, sols.get(now[0], 5_000_000_000))
        feed.evaluate(coin)
        for stamp in (1030.0, 1180.0, 2065.0):
            now[0] = stamp
            feed.recheck()
        assert not feed.pos
        assert "SOL/s" in feed.ledger.skips[-1][1]
        assert feed.ledger.fills == []


def test_last_window_one_wallet_skips_before_a_paper_buy(tmp_path: Path) -> None:
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    # Whole tape clears the older caps. The entry window is one wallet.
    chased = dict(_flow(16), last_window_buyers=1, last_window_top_pct=100.0, last_window_buy_sol=0.6)
    coin = _coin(symbol="CHASE", mint="chase-mint-0001")
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(coin)):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: chased
        _run_watch(feed, coin, now)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("flow last window buyers")
        assert feed.ledger.fills == []
        started = Feed._last_window_start({
            "t": 1000.0,
            "watch_periods": {"medium": {"status": "observed", "elapsed_s": 180.0, "duration_s": 90}},
        })
        assert started == 1180.0
        assert Feed._last_window_start({"t": 1000.0, "watch_periods": {"medium": {"status": "watching", "elapsed_s": 0}}}) is None


def test_last_window_net_sell_skips_before_a_paper_buy(tmp_path: Path) -> None:
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    # Breadth clears the older last-window caps. The window itself is net selling.
    offered = dict(
        _flow(16),
        last_window_buyers=4, last_window_top_pct=25.0, last_window_buy_sol=0.6,
        last_window_signed_sol=-0.6, last_window_imbalance=0.5,
    )
    coin = _coin(symbol="OFFER", mint="offer-mint-0001")
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(coin)):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: offered
        _run_watch(feed, coin, now)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("flow last window imbalance")
        assert feed.ledger.fills == []


def test_last_window_one_seller_skips_before_a_paper_buy(tmp_path: Path) -> None:
    """Buyer caps, the 1.3 bid, and a zero round trip all pass. One wallet is the offer.

    That seller never has to have bought. Skipping pays 0. A flat $130 round trip
    on the test curve is a loss, and a −15% net stop on that notional is −19.50.
    """
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    dumped = dict(
        _flow(16),
        last_window_buyers=4, last_window_top_pct=25.0, last_window_buy_sol=1.0,
        last_window_signed_sol=0.30, last_window_imbalance=1.43, last_window_round_trip_pct=0.0,
        last_window_sell_sol=0.70, last_window_top_seller_pct=100.0, round_trip_pct=0.0,
    )
    coin = _coin(symbol="DUMP", mint="dump-mint-0001")
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(coin)):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: dumped
        _run_watch(feed, coin, now)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("flow last window top seller")
        assert feed.ledger.fills == []
        assert STRAT["size_min"] * -0.15 == -19.5


def test_round_trip_and_thin_dollar_depth_skip_before_a_paper_buy(tmp_path: Path) -> None:
    """Same-wallet churn and a size that cannot pay for the curve both skip.

    The churn tape clears buyers, whale, top-3, meaningful, and 1.3 imbalance.
    The thin book is a $2,000 spot with the test reserves: a $130 flat round trip
    is about -13%, and a -15% net stop on that notional is -19.50. Skipping pays 0.
    """
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    holder = {"coin": _coin(symbol="WASH", mint="wash-mint-0001"), "flow": dict(_flow(16), round_trip_pct=55.0, last_window_round_trip_pct=10.0)}
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(holder["coin"])):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: holder["flow"]
        _run_watch(feed, holder["coin"], now)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("flow round trip")
        assert feed.ledger.fills == []

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="THIN", mint="thin-mint-0001", usd_market_cap=2_000)
        holder["flow"] = _flow(16)
        _run_watch(feed, holder["coin"], now)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("curve impact")
        assert feed.ledger.fills == []
        assert STRAT["size_min"] * -0.15 == -19.5


def test_depth_budget_cuts_a_boost_the_hard_drag_gate_still_allows(tmp_path: Path) -> None:
    """An $8,000 book clears the -10% skip at a boosted size and pays for the curve.

    The -4% budget then cuts that boost back to the $130 floor. A flat round trip
    at the uncut size loses more dollars than the same flat round trip at $130.
    A $30,000 book is inside the budget and is not resized by this rule.
    """
    from src.curve import flat_round_trip_pct, sol_price_usd

    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    coin = _coin(symbol="THINBOOST", mint="thin-boost-0001", usd_market_cap=8_000)
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.SIM, "decide", return_value={"decision": "boost", "score": 0.2, "knn_win": 4, "knn_n": 5, "n_hist": 30, "nw": 16, "nl": 14}), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(coin)):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: {"mention_volume": 500, "sentiment_0_100": 80}
        feed.flow_for = lambda _coin, _since, _last=None: _flow(30)
        _run_watch(feed, coin, now, curve=40)
        assert len(feed.pos) == 1
        position = next(iter(feed.pos.values()))
        assert position["usd"] == STRAT["size_min"]
        reason = feed.ledger.fills[-1][5]
        assert "depth size $130 from $" in reason
        uncut = float(reason.split("depth size $130 from $", 1)[1].split(",", 1)[0])
        assert uncut > STRAT["size_min"]
        virtual_sol, virtual_token = 40_000_000_000, 1_000_000_000_000_000
        sol_px = sol_price_usd(8_000, virtual_sol, virtual_token)
        assert sol_px is not None
        full = flat_round_trip_pct(8_000, uncut, sol_px, virtual_sol, virtual_token, STRAT["curve_fee_bps"])
        cut = flat_round_trip_pct(8_000, position["usd"], sol_px, virtual_sol, virtual_token, STRAT["curve_fee_bps"])
        assert full is not None and cut is not None
        assert STRAT["impact_max_drag_pct"] < full < STRAT["impact_size_budget_pct"]
        assert uncut * full / 100 < position["usd"] * cut / 100


def test_whale_larger_than_the_curve_is_counted_and_skips(tmp_path: Path):
    """Dropping the largest balance hides a 28% whale once the curve is only 20%.

    The proxy then sees top-5 24% and top-10 25%, inside the 25% and 30% caps.
    Excluding the known curve account leaves the whale in both, and the feed skips
    before a paper buy. A missing curve address also skips. Neither path pays the
    −15% net stop on $130, which is −19.50.
    """
    supply = 1000.0
    accounts = [("whale", 280.0), ("curve", 200.0)] + [(f"w{i}", 10.0) for i in range(5)]
    ranked = sorted((amount for _address, amount in accounts), reverse=True)
    proxy_top5 = 100.0 * sum(ranked[1:6]) / supply
    proxy_top10 = 100.0 * sum(ranked[1:11]) / supply
    assert proxy_top5 <= STRAT["p0_bundle_max"]
    assert proxy_top10 <= STRAT["p0_top10_max"]
    stats = concentration(accounts, supply, {"curve"})
    assert stats is not None
    top1, top5, top10 = stats
    assert top1 == 28.0
    assert top5 > STRAT["p0_bundle_max"]
    assert top10 > STRAT["p0_top10_max"]
    assert concentration(accounts, supply, set()) is None
    assert concentration(accounts, supply, {"missing"}) is None
    assert account_ui_amount({"uiAmount": None, "uiAmountString": "280"}) == 280
    assert account_ui_amount({"uiAmount": None}) is None
    assert largest_account_rows([{"address": "curve", "uiAmount": None, "uiAmountString": "200"}]) == [("curve", 200.0)]

    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    # 1e15 raw units at 6 decimals is 1e9 tokens. Whale 28%, curve 20%.
    coin = _coin(
        symbol="WHALE", mint="whale-hide-0001", total_supply=1_000_000_000_000_000, base_decimals=6,
        associated_bonding_curve="curve-token",
    )
    rows = [
        {"address": "whale", "uiAmount": 280_000_000},
        {"address": "curve-token", "uiAmount": 200_000_000},
        *[{"address": f"w{i}", "uiAmount": 10_000_000} for i in range(5)],
    ]
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.curve_state = lambda _coin: _state(40.0, 5_000_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed._rpc = lambda method, params, timeout=20: {"result": {"value": rows}} if method == "getTokenLargestAccounts" else {}
        feed.evaluate(coin)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("P0 top10")
        assert feed.ledger.fills == []

        hidden = _coin(symbol="NOCURVE", mint="nocurve-mint-0001", total_supply=1_000_000_000_000_000, base_decimals=6)
        feed.evaluate(hidden)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("P0 holder data missing")
        assert STRAT["size_min"] * -0.15 == -19.5


def _run_watch_curves(feed, coin, now, curves):
    feed.curve_state = lambda _coin: _state(curves.get(now[0], 40.0), SOLS.get(now[0], 5_000_000_000))
    feed.evaluate(coin)
    for stamp in (1090.0, 1180.0, 1300.0):
        now[0] = stamp
        feed.recheck()


def test_zero_replies_and_a_rising_curve_clear_p2_when_flow_is_unavailable(tmp_path: Path) -> None:
    """The dead-end was a missing tape skipping every coin, and replies are always 0.

    A curve that rises at least 3 points can clear P2 with zero or absent replies.
    A curve that does not rise still skips. A present tape that fails buyers still
    skips. The upper band, a rug dev, a curve outside 2–85%, and a non-accelerating
    reserve path still skip. This is a paper gate, not a claim the rise makes money.
    """
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    rising = {1000.0: 40.0, 1090.0: 42.0, 1180.0: 44.0, 1300.0: 46.0}
    flat = {1000.0: 40.0, 1090.0: 40.0, 1180.0: 40.0, 1300.0: 40.0}
    holder = {"coin": _coin(symbol="RISE", mint="rise-mint-0001", reply_count=0)}
    concentrated = {
        "signed_quote_sol": 6.0, "buy_sol": 10.0, "sell_sol": 4.0, "imbalance": 2.5,
        "unique_buyers": 16, "top_buyer_pct": 19.0, "top3_buyer_pct": 54.0,
        "meaningful_buyers": 16, "trades": 20,
        "last_window_buyers": 4, "last_window_top_pct": 20.0, "last_window_buy_sol": 0.8,
        "last_window_signed_sol": 0.8, "last_window_imbalance": 4.0,
        "round_trip_pct": 0.0, "last_window_round_trip_pct": 0.0,
    }
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_a, **_k: dict(holder["coin"])):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: None
        _run_watch_curves(feed, holder["coin"], now, rising)
        assert len(feed.pos) == 1
        position = next(iter(feed.pos.values()))
        assert position["usd"] == STRAT["size_min"]
        assert position["feat"]["replies"] == 0
        assert position["feat"]["rise_pts"] == 6.0
        assert position["feat"]["flow_buyers"] is None
        reason = feed.ledger.fills[-1][5]
        assert "flow n/a -> fallback (replies unavailable, curve +6.0pts)" in reason
        assert "flow last window n/a (tape missing)" in reason

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="NOREP", mint="norep-mint-0001", reply_count=0)
        holder["coin"].pop("reply_count")
        _run_watch_curves(feed, holder["coin"], now, rising)
        assert any(key.startswith("NOREP") for key in feed.pos)
        norep = next(item for key, item in feed.pos.items() if key.startswith("NOREP"))
        assert "replies unavailable" in feed.ledger.fills[-1][5]
        assert norep["feat"]["rise_pts"] == 6.0

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="FLAT", mint="flat-mint-0001", reply_count=0)
        before = len(feed.pos)
        _run_watch_curves(feed, holder["coin"], now, flat)
        assert len(feed.pos) == before
        assert feed.ledger.skips[-1][1] == "flow n/a -> fallback (replies unavailable, curve +0.0pts < 3)"

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="SHORT", mint="short-mint-0001", reply_count=0)
        barely = {1000.0: 40.0, 1090.0: 41.0, 1180.0: 42.0, 1300.0: 42.9}
        _run_watch_curves(feed, holder["coin"], now, barely)
        assert len(feed.pos) == before
        assert feed.ledger.skips[-1][1] == "flow n/a -> fallback (replies unavailable, curve +2.9pts < 3)"

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="UPPER", mint="upper-mint-0001", reply_count=0)
        high = {1000.0: 72.0, 1090.0: 74.0, 1180.0: 76.0, 1300.0: 78.0}
        _run_watch_curves(feed, holder["coin"], now, high)
        assert len(feed.pos) == before
        assert feed.ledger.skips[-1][1] == "flow unavailable: upper curve band needs buyer breadth"

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="LOWC", mint="lowc-mint-0001", reply_count=0)
        feed.curve_state = lambda _coin: _state(1.0)
        feed.evaluate(holder["coin"])
        assert "P1 curve" in feed.ledger.skips[-1][1]
        assert "lowc-mint-0001" not in feed.watch

        feed.ledger.do_not_rebuy.add("rug-dev")
        feed.curve_state = lambda _coin: _state(46.0)
        feed.evaluate(_coin(symbol="RUG", mint="rug-mint-0001", creator="rug-dev", reply_count=0))
        assert feed.ledger.skips[-1][1] == "same dev as rug -> skip forever"
        assert not any(key.startswith("RUG") for key in feed.pos)

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="TAPE", mint="tape-mint-0001", reply_count=0)
        feed.flow_for = lambda _coin, _since, _last=None: concentrated
        _run_watch_curves(feed, holder["coin"], now, rising)
        assert not any(key.startswith("TAPE") for key in feed.pos)
        assert feed.ledger.skips[-1][1].startswith("flow top3")

        now[0] = 1000.0
        feed.flow_for = lambda _coin, _since, _last=None: None
        holder["coin"] = _coin(symbol="STILL", mint="still-mint-0001", reply_count=0)
        feed.curve_state = lambda _coin: _state(rising.get(now[0], 40.0), 5_000_000_000)
        feed.evaluate(holder["coin"])
        for stamp in (1090.0, 1180.0, 1300.0):
            now[0] = stamp
            feed.recheck()
        assert not any(key.startswith("STILL") for key in feed.pos)
        assert "SOL/s" in feed.ledger.skips[-1][1]


def test_holder_rpc_classes_do_not_turn_a_rate_limit_into_a_zero():
    """429 and timeouts are transient. A bad body is corrupt. Neither is 0%."""
    limited = Exception("HTTP Error 429: Too Many Requests")
    limited.code = 429
    assert rpc_failure_kind(limited) == "transient"
    assert rpc_failure_kind(TimeoutError("timed out")) == "transient"
    denied = Exception("HTTP Error 400: Bad Request")
    denied.code = 400
    assert rpc_failure_kind(denied) == "corrupt"
    assert rpc_failure_kind(ValueError("uiAmount is invalid")) == "corrupt"
    assert rpc_error_kind({"code": 429, "message": "Too many requests"}) == "transient"
    assert rpc_error_kind({"code": -32429, "message": "max usage reached"}) == "transient"
    assert rpc_error_kind({"code": -32602, "message": "Invalid param"}) == "corrupt"
    assert rpc_payload_kind({"result": {"value": []}}) is None
    assert rpc_payload_kind({"error": {"code": 429, "message": "Too many requests"}}) == "transient"
    assert rpc_payload_kind("nope") == "corrupt"


def _open_feed(tmp_path: Path, now: list[float]):
    path = _feed(tmp_path, now)
    return path


def test_curve_band_admits_the_fresh_pool_mass_and_still_rejects_the_near_zero_tail(tmp_path: Path) -> None:
    """Of the sampled out-of-band curve prints, about 84% are at or above 2%.

    2% and 8% start a watch. 1% and 86% still skip on P1, and the near-zero skip
    does not spend a holder RPC.
    """
    path = _open_feed(tmp_path, [1000.0])
    now = [1000.0]
    calls = []

    def rpc(method, params, timeout=20):
        calls.append(method)
        raise AssertionError(method)

    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed._rpc = rpc
        feed.curve_state = lambda _coin: _state(1.0)
        feed.evaluate(_coin(symbol="FRESH", mint="fresh-mint-0001"))
        assert "P1 curve" in feed.ledger.skips[-1][1]
        assert "1.0% not in 2-85%" in feed.ledger.skips[-1][1]
        assert calls == []
        assert "fresh-mint-0001" not in feed.watch
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1

        feed.curve_state = lambda _coin: _state(86.0)
        feed.evaluate(_coin(symbol="GRAD", mint="grad-mint-0001"))
        assert "P1 curve" in feed.ledger.skips[-1][1]
        assert "grad-mint-0001" not in feed.watch

        feed.curve_state = lambda _coin: _state(2.0)
        feed.evaluate(_coin(symbol="EDGE2", mint="edge2-mint-00001"))
        assert "edge2-mint-00001" in feed.watch
        assert not feed.pos

        feed.curve_state = lambda _coin: _state(8.0)
        feed.evaluate(_coin(symbol="EDGE8", mint="edge8-mint-00001"))
        assert "edge8-mint-00001" in feed.watch
        assert not feed.pos

        feed.curve_state = lambda _coin: _state(85.0)
        feed.evaluate(_coin(symbol="EDGE85", mint="edge85-mint-0001"))
        assert "edge85-mint-0001" in feed.watch
        assert not feed.pos


def test_holder_rate_limit_sizes_down_and_a_later_over_cap_read_still_skips(tmp_path: Path) -> None:
    """In-band coins were skipped only as 'P0 holder data missing' while RPC 429'd.

    A 429 starts the watch and, if it is still a 429 at the decision, buys at
    the holder-gap size with the percents left unknown. A later real top-10 over 30% skips.
    An unreadable payload never becomes that half-size entry.
    """
    path = _open_feed(tmp_path, [1000.0])
    now = [1000.0]
    rising = {1000.0: 40.0, 1090.0: 42.0, 1180.0: 44.0, 1300.0: 46.0}
    early = {1000.0: 22.0, 1090.0: 24.0, 1180.0: 26.0, 1300.0: 28.0}
    upper = {1000.0: 72.0, 1090.0: 74.0, 1180.0: 76.0, 1300.0: 78.0}

    def rate_limited(method, params, timeout=20):
        if method == "getTokenLargestAccounts":
            error = Exception("HTTP Error 429: Too Many Requests")
            error.code = 429
            raise error
        raise AssertionError(method)

    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: None
        feed._rpc = lambda method, params, timeout=20: {"result": {"value": [{"uiAmount": 1}]}}
        bad = feed.holders(_coin(total_supply=1_000_000_000_000_000, base_decimals=6, associated_bonding_curve="curve-token"))
        assert bad["status"] == "corrupt"
        feed._rpc = lambda method, params, timeout=20: {"error": {"code": 429, "message": "Too many requests"}}
        limited = feed.holders(_coin())
        assert limited["status"] == "transient"
        assert "top10" not in limited
        feed._rpc = rate_limited
        coin = _coin(symbol="PAPER", mint="paper-mint-0001", reply_count=0)
        _run_watch_curves(feed, coin, now, rising)
        assert len(feed.pos) == 1
        position = next(iter(feed.pos.values()))
        assert position["usd"] == STRAT["size_min"] * STRAT["holder_gap_size_mult"]
        assert position["feat"]["top10_pct"] is None
        assert position["feat"]["dev_pct"] is None
        assert position["feat"]["bundle_pct"] is None
        reason = feed.ledger.fills[-1][5]
        assert "top10 unverified" in reason and "dev unverified" in reason
        assert "holders unverified (RPC unavailable), size x0.65" in reason
        assert "top10 0" not in reason and "top10 None" not in reason and "dev 0" not in reason
        chart = gates_buy(reason)
        assert next(gate["pass"] for gate in chart if gate["name"] == "Holders top10") is False
        assert next(gate["pass"] for gate in chart if gate["name"].endswith("checklist")) is False

        now[0] = 1000.0
        feed.social = lambda _symbol: None
        feed._rpc = rate_limited
        early_coin = _coin(symbol="EARLYG", mint="earlyg-mint-0001", reply_count=0)
        _run_watch_curves(feed, early_coin, now, early)
        early_pos = feed.pos["EARLYG·earl"]
        assert early_pos["usd"] == STRAT["size_min"] * STRAT["holder_gap_size_mult"] * STRAT["early_curve_size_mult"]
        early_reason = feed.ledger.fills[-1][5]
        assert "early curve 28.0% < 30, size x0.45" in early_reason
        assert "holders unverified (RPC unavailable), size x0.65" in early_reason

        now[0] = 1000.0
        before = len(feed.pos)
        upper_coin = _coin(symbol="BLUENAMI", mint="bluenami-mint-01", reply_count=0)
        _run_watch_curves(feed, upper_coin, now, upper)
        assert len(feed.pos) == before
        assert feed.ledger.skips[-1][1] == "flow unavailable: upper curve band needs buyer breadth"

        now[0] = 1000.0
        feed.holders = lambda _coin: {"status": "corrupt", "reason": "P0 holder data missing"}
        feed.curve_state = lambda _coin: _state(66.0)
        feed.evaluate(_coin(symbol="BADBODY", mint="badbody-mint-0001"))
        assert feed.ledger.skips[-1][1] == "P0 holder data missing"
        assert "badbody-mint-0001" not in feed.watch
        assert "BADBODY·badb" not in feed.pos

        now[0] = 1000.0
        seen = {"n": 0}

        def retry_holders(_coin):
            seen["n"] += 1
            if seen["n"] >= 3:
                return {"status": "known", "top": 40.0, "bundle": 20.0, "top10": 50.0, "supply": 1_000_000}
            return {"status": "transient", "reason": "holder RPC unavailable"}

        feed.holders = retry_holders
        feed.dev_pct = lambda _coin, _supply: 1
        veto = _coin(symbol="VETO", mint="veto-mint-0001", reply_count=0)
        feed.curve_state = lambda _coin: _state(rising.get(now[0], 40.0), SOLS.get(now[0], 5_000_000_000))
        feed.evaluate(veto)
        assert "veto-mint-0001" in feed.watch
        for stamp in (1090.0, 1180.0):
            now[0] = stamp
            feed.recheck()
        assert "veto-mint-0001" not in feed.watch
        assert feed.ledger.skips[-1][1].startswith("P0 top10")
        assert "VETO·veto" not in feed.pos
        assert feed.ledger.fills[-1][0] != "VETO·veto" if feed.ledger.fills else True


def test_known_holders_keep_their_caps_and_the_early_band_is_half_size(tmp_path: Path) -> None:
    """A real over-cap reading skips even if the dev read then 429s.

    A complete reading inside 15–30% can buy, at half the signal size. A missing
    creator is still fail-closed. Dev stays unknown on a timeout; it is not stored as 0.
    """
    path = _open_feed(tmp_path, [1000.0])
    now = [1000.0]
    rising = {1000.0: 40.0, 1090.0: 42.0, 1180.0: 44.0, 1300.0: 46.0}
    early = {1000.0: 22.0, 1090.0: 24.0, 1180.0: 26.0, 1300.0: 28.0}
    rows = [
        {"address": "curve-token", "uiAmount": 400_000_000},
        *[{"address": f"h{i}", "uiAmount": 10_000_000} for i in range(5)],
    ]

    def rpc(method, params, timeout=20):
        if method == "getTokenLargestAccounts":
            return {"result": {"value": rows}}
        if method == "getTokenAccountsByOwner":
            raise TimeoutError("timed out")
        raise AssertionError(method)

    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.social = lambda _symbol: None
        feed.flow_for = lambda _coin, _since, _last=None: None
        whale = _coin(
            symbol="WHALE2", mint="whale2-mint-0001", total_supply=1_000_000_000_000_000, base_decimals=6,
            associated_bonding_curve="curve-token", reply_count=0,
        )
        feed._rpc = rpc
        feed.holders = lambda _coin: {"status": "known", "top": 40.0, "bundle": 20.0, "top10": 50.0, "supply": 1_000_000}
        feed.curve_state = lambda _coin: _state(40.0)
        feed.evaluate(whale)
        assert feed.ledger.skips[-1][1].startswith("P0 top10")
        assert "whale2-mint-0001" not in feed.watch
        assert not feed.pos

        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.curve_state = lambda _coin: _state(40.0)
        no_creator = _coin(symbol="NOCRE", mint="nocre-mint-0001", reply_count=0)
        no_creator.pop("creator")
        feed.evaluate(no_creator)
        assert feed.ledger.skips[-1][1] == "P0 holder data missing"
        assert "nocre-mint-0001" not in feed.watch

        feed.holders = Feed.holders.__get__(feed, Feed)
        feed.dev_pct = Feed.dev_pct.__get__(feed, Feed)
        measured = _coin(
            symbol="MEASURED", mint="measured-mint-01", total_supply=1_000_000_000_000_000, base_decimals=6,
            associated_bonding_curve="curve-token", reply_count=0,
        )
        _run_watch_curves(feed, measured, now, rising)
        assert "MEASURED·meas" in feed.pos
        position = feed.pos["MEASURED·meas"]
        assert position["usd"] == STRAT["size_min"] * STRAT["holder_gap_size_mult"]
        assert position["feat"]["top10_pct"] == 5.0
        assert position["feat"]["dev_pct"] is None
        assert position["dev0"] is None
        measured_reason = feed.ledger.fills[-1][5]
        assert "top10 5.0%" in measured_reason
        assert "dev unverified" in measured_reason
        assert "dev 0" not in measured_reason

        now[0] = 1000.0
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        early_coin = _coin(symbol="EARLY", mint="early-mint-0001", reply_count=0)
        _run_watch_curves(feed, early_coin, now, early)
        assert feed.pos["EARLY·earl"]["usd"] == STRAT["size_min"] * STRAT["early_curve_size_mult"]
        assert "early curve 28.0% < 30, size x0.45" in feed.ledger.fills[-1][5]
        assert "top10 1%" in feed.ledger.fills[-1][5]

        now[0] = 1000.0
        full = _coin(symbol="FULL", mint="full-mint-0001", reply_count=0)
        _run_watch_curves(feed, full, now, rising)
        assert feed.pos["FULL·full"]["usd"] == STRAT["size_min"]
        assert "early curve" not in feed.ledger.fills[-1][5]
        assert "unverified" not in feed.ledger.fills[-1][5]


def _strong_social():
    return {"mention_volume": 400, "sentiment_0_100": 80, "trend": "up"}


def test_public_sentiment_tiers_and_fixture_mode_is_not_a_buy_signal(tmp_path: Path) -> None:
    """Strong is the elevated bar. Missing, thin, weak, and downtrend are not a pass.

    The live feed ignores LunarCrush fixture mode so a canned topic cannot qualify
    a borderline curve.
    """
    assert public_sentiment_tier(None, STRAT) == "missing"
    assert public_sentiment_tier({"mention_volume": 10, "sentiment_0_100": 90}, STRAT) == "missing"
    assert public_sentiment_tier({"mention_volume": 400, "sentiment_0_100": 40}, STRAT) == "veto"
    assert public_sentiment_tier({"mention_volume": 400, "sentiment_0_100": 60, "trend": "up"}, STRAT) == "neutral"
    assert public_sentiment_tier({"mention_volume": 120, "sentiment_0_100": 90, "trend": "up"}, STRAT) == "neutral"
    assert public_sentiment_tier({"mention_volume": 400, "sentiment_0_100": 80, "trend": "down"}, STRAT) == "neutral"
    assert public_sentiment_tier(_strong_social(), STRAT) == "strong"

    path = _feed(tmp_path, [1000.0])
    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)

        class _Fixture:
            mode = "fixture"

            def bundle(self, _symbol):
                raise AssertionError("fixture sentiment must not be read")

        feed.lc = _Fixture()
        assert feed.social("BONK") is None

        class _Live:
            mode = "live"

            def bundle(self, _symbol):
                return {
                    "topic": {"data": {
                        "types_count": {"tweet": 200},
                        "types_sentiment": {"tweet": 80},
                        "types_interactions": {"tweet": 5},
                        "interactions_24h": 5,
                        "trend": "up",
                    }},
                    "posts": {"data": []},
                    "news": {"data": []},
                }

        feed.lc = _Live()
        snap = feed.social("BONK")
        assert snap["mention_volume"] == 200
        assert snap["sentiment_0_100"] == 80
        assert public_sentiment_tier(snap, STRAT) == "strong"


def test_strong_sentiment_qualifies_fresh_pools_and_missing_sentiment_stays_small(tmp_path: Path) -> None:
    """A fresh pool is no longer rejected for being under 15%.

    Missing, thin, or downtrend sentiment still buys only at the early-curve size.
    A strong live reading clears that cut and can lift a clean 30–85% ticket to
    $160. It does not erase a measured top-10 cap or a weak-sentiment veto.
    """
    path = _feed(tmp_path, [1000.0])
    now = [1000.0]
    fresh = {1000.0: 8.0, 1090.0: 10.0, 1180.0: 12.0, 1300.0: 14.0}
    border = {1000.0: 12.0, 1090.0: 13.5, 1180.0: 15.0, 1300.0: 16.0}
    rising = {1000.0: 40.0, 1090.0: 42.0, 1180.0: 44.0, 1300.0: 46.0}
    inconclusive = {
        "decision": "inconclusive", "score": None, "knn_win": 0, "knn_n": 0, "n_hist": 0, "nw": 0, "nl": 0,
    }
    asked = []

    def social(symbol):
        asked.append(symbol)
        return _strong_social()

    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.SIM, "decide", return_value=inconclusive), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]):
        feed = Feed({"mode": "paper", "bankroll_usd": 10_000}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 1
        feed.flow_for = lambda _coin, _since, _last=None: None
        feed.social = social
        feed.curve_state = lambda _coin: _state(1.0)
        feed.evaluate(_coin(symbol="TOOLOW", mint="toolow-mint-0001"))
        assert asked == []
        assert "P1 curve" in feed.ledger.skips[-1][1]
        assert "1.0% not in 2-85%" in feed.ledger.skips[-1][1]
        assert "toolow-mint-0001" not in feed.watch

        feed.social = lambda _symbol: None
        quiet = _coin(symbol="QUIET", mint="quiet-mint-0001", reply_count=0)
        _run_watch_curves(feed, quiet, now, fresh)
        assert "QUIET·quie" in feed.pos
        assert feed.pos["QUIET·quie"]["usd"] == STRAT["size_min"] * STRAT["early_curve_size_mult"]
        quiet_reason = feed.ledger.fills[-1][5]
        assert "early curve 14.0% < 30, size x0.45" in quiet_reason
        assert "lifted by social" not in quiet_reason
        assert "not a buy signal" in quiet_reason

        for reading in (
            {"mention_volume": 400, "sentiment_0_100": 60, "trend": "up"},
            {"mention_volume": 120, "sentiment_0_100": 90, "trend": "up"},
            {"mention_volume": 400, "sentiment_0_100": 80, "trend": "down"},
        ):
            feed.social = lambda _symbol, reading=reading: reading
            mint = f"nohelp-{reading['sentiment_0_100']}-{reading['mention_volume']}"
            feed.curve_state = lambda _coin: _state(8.0)
            before_skips = len(feed.ledger.skips)
            feed.evaluate(_coin(symbol="NOHELP", mint=mint))
            assert mint in feed.watch
            assert len(feed.ledger.skips) == before_skips
        feed.watch.clear()

        now[0] = 1000.0
        feed.social = social
        admitted = _coin(symbol="LOUD", mint="loud-mint-0001", reply_count=0)
        _run_watch_curves(feed, admitted, now, border)
        assert "LOUD·loud" in feed.pos
        assert feed.pos["LOUD·loud"]["usd"] == STRAT["size_min"] * STRAT["sentiment_border_size_mult"]
        reason = feed.ledger.fills[-1][5]
        assert "social buy 80/100, 400 mentions" in reason
        assert "early curve 16.0% < 30, size x1.00, lifted by social 80/100" in reason
        assert feed.pos["LOUD·loud"]["feat"]["sentiment_buy"] is True
        assert feed.pos["LOUD·loud"]["usd"] > feed.pos["QUIET·quie"]["usd"]

        now[0] = 1000.0
        feed.social = lambda _symbol: {"mention_volume": 400, "sentiment_0_100": 100, "trend": "up"}
        weighted = _coin(symbol="WEIGHT", mint="weight-mint-0001", reply_count=0)
        _run_watch_curves(feed, weighted, now, rising)
        assert feed.pos["WEIGHT·weig"]["usd"] == STRAT["size_max"]
        assert "sentiment weight +$30" in feed.ledger.fills[-1][5]

        now[0] = 1000.0
        before = len(feed.pos)
        feed.social = lambda _symbol: {"mention_volume": 400, "sentiment_0_100": 40, "trend": "down"}
        veto = _coin(symbol="FUD", mint="fud-mint-0001", reply_count=0)
        _run_watch_curves(feed, veto, now, rising)
        assert len(feed.pos) == before
        assert feed.ledger.skips[-1][1] == "P3 sentiment 40/100"

        now[0] = 1000.0
        asked.clear()
        feed.social = social
        feed.holders = lambda _coin: {"status": "known", "top": 40.0, "bundle": 20.0, "top10": 50.0, "supply": 1_000_000}
        feed.curve_state = lambda _coin: _state(40.0)
        feed.evaluate(_coin(symbol="CAPPED", mint="capped-mint-0001"))
        assert feed.ledger.skips[-1][1].startswith("P0 top10")
        assert asked == []
        assert "capped-mint-0001" not in feed.watch

        def rate_limited(method, params, timeout=20):
            if method == "getTokenLargestAccounts":
                error = Exception("HTTP Error 429: Too Many Requests")
                error.code = 429
                raise error
            raise AssertionError(method)

        now[0] = 1000.0
        feed.holders = Feed.holders.__get__(feed, Feed)
        feed.dev_pct = Feed.dev_pct.__get__(feed, Feed)
        feed._rpc = rate_limited
        feed.social = lambda _symbol: _strong_social()
        gap = _coin(symbol="GAPUP", mint="gapup-mint-0001", reply_count=0)
        _run_watch_curves(feed, gap, now, rising)
        assert feed.pos["GAPUP·gapu"]["usd"] == STRAT["size_min"]
        gap_reason = feed.ledger.fills[-1][5]
        assert "holders unverified (RPC unavailable), size x1.00, lifted by social 80/100" in gap_reason
        assert "top10 unverified" in gap_reason
        assert feed.pos["GAPUP·gapu"]["feat"]["top10_pct"] is None
        assert "size x0.65" not in gap_reason
