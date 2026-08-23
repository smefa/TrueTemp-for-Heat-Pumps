"""DataUpdateCoordinator for TrueTemp.

Reads indoor/outdoor temperature, an optional weather forecast (wind and cloud
only), sun geometry and an optional day-ahead price; advances the offset
learner and the lag estimator; and composes the published compensated outdoor
temperature via the pure `heuristic.compute()`.

## The one timing rule that matters

The learner advances on a FIXED cadence and nothing else. Home Assistant
refreshes this coordinator both on its polling interval and whenever a watched
source entity changes state, and the second kind can fire many times a minute.
The 2026-08-07 validation traced its single largest data-quality failure to
exactly that: an estimator stepped on every event-driven refresh saw a median
6.6-minute interval against a 0.1 degC sensor resolution, which left 63% of its
transitions reading exactly zero change.

So `_async_update_data` always recomposes and republishes the output — a source
recovering should be reflected immediately — but only calls into `learner.step`
and `lag.push` once `LEARNER_STEP_SECONDS` have genuinely elapsed.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiohttp
from astral.sun import elevation as astral_elevation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.sun import get_astral_location
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_DATA_LOGGING,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_ENABLE_SOLAR_INPUT,
    CONF_ENABLE_WIND_INPUT,
    CONF_ENABLE_WEATHER_LOOKAHEAD,
    CONF_HEAT_CURVE_OFFSET_ENTITY,
    CONF_HEAT_CURVE_OFFSET_INVERT,
    CONF_HEATING_TYPE,
    CONF_INDOOR_CLIMATE_ENTITY,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OHMONWIFI_HOST,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OUTPUT_MODE,
    CONF_OUTPUT_NUMBER_ENTITY,
    CONF_PRICE_SIGNIFICANCE_FLOOR,
    CONF_VACATION_PLANS,
    CONF_WEATHER_ENTITY,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_DATA_LOGGING,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_ENABLE_SOLAR_INPUT,
    DEFAULT_ENABLE_WIND_INPUT,
    DEFAULT_ENABLE_WEATHER_LOOKAHEAD,
    DEFAULT_HEAT_CURVE_OFFSET_INVERT,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_OUTPUT_MODE,
    DEFAULT_PRICE_SIGNIFICANCE_FLOOR,
    DOMAIN,
    LEARNER_STEP_SECONDS,
    OUTPUT_MODE_HEAT_CURVE_OFFSET,
    OUTPUT_MODE_INDOOR_CLIMATE,
    OUTPUT_MODE_OUTDOOR_SPOOF,
    STARTUP_GRACE_PERIOD_MINUTES,
    UPDATE_INTERVAL_MINUTES,
)
from .data_logger import async_log_record, log_file_path
from .heuristic import (
    COLD_CAUTION_MID,
    OUTPUT_SANITY_MAX_C,
    OUTPUT_SANITY_MIN_C,
    PRICE_TIER_MID,
    SOLAR_GAIN_C,
    HeuristicInputs,
    HeuristicParams,
    HeuristicResult,
    PriceSpreadHistory,
    compute,
    heat_curve_offset_c,
    heating_hard_limit_offset_c,
    indoor_climate_offset_c,
    initial_price_spread_history,
    price_significance,
    resolve_heating_hard_limit_engaged,
    solar_effect_of,
    today_price_spread_and_median_c,
    update_price_spread_history,
)
from .holiday import HolidayResult
from .lag import (
    DEFAULT_HEATING_TYPE,
    LagResult,
    LagState,
    estimate as lag_estimate,
    initial_state as lag_initial_state,
    push as lag_push,
)
from .learner import (
    SPOOF_PER_INDOOR_C,
    TAU_I_LAG_MULTIPLE,
    LearnerInputs,
    LearnerResult,
    LearnerState,
    initial_state as learner_initial_state,
    recoverable_sag_c,
    step as learner_step,
)
from .learner_store import (
    STORAGE_VERSION as LEARNER_STORAGE_VERSION,
    deserialize as learner_deserialize,
    serialize as learner_serialize,
    store_key as learner_store_key,
)
from .vacation import (
    ACTIVE_PHASES as VACATION_ACTIVE_PHASES,
    VacationPlan,
    VacationReturnRamp,
    deserialize_plans,
    resolve_vacation_with_return_ramp,
    start_return_ramp,
)

_LOGGER = logging.getLogger(__name__)

# Debounce window for persisting learned state. `Store.async_delay_save`
# coalesces every request inside this window into one disk write and always
# flushes on Home Assistant shutdown. The real purpose is to collapse the burst
# of extra refreshes a recovering source can trigger within a few seconds.
LEARNER_STATE_SAVE_DELAY_SECONDS = 30.0

# Skip re-pushing to an output target when the value has not meaningfully moved
# since that channel's last successful push, so a real hardware register is not
# rewritten every cycle for sub-noise changes.
OUTPUT_WRITE_TOLERANCE_C = 0.05

# Force a re-push after this many cycles even when the value has not moved,
# so a target reset by something outside this integration (the OhmOnWifi
# device power-cycling, another integration touching the same number entity,
# a restart of the target's own integration) is noticed and corrected
# instead of being skipped forever by OUTPUT_WRITE_TOLERANCE_C.
OUTPUT_REASSERT_AFTER_CYCLES = 4

# Timeout for the direct OhmOnWifi local-API push. A plain HTTP GET to a device
# on the local network, so a short timeout is right — no point blocking a whole
# cycle on a device that has gone unreachable.
OHMONWIFI_REQUEST_TIMEOUT_SECONDS = 10.0

# Fallback range for the heat-curve-offset dial when the target entity's own
# `min`/`max` attributes cannot be read (entity briefly unavailable, or an
# `input_number` with no configured bounds). Matches the worked example this
# output mode was built against (NIBE's dial is -10..+10) — real entity
# bounds are always preferred and read fresh on every push.
HEAT_CURVE_OFFSET_FALLBACK_MIN_C = -10.0
HEAT_CURVE_OFFSET_FALLBACK_MAX_C = 10.0

# Fallback range for a climate entity's target temperature when its own
# `min_temp`/`max_temp` attributes cannot be read. Matches HA core's own
# `ClimateEntity` defaults, so it's the same bound such an entity would report
# anyway once available.
INDOOR_CLIMATE_FALLBACK_MIN_C = 7.0
INDOOR_CLIMATE_FALLBACK_MAX_C = 35.0

# Tolerance on the learner cadence. HA's scheduler jitters by a second or two,
# and demanding the full interval would drop roughly every other step.
LEARNER_STEP_TOLERANCE = 0.9

# How far ahead to keep forecast entries for the weather lookahead. A fetch
# bound, not a control coefficient: the pre-ramp's real window is the MEASURED
# rise time (`heuristic._term_preramp_c`), which is far shorter than this, and
# this only stops an integration that publishes a week of hourly data from
# handing `compute()` a pointlessly long series every cycle.
WEATHER_LOOKAHEAD_HORIZON_HOURS = 12.0


@dataclass(frozen=True)
class ForecastRead:
    """One cycle's read of the optional weather entity.

    The scalars are the "now" values the steady-state wind and solar terms
    have always used. The series are the hourly lookahead feed, and come from
    the `hourly` forecast ONLY: the `daily` fallback below still backfills a
    missing scalar, but a 24-hour-granularity series inside a 1-4 hour lead
    would be actively misleading rather than merely coarse.
    """

    wind_speed_ms: float
    wind_ok: bool
    cloud_coverage_pct: float | None
    cloud_ok: bool
    outdoor_series: tuple[tuple[float, float], ...] | None = None
    wind_series: tuple[tuple[float, float], ...] | None = None
    solar_series: tuple[tuple[float, float], ...] | None = None


def _forecast_offsets(
    entries: list,
    now: datetime,
    field: str,
    convert: Callable[[float], float],
) -> tuple[tuple[float, float], ...] | None:
    """One forecast field as sorted `(hours_from_now, value)` pairs, or None.

    The same float-offset conversion `_price_forecast_offsets` does, for the
    same reason: `heuristic.py` stays free of any datetime dependency. Entries
    missing the field or carrying an unparseable `datetime` are skipped
    individually rather than discarding the whole series — not every
    integration fills every field on every hour.

    Exactly one entry at or before `now` is kept — the most recent — as the
    reference the pre-ramp differences against (see `heuristic._term_preramp_c`
    on why that must be the forecast's own value and never the wall sensor's).
    Older past entries are dropped: some integrations publish their hourly
    series starting at local midnight, and differencing against an entry hours
    stale would misdate the "now" the pre-ramp measures from.
    """
    offsets: list[tuple[float, float]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get(field)
        when = entry.get("datetime")
        if raw is None or when is None:
            continue
        if isinstance(when, str):
            when = dt_util.parse_datetime(when)
        if when is None:
            continue
        try:
            value = convert(float(raw))
        except (TypeError, ValueError):
            continue
        hours = (dt_util.as_local(when) - now).total_seconds() / 3600.0
        if hours > WEATHER_LOOKAHEAD_HORIZON_HOURS:
            continue
        offsets.append((hours, value))
    if not offsets:
        return None
    offsets.sort(key=lambda item: item[0])
    last_past = -1
    for index, (hours, _value) in enumerate(offsets):
        if hours > 0:
            break
        last_past = index
    if last_past > 0:
        offsets = offsets[last_past:]
    return tuple(offsets)


def _entry_value(entry: ConfigEntry, key: str, default):
    """Options override data, both fall back to `default`."""
    return entry.options.get(key, entry.data.get(key, default))


def _state_is_usable(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _as_float(state: State, attribute: str | None = None) -> float | None:
    raw = state.attributes.get(attribute) if attribute else state.state
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _sanitize_non_finite(value):
    """Replace NaN/Infinity floats with `None`, recursing into dicts and
    lists, so `_build_log_record`'s output round-trips through strict JSON
    (`json.dumps(..., allow_nan=False)` in data_logger.py) instead of
    emitting the bare `NaN`/`Infinity` tokens that break replay parsers."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_non_finite(v) for v in value]
    return value


class TrueTempCoordinator(DataUpdateCoordinator[HeuristicResult]):
    """Fetches inputs each cycle, advances learning on a fixed clock, and
    composes the published result."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )

        # --- Learned state ---------------------------------------------------
        # The offset table and the lag buffer are the only things this
        # integration learns. Both are persisted; neither is ever invalidated
        # by a configuration change, unlike the estimator state this replaced.
        self.learner_state: LearnerState = learner_initial_state()
        self.lag_state: LagState = lag_initial_state(UPDATE_INTERVAL_MINUTES)
        self.learner_result: LearnerResult | None = None
        self.lag_result: LagResult | None = None
        self._learner_last_step_s: float | None = None
        # Whether the learner advanced on the most recent cycle. Recorded in
        # the log so an offline replay can separate decision cycles from bare
        # republishes — see `_build_log_record`.
        self._learner_stepped: bool = False
        # Rolling record of past days' price spread, the seasonal half of
        # `heuristic.price_significance()`. Persisted alongside the learner and
        # lag state (same Store, same payload — see `learner_store.py`) but
        # updated every cycle rather than on the learner's fixed cadence: it is
        # a running max, which only ever needs sampling more often, never
        # produces the zero-transition noise a rate estimator would from
        # irregular polling. See `_async_update_data`.
        self.price_spread_history: PriceSpreadHistory = initial_price_spread_history()
        self._store: Store[dict] = Store(
            hass, LEARNER_STORAGE_VERSION, learner_store_key(entry.entry_id)
        )

        # --- Error tracking ----------------------------------------------------
        # HA's DataUpdateCoordinator.last_exception is sticky - it is set on
        # failure but never cleared on a later successful update - so reading
        # it directly for a "last error" display would show a stale message
        # forever after the sensor recovers. Track our own timestamp instead,
        # scoped to the exact call that can raise UpdateFailed, and clear it as
        # soon as that call succeeds again.
        self.last_error_at: datetime | None = None

        # When this coordinator was constructed, i.e. this config entry's setup
        # (HA start, integration reload, or an options change). Used to hold off
        # Repairs issues for a short grace period — see `_sync_source_issue`.
        self._setup_at: datetime = dt_util.utcnow()

        # --- Output push channels -------------------------------------------
        # Independent, and both pushed every cycle when configured. Each tracks
        # its own last-written value so an unchanged value does not keep
        # rewriting a device register, and so one channel failing never
        # suppresses a retry on the other.
        self._last_ohmonwifi_value_c: float | None = None
        self._last_output_number_value_c: float | None = None
        self._last_heat_curve_offset_value_c: int | None = None
        self._last_indoor_climate_value_c: float | None = None
        # When each channel last wrote successfully, so a stale-but-unchanged
        # cached value can be force-reasserted after OUTPUT_REASSERT_AFTER_CYCLES
        # even though it looks unchanged from here.
        self._last_ohmonwifi_pushed_at: datetime | None = None
        self._last_output_number_pushed_at: datetime | None = None
        self._last_heat_curve_offset_pushed_at: datetime | None = None
        self._last_indoor_climate_pushed_at: datetime | None = None
        # Whether this integration switched the OhmOnWifi device's relay to
        # bypass because the outdoor sensor it spoofs from went unavailable —
        # see `_async_ohmonwifi_relay_failsafe`. Tracked so recovery only
        # re-engages a bypass WE caused, never one set from the device's own
        # web UI.
        self._ohmonwifi_forced_bypass: bool = False

        # --- Live control surface (restored by the entities) -----------------
        # All in-memory rather than config options: an options change reloads
        # the entry, and these get nudged often by hand, schedules and
        # automations. Each entity restores its own value via RestoreEntity;
        # the values here are only the pre-restore defaults.
        #
        # Compensation ships OFF: the occupant opts in once they trust the
        # recommendation, rather than the integration silently steering the
        # heat pump from the moment it's installed.
        self.is_active: bool = False
        self.indoor_target_c: float = _entry_value(
            entry, CONF_INDOOR_TARGET_TEMPERATURE, DEFAULT_INDOOR_TARGET_TEMPERATURE
        )
        self.price_comfort_tier: str = PRICE_TIER_MID
        self.cold_caution: str = COLD_CAUTION_MID

        # Vacation mode master switch: same "live, restorable, never a config
        # option" category as the fields above — a hand-flipped kill switch,
        # restored by switch.py's own RestoreEntity, independent of each
        # plan's own `enabled` flag. The plan list itself is a config option
        # (see `vacation_plans` below, edited via the options-flow CRUD in
        # config_flow.py) since it is authored once and edited rarely, not
        # nudged per trip the way the old single holiday scenario's dates
        # were. `vacation_result`/`active_plan_id`/`_effective_target_c` are
        # recomputed every cycle in `_async_update_data`, not restored.
        # Defaults on (unlike the old HolidayModeSwitch, which defaulted
        # off) — per §Phase 4 of docs/plan_vacation_plans.md: with no plans
        # configured yet, an armed-but-empty switch is a no-op, so there is
        # no cost to starting armed and no need for a first-run flip.
        self.vacation_armed: bool = True
        self.vacation_result: HolidayResult | None = None
        self.active_plan_id: str | None = None
        # An in-progress ramp back to `indoor_target_c`, started when
        # `vacation_armed` is switched off while a plan is actively sagging
        # the house — see `_async_update_data`'s call site and
        # `vacation.VacationReturnRamp`'s docstring for why this can't just
        # live inside `resolve_vacation_with_return_ramp()`'s own arguments.
        # Not restored across a restart: resuming mid-ramp is a nicety, not
        # a correctness requirement, and losing it just means one extra
        # restart degrades to the old instant-snap behaviour for that one
        # disarm.
        self._vacation_return_ramp: VacationReturnRamp | None = None
        # What `indoor_target_c` currently feeds actually feeds THIS instead,
        # everywhere learning/price/output need "the target right now" — see
        # the three call sites this replaces, below. Equal to
        # `indoor_target_c` whenever no vacation plan is sagging the house,
        # so this is a no-op when the feature is unused.
        self._effective_target_c: float = self.indoor_target_c

        # The exact price forecast this cycle's decision used, stashed for the
        # data logger — forecasts get revised, so realised prices are not a
        # substitute for what was known at decision time.
        self._last_price_forecast: tuple[tuple[float, float], ...] | None = None
        self._last_outdoor_forecast: tuple[tuple[float, float], ...] | None = None
        self._last_solar_forecast: tuple[tuple[float, float], ...] | None = None

    # --- Configuration views -------------------------------------------------

    def watched_entity_ids(self) -> list[str]:
        """Source entities whose state changes should trigger an immediate
        refresh instead of waiting for the poll.

        HA's DataUpdateCoordinator skips notifying entities on consecutive
        identical failures, so there is no external visibility into whether its
        background timer is still quietly retrying. A real incident had this
        integration stuck unavailable for six minutes after its (cloud-polled,
        slow to reconnect) source had already recovered. Reacting to the
        sources' own state changes fixes that.

        Note this only republishes: learning still advances on its own clock.
        """
        keys = (
            CONF_OUTDOOR_TEMP_SENSOR,
            CONF_INDOOR_TEMP_SENSOR,
            CONF_NORDPOOL_PRICE_ENTITY,
        )
        return [
            entity_id
            for entity_id in (_entry_value(self.entry, key, None) for key in keys)
            if entity_id
        ]

    @property
    def price_configured(self) -> bool:
        return bool(_entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None))

    @property
    def price_enabled(self) -> bool:
        return bool(
            _entry_value(
                self.entry,
                CONF_ENABLE_PRICE_COMPENSATION,
                DEFAULT_ENABLE_PRICE_COMPENSATION,
            )
        )

    @property
    def output_mode(self) -> str:
        return _entry_value(self.entry, CONF_OUTPUT_MODE, DEFAULT_OUTPUT_MODE)

    @property
    def output_number_entity_id(self) -> str | None:
        return _entry_value(self.entry, CONF_OUTPUT_NUMBER_ENTITY, None) or None

    @property
    def ohmonwifi_host(self) -> str | None:
        return _entry_value(self.entry, CONF_OHMONWIFI_HOST, None) or None

    @property
    def heat_curve_offset_entity_id(self) -> str | None:
        return _entry_value(self.entry, CONF_HEAT_CURVE_OFFSET_ENTITY, None) or None

    @property
    def indoor_climate_entity_id(self) -> str | None:
        return _entry_value(self.entry, CONF_INDOOR_CLIMATE_ENTITY, None) or None

    @property
    def heat_curve_offset_invert(self) -> bool:
        return bool(
            _entry_value(
                self.entry,
                CONF_HEAT_CURVE_OFFSET_INVERT,
                DEFAULT_HEAT_CURVE_OFFSET_INVERT,
            )
        )

    @property
    def data_logging_enabled(self) -> bool:
        return bool(
            _entry_value(
                self.entry, CONF_ENABLE_DATA_LOGGING, DEFAULT_ENABLE_DATA_LOGGING
            )
        )

    @property
    def data_log_path(self) -> str | None:
        if not self.data_logging_enabled:
            return None
        return str(log_file_path(self.hass, self.entry.entry_id))

    @property
    def solar_input_enabled(self) -> bool:
        return bool(
            _entry_value(
                self.entry, CONF_ENABLE_SOLAR_INPUT, DEFAULT_ENABLE_SOLAR_INPUT
            )
        )

    @property
    def wind_input_enabled(self) -> bool:
        return bool(
            _entry_value(self.entry, CONF_ENABLE_WIND_INPUT, DEFAULT_ENABLE_WIND_INPUT)
        )

    @property
    def lookahead_enabled(self) -> bool:
        return bool(
            _entry_value(
                self.entry,
                CONF_ENABLE_WEATHER_LOOKAHEAD,
                DEFAULT_ENABLE_WEATHER_LOOKAHEAD,
            )
        )

    @property
    def heating_type(self) -> str:
        return _entry_value(self.entry, CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE)

    @property
    def price_significance_floor_c(self) -> float:
        """The absolute-money backstop for `heuristic.price_significance()`,
        in the price sensor's own native units — see CONF_PRICE_SIGNIFICANCE_FLOOR's
        docstring in const.py."""
        return float(
            _entry_value(
                self.entry,
                CONF_PRICE_SIGNIFICANCE_FLOOR,
                DEFAULT_PRICE_SIGNIFICANCE_FLOOR,
            )
        )

    @property
    def weather_needed(self) -> bool:
        """Whether the optional weather entity is worth reading at all. Its
        consumers are the wind and solar terms and the forecast lookahead, so
        with all three off the per-cycle forecast service calls are skipped
        entirely. The lookahead is a separate disjunct because its outdoor
        pre-ramp needs the weather entity even with both steady-state weather
        terms switched off — a real config combination."""
        return (
            self.solar_input_enabled
            or self.wind_input_enabled
            or self.lookahead_enabled
        )

    @property
    def comfort_min_c(self) -> float:
        """The absolute indoor floor — see CONF_COMFORT_MIN_C's docstring in
        const.py. Public (unlike most `_entry_value` reads, which stay
        inlined in `_params()`) because `number.py`'s holiday-target entity
        binds its `native_min_value` to this same live value."""
        return float(_entry_value(self.entry, CONF_COMFORT_MIN_C, DEFAULT_COMFORT_MIN_C))

    @property
    def vacation_plans(self) -> list[VacationPlan]:
        """The user-authored plan list, read fresh from `entry.options` every
        cycle — same live-config-option pattern as `output_mode`/
        `comfort_min_c` above, not cached, since an options-flow save reloads
        the config entry anyway. `deserialize_plans` never raises and drops
        any individually corrupt plan rather than the whole list."""
        return deserialize_plans(_entry_value(self.entry, CONF_VACATION_PLANS, []))

    def _params(self) -> HeuristicParams:
        """This cycle's occupant preferences. No control gains live here."""
        return HeuristicParams(
            indoor_target_c=self._effective_target_c,
            user_indoor_target_c=self.indoor_target_c,
            comfort_min_c=self.comfort_min_c,
            enable_price_compensation=self.price_enabled,
            price_comfort_tier=self.price_comfort_tier,
            cold_caution=self.cold_caution,
            enable_solar_input=self.solar_input_enabled,
            enable_wind_input=self.wind_input_enabled,
            enable_weather_lookahead=self.lookahead_enabled,
            spoof_per_indoor_c=SPOOF_PER_INDOOR_C,
        )

    # --- Source health as Repairs ---------------------------------------------
    # HA's own notification bell surfaces active Repairs issues, so this is the
    # "default trigger" for posting a real error/warning without a bespoke
    # automation. One issue per source, keyed by entry so multiple zones don't
    # collide; created while the source is down, deleted the moment it is not
    # (or the moment setup is still within its startup grace period — sources
    # loaded by other integrations routinely read unavailable for the first
    # minute or so after a restart, which is not a real outage).

    _SOURCE_ISSUE_KEYS = (
        "outdoor_sensor_unavailable",
        "indoor_sensor_unavailable",
        "wind_forecast_unavailable",
        "cloud_sun_forecast_unavailable",
        "price_unavailable",
    )

    def in_startup_grace_period(self) -> bool:
        elapsed = dt_util.utcnow() - self._setup_at
        return elapsed < timedelta(minutes=STARTUP_GRACE_PERIOD_MINUTES)

    def _sync_source_issue(
        self,
        key: str,
        ok: bool,
        severity: ir.IssueSeverity,
        entity_id: str | None,
    ) -> None:
        issue_id = f"{key}_{self.entry.entry_id}"
        if ok or self.in_startup_grace_period():
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=key,
            translation_placeholders={
                "name": self.entry.title,
                "entity_id": entity_id or "",
            },
        )

    def _sync_optional_source_issue(
        self, key: str, enabled: bool, ok: bool, entity_id: str | None
    ) -> None:
        """Same as `_sync_source_issue`, but for a source that can be switched
        off entirely — in which case it is never a problem."""
        self._sync_source_issue(
            key, ok=not enabled or ok, severity=ir.IssueSeverity.WARNING, entity_id=entity_id
        )

    def clear_source_issues(self) -> None:
        """Drop every issue this entry could own, e.g. on unload — otherwise a
        Repair raised while broken outlives the integration that raised it."""
        for key in self._SOURCE_ISSUE_KEYS:
            ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{self.entry.entry_id}")

    # --- Source reads --------------------------------------------------------

    def _read_indoor_temp_c(self) -> tuple[float | None, bool]:
        """Return (indoor_temp_c, available).

        Soft-degrades: without an indoor reading the learner freezes and holds
        its last offset, which is a safe hold rather than a failure.
        """
        entity_id = _entry_value(self.entry, CONF_INDOOR_TEMP_SENSOR, None)
        if not entity_id:
            _LOGGER.warning("No indoor temperature sensor configured")
            return None, False
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.warning(
                "Indoor temperature sensor %s is unavailable; holding the learned "
                "offset and pausing learning this cycle",
                entity_id,
            )
            return None, False
        value = _as_float(state)
        if value is None:
            _LOGGER.warning("Indoor temperature sensor %s has no numeric state", entity_id)
            return None, False
        unit = state.attributes.get("unit_of_measurement", UnitOfTemperature.CELSIUS)
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS), True

    def _read_raw_outdoor_temp_c(self) -> float:
        """The one hard-required source: without it there is nothing to publish
        at all, so this is the single read that still fails the update."""
        entity_id = _entry_value(self.entry, CONF_OUTDOOR_TEMP_SENSOR, None)
        if not entity_id:
            raise UpdateFailed("No outdoor temperature sensor configured")
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            raise UpdateFailed(f"Outdoor temperature sensor {entity_id} is unavailable")
        value = _as_float(state)
        if value is None:
            raise UpdateFailed(
                f"Outdoor temperature sensor {entity_id} has no numeric state"
            )
        unit = state.attributes.get("unit_of_measurement", UnitOfTemperature.CELSIUS)
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)

    async def _read_forecast(self) -> ForecastRead:
        """Read the optional weather entity: scalars now, series for lookahead.

        Wind and cloud are tracked independently: not every weather integration
        provides both, and a missing wind value must not discard a perfectly
        good cloud reading. Tries hourly then daily, filling in whichever field
        is still missing. Any failure soft-degrades — the weather entity is
        enrichment only, since raw outdoor temperature comes from a dedicated
        sensor.
        """
        empty = ForecastRead(0.0, False, None, False)
        if not self.weather_needed:
            return empty

        weather_entity_id = _entry_value(self.entry, CONF_WEATHER_ENTITY, None)
        if not weather_entity_id:
            _LOGGER.debug("No weather entity configured; wind/solar terms contribute 0")
            return empty
        weather_state = self.hass.states.get(weather_entity_id)
        if not _state_is_usable(weather_state):
            _LOGGER.warning(
                "Weather entity %s is unavailable; continuing without wind/solar data",
                weather_entity_id,
            )
            return empty

        wind_speed_ms: float | None = None
        cloud_coverage_pct: float | None = None
        hourly: list = []
        # With both steady-state terms off, the only reason to be here is the
        # lookahead — and that reads the hourly series alone, so the daily
        # fallback (which exists purely to backfill a missing scalar) is a
        # service call with nothing to do.
        scalars_needed = self.wind_input_enabled or self.solar_input_enabled

        for forecast_type in ("hourly", "daily"):
            if wind_speed_ms is not None and cloud_coverage_pct is not None:
                break
            if forecast_type == "daily" and not scalars_needed:
                break
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": weather_entity_id, "type": forecast_type},
                    blocking=True,
                    return_response=True,
                )
                forecast = response[weather_entity_id]["forecast"]
                if not forecast:
                    continue
                if forecast_type == "hourly":
                    hourly = list(forecast)
                first = forecast[0]
                if wind_speed_ms is None:
                    raw_wind = first.get("wind_speed")
                    if raw_wind is not None:
                        wind_unit = weather_state.attributes.get(
                            "wind_speed_unit", UnitOfSpeed.METERS_PER_SECOND
                        )
                        wind_speed_ms = SpeedConverter.convert(
                            float(raw_wind), wind_unit, UnitOfSpeed.METERS_PER_SECOND
                        )
                if cloud_coverage_pct is None:
                    raw_cloud = first.get("cloud_coverage")
                    if raw_cloud is not None:
                        cloud_coverage_pct = float(raw_cloud)
            except Exception as err:  # noqa: BLE001 - soft-degrade on any failure
                _LOGGER.debug(
                    "Forecast type %s unavailable for %s: %s",
                    forecast_type,
                    weather_entity_id,
                    err,
                )

        wind_ok = wind_speed_ms is not None
        cloud_ok = cloud_coverage_pct is not None
        if not wind_ok and self.wind_input_enabled:
            _LOGGER.warning(
                "Could not retrieve a wind forecast for %s; wind term contributes 0",
                weather_entity_id,
            )
        if not cloud_ok and self.solar_input_enabled:
            _LOGGER.warning(
                "Could not retrieve a cloud/sun forecast for %s; assuming clear sky",
                weather_entity_id,
            )
        outdoor_series, wind_series, solar_series = self._lookahead_series(
            hourly, weather_state
        )
        return ForecastRead(
            wind_speed_ms=wind_speed_ms if wind_ok else 0.0,
            wind_ok=wind_ok,
            cloud_coverage_pct=cloud_coverage_pct,
            cloud_ok=cloud_ok,
            outdoor_series=outdoor_series,
            wind_series=wind_series,
            solar_series=solar_series,
        )

    def _lookahead_series(
        self, hourly: list, weather_state: State
    ) -> tuple[
        tuple[tuple[float, float], ...] | None,
        tuple[tuple[float, float], ...] | None,
        tuple[tuple[float, float], ...] | None,
    ]:
        """The three `(hours_from_now, value)` series `compute()`'s pre-ramp
        reads, or all-None when the lookahead is off or there is no hourly
        forecast.

        `get_forecasts` reports each field in the weather entity's OWN unit,
        so temperature and wind get the same conversion the scalar reads
        above already get. The solar series is the 0..1 output of
        `solar_effect_of` evaluated at each future hour, computed here rather
        than in `heuristic.py` so that module never has to know what a
        datetime or a sun position is.
        """
        if not self.lookahead_enabled or not hourly:
            return None, None, None
        now = dt_util.now()
        temp_unit = weather_state.attributes.get(
            "temperature_unit", UnitOfTemperature.CELSIUS
        )
        wind_unit = weather_state.attributes.get(
            "wind_speed_unit", UnitOfSpeed.METERS_PER_SECOND
        )
        outdoor_series = _forecast_offsets(
            hourly,
            now,
            "temperature",
            lambda v: TemperatureConverter.convert(
                v, temp_unit, UnitOfTemperature.CELSIUS
            ),
        )
        wind_series = _forecast_offsets(
            hourly,
            now,
            "wind_speed",
            lambda v: SpeedConverter.convert(
                v, wind_unit, UnitOfSpeed.METERS_PER_SECOND
            ),
        )
        cloud_series = _forecast_offsets(hourly, now, "cloud_coverage", float)
        solar_series: tuple[tuple[float, float], ...] | None = None
        if cloud_series:
            try:
                location, _ = get_astral_location(self.hass)
                solar_series = tuple(
                    (
                        t_h,
                        solar_effect_of(
                            astral_elevation(
                                location.observer, now + timedelta(hours=t_h)
                            ),
                            cloud,
                        ),
                    )
                    for t_h, cloud in cloud_series
                )
            except Exception as err:  # noqa: BLE001 - soft-degrade on any failure
                _LOGGER.debug("Could not project future sun elevation: %s", err)
        return outdoor_series, wind_series, solar_series

    def _read_sun_elevation(self) -> float:
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None:
            return 0.0
        try:
            return float(sun_state.attributes.get("elevation", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _read_price(self) -> tuple[float | None, bool]:
        entity_id = _entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None)
        if not entity_id:
            return None, False
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.warning(
                "Price entity %s is unavailable; price compensation contributes 0",
                entity_id,
            )
            return None, False
        value = _as_float(state)
        if value is None:
            _LOGGER.warning("Price entity %s has no numeric state", entity_id)
            return None, False
        return value, True

    def _read_price_entries(self) -> list[tuple[datetime, float]]:
        """Parse the price sensor's `raw_today`/`raw_tomorrow` attributes (the
        well-known Nordpool shape: lists of {start, end, value}) into
        (local start datetime, price) pairs."""
        entity_id = _entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None)
        if not entity_id:
            return []
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            return []
        entries: list[tuple[datetime, float]] = []
        for attr in ("raw_today", "raw_tomorrow"):
            raw = state.attributes.get(attr)
            if not isinstance(raw, (list, tuple)):
                continue
            for item in raw:
                if not isinstance(item, dict):
                    continue
                start = item.get("start")
                value = item.get("value")
                if isinstance(start, str):
                    start = dt_util.parse_datetime(start)
                if start is None or value is None:
                    continue
                try:
                    entries.append((dt_util.as_local(start), float(value)))
                except (TypeError, ValueError):
                    continue
        return entries

    def price_forecast_series(self) -> list[dict[str, object]]:
        """Today/tomorrow's price curve as the card's graph reads it: one
        {start, price} dict per slot, in chronological order. Public (unlike
        `_read_price_entries`) because sensor.py publishes this verbatim as an
        attribute for truetemp-card.js to plot — the heuristic engine itself
        only wants the hours-from-now shape `_price_forecast_offsets` builds.
        """
        return [
            {"start": start.isoformat(), "price": price}
            for start, price in sorted(self._read_price_entries(), key=lambda e: e[0])
        ]

    def _price_forecast_offsets(self) -> tuple[tuple[float, float], ...] | None:
        """Day-ahead price as (hours_from_now, price) pairs, or None.

        None means the price logic does nothing at all this cycle — without a
        day-distribution there is no principled way to know whether the current
        price is high (see `heuristic._price_band_info`).
        """
        entries = self._read_price_entries()
        if not entries:
            return None
        now = dt_util.now()
        return tuple(
            ((start - now).total_seconds() / 3600.0, price)
            for start, price in sorted(entries, key=lambda e: e[0])
        )

    # --- Learning ------------------------------------------------------------

    def _lag_result(self) -> LagResult:
        """The current lag estimate, recomputed lazily and cached per step."""
        if self.lag_result is None:
            self.lag_result = lag_estimate(self.lag_state, self.heating_type)
        return self.lag_result

    def _due_for_learner_step(self) -> bool:
        """Whether enough real time has elapsed to advance learning.

        This is the guard that keeps event-driven refreshes out of the learned
        state — see the module docstring.
        """
        now = time.monotonic()
        last = self._learner_last_step_s
        if last is None:
            return True
        return (now - last) >= LEARNER_STEP_SECONDS * LEARNER_STEP_TOLERANCE

    def _advance_learning(
        self,
        indoor_temp_c: float | None,
        indoor_ok: bool,
        raw_outdoor_temp_c: float,
        sun_elevation_deg: float,
        cloud_coverage_pct: float | None,
    ) -> bool:
        """Step the lag estimator and the offset learner exactly one cycle.

        Returns whether the learner actually advanced, which the data logger
        records so an offline replay can tell decision cycles apart from bare
        republishes.

        Wrapped so a bug in either can never break the published output: on
        failure the previous result stands, which is a held offset.

        The heating-hard-limit and price-braking flags the learner needs come
        from `heuristic.compute`, which needs the learner's offset — so the
        hard limit is recomputed directly here via
        `resolve_heating_hard_limit_engaged` (reading this same PREVIOUS
        cycle's engaged flag for its hysteresis) and price braking is read
        from the PREVIOUS cycle too. One cycle of staleness on a 15-minute
        clock is immaterial against lags measured in hours, and it avoids an
        ordering cycle between the two. `compute()` below recomputes the
        identical value off the identical previous cycle, so the two never
        disagree.
        """
        now = time.monotonic()
        last = self._learner_last_step_s
        dt_hours = (now - last) / 3600.0 if last is not None else 0.0
        self._learner_last_step_s = now

        # Same formula and same SOLAR_GAIN_C the feedforward term in
        # heuristic.compute() uses, threaded across the module boundary as a
        # plain float — see learner.py's module docstring and
        # HeuristicParams.spoof_per_indoor_c for why neither pure module
        # imports the other. Gated on solar_input_enabled, NOT on whether the
        # weather-derived solar_effect happens to be nonzero: a disabled
        # input means the occupant has told us the indoor sensor gets no real
        # solar gain (e.g. a basement), so the correction must be exactly 0.0
        # regardless of what the sun/cloud formula alone would say — mirrors
        # heuristic.compute()'s own `enable_solar_input` gate on
        # sun_adjustment_c.
        # `_read_sun_elevation` already falls back to 0.0 (below horizon) when
        # `sun.sun` is missing or unparsable, which makes solar_effect_of
        # return 0.0 too — a degenerate reading degrades to "no solar gain",
        # never a wild correction, so no extra guard is needed here.
        estimated_solar_gain_c = 0.0
        if self.solar_input_enabled:
            solar_effect = solar_effect_of(sun_elevation_deg, cloud_coverage_pct)
            estimated_solar_gain_c = SOLAR_GAIN_C * solar_effect

        try:
            self.lag_state = lag_push(
                self.lag_state,
                self.learner_state.prev_offset_c,
                indoor_temp_c,
                self._effective_target_c,
            )
            self.lag_result = lag_estimate(self.lag_state, self.heating_type)

            previous = self.data
            self.learner_state, self.learner_result = learner_step(
                self.learner_state,
                LearnerInputs(
                    now_s=now,
                    dt_hours=dt_hours,
                    indoor_temp_c=indoor_temp_c,
                    indoor_data_available=indoor_ok,
                    target_c=self._effective_target_c,
                    outdoor_temp_c=raw_outdoor_temp_c,
                    heating_hard_limit_engaged=resolve_heating_hard_limit_engaged(
                        raw_outdoor_temp_c,
                        bool(previous and previous.heating_hard_limit_engaged),
                    ),
                    is_active=self.is_active,
                    price_braking=bool(previous and previous.price_braking),
                    weather_preramp=bool(previous and previous.weather_preramp_active),
                    sun_precool=bool(previous and previous.sun_precool_active),
                    rise_hours=self.lag_result.rise_hours,
                    estimated_solar_gain_c=estimated_solar_gain_c,
                ),
            )
            _LOGGER.debug(
                "Learner: %s | Lag: %s",
                self.learner_result.reason,
                self.lag_result.reason,
            )
            self._schedule_state_save()
            return True
        except Exception as err:  # noqa: BLE001 - never break output over learning
            _LOGGER.warning("Learner step failed (holding previous offset): %s", err)
            return False

    def _schedule_state_save(self) -> None:
        try:
            self._store.async_delay_save(
                lambda: learner_serialize(
                    self.learner_state, self.lag_state, self.price_spread_history
                ),
                LEARNER_STATE_SAVE_DELAY_SECONDS,
            )
        except Exception as err:  # noqa: BLE001 - persistence is best-effort
            _LOGGER.warning("Could not schedule learned-state save (ignored): %s", err)

    async def async_load_state(self) -> None:
        """Restore learned state before the first refresh.

        Strictly additive and self-guarded: an empty, corrupt or
        version-mismatched store is silently discarded in favour of a clean
        cold start, and any storage error is logged and swallowed.
        """
        try:
            stored = await self._store.async_load()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not load learned state (cold starting): %s", err)
            return
        if stored is None:
            return
        restored = learner_deserialize(stored)
        if restored is None:
            _LOGGER.info("Stored learned state was not usable; starting fresh")
            return
        self.learner_state, self.lag_state, self.price_spread_history = restored
        _LOGGER.debug(
            "Restored learned state: %d samples, %d lag samples",
            self.learner_state.total_samples,
            len(self.lag_state.indoors_c),
        )

    async def async_save_state_now(self) -> None:
        """Flush any pending debounced write before teardown. HA only
        auto-flushes `async_delay_save` on full shutdown, not on an entry
        reload, so without this the most recent learning is lost on every
        options change."""
        try:
            await self._store.async_save(
                learner_serialize(
                    self.learner_state, self.lag_state, self.price_spread_history
                )
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not save learned state (ignored): %s", err)

    # --- The cycle -----------------------------------------------------------

    async def _async_update_data(self) -> HeuristicResult:
        try:
            raw_outdoor_temp_c = self._read_raw_outdoor_temp_c()
        except UpdateFailed:
            self.last_error_at = dt_util.utcnow()
            self._sync_source_issue(
                "outdoor_sensor_unavailable",
                ok=False,
                severity=ir.IssueSeverity.ERROR,
                entity_id=_entry_value(self.entry, CONF_OUTDOOR_TEMP_SENSOR, None),
            )
            await self._async_ohmonwifi_relay_failsafe(sensor_ok=False)
            raise
        self.last_error_at = None
        self._sync_source_issue(
            "outdoor_sensor_unavailable", ok=True, severity=ir.IssueSeverity.ERROR, entity_id=None
        )
        await self._async_ohmonwifi_relay_failsafe(sensor_ok=True)
        indoor_temp_c, indoor_ok = self._read_indoor_temp_c()
        forecast_read = await self._read_forecast()
        wind_speed_ms = forecast_read.wind_speed_ms
        wind_ok = forecast_read.wind_ok
        cloud_coverage_pct = forecast_read.cloud_coverage_pct
        cloud_ok = forecast_read.cloud_ok
        sun_elevation_deg = self._read_sun_elevation()
        current_price, price_ok = self._read_price()
        self._sync_source_issue(
            "indoor_sensor_unavailable",
            ok=indoor_ok,
            severity=ir.IssueSeverity.WARNING,
            entity_id=_entry_value(self.entry, CONF_INDOOR_TEMP_SENSOR, None),
        )
        self._sync_optional_source_issue(
            "wind_forecast_unavailable",
            self.wind_input_enabled,
            wind_ok,
            _entry_value(self.entry, CONF_WEATHER_ENTITY, None),
        )
        self._sync_optional_source_issue(
            "cloud_sun_forecast_unavailable",
            self.solar_input_enabled,
            cloud_ok,
            _entry_value(self.entry, CONF_WEATHER_ENTITY, None),
        )
        self._sync_optional_source_issue(
            "price_unavailable",
            self.price_configured,
            price_ok,
            _entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None),
        )
        price_forecast = self._price_forecast_offsets()
        self._last_price_forecast = price_forecast
        self._last_outdoor_forecast = forecast_read.outdoor_series
        self._last_solar_forecast = forecast_read.solar_series

        # Price significance: a taper on braking/pre-charge authority for days
        # whose price swing is economically trivial — see
        # `heuristic.price_significance()` for the full design and the field
        # data that justified it. The seasonal history only ever records a
        # REAL observation: with no usable forecast this cycle, `today_band`
        # is None and the persisted history — and hence the seasonal
        # reference and the auto floor other days compare against — is left
        # untouched rather than folding in a bogus zero.
        today_band = today_price_spread_and_median_c(price_forecast)
        today_spread: float | None = None
        price_significance_factor = 1.0
        seasonal_reference_spread_c: float | None = None
        if today_band is not None:
            today_spread, today_median = today_band
            updated_history = update_price_spread_history(
                self.price_spread_history,
                dt_util.now().date().isoformat(),
                today_spread,
                today_median,
            )
            # `update_price_spread_history` returns the SAME object (not an
            # equal one) when neither today's spread nor its median moved, so
            # this identity check is what keeps a quiet day from scheduling a
            # write every single cycle.
            if updated_history is not self.price_spread_history:
                self.price_spread_history = updated_history
                self._schedule_state_save()
            price_significance_factor, seasonal_reference_spread_c = price_significance(
                today_spread,
                today_median,
                self.price_spread_history,
                self.price_significance_floor_c,
            )

        # Vacation setback: resolve this cycle's phase/target before anything
        # below reads `_effective_target_c`. `rise_hours` comes from the
        # PREVIOUS cycle's lag estimate (via `_lag_result()`, cached until
        # `_advance_learning` below recomputes it) — same one-cycle-stale
        # convention `_advance_learning`'s own docstring uses for the hard
        # limit and price braking, and immaterial against lags measured in
        # hours. Each plan's own `min_temp_c` is clamped to
        # `HOLIDAY_TARGET_MIN_C` inside `resolve_plan()`, NOT `comfort_min_c`
        # — nobody is home during a vacation, so the setback is allowed to
        # sag past the occupied-house comfort floor. See
        # `HOLIDAY_TARGET_MIN_C`'s docstring in holiday.py.
        #
        # No per-plan "done" auto-disarm here: unlike the old single scenario,
        # `vacation_armed` is a persistent master kill switch shared by every
        # plan (§3 of docs/plan_vacation_plans.md) — one `once` plan finishing
        # must not silently disable plans still scheduled. A finished `once`
        # plan simply stops matching (`occurrence_window` keeps returning its
        # same past window, so `resolve_plan` reports `done` for it forever);
        # `weekly`/`yearly` plans roll over to their next occurrence on their
        # own, never reaching `done`.
        #
        # holiday.py/vacation.py are stdlib-only and build their own
        # start/ramp/return datetimes as naive local time
        # (`datetime.combine(date, time)`) — see those modules' docstrings.
        # `dt_util.now()` is timezone-aware, which raises `TypeError` when
        # compared against a naive datetime, so the tzinfo is stripped here
        # to hand them the naive local wall-clock time they actually expect.
        vacation_now = dt_util.now().replace(tzinfo=None)

        # Disarm mid-trip needs its own ramp back to normal, the same as a
        # plan's scheduled return — otherwise `resolve_vacation_with_return_
        # ramp()` has nothing to ramp FROM once `vacation_armed` is False,
        # since it (like `resolve_vacation()`) only ever computes from the
        # plan list. `self.vacation_result` here is still LAST cycle's
        # value, from while the switch was still armed, which is exactly
        # the target the house was actually sagged to at the moment of
        # disarm. Only fires once per disarm: after the first cycle,
        # `_vacation_return_ramp` is no longer `None`.
        if (
            not self.vacation_armed
            and self._vacation_return_ramp is None
            and self.vacation_result is not None
            and self.vacation_result.phase in VACATION_ACTIVE_PHASES
        ):
            self._vacation_return_ramp = start_return_ramp(
                now=vacation_now,
                from_target_c=self.vacation_result.target_c,
                normal_target_c=self.indoor_target_c,
                rise_hours=self._lag_result().rise_hours,
            )

        self.vacation_result, self.active_plan_id, self._vacation_return_ramp = (
            resolve_vacation_with_return_ramp(
                now=vacation_now,
                master_armed=self.vacation_armed,
                plans=self.vacation_plans,
                normal_target_c=self.indoor_target_c,
                rise_hours=self._lag_result().rise_hours,
                return_ramp=self._vacation_return_ramp,
            )
        )
        self._effective_target_c = self.vacation_result.target_c

        # Learning advances on its own clock; republishing happens every time.
        self._learner_stepped = self._due_for_learner_step() and self._advance_learning(
            indoor_temp_c,
            indoor_ok,
            raw_outdoor_temp_c,
            sun_elevation_deg,
            cloud_coverage_pct,
        )

        lag = self._lag_result()
        learner = self.learner_result
        offset_c = learner.offset_c if learner else 0.0
        # How much sag the current bin has been observed to buy back within one
        # integrator time constant — the window the loop targets anyway.
        recovery_window_h = lag.rise_hours * TAU_I_LAG_MULTIPLE
        recoverable = (
            recoverable_sag_c(learner.close_rate_c_per_h, recovery_window_h)
            if learner
            else 0.0
        )

        result = compute(
            HeuristicInputs(
                indoor_temp_c=indoor_temp_c,
                indoor_data_available=indoor_ok,
                raw_outdoor_temp_c=raw_outdoor_temp_c,
                wind_speed_ms=wind_speed_ms,
                wind_data_available=wind_ok,
                sun_elevation_deg=sun_elevation_deg,
                cloud_coverage_pct=cloud_coverage_pct,
                cloud_data_available=cloud_ok,
                current_price=current_price,
                price_data_available=price_ok,
                price_forecast=price_forecast,
                outdoor_forecast=forecast_read.outdoor_series,
                wind_forecast_ms=forecast_read.wind_series,
                solar_forecast=forecast_read.solar_series,
                learned_offset_c=offset_c,
                fall_minutes=lag.fall_minutes,
                rise_minutes=lag.rise_minutes,
                recoverable_sag_c=recoverable,
                price_significance_factor=price_significance_factor,
                today_price_spread_c=today_spread,
                seasonal_reference_spread_c=seasonal_reference_spread_c,
                prev_heating_hard_limit_engaged=bool(
                    self.data and self.data.heating_hard_limit_engaged
                ),
            ),
            self._params(),
        )

        # A real side effect when configured, but it never affects `result`.
        await self._async_push_output(result)

        if self.data_logging_enabled:
            await self._log_data_point(result)

        return result

    # --- Output push ---------------------------------------------------------

    async def _async_push_output(self, result: HeuristicResult) -> None:
        """Push the published value to whichever target the selected output
        mode uses.

        The two outdoor-spoofing channels are independent, not alternatives —
        set one, both or neither. The heat-curve-offset and indoor-climate
        channels are each a genuine alternative to outdoor spoofing and to
        one another (one writes a delta to a different pump parameter, the
        other a target temperature to a different entity type, neither a
        faked outdoor reading), so both are gated by `output_mode` rather
        than by whether their own entity is configured: leaving stale values
        in another section's fields must never cause a double push to the
        same pump.
        """
        if self.output_mode == OUTPUT_MODE_HEAT_CURVE_OFFSET:
            await self._async_push_heat_curve_offset(result)
            return
        if self.output_mode == OUTPUT_MODE_INDOOR_CLIMATE:
            await self._async_push_indoor_climate(result)
            return

        host = self.ohmonwifi_host
        entity_id = self.output_number_entity_id
        if not host and not entity_id:
            return
        value = (
            result.raw_outdoor_temp_c
            if not self.is_active
            else result.compensated_outdoor_temp_c
        )
        if host:
            await self._async_push_ohmonwifi(host, value)
        if entity_id:
            await self._async_push_number_entity(entity_id, value)

    def _output_stale(self, pushed_at: datetime | None) -> bool:
        """Whether a channel's last successful write is old enough that it
        should be reasserted even though the computed value looks unchanged
        from our local cache — see OUTPUT_REASSERT_AFTER_CYCLES."""
        if pushed_at is None:
            return True
        return dt_util.utcnow() - pushed_at >= timedelta(
            minutes=UPDATE_INTERVAL_MINUTES * OUTPUT_REASSERT_AFTER_CYCLES
        )

    def _target_number_entity_value(self, entity_id: str) -> float | None:
        """Read a `number`/`input_number` target's own current state, so a
        push can be compared against reality instead of a local cache that
        cannot see a reset made by something else. Returns None when the
        entity is missing, unavailable, or not numeric, so the caller can
        fall back to the cache."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _clamp_to_entity_range(
        self,
        entity_id: str,
        value: float,
        min_attr: str,
        max_attr: str,
        fallback_min: float,
        fallback_max: float,
    ) -> float:
        """Clamp `value` to `entity_id`'s own `min_attr`/`max_attr` state
        attributes, so a legitimately-computed value is never rejected
        outright by the target's own service-call validation (`number` and
        `climate` both raise rather than truncate). Falls back to
        `fallback_min`/`fallback_max` when the entity or its bounds cannot be
        read — a clamped-but-useful value beats a swallowed exception and a
        pump left uncompensated indefinitely."""
        minimum, maximum = fallback_min, fallback_max
        state = self.hass.states.get(entity_id)
        if state is not None:
            raw_min = state.attributes.get(min_attr)
            raw_max = state.attributes.get(max_attr)
            if isinstance(raw_min, (int, float)) and not isinstance(raw_min, bool):
                minimum = raw_min
            if isinstance(raw_max, (int, float)) and not isinstance(raw_max, bool):
                maximum = raw_max
        return max(minimum, min(maximum, value))

    async def _async_push_heat_curve_offset(self, result: HeuristicResult) -> None:
        """Push a delta to the pump's own heat-curve-offset parameter instead
        of a faked outdoor reading. Zero while compensation is off — the same
        "preview without touching the pump" semantics the outdoor-spoof path
        gets by publishing the unmodified raw temperature.
        """
        entity_id = self.heat_curve_offset_entity_id
        if not entity_id:
            return
        if not self.is_active:
            value = 0
        elif result.heating_hard_limit_engaged:
            # Fixed rather than derived from `compensated - raw`: raw keeps
            # drifting with ordinary weather noise while the hard limit
            # holds, and `compensated_outdoor_temp_c` is pinned to a fixed
            # ceiling during it — see `heating_hard_limit_offset_c`'s
            # docstring for why that combination flaps.
            value = heating_hard_limit_offset_c(self.heat_curve_offset_invert)
        else:
            value = heat_curve_offset_c(
                result.compensated_outdoor_temp_c,
                result.raw_outdoor_temp_c,
                self.heat_curve_offset_invert,
            )
        # A real pump dial is a small integer range (NIBE's is -10..+10) that
        # the computed delta can legitimately exceed - see heat_curve_offset_c's
        # docstring - and number.set_value raises rather than truncating out
        # of range, so clamp to the entity's own bounds before anything else
        # touches `value` (the dedupe/reassert cache below included, so a
        # value this can never actually deliver is never chased forever).
        value = round(
            self._clamp_to_entity_range(
                entity_id,
                value,
                "min",
                "max",
                HEAT_CURVE_OFFSET_FALLBACK_MIN_C,
                HEAT_CURVE_OFFSET_FALLBACK_MAX_C,
            )
        )
        # Prefer the entity's own current state over the local cache — cheaper
        # than a timer and exactly correct, since it catches a reset by
        # anything else immediately instead of waiting out the reassert
        # window. Fall back to the cache (with the timer backstop) only when
        # the entity's state cannot be read.
        reference = self._target_number_entity_value(entity_id)
        stale = self._output_stale(self._last_heat_curve_offset_pushed_at)
        if reference is None:
            reference = self._last_heat_curve_offset_value_c
        else:
            stale = False
        if (
            reference is not None
            and abs(value - reference) < OUTPUT_WRITE_TOLERANCE_C
            and not stale
        ):
            return
        try:
            await self.hass.services.async_call(
                # Accepts a "number" or "input_number" entity (see the
                # `heat_curve_offset_entity` selector in config_flow.py) —
                # both expose an identical `set_value(entity_id, value)`
                # service, keyed by the entity's own domain.
                entity_id.split(".", 1)[0],
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
            self._last_heat_curve_offset_value_c = value
            self._last_heat_curve_offset_pushed_at = dt_util.utcnow()
        except Exception as err:  # noqa: BLE001 - best-effort, never break output
            _LOGGER.warning("Could not push to %s (ignored): %s", entity_id, err)

    async def _async_push_indoor_climate(self, result: HeuristicResult) -> None:
        """Push a target temperature to a room-sensor climate entity instead
        of a faked outdoor reading or a native curve-offset dial, for pumps
        (or TRVs) that expose one. Zero delta while compensation is off — the
        same "preview without touching the pump" semantics the other two
        output modes get, so the entity simply sits at the occupant's own
        target.
        """
        entity_id = self.indoor_climate_entity_id
        if not entity_id:
            return
        if not self.is_active:
            extra_c = 0.0
        elif result.heating_hard_limit_engaged:
            # Same reason `_async_push_heat_curve_offset` special-cases this:
            # raw keeps drifting with ordinary weather noise while the hard
            # limit holds and `compensated_outdoor_temp_c` is pinned to a
            # fixed ceiling during it, so deriving live from raw would flap.
            extra_c = heating_hard_limit_offset_c(invert=False)
        else:
            extra_c = indoor_climate_offset_c(
                result.compensated_outdoor_temp_c,
                result.raw_outdoor_temp_c,
                SPOOF_PER_INDOOR_C,
            )
        value = result.effective_indoor_target_c + extra_c
        # The hard-limit path in particular can send effective_target - 5°C,
        # which can fall below a TRV's floor; clamp to the entity's own
        # min_temp/max_temp before the dedupe check for the same reason
        # _async_push_heat_curve_offset does.
        value = self._clamp_to_entity_range(
            entity_id,
            value,
            "min_temp",
            "max_temp",
            INDOOR_CLIMATE_FALLBACK_MIN_C,
            INDOOR_CLIMATE_FALLBACK_MAX_C,
        )
        if (
            self._last_indoor_climate_value_c is not None
            and abs(value - self._last_indoor_climate_value_c)
            < OUTPUT_WRITE_TOLERANCE_C
            and not self._output_stale(self._last_indoor_climate_pushed_at)
        ):
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, ATTR_TEMPERATURE: value},
                blocking=True,
            )
            self._last_indoor_climate_value_c = value
            self._last_indoor_climate_pushed_at = dt_util.utcnow()
        except Exception as err:  # noqa: BLE001 - best-effort, never break output
            _LOGGER.warning("Could not push to %s (ignored): %s", entity_id, err)

    async def _async_push_ohmonwifi(self, host: str, value: float) -> None:
        """GET `http://<host>/AT/?T=<value>` — OhmOnWifi's own local API, which
        sets its emulated resistance output so the connected heat pump reads
        `value` as the outdoor temperature. Plain HTTP, no authentication, per
        the vendor's published API doc. Best-effort: any failure is logged and
        swallowed."""
        if (
            self._last_ohmonwifi_value_c is not None
            and abs(value - self._last_ohmonwifi_value_c) < OUTPUT_WRITE_TOLERANCE_C
            and not self._output_stale(self._last_ohmonwifi_pushed_at)
        ):
            return
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"http://{host}/AT/",
                params={"T": f"{value:.1f}"},
                timeout=aiohttp.ClientTimeout(total=OHMONWIFI_REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
            self._last_ohmonwifi_value_c = value
            self._last_ohmonwifi_pushed_at = dt_util.utcnow()
        except Exception as err:  # noqa: BLE001 - best-effort, never break output
            _LOGGER.warning(
                "Could not push to OhmOnWifi device %s (ignored): %s", host, err
            )

    async def _async_ohmonwifi_relay_failsafe(self, sensor_ok: bool) -> None:
        """Keep the OhmOnWifi device off a stale override when its source goes
        bad, and hand control back once it's ours to give.

        `_async_push_ohmonwifi` only fires from a successful cycle, so once
        the outdoor sensor it spoofs from goes unavailable, `_async_update_data`
        raises before ever reaching that push — the device is left holding
        whatever AT value it last received, indefinitely, with no further
        write to even notice the source is down. `/info`'s `relay` field is
        "ON" while the device outputs our override and "OFF" while its relay
        bypasses to the real external sensor; this flips it to "OFF" the
        moment the source sensor goes bad, and back once it recovers.

        Only used while CONF_OUTPUT_MODE is OUTPUT_MODE_OUTDOOR_SPOOF — the
        heat-curve-offset mode never engages the device's relay in the first
        place, see `CONF_OHMONWIFI_HOST` in const.py.

        Guarded by `_ohmonwifi_forced_bypass` so recovery only re-engages a
        bypass this integration itself caused, never one set by hand from the
        device's own web UI.
        """
        host = self.ohmonwifi_host
        if not host or self.output_mode != OUTPUT_MODE_OUTDOOR_SPOOF:
            return
        try:
            if not sensor_ok:
                if await self._async_ohmonwifi_relay_state(host) == "ON":
                    await self._async_ohmonwifi_toggle(host)
                    self._ohmonwifi_forced_bypass = True
                    _LOGGER.warning(
                        "Outdoor sensor unavailable; switched OhmOnWifi device "
                        "%s to bypass so the heat pump reads its real external "
                        "sensor instead of a stale override",
                        host,
                    )
            elif self._ohmonwifi_forced_bypass:
                if await self._async_ohmonwifi_relay_state(host) == "OFF":
                    await self._async_ohmonwifi_toggle(host)
                    _LOGGER.info(
                        "Outdoor sensor recovered; switched OhmOnWifi device %s "
                        "back to override",
                        host,
                    )
                self._ohmonwifi_forced_bypass = False
        except Exception as err:  # noqa: BLE001 - best-effort, never break output
            _LOGGER.warning(
                "Could not update OhmOnWifi device %s relay state (ignored): %s",
                host,
                err,
            )

    async def _async_ohmonwifi_relay_state(self, host: str) -> str | None:
        """GET `/info`'s `relay` field: "ON" (override active) or "OFF"
        (bypassed to the real external sensor)."""
        session = async_get_clientsession(self.hass)
        async with session.get(
            f"http://{host}/info",
            timeout=aiohttp.ClientTimeout(total=OHMONWIFI_REQUEST_TIMEOUT_SECONDS),
        ) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        return data.get("relay")

    async def _async_ohmonwifi_toggle(self, host: str) -> None:
        """GET `/toggle` — OhmOnWifi's own local API to flip its relay between
        override and bypass."""
        session = async_get_clientsession(self.hass)
        async with session.get(
            f"http://{host}/toggle",
            timeout=aiohttp.ClientTimeout(total=OHMONWIFI_REQUEST_TIMEOUT_SECONDS),
        ) as response:
            response.raise_for_status()

    async def _async_push_number_entity(self, entity_id: str, value: float) -> None:
        """Push via a HA `number.set_value` call. Best-effort: any failure
        (entity gone, wrong domain) is logged and swallowed."""
        # Clamp to the entity's own bounds first - see
        # _async_push_heat_curve_offset for why. Unlike that dial, this
        # channel carries a genuine outdoor temperature (potentially deep
        # negative in a cold climate), so the fallback when the entity's own
        # bounds cannot be read is the same sanity range compute() itself
        # clamps to, not a small dial range.
        value = self._clamp_to_entity_range(
            entity_id, value, "min", "max", OUTPUT_SANITY_MIN_C, OUTPUT_SANITY_MAX_C
        )
        # See _async_push_heat_curve_offset for why the entity's own state is
        # preferred over the local cache when it can be read.
        reference = self._target_number_entity_value(entity_id)
        stale = self._output_stale(self._last_output_number_pushed_at)
        if reference is None:
            reference = self._last_output_number_value_c
        else:
            stale = False
        if (
            reference is not None
            and abs(value - reference) < OUTPUT_WRITE_TOLERANCE_C
            and not stale
        ):
            return
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
            self._last_output_number_value_c = value
            self._last_output_number_pushed_at = dt_util.utcnow()
        except Exception as err:  # noqa: BLE001 - best-effort, never break output
            _LOGGER.warning("Could not push to %s (ignored): %s", entity_id, err)

    # --- Opt-in local history logging ---------------------------------------

    def _build_log_record(self, result: HeuristicResult) -> dict:
        """Flatten this cycle into one record for the local history log.

        Raw physical inputs first (what a replay needs to re-drive a candidate
        controller), then what this one actually did. The learner's internal
        state is included because it is not reconstructible from config: the
        offset moves every cycle, and without it an offline replay cannot tell
        what the controller was doing or why.

        `learner_stepped` is the field a replay must filter on. A record is
        written on every coordinator cycle, including the extra refreshes a
        recovering source triggers, but the learner only advances on its fixed
        clock — so consecutive records inside one interval repeat identical
        `learner_*` values while the raw inputs differ. Treating every line as
        a decision is exactly the irregular-cadence mistake that made the
        previous model's data unusable, so the distinction is recorded rather
        than left to be inferred from timestamps.
        """
        learner = self.learner_result
        lag = self.lag_result
        record: dict = {
            "ts": dt_util.utcnow().isoformat(),
            "entry_id": self.entry.entry_id,
            "learner_stepped": self._learner_stepped,
            # --- raw inputs ---
            "indoor_temp_c": result.indoor_temp_c,
            "indoor_ok": result.indoor_data_available,
            "raw_outdoor_temp_c": result.raw_outdoor_temp_c,
            "wind_speed_ms": result.wind_speed_ms,
            "wind_ok": result.wind_data_available,
            "cloud_coverage_pct": result.cloud_coverage_pct,
            "cloud_ok": result.cloud_data_available,
            "solar_effect": result.solar_effect,
            "current_price": result.current_price,
            "price_ok": result.price_data_available,
            # --- live control surface ---
            "is_active": self.is_active,
            "output_mode": self.output_mode,
            "indoor_target_c": result.indoor_target_c,
            "effective_target_c": result.effective_indoor_target_c,
            "user_indoor_target_c": result.user_indoor_target_c,
            "price_comfort_tier": result.price_comfort_tier,
            "cold_caution": result.cold_caution,
            "vacation_armed": self.vacation_armed,
            "vacation_phase": self.vacation_result.phase if self.vacation_result else None,
            "active_plan_id": self.active_plan_id,
            # --- composed output ---
            "compensated_outdoor_temp_c": result.compensated_outdoor_temp_c,
            "learned_offset_c": result.learned_offset_c,
            "wind_adjustment_c": result.wind_adjustment_c,
            "sun_adjustment_c": result.sun_adjustment_c,
            "price_adjustment_c": result.price_adjustment_c,
            "price_response": result.price_response,
            "cold_brake_factor": result.cold_brake_factor,
            "price_significance_factor": result.price_significance_factor,
            "today_price_spread_c": result.today_price_spread_c,
            "seasonal_reference_spread_c": result.seasonal_reference_spread_c,
            "price_braking": result.price_braking,
            "precharge_active": result.precharge_active,
            "outdoor_preramp_c": result.outdoor_preramp_c,
            "wind_preramp_c": result.wind_preramp_c,
            "weather_preramp_c": result.weather_preramp_c,
            "weather_preramp_active": result.weather_preramp_active,
            "sun_precool_c": result.sun_precool_c,
            "sun_precool_active": result.sun_precool_active,
            "heating_hard_limit_engaged": result.heating_hard_limit_engaged,
            "lead_minutes_effective": result.lead_minutes_effective,
        }
        if learner is not None:
            record.update(
                {
                    "learner_bin": learner.bin_index,
                    "learner_hold_offset_c": learner.hold_offset_c,
                    "learner_proportional_c": learner.proportional_c,
                    "learner_authority_c": learner.authority_c,
                    "learner_ceiling_c": learner.ceiling_c,
                    "learner_close_rate_c_per_h": learner.close_rate_c_per_h,
                    "learner_tau_i_h": learner.tau_i_h,
                    "learner_error_c": learner.error_c,
                    "learner_saturated": learner.saturated,
                    "learner_frozen": learner.frozen,
                    "learner_total_samples": learner.total_samples,
                    "learner_coverage": learner.coverage,
                    # Open-loop baseline (see learner.py's "Two regimes"
                    # docstring): what the raw curve does with compensation
                    # off, recorded per outdoor band so a replay can reconstruct
                    # the baseline table's evolution the same way it does the
                    # offset table's, from `learner_bin` plus these. None until
                    # the current band has been observed at all.
                    "learner_baseline_indoor_c": learner.baseline_indoor_c,
                    "learner_baseline_deficit_c": learner.baseline_deficit_c,
                    "learner_baseline_samples": learner.baseline_samples,
                    "learner_baseline_coverage": learner.baseline_coverage,
                    "learner_seeded": learner.seeded,
                }
            )
            # Low-cardinality: only present on the cycle a bin was actually
            # seeded (the off->on transition), not on every row.
            if learner.seeded_bins:
                record["learner_seeded_bins"] = learner.seeded_bins
            # Same reasoning as `learner_freeze_reason` below: the reason string
            # is only informative while compensation is off (see
            # `_observe_baseline`), so it would otherwise dominate the file with
            # a value already implied by `is_active`.
            if learner.baseline_reason:
                record["learner_baseline_reason"] = learner.baseline_reason
            # Low-cardinality diagnostic: logged only when learning is actually
            # blocked, since the full reason every cycle would dominate the file
            # for something reconstructible from the numbers.
            if learner.freeze_reason:
                record["learner_freeze_reason"] = learner.freeze_reason
        if lag is not None:
            record.update(
                {
                    "lag_rise_minutes": lag.rise_minutes,
                    "lag_fall_minutes": lag.fall_minutes,
                    "lag_rise_measured": lag.rise_measured,
                    "lag_fall_measured": lag.fall_measured,
                }
            )
        if self._last_price_forecast is not None:
            record["price_forecast"] = [
                [round(h, 3), p] for h, p in self._last_price_forecast
            ]
        # Forecasts get revised, so realised weather is not a substitute for
        # what was known at decision time. Logged only while the pre-ramp is
        # actually acting, though — the same low-cardinality rule
        # `learner_freeze_reason` follows, since the full series on every row
        # roughly doubles the record for something that only matters on the
        # rows where it drove a decision.
        if result.weather_preramp_active and self._last_outdoor_forecast is not None:
            record["outdoor_forecast"] = [
                [round(h, 3), round(t, 2)] for h, t in self._last_outdoor_forecast
            ]
        if result.sun_precool_active and self._last_solar_forecast is not None:
            record["solar_forecast"] = [
                [round(h, 3), round(v, 3)] for h, v in self._last_solar_forecast
            ]
        return _sanitize_non_finite(record)

    async def _log_data_point(self, result: HeuristicResult) -> None:
        try:
            await async_log_record(
                self.hass, self.entry.entry_id, self._build_log_record(result)
            )
        except Exception as err:  # noqa: BLE001 - logging never breaks output
            _LOGGER.warning("Could not log data point (ignored): %s", err)
