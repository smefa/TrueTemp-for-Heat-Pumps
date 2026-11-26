"""Unit tests for the shadow daily pattern."""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

dp = load("daily_pattern")

STEP = 15.0


def _feed(days, warmth, state=None, hours=None, seed=1, noise=0.0):
    """Feed `days` of 15-minute samples; `warmth(hour)` is the true pattern."""
    rng = random.Random(seed)
    state = state or dp.initial_state()
    for _ in range(days):
        day_noise = rng.gauss(0.0, noise)
        for step in range(96):
            hour = step / 4.0
            if hours is not None and int(hour) not in hours:
                continue
            state = dp.update(state, hour, warmth(hour) + day_noise + rng.gauss(0.0, noise), STEP)
    return state


def evening(hour):
    """Too warm in the evening, too cold in the morning."""
    return 0.3 * math.cos((hour - 21.0) / 24.0 * 2.0 * math.pi)


def test_fresh_state_is_learning_and_adds_nothing():
    result = dp.evaluate(dp.initial_state(), 18.0, 120.0, 1.0, STEP)
    assert result.shadow_offset_c == 0.0
    assert result.confident_hours == 0
    assert result.potential_improvement_pct is None
    assert result.reason.startswith("Learning")


def test_repeating_pattern_is_learned_centred_and_trusted():
    state = _feed(21, evening, noise=0.05)
    result = dp.evaluate(state, 12.0, 0.0, 1.0, STEP)
    assert result.confident_hours >= 12
    assert abs(sum(result.shape_c)) < 0.05
    assert result.shape_c[21] > 0.2
    assert result.shape_c[9] < -0.2
    assert result.potential_improvement_pct > 50.0


def test_offset_aims_at_the_hour_the_heat_arrives():
    state = _feed(21, evening, noise=0.05)
    # At 19:00 with a 2 h response the heat lands around 21:00, usually too warm:
    # less heat, a positive offset.
    ahead_of_warm = dp.evaluate(state, 19.0, 120.0, 1.0, STEP)
    assert ahead_of_warm.shadow_offset_c > 0.2
    assert ahead_of_warm.lead_hour == pytest.approx(21.0)
    ahead_of_cold = dp.evaluate(state, 7.0, 120.0, 1.0, STEP)
    assert ahead_of_cold.shadow_offset_c < -0.2


def test_offset_scales_with_the_indoor_to_offset_ratio():
    state = _feed(21, evening, noise=0.05)
    one = dp.evaluate(state, 19.0, 120.0, 1.0, STEP)
    two = dp.evaluate(state, 19.0, 120.0, 2.0, STEP)
    assert two.shadow_offset_c == pytest.approx(2.0 * one.shadow_offset_c)


def test_large_pattern_is_capped_and_uncapped_value_reported():
    state = _feed(21, lambda h: 10.0 * evening(h), noise=0.05)
    result = dp.evaluate(state, 21.0, 0.0, 1.0, STEP)
    assert result.shadow_offset_c == pytest.approx(dp.FF_CAP_C)
    assert result.shadow_offset_uncapped_c > dp.FF_CAP_C


def test_constant_level_is_not_a_pattern():
    """A house that is simply warm all day is the learner's job, not this one's."""
    state = _feed(21, lambda h: 1.0, noise=0.05)
    result = dp.evaluate(state, 12.0, 120.0, 1.0, STEP)
    assert result.confident_hours == 0
    assert result.shadow_offset_c == 0.0


def test_noise_without_pattern_stays_quiet():
    state = _feed(28, lambda h: 0.0, noise=0.3, seed=7)
    result = dp.evaluate(state, 12.0, 120.0, 1.0, STEP)
    assert result.confident_hours <= 2
    assert abs(result.shadow_offset_c) < 0.2
    assert result.potential_improvement_pct is None or result.potential_improvement_pct < 5.0


def test_too_few_days_are_not_trusted():
    state = _feed(int(dp.MIN_DAYS) - 1, evening)
    assert dp.evaluate(state, 19.0, 120.0, 1.0, STEP).confident_hours == 0


def test_partial_day_coverage_is_not_trusted():
    hours = set(range(dp.MIN_COVERED_HOURS - 1))
    state = _feed(21, evening, hours=hours)
    result = dp.evaluate(state, 19.0, 120.0, 1.0, STEP)
    assert result.confident_hours == 0
    assert result.reason.startswith("Learning")


def test_non_finite_sample_is_ignored():
    state = dp.initial_state()
    assert dp.update(state, 3.0, math.nan, STEP) is state
    assert dp.update(state, math.inf, 0.1, STEP) is state


def test_round_trip_through_json():
    state = _feed(10, evening, noise=0.05)
    restored = dp.from_dict(json.loads(json.dumps(dp.to_dict(state))))
    assert restored == state


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("bins"),
        lambda d: d["bins"].pop(),
        lambda d: d["bins"][3].update(mean_c="x"),
        lambda d: d["bins"][3].update(mean_c=math.nan),
        lambda d: d["bins"][3].update(samples=-1),
        lambda d: d["bins"][3].update(samples=True),
        lambda d: d.update(level_c=math.inf),
    ],
)
def test_corrupt_payload_is_dropped_whole(mutate):
    payload = dp.to_dict(_feed(2, evening))
    mutate(payload)
    assert dp.from_dict(payload) is None


def test_missing_payload_is_none():
    assert dp.from_dict(None) is None
    assert dp.from_dict([]) is None
