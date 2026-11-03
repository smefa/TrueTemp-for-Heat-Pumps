"""Exercise production action, timer, and output methods without Home Assistant."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path
import sys
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from loader import load  # noqa: E402

pause = load("heating_pause")
heuristic = load("heuristic")
const = load("const")
load("holiday")
vacation = load("vacation")


class ValidationError(Exception):
    pass


@pytest.fixture
def control():
    path = Path(__file__).parent.parent / "custom_components/truetemp/coordinator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    coordinator = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "TrueTempCoordinator")
    names = {
        "_params", "_pause_indoor_ready", "_pause_outdoor_ready",
        "_cancel_pause_timer", "_async_pause_due",
        "async_pause_heating", "async_resume_heating", "_apply_heating_pause",
        "_async_push_output", "_async_push_heat_curve_offset", "_async_push_indoor_climate",
        "_clamp_to_entity_range",
    }
    clock = [1000.0]
    timer = Mock(return_value=Mock())
    namespace = dict(
        time=SimpleNamespace(monotonic=lambda: clock[0]), math=math,
        dt_util=SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)),
        timedelta=timedelta, replace=replace, DEFAULT_DURATION_S=pause.DEFAULT_DURATION_S,
        request_heating_pause=pause.request, release_heating_pause=pause.release,
        ServiceValidationError=ValidationError, async_call_later=timer,
        HeuristicParams=heuristic.HeuristicParams, SPOOF_PER_INDOOR_C=1.0,
        VACATION_ACTIVE_PHASES=vacation.ACTIVE_PHASES,
        _entry_value=lambda entry, key, default: entry.get(key, default),
        CONF_INDOOR_SENSOR_MAX_AGE_HOURS=const.CONF_INDOOR_SENSOR_MAX_AGE_HOURS,
        CONF_OUTDOOR_TEMP_SENSOR=const.CONF_OUTDOOR_TEMP_SENSOR,
        CONF_OUTDOOR_SENSOR_MAX_AGE_HOURS=const.CONF_OUTDOOR_SENSOR_MAX_AGE_HOURS,
        OUTPUT_MODE_OUTDOOR_SPOOF=const.OUTPUT_MODE_OUTDOOR_SPOOF,
        OUTPUT_MODE_HEAT_CURVE_OFFSET=const.OUTPUT_MODE_HEAT_CURVE_OFFSET,
        OUTPUT_MODE_INDOOR_CLIMATE=const.OUTPUT_MODE_INDOOR_CLIMATE,
        OUTPUT_SANITY_MAX_C=heuristic.OUTPUT_SANITY_MAX_C,
        HEAT_CURVE_OFFSET_FALLBACK_MIN_C=-10,
        HEAT_CURVE_OFFSET_FALLBACK_MAX_C=10,
        INDOOR_CLIMATE_FALLBACK_MIN_C=5, INDOOR_CLIMATE_FALLBACK_MAX_C=30,
        OUTPUT_WRITE_TOLERANCE_C=0.05, ATTR_TEMPERATURE="temperature",
        heating_hard_limit_offset_c=heuristic.heating_hard_limit_offset_c,
        heat_curve_offset_c=heuristic.heat_curve_offset_c,
        indoor_climate_offset_c=heuristic.indoor_climate_offset_c,
        _LOGGER=logging.getLogger(__name__),
    )
    body = ast.parse("from __future__ import annotations").body + [
        node for node in coordinator.body if getattr(node, "name", None) in names
    ]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    instance = SimpleNamespace(
        heating_pause=pause.HeatingPause(), _pause_adjustment_c=0.0,
        _pause_since_learning=False, _pause_timer_unsub=None,
        is_active=True, last_update_success=True, comfort_min_c=18.0,
        indoor_sensor_readings={"room": 21.0}, indoor_sensor_total_configured=1,
        entry={const.CONF_OUTDOOR_TEMP_SENSOR: "sensor.outdoor"}, hass=SimpleNamespace(
            states=SimpleNamespace(get=lambda _: SimpleNamespace(
                last_updated=datetime.now(timezone.utc))),
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(units=SimpleNamespace(temperature_unit="C")),
        ),
        async_request_refresh=AsyncMock(), _lag_result=lambda: SimpleNamespace(rise_hours=1),
        output_mode=const.OUTPUT_MODE_OUTDOOR_SPOOF,
        ohmonwifi_host="pump.local", output_number_entity_id="number.outdoor",
        heat_curve_offset_entity_id="number.offset", heat_curve_offset_invert=False,
        indoor_climate_entity_id="climate.room",
        _async_push_ohmonwifi=AsyncMock(), _async_push_number_entity=AsyncMock(),
        _sync_heat_curve_offset_issues=Mock(), _entity_bounds=lambda *args: (-10, 10),
        _target_number_entity_value=lambda _: None, _output_stale=lambda _: True,
        _last_heat_curve_offset_value_c=None, _last_heat_curve_offset_pushed_at=None,
        _last_indoor_climate_value_c=None, _last_indoor_climate_pushed_at=None,
        _effective_target_c=21.0, indoor_target_c=21.0,
        vacation_result=SimpleNamespace(phase="inactive"),
        price_enabled=False, price_comfort_tier="medium", cold_caution="medium",
        solar_input_enabled=False, wind_input_enabled=False, lookahead_enabled=False,
        _manual_target_adjustment_c=0.0,
    )
    for name in names:
        setattr(instance, name, MethodType(namespace[name], instance))
    return instance, clock, timer


def result(indoor=21.0):
    return heuristic.compute(heuristic.HeuristicInputs(
        indoor_temp_c=indoor, indoor_data_available=True, raw_outdoor_temp_c=0.0,
        wind_speed_ms=0.0, wind_data_available=False, sun_elevation_deg=-10.0,
        sun_azimuth_deg=180.0, cloud_coverage_pct=100.0, cloud_data_available=False,
        current_price=None, price_data_available=False,
    ), heuristic.HeuristicParams(21.0, 18.0, False))


def test_action_defaults_to_two_hours_and_refreshes_immediately(control):
    instance, _, timer = control
    asyncio.run(instance.async_pause_heating())
    assert instance.heating_pause.deadline_s == 8200
    assert timer.call_args.args[1] == 7200
    assert instance._pause_since_learning
    instance.async_request_refresh.assert_awaited_once()


def test_repeated_request_replaces_timer_and_cancels_old_callback(control):
    instance, clock, timer = control
    asyncio.run(instance.async_pause_heating())
    cancel = timer.return_value
    clock[0] += 100
    asyncio.run(instance.async_pause_heating(600))
    cancel.assert_called_once()
    assert instance.heating_pause.deadline_s == 1700
    assert timer.call_args.args[1] == 600


def test_timer_resumes_early_without_waiting_for_poll(control):
    instance, _, timer = control
    asyncio.run(instance.async_pause_heating())
    asyncio.run(timer.call_args.args[2](None))
    assert not instance.heating_pause.active
    assert instance.heating_pause.recovery_until_s > 1000
    assert instance.async_request_refresh.await_count == 2


def test_old_timer_callback_cannot_release_a_replacement_request(control):
    instance, clock, timer = control
    asyncio.run(instance.async_pause_heating())
    old_callback = timer.call_args.args[2]
    clock[0] += 100
    asyncio.run(instance.async_pause_heating(600))
    requested = instance.heating_pause
    asyncio.run(old_callback(None))
    assert instance.heating_pause is requested
    assert instance.heating_pause.active
    assert instance.async_request_refresh.await_count == 2


@pytest.mark.parametrize("change", [
    {"is_active": False}, {"last_update_success": False},
    {"indoor_sensor_readings": {}}, {"indoor_sensor_total_configured": 2},
    {"indoor_sensor_readings": {"room": 18.0}},
    {"indoor_sensor_readings": {"room": math.nan}},
])
def test_unsafe_action_leaves_state_and_timer_unchanged(control, change):
    instance, _, timer = control
    vars(instance).update(change)
    with pytest.raises(ValidationError):
        asyncio.run(instance.async_pause_heating())
    assert not instance.heating_pause.active
    timer.assert_not_called()


def test_stale_indoor_sensor_is_rejected_even_when_age_check_disabled(control):
    instance, _, _ = control
    instance.hass.states.get = lambda _: SimpleNamespace(
        last_updated=datetime.now(timezone.utc) - timedelta(hours=5))
    with pytest.raises(ValidationError):
        asyncio.run(instance.async_pause_heating())


def test_stale_outdoor_sensor_is_rejected_even_when_age_check_disabled(control):
    instance, _, _ = control
    instance.hass.states.get = lambda entity_id: SimpleNamespace(
        last_updated=datetime.now(timezone.utc) - timedelta(
            hours=5 if entity_id == "sensor.outdoor" else 0),
    )
    with pytest.raises(ValidationError):
        asyncio.run(instance.async_pause_heating())


@pytest.mark.parametrize("duration", [0, -1, 86401, math.inf, math.nan])
def test_invalid_duration_does_not_replace_existing_pause(control, duration):
    instance, _, timer = control
    asyncio.run(instance.async_pause_heating())
    before = instance.heating_pause
    cancel = timer.return_value
    with pytest.raises(ValidationError):
        asyncio.run(instance.async_pause_heating(duration))
    assert instance.heating_pause == before
    cancel.assert_not_called()


def test_outdoor_pause_pushes_warm_value_on_both_channels(control):
    instance, _, _ = control
    asyncio.run(instance.async_pause_heating())
    output = instance._apply_heating_pause(result())
    asyncio.run(instance._async_push_output(output))
    assert output.heating_paused
    assert "stop requested" in output.reason
    instance._async_push_ohmonwifi.assert_awaited_once_with("pump.local", 25.0)
    instance._async_push_number_entity.assert_awaited_once_with("number.outdoor", 25.0)
    instance.hass.services.async_call.assert_not_awaited()


@pytest.mark.parametrize("invert, expected", [(False, -10), (True, 10)])
def test_offset_pause_uses_lowest_heating_bound(control, invert, expected):
    instance, _, _ = control
    instance.output_mode = const.OUTPUT_MODE_HEAT_CURVE_OFFSET
    instance.heat_curve_offset_invert = invert
    asyncio.run(instance.async_pause_heating())
    output = instance._apply_heating_pause(result())
    asyncio.run(instance._async_push_output(output))
    assert "full stop is not guaranteed" in output.reason
    assert instance.hass.services.async_call.call_args.args[2] == {
        "entity_id": "number.offset", "value": expected,
    }
    instance._async_push_ohmonwifi.assert_not_awaited()
    instance._async_push_number_entity.assert_not_awaited()


def test_offset_pause_respects_fractional_bounds(control):
    instance, _, _ = control
    instance.output_mode = const.OUTPUT_MODE_HEAT_CURVE_OFFSET
    instance._entity_bounds = lambda *args: (-3.5, 3.5)
    asyncio.run(instance.async_pause_heating())
    asyncio.run(instance._async_push_output(instance._apply_heating_pause(result())))
    assert instance.hass.services.async_call.call_args.args[2]["value"] == -3


def test_indoor_pause_uses_minimum_supported_room_target(control):
    instance, _, _ = control
    instance.output_mode = const.OUTPUT_MODE_INDOOR_CLIMATE
    instance._entity_bounds = lambda *args: (7, 30)
    asyncio.run(instance.async_pause_heating())
    asyncio.run(instance._async_push_output(instance._apply_heating_pause(result())))
    assert instance.hass.services.async_call.call_args.args[:3] == (
        "climate", "set_temperature", {"entity_id": "climate.room", "temperature": 7},
    )
    instance._async_push_ohmonwifi.assert_not_awaited()


@pytest.mark.parametrize("mode", [const.OUTPUT_MODE_OUTDOOR_SPOOF,
                                  const.OUTPUT_MODE_HEAT_CURVE_OFFSET,
                                  const.OUTPUT_MODE_INDOOR_CLIMATE])
def test_resume_restores_normal_output_in_each_mode(control, mode):
    instance, _, _ = control
    instance.output_mode = mode
    instance._entity_bounds = lambda *args: (5, 30) if mode == "indoor_climate" else (-10, 10)
    asyncio.run(instance.async_pause_heating())
    asyncio.run(instance.async_resume_heating())
    output = instance._apply_heating_pause(result())
    assert not output.heating_paused
    asyncio.run(instance._async_push_output(output))
    if mode == const.OUTPUT_MODE_OUTDOOR_SPOOF:
        assert instance._async_push_ohmonwifi.call_args.args[1] < 25
    elif mode == const.OUTPUT_MODE_HEAT_CURVE_OFFSET:
        assert instance.hass.services.async_call.call_args.args[2]["value"] != -10
    else:
        assert instance.hass.services.async_call.call_args.args[2]["temperature"] > 5


def test_recovery_uses_existing_response_without_stacking_manual_assistance(control):
    instance, _, _ = control
    instance._pause_adjustment_c = -2.0
    params = instance._params()
    assert params.manual_target_adjustment_c == -2
    instance._manual_target_adjustment_c = -1.0
    assert instance._params().manual_target_adjustment_c == -1
    instance._manual_target_adjustment_c = 1.0
    assert instance._params().manual_target_adjustment_c == 1


@pytest.mark.parametrize("name, method", [
    ("_async_handle_pause_heating", "async_pause_heating"),
    ("_async_handle_resume_heating", "async_resume_heating"),
])
def test_action_routes_to_requested_loaded_zone(name, method):
    path = Path(__file__).parent.parent / "custom_components/truetemp/services.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    handler = next(node for node in tree.body if getattr(node, "name", None) == name)
    runtime = SimpleNamespace(async_pause_heating=AsyncMock(), async_resume_heating=AsyncMock())
    entry = SimpleNamespace(state="loaded", runtime_data=runtime)
    resolve = Mock(return_value=entry)
    namespace = dict(_resolve_entry=resolve,
                     ConfigEntryState=SimpleNamespace(LOADED="loaded"),
                     ServiceValidationError=ValidationError, DOMAIN="truetemp")
    body = ast.parse("from __future__ import annotations").body + [handler]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    call = SimpleNamespace(hass=object(), data={
        "config_entry_id": "second-zone", "duration": timedelta(hours=2),
    })
    asyncio.run(namespace[name](call))
    resolve.assert_called_once_with(call.hass, "second-zone")
    getattr(runtime, method).assert_awaited_once_with(*([7200.0] if "pause_" in name else []))
