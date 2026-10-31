import struct

import base64
import struct

from src.curve import (
    INITIAL_REAL_TOKEN_RESERVES,
    buy_fill,
    curve_progress_from_account_data,
    curve_progress_pct,
    flat_round_trip_pct,
    parse_curve_account,
    sell_eff,
    size_for_curve_depth,
    sol_price_usd,
)


def test_progress_uses_real_token_reserves_not_mcap():
    # Observed reserves from a live curve account: token-side delta matches the classic constant.
    real = 790_991_287_913_182
    progress = curve_progress_pct(real)
    sold = INITIAL_REAL_TOKEN_RESERVES - real
    assert abs(progress - 100 * sold / INITIAL_REAL_TOKEN_RESERVES) < 1e-9
    assert progress < 1  # a ~$2.6k coin is not ~4% of a $69k proxy


def test_account_bytes_do_not_use_the_usd_proxy():
    # ~$25k would be ~37% of a $69k proxy and inside the old band. Reserves say ~91%.
    real_token = int(INITIAL_REAL_TOKEN_RESERVES * 0.085)
    blob = b"\x00" * 8 + struct.pack("<QQQQ", 1_073_000_000_000_000, 30_000_000_000, real_token, 1)
    progress = curve_progress_from_account_data(base64.b64encode(blob).decode())
    assert progress is not None and progress > 90
    assert curve_progress_from_account_data(None) is None
    assert curve_progress_from_account_data("not-base64!!!") is None


def test_parse_curve_account_reads_reserve_words():
    blob = b"\x00" * 8 + struct.pack("<QQQQ", 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 0)
    parsed = parse_curve_account(blob)
    assert parsed["virtual_sol"] == 30_000_000_000
    assert parsed["curve_pct"] == 0
    assert parse_curve_account(b"\x00" * 10) is None


def test_buy_and_sell_are_worse_than_spot_and_flat_round_trip_loses():
    spot, usd, sol_px = 20_000.0, 150.0, 100.0
    virtual_sol, virtual_token = 30_000_000_000, 1_073_000_000_000_000
    filled = buy_fill(spot, usd, sol_px, virtual_sol, virtual_token, 100)
    fee_only = spot / 0.99
    assert filled["impact"] is True
    assert filled["tokens"] > 0
    assert filled["entry_eff"] > fee_only  # impact on top of the 100 bps fee
    exited = sell_eff(spot, filled["tokens"], virtual_sol, virtual_token, 100)
    assert exited < spot * 0.99
    assert exited / filled["entry_eff"] - 1 < 0


def test_flat_round_trip_is_none_until_the_curve_can_be_priced():
    assert flat_round_trip_pct(10_000, 130, 0, None, None, 100) is None
    assert sol_price_usd(0, 40_000_000_000, 10**15) is None
    price = sol_price_usd(2_000, 40_000_000_000, 10**15)
    assert price is not None and price == 50
    drag = flat_round_trip_pct(2_000, 130, price, 40_000_000_000, 10**15, 100)
    deep = flat_round_trip_pct(30_000, 130, sol_price_usd(30_000, 40_000_000_000, 10**15), 40_000_000_000, 10**15, 100)
    assert drag is not None and deep is not None
    assert drag < -10 < deep < 0


def test_size_for_curve_depth_cuts_only_the_expensive_dollars():
    virtual_sol, virtual_token = 40_000_000_000, 10**15
    # $8k: even $130 is past a -4% budget, so the cut stops at the floor.
    thin_px = sol_price_usd(8_000, virtual_sol, virtual_token)
    thin = size_for_curve_depth(240, 8_000, thin_px, virtual_sol, virtual_token, 100, budget_pct=-4, floor_usd=130)
    assert thin == 130
    # $15k: $160 misses -4% and $130 clears it, so the ticket shrinks inside the band.
    mid_px = sol_price_usd(15_000, virtual_sol, virtual_token)
    mid = size_for_curve_depth(160, 15_000, mid_px, virtual_sol, virtual_token, 100, budget_pct=-4, floor_usd=130)
    assert 130 < mid < 160
    assert flat_round_trip_pct(15_000, mid, mid_px, virtual_sol, virtual_token, 100) >= -4
    assert flat_round_trip_pct(15_000, mid + 1, mid_px, virtual_sol, virtual_token, 100) < -4
    # $60k and an unpriced book keep the requested size. The cut never raises it.
    deep_px = sol_price_usd(60_000, virtual_sol, virtual_token)
    assert size_for_curve_depth(160, 60_000, deep_px, virtual_sol, virtual_token, 100, budget_pct=-4, floor_usd=130) == 160
    assert size_for_curve_depth(160, 8_000, 0, None, None, 100, budget_pct=-4, floor_usd=130) == 160
    assert size_for_curve_depth(160, 8_000, thin_px, virtual_sol, virtual_token, 100, budget_pct=-100, floor_usd=130) == 160
    full = 240 * (flat_round_trip_pct(8_000, 240, thin_px, virtual_sol, virtual_token, 100) or 0) / 100
    cut = 130 * (flat_round_trip_pct(8_000, 130, thin_px, virtual_sol, virtual_token, 100) or 0) / 100
    assert cut > full  # less negative. A flat spot loses fewer dollars at the floor.


def test_missing_reserves_still_charge_the_fee():
    filled = buy_fill(10_000, 130, 0, None, None, 100)
    assert filled["impact"] is False
    assert filled["tokens"] is None
    assert abs(filled["entry_eff"] - 10_000 / 0.99) < 1e-6
    assert abs(sell_eff(10_000, None, None, None, 100) - 9_900) < 1e-6
