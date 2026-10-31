from src.exits import emergency_drop, failed_impulse_hit, profit_lock_hit, vol_stop_hit, vol_stop_pct
from src.live_feed import STRAT


def test_emergency_drop_is_one_check_and_ten_percent():
    assert emergency_drop(100, 90, 10)
    assert emergency_drop(100, 89, 10)
    assert not emergency_drop(100, 91, 10)
    assert not emergency_drop(None, 50, 10)
    assert not emergency_drop(0, 50, 10)


def test_profit_lock_arms_only_after_a_real_gain_and_stays_non_negative():
    assert not profit_lock_hit(7.9, 1.0, 8, 6)
    assert not profit_lock_hit(8.0, 8.0, 8, 6)
    assert profit_lock_hit(8.0, 2.0, 8, 6)
    assert profit_lock_hit(14.0, 8.0, 8, 6)
    assert not profit_lock_hit(14.0, 8.1, 8, 6)
    # A wide giveback cannot place the line itself below zero.
    assert profit_lock_hit(9.0, 0.0, 8, 20)
    assert not profit_lock_hit(9.0, 0.1, 8, 20)


def test_vol_stop_uses_the_spot_move_not_the_fee_net():
    """A −4% band is a spot move. Fee-inclusive net at an unchanged spot is not a stop.

    The hard stop remains the net check, so a band that is not tighter than −15%
    does not fire here.
    """
    hard = STRAT["stop_loss_pct"]
    quiet = -STRAT["vol_stop_floor_pct"]
    assert not vol_stop_hit(100, 100, quiet, hard)
    assert not vol_stop_hit(100, 97, quiet, hard)
    assert vol_stop_hit(100, 96, quiet, hard)
    assert vol_stop_hit(100, 93, quiet, hard)
    assert not vol_stop_hit(100, 85, hard, hard)
    assert not vol_stop_hit(0, 90, quiet, hard)
    assert not vol_stop_hit(None, 90, quiet, hard)


def test_failed_impulse_cuts_a_never_green_spot_and_leaves_a_bounce():
    """−8% spot with no +2% print is the exit. A bounce, a −7% print, and a flat spot are not."""
    assert failed_impulse_hit(100, 92, 100, 8, 2)
    assert failed_impulse_hit(100, 92, 101.5, 8, 2)
    assert not failed_impulse_hit(100, 92, 102, 8, 2)
    assert not failed_impulse_hit(100, 92, 103, 8, 2)
    assert not failed_impulse_hit(100, 93, 100, 8, 2)
    assert not failed_impulse_hit(100, 100, 100, 8, 2)
    assert not failed_impulse_hit(0, 92, 100, 8, 2)
    assert not failed_impulse_hit(100, 92, 100, 100, 2)


def test_vol_stop_tightens_but_never_widens_past_the_hard_stop():
    assert vol_stop_pct([0.01, 0.01], STRAT) == STRAT["stop_loss_pct"]
    quiet = vol_stop_pct([0.001] * 6, STRAT)
    assert quiet == -STRAT["vol_stop_floor_pct"]
    wild = vol_stop_pct([0.2, -0.2, 0.25, -0.25, 0.2, -0.2], STRAT)
    assert wild == STRAT["stop_loss_pct"]
    mid = vol_stop_pct([0.03, -0.03, 0.03, -0.03, 0.03, -0.03], STRAT)
    assert STRAT["stop_loss_pct"] < mid < -STRAT["vol_stop_floor_pct"]
