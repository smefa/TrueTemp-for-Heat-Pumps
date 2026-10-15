"""Tests for the outdoor-reading jump guard."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

guard = load("outdoor_guard")

T0 = 1_000_000.0


def run(readings, state=None):
    """Feed (seconds_after_T0, reading) pairs; return the final state and every result."""
    state = state or guard.initial_state()
    results = []
    for offset_s, reading in readings:
        state, result = guard.check(state, reading, T0 + offset_s)
        results.append(result)
    return state, results


class TestGlitches:
    def test_the_house_2_restart_sequence_is_ignored(self):
        """5.3, then 0 for 5 s, 5.0, 8.3 for 19 s, then 5.3 — as recorded."""
        _, results = run([(0, 5.3), (17, 0.0), (22, 5.0), (24, 8.3), (43, 5.3)])
        assert [r.value_c for r in results] == [5.3, 5.3, 5.0, 5.0, 5.3]
        assert [r.held for r in results] == [False, True, False, True, False]

    def test_a_gradual_fall_to_zero_is_never_held(self):
        readings = [(i * 900, 2.0 - 0.4 * i) for i in range(6)]
        _, results = run(readings)
        assert not any(r.held for r in results)
        assert results[-1].value_c == pytest.approx(0.0)

    def test_the_held_result_still_reports_the_real_reading(self):
        _, results = run([(0, 5.3), (10, 0.0)])
        assert results[-1].reading_c == 0.0
        assert results[-1].value_c == 5.3


class TestRealJumps:
    def test_a_jump_that_stays_put_is_accepted_after_confirmation(self):
        _, results = run([(0, 5.0), (60, 9.0), (200, 9.2), (360, 9.1)])
        assert [r.value_c for r in results] == [5.0, 5.0, 5.0, 9.1]
        assert not results[-1].held

    def test_confirmation_restarts_if_the_new_value_moves(self):
        _, results = run([(0, 5.0), (60, 9.0), (200, 11.0), (400, 11.1), (560, 11.0)])
        assert results[3].held
        assert results[4].value_c == 11.0

    def test_first_reading_is_taken_as_it_is(self):
        _, results = run([(0, -12.0)])
        assert results[0].value_c == -12.0
        assert not results[0].held


class TestNeverSettles:
    def _jumping(self, minutes):
        readings = [(0, 5.0)]
        value = 9.0
        for m in range(1, minutes + 1):
            value = 9.0 if value != 9.0 else 0.0
            readings.append((m * 60, value))
        return run(readings)

    def test_no_warning_for_a_short_hold(self):
        _, results = self._jumping(10)
        assert results[-1].held
        assert not results[-1].warn

    def test_warns_after_fifteen_minutes(self):
        _, results = self._jumping(16)
        assert results[-1].warn
        assert results[-1].value_c == 5.0

    def test_gives_up_after_an_hour(self):
        _, results = self._jumping(61)
        assert results[-1].value_c is None
        assert results[-1].warn

    def test_recovers_once_it_settles_after_giving_up(self):
        state, _ = self._jumping(61)
        state, a = guard.check(state, 9.5, T0 + 62 * 60)
        state, b = guard.check(state, 9.6, T0 + 68 * 60)
        assert a.value_c is None
        assert b.value_c == 9.6
        assert not b.held


class TestSourceLost:
    def test_unavailable_time_does_not_confirm_a_glitch(self):
        """0, then unavailable for six minutes, then 0 again at startup: the
        second 0 must not be accepted on the strength of the first."""
        state, _ = run([(0, 5.0), (10, 0.0)])
        state = guard.source_lost(state)
        _, result = guard.check(state, 0.0, T0 + 10 + 6 * 60)
        assert result.held
        assert result.value_c == 5.0

    def test_the_last_good_value_and_hold_time_are_kept(self):
        state, _ = run([(0, 5.0), (10, 0.0)])
        lost = guard.source_lost(state)
        assert lost.accepted_c == 5.0
        assert lost.held_since_s == state.held_since_s
        assert guard.confirm_due_s(lost) is None

    def test_confirmation_is_due_five_minutes_after_the_jump(self):
        state, _ = run([(0, 5.0), (60, 9.0)])
        assert guard.confirm_due_s(state) == pytest.approx(T0 + 60 + 300)


class TestReference:
    def test_a_restored_reference_guards_the_first_reading(self):
        """The restart case: a reference persisted a few minutes ago."""
        state = guard.GuardState(accepted_c=5.3, accepted_at_s=T0 - 120)
        _, results = run([(0, 0.0)], state)
        assert results[0].held
        assert results[0].value_c == 5.3

    def test_an_old_reference_is_not_trusted(self):
        state = guard.GuardState(accepted_c=5.3, accepted_at_s=T0 - 2 * 3600)
        _, results = run([(0, -3.0)], state)
        assert not results[0].held
        assert results[0].value_c == -3.0
