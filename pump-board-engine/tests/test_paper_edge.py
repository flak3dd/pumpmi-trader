"""Paper-feed behavior for the v8 expected-edge rules. No network and no orders."""
import json
from pathlib import Path
from unittest.mock import patch

import src.live_feed as live_feed
from src.live_feed import STRAT, Feed


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
    assert STRAT["min_curve_pct"] == 30
    assert STRAT["curve_fee_bps"] == 100
    assert STRAT["flow_max_top3_pct"] == 45
    assert STRAT["flow_meaningful_sol"] == 0.05
    assert STRAT["flow_min_meaningful_buyers"] == 8
    assert STRAT["flow_strong_meaningful_buyers"] == 12
    assert STRAT["flow_last_window_min_buyers"] == 3
    assert STRAT["flow_last_window_max_whale_pct"] == 50
    assert STRAT["flow_last_window_min_bs_ratio"] == 1.3
    assert STRAT["flow_max_round_trip_pct"] == 50
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
        assert feed.ledger.skips[-1][1] == "flow unavailable"

        now[0] = 1000.0
        feed.holders = lambda _coin: (None, None, None, None)
        feed.curve_state = lambda _coin: _state()
        feed.evaluate(_coin(mint="edge-mint-0002", symbol="HOLD"))
        assert feed.ledger.skips[-1][1] == "P0 holder data missing"
        assert "edge-mint-0002" not in feed.watch

        feed.holders = lambda _coin: (1, 1, 1, 1_000_000)
        feed.curve_state = lambda _coin: _state(curve=22)
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
