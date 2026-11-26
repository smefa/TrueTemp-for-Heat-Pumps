"""Exercise the real coordinator log builder without importing Home Assistant."""
from __future__ import annotations

import ast
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_heuristic import compute, lookahead_params, make_inputs


@pytest.fixture
def build_record():
    path = Path(__file__).parents[1] / "custom_components/truetemp/coordinator.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "TrueTempCoordinator")
    method = next(n for n in cls.body if getattr(n, "name", None) == "_build_log_record")
    namespace = {
        "dt_util": SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)),
        "time": time,
        "_sanitize_non_finite": lambda record: record,
    }
    body = ast.parse("from __future__ import annotations").body + [method]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    instance = SimpleNamespace(
        learner_result=None, lag_result=None, daily_pattern_result=None,
        entry=SimpleNamespace(entry_id="zone"),
        _learner_stepped=False, indoor_sensor_readings={"sensor.room": 21.0},
        indoor_aggregation_mode="lowest", outdoor_guard_result=None, is_active=True,
        output_mode="heat_curve_offset", vacation_armed=False, vacation_result=None,
        active_plan_id=None, heating_pause=SimpleNamespace(
            learning_frozen=lambda _: False, release_reason=None,
        ), excess_heat_state=SimpleNamespace(drift_c_per_h=None, learning_frozen=lambda _: False),
        _last_price_forecast=None,
        _last_outdoor_forecast=((0.0, 1.0), (1.0, 1.0)),
        _last_wind_forecast=((0.0, 2.0), (1.0, 2.0)),
        _last_solar_forecast=((0.0, 0.0), (1.0, 0.0)),
    )
    return instance, namespace["_build_log_record"]


def test_inactive_weather_retains_forecasts_and_status_fields(build_record):
    instance, build = build_record
    result = compute(make_inputs(outdoor_forecast=instance._last_outdoor_forecast),
                     lookahead_params())
    assert not result.weather_preramp_active
    record = build(instance, result)
    assert record["outdoor_forecast"] == [[0.0, 1.0], [1.0, 1.0]]
    assert record["wind_forecast_ms"] == [[0.0, 2.0], [1.0, 2.0]]
    assert record["solar_forecast"] == [[0.0, 0.0], [1.0, 0.0]]
    # The status sensor publishes asdict(result); keep log diagnostics identical.
    attributes = asdict(result)
    for key in ("weather_lookahead_reason", "weather_lookahead_minutes",
                "outdoor_forecast_status", "wind_forecast_status", "sun_precool_reason",
                "solar_forecast_status", "sun_precool_lookahead_minutes"):
        assert record[key] == attributes[key]
    assert record["weather_lookahead_reason"] == "no_increasing_heat_demand"


def test_missing_forecasts_do_not_reuse_or_fabricate_log_series(build_record):
    instance, build = build_record
    instance._last_outdoor_forecast = None
    instance._last_wind_forecast = None
    instance._last_solar_forecast = None
    result = compute(make_inputs(), lookahead_params())
    record = build(instance, result)
    for key in ("outdoor_forecast", "wind_forecast_ms", "solar_forecast"):
        assert key not in record
    assert record["weather_lookahead_reason"] == "forecast_unavailable"


@pytest.mark.parametrize("start_minutes", [-30, 30])
def test_coordinator_forecast_offsets_preserve_coverage_for_diagnostics(start_minutes):
    path = Path(__file__).parents[1] / "custom_components/truetemp/coordinator.py"
    tree = ast.parse(path.read_text())
    function = next(n for n in tree.body if getattr(n, "name", None) == "_forecast_offsets")
    now = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)
    namespace = {
        "dt_util": SimpleNamespace(parse_datetime=datetime.fromisoformat, as_local=lambda t: t),
        "WEATHER_LOOKAHEAD_HORIZON_HOURS": 48,
    }
    body = ast.parse("from __future__ import annotations").body + [function]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    forecast = [
        {"datetime": (now + timedelta(minutes=m)).isoformat(), "temperature": temp}
        for m, temp in ((start_minutes, 0.0), (90, -4.0))
    ]
    series = namespace["_forecast_offsets"](forecast, now, "temperature", float)
    result = compute(make_inputs(outdoor_forecast=series, rise_minutes=45), lookahead_params())
    if start_minutes > 0:
        assert result.outdoor_forecast_status == "missing_current_reference"
        assert result.weather_lookahead_reason == "forecast_unavailable"
        assert result.weather_preramp_c == 0.0
    else:
        assert result.outdoor_forecast_status == "available"
        assert result.weather_lookahead_reason == "active"
        assert result.weather_preramp_c < 0.0


def test_weather_logging_preserves_daily_pattern_and_response_time_data(build_record):
    instance, build = build_record
    instance.daily_pattern_result = SimpleNamespace(
        shadow_offset_c=0.4, shadow_offset_uncapped_c=0.4, lead_hour=20.5,
        confident_hours=12, potential_improvement_pct=8.0,
    )
    instance.lag_result = SimpleNamespace(
        rise_minutes=150.0, fall_minutes=120.0, rise_measured=True,
        fall_measured=True, rise_events=3, fall_events=2,
    )
    result = compute(
        make_inputs(outdoor_forecast=instance._last_outdoor_forecast), lookahead_params(),
    )
    record = build(instance, result)
    assert record["pattern_shadow_offset_c"] == 0.4
    assert record["pattern_shadow_offset_uncapped_c"] == 0.4
    assert record["pattern_lead_hour"] == 20.5
    assert record["pattern_consistent_hours"] == 12
    assert record["pattern_potential_improvement_pct"] == 8.0
    assert record["lag_rise_minutes"] == 150.0
    assert record["lag_fall_minutes"] == 120.0
    assert record["lag_rise_measured"] is True
    assert record["lag_fall_measured"] is True
    assert record["lag_rise_events"] == 3
    assert record["lag_fall_events"] == 2
    assert record["weather_lookahead_reason"] == "no_increasing_heat_demand"
    assert record["wind_forecast_ms"] == [[0.0, 2.0], [1.0, 2.0]]
