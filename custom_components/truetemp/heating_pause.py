"""Timed external heating requests and bounded recovery, without HA dependencies."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

DEFAULT_DURATION_S = 2 * 3600
MAX_DURATION_S = 24 * 3600


@dataclass(frozen=True)
class HeatingPause:
    started_s: float = 0.0
    deadline_s: float = 0.0
    recovery_until_s: float = 0.0
    settle_until_s: float = 0.0
    release_reason: str | None = None
    assisted: bool = False

    @property
    def active(self) -> bool:
        return self.deadline_s > 0.0

    def learning_frozen(self, now_s: float) -> bool:
        return self.active or now_s < max(self.recovery_until_s, self.settle_until_s)


def request(state: HeatingPause, now_s: float, duration_s: float) -> HeatingPause:
    """Replace the timer without extending a continuous pause beyond one day."""
    if not math.isfinite(duration_s) or not 0 < duration_s <= MAX_DURATION_S:
        raise ValueError("Duration must be greater than zero and at most 24 hours")
    started = state.started_s if state.active and now_s < state.deadline_s else now_s
    return HeatingPause(started_s=started, deadline_s=min(
        now_s + duration_s, started + MAX_DURATION_S,
    ))


def settling_seconds(rise_hours: float) -> float:
    return min(6.0, max(0.25, rise_hours)) * 3600


def release(
    state: HeatingPause, now_s: float, rise_hours: float, reason: str,
) -> HeatingPause:
    if not state.active:
        return state
    window_s = min(6.0, max(2.0, 2.0 * rise_hours)) * 3600
    return HeatingPause(
        recovery_until_s=now_s + window_s,
        settle_until_s=now_s + settling_seconds(rise_hours),
        release_reason=reason,
    )


def advance(
    state: HeatingPause, *, now_s: float, enabled: bool, available: bool,
    indoor_c: float | None, coldest_c: float | None, target_c: float,
    minimum_c: float, rise_hours: float, recovery_allowed: bool,
) -> tuple[HeatingPause, float]:
    """Return state and temporary indoor-equivalent assistance (negative = heat)."""
    valid = available and all(
        value is not None and math.isfinite(value) for value in (indoor_c, coldest_c)
    )
    if state.active:
        reason = (
            "compensation disabled" if not enabled else
            "indoor sensor unavailable" if not valid else
            "minimum indoor temperature" if coldest_c <= minimum_c else
            "duration elapsed" if now_s >= state.deadline_s else None
        )
        if reason is None:
            return state, 0.0
        state = release(state, now_s, rise_hours, reason)
    if state.recovery_until_s:
        if (
            not enabled or not recovery_allowed or now_s >= state.recovery_until_s
        ):
            ended_s = min(now_s, state.recovery_until_s)
            return replace(
                state, recovery_until_s=0.0,
                settle_until_s=max(state.settle_until_s,
                                   ended_s + settling_seconds(rise_hours)),
            ), 0.0
        if valid:
            if target_c - indoor_c <= 0.1:
                if state.assisted or now_s >= state.settle_until_s:
                    return replace(
                        state, recovery_until_s=0.0,
                        settle_until_s=(now_s + settling_seconds(rise_hours)
                                        if state.assisted else state.settle_until_s),
                    ), 0.0
                return state, 0.0
            return replace(state, assisted=True), -min(2.0, 2.0 * (target_c - indoor_c))
    return state, 0.0
