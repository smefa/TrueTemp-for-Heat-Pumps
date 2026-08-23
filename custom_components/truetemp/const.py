"""Constants for the TrueTemp integration.

No `homeassistant.*` imports here: `heuristic.py`, `learner.py` and `lag.py`
are meant to be importable and unit-testable with zero Home Assistant
dependency, and they would transitively pull in `homeassistant` through this
module if it imported anything from here.

Everything that used to be a hand-typed control coefficient is gone. The
controller no longer has a proportional gain to tune, a price gain to size, or
a cold taper to draw: the offset learner measures what this house actually
needs (see `learner.py`) and the lag estimator measures how long it takes to
get there (see `lag.py`). What remains in config is either an entity to read,
an entity to write, or a genuine occupant preference.
"""

DOMAIN = "truetemp"

# hass.data key: a set of entry_ids whose *next* options update was written by
# services.py's vacation_plan_set/_remove/_reorder rather than the options
# flow. __init__.py's update listener checks this to skip the usual full
# entry reload for those writes — see its own comment for why that's safe.
DATA_VACATION_SKIP_RELOAD = f"{DOMAIN}_vacation_skip_reload"

# --- Config / options keys: sources to read -------------------------------
CONF_INDOOR_TEMP_SENSOR = "indoor_temp_sensor"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_NORDPOOL_PRICE_ENTITY = "nordpool_price_entity"

# --- Config / options keys: targets to write ------------------------------
# Which mechanism carries the published value to the heat pump. The two
# outdoor-spoofing channels below are two ways of reaching the SAME thing (a
# faked outdoor reading) and may be combined; heat-curve-offset writes a
# different quantity (a delta, not a temperature) to a different pump
# parameter, and indoor-climate writes a third (a target temperature, to a
# room-sensor climate entity), so each of those two is an alternative to
# outdoor spoofing and to each other rather than a further independent
# channel — running more than one alongside outdoor spoofing would compensate
# twice.
CONF_OUTPUT_MODE = "output_mode"
OUTPUT_MODE_OUTDOOR_SPOOF = "outdoor_temp_spoof"
OUTPUT_MODE_HEAT_CURVE_OFFSET = "heat_curve_offset"
OUTPUT_MODE_INDOOR_CLIMATE = "indoor_temp_climate"
OUTPUT_MODES = (
    OUTPUT_MODE_OUTDOOR_SPOOF,
    OUTPUT_MODE_HEAT_CURVE_OFFSET,
    OUTPUT_MODE_INDOOR_CLIMATE,
)

# Optional: a number entity to push the published compensated outdoor
# temperature into every cycle (e.g. `number.nibe_ohmigo_temperature`). Only
# used while CONF_OUTPUT_MODE is OUTPUT_MODE_OUTDOOR_SPOOF.
CONF_OUTPUT_NUMBER_ENTITY = "output_number_entity"
# Optional, independent of the above: talk to an OhmOnWifi/Ohmigo device's own
# local HTTP API directly (http://<host>/AT/?T=<value>). If both are set, both
# are pushed to every cycle. Only used while CONF_OUTPUT_MODE is
# OUTPUT_MODE_OUTDOOR_SPOOF.
CONF_OHMONWIFI_HOST = "ohmonwifi_host"
# A number entity backing the pump's own "heat curve offset" ("värmekurvans
# förskjutning") parameter, for pumps that expose one directly instead of
# needing a spoofed outdoor sensor. Only used while CONF_OUTPUT_MODE is
# OUTPUT_MODE_HEAT_CURVE_OFFSET.
CONF_HEAT_CURVE_OFFSET_ENTITY = "heat_curve_offset_entity"
# A hardware fact about the specific pump model, asked once — like
# CONF_HEATING_TYPE, not a tunable gain. This codebase's own offset is
# negative when more heat is wanted (see learner.py's "Sign convention"), but
# most pumps' native curve offset is the other way round: positive means more
# heat. False (the default) flips the sign to match that common convention;
# True is for the pumps that don't.
CONF_HEAT_CURVE_OFFSET_INVERT = "heat_curve_offset_invert"
# A climate entity to steer instead of a spoofed outdoor sensor or a native
# curve-offset dial, for pumps (or TRVs) that expose their own room-sensor
# climate entity. Written via `climate.set_temperature` with the occupant's
# effective indoor target plus the same heat/cool delta the other two modes
# would otherwise send — see `coordinator._async_push_indoor_climate`. Only
# used while CONF_OUTPUT_MODE is OUTPUT_MODE_INDOOR_CLIMATE. No invert flag:
# unlike a pump's proprietary curve dial, a climate entity's target
# temperature has one universal convention (higher means more heat), so
# there is no per-pump direction to flip.
CONF_INDOOR_CLIMATE_ENTITY = "indoor_climate_entity"

# --- Config / options keys: occupant preferences --------------------------
CONF_INDOOR_TARGET_TEMPERATURE = "indoor_target_temperature"
# The emitter type. Its only job is to seed the rise/fall lag fallbacks used
# before `lag.py` has measured this house's own response (see
# `lag.fallback_minutes`). Set once at install; not something that changes.
CONF_HEATING_TYPE = "heating_type"
CONF_ENABLE_PRICE_COMPENSATION = "enable_price_compensation"
# The absolute indoor floor for price compensation: how cold the house is
# allowed to get while chasing a cheap hour. The ONLY comfort bound left —
# there is no upper bound because pre-charging is already limited by the tier's
# own boost allowance, so a second clamp above never bound anything.
CONF_COMFORT_MIN_C = "comfort_min_c"
# How much day-ahead price swing (peak minus median, in the price sensor's own
# native units) counts as economically worth reacting to at all. Below this,
# `heuristic.price_significance()` tapers braking/pre-charge authority toward
# zero regardless of how large the swing looks in RELATIVE terms — see that
# function's docstring for the field data that showed relative-only scoring
# ranking days backwards in money terms and misfiring near a near-zero median.
#
# 0 (the default) means AUTO rather than "no floor": see
# `heuristic._resolve_absolute_floor_c` for why a single fixed constant cannot
# mean the same thing across currencies and sub-units (SEK/kWh, öre/kWh,
# EUR/MWh, ...) and what the auto value is derived from instead. Any other
# value is used verbatim as an explicit override, in the price sensor's own
# native units — see config_flow.py's `_price_unit` for why that unit must be
# read from the price entity's `unit_of_measurement` attribute and never its
# `currency` one.
CONF_PRICE_SIGNIFICANCE_FLOOR = "price_significance_floor"
# Master toggles for the two optional weather-derived feedforward terms. Each
# switches its term off entirely rather than relying on a zeroed coefficient.
# With both off, no weather entity is needed at all.
CONF_ENABLE_SOLAR_INPUT = "enable_solar_input"
CONF_ENABLE_WIND_INPUT = "enable_wind_input"
# Whether to act on the weather forecast — outdoor temperature and wind
# pre-ramp ahead of a cold front the way price compensation already acts
# ahead of a spike, while cloud/sun instead pre-cools ahead of a forecast
# rise in sun (see heuristic._term_precool_c for why that one runs the
# opposite direction). One switch for all three forecast signals: "should it
# act on forecasts at all" is a single occupant preference, not three. Needs
# the weather entity even with both terms above switched off. See
# heuristic._term_preramp_c.
CONF_ENABLE_WEATHER_LOOKAHEAD = "enable_weather_lookahead"

# --- Config / options keys: plumbing --------------------------------------
CONF_ENABLE_DATA_LOGGING = "enable_data_logging"

# The vacation plan list (see docs/plan_vacation_plans.md), a list of
# vacation.serialize_plan()-shaped dicts. Written either through the options
# flow's "vacation" pages (config_flow.py) or, for the dashboard card,
# services.py's vacation_plan_set/_remove/_reorder — see DATA_VACATION_SKIP_RELOAD
# for why the latter path doesn't reload the entry the way an options-flow
# save does.
CONF_VACATION_PLANS = "vacation_plans"

# --- Config entry schema ---------------------------------------------------
# Bumped whenever a stored entry needs rewriting; see `async_migrate_entry`.
#   1 -> 2  prune the keys left behind by the pre-v0.2.0 RC/MPC/gains stack.
CONFIG_ENTRY_VERSION = 2

# Every key this integration still reads, across both `entry.data` and
# `entry.options`. Which of the two a key lives in is not fixed — `_entry_value`
# layers options over data, and several keys moved between the two as the setup
# flow was reorganized — so this is one flat set rather than two.
#
# The migration prunes by this allow-list rather than by naming the dead keys,
# because the options flow saves `{**existing_options, **page_fields}`: any key
# ever written survives every later edit, so the set of possible strays is the
# union of every version's schema and not worth enumerating. Adding a new
# setting means adding it here too, or it will be pruned on the next bump.
KNOWN_CONFIG_KEYS = frozenset(
    {
        "name",  # homeassistant.const.CONF_NAME, inlined to keep this module HA-free
        CONF_INDOOR_TEMP_SENSOR,
        CONF_OUTDOOR_TEMP_SENSOR,
        CONF_WEATHER_ENTITY,
        CONF_NORDPOOL_PRICE_ENTITY,
        CONF_OUTPUT_MODE,
        CONF_OUTPUT_NUMBER_ENTITY,
        CONF_OHMONWIFI_HOST,
        CONF_HEAT_CURVE_OFFSET_ENTITY,
        CONF_HEAT_CURVE_OFFSET_INVERT,
        CONF_INDOOR_CLIMATE_ENTITY,
        CONF_INDOOR_TARGET_TEMPERATURE,
        CONF_HEATING_TYPE,
        CONF_ENABLE_PRICE_COMPENSATION,
        CONF_COMFORT_MIN_C,
        CONF_PRICE_SIGNIFICANCE_FLOOR,
        CONF_ENABLE_SOLAR_INPUT,
        CONF_ENABLE_WIND_INPUT,
        CONF_ENABLE_WEATHER_LOOKAHEAD,
        CONF_ENABLE_DATA_LOGGING,
        CONF_VACATION_PLANS,
    }
)

# --- Defaults -------------------------------------------------------------
DEFAULT_INDOOR_TARGET_TEMPERATURE = 21.0
DEFAULT_ENABLE_PRICE_COMPENSATION = False
DEFAULT_COMFORT_MIN_C = 18.0
# 0 means AUTO: heuristic._resolve_absolute_floor_c derives a floor from this
# install's own price history instead of using a fixed number. A fixed
# non-zero default was tried and rejected — Nordpool alone spans SEK, NOK,
# DKK and EUR, in whole-currency or öre/øre/cents sub-units, and per kWh, Wh
# or MWh: a single constant spanning ~10^8 in real terms would either silently
# disable saving entirely for some users (a EUR/kWh household) or protect
# nobody (an öre/kWh or Wh household). See that function's docstring for what
# the auto value actually computes and its own honestly-stated limitation.
DEFAULT_PRICE_SIGNIFICANCE_FLOOR = 0.0
DEFAULT_ENABLE_SOLAR_INPUT = True
DEFAULT_ENABLE_WIND_INPUT = False
# Off, for the same reason compensation itself ships off: an upgrade must not
# silently change what an existing install publishes.
DEFAULT_ENABLE_WEATHER_LOOKAHEAD = False
DEFAULT_ENABLE_DATA_LOGGING = False
DEFAULT_OUTPUT_MODE = OUTPUT_MODE_OUTDOOR_SPOOF
DEFAULT_HEAT_CURVE_OFFSET_INVERT = False

# The controller runs on a fixed cadence rather than a configurable one. The
# 2026-08-07 validation traced its single largest data-quality failure to the
# estimator being stepped on every event-driven refresh (median 6.6 min, 11% of
# steps under 2 min) instead of on a regular clock: at a 0.1 degC sensor
# resolution that put the signal-to-noise ratio at 0.12 and left 63% of
# transitions reading exactly zero change. The learner now steps on this
# interval and only this interval; refreshes triggered by a source entity
# recovering still republish the output, they just don't advance learning.
UPDATE_INTERVAL_MINUTES = 15
LEARNER_STEP_SECONDS = UPDATE_INTERVAL_MINUTES * 60

# How much of the local JSONL data log (see data_logger.py) diagnostics
# attaches as a trend window. Opt-in and best-effort: empty if data logging
# was never turned on. Kept short enough that even a fully populated window
# stays a reasonable size to paste into a GitHub issue.
DIAGNOSTICS_HISTORY_DAYS = 3

# How long after this config entry's setup to hold off raising a source-unavailable
# Repair issue. HA restarts entities in no guaranteed order, so a sensor that is
# perfectly healthy can still read unavailable/unknown for the first minute or two
# while its own integration is still starting up. Source reads, retries and the
# state-change listener all behave normally through this window - only the
# Repairs alarm is deferred, so a genuine outage still surfaces once the window
# passes.
STARTUP_GRACE_PERIOD_MINUTES = 5

# Bundled Lovelace card. The whole www/ directory is served under this
# prefix (see _async_register_frontend) so the card can `import` its
# per-language files out of www/lang/ alongside the main script. The version
# is appended as a cache-busting query string, so bump it whenever the JS
# changes — the main file or any of the language files.
FRONTEND_STATIC_URL_PREFIX = "/truetemp"
FRONTEND_CARD_URL = f"{FRONTEND_STATIC_URL_PREFIX}/truetemp-card.js"
FRONTEND_JS_VERSION = "19"

# The vacation-plans card is a second, independent bundle (see
# www/truetemp-vacation-card.js's module docstring for why) with its own
# cache-busting version, bumped independently of FRONTEND_JS_VERSION above.
# Replaced the single-scenario truetemp-holiday-card.js in 0.4.0.
FRONTEND_VACATION_CARD_URL = f"{FRONTEND_STATIC_URL_PREFIX}/truetemp-vacation-card.js"
FRONTEND_VACATION_JS_VERSION = "2"
