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
        feed.flow_for = lambda _coin, _since: None
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
        feed.flow_for = lambda _coin, _since: _flow(16)
        _run_watch(feed, coin, now, curve=80)
        assert not feed.pos
        assert "buyers 16 < 25" in feed.ledger.skips[-1][1]

        now[0] = 1000.0
        rich = _coin(symbol="BOOST", mint="boost-mint-001")
        feed.social = lambda _symbol: {"mention_volume": 500, "sentiment_0_100": 80}
        feed.flow_for = lambda _coin, _since: _flow(30)
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

        plant("BE")
        feed.apply_mark("BE", {"spot": 120})
        assert feed.pos["BE"]["be_armed"] is True
        # Steps stay inside the 10% emergency gap until the fee-inclusive mark is back through entry.
        for spot in (114, 108, 102):
            feed.apply_mark("BE", {"spot": spot})
        assert "BE" not in feed.pos
        assert feed.ledger.fills[-1][5].startswith("break-even")

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
        feed.flow_for = lambda _coin, _since: concentrated
        _run_watch(feed, holder["coin"], now)
        assert not feed.pos
        assert feed.ledger.skips[-1][1].startswith("flow top3")

        now[0] = 1000.0
        holder["coin"] = _coin(symbol="DUST", mint="dust-mint-0001")
        feed.flow_for = lambda _coin, _since: dust
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
        feed.flow_for = lambda _coin, _since: _flow(16)
        feed.curve_state = lambda _coin: _state(40.0, sols.get(now[0], 5_000_000_000))
        feed.evaluate(coin)
        for stamp in (1030.0, 1180.0, 2065.0):
            now[0] = stamp
            feed.recheck()
        assert not feed.pos
        assert "SOL/s" in feed.ledger.skips[-1][1]
        assert feed.ledger.fills == []
