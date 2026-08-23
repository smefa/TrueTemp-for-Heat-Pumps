"""Composes the final compensated outdoor temperature from its terms.

Pure module: standard library only and no dependency on any other module in
this package (not even `const.py`), so it stays importable and unit-testable
with zero Home Assistant dependency.

## What this module is, and is no longer

It used to be the whole controller: a proportional term on indoor error with a
hand-typed gain, plus weather and price corrections. The proportional term has
moved to `learner.py`, which measures what offset this house actually needs
instead of scaling an error by a number somebody guessed. What remains here is
composition and the price logic:

    compensated = raw_outdoor
                + learned_offset      from learner.py (integral + small P)
                - wind * WIND_GAIN    feedforward, optional
                + solar * SOLAR_GAIN  feedforward, optional
                + price_adjustment    bounded, tier-scaled, timed off the
                                      measured fall time
                + weather_preramp     one-sided forecast lookahead on outdoor
                                      and wind, timed off the measured rise
                                      time, optional
                + sun_precool         mirror-image lookahead on solar alone:
                                      backs heat off ahead of MORE sun, timed
                                      off the measured fall time, optional

## Why the two feedforward gains are constants and not config

Solar and wind vary far faster than the learner's integrator can track, so they
have to be fed forward rather than learned out — that is what feedforward is
for. But they are also NOT worth learning: the 2026-08-07 validation fitted a
solar coefficient that came out negative and significant, i.e. sunlight cooling
the house, because sun correlates with outdoor temperature and the regression
happily attributed envelope behaviour to it. Wind is worse; it is collinear
with the envelope term outright.

That negative fit has a second cause worth recording, because it bounds how far
the result generalises: the indoor sensor in that house was in the basement.
There was no solar gain at the measurement point to recover, so the only
sun-shaped signal in the data was the correlation with cold, clear weather.
Both effects push the coefficient the same way, and neither is fixed by
collecting more data or by running open loop — the sun-and-cold correlation is
a property of the weather, not of the control loop.

The practical consequence is a placement rule rather than a modelling one, and
it lives in the README and the config-flow help: enable the solar term only
when the indoor sensor is somewhere the sun actually reaches, and the wind term
only for a genuinely draughty building. Both default off. Whether the solar
gain is large is a per-house question — in a house with the sensor in the sun's
path it can dominate a winter afternoon, which is exactly the case the fixed
SOLAR_GAIN_C is a rough compromise for.

A fixed, physically-sensible constant has a much better failure mode. Any bias
in the daily average is absorbed by the integrator within a day, so a slightly
wrong gain causes a little intra-day ripple and no steady-state error at all.
A learned gain with the wrong sign would actively fight the loop.

## Price: a deliberate excursion, not a disturbance

Price compensation is the one thing here that intentionally holds the house
away from target. That matters for the learner, which would otherwise read the
sag as error and wind up fighting it. `price_braking` on the result is what
tells the learner to freeze; see `learner._freeze_reason`.

## Price significance: a taper, not a gate

`_price_band_info` below scores TODAY's price band purely in relative terms —
peak vs median, as a fraction of the median. That is not the same question as
"is today's swing worth the comfort trade", and confirmed against 16 days of
the reference install's real SE2 logs, it answers the wrong one in two
distinct ways: it ranked a 0.19 SEK/kWh day above a 0.91 SEK/kWh day because
the smaller swing happened to sit on a smaller median (400% relative vs 95%),
and it hands out full braking authority for a one-öre swing whenever the
median itself is close to zero (a 0.001/0.01 SEK median/peak pair scores a
9.0 ratio).

`price_significance()` fixes this with two multiplicative 0..1 terms — how
large today's spread is against a SEASONAL reference built from this
install's own price history, and how large it is against one ABSOLUTE floor
(auto-derived from the local price level by default, or a fixed user-set
value) — combined by taking the minimum, and applied as a continuous taper on
`price_shift_c` rather than a second hard gate. A gate would flap:
the band widens within a day as tomorrow's prices publish (typically ~13:00
for Nordpool), so a threshold crossed and re-crossed mid-day would start and
stop braking mid-cycle for no change in outdoor conditions. See
`PriceSpreadHistory`, `update_price_spread_history` and `price_significance`
below for the mechanism, and `compute()` for where the taper is applied.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import cos, radians, sin

MODEL_VERSION = "compose_v2"

# Safety bounds on the final output, independent of anything the user or the
# learner can set — a last-resort guard against garbage propagating to a real
# heat pump if an upstream source misbehaves.
OUTPUT_SANITY_MIN_C = -40.0
OUTPUT_SANITY_MAX_C = 25.0

# Feedforward gains for the two optional weather inputs. See the module
# docstring for why these are constants rather than configuration or learned
# parameters. Units: degC of outdoor spoof per unit of input.
SOLAR_GAIN_C = 4.0
WIND_GAIN_C_PER_MS = 0.3
# Below this the wind term is a breeze, not a draught: contributes exactly 0
# rather than a tiny, mostly-noise correction. The gain applies to the speed
# above the deadband, not the raw reading, so the term is continuous at the
# boundary instead of stepping straight to -WIND_GAIN_C_PER_MS * 4.0.
WIND_DEADBAND_MS = 4.0

# A price adjustment smaller than this is not a deliberate excursion and should
# not freeze the learner.
PRICE_BRAKING_EPS_C = 0.05

# --- Weather lookahead (pre-ramping) ---------------------------------------
# Total budget across the outdoor and wind pre-ramps, not per signal. A cold
# front legitimately raises conduction loss and infiltration loss at once, so
# the terms are additive in physics — but two simultaneous overshoots are
# still one overshoot too many for the learner to unwind afterwards. 3.0 sits
# below learner.MAX_AUTHORITY_C (5.0) and PRICE_CATCHUP_MAX_C (6.0), and
# below the largest single steady-state feedforward term (SOLAR_GAIN_C).
WEATHER_PRERAMP_MAX_C = 3.0
# Below this the ramp is noise: not reported, and does not freeze the learner.
# Deliberately looser than PRICE_BRAKING_EPS_C — a pre-ramp fires far more
# often than a price spike, and freezing on every 0.1 degC wobble would starve
# the learner through a whole winter.
WEATHER_PRERAMP_EPS_C = 0.2

# Budget for the sun pre-cool term, kept separate from WEATHER_PRERAMP_MAX_C
# on purpose: that budget bounds a comfort-overshoot hedge (worst case if
# wrong: briefly a bit warm), while this one bounds a comfort-undershoot bet
# (worst case if wrong: briefly a bit cold with no sun to cover it). Same
# starting magnitude as the shared budget until field data argues for a
# different number.
SOLAR_PRECOOL_MAX_C = 3.0

# --- Price catch-up (feedforward kick) --------------------------------------
# `max_sag_c`/`precharge_c` above are a comfort bound: how far indoor is
# actually allowed to drift, applied verbatim to `effective_indoor_target_c`.
# But the outdoor-spoof delta and the real indoor response are related by the
# house's thermal lag, not a fast controller — sending exactly the
# steady-state degree count takes as long to reach that sag as the house's own
# time constant does, often hours, which can leave a spike half over before
# the pump ever throttles back.
#
# `price_catchup_c` (computed in `compute()`) is a separate feedforward push
# added ON TOP of the steady-state price adjustment — never onto the target
# itself, which stays the comfort bound above — sized to how far indoor still
# has left to go. It decays to zero as indoor actually reaches the target sag,
# so the published value settles back at the plain steady-state shift rather
# than holding an inflated push indefinitely (which would just keep driving
# indoor further than the tier's comfort bound intends).
PRICE_CATCHUP_GAIN = 2.0
PRICE_CATCHUP_MAX_C = 6.0

# Fixed (not target-relative) hard limit: at or above this raw outdoor
# temperature, no plausible combination of learned offset, wind, sun or price
# terms is allowed to call for heat, however each one individually resolves.
# A bad indoor reading or a windy afternoon could otherwise still push the
# published value below raw even when it is plainly warm outside, e.g. asking
# the pump to fight an AC unit that has no way to explain itself to this
# integration. Deliberately absolute rather than derived from the indoor
# target: the target says nothing about whether the house has separate
# cooling.
HEATING_HARD_LIMIT_C = 20.0

# Re-entry margin below `HEATING_HARD_LIMIT_C` before the guardrail lets go
# again — same flapping fix as `HEATING_CUTOFF_HYSTERESIS_C` used to be, for
# the same reason: a raw reading idling within noise of a bare threshold would
# otherwise cross back and forth every cycle. See
# `resolve_heating_hard_limit_engaged`.
HEATING_HARD_LIMIT_HYSTERESIS_C = 2.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def resolve_heating_hard_limit_engaged(
    raw_outdoor_temp_c: float, prev_engaged: bool
) -> bool:
    """Whether the hard heating limit is active this cycle.

    Hysteresis, not a bare `>=`, for the same reason the old heating-cutoff
    guardrail needed it: a raw outdoor reading idling within a fraction of a
    degree of `HEATING_HARD_LIMIT_C` would otherwise snap the published value
    between the forced ceiling and a normally-computed one every cycle. Once
    engaged, the reading has to drop `HEATING_HARD_LIMIT_HYSTERESIS_C` below
    the threshold, not just below it, before normal compensation resumes.
    """
    if prev_engaged:
        return raw_outdoor_temp_c >= HEATING_HARD_LIMIT_C - HEATING_HARD_LIMIT_HYSTERESIS_C
    return raw_outdoor_temp_c >= HEATING_HARD_LIMIT_C


def solar_effect_of(
    sun_elevation_deg: float, sun_azimuth_deg: float, cloud_coverage_pct: float | None
) -> float:
    """Fraction of full solar gain available right now, through a fixed
    south-facing vertical window (no per-house orientation input; south is
    the assumed case this input is meant for).

    Geometry is `cos(elevation) * cos(azimuth_offset)`, not `sin(elevation)`
    alone: a vertical pane is lit best by low, streaming-in sun square to the
    glass, not by sun overhead, so gain falls to zero as the sun approaches
    zenith rather than at the horizon. `azimuth_offset` is how far the sun
    currently sits from due south (`sun_azimuth_deg - 180`, HA's convention of
    0=north/clockwise); its cosine is the standard angle-of-incidence term for
    a flat vertical surface, and is clamped to zero once the sun swings
    beyond +/-90° off south — at that point it is behind the wall, not in
    front of it. This replaces an earlier version of this function that
    dropped the azimuth term entirely and assumed the sun was always due
    south: harmless at midday, but it meant a low morning or evening sun
    scored as if square-on even when it was really raking in from the side or
    already behind the house.

    Multiplied by an atmospheric transmittance term, `0.7^(air_mass^0.678)`,
    air mass being `1/sin(elevation)`: low sun travels through much more
    atmosphere, which cuts the other way from the geometry term and keeps the
    horizon from reading as full-strength. Net effect versus the old
    `sin(elevation)` shape: winter and shoulder-season sun score much higher
    relative to summer, since the geometry term now rewards exactly the low
    sun angles the atmosphere term discounts, rather than both terms
    discounting low sun the same way. The peak, therefore, lands at a modest
    elevation (roughly 30-35°, sun due south) rather than at zenith, and never
    approaches 1.0 — a fixed vertical pane never gets the full extraterrestrial
    dose the old shape treated as achievable.

    Cloud discount is `1 - 0.75 * cloud_fraction^3.4`, not linear: partly
    cloudy skies stay close to full brightness (diffuse light dominates well
    before overcast), and full overcast still passes about a quarter of clear
    sky rather than zero. Missing cloud data is treated as clear
    (`cloud_coverage_pct` is None -> 0% coverage), the same "assume clear"
    behaviour `compute()` already reported in its reason string.

    Extracted as a named function, rather than left inline in `compute()`, so
    the coordinator can reuse this exact formula — e.g. to solar-correct the
    baseline learner's samples — without a second copy that could drift out
    of sync.
    """
    if sun_elevation_deg <= 0.0:
        return 0.0
    elevation_rad = radians(sun_elevation_deg)
    azimuth_offset_rad = radians(sun_azimuth_deg - 180.0)
    incidence_factor = cos(elevation_rad) * cos(azimuth_offset_rad)
    if incidence_factor <= 0.0:
        return 0.0
    air_mass = 1.0 / sin(elevation_rad)
    transmittance = 0.7 ** (air_mass**0.678)
    aperture_factor = incidence_factor * transmittance
    cloud_fraction = (cloud_coverage_pct or 0.0) / 100.0
    cloud_factor = 1.0 - 0.75 * cloud_fraction**3.4
    return aperture_factor * cloud_factor


# --- Price comfort tiers ---------------------------------------------------
# The single knob most users touch: how much comfort to trade for cheaper
# energy. Named by aggressiveness — Low barely reacts (max comfort), High
# brakes hard (max saving).
#
#   max_sag_c       how far indoor may coast below target during a spike. Also
#                   the sag the recovery-feasibility taper is measured against.
#   gamma           convexity of the price->response curve (response = ramp**gamma).
#                   Above 1, small excursions barely register and only a real
#                   spike bites.
#   precharge_c     how far above target heat may be banked in a cheap window
#                   ahead of a spike. Zero disables pre-charging for that tier.
#
# Note what is NOT here any more: a lead time. How early to act is a property
# of the house's emitters, not of how hard the occupant wants to chase price,
# and it is now measured (lag.py) rather than tiered.
PRICE_TIER_LOW = "low"
PRICE_TIER_MID = "mid"
PRICE_TIER_HIGH = "high"


@dataclass(frozen=True)
class PriceTier:
    max_sag_c: float
    gamma: float
    precharge_c: float


PRICE_TIERS: dict[str, PriceTier] = {
    PRICE_TIER_LOW: PriceTier(max_sag_c=1.0, gamma=3.0, precharge_c=0.0),
    PRICE_TIER_MID: PriceTier(max_sag_c=2.0, gamma=2.0, precharge_c=0.0),
    PRICE_TIER_HIGH: PriceTier(max_sag_c=4.0, gamma=1.5, precharge_c=1.0),
}


def resolve_price_tier(name: str | None) -> PriceTier:
    """Map a tier name to its coefficients, defaulting to Mid on anything
    unrecognized so a bad value degrades gracefully rather than disabling
    compensation."""
    return PRICE_TIERS.get((name or "").strip().lower(), PRICE_TIERS[PRICE_TIER_MID])


# --- Cold caution ----------------------------------------------------------
# The one genuinely manual control over deep-cold behaviour, and deliberately
# manual: braking is cheap to enter and expensive to exit, because recovering
# a sag at -15 degC is slow and may run through resistive backup heat at a
# terrible coefficient of performance. That is a risk-appetite question about
# money versus comfort, not a measurable fact about the building, so it is the
# occupant's to answer.
#
#   floor_c      below this outdoor temperature, no price braking at all.
#   exponent     how sharply the measured recovery-feasibility factor bites.
#                Above 1 tapers harder, below 1 more gently.
COLD_CAUTION_LOW = "low"
COLD_CAUTION_MID = "mid"
COLD_CAUTION_HIGH = "high"

# Over how many degrees above the floor braking authority ramps back to full.
COLD_CAUTION_RAMP_C = 8.0


@dataclass(frozen=True)
class ColdCaution:
    floor_c: float
    exponent: float


COLD_CAUTIONS: dict[str, ColdCaution] = {
    COLD_CAUTION_LOW: ColdCaution(floor_c=-20.0, exponent=0.5),
    COLD_CAUTION_MID: ColdCaution(floor_c=-10.0, exponent=1.0),
    COLD_CAUTION_HIGH: ColdCaution(floor_c=-5.0, exponent=2.0),
}


def resolve_cold_caution(name: str | None) -> ColdCaution:
    return COLD_CAUTIONS.get(
        (name or "").strip().lower(), COLD_CAUTIONS[COLD_CAUTION_MID]
    )


def cold_brake_factor(
    outdoor_c: float,
    caution: ColdCaution,
    recoverable_sag_c: float,
    tier_max_sag_c: float,
) -> float:
    """How much price-braking authority survives the cold, in [0, 1].

    Two independent reductions, multiplied:

    1. The occupant's caution setting. Hard zero at or below `floor_c`, ramping
       linearly back to full over `COLD_CAUTION_RAMP_C` degrees above it. This
       works from the first cycle on a fresh install, with nothing learned.

    2. Measured recovery feasibility. Once the current outdoor bin has observed
       how fast it actually closes an error at high offset, the sag is limited
       to what can genuinely be bought back — the question the old three-knob
       hand-drawn taper was approximating. With no measurement yet this
       contributes 1.0, i.e. it does not taper on evidence it does not have,
       leaving the caution ramp in sole charge.
    """
    if outdoor_c <= caution.floor_c:
        return 0.0
    ramp = _clamp((outdoor_c - caution.floor_c) / COLD_CAUTION_RAMP_C, 0.0, 1.0)
    if recoverable_sag_c > 0.0 and tier_max_sag_c > 0.0:
        feasibility = _clamp(recoverable_sag_c / tier_max_sag_c, 0.0, 1.0)
    else:
        feasibility = 1.0
    return _clamp(ramp * (feasibility**caution.exponent), 0.0, 1.0)


# --- Forecast-relative price band ------------------------------------------
# Below this many forecast points there is no usable day-distribution.
_MIN_FORECAST_POINTS = 6
# The day must vary by at least this fraction (peak vs median, relative to the
# median) before any compensation engages. A flat day gets left alone.
#
# This ratio has exactly the two failure modes `price_significance()` below
# was built to fix (ranks money backwards, degenerates near a zero median),
# and is deliberately left as-is here rather than rewritten to use it:
# `engaged` is a hard boolean that `price_flat_day` and the reason string also
# key off, and swapping its logic was more churn than the actual bug needed.
# What changes instead is the failure mode's reach: a day that spuriously
# reads "engaged" off a near-zero median now still gets tapered to ~0
# authority by `price_significance()`'s absolute floor before it reaches
# `price_shift_c`, so this check's own degeneracy no longer reaches the
# published output — see `compute()`.
PRICE_MIN_RELATIVE_SPREAD = 0.2
# Where in the median->peak range braking starts. Below this the price is
# "ordinary for today" and the convex curve keeps the response near zero.
_PRICE_ENGAGE_FRACTION = 0.25


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (q / 100.0) * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _price_band_info(
    forecast: tuple[tuple[float, float], ...] | None,
) -> tuple[float, float, float, bool] | None:
    """The price band for this cycle: (engage, full authority, median, engaged).

    Derived from the day's own distribution — median is "ordinary for today",
    peak is full authority — so the response self-adapts across seasons and
    still catches short one- and two-hour spikes that a high percentile would
    miss. `engaged` is False on a flat day.

    Returns None when there is no usable forecast, which means "do nothing".
    That is deliberately not the same as the absolute-threshold fallback this
    replaced: without a day-distribution there is no principled way to know
    whether today's price is high, and guessing from two configured constants
    was both a config burden and a way to brake on an ordinary day.
    """
    if not forecast or len(forecast) < _MIN_FORECAST_POINTS:
        return None
    prices = sorted(p for _, p in forecast)
    p_med = _percentile(prices, 50)
    p_peak = prices[-1]  # the day's actual peak — catches short spikes
    spread = p_peak - p_med
    engaged = spread / max(abs(p_med), 1e-6) >= PRICE_MIN_RELATIVE_SPREAD
    return p_med + _PRICE_ENGAGE_FRACTION * spread, p_peak, p_med, engaged


def today_price_spread_and_median_c(
    forecast: tuple[tuple[float, float], ...] | None,
) -> tuple[float, float] | None:
    """Today's (peak-minus-median spread, median), or None with no usable
    day-distribution — the same forecast-quality gate `_price_band_info` uses,
    for the same reason (see its docstring).

    Deliberately shares `_price_band_info` rather than recomputing the
    median/peak separately, and deliberately returns BOTH numbers from one
    call rather than offering two single-purpose functions: `compute()`'s
    braking-band logic, the seasonal spread history, and the significance
    floor's auto mode (see `_resolve_absolute_floor_c`) must never read a
    different spread or median for the same cycle, and one function is what
    guarantees that rather than relying on independent implementations
    staying in sync by hand.
    """
    band = _price_band_info(forecast)
    if band is None:
        return None
    _, full, median, _ = band
    return max(0.0, full - median), median


# --- Price significance -----------------------------------------------------
# See the module docstring's "Price significance: a taper, not a gate" section
# for why this exists. Two pieces: a persisted rolling history of past days'
# spread and median price (below), and the pure scoring function that reads it
# (`price_significance`, further down).

# How many trailing days of daily spread/median to keep for the seasonal
# reference and the auto absolute floor. Long enough to smooth day-to-day
# noise and genuinely track winter vs summer; short enough that the reference
# actually moves as the season turns rather than lagging it by months.
PRICE_SPREAD_HISTORY_DAYS = 30

# Below this many STORED (i.e. completed) days there is not enough history to
# trust a seasonal median at all, so the relative term contributes NO damping
# — 1.0, meaning "let the absolute floor alone decide" — rather than blocking
# every saving for weeks on a fresh install while history accumulates. Only
# gates the RELATIVE term; the auto absolute floor has its own, looser
# cold-start rule (see `_resolve_absolute_floor_c`) because a single day's
# price level is already a reasonable scale reference, unlike a single day's
# spread being a reasonable variability reference.
PRICE_SIGNIFICANCE_COLD_START_DAYS = 5

# The auto absolute floor (CONF_PRICE_SIGNIFICANCE_FLOOR == 0) is this
# fraction of the long-run median daily price — see `_resolve_absolute_floor_c`.
PRICE_SIGNIFICANCE_AUTO_FRACTION = 0.33


@dataclass(frozen=True)
class PriceSpreadHistory:
    """Persisted rolling record of past days' price spread and price level —
    together, everything `price_significance()` needs beyond today's own
    numbers.

    `daily_spreads_c` holds up to `PRICE_SPREAD_HISTORY_DAYS` *completed*
    days, oldest first. Each entry is the running MAX peak-minus-median spread
    observed during that local date, not a single end-of-day sample: the band
    widens within a day as tomorrow's prices publish (typically ~13:00 for
    Nordpool) — in the reference install's real logs, one date's spread ran
    0.249 -> 1.139 SEK/kWh on the very same date — so the day's max is the
    fullest view of that day's distribution. An end-of-day snapshot would have
    systematically undercounted every day where the late-arriving tomorrow
    prices turned out to be the spike.

    `daily_medians_c` is the parallel record of each of those same completed
    days' median price — the LATEST value seen for that date, not a running
    max: unlike the spread, which genuinely grows as more of the day's true
    range is revealed, the median is a level estimate that a plain "last
    reading wins" is enough to track, and this is what the auto absolute
    floor (`_resolve_absolute_floor_c`) is built from.

    `current_date`/`current_date_max_spread_c`/`current_date_median_c` track
    the day still in progress; both move into the two tuples above on the next
    date rollover — see `update_price_spread_history`.
    """

    daily_spreads_c: tuple[float, ...] = ()
    daily_medians_c: tuple[float, ...] = ()
    current_date: str | None = None
    current_date_max_spread_c: float = 0.0
    current_date_median_c: float = 0.0


def initial_price_spread_history() -> PriceSpreadHistory:
    """Cold-start value: no history at all. `price_significance` falls back to
    today's own numbers alone (see `PRICE_SIGNIFICANCE_COLD_START_DAYS` and
    `_resolve_absolute_floor_c`) until enough days accumulate."""
    return PriceSpreadHistory()


def update_price_spread_history(
    history: PriceSpreadHistory,
    local_date: str,
    today_spread_c: float,
    today_median_c: float,
) -> PriceSpreadHistory:
    """Advance the rolling history by one cycle's observation.

    `local_date` is an opaque ISO date string (e.g. "2026-08-16"): this module
    stays free of any datetime dependency (see the module docstring), so
    working out what "today" means in the house's own timezone is the
    coordinator's job, not this function's — the same division of labour
    `HeuristicInputs.price_forecast`'s hours-from-now floats already use.

    Safe to call every cycle rather than once a day. A same-date call folds
    `today_spread_c` into the running max but simply OVERWRITES
    `current_date_median_c` with `today_median_c` (see `PriceSpreadHistory`'s
    docstring for why the two use different update rules) — a no-op,
    returning the exact same `history` object, only when neither actually
    changes, which lets the caller tell "nothing changed" from "state
    advanced" without a separate flag. A date change is detected by comparing
    to the stored `current_date` rather than requiring the caller to know when
    midnight passed.
    """
    if history.current_date == local_date:
        max_spread_c = max(today_spread_c, history.current_date_max_spread_c)
        if (
            max_spread_c == history.current_date_max_spread_c
            and today_median_c == history.current_date_median_c
        ):
            return history
        return PriceSpreadHistory(
            daily_spreads_c=history.daily_spreads_c,
            daily_medians_c=history.daily_medians_c,
            current_date=local_date,
            current_date_max_spread_c=max_spread_c,
            current_date_median_c=today_median_c,
        )
    # Either the very first observation ever (current_date is None, nothing to
    # bank) or a genuine date rollover (bank yesterday's completed values).
    banked_spreads = history.daily_spreads_c
    banked_medians = history.daily_medians_c
    if history.current_date is not None:
        banked_spreads = (banked_spreads + (history.current_date_max_spread_c,))[
            -PRICE_SPREAD_HISTORY_DAYS:
        ]
        banked_medians = (banked_medians + (history.current_date_median_c,))[
            -PRICE_SPREAD_HISTORY_DAYS:
        ]
    return PriceSpreadHistory(
        daily_spreads_c=banked_spreads,
        daily_medians_c=banked_medians,
        current_date=local_date,
        current_date_max_spread_c=today_spread_c,
        current_date_median_c=today_median_c,
    )


def _resolve_absolute_floor_c(
    floor_setting_c: float, today_median_c: float, history: PriceSpreadHistory
) -> float:
    """The absolute backstop floor actually in effect this cycle.

    `floor_setting_c` is `CONF_PRICE_SIGNIFICANCE_FLOOR` verbatim. 0 (the
    default) means AUTO: `PRICE_SIGNIFICANCE_AUTO_FRACTION` times the long-run
    median of stored daily median prices, falling back to that same fraction
    of TODAY's median while history is still empty so the floor is sensible
    from day one rather than 0 — which would make `absolute_significance`
    clamp to 1.0 for any nonzero spread, defeating the whole backstop on a
    fresh install.

    This is dimensionally self-consistent in ANY currency and ANY energy
    denominator (SEK/kWh, öre/kWh, EUR/MWh, ...) because the numerator (a
    price spread) and the reference (a price level) carry the same units —
    unlike a fixed constant, which would need a currency/denominator lookup
    table to mean the same thing twice. Sanity-checked against the reference
    install's real SE2 data: daily medians there run ~0.3 SEK/kWh, giving an
    auto floor of ~0.10 SEK/kWh, which is the same figure this project
    originally hand-calibrated before this function existed.

    Any explicit non-zero `floor_setting_c` overrides auto entirely and is
    used as-is, in the price sensor's own units — the money-anchored choice
    for an occupant who wants a fixed answer regardless of how prices are
    trending.

    Residual limitation, stated honestly rather than papered over: the auto
    floor scales WITH the price level, so during a persistently cheap stretch
    it scales down right along with prices and stops protecting against
    economically trivial variation — the exact failure mode it exists to
    guard against, just recurring at a lower price level. It is a
    sensible-SCALE default, not a money-correct one; an occupant who
    specifically cares about the persistently-cheap case should set an
    explicit floor instead of relying on auto.

    The reference median can be negative (routine for Nordpool in spring/
    summer) — `abs()` it so the floor stays a positive backstop instead of
    flipping sign and disabling itself.
    """
    if floor_setting_c != 0.0:
        return floor_setting_c
    if history.daily_medians_c:
        median_c = _percentile(sorted(history.daily_medians_c), 50)
    else:
        median_c = today_median_c
    return PRICE_SIGNIFICANCE_AUTO_FRACTION * abs(median_c)


def price_significance(
    today_spread_c: float,
    today_median_c: float,
    history: PriceSpreadHistory,
    floor_setting_c: float,
) -> tuple[float, float | None]:
    """Combined 0..1 taper on price-braking/pre-charge authority, and the
    seasonal reference spread it used (`None` during cold start, for display).

    Two independent terms, combined by taking the minimum — either one alone
    being low is enough to damp the response, since each is a distinct reason
    today's swing might not be worth reacting to:

    - `relative_significance`: today's spread against the MEDIAN (not mean —
      robust to a single spike day skewing the reference) of up to
      `PRICE_SPREAD_HISTORY_DAYS` stored daily spreads. This is what makes
      winter's larger typical spreads automatically raise the bar, and lets
      the reference drift across the year with no user action. Contributes
      1.0 (no damping at all) during cold start — see
      `PRICE_SIGNIFICANCE_COLD_START_DAYS` — so a fresh install is not blocked
      from saving anything while history accumulates.

    - `absolute_significance`: today's spread against the floor
      `_resolve_absolute_floor_c` resolves — either the one user-configured
      value, or an auto floor scaled off the local price level. This is what
      stops braking for economically meaningless variation during a
      persistently cheap stretch, where the relative term alone would keep
      re-normalising against a low seasonal reference and misfire again — the
      same failure the module docstring's field data describes for
      `_price_band_info`'s ratio, just with a moving reference instead of
      today's own median.
    """
    if len(history.daily_spreads_c) < PRICE_SIGNIFICANCE_COLD_START_DAYS:
        relative_significance = 1.0
        reference_spread_c = None
    else:
        reference_spread_c = _percentile(sorted(history.daily_spreads_c), 50)
        relative_significance = _clamp(
            today_spread_c / max(reference_spread_c, 1e-6), 0.0, 1.0
        )
    absolute_floor_c = _resolve_absolute_floor_c(floor_setting_c, today_median_c, history)
    absolute_significance = _clamp(
        today_spread_c / max(absolute_floor_c, 1e-6), 0.0, 1.0
    )
    return min(relative_significance, absolute_significance), reference_spread_c


def _band_response(price: float, start: float, full: float, gamma: float) -> float:
    """Convex 0..1 response of a price within the [start, full] band."""
    if full > start:
        ramp = (price - start) / (full - start)
    else:
        ramp = 1.0 if price >= start else 0.0
    return _clamp(ramp, 0.0, 1.0) ** gamma


def _lookahead_weighted_peak(
    series: tuple[tuple[float, float], ...] | None,
    lead_minutes: float,
    score: Callable[[float], float],
) -> tuple[float, float | None]:
    """Largest proximity-weighted `score` within the lead window, and when.

    The one scan shared by every lookahead in this module. `series` is
    `(hours_from_now, value)` pairs; entries at or before now, and beyond
    `lead_minutes`, are ignored. Each surviving entry's score is weighted by
    `1 - t/lead` so authority grows as the event approaches, and the peak
    wins.

    Deliberately scores in "bigger is more" space regardless of what the
    caller's term does, so there is exactly one comparison direction to reason
    about; callers that want a negative push negate the magnitude afterwards.
    """
    if not series:
        return 0.0, None
    lead_hours = lead_minutes / 60.0
    if lead_hours <= 0:
        return 0.0, None
    best = 0.0
    best_h: float | None = None
    for t_h, value in series:
        if t_h <= 0.0 or t_h > lead_hours:
            continue
        contrib = score(value) * (1.0 - t_h / lead_hours)
        if contrib > best:
            best = contrib
            best_h = t_h
    return best, best_h


def _wind_effect_c(wind_speed_ms: float) -> float:
    """Steady-state wind term, in degC of outdoor spoof (<= 0).

    Zero at or below WIND_DEADBAND_MS; linear in the excess above it. Shared
    by the instantaneous term and its pre-ramp so the two can never diverge —
    see `_term_preramp_c`.
    """
    return -WIND_GAIN_C_PER_MS * max(0.0, wind_speed_ms - WIND_DEADBAND_MS)


def _lookahead_response(
    forecast: tuple[tuple[float, float], ...] | None,
    start: float,
    full: float,
    gamma: float,
    lead_minutes: float,
) -> tuple[float, float | None]:
    """Pre-brake response from an upcoming spike, and how many hours until it.

    Scans the forecast within `lead_minutes` — the MEASURED fall time, i.e. how
    long this house keeps gaining heat after the pump stops calling for it —
    and weights each upcoming price by proximity. Braking that starts later
    than that is still pushing heat into the hour it was meant to avoid, which
    is exactly what the old fixed tier constants (30/90/150 min) did on any
    slab.
    """
    return _lookahead_weighted_peak(
        forecast,
        lead_minutes,
        lambda price: _band_response(price, start, full, gamma),
    )


def _term_preramp_c(
    series: tuple[tuple[float, float], ...] | None,
    to_term_c: Callable[[float], float],
    lead_minutes: float,
) -> tuple[float, float | None]:
    """One signal's pre-ramp, in degC of outdoor spoof (<= 0), and when it peaks.

    `to_term_c` maps a raw forecast value to the degC this module would
    already contribute for it — the forecast temperature itself for the
    outdoor term, `_wind_effect_c` for wind. So there is no new gain
    anywhere: the pre-ramp is the existing term evaluated at a future time.

    The "now" reference is the series' OWN first entry, never a live sensor
    reading. A forecast that runs a constant 2 degC below the wall sensor is
    completely normal, and differencing against the sensor would produce a
    permanent pre-ramp that never decays because the bias never goes away.
    An internal difference cancels any constant offset.

    One-sided by construction: only the part of the change asking for MORE
    heat survives. Easing off ahead of a forecast warm-up is a comfort
    sacrifice on a forecast that may be wrong, with no saving attached. Sun
    is deliberately not one of the signals fed through here any more — see
    `_term_precool_c` for why it gets the opposite treatment instead.
    """
    if not series:
        return 0.0, None
    now_term_c = to_term_c(series[0][1])
    magnitude, when_h = _lookahead_weighted_peak(
        series, lead_minutes, lambda v: max(0.0, now_term_c - to_term_c(v))
    )
    return -magnitude, when_h


def _term_precool_c(
    series: tuple[tuple[float, float], ...] | None,
    to_term_c: Callable[[float], float],
    lead_minutes: float,
) -> tuple[float, float | None]:
    """The sun term's pre-cool, in degC of outdoor spoof (>= 0), and when it peaks.

    Mirror image of `_term_preramp_c`, in both sign and lead time. Losing sun
    is already fully handled reactively: `sun_adjustment_c` recomputes from
    the live sun/cloud reading every cycle, so there is nothing to pre-empt
    when solar gain is about to drop — the reactive term will simply ask for
    more heat once it actually does, the same way it asks for less right now.
    Pre-ramping that loss in advance would duplicate a correction the reactive
    term already makes on its own.

    GAINING sun is the case worth acting on ahead of time, and in the
    opposite direction from every other pre-ramp here: if the heating output
    isn't backed off before the sun arrives, the heat already in the pipe
    stacks with the incoming solar gain and overshoots comfort. So this is
    timed off `lead_minutes` as the measured FALL time, not the rise time —
    the existing heat surplus needs to have had time to dissipate before the
    sun lands, not still be arriving (compare `_lookahead_response`, which
    times price pre-braking off the same fall time for the same reason).

    Same constant-bias cancellation as `_term_preramp_c`: the "now" reference
    is the series' own first entry, not a live reading.
    """
    if not series:
        return 0.0, None
    now_term_c = to_term_c(series[0][1])
    magnitude, when_h = _lookahead_weighted_peak(
        series, lead_minutes, lambda v: max(0.0, to_term_c(v) - now_term_c)
    )
    return magnitude, when_h


@dataclass(frozen=True)
class HeuristicInputs:
    """Live values gathered by the coordinator for one compute cycle."""

    indoor_temp_c: float | None
    indoor_data_available: bool
    raw_outdoor_temp_c: float
    wind_speed_ms: float
    wind_data_available: bool
    sun_elevation_deg: float
    sun_azimuth_deg: float
    cloud_coverage_pct: float | None
    cloud_data_available: bool
    current_price: float | None
    price_data_available: bool
    # Day-ahead price as (hours_from_now, price) pairs. Plain float offsets
    # rather than datetimes so this module stays free of any datetime or HA
    # dependency; the coordinator does the conversion.
    price_forecast: tuple[tuple[float, float], ...] | None = None
    # Forecast series for the weather lookahead, same `(hours_from_now, value)`
    # shape and same reason for it. `solar_forecast` carries the 0..1 output of
    # `solar_effect_of()` already evaluated at each future time, so this module
    # never has to know what a datetime or a sun position is. All None until
    # the coordinator supplies them, which is also what a weather integration
    # with no hourly forecast leaves them as.
    outdoor_forecast: tuple[tuple[float, float], ...] | None = None
    wind_forecast_ms: tuple[tuple[float, float], ...] | None = None
    solar_forecast: tuple[tuple[float, float], ...] | None = None
    # The learned offset from learner.py. This is the whole indoor-error
    # response — there is no separate proportional gain here any more.
    learned_offset_c: float = 0.0
    # Measured fall time (lag.py), in minutes: how long this house keeps
    # gaining heat after the pump backs off. Sets the pre-brake lead.
    fall_minutes: float = 120.0
    # Measured rise time, in minutes. Sets the pre-charge lead — banked heat
    # must have ARRIVED before the spike, not still be on its way.
    rise_minutes: float = 90.0
    # How much sag the current outdoor bin has been observed to buy back within
    # the recovery window. 0.0 means "no measurement yet".
    recoverable_sag_c: float = 0.0
    # Combined 0..1 taper from `price_significance()`, derived by the
    # coordinator from the persisted `PriceSpreadHistory` before this cycle's
    # `compute()` call — see the module docstring's "Price significance: a
    # taper, not a gate" section. 1.0 means no damping. `today_price_spread_c`
    # and `seasonal_reference_spread_c` are threaded through purely so
    # `compute()` can echo them onto `HeuristicResult` for the same reason
    # `price_band_start`/`price_median` are published: troubleshooting why the
    # published number is what it is.
    price_significance_factor: float = 1.0
    today_price_spread_c: float | None = None
    seasonal_reference_spread_c: float | None = None
    # Last cycle's `HeuristicResult.heating_hard_limit_engaged`, for
    # `resolve_heating_hard_limit_engaged`'s hysteresis. Threaded in by the
    # coordinator the same way `previous.price_braking` is; see that field's
    # docstring in coordinator.py.
    prev_heating_hard_limit_engaged: bool = False


@dataclass(frozen=True)
class HeuristicParams:
    """Occupant preferences and resolved settings for one cycle.

    Every field here is either something the occupant chose or something the
    coordinator derived. None of it is a control gain: there are no k_* values
    left to type in.
    """

    indoor_target_c: float
    # Absolute indoor floor for price compensation in an OCCUPIED house — the
    # only comfort bound left, and it applies to nothing else. Not a floor
    # for `indoor_target_c` itself: during holiday setback, `indoor_target_c`
    # can legitimately arrive already below this (see `compute()`'s
    # `floor_c`), and braking must not drag it back up to this value.
    comfort_min_c: float
    enable_price_compensation: bool
    price_comfort_tier: str = PRICE_TIER_MID
    cold_caution: str = COLD_CAUTION_MID
    enable_solar_input: bool = True
    enable_wind_input: bool = True
    # Whether to act on the weather forecast before the change lands. Covers
    # all three pre-ramps together: "should it act on forecasts at all" is one
    # occupant preference, not three. The wind and sun shares are additionally
    # gated on their own input toggles above.
    enable_weather_lookahead: bool = False
    # Degrees of outdoor spoof per degree of steady indoor change, supplied by
    # the coordinator from learner.SPOOF_PER_INDOOR_C so the constant is
    # single-sourced without this module importing a sibling.
    spoof_per_indoor_c: float = 1.0
    # The occupant's literal configured target — what `climate.target_temperature`
    # reports. `indoor_target_c` above is already vacation-effective; the two
    # intentionally diverge while a plan is sagging the house. Carried through
    # to `HeuristicResult` so a replay/debug session doesn't have to guess
    # which "indoor target" it's looking at.
    user_indoor_target_c: float = 0.0


@dataclass(frozen=True)
class HeuristicResult:
    """The full explainable output. Doubles as the sensor's attribute schema."""

    compensated_outdoor_temp_c: float
    raw_outdoor_temp_c: float
    indoor_temp_c: float | None
    indoor_data_available: bool
    indoor_target_c: float
    effective_indoor_target_c: float
    learned_offset_c: float
    wind_adjustment_c: float
    sun_adjustment_c: float
    price_adjustment_c: float
    wind_speed_ms: float
    wind_data_available: bool
    cloud_coverage_pct: float | None
    cloud_data_available: bool
    solar_effect: float
    current_price: float | None
    price_shift_applied_c: float
    price_data_available: bool
    heating_hard_limit_engaged: bool
    reason: str
    price_comfort_tier: str = PRICE_TIER_MID
    cold_caution: str = COLD_CAUTION_MID
    price_response: float = 0.0
    cold_brake_factor: float = 1.0
    allowed_sag_c: float = 0.0
    # Feedforward-only push added on top of `price_adjustment_c` while indoor
    # hasn't yet reached the target sag/precharge — see `PRICE_CATCHUP_GAIN`'s
    # docstring. Zero once indoor gets there, or whenever price isn't braking.
    price_catchup_c: float = 0.0
    upcoming_spike_in_min: float | None = None
    precharge_active: bool = False
    # True while price compensation is deliberately holding the house away from
    # target. Read by the learner, which must not treat that as error to be
    # integrated away.
    price_braking: bool = False
    lead_minutes_effective: float = 0.0
    price_band_start: float | None = None
    price_band_full: float | None = None
    price_median: float | None = None
    # The same band `price_band_start`/`price_band_full` would hold, but never
    # gated behind `enable_price_compensation` — for a display (e.g. the
    # card's price graph) that wants to colour hours by the real brake
    # thresholds regardless of whether braking is switched on, the way
    # `current_price`/`price_median` already do. `price_band_start`/`_full`
    # stay gated: those mean "braking is engaged here", which is false with
    # compensation off no matter what the math says.
    price_forecast_band_start: float | None = None
    price_forecast_band_full: float | None = None
    # Echoed straight from `HeuristicInputs` — see that dataclass's docstring
    # for what each means and why they live there rather than being
    # recomputed here.
    price_significance_factor: float = 1.0
    today_price_spread_c: float | None = None
    seasonal_reference_spread_c: float | None = None
    # Weather lookahead. Each component is reported separately (unclamped) so a
    # user can see which signal drove the total; `weather_preramp_c` is the sum
    # after the shared `WEATHER_PRERAMP_MAX_C` budget. Sun is not part of this
    # bucket — see `sun_precool_c` below.
    outdoor_preramp_c: float = 0.0
    wind_preramp_c: float = 0.0
    weather_preramp_c: float = 0.0
    weather_preramp_in_min: float | None = None
    # True while the pre-ramp is deliberately holding the house above target.
    # Read by the learner, for the same reason `price_braking` is.
    weather_preramp_active: bool = False
    # Sun's own lookahead: backs heat off ahead of MORE sun arriving, the
    # mirror image of weather_preramp_c above. See `_term_precool_c`.
    sun_precool_c: float = 0.0
    sun_precool_in_min: float | None = None
    # True while the pre-cool is deliberately holding the house below target.
    # Read by the learner, for the same reason `price_braking` is.
    sun_precool_active: bool = False
    model_version: str = MODEL_VERSION
    # See `HeuristicParams.user_indoor_target_c`.
    user_indoor_target_c: float = 0.0


def compute(inputs: HeuristicInputs, params: HeuristicParams) -> HeuristicResult:
    """Compose the compensated outdoor temperature and its explanation."""
    # Solar effect is a physical fact, not a control decision, so it is computed
    # unconditionally — before the hard-limit branch — because the learner and
    # the logs want reality rather than a zeroed value on warm days.
    solar_effect = solar_effect_of(
        inputs.sun_elevation_deg, inputs.sun_azimuth_deg, inputs.cloud_coverage_pct
    )

    if resolve_heating_hard_limit_engaged(
        inputs.raw_outdoor_temp_c, inputs.prev_heating_hard_limit_engaged
    ):
        # Hard limit: force the published value to the system's warmest
        # possible reading rather than merely passing raw through, so no
        # combination of learned offset, wind, sun or price can still leave
        # the pump calling for a trickle of heat this close to (or above) the
        # outdoor temperature it is supposedly holding. No partial credit.
        return HeuristicResult(
            compensated_outdoor_temp_c=OUTPUT_SANITY_MAX_C,
            raw_outdoor_temp_c=inputs.raw_outdoor_temp_c,
            indoor_temp_c=inputs.indoor_temp_c,
            indoor_data_available=inputs.indoor_data_available,
            indoor_target_c=params.indoor_target_c,
            effective_indoor_target_c=params.indoor_target_c,
            user_indoor_target_c=params.user_indoor_target_c,
            learned_offset_c=inputs.learned_offset_c,
            wind_adjustment_c=0.0,
            sun_adjustment_c=0.0,
            price_adjustment_c=0.0,
            wind_speed_ms=inputs.wind_speed_ms,
            wind_data_available=inputs.wind_data_available,
            cloud_coverage_pct=inputs.cloud_coverage_pct,
            cloud_data_available=inputs.cloud_data_available,
            solar_effect=solar_effect,
            # Informational, like the normal path below: a house above the
            # hard limit can still ask what power costs right now.
            current_price=inputs.current_price if inputs.price_data_available else None,
            price_shift_applied_c=0.0,
            price_data_available=inputs.price_data_available,
            heating_hard_limit_engaged=True,
            price_comfort_tier=params.price_comfort_tier,
            cold_caution=params.cold_caution,
            # Also informational and also unaffected by the hard limit, same as
            # current_price above.
            price_significance_factor=inputs.price_significance_factor,
            today_price_spread_c=inputs.today_price_spread_c,
            seasonal_reference_spread_c=inputs.seasonal_reference_spread_c,
            reason=(
                f"Raw outdoor {inputs.raw_outdoor_temp_c:.1f}°C at or above the "
                f"{HEATING_HARD_LIMIT_C:.1f}°C hard limit; publishing "
                f"{OUTPUT_SANITY_MAX_C:.1f}°C to guarantee no heat call"
            ),
        )

    # A disabled input contributes exactly 0, identically to an unavailable
    # sensor — the term is off, not merely small.
    wind_adjustment_c = (
        _wind_effect_c(inputs.wind_speed_ms) if params.enable_wind_input else 0.0
    )
    sun_adjustment_c = SOLAR_GAIN_C * solar_effect if params.enable_solar_input else 0.0

    # The raw price feed is informational and never gated behind the
    # compensation toggle — a house with braking switched off can still ask
    # "what's it cost right now?". `price_for_braking` is the one that feeds
    # the response math below, and stays strictly gated: that's the value
    # that would actually move the published temperature.
    current_price = inputs.current_price if inputs.price_data_available else None
    price_for_braking = current_price if params.enable_price_compensation else None

    tier = resolve_price_tier(params.price_comfort_tier)
    caution = resolve_cold_caution(params.cold_caution)
    taper = cold_brake_factor(
        inputs.raw_outdoor_temp_c, caution, inputs.recoverable_sag_c, tier.max_sag_c
    )

    price_response = 0.0
    price_shift_c = 0.0
    upcoming_spike_in_min: float | None = None
    precharge_active = False
    price_band_start: float | None = None
    price_band_full: float | None = None
    price_median: float | None = None
    price_forecast_band_start: float | None = None
    price_forecast_band_full: float | None = None
    price_flat_day = False
    no_forecast = False
    # Pre-braking is timed off the fall time and pre-charging off the rise
    # time: they are asking different questions of the same plant. Only half
    # of the fall lag is used: what costs money is the compressor still
    # drawing power, not the slab coasting on stored heat afterward, and the
    # measured fall lag conflates both. Halving approximates the compressor
    # cutting out partway through that window without a separate measurement
    # of it.
    lead_minutes = max(0.0, inputs.fall_minutes * 0.5)
    precharge_lead_minutes = max(0.0, inputs.rise_minutes)

    if current_price is not None:
        band = _price_band_info(inputs.price_forecast)
        if band is None:
            no_forecast = True
        else:
            start, full, price_median, band_engaged = band
            price_forecast_band_start, price_forecast_band_full = start, full
            # Everything past this point is braking behaviour, not display —
            # stays gated on price_for_braking so a house with compensation
            # off gets the median for free but never a brake threshold.
            if price_for_braking is not None:
                price_band_start, price_band_full = start, full
                price_flat_day = not band_engaged
                if band_engaged:
                    response_now = _band_response(price_for_braking, start, full, tier.gamma)
                    response_brake, brake_h = _lookahead_response(
                        inputs.price_forecast, start, full, tier.gamma, lead_minutes
                    )
                    response_charge, charge_h = _lookahead_response(
                        inputs.price_forecast,
                        start,
                        full,
                        tier.gamma,
                        precharge_lead_minutes,
                    )

                    # Pre-charge and braking are mutually exclusive, chosen by
                    # the CURRENT price: if it is genuinely cheap now (at or
                    # below the day's median) with a spike coming and the tier
                    # opts in, bank heat instead of cutting it. Pre-braking
                    # into a spike while the price is still cheap would just
                    # make the house cold for no saving.
                    precharge_ready = (
                        tier.precharge_c > 0.0
                        and response_charge > 0.0
                        and price_for_braking <= price_median
                    )
                    if precharge_ready:
                        price_shift_c = -tier.precharge_c * response_charge
                        precharge_active = True
                        upcoming_spike_in_min = (
                            None if charge_h is None else charge_h * 60.0
                        )
                    else:
                        if response_brake > response_now:
                            price_response = response_brake
                            upcoming_spike_in_min = (
                                None if brake_h is None else brake_h * 60.0
                            )
                        else:
                            price_response = response_now
                        price_shift_c = price_response * tier.max_sag_c * taper

                    # Significance taper — see the module docstring's "Price
                    # significance: a taper, not a gate" section. Applied here,
                    # after the precharge/brake if/else rather than inside
                    # either branch, so it damps WHICHEVER one just ran:
                    # banking heat for a two-öre swing is exactly as pointless
                    # as braking for one, and a single multiply after the
                    # branch is what guarantees neither path can be missed by
                    # a future change to one of them.
                    price_shift_c *= inputs.price_significance_factor

    # The comfort floor is the only bound applied here. There is no upper
    # clamp: pre-charging is already limited by the tier's own boost, so a
    # second bound above never bound anything.
    #
    # The floor itself is `min(comfort_min_c, indoor_target_c)`, not
    # `comfort_min_c` alone: `indoor_target_c` can already sit below
    # `comfort_min_c` when it came from holiday setback (see coordinator.py
    # and holiday.HOLIDAY_TARGET_MIN_C), which is a deliberate, separate,
    # colder floor for an unoccupied house. Using `comfort_min_c` unqualified
    # here would silently drag that intentionally-low holiday target back up
    # to the occupied-house floor. Braking still cannot push the target any
    # LOWER than what was already asked for.
    floor_c = min(params.comfort_min_c, params.indoor_target_c)
    effective_indoor_target_c = params.indoor_target_c - price_shift_c
    if effective_indoor_target_c < floor_c:
        effective_indoor_target_c = floor_c
    # Recompute from the clamped target so the reported shift is what is really
    # being asked for, not what was asked for before the floor bit.
    applied_shift_c = params.indoor_target_c - effective_indoor_target_c
    price_adjustment_c = applied_shift_c * params.spoof_per_indoor_c

    # Catch-up: push harder than the steady-state shift while indoor hasn't
    # sagged/precharged as far as `applied_shift_c` intends yet, so the target
    # sag is actually reached before the spike passes rather than merely being
    # asked for. Gated on `applied_shift_c`'s own sign so it only ever pushes
    # further IN the direction already chosen above, never against it — a
    # remaining gap of 0 or the wrong sign (already there, or overshot) yields
    # zero, so this never adds a second, independent excursion of its own.
    price_catchup_c = 0.0
    if (
        applied_shift_c != 0.0
        and inputs.indoor_data_available
        and inputs.indoor_temp_c is not None
    ):
        actual_shift_so_far_c = params.indoor_target_c - inputs.indoor_temp_c
        remaining_c = applied_shift_c - actual_shift_so_far_c
        if applied_shift_c > 0.0:
            price_catchup_c = _clamp(
                PRICE_CATCHUP_GAIN * remaining_c, 0.0, PRICE_CATCHUP_MAX_C
            )
        else:
            price_catchup_c = _clamp(
                PRICE_CATCHUP_GAIN * remaining_c, -PRICE_CATCHUP_MAX_C, 0.0
            )
    price_adjustment_c += price_catchup_c * params.spoof_per_indoor_c

    # Weather lookahead: act on a cold front before it lands, the same way the
    # price logic above acts on a spike before it lands. Timed off the RISE
    # time, not the fall time — this is a pre-charge, and banked heat must have
    # arrived before the change, not still be on its way. Sun is deliberately
    # not part of this bucket — see sun_precool_c below.
    outdoor_preramp_c = 0.0
    wind_preramp_c = 0.0
    weather_preramp_c = 0.0
    weather_preramp_in_min: float | None = None
    preramp_lead_minutes = max(0.0, inputs.rise_minutes)
    # Sun's own lookahead, opposite sign and opposite lead: back off ahead of
    # MORE sun arriving, timed off the FALL time so today's heat surplus has
    # dissipated before the gain lands rather than stacking with it. See
    # _term_precool_c.
    sun_precool_c = 0.0
    sun_precool_in_min: float | None = None
    precool_lead_minutes = max(0.0, inputs.fall_minutes)
    if params.enable_weather_lookahead:
        outdoor_preramp_c, outdoor_h = _term_preramp_c(
            inputs.outdoor_forecast, lambda v: v, preramp_lead_minutes
        )
        wind_h: float | None = None
        # A disabled input contributes exactly 0 to the lookahead too.
        if params.enable_wind_input:
            wind_preramp_c, wind_h = _term_preramp_c(
                inputs.wind_forecast_ms,
                _wind_effect_c,
                preramp_lead_minutes,
            )
        weather_preramp_c = _clamp(
            outdoor_preramp_c + wind_preramp_c,
            -WEATHER_PRERAMP_MAX_C,
            0.0,
        )
        # Report the timing of whichever signal is driving hardest.
        dominant = max(
            ((outdoor_preramp_c, outdoor_h), (wind_preramp_c, wind_h)),
            key=lambda pair: -pair[0],
        )
        weather_preramp_in_min = None if dominant[1] is None else dominant[1] * 60.0

        if params.enable_solar_input:
            sun_precool_raw_c, sun_h = _term_precool_c(
                inputs.solar_forecast,
                lambda v: SOLAR_GAIN_C * v,
                precool_lead_minutes,
            )
            sun_precool_c = _clamp(sun_precool_raw_c, 0.0, SOLAR_PRECOOL_MAX_C)
            sun_precool_in_min = None if sun_h is None else sun_h * 60.0

    compensated_outdoor_temp_c = _clamp(
        inputs.raw_outdoor_temp_c
        + inputs.learned_offset_c
        + price_adjustment_c
        + wind_adjustment_c
        + sun_adjustment_c
        + weather_preramp_c
        + sun_precool_c,
        OUTPUT_SANITY_MIN_C,
        OUTPUT_SANITY_MAX_C,
    )

    if inputs.indoor_data_available and inputs.indoor_temp_c is not None:
        reason = (
            f"Indoor {inputs.indoor_temp_c:.1f}°C vs target "
            f"{params.indoor_target_c:.1f}°C → learned offset "
            f"{inputs.learned_offset_c:+.2f}°C; "
        )
    else:
        reason = (
            f"Indoor sensor unavailable, holding learned offset "
            f"{inputs.learned_offset_c:+.2f}°C; "
        )
    if not params.enable_wind_input:
        reason += "wind off; "
    elif inputs.wind_data_available:
        reason += f"wind {inputs.wind_speed_ms:.1f} m/s → {wind_adjustment_c:+.1f}°C; "
    else:
        reason += "wind forecast unavailable, treated as calm; "
    if not params.enable_solar_input:
        reason += "solar off"
    elif inputs.cloud_data_available:
        reason += f"sun {solar_effect * 100:.0f}% → {sun_adjustment_c:+.1f}°C"
    else:
        reason += f"cloud/sun forecast unavailable, assumed clear → {sun_adjustment_c:+.1f}°C"
    if abs(weather_preramp_c) > WEATHER_PRERAMP_EPS_C:
        reason += f"; pre-ramp {weather_preramp_c:+.1f}°C"
        if weather_preramp_in_min is not None:
            reason += f" for weather change in {weather_preramp_in_min:.0f} min"
        reason += (
            f" (outdoor {outdoor_preramp_c:+.1f}, wind {wind_preramp_c:+.1f}; "
            f"{preramp_lead_minutes:.0f} min rise)"
        )
    if abs(sun_precool_c) > WEATHER_PRERAMP_EPS_C:
        reason += f"; pre-cool {sun_precool_c:+.1f}°C"
        if sun_precool_in_min is not None:
            reason += f" for more sun in {sun_precool_in_min:.0f} min"
        reason += f" ({precool_lead_minutes:.0f} min fall)"
    if price_for_braking is not None:
        reason += f"; price {price_for_braking:.2f} ['{params.price_comfort_tier}' tier"
        if no_forecast:
            reason += ", no day-ahead forecast → no price action"
        if price_band_start is not None and price_band_full is not None:
            reason += f", band {price_band_start:.2f}–{price_band_full:.2f}"
        if price_flat_day:
            reason += ", flat day → no price action"
        if taper < 1.0 and not no_forecast and not price_flat_day:
            if taper <= 0.0:
                reason += (
                    f", too cold to brake ('{params.cold_caution}' cold caution)"
                )
            else:
                reason += f", cold caution ×{taper:.2f}"
        if (
            inputs.price_significance_factor < 1.0
            and not no_forecast
            and not price_flat_day
        ):
            reason += f", low significance ×{inputs.price_significance_factor:.2f}"
        if price_catchup_c != 0.0:
            reason += f", catch-up {price_catchup_c:+.1f}°C until sag reached"
        if precharge_active:
            reason += (
                f", pre-charging ahead of spike in {upcoming_spike_in_min:.0f} min "
                f"({precharge_lead_minutes:.0f} min rise)"
            )
        elif upcoming_spike_in_min is not None:
            reason += (
                f", pre-braking for spike in {upcoming_spike_in_min:.0f} min "
                f"({lead_minutes:.0f} min fall)"
            )
        reason += (
            f"] → target {effective_indoor_target_c:.1f}°C → {price_adjustment_c:+.1f}°C"
        )
    reason += (
        f"; total {compensated_outdoor_temp_c - inputs.raw_outdoor_temp_c:+.1f}°C "
        f"from raw {inputs.raw_outdoor_temp_c:.1f}°C"
    )

    return HeuristicResult(
        compensated_outdoor_temp_c=compensated_outdoor_temp_c,
        raw_outdoor_temp_c=inputs.raw_outdoor_temp_c,
        indoor_temp_c=inputs.indoor_temp_c,
        indoor_data_available=inputs.indoor_data_available,
        indoor_target_c=params.indoor_target_c,
        effective_indoor_target_c=effective_indoor_target_c,
        user_indoor_target_c=params.user_indoor_target_c,
        learned_offset_c=inputs.learned_offset_c,
        wind_adjustment_c=wind_adjustment_c,
        sun_adjustment_c=sun_adjustment_c,
        price_adjustment_c=price_adjustment_c,
        price_catchup_c=price_catchup_c,
        wind_speed_ms=inputs.wind_speed_ms,
        wind_data_available=inputs.wind_data_available,
        cloud_coverage_pct=inputs.cloud_coverage_pct,
        cloud_data_available=inputs.cloud_data_available,
        solar_effect=solar_effect,
        current_price=current_price,
        price_shift_applied_c=applied_shift_c,
        price_data_available=inputs.price_data_available,
        heating_hard_limit_engaged=False,
        reason=reason,
        price_comfort_tier=params.price_comfort_tier,
        cold_caution=params.cold_caution,
        price_response=price_response,
        cold_brake_factor=taper,
        allowed_sag_c=tier.max_sag_c * taper,
        upcoming_spike_in_min=upcoming_spike_in_min,
        precharge_active=precharge_active,
        price_braking=abs(applied_shift_c) > PRICE_BRAKING_EPS_C,
        lead_minutes_effective=lead_minutes,
        price_band_start=price_band_start,
        price_band_full=price_band_full,
        price_median=price_median,
        price_forecast_band_start=price_forecast_band_start,
        price_forecast_band_full=price_forecast_band_full,
        price_significance_factor=inputs.price_significance_factor,
        today_price_spread_c=inputs.today_price_spread_c,
        seasonal_reference_spread_c=inputs.seasonal_reference_spread_c,
        outdoor_preramp_c=outdoor_preramp_c,
        wind_preramp_c=wind_preramp_c,
        weather_preramp_c=weather_preramp_c,
        weather_preramp_in_min=weather_preramp_in_min,
        weather_preramp_active=abs(weather_preramp_c) > WEATHER_PRERAMP_EPS_C,
        sun_precool_c=sun_precool_c,
        sun_precool_in_min=sun_precool_in_min,
        sun_precool_active=abs(sun_precool_c) > WEATHER_PRERAMP_EPS_C,
    )


def heat_curve_offset_c(
    compensated_outdoor_temp_c: float, raw_outdoor_temp_c: float, invert: bool
) -> int:
    """Convert the published spoof into a value for a pump's native heat
    curve offset ("värmekurvans förskjutning"), for pumps that expose one
    directly instead of needing a faked outdoor sensor.

    Sign convention differs by mechanism. This codebase's own offset is added
    to a spoofed OUTDOOR reading, where colder (more negative) means more heat
    — see learner.py's "Sign convention". Most pumps' native curve offset
    works the other way round, applied straight to the flow-temperature
    calculation: positive means warmer supply and more heat. `invert=False`
    (the common case) flips the sign to match that; `invert=True` is for the
    pumps that don't.

    Rounded to a whole number: this parameter is a small integer dial on
    every pump this has been checked against (e.g. NIBE's -10..+10), never a
    decimal one, and pushing a fraction risks the entity silently rejecting
    or truncating it rather than landing on the intended step.
    """
    delta_c = compensated_outdoor_temp_c - raw_outdoor_temp_c
    return round(delta_c if invert else -delta_c)


def indoor_climate_offset_c(
    compensated_outdoor_temp_c: float,
    raw_outdoor_temp_c: float,
    spoof_per_indoor_c: float = 1.0,
) -> float:
    """The delta to add to an indoor climate entity's own target temperature,
    for pumps (or TRVs) that expose a room-sensor climate entity to steer
    instead of a spoofed outdoor sensor or a native curve-offset dial.

    Same *outdoor-spoof* magnitude as `heat_curve_offset_c(..., invert=False)`,
    but converted from degrees of outdoor spoof to degrees of indoor target via
    `spoof_per_indoor_c` (see `HeuristicParams.spoof_per_indoor_c`) before
    returning, since this output lands on an indoor-facing entity, not an
    outdoor-facing one — the two only happen to be numerically identical while
    that constant is 1.0. Never rounded to a whole number: a climate entity's
    target step (commonly 0.1-0.5 degC) is finer than a curve-offset dial's
    integer one. There is also no invert flag here — raising a climate
    entity's target always means "call for more heat", the same universal
    direction `invert=False` already matches, so unlike a pump's proprietary
    curve dial there is no per-pump convention to flip.
    """
    return (raw_outdoor_temp_c - compensated_outdoor_temp_c) / spoof_per_indoor_c


# The delta `heat_curve_offset_c` would compute while the hard limit is
# engaged, using the THRESHOLD rather than the actual (fluctuating) raw
# reading. During the hard limit `compensated_outdoor_temp_c` is pinned to
# `OUTPUT_SANITY_MAX_C` regardless of raw — a stable absolute value in
# outdoor-spoof output mode, which pushes that absolute number straight to the
# device. But `heat_curve_offset_c` publishes a DELTA against raw, so feeding
# it the fixed ceiling alongside a raw reading that keeps drifting with
# ordinary weather noise reintroduces exactly the flapping the hard limit's
# hysteresis was meant to prevent — e.g. raw idling between 20.0°C and 21.6°C
# pushed the offset entity between +5 and +3 every cycle. Using the threshold
# instead of raw keeps the pushed value fixed for as long as the hard limit
# holds, and it is never smaller in magnitude than what raw would have given
# (raw is at or above `HEATING_HARD_LIMIT_C` whenever this applies), so it is
# still guaranteed to be warm enough to prevent a heat call.
HEATING_HARD_LIMIT_OFFSET_C = OUTPUT_SANITY_MAX_C - HEATING_HARD_LIMIT_C


def heating_hard_limit_offset_c(invert: bool) -> int:
    """The fixed heat-curve-offset value to push while the hard limit is
    engaged. See `HEATING_HARD_LIMIT_OFFSET_C` for why this does not simply
    call `heat_curve_offset_c` with the live raw reading."""
    return round(HEATING_HARD_LIMIT_OFFSET_C if invert else -HEATING_HARD_LIMIT_OFFSET_C)
