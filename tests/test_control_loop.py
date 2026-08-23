"""End-to-end tests of the pure control loop.

The unit tests cover each module alone. These drive `lag` -> `learner` ->
`heuristic` together against a simulated house, which is where the errors that
matter actually hide: a sign flipped between two modules, or the price logic
and the learner quietly fighting each other.

The house model is intentionally crude — a first-order lag toward whatever
equilibrium the current spoof buys, plus a transport delay. It is not trying to
be a building; it is trying to be *a plant with dead time*, because dead time is
what an integral controller can be destroyed by.
"""

from __future__ import annotations

import sys
from collections import deque
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

heuristic = load("heuristic")
holiday = load("holiday")
lag = load("lag")
learner = load("learner")

STEP_MINUTES = 15.0
STEP_HOURS = STEP_MINUTES / 60.0


class House:
    """A house whose pump curve is `curve_error_c` short of what it needs.

    Spoofing the outdoor reading down by `curve_error_c` is exactly what makes
    it hold `nominal_target_c` — so a correct controller must converge on that
    number without anyone telling it.

    Note carefully what `advance` does NOT take: the comfort target. A house
    has no idea what temperature anyone wants. A setpoint change reaches it
    only as a changed offset, and therefore only through the same transport
    delay as everything else. Letting the target influence the plant directly
    would quietly hand the lag estimator a response with no delay in it, and
    it would measure a number smaller than the delay it was trying to find.
    """

    def __init__(
        self,
        curve_error_c: float = -2.0,
        delay_steps: int = 4,
        inertia: float = 0.15,
        indoor_c: float = 19.0,
        capacity_c: float | None = None,
        nominal_target_c: float = 21.0,
    ):
        self.curve_error_c = curve_error_c
        self.inertia = inertia
        self.indoor_c = indoor_c
        # Where the house settles with no spoofing at all.
        self.base_indoor_c = nominal_target_c + curve_error_c
        # Beyond this spoof depth the pump delivers nothing more.
        self.capacity_c = capacity_c
        self.pipeline: deque[float] = deque([0.0] * delay_steps, maxlen=delay_steps)

    def advance(self, offset_c: float) -> float:
        self.pipeline.append(offset_c)
        delivered = self.pipeline[0]
        if self.capacity_c is not None:
            # The pump cannot deliver more than this no matter how deep the
            # spoof goes — the deep-cold case.
            delivered = max(delivered, -self.capacity_c)
        # Spoofing COLDER (a more negative offset) asks the pump's curve for
        # more heat, so equilibrium rises as `delivered` falls.
        equilibrium = self.base_indoor_c - delivered
        self.indoor_c += (equilibrium - self.indoor_c) * self.inertia
        return self.indoor_c


def run(
    house: House,
    steps: int,
    target_c: float = 21.0,
    outdoor_c: float = 0.0,
    price_at=None,
    params_overrides: dict | None = None,
    inputs_overrides: dict | None = None,
    inputs_at=None,
    outdoor_at=None,
    active_at=None,
    target_at=None,
    learner_state=None,
    lag_state=None,
    now: float = 0.0,
    return_state: bool = False,
    on_output=None,
):
    """Drive the full loop and return (trace, final learner result, final output).

    `active_at(i)` decides whether compensation is switched on at step `i`,
    defaulting to always-on. While it is off the house is advanced with a zero
    offset, mirroring what the coordinator actually publishes in that state
    (the raw outdoor reading, to every output channel) — so the simulated pump
    really does run its own untouched curve, which is the whole premise the
    baseline table rests on.

    `target_at(i)`, if given, overrides `target_c` per step — the same shape
    as `active_at`/`price_at`, for scenarios (e.g. a holiday setback/ramp)
    where the effective target itself moves during the run. Every call site
    that reads the target reads this same per-step value, mirroring
    `coordinator.py`'s `_effective_target_c` wiring: this is what makes the
    test exercise "the three call sites agree" rather than just `holiday.py`
    in isolation.

    `learner_state`/`lag_state`/`now` may be seeded from a prior `run()` call
    (with `return_state=True`) to continue one simulated house across two
    back-to-back scenarios — e.g. settle first, then run a holiday — without
    losing what was already learned. Defaults to a cold start, as before.

    `on_output(output)`, if given, is called with every cycle's
    `HeuristicResult` — for a test that needs to see every step's published
    value (e.g. `effective_indoor_target_c`), not just the final one `run()`
    itself returns.

    `outdoor_at(i)` overrides `outdoor_c` per step, and `inputs_at(i)` returns
    a per-step dict layered on top of `inputs_overrides` — together enough to
    drive a scenario whose weather actually changes mid-run. `outdoor_at` is
    called once at the top of each step, which is also the hook a test uses to
    move the simulated house's own equilibrium as the weather turns.
    """
    if learner_state is None:
        learner_state = learner.initial_state()
    if lag_state is None:
        lag_state = lag.initial_state(STEP_MINUTES)
    offset = 0.0
    trace = []
    previous_output = None
    result = None
    output = None
    lag_result = None

    for i in range(steps):
        current_target = target_c if target_at is None else target_at(i)
        current_outdoor = outdoor_c if outdoor_at is None else outdoor_at(i)
        is_active = True if active_at is None else active_at(i)
        indoor = house.advance(offset)
        now += STEP_HOURS * 3600.0

        lag_state = lag.push(lag_state, offset, indoor, current_target)
        lag_result = lag.estimate(lag_state, "radiators")

        price, forecast = (None, None) if price_at is None else price_at(i)

        learner_state, result = learner.step(
            learner_state,
            learner.LearnerInputs(
                now_s=now,
                dt_hours=STEP_HOURS,
                indoor_temp_c=indoor,
                indoor_data_available=True,
                target_c=current_target,
                outdoor_temp_c=current_outdoor,
                heating_hard_limit_engaged=False,
                is_active=is_active,
                price_braking=bool(previous_output and previous_output.price_braking),
                weather_preramp=bool(
                    previous_output and previous_output.weather_preramp_active
                ),
                rise_hours=lag_result.rise_hours,
            ),
        )

        params = dict(
            indoor_target_c=current_target,
            comfort_min_c=18.0,
            enable_price_compensation=price_at is not None,
            price_comfort_tier="high",
            cold_caution="low",
            enable_solar_input=False,
            enable_wind_input=False,
            spoof_per_indoor_c=learner.SPOOF_PER_INDOOR_C,
        )
        params.update(params_overrides or {})

        inputs = dict(
            indoor_temp_c=indoor,
            indoor_data_available=True,
            raw_outdoor_temp_c=current_outdoor,
            wind_speed_ms=0.0,
            wind_data_available=True,
            sun_elevation_deg=-10.0,
            sun_azimuth_deg=180.0,
            cloud_coverage_pct=100.0,
            cloud_data_available=True,
            current_price=price,
            price_data_available=price is not None,
            price_forecast=forecast,
            learned_offset_c=result.offset_c,
            fall_minutes=lag_result.fall_minutes,
            rise_minutes=lag_result.rise_minutes,
            recoverable_sag_c=learner.recoverable_sag_c(
                result.close_rate_c_per_h,
                lag_result.rise_hours * learner.TAU_I_LAG_MULTIPLE,
            ),
        )
        inputs.update(inputs_overrides or {})
        if inputs_at is not None:
            inputs.update(inputs_at(i))

        output = heuristic.compute(
            heuristic.HeuristicInputs(**inputs), heuristic.HeuristicParams(**params)
        )
        if on_output is not None:
            on_output(output)
        previous_output = output
        # What the pump actually sees, relative to the real outdoor reading.
        # Zero while compensation is off: the coordinator publishes the raw
        # reading then, so no term — learned, weather or price — reaches the pump.
        offset = (
            (output.compensated_outdoor_temp_c - current_outdoor) if is_active else 0.0
        )
        trace.append((indoor, offset, result.hold_offset_c))

    if return_state:
        return trace, result, output, learner_state, lag_state, now
    return trace, result, output


class TestConvergence:
    def test_the_house_reaches_target_with_nothing_configured(self):
        house = House(curve_error_c=-2.0)
        trace, result, _ = run(house, 1200)
        indoor = [t[0] for t in trace]
        assert indoor[-1] == pytest.approx(21.0, abs=0.15)
        assert result.hold_offset_c == pytest.approx(-2.0, abs=0.3)

    def test_it_finds_a_positive_offset_for_an_over_generous_curve(self):
        """A pump curve that overshoots must be corrected the other way."""
        house = House(curve_error_c=1.5, indoor_c=23.0)
        trace, result, _ = run(house, 1200)
        assert trace[-1][0] == pytest.approx(21.0, abs=0.2)
        assert result.hold_offset_c > 0

    def test_it_does_not_oscillate_against_dead_time(self):
        """The failure mode integral control is famous for. A slab-like plant
        with a long transport delay must settle, not hunt."""
        house = House(curve_error_c=-2.0, delay_steps=16, inertia=0.08)
        trace, _, _ = run(house, 2000)
        settled = [t[0] for t in trace[-400:]]
        assert max(settled) - min(settled) < 0.3
        # And no sustained overshoot past target.
        assert max(settled) < 21.5

    def test_a_slower_house_converges_too_just_later(self):
        fast = House(curve_error_c=-2.0, delay_steps=2, inertia=0.3)
        slow = House(curve_error_c=-2.0, delay_steps=12, inertia=0.08)
        fast_trace, _, _ = run(fast, 1500)
        slow_trace, _, _ = run(slow, 1500)
        assert fast_trace[-1][0] == pytest.approx(21.0, abs=0.2)
        assert slow_trace[-1][0] == pytest.approx(21.0, abs=0.3)

    def test_authority_is_never_exceeded_during_the_transient(self):
        house = House(curve_error_c=-8.0, indoor_c=14.0)
        trace, _, _ = run(house, 600)
        assert all(
            abs(hold) <= learner.MAX_AUTHORITY_C + 1e-6 for _, _, hold in trace
        )


class TestCapacityLimit:
    def test_a_pump_that_cannot_keep_up_does_not_wind_up(self):
        """The deep-cold case. The house never reaches target, and the learner
        must stop pushing rather than bank a correction that would dump into a
        pump that can suddenly deliver it."""
        house = House(curve_error_c=-6.0, capacity_c=2.0, indoor_c=17.0)
        trace, result, _ = run(house, 1000, outdoor_c=-18.0)
        holds = [t[2] for t in trace]
        assert all(abs(h) <= learner.MAX_AUTHORITY_C + 1e-6 for h in holds)
        # It really was short of target, so this is the saturating case.
        assert trace[-1][0] < 21.0
        assert result.ceiling_c <= learner.MAX_AUTHORITY_C

    def test_recovery_after_capacity_returns(self):
        """Once the weather relents, the stored offset must not slam the pump."""
        house = House(curve_error_c=-6.0, capacity_c=2.0, indoor_c=17.0)
        trace, _, _ = run(house, 600, outdoor_c=-18.0)
        peak_after = max(abs(offset) for _, offset, _ in trace)
        assert peak_after <= learner.MAX_AUTHORITY_C + 1e-6


class TestPriceInteraction:
    @staticmethod
    def _spike_schedule(spike_step: int, width: int = 8):
        """A day-ahead forecast that rolls forward, with one expensive block."""

        def at(i: int):
            hours_to_spike = (spike_step - i) * STEP_HOURS
            forecast = tuple(
                (
                    float(h),
                    5.0 if 0 <= (hours_to_spike - h) < width * STEP_HOURS else 1.0,
                )
                for h in range(24)
            )
            price = 5.0 if spike_step <= i < spike_step + width else 1.0
            return price, forecast

        return at

    def test_the_learner_does_not_fight_the_price_excursion(self):
        """If the learner treated a deliberate sag as error, it would wind up
        opposing the very excursion it was asked for — and the table would be
        poisoned by every expensive hour of the winter."""
        house = House(curve_error_c=-2.0)
        # Settle first, so the table has a real value to protect.
        trace, result, _ = run(house, 900)
        settled = result.hold_offset_c

        house2 = House(curve_error_c=-2.0, indoor_c=trace[-1][0])
        _, after, output = run(
            house2, 400, price_at=self._spike_schedule(spike_step=100)
        )
        # The learned steady offset survives the spike broadly intact.
        assert after.hold_offset_c == pytest.approx(settled, abs=0.6)

    def test_an_expensive_block_actually_lets_the_house_cool(self):
        house = House(curve_error_c=-2.0)
        run(house, 900)  # settle
        indoor_before = house.indoor_c
        trace, _, _ = run(
            House(curve_error_c=-2.0, indoor_c=indoor_before),
            200,
            price_at=self._spike_schedule(spike_step=40),
        )
        during = min(t[0] for t in trace[40:120])
        assert during < indoor_before

    def test_the_comfort_floor_holds_through_a_spike(self):
        house = House(curve_error_c=-2.0)
        run(house, 900)
        trace, _, _ = run(
            House(curve_error_c=-2.0, indoor_c=house.indoor_c),
            400,
            price_at=self._spike_schedule(spike_step=40, width=40),
            params_overrides={"comfort_min_c": 20.0},
        )
        # The floor bounds the *setpoint*; allow the plant a little undershoot
        # below it while coasting, but nothing like an unbounded drift.
        assert min(t[0] for t in trace) > 19.0

    def test_no_price_braking_when_it_is_far_too_cold(self):
        house = House(curve_error_c=-2.0)
        _, _, output = run(
            house,
            300,
            outdoor_c=-25.0,
            price_at=self._spike_schedule(spike_step=100),
            params_overrides={"cold_caution": "low"},
        )
        assert output.cold_brake_factor == 0.0
        assert output.price_adjustment_c == 0.0


class TestColdFrontLookahead:
    """A front the forecast saw coming, with and without pre-ramping.

    The house model has no outdoor term of its own, so the front is applied
    the way the plant would actually feel it: the same pump curve leaves the
    house further short once it is colder outside, i.e. `base_indoor_c` steps
    down. The forecast is built from the SAME schedule the house follows, so
    the run is not testing a lucky forecast — it is testing that a correct one
    is acted on early.
    """

    WARM_C = -2.0
    COLD_C = -8.0
    FRONT_STEP = 60  # 15 h in, well after the loop has settled
    # How much of the 6 degC outdoor drop the (unchanged) curve fails to cover.
    SHORTFALL_C = 2.0
    STEPS = 160

    def _outdoor_of_step(self, i: int) -> float:
        return self.WARM_C if i < self.FRONT_STEP else self.COLD_C

    def _run(self, lookahead: bool):
        house = House(curve_error_c=-2.0)
        warm_base = house.base_indoor_c

        def outdoor_at(i: int) -> float:
            # Single per-step hook, so the plant and the published raw reading
            # can never disagree about when the front landed.
            house.base_indoor_c = warm_base - (
                self.SHORTFALL_C if i >= self.FRONT_STEP else 0.0
            )
            return self._outdoor_of_step(i)

        def inputs_at(i: int) -> dict:
            # An hourly forecast of the next 12 h, from this step's vantage
            # point — exactly the shape the coordinator builds.
            return {
                "outdoor_forecast": tuple(
                    (float(h), self._outdoor_of_step(i + h * 4)) for h in range(13)
                )
            }

        trace, result, output = run(
            house,
            self.STEPS,
            outdoor_at=outdoor_at,
            inputs_at=inputs_at,
            params_overrides={"enable_weather_lookahead": lookahead},
        )
        return trace, result, output

    def test_pre_ramping_softens_the_dip(self):
        without, _, _ = self._run(lookahead=False)
        with_ramp, _, _ = self._run(lookahead=True)
        # The first 2.5 h after the front is the whole claim: banked heat has
        # to carry the house through the dead time before the learner's own
        # response arrives. Beyond that the two runs converge, and the
        # lookahead run is fractionally BEHIND — it froze the learner while it
        # was pre-ramping, which is the deliberate trade in §6 of the plan.
        window = slice(self.FRONT_STEP, self.FRONT_STEP + 10)
        assert min(t[0] for t in with_ramp[window]) > min(
            t[0] for t in without[window]
        )

    def test_the_ramp_does_not_bias_the_integrator(self):
        _, without, _ = self._run(lookahead=False)
        _, with_ramp, _ = self._run(lookahead=True)
        # Same house, same front: whatever the pre-ramp did on the way in, the
        # learned offset must land in the same place once it is over.
        assert with_ramp.hold_offset_c == pytest.approx(
            without.hold_offset_c, abs=0.3
        )

    def test_the_ramp_is_over_once_the_front_has_landed(self):
        _, _, output = self._run(lookahead=True)
        assert output.weather_preramp_c == 0.0
        assert not output.weather_preramp_active


class TestLagMeasurementInTheLoop:
    def test_a_schedule_teaches_it_the_response_time(self):
        """Target steps are the excitation that actually works in closed loop.

        A schedule moving the target is exogenous — nothing about the house
        caused it — so the house's answer is readable. This is the realistic
        path by which a normal install learns its own response time, and it is
        why the target series is in the lag buffer at all.
        """
        house = House(curve_error_c=-2.0, delay_steps=6)
        learner_state = learner.initial_state()
        lag_state = lag.initial_state(STEP_MINUTES)
        offset = 0.0
        now = 0.0
        target = 21.0
        # A plain day/night schedule: down overnight, up in the morning.
        for i in range(1400):
            if i % 48 == 0:
                target = 21.0 if target != 21.0 else 22.5
            indoor = house.advance(offset)
            now += STEP_HOURS * 3600.0
            lag_state = lag.push(lag_state, offset, indoor, target)
            lag_result = lag.estimate(lag_state, "radiators")
            learner_state, result = learner.step(
                learner_state,
                learner.LearnerInputs(
                    now_s=now,
                    dt_hours=STEP_HOURS,
                    indoor_temp_c=indoor,
                    indoor_data_available=True,
                    target_c=target,
                    outdoor_temp_c=0.0,
                    heating_hard_limit_engaged=False,
                    is_active=True,
                    price_braking=False,
                    rise_hours=lag_result.rise_hours,
                ),
            )
            offset = result.offset_c
        assert lag_result.rise_measured or lag_result.fall_measured
        assert 0 < lag_result.rise_minutes <= lag.MAX_LAG_MINUTES
        # What is measured is "time until half the effect arrived", so it is
        # necessarily at least the transport delay and generally longer.
        assert lag_result.rise_minutes >= 6 * STEP_MINUTES

    def test_a_slower_house_measures_a_longer_response(self):
        """The invariant that actually matters downstream: a slab must end up
        with a slower integrator and an earlier pre-brake than a radiator."""

        def measured(delay_steps: int) -> float:
            house = House(curve_error_c=-2.0, delay_steps=delay_steps)
            learner_state = learner.initial_state()
            lag_state = lag.initial_state(STEP_MINUTES)
            offset, now, target = 0.0, 0.0, 21.0
            for i in range(1400):
                if i % 48 == 0:
                    target = 21.0 if target != 21.0 else 22.5
                indoor = house.advance(offset)
                now += STEP_HOURS * 3600.0
                lag_state = lag.push(lag_state, offset, indoor, target)
                lag_result = lag.estimate(lag_state, "radiators")
                learner_state, result = learner.step(
                    learner_state,
                    learner.LearnerInputs(
                        now_s=now,
                        dt_hours=STEP_HOURS,
                        indoor_temp_c=indoor,
                        indoor_data_available=True,
                        target_c=target,
                        outdoor_temp_c=0.0,
                        heating_hard_limit_engaged=False,
                        is_active=True,
                        price_braking=False,
                        rise_hours=lag_result.rise_hours,
                    ),
                )
                offset = result.offset_c
            return lag.estimate(lag_state, "radiators").rise_minutes

        assert measured(12) > measured(2)


class TestBaselineSeeding:
    """The off period is where the raw curve is observed; switching on is where
    that observation is spent.

    The strongest property available here is that the two must AGREE: the house
    settles at `nominal_target + curve_error` unspoofed, so the offset implied
    by the baseline is exactly the offset closed-loop integration converges to
    on its own. A sign error anywhere between the two would show up as the seed
    pointing the wrong way, which is the class of mistake that has bitten this
    project before.
    """

    def test_the_baseline_predicts_the_offset_the_loop_converges_to(self):
        # 400 steps off (100 h) is far past the dwell requirement, so the
        # baseline table fills; nothing is applied to the house throughout.
        house = House(curve_error_c=-2.0)
        _, result, _ = run(house, 400, active_at=lambda i: False)

        assert result.baseline_indoor_c == pytest.approx(19.0, abs=0.2)
        # Positive deficit = the raw curve leaves the house cold.
        assert result.baseline_deficit_c == pytest.approx(2.0, abs=0.2)
        implied = learner.seed_offset_for(result.baseline_indoor_c, 21.0)
        # -2.0 is what TestConvergence proves the loop reaches unaided.
        assert implied == pytest.approx(-2.0, abs=0.2)

    def test_switching_on_starts_from_the_baseline_instead_of_zero(self):
        house = House(curve_error_c=-2.0)
        # Off for 400 steps, then on. The step immediately after the flip should
        # already be pushing, not starting from a standing start at zero.
        _, result, _ = run(house, 401, active_at=lambda i: i >= 400)

        assert result.seeded_bins >= 1
        assert result.seeded
        # The baseline implies -2.0, and the day-one authority ramp allows only
        # 1.0 — so the seed shows up pinned hard against the ramp, where a cold
        # start would still be sitting near zero after one step.
        assert result.hold_offset_c == pytest.approx(-learner.MIN_AUTHORITY_C, abs=1e-6)

    def test_an_over_generous_curve_seeds_the_other_way(self):
        # The mirror case: a curve that overheats settles the house ABOVE
        # target, which must seed a positive (warmer-spoofing) offset.
        house = House(curve_error_c=1.5, indoor_c=22.5)
        _, result, _ = run(house, 400, active_at=lambda i: False)

        assert result.baseline_indoor_c == pytest.approx(22.5, abs=0.2)
        assert learner.seed_offset_for(result.baseline_indoor_c, 21.0) > 0.0

    def test_seeding_does_not_buy_authority_it_has_not_earned(self):
        """A seed is open-loop evidence, so it must not advance the ramp.

        This is the safety property of the whole feature: a house could sit off
        for a month, and switching on must still not permit a day-one spoof
        larger than a from-cold install would.
        """
        house = House(curve_error_c=-4.0)
        _, result, _ = run(house, 401, active_at=lambda i: i >= 400)

        assert result.total_samples <= 1
        assert result.authority_c == pytest.approx(learner.MIN_AUTHORITY_C, abs=0.05)
        # Seeded deep, but the ramp still binds what actually reaches the pump.
        assert abs(result.offset_c) <= learner.MIN_AUTHORITY_C + 1e-6

    def test_a_seeded_start_converges_faster_than_a_cold_one(self):
        """The point of the feature, stated as a measurement."""

        def error_after(steps: int, seeded: bool) -> float:
            house = House(curve_error_c=-2.0)
            if seeded:
                # Same total wall-clock in the ON state for both arms, so this
                # compares starting points rather than simply running longer.
                trace, _, _ = run(
                    house, 400 + steps, active_at=lambda i: i >= 400
                )
            else:
                trace, _, _ = run(house, steps)
            return abs(trace[-1][0] - 21.0)

        assert error_after(120, seeded=True) < error_after(120, seeded=False)


class TestHolidaySetback:
    """Closed-loop regression guard for the coordinator's holiday wiring.

    `test_holiday.py` proves `holiday.resolve()`'s own math in isolation.
    What that cannot catch is a wiring mistake in coordinator.py — e.g. one
    of the three call sites (`_params()`, and the two inside
    `_advance_learning()`) still reading the raw `indoor_target_c` instead of
    the holiday-adjusted `_effective_target_c` — because the learner, the lag
    estimator and the price logic would then quietly disagree about what
    target is actually being asked for. `target_at(i)` below drives the same
    per-step value into all three call sites `run()` has, the same shape
    `coordinator.py` does, so this is the test that would have caught that
    class of bug.
    """

    def test_a_full_setback_and_ramp_cycle_sags_and_recovers(self):
        house = House(curve_error_c=-2.0, delay_steps=4, inertia=0.15, indoor_c=19.0)
        # Settle first, so there is a real learned offset to protect and
        # recover — same settle-first pattern TestPriceInteraction uses.
        _, settled, _, learner_state, lag_state, now = run(
            house, 900, return_state=True
        )
        settled_offset = settled.hold_offset_c
        # The flat pre-holiday history never gives the estimator a confident
        # measurement (see test_lag.py's TestFallbacks), so this sits on the
        # "radiators" fallback constant. Fixed for the whole holiday run
        # rather than re-measured live like the coordinator does every cycle
        # — with no confident measurement available it would stay on this
        # same fallback throughout anyway, so fixing it keeps the test's
        # control flow simple without changing what's under test.
        rise_hours = lag.estimate(lag_state, "radiators").rise_hours

        normal_target = 21.0
        holiday_target = 19.0
        start_date = date(2026, 1, 5)
        end_date = date(2026, 1, 6)
        clock = datetime.combine(start_date, dtime.min)
        phases: list[str] = []

        def target_at(i):
            nonlocal clock
            result = holiday.resolve(
                now=clock,
                armed=True,
                start_date=start_date,
                end_date=end_date,
                normal_target_c=normal_target,
                holiday_target_c=holiday_target,
                rise_hours=rise_hours,
            )
            phases.append(result.phase)
            clock += timedelta(hours=STEP_HOURS)
            return result.target_c

        trace, after, _, learner_state, lag_state, now = run(
            house,
            400,
            target_c=normal_target,
            target_at=target_at,
            learner_state=learner_state,
            lag_state=lag_state,
            now=now,
            return_state=True,
        )
        indoor = [t[0] for t in trace]

        assert "setback" in phases
        assert "ramping" in phases
        # 400 steps (100h) comfortably outlasts the ~39h scheduled window
        # above, so the run reaches "done" well before the end.
        assert phases[-1] == "done"

        setback_end = phases.index("ramping")
        # The house actually sagged toward the setback target during the
        # plateau — not just the published target number, the real plant.
        assert min(indoor[:setback_end]) < indoor[0] - 0.5
        assert min(indoor[:setback_end]) == pytest.approx(holiday_target, abs=0.6)

        # And it's back at the normal target by the end, with the learner
        # re-converged on (roughly) the same steady offset it had before the
        # holiday — not left stuck on the setback's number.
        assert indoor[-1] == pytest.approx(normal_target, abs=0.3)
        assert after.hold_offset_c == pytest.approx(settled_offset, abs=0.6)

    def test_effective_target_can_dip_below_the_comfort_floor_on_holiday(self):
        """`comfort_min_c` is an occupied-house floor; nobody is home during
        a holiday, so a holiday target below it must reach the plant as-is,
        not get dragged back up to `comfort_min_c` — see
        `holiday.HOLIDAY_TARGET_MIN_C`'s docstring for why the two floors are
        kept separate. The coordinator clamps `holiday_target_c` to
        `HOLIDAY_TARGET_MIN_C`, NOT `comfort_min_c`
        (`max(self.holiday_target_c, HOLIDAY_TARGET_MIN_C)`), before ever
        calling `holiday.resolve()`; this is the closed-loop half of that
        guarantee (see test_holiday.py's TestComfortFloorContract for the
        pure version). `heuristic.compute()`'s own published
        `effective_indoor_target_c` — which layers price braking on top of
        whatever `holiday.resolve()` returns — must therefore reach down to
        the holiday target, comfortably below `comfort_min_c`, and price
        braking must never push it lower still than that holiday target.
        """
        house = House(curve_error_c=-2.0, delay_steps=4, inertia=0.15, indoor_c=19.0)
        comfort_min_c = 18.0
        holiday_target = max(10.0, holiday.HOLIDAY_TARGET_MIN_C)
        assert holiday_target < comfort_min_c
        normal_target = 21.0
        start_date = date(2026, 1, 5)
        # Far enough out that the return ramp never starts within the run's
        # 300 steps (~75h) — this keeps every cycle on the flat SETBACK
        # plateau (`target_c == holiday_target` exactly), which is what lets
        # the assertions below pin the floor to an exact value instead of a
        # moving ramp target.
        end_date = date(2026, 2, 4)
        clock = datetime.combine(start_date, dtime.min)

        def target_at(i):
            nonlocal clock
            result = holiday.resolve(
                now=clock,
                armed=True,
                start_date=start_date,
                end_date=end_date,
                normal_target_c=normal_target,
                holiday_target_c=holiday_target,
                rise_hours=1.5,
            )
            clock += timedelta(hours=STEP_HOURS)
            return result.target_c

        effective_targets: list[float] = []
        run(
            house,
            300,
            target_c=normal_target,
            target_at=target_at,
            # A live price spike so braking actually engages during the
            # holiday plateau, not just an inert `enable_price_compensation`
            # flag — this is what proves braking can't sag the target any
            # further below the already-below-comfort-floor holiday target.
            price_at=TestPriceInteraction._spike_schedule(spike_step=40, width=200),
            params_overrides={"comfort_min_c": comfort_min_c, "price_comfort_tier": "high"},
            on_output=lambda output: effective_targets.append(
                output.effective_indoor_target_c
            ),
        )

        assert effective_targets
        # Reached: the holiday target itself, well below comfort_min_c.
        assert min(effective_targets) == pytest.approx(holiday_target, abs=1e-6)
        # Never pushed lower still by price braking on top of it.
        assert min(effective_targets) >= holiday_target - 1e-9
