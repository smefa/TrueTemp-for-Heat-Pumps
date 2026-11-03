"""Measured warm-up patterns, latch safety and learning recovery."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from loader import load  # noqa: E402

heat = load("excess_heat")


def tick(state, minute, indoor=23.0, **kw):
    inputs = dict(
        now_s=minute * 60, indoor_c=indoor, target_c=21.5,
        sensors=frozenset({"room"}), available=True, fireplace_enabled=True,
        solar_enabled=False, solar_gain_c=0.0, solar_falling=False,
        excluded=False, rise_hours=0.75,
    )
    inputs.update(kw)
    return heat.advance(state, **inputs)


def slow(**kw):
    state = heat.ExcessHeatState()
    for minute in (0, 15, 30, 45):
        state = tick(state, minute, **kw)
    return state


def test_slow_flat_reporting_sensor_can_pause_all_evening():
    state = slow()
    assert state.source == "fireplace"
    for minute in range(60, 601, 15):
        state = tick(state, minute)
        assert state.source == "fireplace"


def test_fast_freezes_before_trigger_and_releases_with_rise_holdoff():
    state = tick(heat.ExcessHeatState(), 0, 21.5)
    state = tick(state, 15, 22.5)
    assert state.learning_frozen(900) and not state.source
    state = tick(state, 30, 23.0)
    assert state.source == "fireplace"
    state = tick(state, 45, 22.0)
    assert state.source is None
    assert state.learning_frozen(45 * 60 + 2699)
    assert not state.learning_frozen(90 * 60)


def test_fast_below_target_freezes_then_holds_without_pause():
    state = tick(heat.ExcessHeatState(), 0, 20.0)
    state = tick(state, 15, 20.5)
    assert state.fast_rise and not state.source
    state = tick(state, 30, 20.5)
    assert not state.fast_rise and not state.source
    assert state.learning_frozen(31 * 60)


def test_dip_resets_slow_timer_and_cooking_burst_does_not_trigger():
    state = tick(heat.ExcessHeatState(), 0, 21.5)
    state = tick(state, 15, 23.0)
    state = tick(state, 25, 22.4)
    assert not state.source and state.fast_since_s is None
    for minute in (30, 45, 60):
        state = tick(state, minute)
    assert state.source is None
    state = tick(state, 75)
    assert state.source == "fireplace"


@pytest.mark.parametrize("overrides", [
    {"available": False}, {"indoor_c": None}, {"excluded": True},
    {"fireplace_enabled": False}, {"target_c": 22.0},
    {"sensors": frozenset({"other"})},
])
def test_pause_releases_on_invalid_inputs_or_control_change(overrides):
    state = tick(slow(), 60, **overrides)
    assert not state.source
    assert state.learning_frozen(60 * 60)


def test_gap_resets_evidence_and_releases_active_pause():
    assert not tick(slow(), 90).source
    state = tick(heat.ExcessHeatState(), 0)
    state = tick(state, 60)
    assert not state.source and state.slow_since_s == 3600


@pytest.mark.parametrize("pattern", ["fast", "slow"])
def test_solar_alone_supports_both_patterns(pattern):
    kw = dict(fireplace_enabled=False, solar_enabled=True, solar_gain_c=0.5)
    if pattern == "slow":
        state = slow(**kw)
    else:
        state = tick(heat.ExcessHeatState(), 0, 21.5, **kw)
        state = tick(state, 15, 22.5, **kw)
        state = tick(state, 30, 23.0, **kw)
    assert state.source == "solar"
    state = tick(state, 45 if pattern == "fast" else 60, 22.5, **kw)
    assert state.source == "solar"  # Still sunny / not falling.
    state = tick(state, 60 if pattern == "fast" else 75, 22.5, solar_falling=True, **kw)
    assert not state.source


@pytest.mark.parametrize("gain,enabled", [(0.0, True), (0.5, False)])
def test_sunset_or_disabling_solar_releases_even_when_hot(gain, enabled):
    state = slow(fireplace_enabled=False, solar_enabled=True, solar_gain_c=0.5)
    state = tick(state, 60, 24.0, fireplace_enabled=False,
                 solar_enabled=enabled, solar_gain_c=gain)
    assert not state.source


def test_solar_never_keeps_heat_paused_once_room_cools_even_if_forecast_rises():
    state = slow(fireplace_enabled=False, solar_enabled=True, solar_gain_c=0.5)
    state = tick(state, 60, 22.0, fireplace_enabled=False,
                 solar_enabled=True, solar_gain_c=1.0, solar_falling=False)
    assert not state.source
    assert state.learning_frozen(3600)


def test_outdoor_failure_drops_pause_but_preserves_recovery():
    state = heat.source_lost(slow(), 3600, 0.75)
    assert not state.source
    assert state.holdoff_until_s == 6300
    state = heat.source_lost(state, 3700, 0.75)
    assert state.holdoff_until_s == 6300  # Repeated failures do not extend forever.
    state = tick(state, 75)
    assert state.learning_frozen(4500)


def test_disabled_features_leave_learning_alone():
    state = heat.ExcessHeatState()
    for minute in range(0, 61, 15):
        state = tick(state, minute, 23 + minute / 15, fireplace_enabled=False)
        assert not state.source and not state.learning_frozen(minute * 60)


def test_high_frequency_refreshes_keep_rate_history_bounded():
    state = heat.ExcessHeatState()
    for second in range(3601):
        state = tick(state, second / 60, 20 + second / 3600)
    assert state.fast_rise
    assert state.drift_c_per_h == pytest.approx(1.0)
    assert len(state.samples) <= 31


def test_source_change_does_not_inherit_another_sources_timer():
    state = heat.ExcessHeatState()
    for minute in (0, 15, 30):
        state = tick(state, minute)
    state = tick(state, 45, solar_enabled=True, solar_gain_c=0.5)
    assert not state.source
    assert state.slow_since_s == 45 * 60


def test_slow_margin_is_configurable():
    assert not slow(margin_c=2.0).source
