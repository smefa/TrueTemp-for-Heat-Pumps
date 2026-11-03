"""Bounded, temporary assistance after an occupied target change."""

from __future__ import annotations

from dataclasses import dataclass, replace

MIN_TARGET_STEP_C = 0.1
RECOVERY_GAIN = 2.0
MAX_ADJUSTMENT_C = 2.0
MIN_WINDOW_HOURS = 2.0
MAX_WINDOW_HOURS = 6.0


@dataclass(frozen=True)
class TargetRecovery:
    previous_target_c: float
    was_enabled: bool = False
    direction: float = 0.0
    deadline_s: float = 0.0
    within_c: float = 0.1


def resolve(
    state: TargetRecovery,
    *,
    now_s: float,
    target_c: float,
    indoor_temp_c: float | None,
    indoor_available: bool,
    enabled: bool,
    rise_hours: float,
) -> tuple[TargetRecovery, float]:
    """Return state and signed indoor-equivalent spoof: positive means less heat."""
    target_step_c = target_c - state.previous_target_c
    settled = TargetRecovery(target_c, was_enabled=enabled)
    updated = replace(state, previous_target_c=target_c, was_enabled=enabled)
    if not enabled or not state.was_enabled:
        return settled, 0.0
    if abs(target_step_c) >= MIN_TARGET_STEP_C - 1e-9:
        window_hours = min(MAX_WINDOW_HOURS, max(MIN_WINDOW_HOURS, 2.0 * rise_hours))
        updated = TargetRecovery(
            previous_target_c=target_c,
            was_enabled=True,
            direction=1.0 if target_step_c > 0.0 else -1.0,
            deadline_s=now_s + window_hours * 3600.0,
            within_c=min(0.1, max(0.05, abs(target_step_c) * 0.2)),
        )
    if updated.direction == 0.0 or now_s >= updated.deadline_s:
        return settled, 0.0
    if not indoor_available or indoor_temp_c is None:
        return updated, 0.0
    remaining_c = updated.direction * (target_c - indoor_temp_c)
    if remaining_c <= updated.within_c + 1e-9:
        return settled, 0.0
    adjustment_c = -updated.direction * min(MAX_ADJUSTMENT_C, RECOVERY_GAIN * remaining_c)
    return updated, adjustment_c
