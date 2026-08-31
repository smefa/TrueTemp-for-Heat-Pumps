"""Config flow for TrueTemp.

Setup asks four required questions and a name, plus two optional entities
(weather forecast, electricity price) also editable later. Options add four
small pages: the building's own sensors and logging, optional sources, one
comfort preference, and plumbing — the output page branches into a second,
mode-specific page once a method is chosen.

The building-facing fields (indoor/outdoor sensor, heating type) are asked at
setup *and* reachable again from the options "Settings" page, deliberately
duplicating what `async_step_reconfigure` also offers — most occupants only
ever find the gear-icon Configure button, not the entry's separate Reconfigure
action, so the fields that matter most need to live where they'll look.

Nothing here is a control gain. `k_indoor`, `k_wind`, `k_sun`, `k_price`, three
cold-taper thresholds, two price thresholds, a pre-charge boost, three MPC
settings, an update interval, an upper comfort bound and a tuning mode have all
been removed — every one of them is now either measured from the house
(`learner.py`, `lag.py`) or fixed at a value with a physical justification.

What is left divides cleanly: an entity to read, an entity to write, or a
genuine occupant preference. The dividing line is the one this project has
always stated — config describes the occupant, never the building.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, time
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    CONFIG_ENTRY_VERSION,
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_DATA_LOGGING,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_ENABLE_SOLAR_INPUT,
    CONF_ENABLE_WIND_INPUT,
    CONF_ENABLE_WEATHER_LOOKAHEAD,
    CONF_HEAT_CURVE_OFFSET_ENTITY,
    CONF_HEAT_CURVE_OFFSET_INVERT,
    CONF_HEATING_TYPE,
    CONF_INDOOR_AGGREGATION,
    CONF_INDOOR_CLIMATE_ENTITY,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_INDOOR_TEMP_SENSORS_EXTRA,
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
    DEFAULT_INDOOR_AGGREGATION,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_OUTPUT_MODE,
    DEFAULT_PRICE_SIGNIFICANCE_FLOOR,
    DOMAIN,
    OUTPUT_MODE_HEAT_CURVE_OFFSET,
    OUTPUT_MODE_INDOOR_CLIMATE,
    OUTPUT_MODES,
)
from .holiday import HOLIDAY_TARGET_MIN_C
from .indoor_aggregation import MODE_AVERAGE, MODE_LOWEST
from .lag import DEFAULT_HEATING_TYPE, HEATING_TYPES
from .vacation import (
    RECURRENCE_ONCE,
    RECURRENCE_WEEKLY,
    RECURRENCE_YEARLY,
    VacationPlan,
    deserialize_plans,
    find_overlap,
    serialize_plans,
)

# UI-only grouping for the two outdoor-spoofing targets on the "output_spoof"
# page, so each gets its own header. The entry's stored options stay flat, so
# coordinator.py never learns that sections exist (see `async_step_output_spoof`,
# which re-flattens on submit).
SECTION_OUTPUT_NUMBER = "output_number_push"
SECTION_OHMONWIFI = "ohmonwifi_direct"

# Timeout for validating an OhmOnWifi host at options-save time. This happens
# once, interactively, while the user is watching the form, so it can be a
# little more generous than the per-cycle push timeout.
OHMONWIFI_VALIDATE_TIMEOUT_SECONDS = 5.0

# Bare hostname/IPv4, optionally with a port — no scheme, path, query or
# userinfo. The value is interpolated straight into a URL in coordinator.py
# (`f"http://{host}/AT/"` and friends), so anything else would quietly change
# the request target rather than just failing to resolve.
_OHMONWIFI_HOST_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"(:[0-9]{1,5})?$"
)


# 5 sensors total (primary + extras) — see indoor_aggregation.py's module
# docstring and docs/plan_multi_indoor_sensor.md §2.1 for why this is a
# judgement call, not a technical limit.
MAX_INDOOR_EXTRA_SENSORS = 4


def _valid_ohmonwifi_host(host: str) -> bool:
    return bool(_OHMONWIFI_HOST_RE.match(host))


def _indoor_extras_error(
    hass: HomeAssistant,
    primary: str,
    extras: list[str],
    exclude_entry_id: str | None,
) -> str | None:
    """Validate the extra indoor sensors against the primary and every other
    zone's own primary. Returns a translation key for `errors["base"]`, or
    `None` if `extras` is valid."""
    if primary in extras:
        return "indoor_extra_duplicates_primary"
    if len(extras) > MAX_INDOOR_EXTRA_SENSORS:
        return "indoor_extra_too_many"
    # Same "two zones may not share an indoor sensor" rule the primary is
    # already held to (see async_step_settings below) — an extra must not be
    # another zone's identity either.
    other_primaries = {
        entry.unique_id
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != exclude_entry_id
    }
    if not other_primaries.isdisjoint(extras):
        return "indoor_extra_is_other_primary"
    return None


async def _async_ohmonwifi_reachable(hass: HomeAssistant, host: str) -> bool:
    """Best-effort reachability check against the device's own `/info`
    endpoint, so a typo'd address fails where the user can see it rather than
    silently doing nothing every cycle."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"http://{host}/info",
            timeout=aiohttp.ClientTimeout(total=OHMONWIFI_VALIDATE_TIMEOUT_SECONDS),
        ) as response:
            response.raise_for_status()
    except Exception:  # noqa: BLE001 - any failure just means "not reachable"
        return False
    return True


def _user_data_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """The whole of setup: two sensors, a temperature, and what the emitters are."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_NAME, default=defaults.get(CONF_NAME, "TrueTemp")
            ): str,
            vol.Required(
                CONF_INDOOR_TEMP_SENSOR, default=defaults.get(CONF_INDOOR_TEMP_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Optional, 0-4 more sensors averaged (or min()'d) together with
            # the primary above — see docs/plan_multi_indoor_sensor.md. Absent
            # or empty is exactly today's single-sensor behaviour.
            vol.Optional(
                CONF_INDOOR_TEMP_SENSORS_EXTRA,
                default=defaults.get(CONF_INDOOR_TEMP_SENSORS_EXTRA, []),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)
            ),
            vol.Required(
                CONF_INDOOR_AGGREGATION,
                default=defaults.get(CONF_INDOOR_AGGREGATION, DEFAULT_INDOOR_AGGREGATION),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[MODE_AVERAGE, MODE_LOWEST],
                    translation_key="indoor_aggregation",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_OUTDOOR_TEMP_SENSOR, default=defaults.get(CONF_OUTDOOR_TEMP_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Optional, and can also be set later from the options' "sources"
            # page — shown here too because most installs have one or both
            # ready at setup time and would otherwise have to find the
            # options flow just to fill them in.
            #
            # `vol.Any(None, ...)` because HA's frontend submits an explicit
            # null for an untouched/cleared optional entity picker, and a bare
            # EntitySelector only accepts a string.
            vol.Optional(
                CONF_WEATHER_ENTITY, default=defaults.get(CONF_WEATHER_ENTITY)
            ): vol.Any(
                None,
                selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            ),
            vol.Optional(
                CONF_NORDPOOL_PRICE_ENTITY,
                default=defaults.get(CONF_NORDPOOL_PRICE_ENTITY),
            ): vol.Any(
                None,
                selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            ),
            vol.Required(
                CONF_INDOOR_TARGET_TEMPERATURE,
                default=defaults.get(
                    CONF_INDOOR_TARGET_TEMPERATURE, DEFAULT_INDOOR_TARGET_TEMPERATURE
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10, max=30, step=0.5, unit_of_measurement="°C", mode="box"
                )
            ),
            # Asked because it is the fallback for how long this house takes to
            # respond, used until `lag.py` has measured the real figures from a
            # few days of operation. After that it stops mattering — which is
            # why it is the only question about the building that survives.
            vol.Required(
                CONF_HEATING_TYPE,
                default=defaults.get(CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(HEATING_TYPES),
                    translation_key="heating_type",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class TrueTempConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle initial setup of a TrueTemp zone."""

    VERSION = CONFIG_ENTRY_VERSION

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {}
        if user_input is not None:
            defaults = user_input
            error = _indoor_extras_error(
                self.hass,
                user_input[CONF_INDOOR_TEMP_SENSOR],
                user_input.get(CONF_INDOOR_TEMP_SENSORS_EXTRA) or [],
                exclude_entry_id=None,
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_input[CONF_INDOOR_TEMP_SENSOR])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=_user_data_schema(defaults), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        defaults = {**entry.data, **entry.options}
        if user_input is not None:
            defaults = {**defaults, **user_input}
            error = _indoor_extras_error(
                self.hass,
                user_input[CONF_INDOOR_TEMP_SENSOR],
                user_input.get(CONF_INDOOR_TEMP_SENSORS_EXTRA) or [],
                exclude_entry_id=entry.entry_id,
            )
            if error:
                errors["base"] = error
            else:
                if user_input[CONF_INDOOR_TEMP_SENSOR] != entry.unique_id:
                    await self.async_set_unique_id(user_input[CONF_INDOOR_TEMP_SENSOR])
                    self._abort_if_unique_id_configured()
                return self.async_update_reload_and_abort(
                    entry, title=user_input[CONF_NAME], data=user_input
                )
        return self.async_show_form(
            step_id="reconfigure",
            errors=errors,
            # Merged with options: `weather_entity`/`nordpool_price_entity`
            # may have been set or cleared later from the options "sources"
            # page, which is where the current value actually lives once
            # that's happened (options override data — see `_entry_value` in
            # coordinator.py).
            data_schema=_user_data_schema(defaults),
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TrueTempOptionsFlow:
        return TrueTempOptionsFlow()


class TrueTempOptionsFlow(config_entries.OptionsFlow):
    """Three focused pages, each saving independently.

    Each page's submit handler merges its own keys over the currently stored
    options rather than writing only its own fields, so pages can be edited in
    any order without one page's stale defaults clobbering another's freshly
    saved values.
    """

    # Set by `async_step_output` before it hands off to whichever mode-specific
    # page follows (`async_step_output_spoof`, `async_step_output_curve` or
    # `async_step_output_indoor_climate`), which merge it back in on save. Not
    # persisted mid-flow anywhere else.
    _output_common: dict[str, Any]

    # Set by `async_step_vacation_add`/`async_step_vacation_edit_fields` before
    # handing off to whichever recurrence-specific page follows, mirroring
    # `_output_common` above. `_vacation_editing_id` is `None` while adding a
    # new plan, or the id of the plan being replaced while editing one — see
    # `_save_vacation_plan`.
    _vacation_common: dict[str, Any]
    _vacation_editing_id: str | None = None

    def _current(self) -> dict[str, Any]:
        """Stored options layered over setup data — what each page shows."""
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self, updates: dict[str, Any]) -> config_entries.ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **updates})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "sources", "price", "output", "vacation"],
        )

    # --- Page: the building's own sensors and logging -----------------------

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._current()
        errors: dict[str, str] = {}

        if user_input is not None:
            new_indoor = user_input[CONF_INDOOR_TEMP_SENSOR]
            if new_indoor != self.config_entry.unique_id:
                # OptionsFlow has no async_set_unique_id/_abort_if_unique_id_configured
                # (those belong to ConfigFlow, used by async_step_reconfigure above) —
                # so the same "don't let two zones share an indoor sensor" check is
                # done by hand here.
                duplicate = any(
                    other.entry_id != self.config_entry.entry_id
                    and other.unique_id == new_indoor
                    for other in self.hass.config_entries.async_entries(DOMAIN)
                )
                if duplicate:
                    errors["base"] = "already_configured"
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, unique_id=new_indoor
                    )

            if not errors:
                error = _indoor_extras_error(
                    self.hass,
                    new_indoor,
                    user_input.get(CONF_INDOOR_TEMP_SENSORS_EXTRA) or [],
                    exclude_entry_id=self.config_entry.entry_id,
                )
                if error:
                    errors["base"] = error

            if not errors:
                return self._save(user_input)
            current = {**current, **user_input}

        return self.async_show_form(
            step_id="settings",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INDOOR_TEMP_SENSOR,
                        default=current.get(CONF_INDOOR_TEMP_SENSOR),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    # Optional, 0-4 more sensors combined with the primary
                    # above — see docs/plan_multi_indoor_sensor.md. Kept next
                    # to the primary sensor since both define the same zone's
                    # identity, rather than on the "sources" page with the
                    # unrelated sun/wind/price extras.
                    vol.Optional(
                        CONF_INDOOR_TEMP_SENSORS_EXTRA,
                        default=current.get(CONF_INDOOR_TEMP_SENSORS_EXTRA, []),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor", multiple=True)
                    ),
                    vol.Required(
                        CONF_INDOOR_AGGREGATION,
                        default=current.get(
                            CONF_INDOOR_AGGREGATION, DEFAULT_INDOOR_AGGREGATION
                        ),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[MODE_AVERAGE, MODE_LOWEST],
                            translation_key="indoor_aggregation",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_OUTDOOR_TEMP_SENSOR,
                        default=current.get(CONF_OUTDOOR_TEMP_SENSOR),
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Required(
                        CONF_HEATING_TYPE,
                        default=current.get(CONF_HEATING_TYPE, DEFAULT_HEATING_TYPE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(HEATING_TYPES),
                            translation_key="heating_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    # Not an output method itself, but grouped with the other
                    # building-level toggles rather than the "Outgoing sensors"
                    # page, which is about wiring the value out, not this zone.
                    vol.Required(
                        CONF_ENABLE_DATA_LOGGING,
                        default=current.get(
                            CONF_ENABLE_DATA_LOGGING, DEFAULT_ENABLE_DATA_LOGGING
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    # --- Page: optional sources ---------------------------------------------

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._current()

        if user_input is not None:
            return self._save(user_input)

        return self.async_show_form(
            step_id="sources",
            data_schema=vol.Schema(
                {
                    # Only the wind and solar terms consume this, and both can
                    # be switched off below — so with both off it is not needed
                    # at all and the per-cycle forecast calls are skipped.
                    #
                    # `vol.Any(None, ...)` because HA's frontend submits an
                    # explicit null for an untouched/cleared optional entity
                    # picker, and a bare EntitySelector only accepts a string.
                    vol.Optional(
                        CONF_WEATHER_ENTITY, default=current.get(CONF_WEATHER_ENTITY)
                    ): vol.Any(
                        None,
                        selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="weather")
                        ),
                    ),
                    vol.Optional(
                        CONF_NORDPOOL_PRICE_ENTITY,
                        default=current.get(CONF_NORDPOOL_PRICE_ENTITY),
                    ): vol.Any(
                        None,
                        selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="sensor")
                        ),
                    ),
                    vol.Required(
                        CONF_ENABLE_SOLAR_INPUT,
                        default=current.get(
                            CONF_ENABLE_SOLAR_INPUT, DEFAULT_ENABLE_SOLAR_INPUT
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_ENABLE_WIND_INPUT,
                        default=current.get(
                            CONF_ENABLE_WIND_INPUT, DEFAULT_ENABLE_WIND_INPUT
                        ),
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_ENABLE_WEATHER_LOOKAHEAD,
                        default=current.get(
                            CONF_ENABLE_WEATHER_LOOKAHEAD,
                            DEFAULT_ENABLE_WEATHER_LOOKAHEAD,
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    # --- Page: price ---------------------------------------------------------

    def _price_unit(self) -> str | None:
        """Unit label for the price-significance-floor field below, read from
        the configured price entity's OWN `unit_of_measurement` attribute.

        Deliberately NOT `currency`: on the Nordpool integration this project
        targets, `currency` reports the base ISO currency code ("SEK") even
        when the entity's `price_in_cents` option is on and the values it
        actually publishes are in öre — using it here would silently be off by
        100x on exactly the installs most likely to type a small number into
        this field. `unit_of_measurement` gives the right answer either way
        ("SEK/kWh" or "öre/kWh"). Omitted (None, which the selector shows as
        no unit at all) rather than guessed when the entity or the attribute
        is not available — a wrong label is worse than none.
        """
        entity_id = self._current().get(CONF_NORDPOOL_PRICE_ENTITY)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        return unit if isinstance(unit, str) else None

    async def async_step_price(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self._save(user_input)

        # Three fields, because the aggressiveness tier and the cold-caution
        # setting are live entities rather than options — they are adjusted
        # often enough that routing them through a config option (which reloads
        # the entry) would be the wrong home.
        current = self._current()
        return self.async_show_form(
            step_id="price",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLE_PRICE_COMPENSATION,
                        default=current.get(
                            CONF_ENABLE_PRICE_COMPENSATION,
                            DEFAULT_ENABLE_PRICE_COMPENSATION,
                        ),
                    ): selector.BooleanSelector(),
                    # The only comfort bound in the integration, and it applies
                    # to price compensation alone: how cold the house may get
                    # while chasing a cheap hour. It genuinely binds — the High
                    # tier allows a 3 degC sag, so a 21 degC target reaches this
                    # default exactly.
                    vol.Required(
                        CONF_COMFORT_MIN_C,
                        default=current.get(CONF_COMFORT_MIN_C, DEFAULT_COMFORT_MIN_C),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5, max=25, step=0.5, unit_of_measurement="°C", mode="box"
                        )
                    ),
                    # The one absolute-money knob left: see
                    # CONF_PRICE_SIGNIFICANCE_FLOOR's docstring in const.py. 0
                    # (the default) means "auto" — a floor derived from this
                    # zone's own price history rather than a fixed number,
                    # since no single constant means the same thing across
                    # every currency and sub-unit Nordpool can report in. min
                    # deliberately allows exactly 0 for that reason; max/step
                    # are otherwise unit-agnostic rather than trying to rescale
                    # bounds by whatever unit `_price_unit` happens to read
                    # this cycle.
                    vol.Required(
                        CONF_PRICE_SIGNIFICANCE_FLOOR,
                        default=current.get(
                            CONF_PRICE_SIGNIFICANCE_FLOOR,
                            DEFAULT_PRICE_SIGNIFICANCE_FLOOR,
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=50,
                            step=0.01,
                            unit_of_measurement=self._price_unit(),
                            mode="box",
                        )
                    ),
                }
            ),
        )

    # --- Page: outgoing sensors ----------------------------------------------
    #
    # Split across steps rather than one page with all three target sections,
    # because HA's options flow has no way to hide a field conditionally
    # within a single page — the mode has to be chosen and submitted first so
    # the next page can be built with only the fields that mode reads. This
    # also means the mode-specific fields' text no longer needs to name the
    # mode ("Used when the output method above is...") since only the
    # relevant page is ever shown.

    async def async_step_output(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._current()

        if user_input is not None:
            self._output_common = dict(user_input)
            if self._output_common[CONF_OUTPUT_MODE] == OUTPUT_MODE_HEAT_CURVE_OFFSET:
                return await self.async_step_output_curve()
            if self._output_common[CONF_OUTPUT_MODE] == OUTPUT_MODE_INDOOR_CLIMATE:
                return await self.async_step_output_indoor_climate()
            return await self.async_step_output_spoof()

        return self.async_show_form(
            step_id="output",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OUTPUT_MODE,
                        default=current.get(CONF_OUTPUT_MODE, DEFAULT_OUTPUT_MODE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(OUTPUT_MODES),
                            translation_key="output_mode",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_output_spoof(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self._current()

        if user_input is not None:
            # Flatten the cosmetic sections straight back into plain top-level
            # keys, so the stored options — and coordinator.py, which knows
            # these only as flat keys — never learn sections exist.
            output_number_data = dict(user_input.pop(SECTION_OUTPUT_NUMBER, {}))
            ohmonwifi_data = dict(user_input.pop(SECTION_OHMONWIFI, {}))
            host = (ohmonwifi_data.get(CONF_OHMONWIFI_HOST) or "").strip()
            user_input[CONF_OUTPUT_NUMBER_ENTITY] = output_number_data.get(
                CONF_OUTPUT_NUMBER_ENTITY
            )
            user_input[CONF_OHMONWIFI_HOST] = host or None
            if host and not _valid_ohmonwifi_host(host):
                errors["base"] = "invalid_host"
                current = {**current, **user_input}
            elif host and not await _async_ohmonwifi_reachable(self.hass, host):
                errors["base"] = "cannot_connect"
                # Re-show with what was just submitted rather than the stored
                # options, so nothing else the user typed is lost.
                current = {**current, **user_input}
            else:
                return self._save({**self._output_common, **user_input})

        return self.async_show_form(
            step_id="output_spoof",
            errors=errors,
            data_schema=vol.Schema(
                {
                    # Independent, not alternatives — set one, both or neither.
                    vol.Required(SECTION_OUTPUT_NUMBER): section(
                        vol.Schema(
                            {
                                # `vol.Any(None, ...)` — see the OhmOnWifi host
                                # field below, same reason.
                                vol.Optional(
                                    CONF_OUTPUT_NUMBER_ENTITY,
                                    default=current.get(CONF_OUTPUT_NUMBER_ENTITY),
                                ): vol.Any(
                                    None,
                                    selector.EntitySelector(
                                        selector.EntitySelectorConfig(domain="number")
                                    ),
                                ),
                            }
                        ),
                        {"collapsed": False},
                    ),
                    vol.Required(SECTION_OHMONWIFI): section(
                        vol.Schema(
                            {
                                # `vol.Any(None, ...)` because HA's frontend
                                # submits an explicit null when a previously
                                # non-empty optional field is cleared, and a
                                # bare TextSelector only accepts str.
                                vol.Optional(
                                    CONF_OHMONWIFI_HOST,
                                    default=current.get(CONF_OHMONWIFI_HOST),
                                ): vol.Any(
                                    None,
                                    selector.TextSelector(
                                        selector.TextSelectorConfig(
                                            type=selector.TextSelectorType.TEXT
                                        )
                                    ),
                                ),
                            }
                        ),
                        {"collapsed": False},
                    ),
                }
            ),
        )

    async def async_step_output_curve(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._current()

        if user_input is not None:
            return self._save({**self._output_common, **user_input})

        return self.async_show_form(
            step_id="output_curve",
            data_schema=vol.Schema(
                {
                    # `vol.Any(None, ...)` — see the OhmOnWifi host field in
                    # `async_step_output_spoof`, same reason.
                    vol.Optional(
                        CONF_HEAT_CURVE_OFFSET_ENTITY,
                        default=current.get(CONF_HEAT_CURVE_OFFSET_ENTITY),
                    ): vol.Any(
                        None,
                        # Both domains expose the same `set_value` service, so
                        # either a "number" helper/platform entity or a legacy
                        # "input_number" helper works as a push target — see
                        # `_async_push_heat_curve_offset` in coordinator.py.
                        selector.EntitySelector(
                            selector.EntitySelectorConfig(
                                domain=["number", "input_number"]
                            )
                        ),
                    ),
                    # A hardware fact about the pump model, asked once — see
                    # CONF_HEAT_CURVE_OFFSET_INVERT's docstring in const.py for
                    # the convention it flips.
                    vol.Required(
                        CONF_HEAT_CURVE_OFFSET_INVERT,
                        default=current.get(
                            CONF_HEAT_CURVE_OFFSET_INVERT,
                            DEFAULT_HEAT_CURVE_OFFSET_INVERT,
                        ),
                    ): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_output_indoor_climate(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current = self._current()

        if user_input is not None:
            return self._save({**self._output_common, **user_input})

        return self.async_show_form(
            step_id="output_indoor_climate",
            data_schema=vol.Schema(
                {
                    # `vol.Any(None, ...)` — see the OhmOnWifi host field in
                    # `async_step_output_spoof`, same reason.
                    vol.Optional(
                        CONF_INDOOR_CLIMATE_ENTITY,
                        default=current.get(CONF_INDOOR_CLIMATE_ENTITY),
                    ): vol.Any(
                        None,
                        selector.EntitySelector(
                            selector.EntitySelectorConfig(domain="climate")
                        ),
                    ),
                }
            ),
        )

    # --- Pages: vacation plans -----------------------------------------------
    #
    # A hand-rolled list-of-records CRUD, not a single page: HA's options flow
    # has no native list editor, and config-entry subentries (the newer native
    # per-item config UI) are off the table for this integration's HA version
    # floor — see docs/plan_vacation_plans.md §7. Each action below is a short
    # sub-flow that saves and closes, same "no back button" model the other
    # options pages above already use.
    #
    # Selector option labels (weekdays, up/down) are supplied inline, not via
    # `translation_key` — HA has no translation mechanism for selector options
    # defined ad hoc in a flow schema (unlike the fixed `selector.*.options`
    # keys used elsewhere in this file).

    _WEEKDAY_LABELS = (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    )

    def _current_plans(self) -> list[VacationPlan]:
        return deserialize_plans(self.config_entry.options.get(CONF_VACATION_PLANS, []))

    def _editing_plan(self) -> VacationPlan | None:
        if self._vacation_editing_id is None:
            return None
        for plan in self._current_plans():
            if plan.id == self._vacation_editing_id:
                return plan
        return None

    def _save_vacation_plan(self, plan: VacationPlan) -> config_entries.ConfigFlowResult:
        plans = [p for p in self._current_plans() if p.id != plan.id]
        plans.append(plan)
        return self._save({CONF_VACATION_PLANS: serialize_plans(plans)})

    def _plan_overlaps_another(self, plan: VacationPlan) -> bool:
        """Whether saving `plan` (replacing any existing plan with the same
        id) would leave an enabled plan overlapping another — refused, not
        just warned, at save time (open question answer, §9)."""
        plans = [p for p in self._current_plans() if p.id != plan.id]
        plans.append(plan)
        return find_overlap(plans, dt_util.now().replace(tzinfo=None)) is not None

    def _weekday_options(self) -> list[selector.SelectOptionDict]:
        return [
            selector.SelectOptionDict(value=str(i), label=label)
            for i, label in enumerate(self._WEEKDAY_LABELS)
        ]

    async def async_step_vacation(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        plans = self._current_plans()
        menu_options = ["vacation_add"]
        if plans:
            menu_options += ["vacation_edit", "vacation_remove"]
        if len(plans) > 1:
            menu_options.append("vacation_reorder")
        return self.async_show_menu(step_id="vacation", menu_options=menu_options)

    # --- Common fields, shared by add and edit -------------------------------

    def _vacation_common_schema(self, defaults: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("name", default=defaults.get("name", "")): str,
                vol.Required(
                    "enabled", default=defaults.get("enabled", True)
                ): selector.BooleanSelector(),
                vol.Required(
                    "recurrence", default=defaults.get("recurrence", RECURRENCE_ONCE)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=RECURRENCE_ONCE, label="Once"),
                            selector.SelectOptionDict(value=RECURRENCE_WEEKLY, label="Weekly"),
                            selector.SelectOptionDict(value=RECURRENCE_YEARLY, label="Yearly"),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    "min_temp_c", default=defaults.get("min_temp_c", 15.0)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=HOLIDAY_TARGET_MIN_C,
                        max=30,
                        step=0.5,
                        unit_of_measurement="°C",
                        mode="box",
                    )
                ),
            }
        )

    async def async_step_vacation_add(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        self._vacation_editing_id = None
        if user_input is not None:
            self._vacation_common = user_input
            return await self._async_step_vacation_recurrence_fields()
        return self.async_show_form(
            step_id="vacation_add", data_schema=self._vacation_common_schema({})
        )

    async def async_step_vacation_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        plans = self._current_plans()
        if user_input is not None:
            self._vacation_editing_id = user_input["plan_id"]
            return await self.async_step_vacation_edit_fields()
        return self.async_show_form(
            step_id="vacation_edit",
            data_schema=vol.Schema(
                {
                    vol.Required("plan_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=plan.id, label=plan.name)
                                for plan in plans
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_vacation_edit_fields(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        existing = self._editing_plan()
        if user_input is not None:
            self._vacation_common = user_input
            return await self._async_step_vacation_recurrence_fields()
        defaults = (
            {
                "name": existing.name,
                "enabled": existing.enabled,
                "recurrence": existing.recurrence,
                "min_temp_c": existing.min_temp_c,
            }
            if existing is not None
            else {}
        )
        return self.async_show_form(
            step_id="vacation_edit_fields", data_schema=self._vacation_common_schema(defaults)
        )

    async def _async_step_vacation_recurrence_fields(
        self,
    ) -> config_entries.ConfigFlowResult:
        recurrence = self._vacation_common["recurrence"]
        if recurrence == RECURRENCE_WEEKLY:
            return await self.async_step_vacation_weekly()
        if recurrence == RECURRENCE_YEARLY:
            return await self.async_step_vacation_yearly()
        return await self.async_step_vacation_once()

    # --- Recurrence-specific fields, shared by add and edit ------------------

    async def async_step_vacation_once(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        existing = self._editing_plan()
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {
            "start_date": (
                existing.start_date.isoformat() if existing and existing.start_date else None
            ),
            "end_date": (
                existing.end_date.isoformat() if existing and existing.end_date else None
            ),
        }

        if user_input is not None:
            defaults = user_input
            start_date = date.fromisoformat(user_input["start_date"])
            end_date = date.fromisoformat(user_input["end_date"])
            if end_date <= start_date:
                errors["base"] = "vacation_window_invalid"
            else:
                plan = VacationPlan(
                    id=self._vacation_editing_id or str(uuid.uuid4()),
                    name=self._vacation_common["name"],
                    enabled=self._vacation_common["enabled"],
                    recurrence=RECURRENCE_ONCE,
                    min_temp_c=self._vacation_common["min_temp_c"],
                    start_date=start_date,
                    end_date=end_date,
                )
                if self._plan_overlaps_another(plan):
                    errors["base"] = "vacation_overlap"
                else:
                    return self._save_vacation_plan(plan)

        return self.async_show_form(
            step_id="vacation_once",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "start_date",
                        default=defaults.get("start_date"),
                    ): selector.DateSelector(),
                    vol.Required(
                        "end_date",
                        default=defaults.get("end_date"),
                    ): selector.DateSelector(),
                }
            ),
        )

    async def async_step_vacation_weekly(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        existing = self._editing_plan()
        errors: dict[str, str] = {}

        def _weekday_default(value: int | None) -> str | None:
            return str(value) if value is not None else None

        def _time_default(value: time | None) -> str | None:
            return value.isoformat() if value is not None else None

        defaults: dict[str, Any] = {
            "start_weekday": _weekday_default(existing.start_weekday if existing else None),
            "start_time": _time_default(existing.start_time if existing else None),
            "stop_weekday": _weekday_default(existing.stop_weekday if existing else None),
            "stop_time": _time_default(existing.stop_time if existing else None),
        }

        if user_input is not None:
            defaults = user_input
            plan = VacationPlan(
                id=self._vacation_editing_id or str(uuid.uuid4()),
                name=self._vacation_common["name"],
                enabled=self._vacation_common["enabled"],
                recurrence=RECURRENCE_WEEKLY,
                min_temp_c=self._vacation_common["min_temp_c"],
                start_weekday=int(user_input["start_weekday"]),
                start_time=time.fromisoformat(user_input["start_time"]),
                stop_weekday=int(user_input["stop_weekday"]),
                stop_time=time.fromisoformat(user_input["stop_time"]),
            )
            if self._plan_overlaps_another(plan):
                errors["base"] = "vacation_overlap"
            else:
                return self._save_vacation_plan(plan)

        weekday_options = self._weekday_options()
        return self.async_show_form(
            step_id="vacation_weekly",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "start_weekday", default=defaults.get("start_weekday")
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=weekday_options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Required(
                        "start_time", default=defaults.get("start_time")
                    ): selector.TimeSelector(),
                    vol.Required(
                        "stop_weekday", default=defaults.get("stop_weekday")
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=weekday_options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Required(
                        "stop_time", default=defaults.get("stop_time")
                    ): selector.TimeSelector(),
                }
            ),
        )

    async def async_step_vacation_yearly(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        existing = self._editing_plan()
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {
            "start_month": existing.start_month if existing else None,
            "start_day": existing.start_day if existing else None,
            "end_month": existing.end_month if existing else None,
            "end_day": existing.end_day if existing else None,
        }

        if user_input is not None:
            defaults = user_input
            start_month = int(user_input["start_month"])
            start_day = int(user_input["start_day"])
            end_month = int(user_input["end_month"])
            end_day = int(user_input["end_day"])
            try:
                # 2024 is a leap year, so 29 Feb stays legal; the selectors
                # only bound each field to 1..12 / 1..31 independently, which
                # admits impossible combinations like 30 Feb or 31 Apr.
                date(2024, start_month, start_day)
                date(2024, end_month, end_day)
                valid_dates = True
            except ValueError:
                valid_dates = False
                errors["base"] = "vacation_date_invalid"
            if valid_dates and (end_month, end_day) == (start_month, start_day):
                # _yearly_window_for_start_year treats an empty window as a
                # New-Year wrap and produces a 365-day setback.
                errors["base"] = "vacation_window_invalid"
            elif valid_dates:
                plan = VacationPlan(
                    id=self._vacation_editing_id or str(uuid.uuid4()),
                    name=self._vacation_common["name"],
                    enabled=self._vacation_common["enabled"],
                    recurrence=RECURRENCE_YEARLY,
                    min_temp_c=self._vacation_common["min_temp_c"],
                    start_month=start_month,
                    start_day=start_day,
                    end_month=end_month,
                    end_day=end_day,
                )
                if self._plan_overlaps_another(plan):
                    errors["base"] = "vacation_overlap"
                else:
                    return self._save_vacation_plan(plan)

        month_selector = selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=12, step=1, mode="box")
        )
        day_selector = selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=31, step=1, mode="box")
        )
        return self.async_show_form(
            step_id="vacation_yearly",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "start_month", default=defaults.get("start_month")
                    ): month_selector,
                    vol.Required("start_day", default=defaults.get("start_day")): day_selector,
                    vol.Required("end_month", default=defaults.get("end_month")): month_selector,
                    vol.Required("end_day", default=defaults.get("end_day")): day_selector,
                }
            ),
        )

    # --- Remove / reorder ----------------------------------------------------

    async def async_step_vacation_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        plans = self._current_plans()
        if user_input is not None:
            remaining = [p for p in plans if p.id != user_input["plan_id"]]
            return self._save({CONF_VACATION_PLANS: serialize_plans(remaining)})
        return self.async_show_form(
            step_id="vacation_remove",
            data_schema=vol.Schema(
                {
                    vol.Required("plan_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=plan.id, label=plan.name)
                                for plan in plans
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_vacation_reorder(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        plans = self._current_plans()
        if user_input is not None:
            plan_id = user_input["plan_id"]
            index = next((i for i, p in enumerate(plans) if p.id == plan_id), None)
            if index is not None:
                swap_with = index - 1 if user_input["direction"] == "up" else index + 1
                if 0 <= swap_with < len(plans):
                    plans[index], plans[swap_with] = plans[swap_with], plans[index]
            return self._save({CONF_VACATION_PLANS: serialize_plans(plans)})
        return self.async_show_form(
            step_id="vacation_reorder",
            data_schema=vol.Schema(
                {
                    vol.Required("plan_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=plan.id, label=plan.name)
                                for plan in plans
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required("direction", default="up"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value="up", label="Move up"),
                                selector.SelectOptionDict(value="down", label="Move down"),
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )
