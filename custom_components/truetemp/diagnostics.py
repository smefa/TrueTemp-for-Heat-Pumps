"""Config-entry diagnostics: one-click full state dump.

Surfaced in Home Assistant as the "Download diagnostics" button on the
integration's entry page. HA discovers this module by name, so no platform
registration is needed.

Why this exists on top of the live entities and the opt-in JSONL log: they
answer different questions. The entities show the latest value of a handful of
things; the log is a long, opt-in time series most users never enable. Neither
gives a complete cross-sectional snapshot of everything at once when something
looks wrong right now.

`recent_history` bridges the two: the last `DIAGNOSTICS_HISTORY_DAYS` of the
log, if data logging has been on long enough to have any. It's what turns
"the offset table looks odd" into "and here's the trend that produced it"
without a separate replay session. Empty, not an error, if logging was never
enabled — the entities and tables above already stand on their own.

The artefact that appears nowhere else is the **full offset table** — every
outdoor band's learned offset, capacity ceiling, observed recovery rate and
sample count. The `learned_offset` sensor shows only the band currently
occupied, so the table is what distinguishes "still filling in" from "learned
something odd at -10 degC and has not been back since".

Alongside it is the **full baseline table** — the open-loop equilibrium indoor
temperature per band, recorded while compensation is off (see `learner.py`'s
"Two regimes" docstring). It is what makes the before/after comparison
possible: "the raw curve holds you 1.2 degC below target at -5 degC; the
learned offset holds you 0.1 degC below" is only answerable with both tables
in front of you at once.

Redaction: the output-push targets are removed. None is a credential
(this integration has none), but a hostname and the entity id of a device
register describe someone's local network and hardware, and diagnostics files
routinely get pasted into public issue trackers. Source entity ids are kept
deliberately — they are frequently what the problem turns out to be.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import TrueTempConfigEntry
from .const import (
    CONF_HEAT_CURVE_OFFSET_ENTITY,
    CONF_INDOOR_CLIMATE_ENTITY,
    CONF_OHMONWIFI_HOST,
    CONF_OUTPUT_NUMBER_ENTITY,
    DIAGNOSTICS_HISTORY_DAYS,
)
from .data_logger import async_read_recent_records
from .heuristic import (
    COLD_CAUTIONS,
    PRICE_TIERS,
    SOLAR_GAIN_C,
    WIND_GAIN_C_PER_MS,
    resolve_cold_caution,
    resolve_price_tier,
)
from .lag import (
    EVENT_MIN_MOVE_C,
    MAX_LAG_MINUTES,
    MIN_EVENTS,
    MIN_RESPONSE_C,
    RESPONSE_FRACTION,
    TARGET_STEP_C,
    fallback_minutes,
)
from .learner import (
    AUTHORITY_RAMP_SAMPLES,
    BIN_EDGES,
    MAX_AUTHORITY_C,
    MIN_AUTHORITY_C,
    SPOOF_PER_INDOOR_C,
    TAU_I_LAG_MULTIPLE,
    bin_label,
    seed_offset_for,
)

TO_REDACT = {
    CONF_OHMONWIFI_HOST,
    CONF_OUTPUT_NUMBER_ENTITY,
    CONF_HEAT_CURVE_OFFSET_ENTITY,
    CONF_INDOOR_CLIMATE_ENTITY,
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TrueTempConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    learner = coordinator.learner_result
    lag = coordinator.lag_result
    result = coordinator.data
    tier = resolve_price_tier(coordinator.price_comfort_tier)
    caution = resolve_cold_caution(coordinator.cold_caution)
    recent_history = await async_read_recent_records(
        hass, entry.entry_id, DIAGNOSTICS_HISTORY_DAYS
    )

    return {
        "config": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        # Live control surface: in-memory values restored by the entities, so
        # they are NOT visible in the config above.
        "runtime": {
            "is_active": coordinator.is_active,
            "output_mode": coordinator.output_mode,
            "heat_curve_offset_invert": coordinator.heat_curve_offset_invert,
            "indoor_target_c": coordinator.indoor_target_c,
            "price_comfort_tier": coordinator.price_comfort_tier,
            "cold_caution": coordinator.cold_caution,
            "price_configured": coordinator.price_configured,
            "price_enabled": coordinator.price_enabled,
            "solar_input_enabled": coordinator.solar_input_enabled,
            "wind_input_enabled": coordinator.wind_input_enabled,
            "heating_type": coordinator.heating_type,
            "data_logging_enabled": coordinator.data_logging_enabled,
        },
        "output": asdict(result) if result is not None else None,
        "learner": {
            "latest": asdict(learner) if learner is not None else None,
            "total_samples": coordinator.learner_state.total_samples,
            "saturation_streak": coordinator.learner_state.saturation_streak,
            "prev_offset_c": coordinator.learner_state.prev_offset_c,
            # Whether the last cycle observed was compensation-active. Read
            # directly off the persisted state rather than off `learner.latest`,
            # since the off->on edge that triggers seeding is detected from this
            # very field on the NEXT cycle.
            "prev_is_active": coordinator.learner_state.prev_is_active,
            # The whole table. This is the artefact worth having.
            "table": [
                {
                    "band": bin_label(index),
                    "hold_offset_c": b.hold_offset_c,
                    "ceiling_c": b.ceiling_c,
                    "useful_offset_c": b.useful_offset_c,
                    "close_rate_c_per_h": b.close_rate_c_per_h,
                    "samples": b.samples,
                    "seeded": b.seeded,
                }
                for index, b in enumerate(coordinator.learner_state.bins)
            ],
            # The open-loop counterpart, filled in while compensation is off.
            # Carries the implied offset (`seed_offset_for` against the CURRENT
            # target, so it reflects what a seed would do right now rather than
            # whatever the target was at observation time) and the deficit, so
            # the before/after comparison does not require doing the arithmetic
            # by hand.
            "baseline_table": [
                {
                    "band": bin_label(index),
                    "indoor_c": b.indoor_c if b.samples > 0 else None,
                    "samples": b.samples,
                    "implied_offset_c": (
                        seed_offset_for(b.indoor_c, coordinator.indoor_target_c)
                        if b.samples > 0
                        else None
                    ),
                    "deficit_c": (
                        coordinator.indoor_target_c - b.indoor_c
                        if b.samples > 0
                        else None
                    ),
                }
                for index, b in enumerate(coordinator.learner_state.baseline_bins)
            ],
        },
        "lag": {
            "latest": asdict(lag) if lag is not None else None,
            "buffer_samples": len(coordinator.lag_state.indoors_c),
            "step_minutes": coordinator.lag_state.step_minutes,
            "fallback_minutes": fallback_minutes(coordinator.heating_type),
        },
        # Resolved constants, so a diagnostics file is self-describing without
        # needing the matching source checkout.
        "constants": {
            "spoof_per_indoor_c": SPOOF_PER_INDOOR_C,
            "tau_i_lag_multiple": TAU_I_LAG_MULTIPLE,
            "authority_c": [MIN_AUTHORITY_C, MAX_AUTHORITY_C],
            "authority_ramp_samples": AUTHORITY_RAMP_SAMPLES,
            "bin_edges": list(BIN_EDGES),
            "solar_gain_c": SOLAR_GAIN_C,
            "wind_gain_c_per_ms": WIND_GAIN_C_PER_MS,
            "lag_max_minutes": MAX_LAG_MINUTES,
            "lag_min_events": MIN_EVENTS,
            "lag_event_min_move_c": EVENT_MIN_MOVE_C,
            "lag_min_response_c": MIN_RESPONSE_C,
            "lag_response_fraction": RESPONSE_FRACTION,
            "lag_target_step_c": TARGET_STEP_C,
            "price_tier": asdict(tier),
            "price_tiers": {name: asdict(t) for name, t in PRICE_TIERS.items()},
            "cold_caution": asdict(caution),
            "cold_cautions": {name: asdict(c) for name, c in COLD_CAUTIONS.items()},
        },
        # Trend window from the local data log, oldest first. See module
        # docstring; empty if data logging has never been enabled.
        "recent_history": {
            "days": DIAGNOSTICS_HISTORY_DAYS,
            "records": recent_history,
        },
    }
