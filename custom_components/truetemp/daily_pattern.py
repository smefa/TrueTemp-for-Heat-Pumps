"""The house's daily temperature pattern, learned per hour of the day.

Pure module: standard library only, no dependency on any other module in this
package, so it is importable and unit-testable without Home Assistant.

## Why this exists

The learner is a feedback controller: it reacts to an error after the room has
already drifted, and with hours of response time it is always late for anything
that happens at the same clock time every day. Cooking, occupancy, appliances,
a tariff the pump follows on its own: these come back every evening, so they
can be anticipated rather than chased.

## Shadow mode: learn and score, never act

This is step one. The module learns the pattern and works out the offset it
*would* add, and the coordinator logs and displays that, but nothing here
touches the output. The point is to find out, house by house, whether the
pattern is stable enough to act on before letting it act.

## What is learned

Every unfrozen learner step contributes the room's warmth (indoor minus
target, positive meaning too warm) to one of 24 hourly bins in local time.

- **Detrended first.** A slow three-day average of the warmth is subtracted
  before binning, so a cold week or a learner still converging does not show
  up as "every hour is cold". Three days rather than one, because a one-day
  average still ripples with the daily cycle and would eat part of the very
  pattern it is meant to expose. The level is the learner's job; this module
  only learns the shape of the day around it.
- **Centred.** The shape is each bin minus the mean of the bins, so it adds
  nothing over a whole day and cannot fight the learner's integral.
- **Long memory, per bin.** Each bin is a running average that becomes an
  exponential one after `MEMORY_DAYS`, so a single odd day moves it little
  and a changed routine is followed within a few weeks.
- **Only consistent hours count.** A bin is used only once it has
  `MIN_DAYS` days of samples and its shape clears `CONFIDENCE_Z` standard
  errors of its own day-to-day scatter. A house without a daily pattern
  learns bins that never clear this, and its shadow offset stays zero.

## The shadow offset

The offset that would be added now is the confident shape at *now plus the
response time*, because heat commanded now arrives that much later. It is
converted with the same indoor-to-offset ratio the learner uses, so a positive
value means less heat, ahead of an hour that is usually too warm. It is
capped at `FF_CAP_C`; the uncapped value is reported too, so the logs show
whether the cap would bind.

## The score

Once at least one hour is trusted, each sample is scored before it updates
its bin: how much of the detrended warmth the shape would have explained at
that moment, using only what had been learned before it. `potential_improvement_pct` is the
resulting drop in RMS. It assumes perfect timing and full response, so it
is an upper bound on what acting could gain, not a forecast.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

HOURS = 24

# Each bin forgets on this horizon once it has this many days of samples.
MEMORY_DAYS = 14.0
# Days of samples a bin needs before its shape is trusted at all.
MIN_DAYS = 7.0
# How many standard errors of its day-to-day scatter a bin's shape must clear.
CONFIDENCE_Z = 2.0
# Shapes smaller than this are noise as far as heating is concerned.
MIN_SHAPE_C = 0.05
# A shape needs most of the day covered, or centring it means little.
MIN_COVERED_HOURS = 18
# Time constant of the slow level that is removed before binning.
LEVEL_HOURS = 72.0
# Horizon of the shadow score.
SCORE_DAYS = 14.0
# Days of scored samples before a score is reported.
MIN_SCORE_DAYS = 3.0

# Fraction of the predicted warmth the offset would cancel.
FF_GAIN = 1.0
# Largest offset the pattern would ever add, in either direction.
FF_CAP_C = 2.0


@dataclass(frozen=True)
class PatternBin:
    """Running statistics of the detrended warmth in one hour of the day."""

    mean_c: float = 0.0
    mean_sq_c2: float = 0.0
    samples: int = 0


@dataclass(frozen=True)
class DailyPatternState:
    bins: tuple[PatternBin, ...]
    level_c: float | None = None
    score_base_sq_c2: float = 0.0
    score_shadow_sq_c2: float = 0.0
    score_samples: int = 0


@dataclass(frozen=True)
class DailyPatternResult:
    """What the pattern would do now, and how much it would have helped."""

    # Centred warmth per hour, None where the hour has no samples yet.
    shape_c: tuple[float | None, ...]
    # Whether each hour's shape is consistent enough to act on.
    confident: tuple[bool, ...]
    confident_hours: int
    # The hour (local, fractional) the shadow offset is aiming at.
    lead_hour: float
    shadow_offset_c: float
    shadow_offset_uncapped_c: float
    potential_improvement_pct: float | None
    reason: str


def initial_state() -> DailyPatternState:
    return DailyPatternState(bins=tuple(PatternBin() for _ in range(HOURS)))


def _hour_index(hour: float) -> int:
    return int(math.floor(hour % HOURS)) % HOURS


def _samples_per_hour(step_minutes: float) -> float:
    return max(1.0, 60.0 / max(step_minutes, 1e-6))


def _shape(state: DailyPatternState, step_minutes: float) -> tuple[
    tuple[float | None, ...], tuple[bool, ...]
]:
    """The centred shape per hour and whether each hour is trusted."""
    covered = [b for b in state.bins if b.samples > 0]
    if not covered:
        return tuple(None for _ in state.bins), tuple(False for _ in state.bins)
    centre = sum(b.mean_c for b in covered) / len(covered)
    shape = tuple(b.mean_c - centre if b.samples > 0 else None for b in state.bins)
    if len(covered) < MIN_COVERED_HOURS:
        return shape, tuple(False for _ in state.bins)

    per_hour = _samples_per_hour(step_minutes)
    min_samples = MIN_DAYS * per_hour
    memory_samples = MEMORY_DAYS * per_hour
    confident: list[bool] = []
    for b, s in zip(state.bins, shape):
        if s is None or b.samples < min_samples or abs(s) < MIN_SHAPE_C:
            confident.append(False)
            continue
        # Samples inside one hour of one day are not independent; days are.
        days = min(b.samples, memory_samples) / per_hour
        spread = math.sqrt(max(0.0, b.mean_sq_c2 - b.mean_c * b.mean_c))
        standard_error = spread / math.sqrt(max(days, 1.0))
        confident.append(abs(s) >= CONFIDENCE_Z * standard_error)
    return shape, tuple(confident)


def _acting_shape_at(
    shape: tuple[float | None, ...], confident: tuple[bool, ...], hour: float
) -> float:
    """The trusted shape at a fractional hour, interpolated between bin centres.

    An untrusted neighbour counts as zero, so the value fades towards an
    untrusted hour instead of stepping.
    """
    position = (hour - 0.5) % HOURS
    lower = int(math.floor(position)) % HOURS
    upper = (lower + 1) % HOURS
    weight = position - math.floor(position)

    def value(i: int) -> float:
        s = shape[i]
        return s if confident[i] and s is not None else 0.0

    return (1.0 - weight) * value(lower) + weight * value(upper)


def update(
    state: DailyPatternState,
    hour: float,
    warmth_c: float,
    step_minutes: float,
) -> DailyPatternState:
    """Add one unfrozen sample: `hour` is local time, `warmth_c` indoor minus target."""
    if not math.isfinite(warmth_c) or not math.isfinite(hour):
        return state
    per_hour = _samples_per_hour(step_minutes)
    level_alpha = 1.0 / (LEVEL_HOURS * per_hour)
    level = warmth_c if state.level_c is None else (
        state.level_c + level_alpha * (warmth_c - state.level_c)
    )
    detrended = warmth_c - level

    # Score with what was known before this sample, and only once something
    # is trusted: the weeks before that would only dilute it towards zero.
    shape, confident = _shape(state, step_minutes)
    base_sq = state.score_base_sq_c2
    shadow_sq = state.score_shadow_sq_c2
    score_samples = state.score_samples
    if any(confident):
        predicted = _acting_shape_at(shape, confident, hour)
        score_alpha = max(1.0 / (score_samples + 1), 1.0 / (SCORE_DAYS * HOURS * per_hour))
        base_sq += score_alpha * (detrended * detrended - base_sq)
        residual = detrended - FF_GAIN * predicted
        shadow_sq += score_alpha * (residual * residual - shadow_sq)
        score_samples += 1

    index = _hour_index(hour)
    b = state.bins[index]
    alpha = max(1.0 / (b.samples + 1), 1.0 / (MEMORY_DAYS * per_hour))
    updated = PatternBin(
        mean_c=b.mean_c + alpha * (detrended - b.mean_c),
        mean_sq_c2=b.mean_sq_c2 + alpha * (detrended * detrended - b.mean_sq_c2),
        samples=b.samples + 1,
    )
    bins = state.bins[:index] + (updated,) + state.bins[index + 1:]
    return replace(
        state,
        bins=bins,
        level_c=level,
        score_base_sq_c2=base_sq,
        score_shadow_sq_c2=shadow_sq,
        score_samples=score_samples,
    )


def evaluate(
    state: DailyPatternState,
    hour: float,
    lead_minutes: float,
    offset_per_indoor_c: float,
    step_minutes: float,
) -> DailyPatternResult:
    """The offset the pattern would add now, without adding it."""
    shape, confident = _shape(state, step_minutes)
    lead_hour = (hour + max(0.0, lead_minutes) / 60.0) % HOURS
    predicted = _acting_shape_at(shape, confident, lead_hour)
    # Too warm ahead means less heat now, which is a positive offset.
    uncapped = FF_GAIN * offset_per_indoor_c * predicted
    capped = max(-FF_CAP_C, min(FF_CAP_C, uncapped))
    confident_hours = sum(confident)

    per_hour = _samples_per_hour(step_minutes)
    improvement: float | None = None
    if state.score_samples >= MIN_SCORE_DAYS * HOURS * per_hour and state.score_base_sq_c2 > 0:
        ratio = math.sqrt(max(0.0, state.score_shadow_sq_c2) / state.score_base_sq_c2)
        improvement = (1.0 - ratio) * 100.0

    covered = sum(1 for b in state.bins if b.samples > 0)
    if covered < MIN_COVERED_HOURS:
        reason = f"Learning: {covered}/{HOURS} hours seen"
    elif confident_hours == 0:
        reason = "No consistent daily pattern yet"
    else:
        reason = (
            f"{confident_hours} consistent hours; would add {capped:+.2f}°C "
            f"for {int(lead_hour):02d}:{int((lead_hour % 1) * 60):02d}"
        )
    return DailyPatternResult(
        shape_c=shape,
        confident=confident,
        confident_hours=confident_hours,
        lead_hour=lead_hour,
        shadow_offset_c=capped,
        shadow_offset_uncapped_c=uncapped,
        potential_improvement_pct=improvement,
        reason=reason,
    )


def to_dict(state: DailyPatternState) -> dict:
    """JSON-safe form for the learned-state store."""
    return {
        "bins": [
            {"mean_c": b.mean_c, "mean_sq_c2": b.mean_sq_c2, "samples": b.samples}
            for b in state.bins
        ],
        "level_c": state.level_c,
        "score_base_sq_c2": state.score_base_sq_c2,
        "score_shadow_sq_c2": state.score_shadow_sq_c2,
        "score_samples": state.score_samples,
    }


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("not a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("not finite")
    return number


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("not a sample count")
    return value


def from_dict(raw: object) -> DailyPatternState | None:
    """The stored pattern, or None when absent or unreadable.

    All or nothing: a pattern with one corrupt hour would centre wrongly, and
    relearning costs a few weeks of shadow data, nothing that controls heat.
    """
    if not isinstance(raw, dict):
        return None
    try:
        raw_bins = raw["bins"]
        if not isinstance(raw_bins, list) or len(raw_bins) != HOURS:
            return None
        bins = tuple(
            PatternBin(
                mean_c=_finite(b["mean_c"]),
                mean_sq_c2=_finite(b["mean_sq_c2"]),
                samples=_count(b["samples"]),
            )
            for b in raw_bins
        )
        level = raw.get("level_c")
        return DailyPatternState(
            bins=bins,
            level_c=None if level is None else _finite(level),
            score_base_sq_c2=_finite(raw.get("score_base_sq_c2", 0.0)),
            score_shadow_sq_c2=_finite(raw.get("score_shadow_sq_c2", 0.0)),
            score_samples=_count(raw.get("score_samples", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None
