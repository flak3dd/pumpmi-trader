import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import src.live_feed as live_feed
from src.engine import Engine
from src.live_feed import Feed, STRAT
from src.rules import CoinMetrics
from src.strategy_config import (
    StrategyConfigError,
    StrategyRevisionConflict,
    default_document,
    effective_strategy,
    load_strategy_config,
    update_strategy_config,
)


def _seed_strategy(path: Path) -> None:
    path.write_text(json.dumps(default_document()), encoding="utf-8")


def _digest_tree(path: Path) -> dict[Path, str]:
    return {
        item: hashlib.sha256(item.read_bytes()).hexdigest()
        for item in path.rglob("*")
        if item.is_file()
    }


def test_strategy_config_roundtrip_revisions_and_failed_save_preserves_file(tmp_path: Path) -> None:
    path = tmp_path / "strategy.json"
    _seed_strategy(path)
    assert load_strategy_config(path) == default_document()

    saved = update_strategy_config(
        {"watchPeriods": {"short": 120, "medium": 240, "long": 360}, "revision": 0}, path
    )
    assert saved == {
        "version": 1,
        "revision": 1,
        "watchPeriods": {"short": 120, "medium": 240, "long": 360},
        "max_open": 6,
    }
    assert effective_strategy(STRAT, path)["watch_long_s"] == 360

    before_invalid = path.read_bytes()
    with pytest.raises(StrategyConfigError):
        update_strategy_config(
            {"watchPeriods": {"short": 125, "medium": 240, "long": 360}, "revision": 1}, path
        )
    assert path.read_bytes() == before_invalid

    with pytest.raises(StrategyRevisionConflict):
        update_strategy_config(
            {"watchPeriods": {"short": 135, "medium": 255, "long": 375}, "revision": 0}, path
        )
    assert path.read_bytes() == before_invalid
    assert not list(tmp_path.glob(".strategy.json.*.tmp"))


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
        self.do_not_rebuy.add(dev)


class _NoNetworkLunar:
    mode = "test"


def test_feed_records_actual_three_tier_observations_before_buy_and_keeps_initial_timeline(tmp_path: Path) -> None:
    path = tmp_path / "strategy.json"
    _seed_strategy(path)
    production_ledger = Path(live_feed.__file__).resolve().parents[1] / "out" / "ledger_live"
    before_ledger = _digest_tree(production_ledger)
    now = [1000.0]
    coin = {
        "symbol": "WATCH",
        "mint": "watch-mint-0001",
        "name": "Watch token",
        "creator": "creator",
        "usd_market_cap": 34_500,
        "reply_count": 3,
    }

    with patch.object(live_feed, "Ledger", _MemoryLedger), \
         patch.object(live_feed, "LunarCrushClient", _NoNetworkLunar), \
         patch.object(Feed, "restore", lambda self: setattr(self, "restored", [])), \
         patch.object(live_feed.SIM, "append"), \
         patch.object(live_feed.time, "time", side_effect=lambda: now[0]), \
         patch.object(live_feed, "http", side_effect=lambda *_args, **_kwargs: dict(coin)):
        feed: Any = Feed({"mode": "paper", "bankroll_usd": 1_500}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.holders = lambda _coin: (0, 0, 0, 1_000_000)
        feed.dev_pct = lambda _coin, _supply: 0
        feed.social = lambda _symbol: None
        # Reserve progress stays inside the 30–70 band. SOL rises at each checkpoint
        # so the acceleration gate can pass and this test still observes the buy.
        sols = {1000.0: 5_000_000_000, 1090.0: 5_300_000_000, 1180.0: 5_700_000_000, 1300.0: 6_200_000_000}

        def curve_state(_coin):
            return {
                "curve_pct": 40.0,
                "virtual_sol": 40_000_000_000,
                "virtual_token": 1_000_000_000_000_000,
                "real_token": 500_000_000_000_000,
                "real_sol": sols.get(now[0], 5_000_000_000),
            }

        feed.curve_state = curve_state
        feed.flow_for = lambda _coin, _since: {
            "signed_quote_sol": 1.2, "buy_sol": 2.0, "sell_sol": 0.8, "imbalance": 2.5,
            "unique_buyers": 16, "top_buyer_pct": 10.0, "top3_buyer_pct": 22.0,
            "meaningful_buyers": 12, "trades": 20,
        }

        feed.evaluate(coin)
        watch = feed.watch[coin["mint"]]
        assert feed.board.cards[watch["key"]]["ts_seen"] == 1000.0
        assert feed.board.cards[watch["key"]]["watch_started_ts"] == 1000.0
        assert [watch["watch_periods"][tier]["duration_s"] for tier in ("short", "medium", "long")] == [90, 180, 300]

        # A new slider save changes future watches only; this one remains on 90/180/300.
        update_strategy_config(
            {"watchPeriods": {"short": 120, "medium": 240, "long": 360}, "revision": 0}, path
        )
        feed.refresh_strategy()
        assert feed.strategy["watch_long_s"] == 360
        assert watch["watch_periods"]["long"]["duration_s"] == 300

        now[0] = 1090.0
        feed.recheck()
        assert watch["watch_periods"]["short"] == {
            "duration_s": 90,
            "elapsed_s": 90.0,
            "status": "observed",
            "curve_delta": 0.0,
            "real_sol": 5_300_000_000,
        }
        assert watch["watch_periods"]["medium"]["status"] == "watching"
        assert not feed.pos and not feed.ledger.fills

        now[0] = 1180.0
        feed.recheck()
        assert watch["watch_periods"]["medium"]["status"] == "observed"
        assert watch["watch_periods"]["medium"]["elapsed_s"] == 180.0
        assert watch["watch_periods"]["long"]["status"] == "watching"
        assert not feed.pos and not feed.ledger.fills

        now[0] = 1300.0
        feed.recheck()
        assert watch["watch_periods"]["long"]["status"] == "observed"
        assert watch["watch_periods"]["long"]["elapsed_s"] == 300.0
        assert len(feed.pos) == 1
        assert len(feed.ledger.fills) == 1
        position = next(iter(feed.pos.values()))
        assert position["entry_eff"] > position["entry_spot"]
        assert live_feed.STRAT["size_min"] <= position["usd"] <= live_feed.STRAT["size_max"]

    assert _digest_tree(production_ledger) == before_ledger


class _CapacityHolders:
    def metrics(self, mint: str) -> CoinMetrics:
        return CoinMetrics(mint=mint, symbol=mint, dev_wallet=f"dev-{mint}", dev_pct=0, bundle_pct=0)

    def price_path(self, _mint: str) -> list[float]:
        return [1.0, 1.1]


class _CapacitySocial:
    def fetch(self, _symbol: str, _mint: str) -> dict:
        return {
            "posts": [],
            "counts": [],
            "posts_15m": 100,
            "snapshot": {
                "sentiment_-1_1": 0.8,
                "sentiment_0_100": 80,
                "mention_volume": 100,
                "interactions": 0,
                "trend": "up",
                "reasons": ["fixture pass"],
                "by_platform": {},
                "headlines": [],
            },
        }


def test_fixture_engine_enforces_six_positions_from_strat(tmp_path: Path) -> None:
    assert STRAT["max_open"] == 6
    cfg = {
        "mode": "paper",
        "bankroll_usd": 10_000,
        "position_usd": 100,
        "goal_multiple": 99,
        "stop_loss_pct": -30,
        "max_open": STRAT["max_open"],
        "rules": {"max_dev_pct": 10, "max_bundle_pct": 25, "min_posts_15m": 1, "min_sentiment": 0.1},
        "social": {"min_mentions": 0, "min_sentiment": 50},
        "paths": {
            "ledger_dir": str(tmp_path / "ledger"),
            "board_json": str(tmp_path / "board.json"),
            "known_rugs": str(tmp_path / "known_rugs.json"),
        },
    }
    engine = Engine(cfg, _CapacityHolders(), _CapacitySocial())
    verdicts = [engine.evaluate({"symbol": f"C{i}", "mint": f"mint-{i}"})[0] for i in range(7)]

    assert verdicts[:6] == ["BUY"] * 6
    assert verdicts[6] == "SKIP"
    assert len(engine.positions) == STRAT["max_open"]
    assert engine.board.cards["C6"]["reason"] == "max 6 open"
