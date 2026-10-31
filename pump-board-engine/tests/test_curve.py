import struct

import base64
import struct

from src.curve import (
    INITIAL_REAL_TOKEN_RESERVES,
    buy_fill,
    curve_progress_from_account_data,
    curve_progress_pct,
    parse_curve_account,
    sell_eff,
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


def test_missing_reserves_still_charge_the_fee():
    filled = buy_fill(10_000, 130, 0, None, None, 100)
    assert filled["impact"] is False
    assert filled["tokens"] is None
    assert abs(filled["entry_eff"] - 10_000 / 0.99) < 1e-6
    assert abs(sell_eff(10_000, None, None, None, 100) - 9_900) < 1e-6
