"""Listing parser for the paper feed. No network and no orders."""
import struct
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import src.live_feed as live_feed
from src.live_feed import Feed
from src.market_scan import (
    PUMP_PROGRAM_ID,
    apply_account_meta,
    coins_from_listing,
    creator_pubkey,
    listing_url,
    mcap_from_reserves,
)
from src.solana_pda import TOKEN_2022_PROGRAM_ID


def _curve_bytes(creator: bytes, *, complete: int = 0, supply: int = 1_000_000_000_000_000, real_token: int = 400_000_000_000_000) -> bytes:
    virtual_token = real_token + 279_900_000_000_000
    words = struct.pack("<QQQQQ", virtual_token, 30_000_000_000, real_token, 1_000_000_000, supply)
    return b"\x11" * 8 + words + bytes([complete]) + creator


def test_listing_url_is_the_public_pump_fun_pool_page():
    url = listing_url(2)
    assert url == (
        "https://api.geckoterminal.com/api/v2/networks/solana/dexes/pump-fun/pools"
        "?include=base_token&page=2"
    )
    assert "frontend-api" not in url
    assert listing_url(0).endswith("page=1")


def test_coins_from_listing_reads_fdv_mint_and_sol_quote():
    payload = {
        "data": [
            {
                "attributes": {
                    "address": "curve-1",
                    "name": "PAPER / SOL",
                    "fdv_usd": "9426.5",
                    "market_cap_usd": None,
                    "quote_token_price_usd": "95.5",
                    "pool_created_at": "2026-11-03T18:00:14Z",
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_mint-1", "type": "token"}},
                    "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112", "type": "token"}},
                    "dex": {"data": {"id": "pump-fun", "type": "dex"}},
                },
            },
            {
                "attributes": {"address": "other-dex", "name": "NOPE / SOL", "fdv_usd": 10},
                "relationships": {"dex": {"data": {"id": "orca", "type": "dex"}}, "base_token": {"data": {"id": "solana_mint-2"}}},
            },
        ],
        "included": [
            {"id": "solana_mint-1", "type": "token", "attributes": {"address": "mint-1", "name": "Paper", "symbol": "PAPER", "decimals": 6}},
        ],
    }
    coins, sol_usd = coins_from_listing(payload)
    assert sol_usd == 95.5
    assert len(coins) == 1
    coin = coins[0]
    assert coin["mint"] == "mint-1"
    assert coin["symbol"] == "PAPER"
    assert coin["name"] == "Paper"
    assert coin["bonding_curve"] == "curve-1"
    assert coin["usd_market_cap"] == 9426.5
    assert abs(coin["market_cap_quote"] - 9426.5 / 95.5) < 1e-9
    assert coin["created_timestamp"] == 1793728814000
    assert coin["base_decimals"] == 6
    assert coin["complete"] is False
    assert coin["reply_count"] is None


def test_account_meta_keeps_creator_supply_and_curve_token_account():
    creator = bytes.fromhex("0a3e3c84ab2c916412733533e3086d26c4edba7e16ca10f708fe99be551231b8")
    assert creator_pubkey(_curve_bytes(creator)) == "gz7usTxGEYcbnmquaxKvVERmqRAxJcQocvnrEFZ4dpB"
    coin = apply_account_meta(
        {"mint": "3HZ97ZA5QfdUfc3GkL261pYJLNJpsHG5EfQ11GFtDoP3", "bonding_curve": "3C17g4yfbeat6aUbcjBu8U9g7oPfRvMvUC4KPNfRxDCf", "symbol": "PAPER"},
        {
            "3C17g4yfbeat6aUbcjBu8U9g7oPfRvMvUC4KPNfRxDCf": {"owner": PUMP_PROGRAM_ID, "data": _curve_bytes(creator, complete=0)},
            "3HZ97ZA5QfdUfc3GkL261pYJLNJpsHG5EfQ11GFtDoP3": {"owner": TOKEN_2022_PROGRAM_ID, "data": b""},
        },
    )
    assert coin["creator"] == "gz7usTxGEYcbnmquaxKvVERmqRAxJcQocvnrEFZ4dpB"
    assert coin["total_supply"] == 1_000_000_000_000_000
    assert coin["complete"] is False
    assert coin["associated_bonding_curve"] == "B2cQZz4pbHjNTDSZNCBVVFVC5bNEp5duLtHjKyZoTTpF"
    assert apply_account_meta({"mint": "m", "bonding_curve": "c"}, {"c": {"owner": "not-pump", "data": _curve_bytes(creator)}}) is None
    assert creator_pubkey(_curve_bytes(b"\x00" * 32)) is None


def test_mcap_from_reserves_needs_a_real_sol_quote():
    assert mcap_from_reserves(None, 30_000_000_000, 10**15, 10**15) is None
    usd, sol = mcap_from_reserves(100, 30_000_000_000, 10**15, 10**15)
    assert sol == 30
    assert usd == 3000


def test_scan_uses_one_geckoterminal_page_and_backs_off_on_429(tmp_path: Path):
    path = tmp_path / "strategy.json"
    path.write_text(
        '{"version": 1, "revision": 0, "max_open": 6, '
        '"watchPeriods": {"short": 90, "medium": 180, "long": 300}}',
        encoding="utf-8",
    )
    urls = []

    def fake_http(url, body=None, timeout=12, headers=None):
        urls.append(url)
        if len(urls) == 1:
            return {"data": [], "included": []}
        raise HTTPError(url, 429, "Too Many Requests", hdrs={}, fp=None)

    with patch.object(live_feed, "Ledger"), \
         patch.object(live_feed, "LunarCrushClient", lambda *a, **k: type("L", (), {"mode": "off"})()), \
         patch.object(Feed, "restore", lambda self: None), \
         patch.object(live_feed, "http", side_effect=fake_http):
        feed = Feed({"mode": "paper", "bankroll_usd": 1500}, strategy_config_path=path)
        feed.ev = lambda _message: None
        feed.write = lambda: None
        feed.manage_fast = lambda: None
        assert feed.cycle(now=1_000) is True
        assert urls == [listing_url(1)]
        assert feed._listing_page == 2
        assert feed.cycle(now=1_020) is False
        assert urls == [listing_url(1), listing_url(2)]
        assert feed.next_scan == 1_080
        text = Path(live_feed.__file__).read_text(encoding="utf-8")
        assert "PUMP_ENGINE_ENABLE_LIVE" not in text
        assert "sendTransaction" not in text
