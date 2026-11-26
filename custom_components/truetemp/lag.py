"""How long this house takes to respond, measured rather than configured.

Pure module: standard library only, no dependency on any other module in this
package (not even `const.py`), so it is importable and unit-testable without
Home Assistant installed. CI never installs `homeassistant`, so this is what
makes the module testable at all.

## Why this exists

An integral controller with unmodelled dead time oscillates. It keeps winding
up while heat is already in the pipe, overshoots, unwinds, undershoots. So
`learner.py` cannot pick its integrator time constant without knowing how long
commanded heat actually takes to arrive, and the price logic in `heuristic.py`
cannot know how early to start coasting before an expensive hour.

Both numbers used to come from a dropdown: one `emitter_tau_floor_h` constant,
4 h for underfloor and 2 h for radiators, applied to every house of that type.
This module measures them instead.

## Two lags, not one

They are deliberately asymmetric, because the physics is:

    rise (L_up)    command -> measurable indoor response. The pump's own
                   controller integrating its curve error (a Nibe
                   degree-minutes accumulator is exactly this), plus the water
                   loop, plus emitter mass.

    fall (L_down)  stop calling for heat -> the house actually stops gaining.
                   A heavy slab keeps delivering long after the compressor
                   stops, so this is typically LONGER than the rise. It is the
                   one price braking is timed off, but only half of it: what
                   costs money is the compressor still drawing power, not the
                   slab coasting on stored heat afterward, and this measures
                   the latter along with the former.

## What is measured, and why it is not a cross-correlation

An earlier cut of this module correlated offset changes against indoor changes
at every candidate lag and took the argmax. That works beautifully on synthetic
step data and fails on a real closed loop, for a reason worth recording: in
feedback, the controller output and the plant output are correlated at ZERO lag
because the controller is reacting to the plant. That confound swamps the true
transport delay, and the correlation curve comes out monotonically decreasing
with a meaningless peak at lag zero.

So this measures something both better defined and more useful:

    the time from a deliberate change in the applied offset until the house has
    completed HALF of the move that change eventually produces

That is not pure transport delay — it folds in the emitter time constant — but
it is exactly the quantity both consumers actually want. "How long before this
action has meaningfully taken effect" is the right question for scheduling a
pre-brake, and the right thing to scale an integrator's time constant from.

Being event-triggered is what dodges the feedback confound: the measurement is
anchored to a discrete action and asks when a threshold was crossed afterwards,
rather than correlating two continuously coupled rates.

## Two sources of events, and one that must be thrown away

**Target steps** are the strongest signal, because the comfort target is
exogenous — nothing about the house causes it. A schedule, a manual nudge or a
voice command steps it, and the house answers after its own delay. This is the
textbook way to identify a plant, and it needs no disentangling.

**Offset steps** are usable too, but only while the target is holding still.
During a setpoint transient the offset and the indoor temperature are BOTH
consequences of the setpoint change, and they routinely move in opposite
directions: after the target drops, the house coasts down while the controller
simultaneously eases the offset back toward its steady value. Reading that as
"spoofing colder made the house colder" would invert the measurement outright.
So any offset event whose window contains a target change is discarded, and a
window in which the house moved the wrong way is discarded regardless.

## Only clean, full-window events count

An event's response takes a long time to show (hours for a slab), so it can
only be read from an observation window of the full `MAX_LAG_MINUTES`. An
earlier version cut each window short at the NEXT action in either direction
and then asked when half of the move *inside the truncated window* had arrived.
That is wrong in a closed loop: the controller moves the offset every 15-60
minutes, so windows were short, and the "half-way" time of a short window is
the controller's cadence, not the house's. A house whose one clean event showed
~225 min read 38 min. A confident, too-short number is the worst outcome here
(the integrator would be tuned for a house that does not exist), so truncated
windows are now discarded rather than measured.

An event is therefore a reading only if

- its full window is already in the buffer (it is recorded once, when the
  window completes, never earlier and never again),
- no OPPOSITE-direction action (offset or target) falls inside the window,
  because that reversal's response would cancel this one's. Same-direction
  continuation moves are fine: they add to the response rather than cancel it,
  and a later same-direction event inside an earlier one's window is treated as
  that same action's continuation rather than a second reading,
- the target held still throughout, and the house moved the right way by more
  than the sensor's resolution.

## Long memory, and a blend instead of a switch

Clean events are rare: roughly one per few days in a fast house and often none
in a week in a slow underfloor one. So readings are kept for `READING_MAX_AGE_S`
(newest `MAX_READINGS` per direction), independent of the 4-day sample buffer,
and survive restarts, sensor gaps and heating pauses.

The published value is not a switch from default to measured. It is

    value = default + w * (median(readings) - default),  w = n / (n + BLEND_K)

so one reading moves the value only a quarter of the way, and the value
converges as evidence accumulates. With no readings it is exactly the
emitter-type fallback. The result is never below `FLOOR_FRACTION` of the
fallback, because no passive method here measures precisely and a short
number is the dangerous direction. A change of heating type discards the
readings, which described a different house.

## The limit that remains

This is a passive estimate from a closed loop, not a system identification:
even clean events are contaminated by weather, sun and the controller's own
follow-up moves. The design goal is therefore "never a confident wrong (too
short) number; default until clean evidence, then move toward it gradually".
Nothing here claims precision.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# --- Emitter-type fallbacks ------------------------------------------------
# Used until the estimator has a confident opinion, which is the
# only remaining reason the install asks what the emitters are. Rise/fall pairs
# are asymmetric for the reason in the module docstring.
HEATING_TYPE_RADIATORS = "radiators"
HEATING_TYPE_UNDERFLOOR = "underfloor"
HEATING_TYPE_MIXED = "mixed"
HEATING_TYPES = (HEATING_TYPE_RADIATORS, HEATING_TYPE_UNDERFLOOR, HEATING_TYPE_MIXED)
DEFAULT_HEATING_TYPE = HEATING_TYPE_RADIATORS

_FALLBACK_MINUTES: dict[str, tuple[float, float]] = {
    #                        rise,  fall
    HEATING_TYPE_RADIATORS: (90.0, 120.0),
    HEATING_TYPE_MIXED: (150.0, 240.0),
    HEATING_TYPE_UNDERFLOOR: (240.0, 360.0),
}

# --- Estimator tuning ------------------------------------------------------
# Longest response time the search considers, and therefore the width of the
# window examined after each event. Six hours covers a heavy slab. A window
# shorter than this is never measured (see the module docstring).
MAX_LAG_MINUTES = 360.0
# How much sample history to keep as the scan window. Four days at a 15-minute
# cadence: far more than the 24-step event window, so a full window always fits
# with margin. The *result* does not depend on this buffer; see READING_*.
BUFFER_SAMPLES = 384
# How far the offset must move, in one direction, to count as a deliberate
# action rather than the integrator's ordinary drift.
EVENT_MIN_MOVE_C = 0.15
# ...and how quickly it must get there. A slow ramp spread over many hours is
# not step-like: the response would be smeared across the movement itself and
# the reading would measure the ramp rather than the house.
EVENT_MAX_STEPS = 8
# The indoor response must exceed this to be distinguishable from sensor
# quantisation, which is typically 0.1 degC.
MIN_RESPONSE_C = 0.08
# Fraction of the eventual response that counts as "has taken effect".
RESPONSE_FRACTION = 0.5
# Clean readings are kept this long (8 weeks) so rare events accumulate, but a
# house that really changed (new emitters, a serviced pump) still re-converges.
READING_MAX_AGE_S = 8 * 7 * 24 * 3600.0
# Newest readings kept per direction; bounds storage and lets old ones age out.
MAX_READINGS = 20
# Blend weight w = n / (n + BLEND_K): one reading moves the value 25% of the way
# from the default to the measured median, three readings 50%.
BLEND_K = 3.0
# A reading-based value never goes below this fraction of the heating type's
# fallback. Closed-loop contamination biases measurements SHORT, and a too-short
# lag makes the integrator wind up and oscillate, so the floor bounds the
# damage of a bad reading that slipped through.
FLOOR_FRACTION = 0.5
# How far the comfort target must move to count as a deliberate setpoint step.
TARGET_STEP_C = 0.3


@dataclass(frozen=True)
class LagReading:
    """One clean, full-window response measurement."""

    direction: str  # "rise" or "fall"
    minutes: float
    at_s: float  # wall-clock (epoch seconds) when it was recorded


@dataclass(frozen=True)
class LagState:
    """Rolling history of what was asked for, what was applied, and what the
    house did, plus the long-lived clean readings measured from it.

    The three series are sampled on the caller's fixed cadence
    (`step_minutes`), one entry per learner step, oldest first. Storing plain
    aligned series rather than timestamps keeps every lookup a simple index
    shift.

    `sample_counter` counts every sample ever pushed (it survives buffer wipes
    and shifting), so each event has an absolute index that stays valid as the
    buffer slides; `evaluated_through` is the highest absolute event index
    already decided, which is what stops a reading being recorded twice when
    the buffer is re-scanned every cycle.
    """

    offsets_c: tuple[float, ...] = ()
    indoors_c: tuple[float, ...] = ()
    targets_c: tuple[float, ...] = ()
    step_minutes: float = 15.0
    readings: tuple[LagReading, ...] = ()
    heating_type: str | None = None
    sample_counter: int = 0
    evaluated_through: int = -1


@dataclass(frozen=True)
class LagResult:
    """Blended (or default) response times, in minutes.

    `*_measured` means at least one clean reading exists; `*_events` is the
    number of readings behind the value; `*_source` is "default" or "blended".
    """

    rise_minutes: float
    fall_minutes: float
    rise_measured: bool
    fall_measured: bool
    rise_events: int
    fall_events: int
    samples: int
    reason: str
    rise_default_minutes: float = 0.0
    fall_default_minutes: float = 0.0
    rise_source: str = "default"
    fall_source: str = "default"

    @property
    def rise_hours(self) -> float:
        return self.rise_minutes / 60.0

    @property
    def fall_hours(self) -> float:
        return self.fall_minutes / 60.0


def _normalise_type(heating_type: str | None) -> str:
    key = (heating_type or "").strip().lower()
    return key if key in _FALLBACK_MINUTES else DEFAULT_HEATING_TYPE


def fallback_minutes(heating_type: str | None) -> tuple[float, float]:
    """(rise, fall) fallback for an emitter type, defaulting to radiators."""
    return _FALLBACK_MINUTES[_normalise_type(heating_type)]


def initial_state(step_minutes: float = 15.0) -> LagState:
    return LagState(step_minutes=max(1.0, step_minutes))


def clear_readings(state: LagState) -> LagState:
    """Drop the readings and the sample buffer, keeping nothing measured.

    The counter is kept so absolute event indices never repeat.
    """
    return replace(
        state,
        offsets_c=(),
        indoors_c=(),
        targets_c=(),
        readings=(),
        evaluated_through=state.sample_counter - 1,
    )


def push(
    state: LagState, offset_c: float, indoor_c: float | None, target_c: float
) -> LagState:
    """Append one sample, dropping the oldest once the buffer is full.

    A missing indoor reading breaks the chain, so the whole sample buffer is
    dropped rather than stitched across the gap — a discontinuity would
    register as a huge fake event with a fabricated response. The clean
    readings are NOT dropped: they were measured before the gap and remain
    valid evidence about the house.
    """
    if indoor_c is None:
        return replace(state, offsets_c=(), indoors_c=(), targets_c=())
    return replace(
        state,
        offsets_c=(*state.offsets_c, float(offset_c))[-BUFFER_SAMPLES:],
        indoors_c=(*state.indoors_c, float(indoor_c))[-BUFFER_SAMPLES:],
        targets_c=(*state.targets_c, float(target_c))[-BUFFER_SAMPLES:],
        sample_counter=state.sample_counter + 1,
    )


def _find_events(offsets: tuple[float, ...], rising: bool) -> list[int]:
    """Indices where a deliberate offset move in one direction began.

    Sign convention: the offset is added to the outdoor temperature the pump
    reads, so a MORE NEGATIVE offset spoofs colder and buys more heat. A rise
    event is therefore a downward run of the offset, and a fall event an upward
    one.

    A run qualifies only if it covers `EVENT_MIN_MOVE_C` within
    `EVENT_MAX_STEPS` of starting — decisive enough to be an action, and quick
    enough that the response can be told apart from the movement itself.
    """
    want = -1.0 if rising else 1.0
    events: list[int] = []
    index = 1
    while index < len(offsets):
        delta = offsets[index] - offsets[index - 1]
        if delta * want <= 0.0:
            index += 1
            continue
        # Walk the same-signed run from here, watching for the threshold.
        # The event is anchored at the first sample carrying the NEW offset,
        # not the one before it, so a measured response counts from when the
        # action was applied rather than one step early.
        start = index
        travelled = 0.0
        steps = 0
        cursor = index
        qualified = False
        while cursor < len(offsets):
            move = offsets[cursor] - offsets[cursor - 1]
            if move * want <= 0.0:
                break
            travelled += abs(move)
            steps += 1
            if travelled >= EVENT_MIN_MOVE_C and steps <= EVENT_MAX_STEPS:
                qualified = True
                cursor += 1
                break
            if steps > EVENT_MAX_STEPS:
                break
            cursor += 1
        if qualified:
            events.append(start)
            # Skip the rest of this run so one long move is one event.
            while cursor < len(offsets):
                move = offsets[cursor] - offsets[cursor - 1]
                if move * want <= 0.0:
                    break
                cursor += 1
        index = max(cursor, index + 1)
    return events


def _response_steps(
    indoors: tuple[float, ...], start: int, window: int, rising: bool
) -> int | None:
    """Steps from an event until half its eventual response had arrived.

    Returns None when the window runs past the end of the data, when the house
    moved the wrong way (so this event's response is confounded by something
    else), or when the total move is below the sensor's own resolution.
    """
    end = start + window
    if end >= len(indoors):
        return None
    baseline = indoors[start]
    total = indoors[end] - baseline
    if rising and total < MIN_RESPONSE_C:
        return None
    if not rising and total > -MIN_RESPONSE_C:
        return None
    threshold = abs(total) * RESPONSE_FRACTION
    for offset_steps in range(1, window + 1):
        if abs(indoors[start + offset_steps] - baseline) >= threshold:
            return offset_steps
    return None


def _find_target_events(targets: tuple[float, ...], rising: bool) -> list[int]:
    """Indices where the comfort target stepped.

    The cleanest excitation available, because the target is exogenous: nothing
    about the house causes a schedule to fire. A target increase should be
    followed by indoor rising, so it is a rise event.
    """
    want = 1.0 if rising else -1.0
    return [
        index
        for index in range(1, len(targets))
        if (targets[index] - targets[index - 1]) * want >= TARGET_STEP_C
    ]


def _target_holds(targets: tuple[float, ...], start: int, window: int) -> bool:
    """Whether the target stayed put across an event's whole window.

    Offset events are only readable while it does. During a setpoint transient
    the offset and the indoor temperature are both consequences of the setpoint
    change and routinely move in opposite directions, which would invert the
    measurement.
    """
    if start >= len(targets):
        return False
    baseline = targets[start]
    end = min(start + window + 1, len(targets))
    return all(abs(targets[i] - baseline) < TARGET_STEP_C for i in range(start, end))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _candidate_events(state: LagState, rising: bool) -> list[tuple[int, bool]]:
    """(index, is_target_step) for every event worth trying, in order."""
    events = [(i, True) for i in _find_target_events(state.targets_c, rising)]
    events += [(i, False) for i in _find_events(state.offsets_c, rising)]
    return sorted(set(events))


def _window_steps(step: float) -> int:
    return max(1, int(MAX_LAG_MINUTES / step))


def _decide_events(
    state: LagState, window: int
) -> list[tuple[int, bool, int | None]]:
    """(buffer index, rising, response steps or None) for every event whose
    full window is already in the buffer.

    None means the event was decided and discarded. Events whose window has
    not completed yet are omitted: they are pending, not discarded.

    An event is discarded when an opposite-direction action (offset or target)
    lands inside its window, when it continues an earlier same-direction event
    (it would be measured against a response already under way), when the
    target moved during the window, or when the response is unreadable. The
    window is never truncated: a truncated window measures the controller's
    cadence, not the house.
    """
    samples = len(state.indoors_c)
    candidates = {rising: _candidate_events(state, rising) for rising in (True, False)}
    decided: list[tuple[int, bool, int | None]] = []
    for rising in (True, False):
        opposite = [index for index, _ in candidates[not rising]]
        previous: int | None = None
        for start, _is_target in candidates[rising]:
            continuation = previous is not None and start - previous <= window
            previous = start
            if start + window >= samples:
                continue
            steps: int | None = None
            if (
                not continuation
                and not any(start < b <= start + window for b in opposite)
                and _target_holds(state.targets_c, start, window)
            ):
                steps = _response_steps(state.indoors_c, start, window, rising)
            decided.append((start, rising, steps))
    return decided


def _prune(
    readings: tuple[LagReading, ...], now_s: float | None
) -> tuple[LagReading, ...]:
    """Drop expired readings and keep the newest `MAX_READINGS` per direction."""
    kept = [
        r for r in readings
        if now_s is None or now_s - r.at_s <= READING_MAX_AGE_S
    ]
    result: list[LagReading] = []
    for direction in ("rise", "fall"):
        own = sorted((r for r in kept if r.direction == direction), key=lambda r: r.at_s)
        result.extend(own[-MAX_READINGS:])
    return tuple(sorted(result, key=lambda r: r.at_s))


def _same_type(state: LagState, heating_type: str | None) -> bool:
    return state.heating_type is None or state.heating_type == _normalise_type(
        heating_type
    )


def harvest(state: LagState, heating_type: str | None, now_s: float) -> LagState:
    """Record readings for events whose window has just completed.

    Call once per pushed sample. Each event is decided exactly once (tracked by
    its absolute sample index), so re-scanning the buffer every cycle never
    re-records a reading, however the buffer has shifted. A heating-type change
    clears the readings, which described a different house.
    """
    current_type = _normalise_type(heating_type)
    readings = state.readings
    if not _same_type(state, heating_type):
        readings = ()
    step = max(1.0, state.step_minutes)
    base = state.sample_counter - len(state.indoors_c)
    mark = state.evaluated_through
    new: list[LagReading] = []
    for index, rising, steps in sorted(
        _decide_events(state, _window_steps(step)), key=lambda e: (e[0], e[1])
    ):
        absolute = base + index
        if absolute <= state.evaluated_through:
            continue
        mark = max(mark, absolute)
        if steps is not None:
            new.append(
                LagReading(
                    direction="rise" if rising else "fall",
                    minutes=max(step, steps * step),
                    at_s=now_s,
                )
            )
    return replace(
        state,
        readings=_prune((*readings, *new), now_s),
        heating_type=current_type,
        evaluated_through=mark,
    )


def _blend(default: float, readings: list[float]) -> tuple[float, bool]:
    """(value, floored) from a default and the clean readings in minutes."""
    if not readings:
        return default, False
    n = len(readings)
    weight = n / (n + BLEND_K)
    value = default + weight * (_median(readings) - default)
    floor = FLOOR_FRACTION * default
    return (floor, True) if value < floor else (value, False)


def _describe(
    name: str, minutes: float, default: float, n: int, floored: bool
) -> str:
    if n == 0:
        return f"{name} {minutes:.0f} min (default, no clean readings yet)"
    plural = "reading" if n == 1 else "readings"
    note = ", held at floor" if floored else ""
    return (
        f"{name} {minutes:.0f} min (default {default:.0f}, blended with "
        f"{n} clean {plural}{note})"
    )


def estimate(
    state: LagState, heating_type: str | None, now_s: float | None = None
) -> LagResult:
    """Blend the heating-type default with the clean readings.

    Never raises and never returns a non-positive time: every downstream
    consumer divides or schedules by these. With no readings the value is
    exactly the fallback. `now_s`, when given, ignores expired readings.
    """
    fb_rise, fb_fall = fallback_minutes(heating_type)
    samples = len(state.indoors_c)
    readings = (
        _prune(state.readings, now_s) if _same_type(state, heating_type) else ()
    )
    rise = [r.minutes for r in readings if r.direction == "rise"]
    fall = [r.minutes for r in readings if r.direction == "fall"]
    rise_minutes, rise_floored = _blend(fb_rise, rise)
    fall_minutes, fall_floored = _blend(fb_fall, fall)

    window = _window_steps(max(1.0, state.step_minutes))
    if not rise and not fall and samples < window + 2:
        reason = (
            f"Building history ({samples} samples); using "
            f"{heating_type or DEFAULT_HEATING_TYPE} defaults of "
            f"{fb_rise:.0f} min rise / {fb_fall:.0f} min fall"
        )
    else:
        reason = "; ".join(
            (
                _describe("rise", rise_minutes, fb_rise, len(rise), rise_floored),
                _describe("fall", fall_minutes, fb_fall, len(fall), fall_floored),
            )
        )

    return LagResult(
        rise_minutes=rise_minutes,
        fall_minutes=fall_minutes,
        rise_measured=bool(rise),
        fall_measured=bool(fall),
        rise_events=len(rise),
        fall_events=len(fall),
        samples=samples,
        reason=reason,
        rise_default_minutes=fb_rise,
        fall_default_minutes=fb_fall,
        rise_source="blended" if rise else "default",
        fall_source="blended" if fall else "default",
    )
