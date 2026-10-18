"""Sun pre-cool hold: held until the sun peaks, released by the safeguards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.loader import load

sun_hold = load("sun_hold")
SunHoldState = sun_hold.SunHoldState

T0 = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)


def step(state, minutes=0, **kw):
    args = dict(
        now=T0 + timedelta(minutes=minutes),
        enabled=True,
        prev_precool_c=0.0,
        live_effect=0.0,
        forecast_now=None,
        forecast_ahead_max=0.4,
        indoor_c=22.0,
        target_c=21.5,
    )
    args.update(kw)
    return sun_hold.next_hold(state, **args)


def test_disabled_gives_no_hold():
    state, hold = step(SunHoldState(), enabled=False, prev_precool_c=2.0)
    assert hold == 0.0
    assert state == SunHoldState()


def test_tiny_precool_is_not_held():
    _, hold = step(SunHoldState(), prev_precool_c=0.1)
    assert hold == 0.0


def test_keeps_the_highest_level_while_precool_fades_and_sun_rises():
    state, hold = step(SunHoldState(), prev_precool_c=1.5)
    assert hold == 1.5
    # Pre-cool fades to 0.4 as the sun rises, but the hold stays.
    state, hold = step(state, 90, prev_precool_c=0.4, live_effect=0.2, forecast_now=0.2)
    assert hold == 1.5
    state, hold = step(state, 180, prev_precool_c=0.0, live_effect=0.43, forecast_now=0.43)
    assert hold == 1.5


def test_released_after_the_sun_has_peaked():
    state, _ = step(SunHoldState(), prev_precool_c=1.5, live_effect=0.0)
    state, hold = step(state, 180, live_effect=0.43, forecast_now=0.43)
    assert hold == 1.5
    state, hold = step(state, 300, live_effect=0.30, forecast_now=0.30)
    assert hold == 0.0
    assert state == SunHoldState()


def test_released_when_live_sun_is_far_below_the_forecast():
    state, _ = step(SunHoldState(), prev_precool_c=1.5)
    _, hold = step(state, 120, live_effect=0.1, forecast_now=0.4)
    assert hold == 0.0


def test_a_dark_forecast_does_not_release():
    state, _ = step(SunHoldState(), prev_precool_c=1.5)
    _, hold = step(state, 30, live_effect=0.0, forecast_now=0.05)
    assert hold == 1.5


def test_released_when_house_is_below_target():
    state, _ = step(SunHoldState(), prev_precool_c=1.5)
    _, hold = step(state, 30, indoor_c=21.0)
    assert hold == 0.0


def test_unknown_indoor_does_not_release():
    state, _ = step(SunHoldState(), prev_precool_c=1.5)
    _, hold = step(state, 30, indoor_c=None)
    assert hold == 1.5


def test_released_after_the_timeout():
    state, _ = step(SunHoldState(), prev_precool_c=1.5)
    _, hold = step(state, 10 * 60)
    assert hold == 0.0


def test_released_pre_dawn_when_the_forecast_withdraws_the_sun():
    state, _ = step(SunHoldState(), prev_precool_c=1.5)
    _, hold = step(state, 60, forecast_ahead_max=0.05)
    assert hold == 0.0


def test_unknown_forecast_ahead_does_not_release():
    state, _ = step(SunHoldState(), prev_precool_c=1.5)
    _, hold = step(state, 60, forecast_ahead_max=None)
    assert hold == 1.5


def test_low_forecast_ahead_after_the_sun_is_up_does_not_release_early():
    state, _ = step(SunHoldState(), prev_precool_c=1.5)
    state, _ = step(state, 120, live_effect=0.4, forecast_now=0.4)
    _, hold = step(state, 180, live_effect=0.4, forecast_now=0.4, forecast_ahead_max=0.1)
    assert hold == 1.5
