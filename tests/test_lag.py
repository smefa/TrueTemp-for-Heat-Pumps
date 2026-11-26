"""Unit tests for the response-time estimator.

The load-bearing test is `test_recovers_known_asymmetric_lags`: it builds a
synthetic house whose rise and fall delays are known and different, and checks
both are recovered. That is the same discipline that made the previous model's
negative result trustworthy — a harness self-tested against data with known
answers before it is pointed at real data.

Only clean, full-window events become readings: truncated windows, windows
holding an opposite action, and continuations are discarded, and each reading
is recorded exactly once. Readings are long-lived and blended with the heating
type default, so one reading nudges the value and many move it.

`test_offset_events_during_a_setpoint_transient_are_discarded` guards the
finding that forced this module's rewrite: in a closed loop the controller
output and the plant output are coupled, so anything that reads them as cause
and effect without checking what else was moving will measure nonsense.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

lag = load("lag")

STEP_MINUTES = 15.0


def make_series(
    rise_steps: int, fall_steps: int, cycles: int = 6, half_period: int = 40
) -> tuple[list[float], list[float]]:
    """A square-wave offset and the indoor response of a pure-delay house.

    Down-steps of the offset (spoofing colder, so asking for more heat) show up
    as indoor rising `rise_steps` later; up-steps show up as indoor falling
    `fall_steps` later. Deliberately asymmetric, because a real house's slab
    keeps delivering long after the pump backs off.
    """
    n = cycles * 2 * half_period
    offsets = [0.0 if (i // half_period) % 2 == 0 else -1.0 for i in range(n)]
    indoors = [20.0]
    for i in range(1, n):
        delta = 0.0
        src = i - rise_steps
        if src >= 1:
            move = offsets[src] - offsets[src - 1]
            if move < 0:
                delta += -move * 0.5
        src = i - fall_steps
        if src >= 1:
            move = offsets[src] - offsets[src - 1]
            if move > 0:
                delta += -move * 0.5
        indoors.append(indoors[-1] + delta)
    return offsets, indoors


T0 = 1_000_000.0
STEP_S = STEP_MINUTES * 60.0
WINDOW = int(lag.MAX_LAG_MINUTES / STEP_MINUTES)


def build_state(
    offsets: list[float],
    indoors: list[float],
    targets: list[float] | None = None,
    heating_type: str = "radiators",
    state: "lag.LagState | None" = None,
    t0: float = T0,
) -> "lag.LagState":
    """Push then harvest sample by sample, as the coordinator does.

    Defaults to a target that never moves, so offset events are readable.
    """
    if targets is None:
        targets = [21.0] * len(offsets)
    if state is None:
        state = lag.initial_state(STEP_MINUTES)
    for i, (offset, indoor, target) in enumerate(zip(offsets, indoors, targets)):
        state = lag.push(state, offset, indoor, target)
        state = lag.harvest(state, heating_type, t0 + i * STEP_S)
    return state


def est(state, heating_type="radiators", now_s=None):
    return lag.estimate(state, heating_type, now_s)


def reading(direction, minutes, at_s=T0):
    return lag.LagReading(direction, minutes, at_s)


class TestFallbacks:
    def test_each_heating_type_has_a_distinct_pair(self):
        radiators = lag.fallback_minutes("radiators")
        underfloor = lag.fallback_minutes("underfloor")
        mixed = lag.fallback_minutes("mixed")
        assert radiators[0] < mixed[0] < underfloor[0]
        assert radiators[1] < mixed[1] < underfloor[1]

    def test_fall_is_never_shorter_than_rise(self):
        """A house cannot stop gaining heat faster than it starts."""
        for heating_type in lag.HEATING_TYPES:
            rise, fall = lag.fallback_minutes(heating_type)
            assert fall >= rise

    def test_unknown_type_falls_back_to_the_default(self):
        assert lag.fallback_minutes("geothermal wizardry") == lag.fallback_minutes(
            lag.DEFAULT_HEATING_TYPE
        )
        assert lag.fallback_minutes(None) == lag.fallback_minutes(
            lag.DEFAULT_HEATING_TYPE
        )


class TestBuffer:
    def test_push_appends_every_series(self):
        state = lag.push(lag.initial_state(STEP_MINUTES), -1.0, 20.0, 21.0)
        assert state.offsets_c == (-1.0,)
        assert state.indoors_c == (20.0,)
        assert state.targets_c == (21.0,)

    def test_buffer_is_bounded(self):
        state = lag.initial_state(STEP_MINUTES)
        for i in range(lag.BUFFER_SAMPLES + 50):
            state = lag.push(state, -1.0, 20.0 + i, 21.0)
        assert len(state.offsets_c) == lag.BUFFER_SAMPLES
        assert len(state.indoors_c) == lag.BUFFER_SAMPLES
        # The newest samples survive, not the oldest.
        assert state.indoors_c[-1] == 20.0 + lag.BUFFER_SAMPLES + 49

    def test_missing_indoor_reading_clears_the_buffer(self):
        """A gap must not be stitched across: the discontinuity would register
        as an enormous fake movement event at whatever lag lined up."""
        state = build_state(*make_series(4, 8, cycles=2))
        assert state.indoors_c
        state = lag.push(state, -1.0, None, 21.0)
        assert state.offsets_c == ()
        assert state.indoors_c == ()
        assert state.targets_c == ()


class TestEstimate:
    def test_insufficient_history_reports_unmeasured(self):
        state = lag.initial_state(STEP_MINUTES)
        for i in range(10):
            state = lag.push(state, -1.0, 20.0 + i * 0.1, 21.0)
            state = lag.harvest(state, "radiators", T0 + i)
        result = est(state)
        assert not result.rise_measured
        assert not result.fall_measured
        assert (result.rise_minutes, result.fall_minutes) == lag.fallback_minutes(
            "radiators"
        )
        assert "Building history" in result.reason

    def test_zero_readings_is_exactly_the_fallback(self):
        for heating_type in lag.HEATING_TYPES:
            result = est(lag.initial_state(STEP_MINUTES), heating_type)
            assert (result.rise_minutes, result.fall_minutes) == lag.fallback_minutes(
                heating_type
            )
            assert result.rise_source == result.fall_source == "default"
            assert result.rise_events == result.fall_events == 0

    def test_flat_data_reports_unmeasured(self):
        n = 400
        state = build_state([0.0] * n, [20.0] * n, heating_type="underfloor")
        result = est(state, "underfloor")
        assert not result.rise_measured
        assert not result.fall_measured
        assert (result.rise_minutes, result.fall_minutes) == lag.fallback_minutes(
            "underfloor"
        )

    def test_offset_moves_with_no_response_report_unmeasured(self):
        n = 400
        offsets = [0.0 if (i // 40) % 2 == 0 else -1.0 for i in range(n)]
        state = build_state(offsets, [20.0] * n)
        result = est(state)
        assert not result.rise_measured
        assert not result.fall_measured

    def test_sub_noise_offset_movement_is_ignored(self):
        n = 400
        tiny = lag.EVENT_MIN_MOVE_C / 10.0
        offsets = [0.0 if (i // 40) % 2 == 0 else -tiny for i in range(n)]
        indoors = [20.0 + (0.1 if (i // 40) % 2 else 0.0) for i in range(n)]
        assert not est(build_state(offsets, indoors)).rise_measured

    def test_a_window_is_valid_only_while_the_target_holds(self):
        steady = (21.0,) * 30
        assert lag._target_holds(steady, 0, 10)
        moved = (21.0,) * 5 + (23.0,) * 25
        assert not lag._target_holds(moved, 0, 10)
        assert lag._target_holds(moved, 0, 3)

    def test_never_returns_a_non_positive_time(self):
        for state in (
            lag.initial_state(STEP_MINUTES),
            build_state(*make_series(1, 1)),
            build_state(*make_series(4, 8)),
        ):
            result = est(state, None)
            assert result.rise_minutes > 0
            assert result.fall_minutes > 0

    def test_result_hours_helpers(self):
        result = est(build_state(*make_series(4, 8)))
        assert result.rise_hours == result.rise_minutes / 60.0
        assert result.fall_hours == result.fall_minutes / 60.0


class TestCleanReadings:
    def test_clean_isolated_events_become_readings(self):
        state = build_state(*make_series(4, 8))
        rises = [r.minutes for r in state.readings if r.direction == "rise"]
        falls = [r.minutes for r in state.readings if r.direction == "fall"]
        assert rises and falls
        assert set(rises) == {4 * STEP_MINUTES}
        assert set(falls) == {8 * STEP_MINUTES}

    def test_asymmetry_survives_the_blend(self):
        result = est(build_state(*make_series(4, 8)))
        assert result.rise_measured and result.fall_measured
        # Each direction is pulled from its own default toward its own reading.
        d_rise, d_fall = lag.fallback_minutes("radiators")
        assert result.rise_minutes != d_rise or result.fall_minutes != d_fall
        assert result.rise_source == result.fall_source == "blended"

    def test_truncated_window_is_discarded(self):
        """An event with less than a full window of history is still pending,
        and recording it early would measure the controller's cadence."""
        offsets = [0.0] * 5 + [-1.0] * 100
        indoors = [20.0] * 9 + [20.5] * 100
        state = build_state(offsets[: 5 + WINDOW], indoors[: 5 + WINDOW])
        assert state.readings == ()
        # The same event is recorded once the window completes.
        state = build_state(offsets, indoors)
        assert [r.direction for r in state.readings] == ["rise"]

    def test_same_direction_continuation_is_allowed(self):
        """A second step the same way before the response ends is not an
        opposite action, so the first event is still a clean reading."""
        n = 200
        offsets = [0.0] * 5 + [-0.5] * 5 + [-1.0] * (n - 10)
        indoors = [20.0] * 9 + [20.5] * (n - 9)
        state = build_state(offsets, indoors)
        assert len([r for r in state.readings if r.direction == "rise"]) == 1

    def test_opposite_event_in_window_discards(self):
        n = 200
        # Down at 5, up again at 5 + 10: inside the 24-step window of both.
        offsets = [0.0] * 5 + [-1.0] * 10 + [0.0] * (n - 15)
        indoors = [20.0] * 9 + [20.5] * (n - 9)
        state = build_state(offsets, indoors)
        assert state.readings == ()

    def test_event_is_recorded_once_across_repeated_scans(self):
        state = build_state(*make_series(4, 8))
        count = len(state.readings)
        assert count
        # Re-harvesting the same state later, many times, adds nothing.
        for k in range(5):
            state = lag.harvest(state, "radiators", T0 + 10**6 + k)
        assert len(state.readings) == count

    def test_event_is_recorded_once_as_the_buffer_shifts(self):
        """Keep pushing quiet samples until the buffer has slid far past the
        event; the count must not change."""
        state = build_state(*make_series(4, 8))
        count = len(state.readings)
        quiet = lag.BUFFER_SAMPLES + 50
        state = build_state(
            [state.offsets_c[-1]] * quiet,
            [state.indoors_c[-1]] * quiet,
            state=state,
            t0=T0 + 10**5,
        )
        assert len(state.readings) == count
        assert state.sample_counter > lag.BUFFER_SAMPLES

    def test_readings_survive_a_buffer_wipe(self):
        state = build_state(*make_series(4, 8))
        before = state.readings
        assert before
        wiped = lag.push(state, -1.0, None, 21.0)
        assert wiped.indoors_c == ()
        assert wiped.readings == before
        assert wiped.sample_counter == state.sample_counter
        # And the estimate keeps using them.
        assert est(wiped, now_s=T0).rise_measured

    def test_offset_events_during_a_setpoint_transient_are_discarded(self):
        offsets, indoors = make_series(4, 8)
        assert est(build_state(offsets, indoors)).rise_measured
        drifting = [21.0 + 0.05 * i for i in range(len(offsets))]
        result = est(build_state(offsets, indoors, drifting))
        assert not result.rise_measured
        assert not result.fall_measured

    def test_target_steps_alone_are_usable_excitation(self):
        delay, half_period, n = 5, 40, 600
        targets = [21.0 if (i // half_period) % 2 == 0 else 23.0 for i in range(n)]
        indoors = [targets[max(0, i - delay)] for i in range(n)]
        state = build_state([0.0] * n, indoors, targets)
        assert state.readings
        assert {r.minutes for r in state.readings} == {delay * STEP_MINUTES}


class TestBlend:
    def test_one_reading_nudges_three_move_it(self):
        default = lag.fallback_minutes("radiators")[0]
        assert default == 90.0
        one = lag.LagState(readings=(reading("rise", 225.0),), heating_type="radiators")
        three = lag.LagState(
            readings=tuple(reading("rise", 225.0) for _ in range(3)),
            heating_type="radiators",
        )
        assert est(one).rise_minutes == default + (1 / 4) * (225.0 - default)
        assert round(est(one).rise_minutes) == 124
        assert est(three).rise_minutes == default + (3 / 6) * (225.0 - default)
        assert round(est(three).rise_minutes) == 158

    def test_blend_uses_the_median(self):
        state = lag.LagState(
            readings=tuple(reading("rise", m) for m in (60.0, 120.0, 900.0)),
            heating_type="radiators",
        )
        assert est(state).rise_minutes == 90.0 + 0.5 * (120.0 - 90.0)

    def test_directions_blend_independently(self):
        state = lag.LagState(
            readings=(reading("rise", 225.0),), heating_type="radiators"
        )
        result = est(state)
        assert result.rise_source == "blended"
        assert result.fall_source == "default"
        assert result.fall_minutes == lag.fallback_minutes("radiators")[1]
        assert (result.rise_events, result.fall_events) == (1, 0)

    def test_floor_is_half_the_default(self):
        d_rise, d_fall = lag.fallback_minutes("underfloor")
        state = lag.LagState(
            readings=tuple(reading("rise", 15.0) for _ in range(30)),
            heating_type="underfloor",
        )
        result = est(state, "underfloor")
        assert result.rise_minutes == lag.FLOOR_FRACTION * d_rise
        assert "floor" in result.reason
        assert result.fall_minutes == d_fall

    def test_transparency_fields_and_reason(self):
        state = lag.LagState(
            readings=(reading("rise", 225.0),), heating_type="radiators"
        )
        result = est(state)
        assert result.rise_default_minutes == 90.0
        assert result.fall_default_minutes == lag.fallback_minutes("radiators")[1]
        assert "default 90" in result.reason
        assert "1 clean reading" in result.reason
        assert "no clean readings yet" in result.reason

    def test_expired_readings_are_ignored_by_estimate(self):
        old = T0 - lag.READING_MAX_AGE_S - 1
        state = lag.LagState(readings=(reading("rise", 225.0, old),), heating_type="radiators")
        assert not est(state, now_s=T0).rise_measured
        fresh = lag.LagState(readings=(reading("rise", 225.0, T0),), heating_type="radiators")
        assert est(fresh, now_s=T0).rise_measured


class TestMemory:
    def test_harvest_drops_readings_older_than_eight_weeks(self):
        old = T0 - lag.READING_MAX_AGE_S - 10
        state = lag.LagState(
            readings=(reading("rise", 225.0, old), reading("fall", 200.0, T0)),
            heating_type="radiators",
        )
        state = lag.harvest(state, "radiators", T0)
        assert [r.direction for r in state.readings] == ["fall"]

    def test_readings_are_capped_per_direction_keeping_the_newest(self):
        n = lag.MAX_READINGS + 7
        state = lag.LagState(
            readings=tuple(reading("rise", float(100 + i), T0 + i) for i in range(n)),
            heating_type="radiators",
        )
        state = lag.harvest(state, "radiators", T0 + n)
        minutes = [r.minutes for r in state.readings]
        assert len(minutes) == lag.MAX_READINGS
        assert minutes[-1] == float(100 + n - 1)
        assert min(minutes) == float(100 + 7)

    def test_heating_type_change_clears_the_readings(self):
        state = build_state(*make_series(4, 8), heating_type="radiators")
        assert state.readings
        changed = lag.harvest(state, "underfloor", T0 + 10**6)
        assert changed.readings == ()
        assert changed.heating_type == "underfloor"
        result = est(changed, "underfloor")
        assert result.rise_minutes == lag.fallback_minutes("underfloor")[0]

    def test_estimate_ignores_readings_of_another_heating_type(self):
        state = build_state(*make_series(4, 8), heating_type="radiators")
        result = est(state, "underfloor")
        assert not result.rise_measured
        assert result.rise_minutes == lag.fallback_minutes("underfloor")[0]

    def test_clear_readings_keeps_nothing_and_never_re_reads_old_events(self):
        state = build_state(*make_series(4, 8))
        assert state.readings
        cleared = lag.clear_readings(state)
        assert cleared.readings == ()
        assert cleared.indoors_c == ()
        # Pushing again must not resurrect the cleared events.
        again = build_state([0.0] * 5, [20.0] * 5, state=cleared, t0=T0 + 10**6)
        assert again.readings == ()
