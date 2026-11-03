"""External pause timing, safety release, and bounded recovery."""

from __future__ import annotations

from dataclasses import replace
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from loader import load  # noqa: E402

pause = load("heating_pause")
learner = load("learner")


def advance(state, now=1000.0, **changes):
    values = dict(
        now_s=now, enabled=True, available=True, indoor_c=21.0, coldest_c=21.0,
        target_c=21.0, minimum_c=18.0, rise_hours=1.0, recovery_allowed=True,
    )
    values.update(changes)
    return pause.advance(state, **values)


def test_unused_feature_is_inert():
    state = pause.HeatingPause()
    assert advance(state) == (state, 0.0)
    assert not state.learning_frozen(1000)


def test_default_duration_and_timer_replacement():
    state = pause.request(pause.HeatingPause(), 1000, pause.DEFAULT_DURATION_S)
    assert state.deadline_s == 8200
    assert advance(state, now=8199)[0].active
    state = pause.request(state, 2000, 600)
    assert state.started_s == 1000
    assert state.deadline_s == 2600
    assert not advance(state, now=2600)[0].active


@pytest.mark.parametrize("duration", [0, -1, 86401, math.inf, math.nan])
def test_invalid_duration_is_rejected(duration):
    with pytest.raises(ValueError):
        pause.request(pause.HeatingPause(), 1000, duration)


def test_renewal_cannot_extend_a_continuous_pause_past_24_hours():
    state = pause.request(pause.HeatingPause(), 1000, 86400)
    state = pause.request(state, 80000, 86400)
    assert state.deadline_s == 87400


@pytest.mark.parametrize("changes, reason", [
    ({"enabled": False}, "compensation disabled"),
    ({"available": False}, "indoor sensor unavailable"),
    ({"indoor_c": None}, "indoor sensor unavailable"),
    ({"coldest_c": math.nan}, "indoor sensor unavailable"),
    ({"coldest_c": 18.0}, "minimum indoor temperature"),
])
def test_safety_release(changes, reason):
    state = pause.request(pause.HeatingPause(), 1000, 7200)
    state, adjustment = advance(state, **changes)
    assert not state.active
    assert state.release_reason == reason
    assert adjustment == 0
    assert state.learning_frozen(1000)


def test_cold_house_gets_capped_tapering_assistance_and_then_settles():
    state = pause.request(pause.HeatingPause(), 1000, 7200)
    state, adjustment = advance(state, now=8200, indoor_c=19.0, coldest_c=19.0)
    assert not state.active
    assert adjustment == -2.0
    deadline = state.recovery_until_s
    state, adjustment = advance(state, now=9000, indoor_c=20.5)
    assert adjustment == -1.0
    assert state.recovery_until_s == deadline
    state, adjustment = advance(state, now=10000, indoor_c=20.95)
    assert adjustment == 0
    assert state.recovery_until_s == 0
    assert state.learning_frozen(13599)
    assert not state.learning_frozen(13600)


def test_no_deficit_means_no_recovery_boost():
    state = pause.request(pause.HeatingPause(), 1000, 7200)
    state, adjustment = advance(state, now=8200)
    assert not state.active
    assert adjustment == 0
    assert state.recovery_until_s
    assert state.settle_until_s == 11800
    state, adjustment = advance(state, now=11800)
    assert not state.recovery_until_s
    assert not state.learning_frozen(11800)
    assert adjustment == 0


def test_delayed_cooling_after_release_still_gets_recovery_assistance():
    state = pause.request(pause.HeatingPause(), 1000, 7200)
    state, adjustment = advance(state, now=8200)
    assert adjustment == 0
    state, adjustment = advance(state, now=9100, indoor_c=20.5)
    assert adjustment == -1.0
    assert state.assisted


def test_recovery_and_settling_have_bounded_deadlines():
    state = pause.request(pause.HeatingPause(), 1000, 7200)
    state, _ = advance(state, now=8200, indoor_c=19.0, rise_hours=100)
    assert state.recovery_until_s == 8200 + 6 * 3600
    state, adjustment = advance(state, now=state.recovery_until_s, indoor_c=19.0,
                                rise_hours=100)
    assert adjustment == 0
    deadline = state.settle_until_s
    state, _ = advance(state, now=deadline + 1, indoor_c=19.0)
    assert not state.learning_frozen(deadline + 1)
    assert state.settle_until_s == deadline


def test_missing_sensor_suppresses_recovery_without_extending_it():
    state = pause.release(pause.HeatingPause(deadline_s=8200), 2000, 1, "action")
    deadline = state.recovery_until_s
    state, adjustment = advance(state, now=3000, available=False, indoor_c=19.0)
    assert adjustment == 0
    assert state.recovery_until_s == deadline
    state, adjustment = advance(state, now=4000, indoor_c=19.0)
    assert adjustment == -2
    assert state.recovery_until_s == deadline


def test_other_recovery_takes_precedence_without_stacking():
    state = pause.release(pause.HeatingPause(deadline_s=8200), 2000, 1, "action")
    state, adjustment = advance(state, now=3000, indoor_c=19.0, recovery_allowed=False)
    assert adjustment == 0
    assert not state.recovery_until_s
    assert state.learning_frozen(3000)


def test_repeated_resume_is_idempotent_and_restart_is_unpaused():
    state = pause.release(pause.HeatingPause(deadline_s=8200), 2000, 1, "action")
    assert pause.release(state, 3000, 1, "action") == state
    assert not pause.HeatingPause().active


def test_new_request_clears_previous_recovery():
    state = pause.HeatingPause(recovery_until_s=9000, settle_until_s=10000)
    state = pause.request(state, 2000, 7200)
    assert state == replace(pause.HeatingPause(), started_s=2000, deadline_s=9200)


def test_inactive_baseline_does_not_learn_pause_or_recovery_cooling():
    state = learner.initial_state()
    for index in range(40):
        state, result = learner.step(state, learner.LearnerInputs(
            now_s=1000 + index * 900, dt_hours=0.25,
            indoor_temp_c=19.0, indoor_data_available=True, target_c=21.0,
            outdoor_temp_c=0.0, heating_hard_limit_engaged=False,
            is_active=False, price_braking=False, rise_hours=1.0, heating_pause=True,
        ))
    assert state.baseline_bins == learner.initial_state().baseline_bins
    assert "external heating pause" in result.baseline_reason
