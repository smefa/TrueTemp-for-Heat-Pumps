"""`truetemp.vacation_plan_set`/`_remove`/`_reorder` services.

The write path the Phase 6 dashboard card uses (`docs/plan_vacation_plans.md`
§6/§7) — the card has no other way to persist a plan, since the options-flow
CRUD in `config_flow.py` only runs inside a config-flow step. `_reorder` was
added alongside the Phase 6b card itself (not part of the original Phase 6a
cut) once the card's up/down reorder requirement turned out to have no
service to write through — `_set`'s upsert always appends, it can't reposition
an existing plan. Deliberately NOT entity-targeted (no HA entity models "one
vacation plan"): all three services take an explicit `config_entry_id` so a
multi-zone install can address one TrueTemp instance's plan list unambiguously,
mirroring how e.g. the built-in `backup.create` service targets an agent by id
rather than an entity.

Registered once, globally, guarded by a `hass.data` flag exactly like
`_async_register_frontend` in `__init__.py` — services are integration-level,
not per-config-entry, and (like the frontend static path) are never
unregistered; a second config entry loading must not attempt to re-register
and raise.

Validation mirrors `config_flow.py`'s vacation steps exactly: recurrence-
specific fields are required by hand (a plain schema can't express "required
only when recurrence == weekly"), a `once` window must have `end_date` after
`start_date`, and `vacation.find_overlap` refuses (not warns) a save that
would leave two enabled plans overlapping — the same §9 answer. Anything
that would be a form error there is a `ServiceValidationError` here, so the
card's `hass.callService` promise rejects with a readable message.
"""

from __future__ import annotations

import uuid
from datetime import date, time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

import homeassistant.util.dt as dt_util

from .const import CONF_VACATION_PLANS, DATA_VACATION_SKIP_RELOAD, DOMAIN
from .vacation import (
    RECURRENCE_ONCE,
    RECURRENCE_WEEKLY,
    RECURRENCES,
    VacationPlan,
    deserialize_plans,
    find_overlap,
    serialize_plans,
)

SERVICE_VACATION_PLAN_SET = "vacation_plan_set"
SERVICE_VACATION_PLAN_REMOVE = "vacation_plan_remove"
SERVICE_VACATION_PLAN_REORDER = "vacation_plan_reorder"

SERVICE_RESET_LEARNING = "reset_learning"

_SERVICES_REGISTERED = f"{DOMAIN}_services_registered"

_RESET_LEARNING_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Optional("current_band_only", default=False): cv.boolean,
    }
)

_WEEKDAY = vol.All(vol.Coerce(int), vol.Range(min=0, max=6))
_MONTH = vol.All(vol.Coerce(int), vol.Range(min=1, max=12))
_DAY = vol.All(vol.Coerce(int), vol.Range(min=1, max=31))

_VACATION_PLAN_SET_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Optional("id"): cv.string,
        vol.Required("name"): cv.string,
        vol.Required("enabled"): cv.boolean,
        vol.Required("recurrence"): vol.In(RECURRENCES),
        vol.Required("min_temp_c"): vol.Coerce(float),
        vol.Optional("start_date"): cv.string,
        vol.Optional("end_date"): cv.string,
        vol.Optional("start_weekday"): _WEEKDAY,
        vol.Optional("start_time"): cv.string,
        vol.Optional("stop_weekday"): _WEEKDAY,
        vol.Optional("stop_time"): cv.string,
        vol.Optional("start_month"): _MONTH,
        vol.Optional("start_day"): _DAY,
        vol.Optional("end_month"): _MONTH,
        vol.Optional("end_day"): _DAY,
    }
)

_VACATION_PLAN_REMOVE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("id"): cv.string,
    }
)

_VACATION_PLAN_REORDER_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry_id"): cv.string,
        vol.Required("id"): cv.string,
        vol.Required("direction"): vol.In(("up", "down")),
    }
)


def _resolve_entry(hass: HomeAssistant, config_entry_id: str):
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="vacation_service_unknown_entry"
        )
    return entry


def _update_plans(hass: HomeAssistant, entry, plans: list[VacationPlan]) -> None:
    """Write the plan list, flagged so __init__.py's update listener does a
    plain coordinator refresh instead of its usual full reload — see
    DATA_VACATION_SKIP_RELOAD. Every write in this module goes through here
    so no call site can forget the flag and reintroduce the card's blank-for-
    a-few-seconds reload flicker.

    The flag is only set when async_update_entry actually changes something.
    HA does not fire the update listener at all for a no-op write (identical
    options), so an unconditional flag would sit stale in hass.data and get
    consumed by the *next* real options-flow change instead, turning that
    change's reload into a no-op refresh."""
    changed = hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_VACATION_PLANS: serialize_plans(plans)}
    )
    if changed:
        hass.data.setdefault(DATA_VACATION_SKIP_RELOAD, set()).add(entry.entry_id)


def _plan_from_service_data(data: dict[str, Any]) -> VacationPlan:
    common = {
        "id": data.get("id") or str(uuid.uuid4()),
        "name": data["name"],
        "enabled": data["enabled"],
        "recurrence": data["recurrence"],
        "min_temp_c": data["min_temp_c"],
    }

    try:
        if data["recurrence"] == RECURRENCE_ONCE:
            start_date = date.fromisoformat(data["start_date"])
            end_date = date.fromisoformat(data["end_date"])
            if end_date <= start_date:
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="vacation_service_window_invalid"
                )
            return VacationPlan(**common, start_date=start_date, end_date=end_date)

        if data["recurrence"] == RECURRENCE_WEEKLY:
            return VacationPlan(
                **common,
                start_weekday=data["start_weekday"],
                start_time=time.fromisoformat(data["start_time"]),
                stop_weekday=data["stop_weekday"],
                stop_time=time.fromisoformat(data["stop_time"]),
            )

        # RECURRENCE_YEARLY — the only remaining option once `recurrence`
        # passed the schema's `vol.In(RECURRENCES)` check.
        start_month = data["start_month"]
        start_day = data["start_day"]
        end_month = data["end_month"]
        end_day = data["end_day"]
        try:
            # 2024 is a leap year, so 29 Feb stays legal; the schema only
            # bounds each field to 1..12 / 1..31 independently, which admits
            # impossible combinations like 30 Feb or 31 Apr.
            date(2024, start_month, start_day)
            date(2024, end_month, end_day)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="vacation_service_date_invalid"
            ) from err
        if (end_month, end_day) == (start_month, start_day):
            # _yearly_window_for_start_year treats an empty window as a
            # New-Year wrap and produces a 365-day setback.
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="vacation_service_window_invalid"
            )
        return VacationPlan(
            **common,
            start_month=start_month,
            start_day=start_day,
            end_month=end_month,
            end_day=end_day,
        )
    except KeyError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="vacation_service_missing_fields"
        ) from err
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="vacation_service_window_invalid"
        ) from err


async def _async_handle_vacation_plan_set(call: ServiceCall) -> None:
    entry = _resolve_entry(call.hass, call.data["config_entry_id"])
    plan = _plan_from_service_data(call.data)

    existing_plans = deserialize_plans(entry.options.get(CONF_VACATION_PLANS, []))
    plans = [p for p in existing_plans if p.id != plan.id]
    plans.append(plan)
    if find_overlap(plans, dt_util.now().replace(tzinfo=None)) is not None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="vacation_service_overlap"
        )

    _update_plans(call.hass, entry, plans)


async def _async_handle_vacation_plan_remove(call: ServiceCall) -> None:
    entry = _resolve_entry(call.hass, call.data["config_entry_id"])
    plans = [
        p
        for p in deserialize_plans(entry.options.get(CONF_VACATION_PLANS, []))
        if p.id != call.data["id"]
    ]
    _update_plans(call.hass, entry, plans)


async def _async_handle_vacation_plan_reorder(call: ServiceCall) -> None:
    """Swap a plan with its neighbour in list order — the priority walk in
    `vacation.resolve_vacation()` is list-order-based, so this is the only
    way to change which plan wins an overlap. Mirrors `config_flow.py`'s
    `async_step_vacation_reorder` swap logic field-for-field, including its
    silent no-op when the plan is unknown or already at that end of the
    list (not a validation error there, so not one here either)."""
    entry = _resolve_entry(call.hass, call.data["config_entry_id"])
    plans = deserialize_plans(entry.options.get(CONF_VACATION_PLANS, []))

    index = next((i for i, p in enumerate(plans) if p.id == call.data["id"]), None)
    if index is not None:
        swap_with = index - 1 if call.data["direction"] == "up" else index + 1
        if 0 <= swap_with < len(plans):
            plans[index], plans[swap_with] = plans[swap_with], plans[index]

    _update_plans(call.hass, entry, plans)


async def _async_handle_reset_learning(call: ServiceCall) -> None:
    entry = _resolve_entry(call.hass, call.data["config_entry_id"])
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="service_entry_not_loaded"
        )
    if call.data["current_band_only"]:
        await entry.runtime_data.async_reset_current_band()
    else:
        await entry.runtime_data.async_reset_learning()


def async_register_services(hass: HomeAssistant) -> None:
    if hass.data.get(_SERVICES_REGISTERED):
        return
    hass.data[_SERVICES_REGISTERED] = True

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_LEARNING,
        _async_handle_reset_learning,
        schema=_RESET_LEARNING_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VACATION_PLAN_SET,
        _async_handle_vacation_plan_set,
        schema=_VACATION_PLAN_SET_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VACATION_PLAN_REMOVE,
        _async_handle_vacation_plan_remove,
        schema=_VACATION_PLAN_REMOVE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VACATION_PLAN_REORDER,
        _async_handle_vacation_plan_reorder,
        schema=_VACATION_PLAN_REORDER_SCHEMA,
    )
