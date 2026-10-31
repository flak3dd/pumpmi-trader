from src.exits import emergency_drop, vol_stop_pct
from src.live_feed import STRAT


def test_emergency_drop_is_one_check_and_ten_percent():
    assert emergency_drop(100, 90, 10)
    assert emergency_drop(100, 89, 10)
    assert not emergency_drop(100, 91, 10)
    assert not emergency_drop(None, 50, 10)
    assert not emergency_drop(0, 50, 10)


def test_vol_stop_tightens_but_never_widens_past_the_hard_stop():
    assert vol_stop_pct([0.01, 0.01], STRAT) == STRAT["stop_loss_pct"]
    quiet = vol_stop_pct([0.001] * 6, STRAT)
    assert quiet == -STRAT["vol_stop_floor_pct"]
    wild = vol_stop_pct([0.2, -0.2, 0.25, -0.25, 0.2, -0.2], STRAT)
    assert wild == STRAT["stop_loss_pct"]
    mid = vol_stop_pct([0.03, -0.03, 0.03, -0.03, 0.03, -0.03], STRAT)
    assert STRAT["stop_loss_pct"] < mid < -STRAT["vol_stop_floor_pct"]
