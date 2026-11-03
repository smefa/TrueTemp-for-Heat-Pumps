"""Exercise actual coordinator wiring without importing Home Assistant.

Compile the recovery/learning statements and learner method directly from the
coordinator AST. Only HA-facing services and the clock are replaced; control
decisions and the learner's inputs come from the production methods.
"""

from __future__ import annotations

import ast
import logging
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

heuristic = load("heuristic")
learner = load("learner")
lag = load("lag")
recovery = load("target_recovery")
excess = load("excess_heat")
pause = load("heating_pause")
const = load("const")
load("holiday")
vacation = load("vacation")


@pytest.fixture
def coordinator():
    path = Path(__file__).parent.parent / "custom_components/truetemp/coordinator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    control = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "TrueTempCoordinator")
    advance = next(node for node in control.body
                   if getattr(node, "name", None) == "_advance_learning")
    update = next(node for node in control.body
                  if getattr(node, "name", None) == "_async_update_cycle")
    start = next(
        index for index, node in enumerate(update.body)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "_effective_target_c"
    )
    stop = next(
        index for index, node in enumerate(update.body)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "_learner_stepped"
    )
    refresh = ast.parse(
        "def refresh(self, indoor_temp_c=21.0, indoor_ok=True, "
        "indoor_contributing=frozenset(), raw_outdoor_temp_c=0.0, "
        "sun_elevation_deg=-10.0, sun_azimuth_deg=180.0, "
        "cloud_coverage_pct=100.0, cloud_ok=False):\n"
        "    return self._learner_stepped\n"
    ).body[0]
    preamble = ast.parse(
        "wind_speed_ms = 0.0\nwind_ok = False\n"
        "current_price = None\nprice_ok = False\nprice_forecast = None\n"
        "price_significance_factor = 1.0\ntoday_spread = None\n"
        "seasonal_reference_spread_c = None\n"
        "forecast_read = SimpleNamespace(outdoor_series=None, wind_series=None, solar_series=None)"
    ).body
    refresh.body = preamble + update.body[start:stop + 1] + refresh.body
    excess_method = next(node for node in control.body
                         if getattr(node, "name", None) == "_advance_excess_heat")
    clock = [1000.0]
    inputs_seen = []

    def step(state, inputs):
        inputs_seen.append(inputs)
        return learner.step(state, inputs)

    namespace = dict(
        replace=replace, SimpleNamespace=SimpleNamespace,
        timedelta=timedelta,
        dt_util=SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)),
        advance_excess_heat=excess.advance,
        advance_heating_pause=pause.advance,
        release_heating_pause=pause.release,
        math=__import__("math"),
        _entry_value=lambda entry, key, default: entry.get(key, default),
        CONF_INDOOR_SENSOR_MAX_AGE_HOURS=const.CONF_INDOOR_SENSOR_MAX_AGE_HOURS,
        CONF_OUTDOOR_TEMP_SENSOR=const.CONF_OUTDOOR_TEMP_SENSOR,
        CONF_OUTDOOR_SENSOR_MAX_AGE_HOURS=const.CONF_OUTDOOR_SENSOR_MAX_AGE_HOURS,
        CONF_EXCESS_HEAT_MARGIN_C=const.CONF_EXCESS_HEAT_MARGIN_C,
        DEFAULT_EXCESS_HEAT_MARGIN_C=const.DEFAULT_EXCESS_HEAT_MARGIN_C,
        OUTPUT_MODE_OUTDOOR_SPOOF=const.OUTPUT_MODE_OUTDOOR_SPOOF,
        HeuristicInputs=heuristic.HeuristicInputs, compute=heuristic.compute,
        recoverable_sag_c=learner.recoverable_sag_c,
        TAU_I_LAG_MULTIPLE=learner.TAU_I_LAG_MULTIPLE,
        SOLAR_GAIN_C=heuristic.SOLAR_GAIN_C, solar_effect_of=heuristic.solar_effect_of,
        time=SimpleNamespace(monotonic=lambda: clock[0]),
        TargetRecovery=recovery.TargetRecovery,
        resolve_target_recovery=recovery.resolve,
        VACATION_ACTIVE_PHASES=vacation.ACTIVE_PHASES,
        SPOOF_PER_INDOOR_C=learner.SPOOF_PER_INDOOR_C,
        resolve_heating_hard_limit_engaged=heuristic.resolve_heating_hard_limit_engaged,
        lag_push=lag.push,
        lag_estimate=lag.estimate,
        learner_step=step,
        LearnerInputs=learner.LearnerInputs,
        _LOGGER=logging.getLogger(__name__),
    )
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")],
                             level=0), advance, excess_method, refresh] + [
            node for node in control.body if getattr(node, "name", None) in (
                "_advance_heating_pause", "_pause_indoor_ready", "_pause_outdoor_ready",
                "_cancel_pause_timer", "_retain_pause_lag",
            )
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    instance = SimpleNamespace(
        excess_heat_state=excess.ExcessHeatState(), excess_heat_enabled=False,
        heating_pause=pause.HeatingPause(), _pause_adjustment_c=0.0,
        _pause_since_learning=False, _pause_lag_reference=None,
        _pause_timer_unsub=None, comfort_min_c=18.0,
        entry={const.CONF_OUTDOOR_TEMP_SENSOR: "sensor.outdoor"},
        output_mode=const.OUTPUT_MODE_OUTDOOR_SPOOF,
        indoor_sensor_readings={}, indoor_sensor_total_configured=0,
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _: None)),
        _last_solar_forecast=None,
        _next_sun_hold_c=lambda *args: 0.0,
        _params=lambda: heuristic.HeuristicParams(21.0, 18.0, False),
        learner_result=None,
        indoor_target_c=21.0,
        vacation_result=SimpleNamespace(phase="inactive", target_c=21.0),
        _target_recovery=recovery.TargetRecovery(21.0, was_enabled=True),
        _manual_target_adjustment_c=0.0,
        _manual_recovery_since_learning=False,
        _learner_last_step_s=100.0,
        _last_indoor_sensor_set=None,
        is_active=True,
        data=None,
        solar_input_enabled=False,
        heating_type="radiators",
        lag_state=lag.initial_state(15.0),
        lag_result=lag.estimate(lag.initial_state(15.0), "radiators"),
        learner_state=learner.initial_state(),
        _lag_result=lambda: instance.lag_result,
        _schedule_state_save=lambda: None,
    )
    instance._advance_excess_heat = MethodType(namespace["_advance_excess_heat"], instance)
    for method in ("_advance_heating_pause", "_pause_indoor_ready", "_pause_outdoor_ready",
                   "_cancel_pause_timer", "_retain_pause_lag"):
        setattr(instance, method, MethodType(namespace[method], instance))
    instance._advance_learning = MethodType(namespace["_advance_learning"], instance)
    instance._due_for_learner_step = (
        lambda: clock[0] - instance._learner_last_step_s >= 900.0
    )
    instance.refresh = MethodType(namespace["refresh"], instance)
    return instance, clock, inputs_seen


def test_manual_change_freezes_the_same_learning_step(coordinator):
    instance, _, seen = coordinator
    instance.indoor_target_c = 21.5
    instance.vacation_result.target_c = 21.5
    assert instance.refresh()
    assert instance._manual_target_adjustment_c == -1.0
    assert seen[-1].manual_target_recovery
    assert instance.learner_state.total_samples == 0


def prepare_room(instance, count=1):
    instance.indoor_sensor_total_configured = count
    instance.hass.states.get = lambda _: SimpleNamespace(last_updated=datetime.now(timezone.utc))
    instance.excess_heat_enabled = True


def test_fast_heat_freezes_same_learning_tick_and_preserves_table(coordinator):
    instance, clock, seen = coordinator
    prepare_room(instance)
    instance.indoor_sensor_readings = {"room": 21.0}
    instance.refresh(indoor_contributing=frozenset({"room"}))
    before = instance.learner_state
    for temp in (22.0, 22.5, 24.0, 23.0, 21.5):
        clock[0] += 900
        instance.indoor_sensor_readings = {"room": temp}
        instance.refresh(indoor_temp_c=temp, indoor_contributing=frozenset({"room"}))
        assert seen[-1].excess_heat
        assert instance.learner_state.bins == before.bins
        assert instance.learner_state.shared_offset_c == before.shared_offset_c
    assert not instance.excess_heat_state.source
    assert instance.learner_state.holdoff_until_s > clock[0]


def test_coldest_room_and_partial_sensor_failure_prevent_pause(coordinator):
    instance, clock, _ = coordinator
    prepare_room(instance, count=2)
    for _ in range(5):
        instance.indoor_sensor_readings = {"hot": 25.0, "cold": 21.0}
        instance.refresh(indoor_temp_c=23.0, indoor_contributing=frozenset({"hot", "cold"}))
        clock[0] += 900
    assert not instance.excess_heat_state.source
    for _ in range(5):
        instance.indoor_sensor_readings = {"hot": 25.0}
        instance.refresh(indoor_temp_c=25.0, indoor_contributing=frozenset({"hot"}))
        clock[0] += 900
    assert not instance.excess_heat_state.source


def test_sensor_freshness_releases_pause_even_if_general_age_check_is_off(coordinator):
    instance, clock, _ = coordinator
    prepare_room(instance)
    instance.indoor_sensor_readings = {"room": 23.0}
    for _ in range(4):
        instance.refresh(indoor_temp_c=23.0, indoor_contributing=frozenset({"room"}))
        clock[0] += 900
    assert instance.excess_heat_state.source == "fireplace"
    instance.hass.states.get = lambda _: SimpleNamespace(
        last_updated=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    instance.refresh(indoor_temp_c=23.0, indoor_contributing=frozenset({"room"}))
    assert not instance.excess_heat_state.source


@pytest.mark.parametrize("mode", [const.OUTPUT_MODE_HEAT_CURVE_OFFSET,
                                  const.OUTPUT_MODE_INDOOR_CLIMATE])
def test_unsupported_output_modes_cannot_pause(coordinator, mode):
    instance, clock, _ = coordinator
    prepare_room(instance)
    instance.output_mode = mode
    for _ in range(5):
        instance.indoor_sensor_readings = {"room": 24.0}
        instance.refresh(indoor_temp_c=24.0, indoor_contributing=frozenset({"room"}))
        clock[0] += 900
    assert not instance.excess_heat_state.source


def test_existing_solar_toggle_gates_pause_and_releases_when_disabled(coordinator):
    instance, clock, _ = coordinator
    prepare_room(instance)
    instance.excess_heat_enabled = False
    instance.solar_input_enabled = True
    instance._params = lambda: heuristic.HeuristicParams(21.0, 18.0, False)
    for _ in range(4):
        instance.indoor_sensor_readings = {"room": 23.0}
        instance.refresh(indoor_temp_c=23.0, indoor_contributing=frozenset({"room"}),
                         sun_elevation_deg=30.0, cloud_coverage_pct=0.0, cloud_ok=True)
        clock[0] += 900
    assert instance.excess_heat_state.source == "solar"
    instance.solar_input_enabled = False
    instance.refresh(indoor_temp_c=23.0, indoor_contributing=frozenset({"room"}),
                     sun_elevation_deg=30.0, cloud_coverage_pct=0.0, cloud_ok=True)
    assert not instance.excess_heat_state.source


@pytest.mark.parametrize("deliberate", [
    {"precharge_active": True}, {"price_catchup_c": -1.0},
    {"weather_preramp_active": True},
])
def test_current_cycle_deliberate_heat_cannot_trigger(coordinator, deliberate):
    instance, clock, _ = coordinator
    prepare_room(instance)
    instance._effective_target_c = 21.0
    instance.indoor_sensor_readings = {"room": 24.0}
    values = dict(
        solar_effect=0.0, cloud_data_available=False,
        indoor_data_available=True, heating_hard_limit_engaged=False,
        precharge_active=False, price_catchup_c=0.0, weather_preramp_active=False,
    )
    values.update(deliberate)
    for _ in range(5):
        instance._advance_excess_heat(SimpleNamespace(**values), frozenset({"room"}))
        clock[0] += 900
    assert not instance.excess_heat_state.source
    assert not instance.excess_heat_state.slow_since_s


@pytest.mark.parametrize("phase", vacation.ACTIVE_PHASES)
def test_vacation_phases_take_precedence_and_do_not_replay_change(coordinator, phase):
    instance, _, _ = coordinator
    instance.vacation_result.phase = phase
    instance.indoor_target_c = 21.5
    instance.refresh()
    assert instance._manual_target_adjustment_c == 0.0
    instance.vacation_result.phase = "inactive"
    instance.refresh()
    assert instance._manual_target_adjustment_c == 0.0


def test_hard_cutoff_cancels_manual_assistance(coordinator):
    instance, _, _ = coordinator
    instance.indoor_target_c = 21.5
    instance.refresh(raw_outdoor_temp_c=30.0)
    assert instance._manual_target_adjustment_c == 0.0
    instance.refresh(raw_outdoor_temp_c=0.0)
    assert instance._manual_target_adjustment_c == 0.0


def test_sensor_loss_suppresses_then_resumes_without_new_deadline(coordinator):
    instance, _, _ = coordinator
    instance.indoor_target_c = 21.5
    instance.refresh()
    deadline = instance._target_recovery.deadline_s
    instance.refresh(indoor_temp_c=None, indoor_ok=False)
    assert instance._manual_target_adjustment_c == 0.0
    assert instance._target_recovery.deadline_s == deadline
    instance.refresh()
    assert instance._manual_target_adjustment_c == -1.0
    assert instance._target_recovery.deadline_s == deadline


def test_recovery_ending_between_learning_ticks_still_freezes_next_step(coordinator):
    instance, clock, seen = coordinator
    instance._learner_last_step_s = clock[0]
    instance.indoor_target_c = 21.5
    instance.refresh()
    assert not seen
    instance.refresh(indoor_temp_c=21.5)
    instance.refresh(indoor_temp_c=21.5)
    assert instance._manual_target_adjustment_c == 0.0
    assert instance._manual_recovery_since_learning
    clock[0] += 900.0
    instance.refresh(indoor_temp_c=21.5)
    assert seen[-1].manual_target_recovery
    assert not instance._manual_recovery_since_learning
    clock[0] += 900.0
    instance.refresh(indoor_temp_c=21.5)
    assert not seen[-1].manual_target_recovery


def test_restored_target_is_consumed_before_manual_changes(coordinator):
    instance, _, _ = coordinator
    instance.is_active = False
    instance.refresh()
    instance.indoor_target_c = 21.5
    instance.is_active = True
    instance.refresh()
    assert instance._manual_target_adjustment_c == 0.0
    instance.indoor_target_c = 22.0
    instance.refresh()
    assert instance._manual_target_adjustment_c < 0.0


@pytest.mark.parametrize("mode", [const.OUTPUT_MODE_OUTDOOR_SPOOF,
                                  const.OUTPUT_MODE_HEAT_CURVE_OFFSET,
                                  const.OUTPUT_MODE_INDOOR_CLIMATE])
def test_external_pause_freezes_learning_in_every_output_mode(coordinator, mode):
    instance, clock, seen = coordinator
    prepare_room(instance)
    instance.output_mode = mode
    instance.indoor_sensor_readings = {"room": 20.0}
    instance.heating_pause = pause.request(pause.HeatingPause(), clock[0], 7200)
    before = instance.learner_state
    instance.refresh(indoor_temp_c=20.0, indoor_contributing=frozenset({"room"}))
    assert instance.heating_pause.active
    assert seen[-1].heating_pause
    assert instance.learner_state.bins == before.bins
    assert instance.learner_state.total_samples == before.total_samples
    assert not instance.lag_state.indoors_c


def test_external_pause_releases_on_partial_sensor_loss(coordinator):
    instance, clock, _ = coordinator
    prepare_room(instance, count=2)
    instance.indoor_sensor_readings = {"room": 20.0}
    instance.heating_pause = pause.request(pause.HeatingPause(), clock[0], 7200)
    instance.refresh(indoor_temp_c=20.0, indoor_contributing=frozenset({"room"}))
    assert not instance.heating_pause.active
    assert instance._pause_adjustment_c == 0.0


def test_external_recovery_and_settling_preserve_offsets(coordinator):
    instance, clock, seen = coordinator
    prepare_room(instance)
    instance.indoor_sensor_readings = {"room": 20.0}
    instance.heating_pause = pause.request(pause.HeatingPause(), clock[0], 900)
    instance.refresh(indoor_temp_c=20.0, indoor_contributing=frozenset({"room"}))
    before = instance.learner_state
    clock[0] += 900
    instance.refresh(indoor_temp_c=20.0, indoor_contributing=frozenset({"room"}))
    assert not instance.heating_pause.active
    assert instance._pause_adjustment_c == -2.0
    assert seen[-1].heating_pause
    instance.indoor_sensor_readings = {"room": 21.0}
    clock[0] += 900
    instance.refresh(indoor_contributing=frozenset({"room"}))
    assert instance._pause_adjustment_c == 0.0
    assert seen[-1].heating_pause
    assert instance.learner_state.bins == before.bins
    assert instance.learner_state.total_samples == before.total_samples


def test_pause_between_learning_ticks_still_freezes_next_tick(coordinator):
    instance, clock, seen = coordinator
    instance._learner_last_step_s = clock[0]
    prepare_room(instance)
    instance.indoor_sensor_readings = {"room": 21.0}
    instance.heating_pause = pause.request(pause.HeatingPause(), clock[0], 1)
    instance.refresh(indoor_contributing=frozenset({"room"}))
    instance.heating_pause = pause.HeatingPause()
    clock[0] += 900
    instance.refresh(indoor_contributing=frozenset({"room"}))
    assert seen[-1].heating_pause
    assert not instance._pause_since_learning


def test_external_pause_preserves_measured_response_estimate(coordinator):
    instance, clock, _ = coordinator
    prepare_room(instance)
    instance.indoor_sensor_readings = {"room": 20.0}
    measured = replace(instance.lag_result, rise_minutes=240.0, rise_measured=True)
    instance.lag_result = measured
    instance.heating_pause = pause.request(pause.HeatingPause(), clock[0], 900)
    instance.refresh(indoor_temp_c=20.0, indoor_contributing=frozenset({"room"}))
    assert instance.lag_result is measured
    clock[0] += 900
    instance.refresh(indoor_temp_c=20.0, indoor_contributing=frozenset({"room"}))
    assert instance.lag_result is measured
    assert instance.heating_pause.recovery_until_s == clock[0] + 6 * 3600
    instance.heating_pause = pause.HeatingPause()
    instance._pause_since_learning = False
    clock[0] += 900
    instance.refresh(indoor_temp_c=20.0, indoor_contributing=frozenset({"room"}))
    assert instance.lag_result.rise_minutes == 240.0
    assert instance.lag_result.rise_measured
    assert "Rebuilding clean events" in instance.lag_result.reason


def test_new_clean_estimates_replace_pre_pause_measurements_per_direction(coordinator):
    instance, _, _ = coordinator
    instance._pause_lag_reference = replace(
        instance.lag_result, rise_minutes=240, rise_measured=True,
        fall_minutes=300, fall_measured=True,
    )
    candidate = replace(instance.lag_result, rise_minutes=180, rise_measured=True)
    rebuilt = instance._retain_pause_lag(candidate)
    assert rebuilt.rise_minutes == 180
    assert rebuilt.fall_minutes == 300
    fallback = lag.estimate(lag.initial_state(15.0), "radiators")
    assert instance._retain_pause_lag(fallback).rise_minutes == 180
    candidate = replace(candidate, fall_minutes=240, fall_measured=True)
    assert instance._retain_pause_lag(candidate) == candidate
    assert instance._pause_lag_reference is None


def test_stale_outdoor_sensor_releases_external_pause(coordinator):
    instance, clock, _ = coordinator
    prepare_room(instance)
    instance.indoor_sensor_readings = {"room": 20.0}
    instance.hass.states.get = lambda entity_id: SimpleNamespace(
        last_updated=datetime.now(timezone.utc) - timedelta(
            hours=5 if entity_id == "sensor.outdoor" else 0),
    )
    instance.heating_pause = pause.request(pause.HeatingPause(), clock[0], 7200)
    instance.refresh(indoor_temp_c=20.0, indoor_contributing=frozenset({"room"}))
    assert not instance.heating_pause.active
    assert instance.heating_pause.release_reason == "outdoor sensor stale or unavailable"
    assert instance._pause_adjustment_c == 0


def test_pause_recovery_converts_indoor_assistance_to_spoof_units(coordinator):
    instance, clock, _ = coordinator
    prepare_room(instance)
    instance.indoor_sensor_readings = {"room": 20.5}
    instance.heating_pause = pause.release(
        pause.HeatingPause(deadline_s=clock[0] + 7200), clock[0], 1, "action",
    )
    instance._advance_heating_pause.__func__.__globals__["SPOOF_PER_INDOOR_C"] = 2.0
    instance.refresh(indoor_temp_c=20.5, indoor_contributing=frozenset({"room"}))
    assert instance._pause_adjustment_c == -2.0
