"""Unit tests for the offset learner.

The most important behaviours to pin down are the ones that make integral
action safe rather than the ones that make it work: it must never accumulate
error the plant has not had a chance to respond to, and it must never be able
to move the heat pump further than its earned authority.
"""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

learner = load("learner")

LearnerInputs = learner.LearnerInputs
LearnerState = learner.LearnerState
OutdoorBin = learner.OutdoorBin


def make_inputs(**overrides) -> LearnerInputs:
    defaults = dict(
        now_s=1000.0,
        dt_hours=0.25,
        indoor_temp_c=21.0,
        indoor_data_available=True,
        target_c=21.0,
        outdoor_temp_c=0.0,
        heating_hard_limit_engaged=False,
        is_active=True,
        price_braking=False,
        rise_hours=1.0,
    )
    defaults.update(overrides)
    return LearnerInputs(**defaults)


def mature_state(**overrides) -> LearnerState:
    """A state past the authority ramp, so clamps under test are the real ones."""
    defaults = dict(
        total_samples=learner.AUTHORITY_RAMP_SAMPLES,
        prev_target_c=21.0,
        prev_indoor_c=21.0,
    )
    defaults.update(overrides)
    return replace(learner.initial_state(), **defaults)


class TestBinning:
    def test_index_covers_the_whole_real_line(self):
        assert learner.bin_index_for(-100.0) == 0
        assert learner.bin_index_for(100.0) == learner.N_BINS - 1
        assert 0 <= learner.bin_index_for(0.0) < learner.N_BINS

    def test_edges_are_lower_inclusive(self):
        for i, edge in enumerate(learner.BIN_EDGES):
            assert learner.bin_index_for(edge - 0.01) == i
            assert learner.bin_index_for(edge) == i + 1

    def test_coldest_bin_is_open_ended(self):
        """Deep cold shares one wide band on purpose: it is the slowest to fill
        and the most consequential, so one bin with real evidence beats three
        that never accumulate any."""
        assert learner.bin_index_for(-15.0) == 1
        assert learner.bin_index_for(-40.0) == 0
        assert "below" in learner.bin_label(0)
        assert "above" in learner.bin_label(learner.N_BINS - 1)

    def test_hovering_on_an_edge_holds_the_previous_bin(self):
        """A reading that sits right on an edge must not flip the occupied bin
        back and forth every cycle — that splits samples across two bins and
        keeps restarting the baseline dwell timer."""
        edge = learner.BIN_EDGES[2]  # -5.0
        lower_index = 2
        upper_index = 3
        assert learner.bin_index_for(edge, lower_index) == lower_index
        assert learner.bin_index_for(edge + 0.01, lower_index) == lower_index
        assert learner.bin_index_for(edge - 0.01, upper_index) == upper_index

    def test_clearing_the_edge_by_the_hysteresis_margin_switches_bins(self):
        edge = learner.BIN_EDGES[2]  # -5.0
        lower_index = 2
        upper_index = 3
        assert (
            learner.bin_index_for(edge + learner.BIN_HYSTERESIS_C, lower_index)
            == upper_index
        )
        assert (
            learner.bin_index_for(
                edge - learner.BIN_HYSTERESIS_C, upper_index
            )
            == lower_index
        )

    def test_a_large_jump_switches_immediately_without_hysteresis(self):
        """Hysteresis only guards a single edge — a real, multi-bin swing in
        outdoor temperature is not the noise-on-an-edge case it exists for."""
        assert learner.bin_index_for(20.0, 0) == learner.N_BINS - 1

    def test_no_previous_index_gives_the_raw_answer(self):
        edge = learner.BIN_EDGES[2]
        assert learner.bin_index_for(edge) == 3
        assert learner.bin_index_for(edge - 0.01) == 2


class TestAuthorityRamp:
    def test_starts_minimal_and_ends_maximal(self):
        assert learner.authority_for(0) == learner.MIN_AUTHORITY_C
        assert learner.authority_for(learner.AUTHORITY_RAMP_SAMPLES) == (
            learner.MAX_AUTHORITY_C
        )

    def test_is_monotonic_and_bounded(self):
        values = [learner.authority_for(n) for n in range(0, 1000, 25)]
        assert values == sorted(values)
        assert max(values) == learner.MAX_AUTHORITY_C

    def test_fresh_install_can_barely_move_the_pump(self):
        """The whole argument for shipping active by default."""
        state, result = learner.step(
            learner.initial_state(), make_inputs(indoor_temp_c=5.0)
        )
        assert abs(result.offset_c) <= learner.MIN_AUTHORITY_C + 1e-9


class TestIntegration:
    def test_too_cold_drives_the_offset_down(self):
        """Spoofing colder is what asks the pump's own curve for more heat."""
        _, result = learner.step(
            mature_state(), make_inputs(indoor_temp_c=20.0, target_c=21.0)
        )
        assert result.offset_c < 0
        assert result.hold_offset_c < 0

    def test_too_warm_drives_the_offset_up(self):
        _, result = learner.step(
            mature_state(), make_inputs(indoor_temp_c=22.0, target_c=21.0)
        )
        assert result.offset_c > 0
        assert result.hold_offset_c > 0

    def test_on_target_barely_moves(self):
        _, result = learner.step(mature_state(), make_inputs(indoor_temp_c=21.0))
        assert result.hold_offset_c == pytest.approx(0.0, abs=1e-9)

    def test_per_cycle_movement_is_rate_limited(self):
        """A bad dt or a wild reading must not become a step change at the pump."""
        _, result = learner.step(
            mature_state(),
            make_inputs(indoor_temp_c=-50.0, dt_hours=10.0),
        )
        assert abs(result.hold_offset_c) <= learner.MAX_STEP_PER_CYCLE_C + 1e-9

    def test_settling_time_follows_the_measured_lag(self):
        """The stability margin against dead time — the reason lag.py exists."""
        _, fast = learner.step(mature_state(), make_inputs(rise_hours=1.0))
        _, slow = learner.step(mature_state(), make_inputs(rise_hours=4.0))
        assert slow.tau_i_h > fast.tau_i_h
        assert fast.tau_i_h == pytest.approx(1.0 * learner.TAU_I_LAG_MULTIPLE)

    def test_settling_time_is_bounded_both_ways(self):
        _, tiny = learner.step(mature_state(), make_inputs(rise_hours=0.01))
        _, huge = learner.step(mature_state(), make_inputs(rise_hours=100.0))
        assert tiny.tau_i_h == learner.TAU_I_MIN_H
        assert huge.tau_i_h == learner.TAU_I_MAX_H

    def test_converges_on_the_offset_a_house_actually_needs(self):
        """The end-to-end claim: a house whose pump curve is 2 degC short should
        be taught a -2 degC offset, with nobody typing a gain anywhere."""
        needed = -2.0
        state = learner.initial_state()
        indoor = 19.0
        now = 0.0
        dt = 0.25
        offset = 0.0
        for _ in range(1500):
            # Plant: indoor relaxes toward the equilibrium the current offset
            # buys, where offset == needed gives exactly the target.
            equilibrium = 19.0 + (offset / needed) * 2.0
            indoor += (equilibrium - indoor) * 0.2
            now += dt * 3600.0
            state, result = learner.step(
                state,
                make_inputs(
                    now_s=now, dt_hours=dt, indoor_temp_c=indoor, target_c=21.0
                ),
            )
            offset = result.offset_c
        assert result.hold_offset_c == pytest.approx(needed, abs=0.25)
        assert indoor == pytest.approx(21.0, abs=0.2)

    def test_proportional_term_is_bounded(self):
        _, result = learner.step(
            mature_state(), make_inputs(indoor_temp_c=-100.0)
        )
        assert abs(result.proportional_c) <= learner.KP_MAX_C + 1e-9


class TestFreezing:
    """Every entry restates one rule: only accumulate error the plant has had a
    chance to respond to, and only while the output is actually driving."""

    @pytest.mark.parametrize(
        "override,fragment",
        [
            ({"is_active": False}, "compensation is off"),
            ({"indoor_data_available": False, "indoor_temp_c": None}, "unavailable"),
            ({"heating_hard_limit_engaged": True}, "hard limit"),
            ({"price_braking": True}, "price"),
            ({"dt_hours": 0.0}, "elapsed"),
            ({"indoor_sensor_set_changed": True}, "contributing indoor sensors changed"),
        ],
    )
    def test_learning_pauses(self, override, fragment):
        before = mature_state(bins=tuple(
            OutdoorBin(hold_offset_c=-1.0, samples=5) for _ in range(learner.N_BINS)
        ))
        after, result = learner.step(
            before, make_inputs(**{"indoor_temp_c": 18.0, **override})
        )
        assert result.frozen
        assert fragment in result.freeze_reason
        assert after.total_samples == before.total_samples
        assert after.bins[result.bin_index].hold_offset_c == pytest.approx(-1.0)

    def test_indoor_sensor_set_unchanged_is_a_no_op(self):
        """`indoor_sensor_set_changed` defaults False, so every pre-existing
        call site — and every cycle with a stable sensor set — learns exactly
        as before. Regression guard for the composition guard added in PR2."""
        before = mature_state(bins=tuple(
            OutdoorBin(hold_offset_c=-1.0, samples=5) for _ in range(learner.N_BINS)
        ))
        after, result = learner.step(
            before, make_inputs(indoor_temp_c=18.0, indoor_sensor_set_changed=False)
        )
        assert not result.frozen

    def test_price_braking_does_not_get_integrated_away(self):
        """Price compensation deliberately holds the house below target. If the
        learner treated that as error it would wind up fighting the very
        excursion it was asked for."""
        state = mature_state()
        after, result = learner.step(
            state, make_inputs(indoor_temp_c=19.5, target_c=21.0, price_braking=True)
        )
        assert result.frozen
        assert after.bins[result.bin_index].hold_offset_c == pytest.approx(0.0)

    def test_target_change_starts_a_holdoff(self):
        state = mature_state(prev_target_c=21.0)
        after, result = learner.step(
            state, make_inputs(target_c=23.0, indoor_temp_c=21.0, rise_hours=2.0)
        )
        assert after.holdoff_until_s == pytest.approx(1000.0 + 2.0 * 3600.0)
        assert result.frozen

    def test_holdoff_expires(self):
        state = mature_state(holdoff_until_s=2000.0)
        _, during = learner.step(state, make_inputs(now_s=1500.0, indoor_temp_c=20.0))
        _, after = learner.step(state, make_inputs(now_s=2500.0, indoor_temp_c=20.0))
        assert during.frozen
        assert not after.frozen

    def test_frozen_still_reports_an_offset_to_preview(self):
        state = mature_state(
            bins=tuple(
                OutdoorBin(hold_offset_c=-1.5, samples=9) for _ in range(learner.N_BINS)
            )
        )
        _, result = learner.step(state, make_inputs(is_active=False))
        assert result.frozen
        assert result.hold_offset_c == pytest.approx(-1.5)


class TestSaturation:
    def _saturating(self, state, steps, **overrides):
        result = None
        for i in range(steps):
            state, result = learner.step(
                state,
                make_inputs(
                    now_s=1000.0 + i * 900.0,
                    indoor_temp_c=19.0,
                    target_c=21.0,
                    **overrides,
                ),
            )
        return state, result

    def test_pinned_and_not_closing_reports_saturated(self):
        state = mature_state(
            bins=tuple(
                OutdoorBin(hold_offset_c=-learner.MAX_AUTHORITY_C, samples=50)
                for _ in range(learner.N_BINS)
            ),
            prev_indoor_c=19.0,
        )
        _, result = self._saturating(state, learner.SATURATION_STREAK + 1)
        assert result.saturated
        assert result.frozen is False or result.freeze_reason is None
        assert "capacity" in result.reason

    def test_authority_clamp_alone_is_not_treated_as_capacity(self):
        """A fresh install pinned at its 1 degC safety clamp has learned nothing
        about the pump — capping the band there would be permanent damage."""
        state = learner.initial_state()
        state, result = self._saturating(state, learner.SATURATION_STREAK + 3)
        assert not result.saturated
        assert state.bins[result.bin_index].ceiling_c == learner.MAX_AUTHORITY_C

    def test_ceiling_tightens_toward_proven_useful_depth(self):
        index = learner.bin_index_for(0.0)
        bins = list(learner.initial_state().bins)
        bins[index] = OutdoorBin(
            hold_offset_c=-learner.MAX_AUTHORITY_C,
            ceiling_c=learner.MAX_AUTHORITY_C,
            useful_offset_c=2.0,
            samples=50,
        )
        state = mature_state(bins=tuple(bins), prev_indoor_c=19.0)
        after, _ = self._saturating(state, learner.SATURATION_STREAK + 1)
        assert after.bins[index].ceiling_c == pytest.approx(
            2.0 + learner.CEILING_MARGIN_C
        )

    def test_ceiling_never_latches_shut(self):
        index = learner.bin_index_for(0.0)
        bins = list(learner.initial_state().bins)
        bins[index] = OutdoorBin(
            hold_offset_c=-learner.CEILING_MIN_C,
            ceiling_c=learner.CEILING_MIN_C,
            samples=50,
        )
        state = mature_state(bins=tuple(bins), prev_indoor_c=19.0)
        after, _ = self._saturating(state, learner.SATURATION_STREAK * 3)
        assert after.bins[index].ceiling_c >= learner.CEILING_MIN_C

    def test_closing_at_depth_records_useful_offset_and_recovers_ceiling(self):
        index = learner.bin_index_for(0.0)
        bins = list(learner.initial_state().bins)
        bins[index] = OutdoorBin(
            hold_offset_c=-3.0, ceiling_c=3.5, useful_offset_c=0.0, samples=50
        )
        state = mature_state(bins=tuple(bins), prev_indoor_c=19.0)
        after, result = learner.step(
            state, make_inputs(indoor_temp_c=20.0, target_c=21.0)
        )
        assert after.bins[index].useful_offset_c == pytest.approx(3.0)
        assert after.bins[index].ceiling_c > 3.5
        assert after.bins[index].close_rate_c_per_h > 0
        assert result.close_rate_c_per_h > 0

    def test_saturation_streak_resets_when_it_starts_closing(self):
        state = mature_state(
            bins=tuple(
                OutdoorBin(hold_offset_c=-learner.MAX_AUTHORITY_C, samples=50)
                for _ in range(learner.N_BINS)
            ),
            prev_indoor_c=19.0,
        )
        state, _ = self._saturating(state, 2)
        assert state.saturation_streak == 2
        state, _ = learner.step(state, make_inputs(indoor_temp_c=20.0, target_c=21.0))
        assert state.saturation_streak == 0


class TestTable:
    def test_neighbours_are_pre_warmed_without_claiming_evidence(self):
        state = mature_state()
        after, result = learner.step(
            state, make_inputs(indoor_temp_c=19.0, outdoor_temp_c=0.0)
        )
        index = result.bin_index
        assert after.bins[index].samples == 1
        for neighbour in (index - 1, index + 1):
            if 0 <= neighbour < learner.N_BINS:
                assert after.bins[neighbour].hold_offset_c < 0
                # Pre-warmed, but not credited with evidence never collected.
                assert after.bins[neighbour].samples == 0

    def test_unvisited_bins_interpolate_between_visited_ones(self):
        bins = list(learner.initial_state().bins)
        bins[0] = OutdoorBin(hold_offset_c=-4.0, samples=10)
        bins[4] = OutdoorBin(hold_offset_c=0.0, samples=10)
        value = learner.effective_hold_offset(tuple(bins), 2)
        assert value == pytest.approx(-2.0)

    def test_beyond_the_outermost_visited_bin_the_value_is_held(self):
        """A first cold snap starts from what the house needed at the coldest
        temperature already seen, not from zero."""
        bins = list(learner.initial_state().bins)
        bins[3] = OutdoorBin(hold_offset_c=-1.5, samples=10)
        assert learner.effective_hold_offset(tuple(bins), 0) == pytest.approx(-1.5)
        assert learner.effective_hold_offset(
            tuple(bins), learner.N_BINS - 1
        ) == pytest.approx(-1.5)

    def test_nothing_visited_yields_the_bin_own_value(self):
        bins = learner.initial_state().bins
        assert learner.effective_hold_offset(bins, 3) == 0.0

    def test_coverage_counts_visited_bins(self):
        bins = list(learner.initial_state().bins)
        assert learner.coverage_of(tuple(bins)) == 0.0
        bins[0] = OutdoorBin(samples=1)
        bins[1] = OutdoorBin(samples=1)
        assert learner.coverage_of(tuple(bins)) == pytest.approx(
            2 / learner.N_BINS
        )

    def test_truncated_restored_state_is_rebuilt_not_crashed(self):
        state = replace(learner.initial_state(), bins=(OutdoorBin(),))
        after, result = learner.step(state, make_inputs())
        assert len(after.bins) == learner.N_BINS
        assert result.offset_c == pytest.approx(0.0, abs=1e-9)


class TestRecoverableSag:
    def test_scales_with_rate_and_window(self):
        assert learner.recoverable_sag_c(0.5, 4.0) == pytest.approx(2.0)

    def test_no_measurement_reports_zero(self):
        """Callers read 0.0 as 'no measured basis to taper on', which is not the
        same as 'this band recovers nothing'."""
        assert learner.recoverable_sag_c(0.0, 4.0) == 0.0
        assert learner.recoverable_sag_c(-1.0, 4.0) == 0.0
        assert learner.recoverable_sag_c(0.5, 0.0) == 0.0


class TestInvariants:
    @pytest.mark.parametrize("indoor", [-40.0, 0.0, 19.0, 21.0, 25.0, 60.0])
    @pytest.mark.parametrize("outdoor", [-40.0, -12.0, 0.0, 8.0, 30.0])
    def test_offset_never_exceeds_earned_authority(self, indoor, outdoor):
        """Both directions have to earn their authority — see learner.py's
        clamp sites. Neither side may exceed it, in either direction."""
        state = mature_state()
        for _ in range(200):
            state, result = learner.step(
                state, make_inputs(indoor_temp_c=indoor, outdoor_temp_c=outdoor)
            )
            assert result.offset_c >= -result.authority_c - 1e-9
            assert result.offset_c <= result.authority_c + 1e-9
            assert result.hold_offset_c >= -result.authority_c - 1e-9
            assert result.hold_offset_c <= result.authority_c + 1e-9
            assert math.isfinite(result.offset_c)
            assert math.isfinite(result.hold_offset_c)

    def test_positive_side_saturates_at_authority_instead_of_winding_up(self):
        """A sustained positive error (wood stove, uncompensated solar gain, a
        mis-sited sensor) must not wind the integrator up without limit — it
        saturates at the same MAX_AUTHORITY_C ceiling the negative side earns,
        so the house is never left with weeks of stored windup to unwind."""
        state = mature_state()
        for _ in range(500):
            state, result = learner.step(
                state, make_inputs(indoor_temp_c=24.0, target_c=21.0)
            )
        assert result.offset_c == pytest.approx(learner.MAX_AUTHORITY_C)
        assert result.hold_offset_c == pytest.approx(learner.MAX_AUTHORITY_C)

    def test_step_never_raises_on_hostile_input(self):
        for inputs in (
            make_inputs(indoor_temp_c=None, indoor_data_available=True),
            make_inputs(dt_hours=-5.0),
            make_inputs(rise_hours=0.0),
            make_inputs(outdoor_temp_c=1e6),
            make_inputs(target_c=-1e6),
        ):
            state, result = learner.step(learner.initial_state(), inputs)
            assert result.reason
            assert len(state.bins) == learner.N_BINS


def settled_off_state(**overrides) -> LearnerState:
    """Parked in bin 4 (0..5 degC) long enough to accept a baseline sample."""
    defaults = dict(prev_bin_index=4, bin_entered_s=0.0, prev_indoor_c=19.0)
    defaults.update(overrides)
    return replace(learner.initial_state(), **defaults)


def off_inputs(**overrides) -> LearnerInputs:
    defaults = dict(
        is_active=False, now_s=7200.0, indoor_temp_c=19.0, outdoor_temp_c=0.0
    )
    defaults.update(overrides)
    return make_inputs(**defaults)


class TestBaselineObservation:
    """What the learner may conclude while its own output is not being applied.

    The integrator is frozen here by design, so these tests are about the one
    thing that IS observable with compensation off: where the pump's untouched
    weather curve leaves the house.
    """

    def test_it_records_where_the_raw_curve_leaves_the_house(self):
        state, result = learner.step(settled_off_state(), off_inputs())
        assert state.baseline_bins[4].samples == 1
        assert state.baseline_bins[4].indoor_c == pytest.approx(19.0)
        assert result.baseline_deficit_c == pytest.approx(2.0)

    def test_it_records_nothing_while_compensation_is_on(self):
        """The premise fails the moment the offset is being applied: indoor then
        reflects the controller, not the curve."""
        state, _ = learner.step(
            settled_off_state(), off_inputs(is_active=True)
        )
        assert all(b.samples == 0 for b in state.baseline_bins)

    def test_it_waits_for_the_house_to_respond_to_the_band(self):
        """A sample taken on arrival describes the band we just left."""
        state, _ = learner.step(learner.initial_state(), off_inputs())
        assert all(b.samples == 0 for b in state.baseline_bins)

    def test_the_dwell_requirement_scales_with_the_measured_lag(self):
        slow = settled_off_state()
        # 2 h of dwell, against a house measured to need 6 h to respond.
        state, _ = learner.step(slow, off_inputs(rise_hours=6.0))
        assert state.baseline_bins[4].samples == 0

    def test_it_rejects_a_house_that_is_still_moving(self):
        state, _ = learner.step(
            settled_off_state(prev_indoor_c=18.0), off_inputs(indoor_temp_c=19.0)
        )
        # 1.0 degC over 0.25 h is 4 degC/h — a transient, not an equilibrium.
        assert state.baseline_bins[4].samples == 0

    def test_it_rejects_samples_above_the_heating_hard_limit(self):
        """With the raw curve not heating either, where the house sits says
        nothing about the heat curve."""
        state, _ = learner.step(
            settled_off_state(), off_inputs(heating_hard_limit_engaged=True)
        )
        assert state.baseline_bins[4].samples == 0

    def test_an_unavailable_indoor_sensor_records_nothing(self):
        state, _ = learner.step(
            settled_off_state(),
            off_inputs(indoor_data_available=False, indoor_temp_c=None),
        )
        assert all(b.samples == 0 for b in state.baseline_bins)

    def test_a_solar_gain_estimate_lowers_the_stored_baseline(self):
        """The whole point: a sunny sample must not be credited to the pump
        curve. Correcting it out should store a COLDER equilibrium than the
        raw reading, by exactly the estimated solar contribution."""
        corrected, _ = learner.step(
            settled_off_state(), off_inputs(estimated_solar_gain_c=1.5)
        )
        uncorrected, _ = learner.step(
            settled_off_state(), off_inputs(estimated_solar_gain_c=0.0)
        )
        assert corrected.baseline_bins[4].indoor_c == pytest.approx(
            uncorrected.baseline_bins[4].indoor_c - 1.5
        )

    def test_zero_solar_gain_is_byte_for_byte_identical_to_before(self):
        """The default must be a true no-op: this is what every existing
        baseline test above already exercises implicitly (they all pass
        estimated_solar_gain_c=0.0 via off_inputs' defaults)."""
        state, result = learner.step(
            settled_off_state(), off_inputs(estimated_solar_gain_c=0.0)
        )
        assert state.baseline_bins[4].indoor_c == pytest.approx(19.0)
        assert result.baseline_deficit_c == pytest.approx(2.0)

    def test_solar_gain_correction_does_not_affect_drift_rejection(self):
        """The drift filter must stay on the raw reading — solar-adjusting it
        would risk masking or introducing spurious transient rejections."""
        state, _ = learner.step(
            settled_off_state(prev_indoor_c=18.0),
            off_inputs(indoor_temp_c=19.0, estimated_solar_gain_c=5.0),
        )
        # Same rejection as test_it_rejects_a_house_that_is_still_moving,
        # regardless of the solar correction: 4 degC/h of drift is a
        # transient, not something a solar term should be able to launder.
        assert state.baseline_bins[4].samples == 0


class TestBaselineSeeding:
    """Turning a month of open-loop observation into a starting point."""

    def _baseline(self, indoor_c: float, samples: int = 50):
        bins = list(learner.initial_state().baseline_bins)
        bins[4] = learner.BaselineBin(indoor_c=indoor_c, samples=samples)
        return tuple(bins)

    def test_a_cold_house_seeds_a_colder_spoof(self):
        """The sign that matters: the raw curve leaving the house below target
        must ask the pump for MORE heat, which is a NEGATIVE offset."""
        bins, count = learner.seed_offsets_from_baseline(
            learner.initial_state().bins, self._baseline(19.0), 21.0
        )
        assert count == 1
        assert bins[4].hold_offset_c == pytest.approx(-2.0)
        assert bins[4].seeded

    def test_an_overheating_house_seeds_the_other_way(self):
        bins, _ = learner.seed_offsets_from_baseline(
            learner.initial_state().bins, self._baseline(22.5), 21.0
        )
        assert bins[4].hold_offset_c == pytest.approx(1.5)

    def test_it_never_overwrites_closed_loop_learning(self):
        """Measured evidence beats inferred evidence, always."""
        learned = list(learner.initial_state().bins)
        learned[4] = OutdoorBin(hold_offset_c=-3.2, samples=40)
        bins, count = learner.seed_offsets_from_baseline(
            tuple(learned), self._baseline(19.0), 21.0
        )
        assert count == 0
        assert bins[4].hold_offset_c == pytest.approx(-3.2)
        assert not bins[4].seeded

    def test_it_waits_for_enough_baseline_evidence(self):
        _, count = learner.seed_offsets_from_baseline(
            learner.initial_state().bins,
            self._baseline(19.0, samples=learner.BASELINE_MIN_SAMPLES_TO_SEED - 1),
            21.0,
        )
        assert count == 0

    def test_a_wildly_mismatched_curve_is_seeded_only_part_way(self):
        """Open-loop evidence gets less trust than the integrator's own, so one
        observation never buys the whole correction."""
        bins, _ = learner.seed_offsets_from_baseline(
            learner.initial_state().bins, self._baseline(13.0), 21.0
        )
        assert bins[4].hold_offset_c == pytest.approx(-learner.BASELINE_SEED_MAX_C)

    def test_a_seed_respects_a_learned_capacity_ceiling(self):
        capped = list(learner.initial_state().bins)
        capped[4] = OutdoorBin(ceiling_c=1.5, samples=0)
        bins, _ = learner.seed_offsets_from_baseline(
            tuple(capped), self._baseline(17.0), 21.0
        )
        assert bins[4].hold_offset_c == pytest.approx(-1.5)

    def test_seeding_fires_on_the_transition_and_not_on_every_cycle(self):
        state = replace(
            settled_off_state(),
            baseline_bins=self._baseline(19.0),
            prev_is_active=False,
        )
        state, first = learner.step(state, off_inputs(is_active=True))
        assert first.seeded_bins == 1

        _, second = learner.step(state, off_inputs(is_active=True))
        assert second.seeded_bins == 0

    def test_a_seeded_bin_is_not_counted_as_proven_coverage(self):
        """Coverage reports what has actually been learned, so an open-loop
        guess must not inflate it."""
        bins, _ = learner.seed_offsets_from_baseline(
            learner.initial_state().bins, self._baseline(19.0), 21.0
        )
        assert learner.coverage_of(bins) == 0.0
