"""Holiday setback: a temporary target override, timed off the house's own
measured lag rather than a manual dial.

Pure module: standard library only (`datetime`), no dependency on any other
module in this package (not even `const.py`), so it stays importable and
unit-testable without Home Assistant installed — same discipline as
`lag.py`/`learner.py`/`heuristic.py`, for the same reason: CI never installs
`homeassistant`.

## What this is, architecturally

Same shape as the price-braking/pre-charge feature in `heuristic.py`: a
temporary target override, layered underneath the existing price/comfort-floor
logic rather than a separate control path. `resolve()` below only ever
produces a `target_c` for the coordinator to feed into whatever
`indoor_target_c` currently feeds — it never touches price, the learner or the
lag estimator directly. That means all of those keep working unmodified during
a holiday; they just see a different target for a while, exactly as they
already tolerate the occupant changing `indoor_target_c` by hand at any time.

## Why the ramp is paced off `rise_hours`, not a fixed duration

A fixed "ramp for N hours before return" would either overshoot a fast house
(wasting savings) or undershoot a slow one (tripping the heat pump's backup
heat, which is the whole failure mode this exists to avoid). `rise_hours`
(from `lag.py`, measured or fallback) is how long this specific house takes to
close a temperature gap, so scaling the ramp duration off it — the same shape
as `learner.py`'s `recovery_window_h = lag.rise_hours * TAU_I_LAG_MULTIPLE` —
makes the pacing self-calibrating per house rather than a number somebody
guessed.

## Why the single sharp drop at `start_at` is fine

A gradual ramp (a few tenths of a degree per 15-minute cycle) is far below
`lag.py`'s `EVENT_MIN_MOVE_C`/`EVENT_MAX_STEPS` step-detection thresholds, so
it correctly reads as drift rather than a spurious "target step" event — no
change needed to the lag estimator. The one sharp drop at `start_at` *will*
register as a genuine target-step event, which is a free, clean lag
measurement, not a problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# Fixed local time-of-day the house must be back at the normal target by,
# on the end date. Not user-configurable — see the plan's "Dates" note: two
# `date` entities, not date+time, to keep the card simple.
HOLIDAY_RETURN_TIME = time(15, 0)

# Safety margin over the measured rise time when sizing the ramp: the ramp
# runs slower than a bare recovery would, so a step-shaped demand never lands
# on the pump the way a single lag-window recovery legitimately can.
RAMP_HOURS_PER_DEGREE_MULTIPLE = 2.0
MIN_RAMP_HOURS = 1.0

DEFAULT_HOLIDAY_TARGET_C = 16.0
# Holiday setback's own absolute floor — deliberately independent of
# `comfort_min_c`. That value bounds how cold price compensation may sag the
# house while someone is actually living in it; nobody is home during a
# holiday, so the setback is allowed to sag well past it. This is the only
# bound left, a frost-safety minimum rather than a comfort one.
HOLIDAY_TARGET_MIN_C = 5.0

HOLIDAY_PHASE_INACTIVE = "inactive"
HOLIDAY_PHASE_INVALID = "invalid"
HOLIDAY_PHASE_SCHEDULED = "scheduled"
HOLIDAY_PHASE_SETBACK = "setback"
HOLIDAY_PHASE_RAMPING = "ramping"
HOLIDAY_PHASE_RECOVERING = "recovering"
HOLIDAY_PHASE_DONE = "done"
HOLIDAY_PHASES = (
    HOLIDAY_PHASE_INACTIVE,
    HOLIDAY_PHASE_INVALID,
    HOLIDAY_PHASE_SCHEDULED,
    HOLIDAY_PHASE_SETBACK,
    HOLIDAY_PHASE_RAMPING,
    HOLIDAY_PHASE_RECOVERING,
    HOLIDAY_PHASE_DONE,
)


@dataclass(frozen=True)
class HolidayResult:
    """One cycle's holiday state. Doubles as the status sensor's attribute
    schema."""

    phase: str
    # The base target for THIS cycle — feed straight into what
    # `indoor_target_c` currently feeds. Equal to `normal_target_c` whenever
    # the holiday isn't actively sagging the house (inactive/invalid/
    # scheduled/done), so a caller that always wires this through gets zero
    # behaviour change when holiday mode is unused.
    target_c: float
    # When the target steps down to `holiday_target_c` — midnight on
    # `start_date`. This is the "true" start of the holiday setback (a single
    # sharp drop, see module docstring), distinct from `ramp_start_at` below,
    # which is when the *return* ramp begins near the other end of the trip.
    start_at: datetime | None
    ramp_start_at: datetime | None
    return_at: datetime | None
    hours_needed: float
    on_track: bool
    reason: str


def _inactive(target_c: float, phase: str, reason: str) -> HolidayResult:
    return HolidayResult(
        phase=phase,
        target_c=target_c,
        start_at=None,
        ramp_start_at=None,
        return_at=None,
        hours_needed=0.0,
        on_track=True,
        reason=reason,
    )


def resolve(
    now: datetime,
    armed: bool,
    start_date: date | None,
    end_date: date | None,
    normal_target_c: float,
    holiday_target_c: float,
    rise_hours: float,
) -> HolidayResult:
    """Resolve this cycle's holiday phase and target.

    `now` must be a naive local datetime (no tzinfo), matching `start_at`/
    `ramp_start_at`/`return_at` below, which are all built from naive
    `date`/`time` objects via `datetime.combine`. Comparing a naive datetime
    against an aware one raises `TypeError`, so a caller sitting on an aware
    "now" (e.g. Home Assistant's `dt_util.now()`) must strip its tzinfo
    first — this module has no timezone of its own to convert against.

    `holiday_target_c` must already be clamped to `HOLIDAY_TARGET_MIN_C` by
    the caller (`max(holiday_target_c, HOLIDAY_TARGET_MIN_C)`) — deliberately
    NOT `comfort_min_c`, which is a separate, occupied-house floor this
    module has no notion of; see `HOLIDAY_TARGET_MIN_C`'s docstring above.
    That clamp is a cross-cutting concern that belongs at the coordinator,
    not here.

    Never raises: a missing/invalid date, or a house whose target for some
    reason wants a setback deeper than shallow, all degrade to a safe phase
    (`inactive`/`invalid`) with `target_c = normal_target_c` rather than
    surfacing an exception into the update loop.

    Validates `armed`/dates, then builds `start_at`/`return_at` from the
    dates and delegates everything else (the ramp/phase state machine) to
    `resolve_window()`, which is also what the multi-plan `vacation.py`
    resolver calls directly with its own occurrence-derived `start_at`/
    `return_at`.
    """
    if not armed:
        return _inactive(normal_target_c, HOLIDAY_PHASE_INACTIVE, "Holiday mode not armed")
    if start_date is None or end_date is None:
        return _inactive(
            normal_target_c, HOLIDAY_PHASE_INACTIVE, "Holiday mode armed but dates not set"
        )
    if end_date <= start_date:
        return _inactive(
            normal_target_c,
            HOLIDAY_PHASE_INVALID,
            f"Return date {end_date} is not after start date {start_date}",
        )

    start_at = datetime.combine(start_date, time.min)
    return_at = datetime.combine(end_date, HOLIDAY_RETURN_TIME)

    return resolve_window(
        now=now,
        start_at=start_at,
        return_at=return_at,
        normal_target_c=normal_target_c,
        holiday_target_c=holiday_target_c,
        rise_hours=rise_hours,
    )


def ramp_hours_needed(delta_c: float, rise_hours: float) -> float:
    """How many hours a `delta_c`-sized return ramp should take, paced off
    this house's measured `rise_hours` — see the module docstring's "Why the
    ramp is paced off `rise_hours`" section.

    Shared by `resolve_window()`'s scheduled-return ramp and
    `vacation.py`'s manual-disarm return ramp (`start_return_ramp()`) so the
    two paths can never compute a different duration for the same-sized
    recovery.
    """
    if delta_c <= 0.0:
        return MIN_RAMP_HOURS
    return max(MIN_RAMP_HOURS, delta_c * rise_hours * RAMP_HOURS_PER_DEGREE_MULTIPLE)


def resolve_window(
    now: datetime,
    start_at: datetime,
    return_at: datetime,
    normal_target_c: float,
    holiday_target_c: float,
    rise_hours: float,
) -> HolidayResult:
    """Resolve the phase/ramp state machine for one concrete `[start_at,
    return_at)` window.

    This is `resolve()`'s engine with the window handed in directly instead
    of being built from `start_date`/`end_date`/`HOLIDAY_RETURN_TIME` — the
    same math, generalised so a caller with its own way of picking a window
    (e.g. `vacation.py`'s recurrence-driven occurrence picking) can reuse it
    without going through date-only args. `now`/`start_at`/`return_at` must
    all be naive local datetimes, same contract as `resolve()`. Never raises.
    """
    delta_c = normal_target_c - holiday_target_c
    hours_needed = ramp_hours_needed(delta_c, rise_hours)

    ramp_start_at = return_at - timedelta(hours=hours_needed)
    on_track = True
    if ramp_start_at < start_at:
        ramp_start_at = start_at
        on_track = False

    if now < start_at:
        return HolidayResult(
            phase=HOLIDAY_PHASE_SCHEDULED,
            target_c=normal_target_c,
            start_at=start_at,
            ramp_start_at=ramp_start_at,
            return_at=return_at,
            hours_needed=hours_needed,
            on_track=on_track,
            reason=(
                f"Holiday scheduled from {start_at:%Y-%m-%d %H:%M} "
                f"to {return_at:%Y-%m-%d %H:%M}"
            ),
        )
    if now < ramp_start_at:
        return HolidayResult(
            phase=HOLIDAY_PHASE_SETBACK,
            target_c=holiday_target_c,
            start_at=start_at,
            ramp_start_at=ramp_start_at,
            return_at=return_at,
            hours_needed=hours_needed,
            on_track=on_track,
            reason=f"Holding holiday setback of {holiday_target_c:.1f}°C",
        )
    if now < return_at:
        window_hours = (return_at - ramp_start_at).total_seconds() / 3600.0
        elapsed_hours = (now - ramp_start_at).total_seconds() / 3600.0
        fraction = 1.0 if window_hours <= 0.0 else min(1.0, elapsed_hours / window_hours)
        target_c = holiday_target_c + delta_c * fraction
        reason = f"Ramping back to {normal_target_c:.1f}°C by {return_at:%Y-%m-%d %H:%M}"
        if not on_track:
            reason += " (insufficient runway — ramp started immediately at holiday start)"
        return HolidayResult(
            phase=HOLIDAY_PHASE_RAMPING,
            target_c=target_c,
            start_at=start_at,
            ramp_start_at=ramp_start_at,
            return_at=return_at,
            hours_needed=hours_needed,
            on_track=on_track,
            reason=reason,
        )

    return HolidayResult(
        phase=HOLIDAY_PHASE_DONE,
        target_c=normal_target_c,
        start_at=start_at,
        ramp_start_at=ramp_start_at,
        return_at=return_at,
        hours_needed=hours_needed,
        on_track=on_track,
        reason="Holiday complete, back at normal target",
    )
