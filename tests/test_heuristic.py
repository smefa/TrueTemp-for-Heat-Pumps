"""Unit tests for the term-composition and price logic.

What used to be the whole controller is now composition plus price. The indoor
response lives in `learner.py` and arrives here as a finished number, so these
tests are about how terms combine, when price acts, and — the part with real
teeth — when price refuses to act because it is too cold to buy the sag back.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

heuristic = load("heuristic")

HeuristicInputs = heuristic.HeuristicInputs
HeuristicParams = heuristic.HeuristicParams
compute = heuristic.compute


def make_params(**overrides) -> HeuristicParams:
    defaults = dict(
        indoor_target_c=21.0,
        comfort_min_c=18.0,
        enable_price_compensation=False,
    )
    defaults.update(overrides)
    return HeuristicParams(**defaults)


def make_inputs(**overrides) -> HeuristicInputs:
    defaults = dict(
        indoor_temp_c=21.0,
        indoor_data_available=True,
        raw_outdoor_temp_c=3.0,
        wind_speed_ms=0.0,
        wind_data_available=True,
        sun_elevation_deg=0.0,
        sun_azimuth_deg=180.0,
        cloud_coverage_pct=0.0,
        cloud_data_available=True,
        current_price=None,
        price_data_available=False,
    )
    defaults.update(overrides)
    return HeuristicInputs(**defaults)


def flat_forecast(price: float = 1.0, hours: int = 24):
    return tuple((float(h), price) for h in range(hours))


def spiky_forecast(base: float = 1.0, peak: float = 5.0, spike_at: int = 4, hours: int = 24):
    """A day with one clear spike, which is what the band logic is tuned for."""
    return tuple(
        (float(h), peak if h == spike_at else base) for h in range(hours)
    )


class TestSolarEffectOf:
    """Direct tests of the extracted formula, since `compute()` now just calls
    it — see `TestComposition.test_compute_still_uses_the_extracted_formula`
    for confirmation the extraction did not change `compute()`'s behaviour."""

    def test_sun_below_horizon_is_zero(self):
        assert heuristic.solar_effect_of(-20.0, 180.0, 0.0) == 0.0

    def test_sun_at_zenith_is_zero(self):
        """A fixed vertical window gets nothing from directly overhead sun —
        the defining difference from the old sin(elevation) shape, which
        peaked here instead."""
        assert heuristic.solar_effect_of(90.0, 180.0, 0.0) == pytest.approx(0.0, abs=1e-9)

    def test_full_cloud_cover_still_passes_diffuse_light(self):
        """Overcast discounts hard but doesn't zero out — diffuse light still
        gets through."""
        clear = heuristic.solar_effect_of(45.0, 180.0, 0.0)
        overcast = heuristic.solar_effect_of(45.0, 180.0, 100.0)
        assert overcast == pytest.approx(clear * 0.25)
        assert overcast > 0.0

    def test_missing_cloud_data_is_treated_as_clear(self):
        """Same "assume clear" behaviour `compute()` documents for a missing
        forecast — None must not collapse the term to zero."""
        assert heuristic.solar_effect_of(30.0, 180.0, None) == pytest.approx(
            heuristic.solar_effect_of(30.0, 180.0, 0.0)
        )

    def test_sun_due_south_is_unaffected_by_azimuth_term(self):
        """Azimuth offset of zero (sun_azimuth_deg=180, HA's convention) must
        reduce to the old always-south formula exactly — the new term is a
        pure extension, not a rescale of the south case."""
        assert heuristic.solar_effect_of(30.0, 180.0, 0.0) == pytest.approx(0.489, abs=5e-3)

    def test_sun_square_off_south_is_zero(self):
        """90° off south (due east/west) is edge-on to a south-facing pane —
        no direct gain, regardless of elevation."""
        assert heuristic.solar_effect_of(30.0, 90.0, 0.0) == pytest.approx(0.0, abs=1e-9)
        assert heuristic.solar_effect_of(30.0, 270.0, 0.0) == pytest.approx(0.0, abs=1e-9)

    def test_sun_behind_the_house_is_zero(self):
        """Beyond 90° off south the sun is behind the wall, not just edge-on —
        must clamp to zero rather than go negative."""
        assert heuristic.solar_effect_of(30.0, 40.0, 0.0) == 0.0
        assert heuristic.solar_effect_of(30.0, 320.0, 0.0) == 0.0

    def test_evening_off_south_sun_scores_less_than_due_south(self):
        """The symptom this term was added to fix: a low evening sun well off
        south must score below the same elevation due south, not the same or
        more."""
        due_south = heuristic.solar_effect_of(10.0, 180.0, 50.0)
        evening_west = heuristic.solar_effect_of(10.0, 250.0, 50.0)
        assert 0.0 < evening_west < due_south


class TestComposition:
    def test_compute_still_uses_the_extracted_formula(self):
        """Confirms the extraction in `solar_effect_of` was a pure refactor:
        `compute()`'s own solar_effect must exactly match calling the
        extracted function directly with the same inputs."""
        result = compute(
            make_inputs(sun_elevation_deg=45.0, cloud_coverage_pct=30.0),
            make_params(),
        )
        assert result.solar_effect == pytest.approx(
            heuristic.solar_effect_of(45.0, 180.0, 30.0)
        )


    def test_learned_offset_passes_straight_through(self):
        result = compute(make_inputs(learned_offset_c=-2.5), make_params())
        assert result.compensated_outdoor_temp_c == pytest.approx(0.5)
        assert result.learned_offset_c == pytest.approx(-2.5)

    def test_user_indoor_target_c_echoes_the_true_target_even_during_setback(self):
        """`indoor_target_c` is vacation-effective (see coordinator._params());
        `user_indoor_target_c` must keep reporting the occupant's literal
        configured number — what `climate.target_temperature` shows — so the
        two stay distinguishable in the sensor/JSONL output."""
        result = compute(
            make_inputs(),
            make_params(indoor_target_c=15.0, user_indoor_target_c=21.0),
        )
        assert result.indoor_target_c == pytest.approx(15.0)
        assert result.user_indoor_target_c == pytest.approx(21.0)

    def test_terms_sum_onto_the_raw_reading(self):
        result = compute(
            make_inputs(
                raw_outdoor_temp_c=0.0,
                learned_offset_c=-1.0,
                wind_speed_ms=10.0,
                sun_elevation_deg=90.0,
                cloud_coverage_pct=0.0,
            ),
            make_params(),
        )
        expected = (
            0.0
            + result.learned_offset_c
            + result.wind_adjustment_c
            + result.sun_adjustment_c
            + result.price_adjustment_c
        )
        assert result.compensated_outdoor_temp_c == pytest.approx(expected)

    def test_wind_asks_for_more_heat_and_sun_asks_for_less(self):
        result = compute(
            make_inputs(
                wind_speed_ms=8.0, sun_elevation_deg=45.0, cloud_coverage_pct=0.0
            ),
            make_params(),
        )
        assert result.wind_adjustment_c < 0
        assert result.sun_adjustment_c > 0

    def test_wind_below_the_deadband_contributes_nothing(self):
        result = compute(
            make_inputs(wind_speed_ms=heuristic.WIND_DEADBAND_MS - 0.1),
            make_params(),
        )
        assert result.wind_adjustment_c == 0.0

    def test_wind_above_the_deadband_gains_on_the_excess_only(self):
        result = compute(
            make_inputs(wind_speed_ms=heuristic.WIND_DEADBAND_MS + 2.0),
            make_params(),
        )
        assert result.wind_adjustment_c == pytest.approx(
            -heuristic.WIND_GAIN_C_PER_MS * 2.0
        )

    def test_cloud_cover_scales_the_solar_term(self):
        clear = compute(
            make_inputs(sun_elevation_deg=45.0, cloud_coverage_pct=0.0), make_params()
        )
        overcast = compute(
            make_inputs(sun_elevation_deg=45.0, cloud_coverage_pct=100.0), make_params()
        )
        assert clear.sun_adjustment_c > overcast.sun_adjustment_c
        assert overcast.sun_adjustment_c > 0.0

    def test_night_contributes_no_solar(self):
        result = compute(make_inputs(sun_elevation_deg=-20.0), make_params())
        assert result.solar_effect == 0.0
        assert result.sun_adjustment_c == 0.0

    def test_disabled_inputs_contribute_exactly_zero(self):
        result = compute(
            make_inputs(
                wind_speed_ms=10.0, sun_elevation_deg=45.0, cloud_coverage_pct=0.0
            ),
            make_params(enable_wind_input=False, enable_solar_input=False),
        )
        assert result.wind_adjustment_c == 0.0
        assert result.sun_adjustment_c == 0.0
        assert "wind off" in result.reason
        assert "solar off" in result.reason

    def test_solar_effect_is_reported_even_when_the_term_is_off(self):
        """It is a physical fact about the world, and the log wants reality."""
        result = compute(
            make_inputs(sun_elevation_deg=45.0, cloud_coverage_pct=0.0),
            make_params(enable_solar_input=False),
        )
        assert result.solar_effect == pytest.approx(heuristic.solar_effect_of(45.0, 180.0, 0.0))
        assert result.sun_adjustment_c == 0.0

    def test_output_is_clamped_to_sane_bounds(self):
        low = compute(
            make_inputs(raw_outdoor_temp_c=-39.0, learned_offset_c=-50.0), make_params()
        )
        assert low.compensated_outdoor_temp_c == heuristic.OUTPUT_SANITY_MIN_C
        high = compute(
            # Below the hard limit, so this exercises the sanity clamp on the
            # normal compute path rather than the hard-limit override.
            make_inputs(raw_outdoor_temp_c=10.0, learned_offset_c=50.0),
            make_params(),
        )
        assert high.compensated_outdoor_temp_c == heuristic.OUTPUT_SANITY_MAX_C

    def test_missing_indoor_reading_still_publishes(self):
        result = compute(
            make_inputs(
                indoor_temp_c=None, indoor_data_available=False, learned_offset_c=-1.0
            ),
            make_params(),
        )
        assert result.compensated_outdoor_temp_c == pytest.approx(2.0)
        assert "Indoor sensor unavailable" in result.reason


class TestHeatingHardLimit:
    def test_at_or_above_the_limit_forces_the_warm_ceiling(self):
        result = compute(
            make_inputs(
                raw_outdoor_temp_c=20.0,
                learned_offset_c=-3.0,
                wind_speed_ms=10.0,
                sun_elevation_deg=45.0,
                current_price=9.0,
                price_data_available=True,
                price_forecast=spiky_forecast(),
            ),
            make_params(enable_price_compensation=True),
        )
        assert result.heating_hard_limit_engaged
        assert result.compensated_outdoor_temp_c == heuristic.OUTPUT_SANITY_MAX_C
        # Echoes the learner's actual held offset rather than zeroing it out —
        # zero would be indistinguishable from the learner having unwound.
        assert result.learned_offset_c == -3.0
        assert result.wind_adjustment_c == 0.0
        assert result.sun_adjustment_c == 0.0
        assert result.price_adjustment_c == 0.0
        assert result.current_price == 9.0

    def test_just_below_the_limit_still_compensates(self):
        result = compute(
            make_inputs(raw_outdoor_temp_c=19.9, learned_offset_c=-1.0),
            make_params(),
        )
        assert not result.heating_hard_limit_engaged
        assert result.compensated_outdoor_temp_c == pytest.approx(18.9)


class TestHeatingHardLimitHysteresis:
    """Same flapping symptom the old heating-cutoff guardrail closed off,
    reproduced against the fixed 20°C hard limit: `test_just_below_the_limit_
    still_compensates` above and `test_at_or_above_the_limit_forces_the_warm_
    ceiling` would both fire, alternately, on every cycle a raw reading
    idling near the threshold crosses back and forth."""

    def test_resolve_engaged_is_a_bare_threshold_when_not_previously_engaged(self):
        engaged = heuristic.resolve_heating_hard_limit_engaged
        assert not engaged(19.9, prev_engaged=False)
        assert engaged(20.0, prev_engaged=False)

    def test_resolve_engaged_requires_the_margin_to_release(self):
        engaged = heuristic.resolve_heating_hard_limit_engaged
        margin = heuristic.HEATING_HARD_LIMIT_HYSTERESIS_C
        # Still within the margin below the limit: stays engaged.
        assert engaged(20.0 - margin + 0.1, prev_engaged=True)
        # Past the margin: releases.
        assert not engaged(20.0 - margin - 0.1, prev_engaged=True)

    def test_previously_engaged_stays_forced_just_below_the_limit(self):
        """Same 19.9°C reading as `test_just_below_the_limit_still_compensates`,
        but arriving with last cycle's hard limit already engaged — the case
        that formula-only comparison got wrong."""
        result = compute(
            replace(
                make_inputs(raw_outdoor_temp_c=19.9, learned_offset_c=-1.0),
                prev_heating_hard_limit_engaged=True,
            ),
            make_params(),
        )
        assert result.heating_hard_limit_engaged
        assert result.compensated_outdoor_temp_c == heuristic.OUTPUT_SANITY_MAX_C
        assert result.learned_offset_c == -1.0

    def test_releases_once_past_the_hysteresis_margin(self):
        margin = heuristic.HEATING_HARD_LIMIT_HYSTERESIS_C
        result = compute(
            replace(
                make_inputs(
                    raw_outdoor_temp_c=20.0 - margin - 0.1, learned_offset_c=-1.0
                ),
                prev_heating_hard_limit_engaged=True,
            ),
            make_params(),
        )
        assert not result.heating_hard_limit_engaged
        assert result.compensated_outdoor_temp_c == pytest.approx(
            20.0 - margin - 0.1 - 1.0
        )


class TestPriceGating:
    def test_no_price_entity_means_no_price_action(self):
        result = compute(make_inputs(), make_params(enable_price_compensation=True))
        assert result.price_adjustment_c == 0.0
        assert not result.price_braking

    def test_disabled_price_ignores_a_real_spike(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(enable_price_compensation=False),
        )
        assert result.price_adjustment_c == 0.0

    def test_no_forecast_means_no_price_action(self):
        """Deliberate: without a day-distribution there is no principled way to
        know whether the current price is high. Two configured absolute
        thresholds were both a config burden and a way to brake on an ordinary
        day."""
        result = compute(
            make_inputs(current_price=99.0, price_data_available=True, price_forecast=None),
            make_params(enable_price_compensation=True),
        )
        assert result.price_adjustment_c == 0.0
        assert "no day-ahead forecast" in result.reason

    def test_too_few_forecast_points_means_no_price_action(self):
        result = compute(
            make_inputs(
                current_price=99.0,
                price_data_available=True,
                price_forecast=((0.0, 1.0), (1.0, 9.0)),
            ),
            make_params(enable_price_compensation=True),
        )
        assert result.price_adjustment_c == 0.0

    def test_flat_day_is_left_alone(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=flat_forecast(),
            ),
            make_params(enable_price_compensation=True),
        )
        assert result.price_adjustment_c == 0.0
        assert "flat day" in result.reason

    def test_disabled_compensation_still_reports_the_raw_feed(self):
        """current_price and price_median are informational — a house with
        braking switched off can still ask what power costs right now. Only
        the brake thresholds are supposed to disappear."""
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(enable_price_compensation=False),
        )
        assert result.current_price == 5.0
        assert result.price_median is not None
        assert result.price_adjustment_c == 0.0
        assert result.price_band_start is None
        assert result.price_band_full is None


class TestPriceBraking:
    def _braking(self, **overrides):
        params = dict(
            enable_price_compensation=True,
            price_comfort_tier="high",
            cold_caution="low",
        )
        params.update(overrides.pop("params", {}))
        inputs = dict(
            raw_outdoor_temp_c=3.0,
            current_price=5.0,
            price_data_available=True,
            price_forecast=spiky_forecast(spike_at=0),
        )
        inputs.update(overrides)
        return compute(make_inputs(**inputs), make_params(**params))

    def test_an_expensive_hour_raises_the_published_temperature(self):
        """Spoofing warmer is what tells the pump's curve to back off."""
        result = self._braking()
        assert result.price_adjustment_c > 0
        assert result.price_braking
        assert result.effective_indoor_target_c < result.indoor_target_c

    def test_the_comfort_floor_is_a_hard_stop(self):
        result = self._braking(params={"comfort_min_c": 20.5, "price_comfort_tier": "high"})
        assert result.effective_indoor_target_c >= 20.5
        assert result.price_shift_applied_c == pytest.approx(0.5, abs=1e-6)

    def test_an_already_below_floor_target_is_not_dragged_back_up(self):
        """`comfort_min_c` bounds price braking in an OCCUPIED house. A
        holiday-derived `indoor_target_c` can legitimately already sit below
        it (see coordinator.py/holiday.HOLIDAY_TARGET_MIN_C) — braking must
        not silently drag that back up to `comfort_min_c`, only refuse to
        push it any lower still."""
        result = self._braking(
            params={"comfort_min_c": 20.0, "indoor_target_c": 10.0, "price_comfort_tier": "high"}
        )
        assert result.effective_indoor_target_c == pytest.approx(10.0, abs=1e-6)

    def test_reported_shift_reflects_the_floor_not_the_request(self):
        floored = self._braking(params={"comfort_min_c": 20.0})
        assert floored.price_shift_applied_c == pytest.approx(
            floored.indoor_target_c - floored.effective_indoor_target_c
        )

    def test_aggressive_tiers_sag_further(self):
        low = self._braking(params={"price_comfort_tier": "low"})
        high = self._braking(params={"price_comfort_tier": "high"})
        assert high.price_shift_applied_c > low.price_shift_applied_c

    def test_pre_braking_starts_before_the_spike(self):
        """Braking that starts later than half the fall time is still pushing
        heat into the hour it was meant to avoid."""
        result = compute(
            make_inputs(
                current_price=2.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=2),
                fall_minutes=360.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="mid",
                cold_caution="low",
            ),
        )
        assert result.upcoming_spike_in_min == pytest.approx(120.0)
        assert result.price_adjustment_c > 0
        assert "pre-braking" in result.reason

    def test_a_spike_beyond_the_fall_time_is_not_acted_on_yet(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=10),
                fall_minutes=120.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="mid",
                cold_caution="low",
            ),
        )
        assert result.price_adjustment_c == 0.0

    def test_lead_time_comes_from_the_measured_fall_time(self):
        slab = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                fall_minutes=360.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="mid",
                cold_caution="low",
            ),
        )
        assert slab.lead_minutes_effective == 180.0
        assert slab.price_adjustment_c > 0


class TestPriceCatchup:
    """Feedforward-only push added on top of the steady-state price shift so
    the target sag/precharge is actually reached quickly rather than merely
    asked for. See `PRICE_CATCHUP_GAIN`'s docstring in heuristic.py."""

    def _braking(self, **overrides):
        params = dict(
            enable_price_compensation=True,
            price_comfort_tier="high",
            cold_caution="low",
        )
        params.update(overrides.pop("params", {}))
        inputs = dict(
            raw_outdoor_temp_c=3.0,
            current_price=5.0,
            price_data_available=True,
            price_forecast=spiky_forecast(spike_at=0),
        )
        inputs.update(overrides)
        return compute(make_inputs(**inputs), make_params(**params))

    def test_pushes_harder_before_indoor_has_sagged(self):
        """Indoor still sitting at target: the whole gap is still ahead, so
        the sent value exceeds the steady-state shift alone."""
        result = self._braking(indoor_temp_c=21.0)
        assert result.price_catchup_c > 0.0
        assert result.price_adjustment_c > result.price_shift_applied_c

    def test_settles_once_the_target_sag_is_reached(self):
        """Once indoor has actually sagged as far as the target, there is no
        remaining gap to push for — the published value settles back to the
        plain steady-state shift."""
        probe = self._braking(indoor_temp_c=21.0)
        reached = self._braking(indoor_temp_c=21.0 - probe.price_shift_applied_c)
        assert reached.price_catchup_c == pytest.approx(0.0, abs=1e-9)
        assert reached.price_adjustment_c == pytest.approx(reached.price_shift_applied_c)

    def test_never_reverses_once_overshot(self):
        """Indoor already sagged further than the target: catch-up must not
        claw the offset back the other way, only ever stop adding."""
        result = self._braking(indoor_temp_c=10.0)
        assert result.price_catchup_c == pytest.approx(0.0, abs=1e-9)

    def test_requires_indoor_data(self):
        result = self._braking(indoor_temp_c=None, indoor_data_available=False)
        assert result.price_catchup_c == 0.0

    def test_capped(self):
        result = self._braking(indoor_temp_c=21.0)
        assert result.price_catchup_c <= heuristic.PRICE_CATCHUP_MAX_C

    def test_precharge_direction_pushes_more_heat_while_catching_up(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
                indoor_temp_c=21.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.precharge_active
        assert result.price_catchup_c < 0.0
        assert result.price_adjustment_c < result.price_shift_applied_c


class TestPreCharge:
    def test_high_tier_banks_heat_while_it_is_cheap(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.precharge_active
        assert result.effective_indoor_target_c > result.indoor_target_c
        assert result.price_adjustment_c < 0
        assert "pre-charging" in result.reason

    def test_lower_tiers_do_not_pre_charge(self):
        for tier in ("low", "mid"):
            result = compute(
                make_inputs(
                    current_price=1.0,
                    price_data_available=True,
                    price_forecast=spiky_forecast(spike_at=1),
                    rise_minutes=120.0,
                ),
                make_params(
                    enable_price_compensation=True,
                    price_comfort_tier=tier,
                    cold_caution="low",
                ),
            )
            assert not result.precharge_active

    def test_pre_charge_is_timed_off_the_rise_time(self):
        """Banked heat must have ARRIVED before the spike, not be on its way."""
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=6),
                rise_minutes=60.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert not result.precharge_active

    def test_no_pre_charging_once_the_price_is_already_high(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert not result.precharge_active


class TestColdCaution:
    """The one manual control over deep cold: braking is cheap to enter and
    expensive to exit, and on most installs the exit eventually runs through
    resistive backup heat."""

    def test_below_the_floor_braking_stops_entirely(self):
        result = compute(
            make_inputs(
                raw_outdoor_temp_c=-25.0,
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.cold_brake_factor == 0.0
        assert result.price_adjustment_c == 0.0
        assert "too cold to brake" in result.reason

    def test_higher_caution_stops_braking_sooner(self):
        def factor(caution):
            return compute(
                make_inputs(
                    raw_outdoor_temp_c=-8.0,
                    current_price=5.0,
                    price_data_available=True,
                    price_forecast=spiky_forecast(spike_at=0),
                ),
                make_params(
                    enable_price_compensation=True,
                    price_comfort_tier="high",
                    cold_caution=caution,
                ),
            ).cold_brake_factor

        assert factor("high") == 0.0
        assert factor("mid") < factor("low")

    def test_mild_weather_brakes_at_full_authority(self):
        for caution in heuristic.COLD_CAUTIONS:
            result = compute(
                make_inputs(
                    raw_outdoor_temp_c=10.0,
                    current_price=5.0,
                    price_data_available=True,
                    price_forecast=spiky_forecast(spike_at=0),
                ),
                make_params(
                    enable_price_compensation=True,
                    price_comfort_tier="high",
                    cold_caution=caution,
                ),
            )
            assert result.cold_brake_factor == pytest.approx(1.0)

    def test_measured_recovery_limits_the_sag_further(self):
        """Once a band knows how fast it recovers, the sag is limited to what
        can actually be bought back — the question the old hand-drawn taper was
        approximating."""
        def factor(recoverable):
            return heuristic.cold_brake_factor(
                10.0,
                heuristic.resolve_cold_caution("mid"),
                recoverable_sag_c=recoverable,
                tier_max_sag_c=3.0,
            )

        assert factor(0.0) == pytest.approx(1.0)  # no measurement, no taper
        assert factor(3.0) == pytest.approx(1.0)
        assert factor(1.5) == pytest.approx(0.5)
        assert factor(0.15) == pytest.approx(0.05)

    def test_caution_exponent_sharpens_the_feasibility_taper(self):
        gentle = heuristic.cold_brake_factor(
            10.0, heuristic.resolve_cold_caution("low"), 1.5, 3.0
        )
        sharp = heuristic.cold_brake_factor(
            10.0, heuristic.resolve_cold_caution("high"), 1.5, 3.0
        )
        assert sharp < gentle

    def test_unknown_names_fall_back_to_balanced(self):
        assert heuristic.resolve_cold_caution("nonsense") is heuristic.COLD_CAUTIONS["mid"]
        assert heuristic.resolve_cold_caution(None) is heuristic.COLD_CAUTIONS["mid"]
        assert heuristic.resolve_price_tier("nonsense") is heuristic.PRICE_TIERS["mid"]


class TestPriceBrakingFlag:
    """The learner freezes on this. If it were wrong in either direction the
    loop would either fight the price excursion or stop learning for no reason."""

    def test_set_while_deliberately_holding_away_from_target(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.price_braking

    def test_set_while_pre_charging_too(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.precharge_active
        assert result.price_braking

    def test_clear_when_price_is_doing_nothing(self):
        assert not compute(make_inputs(), make_params()).price_braking

    def test_clear_on_a_flat_day(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=flat_forecast(),
            ),
            make_params(enable_price_compensation=True),
        )
        assert not result.price_braking


class TestExplainability:
    def test_reason_is_always_populated(self):
        for inputs in (
            make_inputs(),
            make_inputs(indoor_temp_c=None, indoor_data_available=False),
            make_inputs(raw_outdoor_temp_c=30.0),
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
        ):
            result = compute(inputs, make_params(enable_price_compensation=True))
            assert result.reason
            assert "total" in result.reason or "hard limit" in result.reason

    def test_band_thresholds_are_published_for_troubleshooting(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(enable_price_compensation=True),
        )
        assert result.price_band_start is not None
        assert result.price_band_full is not None
        assert result.price_median is not None
        assert result.price_band_start < result.price_band_full


class TestHeatCurveOffset:
    """`heat_curve_offset_c` is the one piece of the native-curve-offset
    output mode that can be unit-tested without Home Assistant — and the sign
    is exactly the kind of thing this project has gotten wrong before (see
    the lag estimator). A house calling for MORE heat means a more negative
    internal spoof (colder outdoor, per learner.py's "Sign convention") but,
    on the common pump convention, a MORE POSITIVE curve offset.
    """

    heat_curve_offset_c = staticmethod(heuristic.heat_curve_offset_c)

    def test_more_heat_wanted_is_positive_by_default(self):
        # Spoofed colder than raw -> more heat wanted.
        value = self.heat_curve_offset_c(
            compensated_outdoor_temp_c=-2.0, raw_outdoor_temp_c=3.0, invert=False
        )
        assert value == pytest.approx(5.0)

    def test_less_heat_wanted_is_negative_by_default(self):
        value = self.heat_curve_offset_c(
            compensated_outdoor_temp_c=6.0, raw_outdoor_temp_c=3.0, invert=False
        )
        assert value == pytest.approx(-3.0)

    def test_invert_flips_the_sign(self):
        assert self.heat_curve_offset_c(
            compensated_outdoor_temp_c=-2.0, raw_outdoor_temp_c=3.0, invert=True
        ) == pytest.approx(-5.0)

    def test_no_compensation_is_zero_regardless_of_invert(self):
        for invert in (False, True):
            assert self.heat_curve_offset_c(
                compensated_outdoor_temp_c=3.0, raw_outdoor_temp_c=3.0, invert=invert
            ) == pytest.approx(0.0)

    def test_rounds_to_a_whole_number(self):
        # Most pumps' native curve-offset parameter is an integer dial — a
        # fractional delta must round rather than be sent as-is.
        value = self.heat_curve_offset_c(
            compensated_outdoor_temp_c=-2.6, raw_outdoor_temp_c=3.0, invert=False
        )
        assert value == 6
        assert isinstance(value, int)


class TestIndoorClimateOffset:
    """`indoor_climate_offset_c` is the delta added to an indoor climate
    entity's own target temperature — same magnitude and sign as
    `heat_curve_offset_c(..., invert=False)`, but never rounded, since there
    is only one universal convention for a climate entity's target (higher
    means more heat) and no per-pump direction to flip.
    """

    indoor_climate_offset_c = staticmethod(heuristic.indoor_climate_offset_c)

    def test_more_heat_wanted_is_positive(self):
        # Spoofed colder than raw -> more heat wanted -> target nudged up.
        value = self.indoor_climate_offset_c(
            compensated_outdoor_temp_c=-2.0, raw_outdoor_temp_c=3.0
        )
        assert value == pytest.approx(5.0)

    def test_less_heat_wanted_is_negative(self):
        value = self.indoor_climate_offset_c(
            compensated_outdoor_temp_c=6.0, raw_outdoor_temp_c=3.0
        )
        assert value == pytest.approx(-3.0)

    def test_no_compensation_is_zero(self):
        assert self.indoor_climate_offset_c(
            compensated_outdoor_temp_c=3.0, raw_outdoor_temp_c=3.0
        ) == pytest.approx(0.0)

    def test_matches_heat_curve_offset_unrounded(self):
        # Same formula as heat_curve_offset_c(invert=False), just not rounded
        # to a whole number.
        assert self.indoor_climate_offset_c(
            compensated_outdoor_temp_c=-2.6, raw_outdoor_temp_c=3.0
        ) == pytest.approx(5.6)

    def test_scales_by_spoof_per_indoor_c(self):
        # Degrees of outdoor spoof only equal degrees of indoor target while
        # spoof_per_indoor_c is 1.0 — otherwise the result must be divided by
        # it to land back in indoor-target degrees.
        assert self.indoor_climate_offset_c(
            compensated_outdoor_temp_c=-2.0,
            raw_outdoor_temp_c=3.0,
            spoof_per_indoor_c=2.0,
        ) == pytest.approx(2.5)


class TestHeatingHardLimitOffset:
    """`heating_hard_limit_offset_c` is the fixed value pushed to the native
    curve-offset entity while the hard limit is engaged — deliberately NOT
    `heat_curve_offset_c(compensated, raw, invert)`, because raw keeps
    drifting with ordinary weather noise while the hard limit holds and
    `compensated_outdoor_temp_c` is pinned to a fixed ceiling during it. Using
    the live raw reading there reintroduced the exact flapping (offset
    hopping between +3 and +5 as raw idled a couple of degrees) the hard
    limit's own hysteresis was supposed to prevent.
    """

    def test_stable_regardless_of_raw(self):
        # Same call every time: nothing here depends on the actual outdoor
        # reading, which is the whole point.
        assert heuristic.heating_hard_limit_offset_c(
            invert=False
        ) == heuristic.heating_hard_limit_offset_c(invert=False)

    def test_matches_the_threshold_derived_delta_by_default(self):
        assert heuristic.heating_hard_limit_offset_c(invert=False) == -round(
            heuristic.OUTPUT_SANITY_MAX_C - heuristic.HEATING_HARD_LIMIT_C
        )

    def test_invert_flips_the_sign(self):
        assert heuristic.heating_hard_limit_offset_c(
            invert=True
        ) == -heuristic.heating_hard_limit_offset_c(invert=False)


class TestTodayPriceSpreadAndMedian:
    """`today_price_spread_and_median_c` — the one function both the seasonal
    history and `compute()`'s own braking-band logic read the day's spread
    and median through, so they can never disagree for the same cycle."""

    def test_no_forecast_is_none(self):
        assert heuristic.today_price_spread_and_median_c(None) is None

    def test_too_few_forecast_points_is_none(self):
        assert (
            heuristic.today_price_spread_and_median_c(((0.0, 1.0), (1.0, 9.0)))
            is None
        )

    def test_flat_day_has_zero_spread(self):
        result = heuristic.today_price_spread_and_median_c(flat_forecast(price=2.0))
        assert result == pytest.approx((0.0, 2.0))

    def test_spike_widens_the_spread_around_the_median(self):
        # 23 hours at 1.0, one hour at 5.0: median stays at the ordinary
        # price, peak is the spike, spread is peak minus median.
        result = heuristic.today_price_spread_and_median_c(spiky_forecast())
        assert result == pytest.approx((4.0, 1.0))

    def test_never_negative_even_if_median_exceeded_peak(self):
        # Degenerate/contrived forecast, but the function must not hand back a
        # negative "spread" that would then feed a negative significance ratio.
        result = heuristic.today_price_spread_and_median_c(
            tuple((float(h), 1.0) for h in range(6))
        )
        assert result[0] >= 0.0


class TestPriceSpreadHistoryRollover:
    """`update_price_spread_history` — the pure state-advance step behind the
    seasonal half of `price_significance()`. See `PriceSpreadHistory`'s
    docstring for why spread uses a running MAX per date but median just takes
    the latest reading."""

    def test_cold_start_has_no_history(self):
        history = heuristic.initial_price_spread_history()
        assert history.daily_spreads_c == ()
        assert history.daily_medians_c == ()
        assert history.current_date is None

    def test_first_observation_seeds_the_current_date_without_banking(self):
        history = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        assert history.current_date == "2026-08-01"
        assert history.current_date_max_spread_c == pytest.approx(0.2)
        assert history.current_date_median_c == pytest.approx(0.3)
        # Nothing completed yet, so nothing banked.
        assert history.daily_spreads_c == ()
        assert history.daily_medians_c == ()

    def test_same_date_spread_only_ever_grows(self):
        history = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        # A smaller spread later the same day must not erase the earlier max
        # — see the module docstring's "0.249 -> 1.139 SEK/kWh on the same
        # date" field observation for why the running max matters.
        history = heuristic.update_price_spread_history(
            history, "2026-08-01", 0.05, 0.3
        )
        assert history.current_date_max_spread_c == pytest.approx(0.2)

    def test_same_date_median_always_takes_the_latest_reading(self):
        """Unlike the spread, the median is a level estimate, not a range —
        see PriceSpreadHistory's docstring for why "latest wins" is the right
        rule here even though it is the wrong one for spread."""
        history = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        history = heuristic.update_price_spread_history(
            history, "2026-08-01", 0.05, 0.1
        )
        assert history.current_date_median_c == pytest.approx(0.1)

    def test_no_op_call_returns_the_identical_object(self):
        """A cycle that changes nothing must be a true no-op — same object,
        not just an equal one — so the coordinator can skip a debounced state
        save with a plain identity check rather than a deep comparison."""
        history = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        same = heuristic.update_price_spread_history(history, "2026-08-01", 0.1, 0.3)
        assert same is history

    def test_date_rollover_banks_the_completed_day(self):
        day_one = heuristic.update_price_spread_history(
            heuristic.initial_price_spread_history(), "2026-08-01", 0.2, 0.3
        )
        day_two = heuristic.update_price_spread_history(
            day_one, "2026-08-02", 0.15, 0.25
        )
        assert day_two.daily_spreads_c == (pytest.approx(0.2),)
        assert day_two.daily_medians_c == (pytest.approx(0.3),)
        assert day_two.current_date == "2026-08-02"
        assert day_two.current_date_max_spread_c == pytest.approx(0.15)
        assert day_two.current_date_median_c == pytest.approx(0.25)

    def test_history_window_trims_to_the_newest_days(self):
        history = heuristic.initial_price_spread_history()
        # The first call only SEEDS the current date rather than banking
        # anything (there is no prior day yet to bank), so getting one more
        # BANKED day than the window holds needs PRICE_SPREAD_HISTORY_DAYS + 2
        # total calls. Plain sequential labels rather than real calendar
        # dates: `update_price_spread_history` only ever compares
        # `local_date` for equality, never parses it (see its docstring).
        for day in range(heuristic.PRICE_SPREAD_HISTORY_DAYS + 2):
            history = heuristic.update_price_spread_history(
                history, f"day-{day:03d}", float(day), 0.3
            )
        assert len(history.daily_spreads_c) == heuristic.PRICE_SPREAD_HISTORY_DAYS
        # Oldest first: day 0's spread (banked first) was pushed out, so the
        # oldest surviving entry is day 1's.
        assert history.daily_spreads_c[0] == pytest.approx(1.0)
        assert history.daily_spreads_c[-1] == pytest.approx(
            float(heuristic.PRICE_SPREAD_HISTORY_DAYS)
        )


class TestPriceSignificance:
    """`price_significance` — the combined taper that replaced a
    relative-only spread ratio which ranked days backwards in money terms and
    degenerated near a zero median (see the module docstring's field data)."""

    def test_cold_start_relative_term_contributes_no_damping(self):
        """Fewer than PRICE_SIGNIFICANCE_COLD_START_DAYS stored days: the
        relative term must not block saving while history accumulates, so
        only the absolute floor decides."""
        history = heuristic.initial_price_spread_history()
        significance, reference = heuristic.price_significance(
            today_spread_c=0.05, today_median_c=0.3, history=history, floor_setting_c=1.0
        )
        assert reference is None
        # relative=1.0, absolute=clamp(0.05/1.0)=0.05 -> combined is the min.
        assert significance == pytest.approx(0.05)

    def test_seasonal_reference_is_the_median_of_stored_spreads(self):
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.1, 0.2, 0.3, 0.4, 0.5),
            daily_medians_c=(0.3,) * 5,
        )
        significance, reference = heuristic.price_significance(
            today_spread_c=0.15,
            today_median_c=0.3,
            history=history,
            # Tiny floor so the absolute term never binds in this test —
            # isolates the relative term's own behaviour.
            floor_setting_c=0.0001,
        )
        assert reference == pytest.approx(0.3)
        assert significance == pytest.approx(0.5)  # 0.15 / 0.3

    def test_median_reference_ignores_one_spike_day(self):
        """Median, not mean — a single outlier day must not drag the
        reference far off what most days actually looked like."""
        normal_days = (0.1, 0.1, 0.1, 0.1, 0.1)
        history_with_spike = heuristic.PriceSpreadHistory(
            daily_spreads_c=normal_days + (50.0,),
            daily_medians_c=(0.3,) * 6,
        )
        _, reference = heuristic.price_significance(
            today_spread_c=0.1,
            today_median_c=0.3,
            history=history_with_spike,
            floor_setting_c=0.0001,
        )
        assert reference == pytest.approx(0.1)

    def test_relative_term_clamps_at_one(self):
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.1,) * 5, daily_medians_c=(0.3,) * 5
        )
        significance, _ = heuristic.price_significance(
            today_spread_c=10.0,
            today_median_c=0.3,
            history=history,
            floor_setting_c=0.0001,
        )
        assert significance == pytest.approx(1.0)

    def test_explicit_floor_overrides_auto(self):
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.05,) * 5, daily_medians_c=(0.9,) * 5
        )
        # Auto floor here would be 0.33 * 0.9 = 0.297; an explicit floor of
        # 0.05 must be used verbatim instead, in the price sensor's own units.
        significance, _ = heuristic.price_significance(
            today_spread_c=0.025,
            today_median_c=0.9,
            history=history,
            floor_setting_c=0.05,
        )
        # relative = 0.025 / 0.05 (reference spread) = 0.5
        # absolute = 0.025 / 0.05 (explicit floor) = 0.5
        assert significance == pytest.approx(0.5)

    def test_auto_floor_uses_the_median_of_stored_daily_prices(self):
        history = heuristic.PriceSpreadHistory(
            # Tiny reference spreads so the relative term clamps to 1.0 and
            # only the absolute (auto-floor) term is what this test measures.
            daily_spreads_c=(0.001,) * 5,
            daily_medians_c=(0.2, 0.3, 0.4, 0.5, 0.6),
        )
        # Auto floor = 0.33 * median(0.2..0.6) = 0.33 * 0.4 = 0.132.
        significance, _ = heuristic.price_significance(
            today_spread_c=0.066,  # half the auto floor
            today_median_c=999.0,  # must be ignored: history exists
            history=history,
            floor_setting_c=0.0,
        )
        assert significance == pytest.approx(0.5, rel=1e-3)

    def test_auto_floor_falls_back_to_todays_median_with_no_history(self):
        history = heuristic.initial_price_spread_history()
        # Auto floor = 0.33 * today's own median (0.3) = 0.099.
        significance, _ = heuristic.price_significance(
            today_spread_c=0.0495,  # half the auto floor
            today_median_c=0.3,
            history=history,
            floor_setting_c=0.0,
        )
        assert significance == pytest.approx(0.5, rel=1e-3)

    def test_auto_floor_still_tapers_when_the_median_is_negative(self):
        """Negative Nordpool day-ahead prices are routine in spring/summer.
        A negative reference must not flip the auto floor's sign (which would
        make `today_spread_c / floor` clamp to 1.0 — no backstop at all, the
        opposite of the intended effect)."""
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.001,) * 5,
            daily_medians_c=(-0.2, -0.3, -0.4, -0.5, -0.6),
        )
        # Auto floor = 0.33 * abs(median(-0.2..-0.6)) = 0.33 * 0.4 = 0.132.
        significance, _ = heuristic.price_significance(
            today_spread_c=0.066,  # half the auto floor
            today_median_c=999.0,  # must be ignored: history exists
            history=history,
            floor_setting_c=0.0,
        )
        assert significance == pytest.approx(0.5, rel=1e-3)

    def test_auto_floor_falls_back_to_todays_negative_median_with_no_history(self):
        history = heuristic.initial_price_spread_history()
        # Auto floor = 0.33 * abs(-0.3) = 0.099.
        significance, _ = heuristic.price_significance(
            today_spread_c=0.0495,  # half the auto floor
            today_median_c=-0.3,
            history=history,
            floor_setting_c=0.0,
        )
        assert significance == pytest.approx(0.5, rel=1e-3)

    def test_absolute_floor_backstops_a_near_zero_median_day(self):
        """The exact degeneracy the module docstring's field data describes:
        a 0.001/0.01 median/peak pair scores a 9.0 RELATIVE ratio (full
        authority), but a tiny absolute spread must still be tapered near 0
        once a real floor backstops it."""
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.05,) * 5, daily_medians_c=(0.05,) * 5
        )
        significance, _ = heuristic.price_significance(
            today_spread_c=0.009,  # 0.01 peak - 0.001 median
            today_median_c=0.001,
            history=history,
            floor_setting_c=0.10,
        )
        assert significance < 0.1

    def test_either_term_being_low_is_enough_to_damp(self):
        """min(), not a product or an average: a day that is significant on
        ONE axis but not the other must still be damped."""
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.1,) * 5, daily_medians_c=(0.3,) * 5
        )
        # High relative (spread far exceeds the seasonal reference) but low
        # absolute (still under an explicit high floor).
        significance, _ = heuristic.price_significance(
            today_spread_c=1.0, today_median_c=0.3, history=history, floor_setting_c=50.0
        )
        assert significance == pytest.approx(1.0 / 50.0)


class TestComputeAppliesSignificanceTaper:
    """Integration: `compute()` must apply `price_significance_factor` to
    BOTH the braking and pre-charge branches, as a continuous taper rather
    than a second hard gate — see the module docstring's "Price significance:
    a taper, not a gate" section."""

    def test_default_factor_of_one_changes_nothing(self):
        """Every pre-existing price test in this file constructs inputs
        without a significance factor and relies on it defaulting to 1.0 (no
        damping) — this pins that default down explicitly."""
        baseline = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        damped = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
                price_significance_factor=1.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert damped.price_adjustment_c == pytest.approx(baseline.price_adjustment_c)

    def test_half_significance_halves_the_braking_shift(self):
        full = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
                price_significance_factor=1.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
                comfort_min_c=0.0,
            ),
        )
        half = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
                price_significance_factor=0.5,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
                comfort_min_c=0.0,
            ),
        )
        assert half.price_shift_applied_c == pytest.approx(
            full.price_shift_applied_c * 0.5
        )

    def test_zero_significance_fully_suppresses_braking(self):
        result = compute(
            make_inputs(
                current_price=5.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=0),
                price_significance_factor=0.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert result.price_adjustment_c == 0.0
        assert not result.price_braking

    def test_zero_significance_also_suppresses_pre_charge(self):
        result = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
                price_significance_factor=0.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        # Pre-charge is still the branch that WOULD have run (the price is
        # cheap with a spike coming), it just banks nothing once damped.
        assert result.precharge_active
        assert result.price_adjustment_c == 0.0

    def test_half_significance_halves_pre_charge_too(self):
        full = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
                price_significance_factor=1.0,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        half = compute(
            make_inputs(
                current_price=1.0,
                price_data_available=True,
                price_forecast=spiky_forecast(spike_at=1),
                rise_minutes=120.0,
                price_significance_factor=0.5,
            ),
            make_params(
                enable_price_compensation=True,
                price_comfort_tier="high",
                cold_caution="low",
            ),
        )
        assert half.price_shift_applied_c == pytest.approx(
            full.price_shift_applied_c * 0.5
        )

    def test_significance_fields_are_echoed_onto_the_result(self):
        result = compute(
            make_inputs(
                price_significance_factor=0.42,
                today_price_spread_c=1.23,
                seasonal_reference_spread_c=0.87,
            ),
            make_params(),
        )
        assert result.price_significance_factor == pytest.approx(0.42)
        assert result.today_price_spread_c == pytest.approx(1.23)
        assert result.seasonal_reference_spread_c == pytest.approx(0.87)


# --- Weather lookahead ------------------------------------------------------

_lookahead_weighted_peak = heuristic._lookahead_weighted_peak
WEATHER_PRERAMP_MAX_C = heuristic.WEATHER_PRERAMP_MAX_C
WEATHER_PRERAMP_EPS_C = heuristic.WEATHER_PRERAMP_EPS_C
SOLAR_PRECOOL_MAX_C = heuristic.SOLAR_PRECOOL_MAX_C
SOLAR_GAIN_C = heuristic.SOLAR_GAIN_C
WIND_GAIN_C_PER_MS = heuristic.WIND_GAIN_C_PER_MS
WIND_DEADBAND_MS = heuristic.WIND_DEADBAND_MS


def temp_forecast(*temps: float, step_h: float = 1.0):
    """`(hours_from_now, temperature)` starting at t=0, one entry per step."""
    return tuple((i * step_h, t) for i, t in enumerate(temps))


def lookahead_params(**overrides) -> HeuristicParams:
    defaults = dict(enable_weather_lookahead=True)
    defaults.update(overrides)
    return make_params(**defaults)


class TestLookaheadWeightedPeak:
    def test_weights_by_proximity(self):
        # Same score at 1 h and 3 h inside a 4 h window: the nearer one wins.
        near, near_h = _lookahead_weighted_peak(((1.0, 1.0),), 240.0, lambda v: v)
        far, far_h = _lookahead_weighted_peak(((3.0, 1.0),), 240.0, lambda v: v)
        assert near == pytest.approx(0.75)
        assert far == pytest.approx(0.25)
        assert near_h == 1.0 and far_h == 3.0

    def test_entries_beyond_the_window_are_ignored(self):
        peak, when = _lookahead_weighted_peak(((5.0, 10.0),), 240.0, lambda v: v)
        assert peak == 0.0
        assert when is None

    def test_entries_at_or_before_now_are_ignored(self):
        peak, when = _lookahead_weighted_peak(
            ((-1.0, 10.0), (0.0, 10.0)), 240.0, lambda v: v
        )
        assert peak == 0.0
        assert when is None

    def test_empty_and_none_series_do_nothing(self):
        assert _lookahead_weighted_peak(None, 240.0, lambda v: v) == (0.0, None)
        assert _lookahead_weighted_peak((), 240.0, lambda v: v) == (0.0, None)

    def test_non_positive_lead_does_nothing(self):
        assert _lookahead_weighted_peak(((1.0, 5.0),), 0.0, lambda v: v) == (0.0, None)
        assert _lookahead_weighted_peak(((1.0, 5.0),), -60.0, lambda v: v) == (0.0, None)

    def test_reports_the_peak_not_the_last_entry(self):
        peak, when = _lookahead_weighted_peak(
            ((1.0, 4.0), (2.0, 1.0)), 240.0, lambda v: v
        )
        assert peak == pytest.approx(3.0)
        assert when == 1.0


class TestWeatherPreRamp:
    def test_a_drop_inside_the_window_ramps(self):
        result = compute(
            make_inputs(
                outdoor_forecast=temp_forecast(0.0, -4.0),
                rise_minutes=120.0,
            ),
            lookahead_params(),
        )
        # 4 degC colder at t=1 h inside a 2 h lead: 4 * (1 - 0.5).
        assert result.outdoor_preramp_c == pytest.approx(-2.0)
        assert result.weather_preramp_c == pytest.approx(-2.0)
        assert result.weather_preramp_in_min == pytest.approx(60.0)

    def test_the_same_drop_beyond_the_rise_time_does_nothing(self):
        result = compute(
            make_inputs(
                outdoor_forecast=temp_forecast(0.0, 0.0, 0.0, 0.0, -4.0),
                rise_minutes=120.0,
            ),
            lookahead_params(),
        )
        assert result.weather_preramp_c == 0.0
        assert not result.weather_preramp_active

    def test_a_forecast_rise_produces_exactly_zero(self):
        result = compute(
            make_inputs(
                outdoor_forecast=temp_forecast(0.0, 6.0),
                rise_minutes=120.0,
            ),
            lookahead_params(),
        )
        assert result.outdoor_preramp_c == 0.0
        assert result.weather_preramp_c == 0.0

    def test_the_ramp_decays_as_the_event_arrives(self):
        far = compute(
            make_inputs(outdoor_forecast=((0.0, 0.0), (1.5, -4.0)), rise_minutes=120.0),
            lookahead_params(),
        )
        near = compute(
            make_inputs(outdoor_forecast=((0.0, 0.0), (0.5, -4.0)), rise_minutes=120.0),
            lookahead_params(),
        )
        arrived = compute(
            make_inputs(outdoor_forecast=((0.0, -4.0), (1.0, -4.0)), rise_minutes=120.0),
            lookahead_params(),
        )
        assert near.weather_preramp_c < far.weather_preramp_c < 0.0
        # Once the drop IS the current forecast value it is the steady-state
        # raw reading's problem, not the lookahead's.
        assert arrived.weather_preramp_c == 0.0

    def test_a_constant_forecast_bias_produces_exactly_zero(self):
        """The regression that matters most: a forecast running a constant
        offset below the wall sensor must not produce a permanent ramp."""
        for bias in (-5.0, 0.0, 5.0):
            result = compute(
                make_inputs(
                    raw_outdoor_temp_c=3.0,
                    outdoor_forecast=temp_forecast(*(bias for _ in range(4))),
                    rise_minutes=180.0,
                ),
                lookahead_params(),
            )
            assert result.weather_preramp_c == 0.0

    def test_rising_wind_ramps_at_the_existing_gain(self):
        result = compute(
            make_inputs(
                wind_forecast_ms=((0.0, 0.0), (1.0, 10.0)),
                rise_minutes=120.0,
            ),
            lookahead_params(enable_wind_input=True),
        )
        assert result.wind_preramp_c == pytest.approx(
            -WIND_GAIN_C_PER_MS * (10.0 - WIND_DEADBAND_MS) * 0.5
        )

    def test_the_sun_going_in_does_not_pre_ramp(self):
        # Losing sun is handled reactively (sun_adjustment_c recomputes every
        # cycle) — it no longer feeds this bucket at all.
        result = compute(
            make_inputs(
                solar_forecast=((0.0, 1.0), (1.0, 0.0)),
                rise_minutes=120.0,
            ),
            lookahead_params(enable_solar_input=True),
        )
        assert result.weather_preramp_c == 0.0

    def test_the_ramp_reaches_the_published_value(self):
        without = compute(make_inputs(), make_params())
        with_ramp = compute(
            make_inputs(outdoor_forecast=temp_forecast(0.0, -4.0), rise_minutes=120.0),
            lookahead_params(),
        )
        assert with_ramp.compensated_outdoor_temp_c == pytest.approx(
            without.compensated_outdoor_temp_c - 2.0
        )


class TestPreRampBudget:
    def _both(self, **params):
        return compute(
            make_inputs(
                outdoor_forecast=((0.0, 0.0), (0.1, -8.0)),
                wind_forecast_ms=((0.0, 0.0), (0.1, 20.0)),
                rise_minutes=120.0,
            ),
            lookahead_params(enable_wind_input=True, **params),
        )

    def test_two_simultaneous_ramps_clamp_to_the_shared_budget(self):
        result = self._both()
        assert result.weather_preramp_c == pytest.approx(-WEATHER_PRERAMP_MAX_C)

    def test_components_are_still_reported_unclamped(self):
        result = self._both()
        raw_sum = result.outdoor_preramp_c + result.wind_preramp_c
        assert raw_sum < -WEATHER_PRERAMP_MAX_C
        assert result.outdoor_preramp_c < 0.0
        assert result.wind_preramp_c < 0.0


class TestPreRampGating:
    def _inputs(self, **overrides):
        defaults = dict(
            outdoor_forecast=temp_forecast(0.0, -4.0),
            wind_forecast_ms=((0.0, 0.0), (1.0, 10.0)),
            solar_forecast=((0.0, 0.0), (1.0, 1.0)),
            rise_minutes=120.0,
            fall_minutes=120.0,
        )
        defaults.update(overrides)
        return make_inputs(**defaults)

    def test_lookahead_off_is_bit_identical_to_no_forecast_at_all(self):
        off = compute(self._inputs(), make_params())
        bare = compute(make_inputs(rise_minutes=120.0, fall_minutes=120.0), make_params())
        assert off.weather_preramp_c == 0.0
        assert off.outdoor_preramp_c == 0.0
        assert off.wind_preramp_c == 0.0
        assert off.weather_preramp_in_min is None
        assert off.sun_precool_c == 0.0
        assert off.sun_precool_in_min is None
        assert off.compensated_outdoor_temp_c == pytest.approx(
            bare.compensated_outdoor_temp_c
        )

    def test_wind_off_contributes_exactly_zero(self):
        result = compute(
            self._inputs(outdoor_forecast=None, solar_forecast=None),
            lookahead_params(enable_wind_input=False),
        )
        assert result.wind_preramp_c == 0.0
        assert result.weather_preramp_c == 0.0

    def test_solar_off_contributes_exactly_zero(self):
        result = compute(
            self._inputs(outdoor_forecast=None, wind_forecast_ms=None),
            lookahead_params(enable_solar_input=False),
        )
        assert result.sun_precool_c == 0.0

    def test_missing_series_ramps_nothing(self):
        result = compute(
            make_inputs(rise_minutes=120.0, fall_minutes=120.0),
            lookahead_params(enable_wind_input=True, enable_solar_input=True),
        )
        assert result.weather_preramp_c == 0.0
        assert result.sun_precool_c == 0.0

    def test_hard_limit_short_circuits_on_raw_and_zeroes_the_ramp(self):
        result = compute(
            self._inputs(raw_outdoor_temp_c=22.0),
            lookahead_params(),
        )
        assert result.heating_hard_limit_engaged
        assert result.weather_preramp_c == 0.0
        assert result.outdoor_preramp_c == 0.0
        assert result.sun_precool_c == 0.0


class TestPreRampFreezesLearner:
    def test_flag_set_above_eps(self):
        result = compute(
            make_inputs(outdoor_forecast=temp_forecast(0.0, -4.0), rise_minutes=120.0),
            lookahead_params(),
        )
        assert abs(result.weather_preramp_c) > WEATHER_PRERAMP_EPS_C
        assert result.weather_preramp_active
        assert "pre-ramp" in result.reason

    def test_flag_clear_below_eps(self):
        result = compute(
            make_inputs(
                outdoor_forecast=temp_forecast(0.0, -0.2),
                rise_minutes=120.0,
            ),
            lookahead_params(),
        )
        assert 0.0 < abs(result.weather_preramp_c) <= WEATHER_PRERAMP_EPS_C
        assert not result.weather_preramp_active
        assert "pre-ramp" not in result.reason


# --- Sun pre-cool -------------------------------------------------------


class TestSunPreCool:
    def test_a_rise_inside_the_window_precools(self):
        result = compute(
            make_inputs(
                solar_forecast=((0.0, 0.0), (1.0, 1.0)),
                fall_minutes=120.0,
            ),
            lookahead_params(enable_solar_input=True),
        )
        # 3 degC more sun-equivalent at t=1 h inside a 2 h fall window:
        # SOLAR_GAIN_C * (1 - 0.5).
        assert result.sun_precool_c == pytest.approx(SOLAR_GAIN_C * 0.5)
        assert result.sun_precool_in_min == pytest.approx(60.0)

    def test_the_same_rise_beyond_the_fall_time_does_nothing(self):
        result = compute(
            make_inputs(
                solar_forecast=temp_forecast(0.0, 0.0, 0.0, 0.0, 1.0),
                fall_minutes=120.0,
            ),
            lookahead_params(enable_solar_input=True),
        )
        assert result.sun_precool_c == 0.0
        assert not result.sun_precool_active

    def test_a_forecast_drop_produces_exactly_zero(self):
        # Losing sun is the reactive term's job now, not this one's.
        result = compute(
            make_inputs(
                solar_forecast=((0.0, 1.0), (1.0, 0.0)),
                fall_minutes=120.0,
            ),
            lookahead_params(enable_solar_input=True),
        )
        assert result.sun_precool_c == 0.0

    def test_the_precool_decays_as_the_event_arrives(self):
        far = compute(
            make_inputs(solar_forecast=((0.0, 0.0), (1.5, 1.0)), fall_minutes=120.0),
            lookahead_params(enable_solar_input=True),
        )
        near = compute(
            make_inputs(solar_forecast=((0.0, 0.0), (0.5, 1.0)), fall_minutes=120.0),
            lookahead_params(enable_solar_input=True),
        )
        arrived = compute(
            make_inputs(solar_forecast=((0.0, 1.0), (1.0, 1.0)), fall_minutes=120.0),
            lookahead_params(enable_solar_input=True),
        )
        assert near.sun_precool_c > far.sun_precool_c > 0.0
        # Once the rise IS the current forecast value it is the reactive
        # term's problem, not the lookahead's.
        assert arrived.sun_precool_c == 0.0

    def test_a_constant_forecast_bias_produces_exactly_zero(self):
        for bias in (0.0, 0.5, 1.0):
            result = compute(
                make_inputs(
                    solar_forecast=temp_forecast(*(bias for _ in range(4))),
                    fall_minutes=180.0,
                ),
                lookahead_params(enable_solar_input=True),
            )
            assert result.sun_precool_c == 0.0

    def test_the_precool_reaches_the_published_value(self):
        without = compute(make_inputs(), make_params())
        with_precool = compute(
            make_inputs(solar_forecast=((0.0, 0.0), (1.0, 1.0)), fall_minutes=120.0),
            lookahead_params(enable_solar_input=True),
        )
        assert with_precool.compensated_outdoor_temp_c == pytest.approx(
            without.compensated_outdoor_temp_c + SOLAR_GAIN_C * 0.5
        )

    def test_precool_clamps_to_its_own_budget(self):
        result = compute(
            make_inputs(
                solar_forecast=((0.0, 0.0), (0.01, 10.0)),
                fall_minutes=120.0,
            ),
            lookahead_params(enable_solar_input=True),
        )
        assert result.sun_precool_c == pytest.approx(SOLAR_PRECOOL_MAX_C)

    def test_lookahead_off_contributes_exactly_zero(self):
        result = compute(
            make_inputs(solar_forecast=((0.0, 0.0), (1.0, 1.0)), fall_minutes=120.0),
            make_params(enable_solar_input=True),
        )
        assert result.sun_precool_c == 0.0
        assert result.sun_precool_in_min is None


class TestSunPreCoolFreezesLearner:
    def test_flag_set_above_eps(self):
        result = compute(
            make_inputs(solar_forecast=((0.0, 0.0), (1.0, 1.0)), fall_minutes=120.0),
            lookahead_params(enable_solar_input=True),
        )
        assert abs(result.sun_precool_c) > WEATHER_PRERAMP_EPS_C
        assert result.sun_precool_active
        assert "pre-cool" in result.reason

    def test_flag_clear_below_eps(self):
        result = compute(
            make_inputs(solar_forecast=((0.0, 0.0), (1.0, 0.06)), fall_minutes=120.0),
            lookahead_params(enable_solar_input=True),
        )
        assert 0.0 < abs(result.sun_precool_c) <= WEATHER_PRERAMP_EPS_C
        assert not result.sun_precool_active
        assert "pre-cool" not in result.reason
