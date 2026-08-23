"""Sensor platform for TrueTemp.

Four sensors, where there used to be nineteen.

The old entity list carried five RC-estimator diagnostics, four MPC planner
diagnostics, two auto-tune diagnostics and several plain echoes of source
entities the user already had. Most of those reported on the internal
convergence of subsystems that no longer exist, and the ones worth keeping were
never worth their own entity — they are numbers you read when something looks
wrong, not series you graph.

So the rule here is: an entity earns its place only if it is worth a graph or a
history. Everything else lives in `extra_state_attributes` on `status`, where
it costs no registry entry and no recorder row of its own.

    compensated_outdoor_temperature   the output for OUTPUT_MODE_OUTDOOR_SPOOF.
    heat_pump_offset                  the output for OUTPUT_MODE_HEAT_CURVE_OFFSET.
    indoor_climate_target_temperature the output for OUTPUT_MODE_INDOOR_CLIMATE.
                                      Exactly one of this trio is ever the
                                      thing actually reaching the pump — see
                                      `output_mode` in const.py — so the other
                                      two report `available = False` rather
                                      than a number that means nothing in that
                                      mode.
    learned_offset                    what the house has taught the controller.
                                      The one number genuinely worth graphing:
                                      it should be stable and season-shaped,
                                      and misbehaviour shows up here first. The
                                      full per-band table is in `bands`.
    status                            health, plus everything about the learner
                                      and the lag estimator in attributes —
                                      including the full per-term breakdown
                                      behind whichever output is currently
                                      published, since that explanation is
                                      useful regardless of which output mode
                                      is active.
"""

from __future__ import annotations

from dataclasses import asdict

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import TrueTempConfigEntry
from .const import (
    DOMAIN,
    OUTPUT_MODE_HEAT_CURVE_OFFSET,
    OUTPUT_MODE_INDOOR_CLIMATE,
    OUTPUT_MODE_OUTDOOR_SPOOF,
)
from .coordinator import TrueTempCoordinator
from .heuristic import (
    HeuristicResult,
    heat_curve_offset_c,
    heating_hard_limit_offset_c,
    indoor_climate_offset_c,
)
from .holiday import HOLIDAY_PHASES
from .learner import SPOOF_PER_INDOOR_C, bin_label
from .vacation import serialize_plans

# Substituted for an attribute that is None only because the feature that would
# fill it is switched off. Without this the UI shows "Unknown", which reads as a
# broken source — the question that prompted this — when nothing is broken and
# nothing is being read. Applied ONLY where the value is already None, so a
# disabled feature never hides a reading that was genuinely taken.
ATTR_DISABLED = "disabled"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TrueTempConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            CompensatedOutdoorTempSensor(coordinator, entry),
            HeatPumpOffsetSensor(coordinator, entry),
            IndoorClimateTargetSensor(coordinator, entry),
            LearnedOffsetSensor(coordinator, entry),
            StatusSensor(coordinator, entry),
            VacationStatusSensor(coordinator, entry),
        ]
    )


class TrueTempEntity(CoordinatorEntity[TrueTempCoordinator]):
    """Common device grouping for all TrueTemp entities of one entry."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="TrueTemp",
            model="Self-learning weather compensation",
            entry_type=DeviceEntryType.SERVICE,
        )


class CompensatedOutdoorTempSensor(TrueTempEntity, SensorEntity):
    """The output for OUTPUT_MODE_OUTDOOR_SPOOF: the temperature to feed the
    heat pump's weather curve.

    While compensation is off, this publishes the raw outdoor temperature
    unmodified — a true no-op that can never behave worse than the pump's own
    curve did before this integration existed. The controller keeps running
    regardless, and its recommendation stays readable on `status` as
    `recommended_compensated_outdoor_temp_c`, so "off" is also how you preview
    it.

    Reports unavailable outside OUTPUT_MODE_OUTDOOR_SPOOF, where this number is
    never actually sent anywhere — see `HeatPumpOffsetSensor` and
    `IndoorClimateTargetSensor` for those modes' equivalents — rather than show
    a value that would just be confusing.
    """

    _attr_translation_key = "compensated_outdoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_compensated_outdoor_temperature"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.output_mode == OUTPUT_MODE_OUTDOOR_SPOOF

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return None
        if not self.coordinator.is_active:
            return result.raw_outdoor_temp_c
        return result.compensated_outdoor_temp_c


class HeatPumpOffsetSensor(TrueTempEntity, SensorEntity):
    """The output for OUTPUT_MODE_HEAT_CURVE_OFFSET: the delta to write to the
    pump's own heat-curve-offset parameter, mirroring exactly what
    `_async_push_heat_curve_offset` sends — zero while compensation is off,
    a whole number always (see `heat_curve_offset_c`'s docstring for why).

    Reports unavailable outside OUTPUT_MODE_HEAT_CURVE_OFFSET, the counterpart
    to how `CompensatedOutdoorTempSensor` and `IndoorClimateTargetSensor`
    behave in this mode — exactly one of the trio is ever the thing actually
    reaching the pump.
    """

    _attr_translation_key = "heat_pump_offset"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_heat_pump_offset"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.output_mode == OUTPUT_MODE_HEAT_CURVE_OFFSET
        )

    @property
    def native_value(self) -> int | None:
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return None
        if not self.coordinator.is_active:
            return 0
        if result.heating_hard_limit_engaged:
            # Same fixed value `_async_push_heat_curve_offset` sends while the
            # hard limit holds — see `heating_hard_limit_offset_c`'s
            # docstring. Falling through to the raw-derived formula below
            # would flap this sensor even though the pump itself now gets a
            # stable number.
            return heating_hard_limit_offset_c(self.coordinator.heat_curve_offset_invert)
        return heat_curve_offset_c(
            result.compensated_outdoor_temp_c,
            result.raw_outdoor_temp_c,
            self.coordinator.heat_curve_offset_invert,
        )


class IndoorClimateTargetSensor(TrueTempEntity, SensorEntity):
    """The output for OUTPUT_MODE_INDOOR_CLIMATE: the target temperature
    written to a room-sensor climate entity, mirroring exactly what
    `_async_push_indoor_climate` sends — the occupant's own effective target
    while compensation is off.

    Reports unavailable outside OUTPUT_MODE_INDOOR_CLIMATE, the counterpart to
    how `CompensatedOutdoorTempSensor` and `HeatPumpOffsetSensor` behave in
    this mode — exactly one of the trio is ever the thing actually reaching
    the pump.
    """

    _attr_translation_key = "indoor_climate_target_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_indoor_climate_target_temperature"

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.output_mode == OUTPUT_MODE_INDOOR_CLIMATE
        )

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return None
        if not self.coordinator.is_active:
            return result.effective_indoor_target_c
        if result.heating_hard_limit_engaged:
            # Same fixed value `_async_push_indoor_climate` sends while the
            # hard limit holds — see that method's docstring. Falling through
            # to the raw-derived formula below would flap this sensor even
            # though the pump itself now gets a stable number.
            return result.effective_indoor_target_c + heating_hard_limit_offset_c(
                invert=False
            )
        return result.effective_indoor_target_c + indoor_climate_offset_c(
            result.compensated_outdoor_temp_c,
            result.raw_outdoor_temp_c,
            SPOOF_PER_INDOOR_C,
        )


class LearnedOffsetSensor(TrueTempEntity, SensorEntity):
    """What this house has taught the controller it needs, right now.

    The number to watch. It should settle within a couple of days and then move
    slowly with the season, tracing the shape of the pump's own heat curve
    error. Drift or oscillation here is the earliest visible sign of a problem —
    long before it becomes a comfort complaint — which is exactly why it is a
    first-class sensor and the rest of the learner is attributes on `status`.
    """

    _attr_translation_key = "learned_offset"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The per-band table is re-published in full every cycle and is worth
    # reading, not graphing — its history is the sensor's own state over
    # time. Excluding it keeps the recorder DB from growing by a nested dict
    # per outdoor band on every update.
    _unrecorded_attributes = frozenset({"bands"})

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_learned_offset"

    @property
    def native_value(self) -> float | None:
        learner = self.coordinator.learner_result
        return round(learner.offset_c, 2) if learner else None

    @property
    def extra_state_attributes(self) -> dict:
        learner = self.coordinator.learner_result
        if learner is None:
            return {}
        return {
            "outdoor_band": learner.bin_label,
            "steady_offset_c": round(learner.hold_offset_c, 2),
            "fast_correction_c": round(learner.proportional_c, 2),
            "indoor_error_c": (
                round(learner.error_c, 2) if learner.error_c is not None else None
            ),
            "reason": learner.reason,
            "bands": self._bands(),
        }

    def _bands(self) -> dict[str, dict]:
        """The whole offset table, one entry per outdoor band.

        The sensor's own state is only the band currently occupied, which
        cannot distinguish "this house needs +1.4°C everywhere" from "it needs
        +1.4°C here and something odd at -10°C that it has not revisited since".
        The table was previously visible only in a downloaded diagnostics file
        — too far away to check casually, and impossible to put on a dashboard.

        `samples` is the field to read first, and it counts only direct
        observations. A band can hold a non-zero offset on zero samples: each
        step also nudges the neighbouring bands by `NEIGHBOUR_SHARE` without
        crediting them a sample, so an offset there is extrapolation from the
        band next door, not something measured in that weather. Distinguishing
        the two is most of the point of showing the table.

        Bands are emitted coldest-to-warmest and every band is included, so the
        gaps in coverage are as visible as the entries.

        Each row also carries the open-loop baseline for that band — what the
        raw curve settles the house at while compensation is off — so the
        table has something to show in a band closed-loop learning has never
        touched, and stays visible on an install that has never been switched
        on. `seeded` marks a band whose offset came from that baseline via
        `seed_offsets_from_baseline` rather than from closed-loop samples.
        """
        baseline_bins = self.coordinator.learner_state.baseline_bins
        return {
            bin_label(index): {
                "steady_offset_c": round(b.hold_offset_c, 2),
                "capacity_ceiling_c": round(b.ceiling_c, 2),
                "recovery_rate_c_per_h": round(b.close_rate_c_per_h, 3),
                "samples": b.samples,
                "seeded": b.seeded,
                "baseline_indoor_c": (
                    round(baseline_bins[index].indoor_c, 2)
                    if baseline_bins[index].samples
                    else None
                ),
                "baseline_samples": baseline_bins[index].samples,
            }
            for index, b in enumerate(self.coordinator.learner_state.bins)
        }


class StatusSensor(TrueTempEntity, SensorEntity):
    """Health, and everything worth knowing about how learning is going.

    Always available, unlike every other entity here, because its entire
    purpose is to report problems — including the case where the coordinator
    itself is failing and everything else shows unavailable.

    "error" means the outdoor sensor (the one hard-required source) is down or
    the cycle is otherwise failing entirely, so the published value has gone
    stale. "degraded" means the cycle is succeeding but a soft source (indoor
    sensor, wind, cloud/sun, price) is down and that term is contributing
    nothing.

    Also carries the open-loop baseline: what the raw curve does with
    compensation off, so an install that has never been switched on still has
    something to show, and a before/after comparison is possible once it is.
    """

    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "degraded", "error"]

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        if not self.coordinator.last_update_success:
            return "error"
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return "error"
        if (
            not result.indoor_data_available
            or (self.coordinator.wind_input_enabled and not result.wind_data_available)
            or (self.coordinator.solar_input_enabled and not result.cloud_data_available)
            or (self.coordinator.price_configured and not result.price_data_available)
        ):
            return "degraded"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict:
        result: HeuristicResult | None = self.coordinator.data
        learner = self.coordinator.learner_result
        lag = self.coordinator.lag_result
        outdoor_ok = self.coordinator.last_update_success
        # last_exception is sticky in HA's DataUpdateCoordinator - it is never
        # cleared on a later successful update - so it is only trustworthy as
        # "the current problem" while outdoor_ok is False. Once recovered, drop
        # it rather than showing a stale message with no way to tell it is old.
        last_error = self.coordinator.last_exception if not outdoor_ok else None

        attrs: dict = {
            "outdoor_sensor_ok": outdoor_ok,
            "last_error": str(last_error) if last_error else None,
            "last_error_at": (
                dt_util.as_local(self.coordinator.last_error_at).isoformat()
                if last_error and self.coordinator.last_error_at
                else None
            ),
        }
        if result is not None:
            attrs["indoor_sensor_ok"] = result.indoor_data_available
            if self.coordinator.wind_input_enabled:
                attrs["wind_forecast_ok"] = result.wind_data_available
            if self.coordinator.solar_input_enabled:
                attrs["cloud_sun_forecast_ok"] = result.cloud_data_available
            if self.coordinator.price_configured:
                attrs["price_ok"] = result.price_data_available
                attrs["price_forecast"] = self.coordinator.price_forecast_series()

        # --- Output breakdown --------------------------------------------------
        # Every term behind the published value, plus the plain-language
        # reason. Lives here rather than on the output sensor(s) because it's
        # about why a number was chosen, which stays useful no matter which
        # output mode is active — unlike the sensors in sensor.py that carry
        # the number itself, exactly one of which is available at a time.
        if result is not None:
            breakdown = asdict(result)
            recommended = breakdown.pop("compensated_outdoor_temp_c")
            breakdown["recommended_compensated_outdoor_temp_c"] = recommended
            # These four are already published above, under friendlier names
            # (indoor_sensor_ok, wind_forecast_ok, cloud_sun_forecast_ok,
            # price_ok) that the card actually reads. Left in place here they'd
            # be exact duplicates under a second name. model_version is
            # heuristic.py's own internal versioning marker (distinct from
            # learner.py's, which IS load-bearing for state-store staleness
            # checks) and has no consumer as an attribute.
            for dead_key in (
                "indoor_data_available",
                "wind_data_available",
                "cloud_data_available",
                "price_data_available",
                "model_version",
            ):
                breakdown.pop(dead_key)
            # The bottom line of the per-term breakdown. Taken from the
            # published value rather than by summing the terms, so it
            # reflects the output sanity clamp when that bites.
            breakdown["total_adjustment_c"] = round(
                recommended - result.raw_outdoor_temp_c, 2
            )
            breakdown["recommended_heat_pump_offset"] = (
                heating_hard_limit_offset_c(self.coordinator.heat_curve_offset_invert)
                if result.heating_hard_limit_engaged
                else heat_curve_offset_c(
                    recommended,
                    result.raw_outdoor_temp_c,
                    self.coordinator.heat_curve_offset_invert,
                )
            )
            breakdown["active"] = self.coordinator.is_active
            breakdown["output_mode"] = self.coordinator.output_mode

            # Distinguish "switched off" from "should be here and isn't". Both
            # arrive as None, and telling them apart otherwise means knowing
            # that e.g. all five price attributes collapse together the
            # moment price compensation is disabled. Anything still None
            # after this really is unknown: the feature is on and the value
            # was not available.
            for enabled, keys in (
                (
                    # current_price and price_median are the raw feed and its
                    # day's-ordinary line — read regardless of this toggle, so
                    # they're excluded here. Only the braking thresholds
                    # actually depend on compensation being switched on.
                    self.coordinator.price_enabled,
                    (
                        "price_band_start",
                        "price_band_full",
                        "upcoming_spike_in_min",
                    ),
                ),
                # Cloud feeds the solar term and nothing else. Note the
                # weather entity is still read when wind alone is on, so this
                # can hold a real number with solar off — hence the None
                # check below rather than an unconditional overwrite.
                (self.coordinator.solar_input_enabled, ("cloud_coverage_pct",)),
            ):
                if enabled:
                    continue
                for key in keys:
                    if breakdown.get(key) is None:
                        breakdown[key] = ATTR_DISABLED
            # Separate from the loop above because these are 0.0 rather than
            # None when the feature is off, and a flat 0.0 reads as "the
            # forecast is calling for nothing" instead of "nobody is looking".
            if not self.coordinator.lookahead_enabled:
                for key in (
                    "outdoor_preramp_c",
                    "wind_preramp_c",
                    "weather_preramp_c",
                    "weather_preramp_in_min",
                    "sun_precool_c",
                    "sun_precool_in_min",
                ):
                    breakdown[key] = ATTR_DISABLED
            attrs.update(breakdown)

        # --- How learning is going ------------------------------------------
        if learner is not None:
            attrs.update(
                {
                    "learning_progress_pct": round(learner.progress * 100),
                    "outdoor_bands_covered_pct": round(learner.coverage * 100),
                    "authority_limit_c": round(learner.authority_c, 2),
                    "capacity_ceiling_c": round(learner.ceiling_c, 2),
                    "settling_time_h": round(learner.tau_i_h, 1),
                    "samples_learned": learner.total_samples,
                    "samples_this_band": learner.bin_samples,
                    "pump_at_capacity": learner.saturated,
                    "learning_paused": learner.frozen,
                }
            )
            if learner.freeze_reason and not (
                learner.freeze_reason == "indoor sensor unavailable"
                and self.coordinator.in_startup_grace_period()
            ):
                attrs["learning_paused_because"] = learner.freeze_reason
            if learner.close_rate_c_per_h > 0:
                attrs["recovery_rate_c_per_h"] = round(learner.close_rate_c_per_h, 3)

        # --- Open-loop baseline (what the raw curve does, compensation off) --
        # Filled in even while `is_active` is True: the reading below is always
        # the CURRENT band's baseline, which may have been observed on an
        # earlier off period and is still the fact seeding would use if the
        # house left this band and came back. `baseline_reason` itself only
        # updates while off — see `learner.py`'s "Two regimes" docstring.
        if learner is not None:
            attrs["baseline_coverage_pct"] = round(learner.baseline_coverage * 100)
            if learner.baseline_indoor_c is not None:
                attrs["baseline_indoor_c"] = round(learner.baseline_indoor_c, 2)
                attrs["baseline_deficit_c"] = round(learner.baseline_deficit_c, 2)
            attrs["baseline_samples_this_band"] = learner.baseline_samples
            if learner.baseline_reason:
                attrs["baseline_reason"] = learner.baseline_reason
            if learner.seeded_bins:
                attrs["baseline_seeded_bins"] = learner.seeded_bins
            attrs["offset_seeded_this_band"] = learner.seeded

        # --- How the house responds -----------------------------------------
        if lag is not None:
            attrs.update(
                {
                    "response_time_min": round(lag.rise_minutes),
                    "wind_down_time_min": round(lag.fall_minutes),
                    "response_time_measured": lag.rise_measured,
                    "wind_down_time_measured": lag.fall_measured,
                }
            )

        # --- Plumbing --------------------------------------------------------
        attrs["data_logging_enabled"] = self.coordinator.data_logging_enabled
        if self.coordinator.data_log_path:
            attrs["data_log_path"] = self.coordinator.data_log_path
        return attrs


class VacationStatusSensor(TrueTempEntity, SensorEntity):
    """Vacation setback phase and the numbers behind it, across every plan.

    What the vacation card (and any automations/notifications) read to show
    e.g. "ramp starts Tue 14:00" — see holiday.py's `HolidayResult`, which
    `resolve_vacation()` still returns and this mirrors field-for-field.
    Unlike the old single-scenario sensor, attributes also carry the whole
    plan list plus which plan (if any) is the one currently winning, so the
    card has a single read entity (§6 of docs/plan_vacation_plans.md).
    """

    _attr_translation_key = "vacation_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(HOLIDAY_PHASES)
    # `plans` is the whole serialized plan list, re-published unchanged most
    # cycles — worth reading on demand (the card does), not worth a recorder
    # row every update. The phase timing fields below it are small and do
    # change meaningfully over a vacation, so those stay recorded.
    _unrecorded_attributes = frozenset({"plans"})

    def __init__(
        self,
        coordinator: TrueTempCoordinator,
        entry: TrueTempConfigEntry,
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_vacation_status"

    @property
    def available(self) -> bool:
        """Always available: a local computation, not fetched data — same
        override the other live-control-surface entities carry."""
        return True

    @property
    def native_value(self) -> str | None:
        result = self.coordinator.vacation_result
        return result.phase if result else None

    @property
    def extra_state_attributes(self) -> dict:
        result = self.coordinator.vacation_result
        attrs: dict = {
            "plans": serialize_plans(self.coordinator.vacation_plans),
            "active_plan_id": self.coordinator.active_plan_id,
        }
        if result is None:
            return attrs
        attrs.update(
            {
                "start_at": result.start_at.isoformat() if result.start_at else None,
                "ramp_start_at": (
                    result.ramp_start_at.isoformat() if result.ramp_start_at else None
                ),
                "return_at": result.return_at.isoformat() if result.return_at else None,
                "hours_needed": round(result.hours_needed, 2),
                "on_track": result.on_track,
                "reason": result.reason,
            }
        )
        return attrs
