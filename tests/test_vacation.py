"""Unit tests for the multi-plan vacation module.

Same discipline as `test_holiday.py`: every case here is a synthetic
scenario with a known right answer. `occurrence_window()`'s weekly/yearly
math is the largest new correctness surface in this feature (see
`docs/plan_vacation_plans.md` §7/§8), so it gets the heaviest coverage.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

holiday = load("holiday")
vacation = load("vacation")

VacationPlan = vacation.VacationPlan
occurrence_window = vacation.occurrence_window
resolve_plan = vacation.resolve_plan
resolve_vacation = vacation.resolve_vacation
find_overlap = vacation.find_overlap


def make_plan(**overrides):
    defaults = dict(
        id="p1",
        name="Test plan",
        enabled=True,
        recurrence=vacation.RECURRENCE_ONCE,
        min_temp_c=17.0,
    )
    defaults.update(overrides)
    return VacationPlan(**defaults)


class TestOnceRecurrence:
    def test_scheduled_setback_ramping_done_phases_via_resolve_plan(self):
        plan = make_plan(
            recurrence=vacation.RECURRENCE_ONCE,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 10),
            min_temp_c=17.0,
        )
        kwargs = dict(normal_target_c=21.0, rise_hours=1.0)

        scheduled = resolve_plan(datetime(2026, 1, 1, 0, 0), plan, **kwargs)
        assert scheduled.phase == holiday.HOLIDAY_PHASE_SCHEDULED
        assert scheduled.target_c == 21.0

        setback = resolve_plan(datetime(2026, 1, 5, 6, 0), plan, **kwargs)
        assert setback.phase == holiday.HOLIDAY_PHASE_SETBACK
        assert setback.target_c == 17.0

        probe = resolve_plan(
            datetime.combine(plan.start_date, holiday.HOLIDAY_RETURN_TIME), plan, **kwargs
        )
        ramp_start_at = probe.ramp_start_at
        ramping = resolve_plan(ramp_start_at, plan, **kwargs)
        assert ramping.phase == holiday.HOLIDAY_PHASE_RAMPING

        done = resolve_plan(
            datetime.combine(plan.end_date, holiday.HOLIDAY_RETURN_TIME), plan, **kwargs
        )
        assert done.phase == holiday.HOLIDAY_PHASE_DONE
        assert done.target_c == 21.0

    def test_disabled_plan_is_inactive_regardless_of_dates(self):
        plan = make_plan(
            enabled=False,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 10),
        )
        result = resolve_plan(
            datetime(2026, 1, 6, 0, 0), plan, normal_target_c=21.0, rise_hours=1.0
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
        assert result.target_c == 21.0

    def test_missing_dates_is_invalid(self):
        for start, end in (
            (None, date(2026, 1, 10)),
            (date(2026, 1, 5), None),
            (None, None),
        ):
            plan = make_plan(start_date=start, end_date=end)
            result = resolve_plan(
                datetime(2026, 1, 6, 0, 0), plan, normal_target_c=21.0, rise_hours=1.0
            )
            assert result.phase == holiday.HOLIDAY_PHASE_INVALID
            assert result.target_c == 21.0

    def test_end_not_after_start_is_invalid(self):
        plan = make_plan(start_date=date(2026, 1, 10), end_date=date(2026, 1, 5))
        result = resolve_plan(
            datetime(2026, 1, 6, 0, 0), plan, normal_target_c=21.0, rise_hours=1.0
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INVALID

    def test_min_temp_c_is_clamped_to_the_frost_floor(self):
        plan = make_plan(
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 10),
            min_temp_c=1.0,  # below HOLIDAY_TARGET_MIN_C
        )
        result = resolve_plan(
            datetime(2026, 1, 6, 0, 0), plan, normal_target_c=21.0, rise_hours=1.0
        )
        assert result.phase == holiday.HOLIDAY_PHASE_SETBACK
        assert result.target_c == holiday.HOLIDAY_TARGET_MIN_C

    def test_min_temp_c_above_normal_target_is_clamped_down_to_it(self):
        # A plan whose min_temp_c is above the house's normal target must
        # never make vacation mode heat harder than an occupied house would.
        plan = make_plan(
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 10),
            min_temp_c=24.0,  # above normal_target_c below
        )
        result = resolve_plan(
            datetime(2026, 1, 6, 0, 0), plan, normal_target_c=21.0, rise_hours=1.0
        )
        assert result.phase == holiday.HOLIDAY_PHASE_SETBACK
        assert result.target_c == 21.0


class TestWeeklyOccurrence:
    WEEKLY_KW = dict(
        recurrence=vacation.RECURRENCE_WEEKLY,
        start_weekday=4,  # Friday
        start_time=time(18, 0),
        stop_weekday=0,  # Monday
        stop_time=time(7, 0),
    )

    def test_currently_active_window_is_returned(self):
        # Friday 2026-01-02 18:00 -> Monday 2026-01-05 07:00; now = Saturday.
        plan = make_plan(**self.WEEKLY_KW)
        now = datetime(2026, 1, 3, 10, 0)  # Saturday
        window = occurrence_window(plan, now)
        assert window == (datetime(2026, 1, 2, 18, 0), datetime(2026, 1, 5, 7, 0))

    def test_window_ended_today_advances_to_next_week(self):
        # Same-day plan: Monday 09:00 -> Monday 17:00; now after stop.
        plan = make_plan(
            recurrence=vacation.RECURRENCE_WEEKLY,
            start_weekday=0,
            start_time=time(9, 0),
            stop_weekday=0,
            stop_time=time(17, 0),
        )
        now = datetime(2026, 1, 5, 18, 0)  # Monday, after stop
        window = occurrence_window(plan, now)
        assert window == (datetime(2026, 1, 12, 9, 0), datetime(2026, 1, 12, 17, 0))

    def test_window_starts_later_this_week(self):
        plan = make_plan(**self.WEEKLY_KW)
        now = datetime(2026, 1, 5, 10, 0)  # Monday, before this week's Friday start
        window = occurrence_window(plan, now)
        assert window == (datetime(2026, 1, 9, 18, 0), datetime(2026, 1, 12, 7, 0))
        # Feeding this into resolve_plan should read as scheduled, not active.
        result = resolve_plan(now, plan, normal_target_c=21.0, rise_hours=1.0)
        assert result.phase == holiday.HOLIDAY_PHASE_SCHEDULED

    def test_same_weekday_same_day_window_currently_active(self):
        plan = make_plan(
            recurrence=vacation.RECURRENCE_WEEKLY,
            start_weekday=0,
            start_time=time(9, 0),
            stop_weekday=0,
            stop_time=time(17, 0),
        )
        now = datetime(2026, 1, 5, 13, 0)  # Monday, mid-window
        window = occurrence_window(plan, now)
        assert window == (datetime(2026, 1, 5, 9, 0), datetime(2026, 1, 5, 17, 0))

    def test_window_crossing_week_boundary_fri_to_mon(self):
        plan = make_plan(**self.WEEKLY_KW)
        now = datetime(2026, 1, 4, 23, 0)  # Sunday night, inside Fri->Mon window
        window = occurrence_window(plan, now)
        assert window == (datetime(2026, 1, 2, 18, 0), datetime(2026, 1, 5, 7, 0))

    def test_missing_fields_is_none(self):
        plan = make_plan(
            recurrence=vacation.RECURRENCE_WEEKLY,
            start_weekday=4,
            start_time=time(18, 0),
            stop_weekday=None,
            stop_time=None,
        )
        assert occurrence_window(plan, datetime(2026, 1, 3, 10, 0)) is None


class TestYearlyOccurrence:
    def test_non_wrapping_window_before_during_after(self):
        plan = make_plan(
            recurrence=vacation.RECURRENCE_YEARLY,
            start_month=7,
            start_day=1,
            end_month=7,
            end_day=14,
        )
        before = occurrence_window(plan, datetime(2026, 6, 1, 0, 0))
        assert before == (datetime(2026, 7, 1, 0, 0), datetime(2026, 7, 14, 15, 0))

        during = occurrence_window(plan, datetime(2026, 7, 7, 0, 0))
        assert during == (datetime(2026, 7, 1, 0, 0), datetime(2026, 7, 14, 15, 0))

        after = occurrence_window(plan, datetime(2026, 8, 1, 0, 0))
        assert after == (datetime(2027, 7, 1, 0, 0), datetime(2027, 7, 14, 15, 0))

    def test_wrapping_window_over_new_year(self):
        plan = make_plan(
            recurrence=vacation.RECURRENCE_YEARLY,
            start_month=12,
            start_day=20,
            end_month=1,
            end_day=10,
        )
        # December, before start.
        before = occurrence_window(plan, datetime(2026, 12, 10, 0, 0))
        assert before == (datetime(2026, 12, 20, 0, 0), datetime(2027, 1, 10, 15, 0))

        # December, during (after start, before Dec 31).
        during_dec = occurrence_window(plan, datetime(2026, 12, 25, 0, 0))
        assert during_dec == (datetime(2026, 12, 20, 0, 0), datetime(2027, 1, 10, 15, 0))

        # January, during (after Jan 1, before Jan 10) — the tricky one:
        # the active occurrence started in December of the *previous* year.
        during_jan = occurrence_window(plan, datetime(2026, 1, 5, 0, 0))
        assert during_jan == (datetime(2025, 12, 20, 0, 0), datetime(2026, 1, 10, 15, 0))

        # January, after end but before next December's occurrence.
        after_jan = occurrence_window(plan, datetime(2026, 2, 1, 0, 0))
        assert after_jan == (datetime(2026, 12, 20, 0, 0), datetime(2027, 1, 10, 15, 0))

    def test_missing_or_invalid_calendar_fields_is_none(self):
        missing = make_plan(
            recurrence=vacation.RECURRENCE_YEARLY,
            start_month=7,
            start_day=1,
            end_month=None,
            end_day=None,
        )
        assert occurrence_window(missing, datetime(2026, 1, 1, 0, 0)) is None

        invalid = make_plan(
            recurrence=vacation.RECURRENCE_YEARLY,
            start_month=2,
            start_day=30,  # not a real calendar day
            end_month=3,
            end_day=1,
        )
        assert occurrence_window(invalid, datetime(2026, 1, 1, 0, 0)) is None


class TestResolveVacationPriority:
    def _active_plan(self, plan_id, start_offset_hours=-1, end_offset_hours=5):
        # A `once` plan already in its setback plateau at now=2026-01-05 12:00.
        return make_plan(
            id=plan_id,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 20),
            min_temp_c=17.0,
        )

    def test_two_active_plans_first_in_list_wins(self):
        now = datetime(2026, 1, 6, 0, 0)  # both plateaued
        plan_a = self._active_plan("a")
        plan_b = self._active_plan("b")
        result, active_id = resolve_vacation(
            now, True, [plan_a, plan_b], normal_target_c=21.0, rise_hours=1.0
        )
        assert active_id == "a"
        assert result.phase == holiday.HOLIDAY_PHASE_SETBACK

    def test_active_beats_scheduled_regardless_of_list_order(self):
        now = datetime(2026, 1, 6, 0, 0)
        scheduled_plan = make_plan(
            id="sched",
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 10),
        )
        active_plan = self._active_plan("active")
        # Scheduled plan listed first, active plan second.
        result, active_id = resolve_vacation(
            now, True, [scheduled_plan, active_plan], normal_target_c=21.0, rise_hours=1.0
        )
        assert active_id == "active"
        assert result.phase == holiday.HOLIDAY_PHASE_SETBACK

    def test_two_scheduled_soonest_start_wins_regardless_of_list_order(self):
        now = datetime(2026, 1, 1, 0, 0)
        later = make_plan(
            id="later", start_date=date(2026, 3, 1), end_date=date(2026, 3, 10)
        )
        sooner = make_plan(
            id="sooner", start_date=date(2026, 2, 1), end_date=date(2026, 2, 10)
        )
        # "later" listed first, "sooner" listed second — sooner should still win.
        result, active_id = resolve_vacation(
            now, True, [later, sooner], normal_target_c=21.0, rise_hours=1.0
        )
        assert active_id == "sooner"
        assert result.phase == holiday.HOLIDAY_PHASE_SCHEDULED

    def test_master_switch_off_is_inactive_regardless_of_plan_states(self):
        now = datetime(2026, 1, 6, 0, 0)
        plan = self._active_plan("a")
        result, active_id = resolve_vacation(
            now, False, [plan], normal_target_c=21.0, rise_hours=1.0
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
        assert result.target_c == 21.0
        assert active_id is None

    def test_empty_plan_list_is_inactive(self):
        now = datetime(2026, 1, 6, 0, 0)
        result, active_id = resolve_vacation(
            now, True, [], normal_target_c=21.0, rise_hours=1.0
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
        assert active_id is None

    def test_no_active_or_scheduled_plans_is_inactive(self):
        now = datetime(2026, 2, 1, 0, 0)
        done_plan = make_plan(
            id="done", start_date=date(2026, 1, 1), end_date=date(2026, 1, 5)
        )
        invalid_plan = make_plan(id="bad", start_date=None, end_date=None)
        result, active_id = resolve_vacation(
            now, True, [done_plan, invalid_plan], normal_target_c=21.0, rise_hours=1.0
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
        assert active_id is None


VacationReturnRamp = vacation.VacationReturnRamp
start_return_ramp = vacation.start_return_ramp
resolve_vacation_with_return_ramp = vacation.resolve_vacation_with_return_ramp


class TestReturnRamp:
    """Disarming the master switch mid-setback/mid-ramp used to snap the
    target straight back to `normal_target_c` in one cycle. These cover the
    fix: a ramp with the same pacing as a plan's own scheduled return."""

    def test_start_return_ramp_none_when_already_at_or_above_normal(self):
        assert (
            start_return_ramp(
                datetime(2026, 1, 6, 0, 0),
                from_target_c=21.0,
                normal_target_c=21.0,
                rise_hours=1.0,
            )
            is None
        )
        assert (
            start_return_ramp(
                datetime(2026, 1, 6, 0, 0),
                from_target_c=22.0,
                normal_target_c=21.0,
                rise_hours=1.0,
            )
            is None
        )

    def test_start_return_ramp_matches_holiday_pacing(self):
        now = datetime(2026, 1, 6, 0, 0)
        ramp = start_return_ramp(
            now, from_target_c=17.0, normal_target_c=21.0, rise_hours=1.0
        )
        assert ramp is not None
        assert ramp.from_target_c == 17.0
        assert ramp.started_at == now
        assert ramp.hours_needed == holiday.ramp_hours_needed(4.0, 1.0)

    def test_armed_ignores_and_clears_any_pending_ramp(self):
        now = datetime(2026, 1, 6, 0, 0)
        plan = self._active_plan_helper("a")
        stale_ramp = VacationReturnRamp(
            from_target_c=17.0, started_at=now, hours_needed=4.0
        )
        result, active_id, next_ramp = resolve_vacation_with_return_ramp(
            now, True, [plan], normal_target_c=21.0, rise_hours=1.0, return_ramp=stale_ramp
        )
        assert active_id == "a"
        assert result.phase == holiday.HOLIDAY_PHASE_SETBACK
        assert next_ramp is None

    def test_disarm_with_no_ramp_is_instant_inactive(self):
        now = datetime(2026, 1, 6, 0, 0)
        result, active_id, next_ramp = resolve_vacation_with_return_ramp(
            now, False, [], normal_target_c=21.0, rise_hours=1.0, return_ramp=None
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
        assert result.target_c == 21.0
        assert active_id is None
        assert next_ramp is None

    def test_disarm_with_ramp_reports_ramping_and_interpolated_target(self):
        started_at = datetime(2026, 1, 6, 0, 0)
        ramp = start_return_ramp(
            started_at, from_target_c=17.0, normal_target_c=21.0, rise_hours=1.0
        )
        assert ramp is not None
        midpoint = started_at + timedelta(hours=ramp.hours_needed / 2.0)
        result, active_id, next_ramp = resolve_vacation_with_return_ramp(
            midpoint, False, [], normal_target_c=21.0, rise_hours=1.0, return_ramp=ramp
        )
        assert result.phase == holiday.HOLIDAY_PHASE_RAMPING
        assert result.target_c == pytest.approx(19.0)
        assert active_id is None
        assert next_ramp == ramp  # unchanged: caller re-persists the same ramp

    def test_ramp_completes_to_plain_inactive_and_clears_state(self):
        started_at = datetime(2026, 1, 6, 0, 0)
        ramp = start_return_ramp(
            started_at, from_target_c=17.0, normal_target_c=21.0, rise_hours=1.0
        )
        assert ramp is not None
        after_finish = started_at + timedelta(hours=ramp.hours_needed + 1.0)
        result, active_id, next_ramp = resolve_vacation_with_return_ramp(
            after_finish, False, [], normal_target_c=21.0, rise_hours=1.0, return_ramp=ramp
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
        assert result.target_c == 21.0
        assert active_id is None
        assert next_ramp is None

    def test_normal_target_lowered_mid_ramp_below_from_target_ends_ramp(self):
        started_at = datetime(2026, 1, 6, 0, 0)
        ramp = start_return_ramp(
            started_at, from_target_c=17.0, normal_target_c=21.0, rise_hours=1.0
        )
        assert ramp is not None
        midpoint = started_at + timedelta(hours=ramp.hours_needed / 2.0)
        # Occupant drops the normal target to/below where the ramp started.
        result, active_id, next_ramp = resolve_vacation_with_return_ramp(
            midpoint, False, [], normal_target_c=16.0, rise_hours=1.0, return_ramp=ramp
        )
        assert result.phase == holiday.HOLIDAY_PHASE_INACTIVE
        assert result.target_c == 16.0
        assert next_ramp is None

    @staticmethod
    def _active_plan_helper(plan_id):
        return make_plan(
            id=plan_id,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 20),
            min_temp_c=17.0,
        )


class TestVacationRecovery:
    """Post-setpoint catch-up window — rooms warm, or deadline."""

    def test_should_start_after_ramping_when_rooms_are_cold(self):
        assert (
            vacation.should_start_recovery(
                prev_phase=holiday.HOLIDAY_PHASE_RAMPING,
                result_phase=holiday.HOLIDAY_PHASE_INACTIVE,
                normal_target_c=20.5,
                indoor_temp_c=17.7,
                indoor_ok=True,
            )
            is True
        )

    def test_should_not_start_when_rooms_already_warm(self):
        assert (
            vacation.should_start_recovery(
                prev_phase=holiday.HOLIDAY_PHASE_RAMPING,
                result_phase=holiday.HOLIDAY_PHASE_INACTIVE,
                normal_target_c=20.5,
                indoor_temp_c=20.4,
                indoor_ok=True,
            )
            is False
        )

    def test_should_not_start_when_a_new_setback_won(self):
        assert (
            vacation.should_start_recovery(
                prev_phase=holiday.HOLIDAY_PHASE_RAMPING,
                result_phase=holiday.HOLIDAY_PHASE_SETBACK,
                normal_target_c=20.5,
                indoor_temp_c=17.0,
                indoor_ok=True,
            )
            is False
        )

    def test_should_start_when_indoor_unknown(self):
        assert (
            vacation.should_start_recovery(
                prev_phase=holiday.HOLIDAY_PHASE_SETBACK,
                result_phase=holiday.HOLIDAY_PHASE_INACTIVE,
                normal_target_c=20.5,
                indoor_temp_c=None,
                indoor_ok=False,
            )
            is True
        )

    def test_resolve_reports_recovering_with_normal_target(self):
        now = datetime(2026, 1, 10, 15, 0)
        recovery = vacation.start_recovery(now)
        assert recovery.deadline_at == now + timedelta(
            hours=vacation.RECOVERY_DEADLINE_HOURS
        )
        overlay, kept = vacation.resolve_recovery(
            now=now + timedelta(hours=1),
            recovery=recovery,
            normal_target_c=20.5,
            indoor_temp_c=17.7,
            indoor_ok=True,
        )
        assert kept is recovery
        assert overlay is not None
        assert overlay.phase == holiday.HOLIDAY_PHASE_RECOVERING
        assert overlay.target_c == 20.5
        assert "17.7" in overlay.reason

    def test_resolve_clears_when_rooms_reach_target(self):
        now = datetime(2026, 1, 10, 15, 0)
        recovery = vacation.start_recovery(now)
        overlay, kept = vacation.resolve_recovery(
            now=now + timedelta(hours=2),
            recovery=recovery,
            normal_target_c=20.5,
            indoor_temp_c=20.3,
            indoor_ok=True,
        )
        assert overlay is None
        assert kept is None

    def test_resolve_clears_at_deadline_even_if_still_cold(self):
        now = datetime(2026, 1, 10, 15, 0)
        recovery = vacation.start_recovery(now)
        overlay, kept = vacation.resolve_recovery(
            now=recovery.deadline_at,
            recovery=recovery,
            normal_target_c=20.5,
            indoor_temp_c=18.0,
            indoor_ok=True,
        )
        assert overlay is None
        assert kept is None

    def test_recovering_is_an_active_phase_for_catchup(self):
        assert holiday.HOLIDAY_PHASE_RECOVERING in vacation.ACTIVE_PHASES


class TestFindOverlap:
    def test_two_enabled_overlapping_plans_returns_ids(self):
        now = datetime(2026, 1, 1, 0, 0)
        plan_a = make_plan(id="a", start_date=date(2026, 1, 5), end_date=date(2026, 1, 15))
        plan_b = make_plan(id="b", start_date=date(2026, 1, 10), end_date=date(2026, 1, 20))
        assert find_overlap([plan_a, plan_b], now) == ("a", "b")

    def test_overlapping_but_one_disabled_is_none(self):
        now = datetime(2026, 1, 1, 0, 0)
        plan_a = make_plan(id="a", start_date=date(2026, 1, 5), end_date=date(2026, 1, 15))
        plan_b = make_plan(
            id="b",
            enabled=False,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 20),
        )
        assert find_overlap([plan_a, plan_b], now) is None

    def test_non_overlapping_plans_is_none(self):
        now = datetime(2026, 1, 1, 0, 0)
        plan_a = make_plan(id="a", start_date=date(2026, 1, 5), end_date=date(2026, 1, 10))
        plan_b = make_plan(id="b", start_date=date(2026, 1, 15), end_date=date(2026, 1, 20))
        assert find_overlap([plan_a, plan_b], now) is None

    def test_invalid_plan_alongside_valid_plan_is_not_a_false_overlap(self):
        now = datetime(2026, 1, 1, 0, 0)
        valid_plan = make_plan(
            id="valid", start_date=date(2026, 1, 5), end_date=date(2026, 1, 15)
        )
        invalid_plan = make_plan(id="invalid", start_date=None, end_date=None)
        assert find_overlap([valid_plan, invalid_plan], now) is None

    def test_genuinely_back_to_back_windows_do_not_overlap(self):
        # `once` plans always return at HOLIDAY_RETURN_TIME (15:00) and start
        # at midnight, so a same-day handoff (b starts the day a ends) is
        # NOT actually back-to-back — a's return_at (15:00) is after b's
        # start_at (00:00), a genuine overlap. Give b's start a day of
        # margin instead so the two ranges are truly disjoint and adjacent
        # in spirit, confirming find_overlap doesn't false-positive on
        # closely-spaced non-overlapping windows.
        now = datetime(2026, 1, 1, 0, 0)
        plan_a = make_plan(id="a", start_date=date(2026, 1, 5), end_date=date(2026, 1, 10))
        plan_b = make_plan(id="b", start_date=date(2026, 1, 11), end_date=date(2026, 1, 20))
        window_a = occurrence_window(plan_a, now)
        window_b = occurrence_window(plan_b, now)
        assert window_a[1] < window_b[0]
        assert find_overlap([plan_a, plan_b], now) is None


class TestFindOverlapCrossRecurrenceKind:
    """`find_overlap` must be correct for pairings whose recurrence kinds
    differ, where the nearest-to-`now` occurrence isn't necessarily the
    occurrence that actually collides — see `_plans_overlap()`'s docstring.
    """

    WEEKEND_CABIN = dict(
        recurrence=vacation.RECURRENCE_WEEKLY,
        start_weekday=4,  # Friday
        start_time=time(18, 0),
        stop_weekday=0,  # Monday
        stop_time=time(7, 0),
    )

    def test_once_plan_months_out_overlaps_weekly_plan_on_a_coinciding_weekend(self):
        # now is 2026-01-01; the once plan is a Sat/Sun in April, three
        # months out, landing inside that week's Fri18:00->Mon07:00 cabin
        # window (2026-04-03 18:00 -> 2026-04-06 07:00).
        now = datetime(2026, 1, 1, 0, 0)
        once_plan = make_plan(
            id="once",
            recurrence=vacation.RECURRENCE_ONCE,
            start_date=date(2026, 4, 4),
            end_date=date(2026, 4, 5),
        )
        weekly_plan = make_plan(id="weekly", **self.WEEKEND_CABIN)
        assert find_overlap([once_plan, weekly_plan], now) == ("once", "weekly")
        # Order shouldn't matter for detection (only which id comes first
        # in the returned tuple, which follows list order).
        assert find_overlap([weekly_plan, once_plan], now) == ("weekly", "once")

    def test_once_plan_months_out_with_no_coincidence_is_not_an_overlap(self):
        # Same weekly cabin plan, but the once plan is a weekday (Tue/Wed)
        # in the same month, which never touches the Fri->Mon window.
        now = datetime(2026, 1, 1, 0, 0)
        once_plan = make_plan(
            id="once",
            recurrence=vacation.RECURRENCE_ONCE,
            start_date=date(2026, 4, 7),
            end_date=date(2026, 4, 8),
        )
        weekly_plan = make_plan(id="weekly", **self.WEEKEND_CABIN)
        assert find_overlap([once_plan, weekly_plan], now) is None

    def test_once_plan_overlaps_yearly_plan_across_a_multi_year_gap(self):
        # now is 2026; the once plan is in 2029, well past the now.year+1
        # window occurrence_window()'s own nearest-occurrence search would
        # find on its own.
        now = datetime(2026, 1, 1, 0, 0)
        once_plan = make_plan(
            id="once",
            recurrence=vacation.RECURRENCE_ONCE,
            start_date=date(2029, 7, 5),
            end_date=date(2029, 7, 8),
        )
        yearly_plan = make_plan(
            id="yearly",
            recurrence=vacation.RECURRENCE_YEARLY,
            start_month=7,
            start_day=1,
            end_month=7,
            end_day=14,
        )
        assert find_overlap([once_plan, yearly_plan], now) == ("once", "yearly")

    def test_once_plan_overlaps_new_year_wrapping_yearly_plan(self):
        # Yearly plan wraps Dec 20 -> Jan 10. The once plan lands in the
        # January portion of the *following* year's occurrence — the same
        # tricky wrap edge covered for occurrence_window() itself, now
        # exercised through find_overlap's cross-kind path.
        now = datetime(2026, 1, 1, 0, 0)
        once_plan = make_plan(
            id="once",
            recurrence=vacation.RECURRENCE_ONCE,
            start_date=date(2027, 1, 3),
            end_date=date(2027, 1, 5),
        )
        yearly_plan = make_plan(
            id="yearly",
            recurrence=vacation.RECURRENCE_YEARLY,
            start_month=12,
            start_day=20,
            end_month=1,
            end_day=10,
        )
        assert find_overlap([once_plan, yearly_plan], now) == ("once", "yearly")

    def test_once_plan_not_touching_new_year_wrapping_yearly_plan_is_not_an_overlap(self):
        # Sanity check alongside the wrap case above: a once plan safely
        # outside the wrapped window (e.g. mid-February) must not false-
        # positive.
        now = datetime(2026, 1, 1, 0, 0)
        once_plan = make_plan(
            id="once",
            recurrence=vacation.RECURRENCE_ONCE,
            start_date=date(2027, 2, 10),
            end_date=date(2027, 2, 12),
        )
        yearly_plan = make_plan(
            id="yearly",
            recurrence=vacation.RECURRENCE_YEARLY,
            start_month=12,
            start_day=20,
            end_month=1,
            end_day=10,
        )
        assert find_overlap([once_plan, yearly_plan], now) is None


class TestHolidayStillGreenAfterRefactor:
    """Smoke check that `holiday.resolve()` is unaffected by the
    `resolve_window()` extraction — the authoritative check is running
    `tests/test_holiday.py` itself, which this file's presence doesn't
    replace."""

    def test_resolve_and_resolve_window_agree_on_a_shared_scenario(self):
        start_date = date(2026, 1, 5)
        end_date = date(2026, 1, 10)
        via_resolve = holiday.resolve(
            now=datetime(2026, 1, 6, 0, 0),
            armed=True,
            start_date=start_date,
            end_date=end_date,
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.0,
        )
        via_window = holiday.resolve_window(
            now=datetime(2026, 1, 6, 0, 0),
            start_at=datetime.combine(start_date, time.min),
            return_at=datetime.combine(end_date, holiday.HOLIDAY_RETURN_TIME),
            normal_target_c=21.0,
            holiday_target_c=17.0,
            rise_hours=1.0,
        )
        assert via_resolve.phase == via_window.phase
        assert via_resolve.target_c == via_window.target_c
        assert via_resolve.start_at == via_window.start_at
        assert via_resolve.return_at == via_window.return_at


class TestPersistence:
    """`serialize_plan`/`deserialize_plan` round-trip and defensive parsing —
    the shape `config_flow.py`'s options-flow CRUD (Phase 2) reads/writes
    from `entry.options`."""

    def test_round_trips_a_once_plan(self):
        plan = make_plan(
            recurrence=vacation.RECURRENCE_ONCE,
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 27),
        )
        raw = vacation.serialize_plan(plan)
        assert vacation.deserialize_plan(raw) == plan

    def test_round_trips_a_weekly_plan(self):
        plan = make_plan(
            recurrence=vacation.RECURRENCE_WEEKLY,
            start_weekday=4,
            start_time=time(18, 0),
            stop_weekday=0,
            stop_time=time(7, 0),
        )
        raw = vacation.serialize_plan(plan)
        assert vacation.deserialize_plan(raw) == plan

    def test_round_trips_a_yearly_plan(self):
        plan = make_plan(
            recurrence=vacation.RECURRENCE_YEARLY,
            start_month=7,
            start_day=1,
            end_month=7,
            end_day=14,
        )
        raw = vacation.serialize_plan(plan)
        assert vacation.deserialize_plan(raw) == plan

    def test_serialized_plan_is_json_safe(self):
        import json

        plan = make_plan(
            recurrence=vacation.RECURRENCE_WEEKLY,
            start_weekday=4,
            start_time=time(18, 0),
            stop_weekday=0,
            stop_time=time(7, 0),
        )
        # Raises if anything in the payload isn't JSON-serializable.
        json.dumps(vacation.serialize_plans([plan]))

    def test_deserialize_plan_rejects_non_dict(self):
        assert vacation.deserialize_plan("not a plan") is None
        assert vacation.deserialize_plan(None) is None
        assert vacation.deserialize_plan([1, 2, 3]) is None

    def test_deserialize_plan_rejects_missing_required_field(self):
        raw = vacation.serialize_plan(make_plan())
        del raw["min_temp_c"]
        assert vacation.deserialize_plan(raw) is None

    def test_deserialize_plan_rejects_unknown_recurrence(self):
        raw = vacation.serialize_plan(make_plan())
        raw["recurrence"] = "monthly"
        assert vacation.deserialize_plan(raw) is None

    def test_deserialize_plan_rejects_non_finite_min_temp(self):
        raw = vacation.serialize_plan(make_plan())
        raw["min_temp_c"] = float("nan")
        assert vacation.deserialize_plan(raw) is None

    def test_deserialize_plan_rejects_malformed_int_field_type(self):
        raw = vacation.serialize_plan(
            make_plan(recurrence=vacation.RECURRENCE_YEARLY, start_month=7, start_day=1,
                      end_month=7, end_day=14)
        )
        raw["start_month"] = "july"
        assert vacation.deserialize_plan(raw) is None

    def test_deserialize_plan_rejects_unparsable_date_string(self):
        raw = vacation.serialize_plan(
            make_plan(start_date=date(2026, 1, 1), end_date=date(2026, 1, 10))
        )
        raw["start_date"] = "not-a-date"
        # Malformed date degrades to None on the field, not a raised error —
        # occurrence_window() then reports the plan as invalid, same as a
        # plan that never had the field set.
        plan = vacation.deserialize_plan(raw)
        assert plan is not None
        assert plan.start_date is None

    def test_deserialize_plans_drops_only_the_corrupt_entry(self):
        good = vacation.serialize_plan(make_plan(id="good"))
        bad = {"id": "bad", "name": "Bad plan"}  # missing recurrence/min_temp_c
        plans = vacation.deserialize_plans([good, bad])
        assert [p.id for p in plans] == ["good"]

    def test_deserialize_plans_handles_non_list_input(self):
        assert vacation.deserialize_plans(None) == []
        assert vacation.deserialize_plans({"not": "a list"}) == []

    def test_serialize_plans_round_trips_a_list(self):
        plans = [
            make_plan(id="p1", name="First"),
            make_plan(id="p2", name="Second", enabled=False),
        ]
        assert vacation.deserialize_plans(vacation.serialize_plans(plans)) == plans
