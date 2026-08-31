"""The offset learner: what spoof this house needs, learned from living in it.

Pure module: standard library only, no dependency on any other module in this
package, so it is importable and unit-testable without Home Assistant
installed.

## What replaced what

This module replaces three: a grey-box RC estimator (`rc_model.py`), an MPC
planner built on top of it (`mpc.py`), and a derivation that inverted the
estimator into control gains (`autotune.py`). That stack asked "what are this
building's physical parameters", which turned out not to be answerable from the
available data — validated 2026-08-07 against 1,193 real records, batch
R^2 = 0.029, worse than predicting "nothing changes".

This module asks a much smaller question, and the whole design follows from it:

    what offset holds this house at target, right now?

Answered by integral action. An integrator accumulates the spoof needed to hold
target, and its accumulated state is binned by outdoor temperature.

Three properties fall out of that choice, and they are why it works where
identification did not:

  * Integrating a 0.1 degC-resolution error over hours is exactly what
    integrators are good at. Differentiating it one sample at a time — what a
    per-sample regression does — is what destroyed the old fit.
  * It learns from ordinary closed-loop operation. There is no excitation
    requirement, so no "the heat-pump gain has never been excited" blocker, no
    lazy dimension expansion, no trust gate, no confidence threshold.
  * Binning by outdoor temperature learns the pump's non-linear heat curve
    directly. One degree of spoofing buys different amounts of heat at -15 degC
    than at +5 degC, and each bin simply measures its own. That was the entire
    original motivation for dividing control gains by an estimated heat-pump
    gain, obtained here from data that actually converges.

The model is one sentence: *at -5 degC this house needs -2.3 degC of spoofing
to hold 21 degC.*

## Sign convention

`offset_c` is added to the outdoor temperature the heat pump reads. Spoofing
COLDER (a more negative offset) makes the pump's own weather curve call for
MORE heat. So a house below target drives the offset down.

## Two regimes, and why the off one is not wasted

Integral action can only learn while its output is actually applied — it has to
observe the response to its own action, which is why `_freeze_reason` stops the
integrator dead whenever compensation is off. That used to mean an install sat
learning nothing until somebody flipped the switch, which matters more now that
compensation ships OFF by default.

But "cannot run the integrator" is not the same as "cannot observe anything".
With compensation off the pump runs its own weather curve untouched — the
coordinator publishes the raw outdoor temperature to every output channel, with
no price, wind or solar term added — so the house settles wherever that curve
puts it. Where it settles is exactly the open-loop fact the integrator would
otherwise spend days rediscovering.

So the two regimes are complementary, and each learns the thing the other
cannot:

    compensation ON   integral action     what offset holds target
    compensation OFF  baseline observation where the raw curve leaves the house

The baseline table records the *equilibrium indoor temperature* per outdoor
bin, deliberately NOT the deviation from target. Equilibrium indoor is a
property of the house, the curve and the weather; deviation from target is not,
because moving the target changes it without anything about the house having
changed. Storing the absolute temperature keeps the table valid across target
changes, and the conversion to an offset happens at seeding time against
whatever the target is then:

    seed = -(target_c - baseline_indoor_c) * SPOOF_PER_INDOOR_C

which is the same sign convention as the integrator's own update: a house the
raw curve leaves below target seeds a negative (colder-spoofing) offset.

Binning by outdoor temperature alone still leaves the equilibrium contaminated
by weather this module cannot see: a -5 degC bin filled by both sunny
afternoons and overcast nights blends a house coasting on solar gain with one
purely on the pump. `LearnerInputs.estimated_solar_gain_c` is the coordinator's
correction for that — computed with the same SOLAR_GAIN_C the feedforward term
uses, so the bin isolates the pump curve's own behaviour from the weather's.
This module treats it as an opaque float and applies it only to the baseline
path; see `_observe_baseline`.

Seeding fires on the off->on transition and only fills bins with no closed-loop
evidence yet — a measured `samples > 0` bin is strictly better information and
is never overwritten. Crucially, seeding does NOT advance `total_samples`: the
authority ramp is a safety property that asks "how much evidence is there that
*our own actions* work", which open-loop observation cannot answer. So a seeded
install starts from a far better guess but still ramps its authority over the
same three days.

## Deep cold, and why a bin carries four numbers

Below some outdoor temperature the pump is flat out and additional spoofing
buys nothing. A naive integrator keeps winding anyway — the house sits
stable-but-cold rather than falling, so a "freeze when falling" disturbance
guard never trips — and when the weather warms, that stored windup dumps into a
pump that can suddenly deliver it.

So each bin also learns a `ceiling_c`: the offset magnitude beyond which more
spoofing stopped helping *here*. Integration stops at the bin's own ceiling
rather than at the global authority clamp. The ceiling caps at the current
offset rather than backing off below it — saturation means more does not help,
not that less is wanted — and recovers slowly when the bin is later observed
closing its error at high offset. It also approximates where auxiliary
resistive heat engages, which reads as a capacity discontinuity; this
integration deliberately never reads pump internals, so that is the closest
observable proxy available.

`close_rate_c_per_h` records how fast the error actually closed at high offset,
which is what the price logic needs to answer "can I recover this sag before I
need to be back at target" — measured per bin, rather than computed from a
global gain that never converged.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

MODEL_VERSION = "offset_learner_v1"

# --- Outdoor bins ----------------------------------------------------------
# Upper edges; bin i covers [EDGES[i-1], EDGES[i]). Deliberately NOT uniform:
# the coldest bin is open-ended and therefore wide, because deep-cold bins fill
# slowest and matter most. One shared bin that accumulates real evidence beats
# three empty ones that never do.
BIN_EDGES: tuple[float, ...] = (-15.0, -10.0, -5.0, 0.0, 5.0, 10.0, 15.0)
N_BINS = len(BIN_EDGES) + 1

# How far outdoor temperature must clear a bin edge before the occupied bin
# actually changes. Without this, a reading sitting right on an edge flips the
# bin back and forth every cycle — splitting closed-loop samples between two
# bins instead of building evidence in one, and restarting the baseline dwell
# timer (see `bin_entered_s` below) each time it flips back. Sized above
# typical sensor resolution (0.1 degC) so ordinary noise cannot cross it.
BIN_HYSTERESIS_C = 0.3

# --- Control law -----------------------------------------------------------
# Degrees of outdoor spoof equivalent to one degree of steady indoor change.
# Physically motivated rather than tuned: spoofing outdoor +1 degC and letting
# indoor fall 1 degC change the indoor-outdoor gap identically, so a pump whose
# own curve is reasonably matched to the house gives a ratio of one.
#
# Deliberately a constant and not a learned parameter. Getting it wrong only
# changes how FAST the loop converges, never where it converges to — that is
# the property of integral action that makes the whole design robust — so
# learning it would add a failure mode to buy nothing. The price logic uses the
# same constant to convert an allowed sag into an offset.
SPOOF_PER_INDOOR_C = 1.0

# The integrator's time constant, as a multiple of the MEASURED rise time (see
# lag.py). Dimensionless, which is what makes it legitimate to hardcode where a
# gain in degC-per-degC was not: it is a stability margin against dead time,
# not a property of any particular house.
TAU_I_LAG_MULTIPLE = 3.0
TAU_I_MIN_H = 1.0
TAU_I_MAX_H = 24.0

# A small fixed proportional term for fast disturbance rejection. It does not
# need tuning because it does no steady-state work — the integrator does that —
# so too small merely means slower, never unstable. Bounded so it can never
# dominate the learned offset.
KP = 0.3
KP_MAX_C = 1.5

# --- Authority ------------------------------------------------------------
# The offset clamp opens as evidence accumulates, so a fresh install ships
# ACTIVE without shipping a controller that can do much. Day one the worst case
# is a 1 degC spoof, which is inside the noise of any weather curve; full
# authority arrives after roughly three days at a 15-minute cadence.
# These bound the negative (more-heat) direction only — asking for less heat
# has no equivalent runaway risk, so it does not need to earn trust the same
# way; see the clamp sites in `step`.
MIN_AUTHORITY_C = 1.0
MAX_AUTHORITY_C = 5.0
AUTHORITY_RAMP_SAMPLES = 288

# No single cycle may move the learned offset more than this, independently of
# the integrator's time constant. A second line of defence against a bad dt or
# a wild reading turning into a step change at the heat pump.
MAX_STEP_PER_CYCLE_C = 0.25

# --- Saturation / ceiling learning ----------------------------------------
# An error smaller than this is not evidence of anything; the sensor resolution
# is typically 0.1 degC.
SATURATION_ERROR_C = 0.3
# Indoor must be rising by more than this per hour to count as "closing".
CLOSING_RATE_C_PER_H = 0.05
# Consecutive saturated cycles before the bin's ceiling is tightened. Long
# enough that a single odd reading, or a brief window opening, does not
# permanently cap a bin.
SATURATION_STREAK = 4
# A ceiling never drops below this, or a bin could latch itself shut.
CEILING_MIN_C = 1.0
# How fast a ceiling recovers per cycle once the bin closes error at high
# offset again. Deliberately far slower than it tightens.
CEILING_RECOVER_C = 0.02
# Headroom above the deepest offset that was actually observed to work, so a
# learned ceiling sits just beyond proven-useful rather than exactly on it.
CEILING_MARGIN_C = 0.5
# "High offset" for ceiling recovery and close-rate learning, as a fraction of
# the bin's current ceiling.
HIGH_OFFSET_FRACTION = 0.7

# --- Baseline observation (compensation off) -------------------------------
# EWMA weight for the per-bin equilibrium indoor temperature. Deliberately slow:
# this is a steady-state fact being averaged out of a noisy, slowly-drifting
# signal, and there is no hurry — the table is only read at the moment
# compensation is switched on.
BASELINE_ALPHA = 0.05
# The house must have been in this outdoor bin long enough to have responded to
# it before its indoor temperature says anything about that bin. Expressed as a
# multiple of the MEASURED rise time, for the same reason the integrator's tau
# is: it is a dimensionless statement about dead time, not a tuned constant.
BASELINE_DWELL_LAG_MULTIPLE = 1.0
# Floor for the dwell requirement, used before any rise time has been measured
# (`rise_hours` is 0.0 until lag.py has evidence), so a fresh install cannot
# accept a sample the instant it crosses a bin edge.
BASELINE_MIN_DWELL_H = 1.0
# Reject samples taken while the house is still visibly moving. This is a
# quality filter against transients — a window opened, a setback recovery — not
# a demand for true equilibrium, which never quite happens outdoors. Loose
# enough that ordinary diurnal drift still contributes, so the average is not
# biased toward the turning points of the daily cycle.
BASELINE_MAX_DRIFT_C_PER_H = 0.5
# Evidence required in a bin before its baseline may seed an offset. Six
# accepted samples is 1.5 h of a 15-minute cadence spent settled in one band.
BASELINE_MIN_SAMPLES_TO_SEED = 6
# Hard cap on a seeded offset, independent of the authority ramp that will clamp
# it anyway. A baseline is open-loop evidence, so it gets less trust than the
# integrator's own: if the raw curve leaves the house 6 degC cold, that is a
# badly misconfigured pump curve and the right response is to start moving in
# the right direction, not to leap the whole way on one observation.
BASELINE_SEED_MAX_C = 3.0

# --- Table shaping ---------------------------------------------------------
# Share of each update bled into the two neighbouring bins. Spreads coverage so
# a bin entered for the first time starts from a sensible value instead of
# zero, and keeps the table smooth across the outdoor range.
NEIGHBOUR_SHARE = 0.25
# EWMA weight for the per-bin close-rate estimate.
CLOSE_RATE_ALPHA = 0.1
# A target change at least this large restarts the hold-off window.
HOLDOFF_TARGET_STEP_C = 0.3


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class OutdoorBin:
    """What has been learned about one outdoor-temperature band."""

    hold_offset_c: float = 0.0
    ceiling_c: float = MAX_AUTHORITY_C
    # Deepest offset magnitude at which this band was actually observed closing
    # its error. The evidence a learned ceiling is set from: "spoofing this far
    # demonstrably worked here", so anything much beyond it is a candidate for
    # not helping.
    useful_offset_c: float = 0.0
    close_rate_c_per_h: float = 0.0
    samples: int = 0
    # This bin's offset came from the open-loop baseline table, not from closed-
    # loop integration. Kept distinct from `samples` because the two carry very
    # different weight: `samples` is evidence our own action works and drives the
    # authority ramp, while a seed is only a better starting guess than zero.
    seeded: bool = False


@dataclass(frozen=True)
class BaselineBin:
    """Where the pump's own weather curve leaves the house in one outdoor band.

    Recorded only while compensation is off, when the published value is the raw
    outdoor temperature and the curve is therefore running untouched.

    `indoor_c` is an absolute equilibrium temperature rather than an error
    against target, so the table survives the occupant changing their mind about
    what temperature they want — see the module docstring.
    """

    indoor_c: float = 0.0
    samples: int = 0


@dataclass(frozen=True)
class LearnerState:
    """Everything the learner carries between cycles. Persisted verbatim."""

    bins: tuple[OutdoorBin, ...] = field(
        default_factory=lambda: tuple(OutdoorBin() for _ in range(N_BINS))
    )
    baseline_bins: tuple[BaselineBin, ...] = field(
        default_factory=lambda: tuple(BaselineBin() for _ in range(N_BINS))
    )
    total_samples: int = 0
    saturation_streak: int = 0
    holdoff_until_s: float | None = None
    prev_indoor_c: float | None = None
    prev_target_c: float | None = None
    prev_offset_c: float = 0.0
    # Dwell tracking for baseline acceptance: which bin we were in last cycle and
    # when we entered it.
    prev_bin_index: int | None = None
    bin_entered_s: float | None = None
    # Edge detection for seeding. Defaults False so that an install whose very
    # first cycle is already active does not count as an off->on transition and
    # try to seed from an empty baseline table.
    prev_is_active: bool = False


@dataclass(frozen=True)
class LearnerInputs:
    """One cycle's worth of live values, gathered by the coordinator."""

    now_s: float
    dt_hours: float
    indoor_temp_c: float | None
    indoor_data_available: bool
    target_c: float
    outdoor_temp_c: float
    # True while the hard heating limit is forcing the published value to the
    # warm ceiling instead of the learner's own offset. See `heuristic.
    # resolve_heating_hard_limit_engaged`.
    heating_hard_limit_engaged: bool
    is_active: bool
    # True while price compensation is deliberately holding the house below
    # target. The integrator freezes rather than fighting it — see `step`.
    price_braking: bool
    # Measured rise time from lag.py, in hours. Sets the integrator's time
    # constant and the hold-off window.
    rise_hours: float
    # Indoor-temperature-equivalent contribution of solar gain this cycle, as
    # estimated by the coordinator from heuristic.SOLAR_GAIN_C and the same
    # sun/cloud formula the feedforward term uses. Opaque here on purpose —
    # this module does not know or care where the number came from — and
    # already zeroed by the caller when solar shouldn't be trusted (e.g. an
    # indoor sensor with no solar gain reaching it, see heuristic.py's module
    # docstring). Used only to correct the open-loop baseline observation
    # below; defaults to 0.0 so every existing call site is unaffected.
    estimated_solar_gain_c: float = 0.0
    # True while the weather lookahead is deliberately holding the house ABOVE
    # target ahead of a forecast change. Same freeze as `price_braking` and
    # for the same reason, just in the other direction — see `step`. Defaults
    # False so every existing call site is unaffected.
    weather_preramp: bool = False
    # True while the sun pre-cool is deliberately holding the house BELOW
    # target ahead of more sun arriving. Same freeze, same reason, opposite
    # direction from `weather_preramp` — see `step`. Defaults False so every
    # existing call site is unaffected.
    sun_precool: bool = False
    # True when the set of contributing indoor sensors differs from last
    # cycle (a sensor joined or dropped out of the aggregate). The aggregate
    # value took a step that is not a real temperature change, so the
    # integrator must not read it as drift. Defaults False so every existing
    # call site is unaffected.
    indoor_sensor_set_changed: bool = False


@dataclass(frozen=True)
class LearnerResult:
    """The offset to apply, plus everything needed to explain it."""

    offset_c: float
    hold_offset_c: float
    proportional_c: float
    bin_index: int
    bin_label: str
    authority_c: float
    ceiling_c: float
    close_rate_c_per_h: float
    tau_i_h: float
    error_c: float | None
    saturated: bool
    frozen: bool
    freeze_reason: str | None
    coverage: float
    progress: float
    total_samples: int
    bin_samples: int
    reason: str
    # --- Open-loop baseline (what the raw curve does with no compensation) ---
    # `baseline_indoor_c` is where this outdoor band was last observed to settle
    # with compensation off; `baseline_deficit_c` is how far short of target
    # that is, positive meaning the raw curve leaves the house cold. Both None
    # until the band has been observed. Defaulted so a test or a caller can
    # build a result without knowing about the baseline at all.
    baseline_indoor_c: float | None = None
    baseline_deficit_c: float | None = None
    baseline_samples: int = 0
    baseline_coverage: float = 0.0
    baseline_reason: str = ""
    seeded_bins: int = 0
    seeded: bool = False
    model_version: str = MODEL_VERSION


def _raw_bin_index_for(outdoor_c: float) -> int:
    """Which bin an outdoor temperature falls in, with no hysteresis applied."""
    for index, edge in enumerate(BIN_EDGES):
        if outdoor_c < edge:
            return index
    return N_BINS - 1


def bin_index_for(outdoor_c: float, prev_index: int | None = None) -> int:
    """Which bin an outdoor temperature falls in.

    With `prev_index` given, a move to an adjacent bin only takes effect once
    `outdoor_c` clears the edge between them by `BIN_HYSTERESIS_C` — otherwise
    the previous bin is held. A jump of more than one bin (a real change, not
    noise on an edge) always takes effect immediately. `prev_index=None` gives
    the raw, hysteresis-free answer, which is what a first cycle or a
    from-scratch lookup wants.
    """
    raw_index = _raw_bin_index_for(outdoor_c)
    if prev_index is None or abs(raw_index - prev_index) != 1:
        return raw_index
    edge = BIN_EDGES[min(raw_index, prev_index)]
    if raw_index > prev_index:
        return raw_index if outdoor_c >= edge + BIN_HYSTERESIS_C else prev_index
    return raw_index if outdoor_c <= edge - BIN_HYSTERESIS_C else prev_index


def bin_label(index: int) -> str:
    """Human-readable range for a bin, e.g. "-10..-5 degC"."""
    index = max(0, min(N_BINS - 1, index))
    if index == 0:
        return f"below {BIN_EDGES[0]:.0f}°C"
    if index == N_BINS - 1:
        return f"above {BIN_EDGES[-1]:.0f}°C"
    return f"{BIN_EDGES[index - 1]:.0f}..{BIN_EDGES[index]:.0f}°C"


def initial_state() -> LearnerState:
    return LearnerState()


def authority_for(total_samples: int) -> float:
    """The offset clamp, widening with accumulated evidence."""
    progress = _clamp(total_samples / float(AUTHORITY_RAMP_SAMPLES), 0.0, 1.0)
    return MIN_AUTHORITY_C + (MAX_AUTHORITY_C - MIN_AUTHORITY_C) * progress


def coverage_of(bins: tuple[OutdoorBin, ...]) -> float:
    """Fraction of outdoor bins that have been visited at least once."""
    if not bins:
        return 0.0
    return sum(1 for b in bins if b.samples > 0) / float(len(bins))


def _has_value(b: OutdoorBin) -> bool:
    """Whether a bin holds a usable offset, learned or seeded.

    A seeded bin counts: an open-loop measurement of what the raw curve did in
    *this* band beats interpolating from a neighbouring band, even though it is
    weaker evidence than closed-loop integration. It deliberately does NOT count
    towards `coverage_of`, which reports proven learning.
    """
    return b.samples > 0 or b.seeded


def effective_hold_offset(bins: tuple[OutdoorBin, ...], index: int) -> float:
    """The offset to use for a bin, interpolating across unvisited ones.

    A bin nobody has occupied yet is not a cold start: the two nearest visited
    bins on either side are interpolated between, and beyond the outermost
    visited bin its value is held. So the first genuinely cold snap starts from
    whatever the house needed at the coldest temperature already seen, rather
    than from zero.

    Falls back to the bin's own stored value (normally 0.0, or whatever
    neighbour smoothing has bled into it) when nothing has been visited at all.
    """
    if _has_value(bins[index]):
        return bins[index].hold_offset_c

    lower: tuple[int, float] | None = None
    for i in range(index - 1, -1, -1):
        if _has_value(bins[i]):
            lower = (i, bins[i].hold_offset_c)
            break
    upper: tuple[int, float] | None = None
    for i in range(index + 1, len(bins)):
        if _has_value(bins[i]):
            upper = (i, bins[i].hold_offset_c)
            break

    if lower is None and upper is None:
        return bins[index].hold_offset_c
    if lower is None:
        return upper[1]
    if upper is None:
        return lower[1]
    span = upper[0] - lower[0]
    frac = (index - lower[0]) / float(span)
    return lower[1] + (upper[1] - lower[1]) * frac


def _freeze_reason(inputs: LearnerInputs, state: LearnerState) -> str | None:
    """Why learning must not advance this cycle, or None to proceed.

    Every entry here is a restatement of one rule: the integrator only
    accumulates error that the plant has had a chance to respond to, and only
    when its own output is what is actually driving the house.
    """
    if not inputs.is_active:
        return "compensation is off, so nothing is actually being applied"
    if not inputs.indoor_data_available or inputs.indoor_temp_c is None:
        return "indoor sensor unavailable"
    if inputs.indoor_sensor_set_changed:
        return "the set of contributing indoor sensors changed"
    if inputs.heating_hard_limit_engaged:
        return "heating hard limit engaged, output is forced rather than learned"
    if inputs.price_braking:
        return "price compensation is deliberately holding below target"
    if inputs.weather_preramp:
        return "anticipating a weather change, holding the house above target"
    if inputs.sun_precool:
        return "anticipating more sun, holding the house below target"
    if state.holdoff_until_s is not None and inputs.now_s < state.holdoff_until_s:
        remaining = (state.holdoff_until_s - inputs.now_s) / 60.0
        return f"waiting {remaining:.0f} min for the last change to reach the room"
    if inputs.dt_hours <= 0.0:
        return "no elapsed time since the last step"
    return None


def baseline_coverage_of(baseline_bins: tuple[BaselineBin, ...]) -> float:
    """Fraction of outdoor bins with an open-loop baseline observation."""
    if not baseline_bins:
        return 0.0
    return sum(1 for b in baseline_bins if b.samples > 0) / float(len(baseline_bins))


def seed_offset_for(baseline_indoor_c: float, target_c: float) -> float:
    """The offset implied by an observed open-loop equilibrium.

    Same sign convention as the integrator's own update: the raw curve leaving
    the house below target means a deficit, which wants a colder (negative)
    spoof to make the pump work harder.
    """
    return -(target_c - baseline_indoor_c) * SPOOF_PER_INDOOR_C


def _observe_baseline(
    baseline_bins: tuple[BaselineBin, ...],
    inputs: LearnerInputs,
    index: int,
    dwell_h: float,
    drift_c_per_h: float | None,
) -> tuple[tuple[BaselineBin, ...], str]:
    """Fold this cycle's indoor temperature into the open-loop baseline table.

    Only ever called while compensation is off, i.e. while the pump is running
    its own untouched weather curve. Returns the table (updated or not) and a
    human-readable account of what happened, which surfaces on the sensor so the
    occupant can see the off period doing something.
    """
    if not inputs.indoor_data_available or inputs.indoor_temp_c is None:
        return baseline_bins, "indoor sensor unavailable"
    if inputs.heating_hard_limit_engaged:
        # This far above the hard limit the raw curve is not heating either,
        # so where the house sits is free-floating (solar, venting, an AC
        # unit) rather than evidence about the heat curve.
        return baseline_bins, "heating hard limit engaged, nothing to measure"

    required_dwell_h = max(
        BASELINE_MIN_DWELL_H, inputs.rise_hours * BASELINE_DWELL_LAG_MULTIPLE
    )
    if dwell_h < required_dwell_h:
        return baseline_bins, (
            f"settling into {bin_label(index)} "
            f"({dwell_h:.1f}/{required_dwell_h:.1f} h)"
        )
    if drift_c_per_h is not None and abs(drift_c_per_h) > BASELINE_MAX_DRIFT_C_PER_H:
        return baseline_bins, f"house still moving ({drift_c_per_h:+.2f}°C/h)"

    # Strip solar gain out of the reading before it becomes evidence about the
    # pump curve: the baseline wants "what would this outdoor band produce with
    # zero solar gain", not a mix of sunny-afternoon and overcast-night samples
    # blended by the EWMA. Deliberately scoped to this path only — the drift
    # filter above stays on the raw reading (it is rejecting transients, not
    # measuring equilibrium) and so does `rate_c_per_h` in `step()`.
    corrected_indoor_c = inputs.indoor_temp_c - inputs.estimated_solar_gain_c

    current = baseline_bins[index]
    if current.samples == 0:
        updated_indoor = corrected_indoor_c
    else:
        updated_indoor = (
            1.0 - BASELINE_ALPHA
        ) * current.indoor_c + BASELINE_ALPHA * corrected_indoor_c

    updated = list(baseline_bins)
    updated[index] = BaselineBin(
        indoor_c=updated_indoor, samples=current.samples + 1
    )
    deficit = inputs.target_c - updated_indoor
    return tuple(updated), (
        f"raw curve holds {bin_label(index)} at {updated_indoor:.1f}°C "
        f"({deficit:+.1f}°C vs target)"
    )


def seed_offsets_from_baseline(
    bins: tuple[OutdoorBin, ...],
    baseline_bins: tuple[BaselineBin, ...],
    target_c: float,
) -> tuple[tuple[OutdoorBin, ...], int]:
    """Give un-learned bins a starting offset from what the raw curve did.

    Called once, on the transition from compensation-off to compensation-on.
    Bins with closed-loop evidence (`samples > 0`) are never touched: measuring
    the response to our own action is strictly better information than inferring
    it from where the curve settled.

    A seed deeper than today's authority ramp allows is truncated to it by
    `_apply_to_bins` on the very next cycle, and that is deliberate rather than
    a loss. The truncated value is still pinned against the clamp, so the pump
    gets everything it is allowed to get; as the ramp opens, the persistent
    error re-deepens the bin exactly as fast as it is permitted to. And if the
    error has by then vanished, the shallower value was the right one and the
    baseline was optimistic — so the arrangement is self-correcting in both
    directions. Storing the seed deeper than it can be applied would only be
    windup wearing a different hat.

    Returns the table and how many bins were seeded.
    """
    updated = list(bins)
    seeded_count = 0
    for i, (bin_, baseline) in enumerate(zip(bins, baseline_bins)):
        if bin_.samples > 0:
            continue
        if baseline.samples < BASELINE_MIN_SAMPLES_TO_SEED:
            continue
        seed = _clamp(
            seed_offset_for(baseline.indoor_c, target_c),
            -BASELINE_SEED_MAX_C,
            BASELINE_SEED_MAX_C,
        )
        # The bin's own learned capacity ceiling still binds on the heating side:
        # a seed must not ask for more spoofing than this band was previously
        # observed to be able to use.
        ceiling = _clamp(bin_.ceiling_c, CEILING_MIN_C, MAX_AUTHORITY_C)
        seed = _clamp(seed, -ceiling, MAX_AUTHORITY_C)
        updated[i] = replace(bin_, hold_offset_c=seed, seeded=True)
        seeded_count += 1
    return tuple(updated), seeded_count


def _apply_to_bins(
    bins: tuple[OutdoorBin, ...], index: int, delta_c: float, lower: float, upper: float
) -> tuple[OutdoorBin, ...]:
    """Move the occupied bin by `delta_c` and bleed a share into its neighbours.

    Only the occupied bin's sample count advances: the neighbour share exists to
    pre-warm values so an adjacent bin starts sensibly when first entered, not
    to claim evidence that was never collected there.
    """
    updated = list(bins)
    occupied = updated[index]
    updated[index] = replace(
        occupied,
        hold_offset_c=_clamp(occupied.hold_offset_c + delta_c, lower, upper),
        samples=occupied.samples + 1,
    )
    for neighbour in (index - 1, index + 1):
        if 0 <= neighbour < len(updated):
            nb = updated[neighbour]
            updated[neighbour] = replace(
                nb,
                hold_offset_c=_clamp(
                    nb.hold_offset_c + delta_c * NEIGHBOUR_SHARE, lower, upper
                ),
            )
    return tuple(updated)


def step(state: LearnerState, inputs: LearnerInputs) -> tuple[LearnerState, LearnerResult]:
    """Advance the learner one cycle and return the offset to apply.

    Never raises: every guard degrades to "hold the current offset and explain
    why", because this function's output goes straight to a heat pump.
    """
    index = bin_index_for(inputs.outdoor_temp_c, state.prev_bin_index)
    bins = state.bins
    if len(bins) != N_BINS:  # defensive: a truncated restore
        bins = tuple(OutdoorBin() for _ in range(N_BINS))
    baseline_bins = state.baseline_bins
    if len(baseline_bins) != N_BINS:  # defensive: a truncated restore
        baseline_bins = tuple(BaselineBin() for _ in range(N_BINS))

    # How long we have been continuously in this outdoor bin. Reset rather than
    # restored across a restart, because it is measured against the same
    # monotonic clock as `holdoff_until_s` and for the same reason is not
    # persisted — the safe reading of "unknown" is "start settling again".
    bin_entered_s = state.bin_entered_s
    if state.prev_bin_index != index or bin_entered_s is None:
        bin_entered_s = inputs.now_s
    dwell_h = max(0.0, (inputs.now_s - bin_entered_s) / 3600.0)

    # Deliberately None rather than 0.0 when unknown: the learning block below
    # treats an unmeasurable rate as "not closing", but for baseline acceptance
    # "unknown" must not masquerade as "perfectly settled".
    drift_c_per_h: float | None = None
    if (
        state.prev_indoor_c is not None
        and inputs.indoor_temp_c is not None
        and inputs.dt_hours > 0.0
    ):
        drift_c_per_h = (inputs.indoor_temp_c - state.prev_indoor_c) / inputs.dt_hours

    # Switching compensation on is the one moment the baseline table pays off.
    # Done before `hold` is read below, so the first active cycle already acts on
    # the seeded value instead of starting from zero.
    seeded_bins = 0
    if inputs.is_active and not state.prev_is_active:
        bins, seeded_bins = seed_offsets_from_baseline(
            bins, baseline_bins, inputs.target_c
        )

    # While off the integrator is frozen and the pump runs its own curve, so
    # observe where that curve leaves the house instead of learning nothing.
    baseline_reason = ""
    if not inputs.is_active:
        baseline_bins, baseline_reason = _observe_baseline(
            baseline_bins, inputs, index, dwell_h, drift_c_per_h
        )

    authority = authority_for(state.total_samples)
    progress = _clamp(state.total_samples / float(AUTHORITY_RAMP_SAMPLES), 0.0, 1.0)
    current_bin = bins[index]
    ceiling = _clamp(current_bin.ceiling_c, CEILING_MIN_C, MAX_AUTHORITY_C)
    tau_i_h = _clamp(
        inputs.rise_hours * TAU_I_LAG_MULTIPLE, TAU_I_MIN_H, TAU_I_MAX_H
    )

    # Heating is the negative direction, so the bin's ceiling binds there.
    # Backing off heat has no capacity limit the way heating does, but a
    # sustained disturbance (wood stove, uncompensated solar gain, a
    # mis-sited sensor) can still wind the integrator up indefinitely if the
    # positive side is unbounded — the house then runs cold for as long as it
    # takes to unwind once the disturbance ends. So it earns authority
    # symmetrically with the negative side, same as the ramp already does for
    # a fresh install.
    lower = -min(authority, ceiling)
    upper = authority

    hold = _clamp(effective_hold_offset(bins, index), lower, upper)

    error_c: float | None = None
    if inputs.indoor_data_available and inputs.indoor_temp_c is not None:
        error_c = inputs.target_c - inputs.indoor_temp_c

    # A target change restarts the hold-off: the integrator must not accumulate
    # the step it just asked for while that heat is still on its way.
    holdoff_until = state.holdoff_until_s
    if (
        state.prev_target_c is not None
        and abs(inputs.target_c - state.prev_target_c) >= HOLDOFF_TARGET_STEP_C
    ):
        holdoff_until = inputs.now_s + max(0.0, inputs.rise_hours) * 3600.0

    working = replace(state, bins=bins, holdoff_until_s=holdoff_until)
    freeze_reason = _freeze_reason(inputs, working)

    saturated = False
    saturation_streak = state.saturation_streak
    new_bins = bins
    total_samples = state.total_samples

    # `error_c is not None` is always true here — `_freeze_reason` already
    # returns "indoor sensor unavailable" on the only path that leaves it
    # `None` — but it's what lets `error_c` be used below as a plain `float`
    # rather than `float | None`, so it stays as a type-narrowing guard.
    if freeze_reason is None and error_c is not None:
        rate_c_per_h = 0.0
        if state.prev_indoor_c is not None and inputs.dt_hours > 0.0:
            rate_c_per_h = (inputs.indoor_temp_c - state.prev_indoor_c) / inputs.dt_hours
        closing = rate_c_per_h > CLOSING_RATE_C_PER_H

        raw_delta = -(error_c * SPOOF_PER_INDOOR_C) * (inputs.dt_hours / tau_i_h)
        delta = _clamp(raw_delta, -MAX_STEP_PER_CYCLE_C, MAX_STEP_PER_CYCLE_C)
        candidate = hold + delta

        # Pinned against the bin's CAPACITY ceiling specifically, not merely
        # against the authority clamp. The distinction matters: early on the
        # authority ramp is far tighter than any ceiling, and hitting a
        # deliberate safety clamp is not evidence that the pump ran out of
        # output. Conflating the two would let a fresh install permanently cap
        # every band it visited at 1 degC.
        at_capacity = candidate <= -ceiling + 1e-9

        # Saturation: at capacity, still meaningfully short of target, and not
        # closing. Covers both the deep-cold capacity case and a sustained
        # disturbance (an open window), which are not distinguishable from
        # indoor temperature alone — and which want the same response, so they
        # do not need to be.
        if at_capacity and error_c > SATURATION_ERROR_C and not closing:
            saturation_streak += 1
            saturated = True
        else:
            saturation_streak = 0

        if saturated and saturation_streak >= SATURATION_STREAK:
            # Cap just beyond whatever depth was last seen actually working
            # here. With no such evidence yet, cap where we are — saturation
            # means more spoofing does not help, never that less heat is
            # wanted, so the ceiling never drops below the current offset.
            if current_bin.useful_offset_c > 0.0:
                target_ceiling = current_bin.useful_offset_c + CEILING_MARGIN_C
            else:
                target_ceiling = abs(hold)
            ceiling = _clamp(
                min(ceiling, target_ceiling), CEILING_MIN_C, MAX_AUTHORITY_C
            )
            bins = tuple(
                replace(b, ceiling_c=ceiling) if i == index else b
                for i, b in enumerate(bins)
            )
            lower = -min(authority, ceiling)
        elif closing and abs(hold) >= HIGH_OFFSET_FRACTION * ceiling:
            # The bin is delivering at high offset after all. Record the depth
            # as proven-useful, let the ceiling recover, and track how fast the
            # error actually closes here (which the price logic needs to answer
            # "can I buy this sag back").
            recovered = _clamp(
                ceiling + CEILING_RECOVER_C, CEILING_MIN_C, MAX_AUTHORITY_C
            )
            blended = (
                (1.0 - CLOSE_RATE_ALPHA) * current_bin.close_rate_c_per_h
                + CLOSE_RATE_ALPHA * rate_c_per_h
            )
            bins = tuple(
                replace(
                    b,
                    ceiling_c=recovered,
                    close_rate_c_per_h=blended,
                    useful_offset_c=max(b.useful_offset_c, abs(hold)),
                )
                if i == index
                else b
                for i, b in enumerate(bins)
            )
            ceiling = recovered
            lower = -min(authority, ceiling)

        if not saturated:
            bins = _apply_to_bins(bins, index, delta, lower, upper)
            total_samples = state.total_samples + 1
            hold = bins[index].hold_offset_c
        else:
            hold = _clamp(hold, lower, upper)
        new_bins = bins

    # The proportional term is transient and never accumulated, so it is exempt
    # from the bin ceiling — but not from the authority clamp on the total.
    proportional_c = 0.0
    if error_c is not None:
        proportional_c = _clamp(
            -KP * error_c * SPOOF_PER_INDOOR_C, -KP_MAX_C, KP_MAX_C
        )

    # Same authority clamp on both sides of the total — see the lower/upper
    # comment above for why the positive side is bounded too.
    offset_c = _clamp(hold + proportional_c, -authority, authority)

    final_bin = new_bins[index]
    final_baseline = baseline_bins[index]
    new_state = LearnerState(
        bins=new_bins,
        baseline_bins=baseline_bins,
        total_samples=total_samples,
        saturation_streak=saturation_streak,
        holdoff_until_s=holdoff_until,
        prev_indoor_c=inputs.indoor_temp_c if inputs.indoor_data_available else None,
        prev_target_c=inputs.target_c,
        prev_offset_c=offset_c,
        prev_bin_index=index,
        bin_entered_s=bin_entered_s,
        prev_is_active=inputs.is_active,
    )

    label = bin_label(index)
    if freeze_reason is not None:
        reason = f"Holding {hold:+.2f}°C at {label} — {freeze_reason}"
        if not inputs.is_active and baseline_reason:
            reason += f"; baseline: {baseline_reason}"
    elif saturated:
        reason = (
            f"{label}: pump appears at capacity, capping at {abs(lower):.2f}°C "
            f"(error {error_c:+.2f}°C)"
        )
    else:
        reason = (
            f"{label}: holding {hold:+.2f}°C"
            + (f", error {error_c:+.2f}°C" if error_c is not None else "")
            + f", closing over {tau_i_h:.1f} h"
        )

    if seeded_bins:
        reason = (
            f"Started from the raw-curve baseline in {seeded_bins} band(s); " + reason
        )

    result = LearnerResult(
        baseline_indoor_c=(
            final_baseline.indoor_c if final_baseline.samples > 0 else None
        ),
        baseline_deficit_c=(
            inputs.target_c - final_baseline.indoor_c
            if final_baseline.samples > 0
            else None
        ),
        baseline_samples=final_baseline.samples,
        baseline_coverage=baseline_coverage_of(baseline_bins),
        baseline_reason=baseline_reason,
        seeded_bins=seeded_bins,
        seeded=final_bin.seeded,
        offset_c=offset_c,
        hold_offset_c=hold,
        proportional_c=proportional_c,
        bin_index=index,
        bin_label=label,
        authority_c=authority,
        ceiling_c=ceiling,
        close_rate_c_per_h=final_bin.close_rate_c_per_h,
        tau_i_h=tau_i_h,
        error_c=error_c,
        saturated=saturated,
        frozen=freeze_reason is not None,
        freeze_reason=freeze_reason,
        coverage=coverage_of(new_bins),
        progress=progress,
        total_samples=total_samples,
        bin_samples=final_bin.samples,
        reason=reason,
    )
    return new_state, result


def recoverable_sag_c(close_rate_c_per_h: float, window_h: float) -> float:
    """How much sag this bin can actually buy back within `window_h`.

    Used by the price logic to answer the question the old hand-drawn
    cold taper was approximating: not "is it cold" but "can I get this back
    before I need to be at target again". A bin with no close-rate evidence
    yet returns 0.0, which callers treat as "no measured basis to taper on"
    rather than as "recovers nothing".
    """
    if close_rate_c_per_h <= 0.0 or window_h <= 0.0:
        return 0.0
    return close_rate_c_per_h * window_h
