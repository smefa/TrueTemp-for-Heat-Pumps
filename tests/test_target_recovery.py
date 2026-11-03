"""Manual target assistance follows the gap without becoming a permanent boost."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

recovery = load("target_recovery")


def resolve(state=None, **overrides):
    inputs = dict(
        now_s=100.0,
        target_c=21.5,
        indoor_temp_c=21.0,
        indoor_available=True,
        enabled=True,
        rise_hours=1.5,
    )
    inputs.update(overrides)
    return recovery.resolve(state or recovery.TargetRecovery(21.0, was_enabled=True), **inputs)


@pytest.mark.parametrize("step, expected", [(0.5, -1.0), (-0.5, 1.0)])
def test_half_degree_change_immediately_acts_in_the_right_direction(step, expected):
    _, adjustment = resolve(target_c=21.0 + step)
    assert adjustment == pytest.approx(expected)


@pytest.mark.parametrize("step", [0.1, -0.1])
def test_small_changes_are_not_lost_to_float_rounding(step):
    state, adjustment = resolve(target_c=21.0 + step)
    assert state.direction != 0.0
    assert adjustment == pytest.approx(-2.0 * step)


def test_no_target_change_or_startup_does_not_push():
    state, adjustment = resolve(target_c=21.0, indoor_temp_c=19.0)
    assert state.direction == adjustment == 0.0


def test_restored_target_on_first_active_cycle_is_not_a_manual_change():
    state, adjustment = resolve(recovery.TargetRecovery(20.0))
    assert state.direction == adjustment == 0.0
    state, adjustment = resolve(state, target_c=22.0)
    assert adjustment < 0.0


def test_subthreshold_change_does_not_start_recovery():
    state, adjustment = resolve(target_c=21.01)
    assert state.direction == adjustment == 0.0


@pytest.mark.parametrize("target, indoor, nearer", [(21.5, 21.0, 21.3), (20.5, 21.0, 20.7)])
def test_assistance_tapers_with_remaining_gap(target, indoor, nearer):
    state, initial = resolve(target_c=target, indoor_temp_c=indoor)
    _, tapered = resolve(state, target_c=target, indoor_temp_c=nearer, now_s=200.0)
    assert abs(tapered) < abs(initial)
    assert tapered == pytest.approx(initial * 0.4)


@pytest.mark.parametrize("target, indoor", [(21.5, 21.4), (20.5, 20.6)])
def test_stops_near_target_and_does_not_rearm_when_room_drifts(target, indoor):
    state, _ = resolve(target_c=target)
    state, adjustment = resolve(state, target_c=target, indoor_temp_c=indoor)
    assert state.direction == adjustment == 0.0
    _, later = resolve(state, target_c=target, indoor_temp_c=21.0)
    assert later == 0.0


@pytest.mark.parametrize("target, indoor", [(21.5, 22.0), (20.5, 20.0)])
def test_already_across_target_does_not_push_the_wrong_way(target, indoor):
    state, adjustment = resolve(target_c=target, indoor_temp_c=indoor)
    assert state.direction == adjustment == 0.0


@pytest.mark.parametrize("target, expected", [(26.0, -2.0), (16.0, 2.0)])
def test_large_gap_is_capped(target, expected):
    _, adjustment = resolve(target_c=target)
    assert adjustment == expected


@pytest.mark.parametrize("rise, window", [(0.0, 2.0), (1.5, 3.0), (8.0, 6.0)])
def test_window_tracks_response_time_with_bounds(rise, window):
    state, _ = resolve(rise_hours=rise)
    assert state.deadline_s == 100.0 + window * 3600.0
    expired, adjustment = resolve(state, now_s=state.deadline_s)
    assert expired.direction == adjustment == 0.0


def test_unavailable_sensor_suppresses_without_extending_deadline():
    state, _ = resolve()
    missing, adjustment = resolve(state, now_s=200.0, indoor_available=False)
    assert adjustment == 0.0
    assert missing.deadline_s == state.deadline_s
    _, resumed = resolve(missing, now_s=300.0)
    assert resumed == -1.0
    expired, adjustment = resolve(missing, now_s=state.deadline_s, indoor_temp_c=None)
    assert expired.direction == adjustment == 0.0


def test_disabled_consumes_changes_and_cancels_pending_recovery():
    state, _ = resolve()
    state, adjustment = resolve(state, target_c=20.5, enabled=False)
    assert state.direction == adjustment == 0.0
    _, reenabled = resolve(state, target_c=20.5)
    assert reenabled == 0.0


def test_reversal_replaces_direction_and_deadline():
    state, _ = resolve()
    reversed_state, adjustment = resolve(state, now_s=500.0, target_c=20.5)
    assert adjustment == 1.0
    assert reversed_state.deadline_s == 500.0 + 3.0 * 3600.0


def test_small_step_uses_a_smaller_stopping_margin():
    state, _ = resolve(target_c=21.1)
    state, adjustment = resolve(state, target_c=21.1, indoor_temp_c=21.04)
    assert adjustment < 0.0
    state, adjustment = resolve(state, target_c=21.1, indoor_temp_c=21.05)
    assert state.direction == adjustment == 0.0
