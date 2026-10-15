"""Outdoor-reading jump guard: hold a reading that jumps until it proves real.

Pure module: standard library only, so it is unit-testable without Home
Assistant installed.

## Why

The value cannot tell a glitch from a real reading — 0.0 degC is an ordinary
winter temperature. What gives a glitch away is how it arrives. Seen on a real
install during a restart: a template sensor read 5.3, then 0 for five seconds
while its source was unavailable, then 8.3 for nineteen seconds, then 5.3 again.
Each of those was published to the heat pump as if it were the weather.

Outdoor air does not move several degrees in seconds and then move back. So a
reading more than `JUMP_C` from the last good one is held: the last good value
is used instead until the new reading has stayed put for `CONFIRM_MINUTES`.
A real change only arrives that much later. A real 0.0 arrives gradually and
never trips the guard.

## When it does not settle

A reading that keeps jumping is not stuck forever on a value that may no longer
be true. After `WARN_AFTER_MINUTES` the caller is told to warn, and after
`GIVE_UP_AFTER_MINUTES` the result carries no value, which the caller treats
exactly like an unavailable sensor.

## Across restarts

The glitch above happened at a restart, which is exactly when an in-memory
reference would be missing. So the last good value and its time are persisted
by the caller. A reference older than `REFERENCE_MAX_AGE_MINUTES` is not
trusted, and the first reading is then taken as it is.
"""

from __future__ import annotations

from dataclasses import dataclass

JUMP_C = 2.0
# How close a later reading must be to the held one to count as the same new
# value while it is being confirmed.
CONFIRM_TOLERANCE_C = 0.5
CONFIRM_MINUTES = 5.0
WARN_AFTER_MINUTES = 15.0
GIVE_UP_AFTER_MINUTES = 60.0
REFERENCE_MAX_AGE_MINUTES = 60.0


@dataclass(frozen=True)
class GuardState:
    """The last good reading, and the jump being held, if any."""

    accepted_c: float | None = None
    accepted_at_s: float | None = None
    pending_c: float | None = None
    pending_since_s: float | None = None
    held_since_s: float | None = None


@dataclass(frozen=True)
class GuardResult:
    """What to use this cycle. `value_c` is None once the guard gives up."""

    value_c: float | None
    reading_c: float
    held: bool
    held_minutes: float = 0.0

    @property
    def warn(self) -> bool:
        return self.held and self.held_minutes >= WARN_AFTER_MINUTES


def initial_state() -> GuardState:
    return GuardState()


def source_lost(state: GuardState) -> GuardState:
    """Forget a confirmation in progress when the sensor stops reporting.

    Time spent unavailable says nothing about whether the held value is real,
    so it must not count towards confirming it. The last good value and how
    long the jump has been held are kept.
    """
    return GuardState(
        accepted_c=state.accepted_c,
        accepted_at_s=state.accepted_at_s,
        held_since_s=state.held_since_s,
    )


def confirm_due_s(state: GuardState) -> float | None:
    """When a held reading becomes confirmable, so the caller can re-check it then."""
    if state.pending_since_s is None:
        return None
    return state.pending_since_s + CONFIRM_MINUTES * 60.0


def _accept(reading_c: float, now_s: float) -> tuple[GuardState, GuardResult]:
    return (
        GuardState(accepted_c=reading_c, accepted_at_s=now_s),
        GuardResult(value_c=reading_c, reading_c=reading_c, held=False),
    )


def check(state: GuardState, reading_c: float, now_s: float) -> tuple[GuardState, GuardResult]:
    """Pass a reading through the guard. `now_s` is wall-clock epoch seconds."""
    if state.accepted_c is None or state.accepted_at_s is None:
        return _accept(reading_c, now_s)
    if (
        state.held_since_s is None
        and now_s - state.accepted_at_s > REFERENCE_MAX_AGE_MINUTES * 60.0
    ):
        return _accept(reading_c, now_s)
    if abs(reading_c - state.accepted_c) <= JUMP_C:
        return _accept(reading_c, now_s)

    held_since_s = state.held_since_s if state.held_since_s is not None else now_s
    if (
        state.pending_c is not None
        and state.pending_since_s is not None
        and abs(reading_c - state.pending_c) <= CONFIRM_TOLERANCE_C
    ):
        pending_c, pending_since_s = state.pending_c, state.pending_since_s
    else:
        pending_c, pending_since_s = reading_c, now_s

    if now_s - pending_since_s >= CONFIRM_MINUTES * 60.0:
        return _accept(reading_c, now_s)

    held_minutes = (now_s - held_since_s) / 60.0
    new_state = GuardState(
        accepted_c=state.accepted_c,
        accepted_at_s=state.accepted_at_s,
        pending_c=pending_c,
        pending_since_s=pending_since_s,
        held_since_s=held_since_s,
    )
    value_c = None if held_minutes >= GIVE_UP_AFTER_MINUTES else state.accepted_c
    return new_state, GuardResult(
        value_c=value_c, reading_c=reading_c, held=True, held_minutes=held_minutes
    )
