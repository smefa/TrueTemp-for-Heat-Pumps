"""Measured excess warmth detection, independent of Home Assistant.

Times use the coordinator's monotonic clock. State is deliberately not persisted.
Flat but reporting sensors are valid; freshness belongs to the input reader.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

RATE_WINDOW_S = 15 * 60
MAX_GAP_S = 20 * 60
SOLAR_MIN_GAIN_C = 0.3


@dataclass(frozen=True)
class ExcessHeatState:
    source: str | None = None
    slow_since_s: float | None = None
    fast_since_s: float | None = None
    samples: tuple[tuple[float, float], ...] = ()
    target_c: float | None = None
    sensors: frozenset[str] = frozenset()
    candidate_source: str | None = None
    fast_rise: bool = False
    holdoff_until_s: float = 0.0
    drift_c_per_h: float | None = None

    def learning_frozen(self, now_s: float) -> bool:
        return bool(self.source or self.fast_rise or now_s < self.holdoff_until_s)


def source_lost(state: ExcessHeatState, now_s: float, rise_hours: float) -> ExcessHeatState:
    """Drop detection evidence without discarding recovery after a source failure."""
    until = state.holdoff_until_s
    if state.source or state.fast_rise:
        until = max(until, now_s + max(0.0, rise_hours) * 3600)
    return ExcessHeatState(holdoff_until_s=until)


def advance(
    state: ExcessHeatState,
    *,
    now_s: float,
    indoor_c: float | None,
    target_c: float,
    sensors: frozenset[str],
    available: bool,
    fireplace_enabled: bool,
    solar_enabled: bool,
    solar_gain_c: float,
    solar_falling: bool,
    excluded: bool,
    rise_hours: float,
    margin_c: float = 1.5,
) -> ExcessHeatState:
    """Advance timers on every refresh; use a 15-minute observed rise window.

    No evidence is bridged over a gap >20 minutes, sensor/target changes or
    deliberate heating. Every configured room must be available at the caller.
    Solar uses the existing learner's meaningful-gain threshold, rather than
    requiring 2 C of modelled gain (which missed the recorded morning events).
    """
    valid = available and indoor_c is not None and math.isfinite(indoor_c)
    discontinuity = (
        state.target_c != target_c or state.sensors != sensors
        or bool(state.samples and (
            now_s < state.samples[-1][0] or now_s - state.samples[-1][0] > MAX_GAP_S
        ))
    )
    solar_present = solar_enabled and solar_gain_c > 0.05
    solar_eligible = solar_enabled and solar_gain_c >= SOLAR_MIN_GAIN_C
    candidate = "solar" if solar_eligible else "fireplace" if fireplace_enabled else None
    release = bool(state.source and (
        not valid or excluded or discontinuity
        or (state.source == "fireplace" and (
            not fireplace_enabled or indoor_c <= target_c + 0.5
        ))
        or (state.source == "solar" and (
            not solar_present or indoor_c <= target_c + 0.5
            or (solar_falling and indoor_c <= target_c + 1.0)
        ))
    ))
    holdoff = state.holdoff_until_s
    if release or (state.fast_rise and (not valid or excluded or discontinuity)):
        holdoff = max(holdoff, now_s + max(0.0, rise_hours) * 3600)
    if not valid or excluded or discontinuity or release:
        state = ExcessHeatState(holdoff_until_s=holdoff, target_c=target_c, sensors=sensors)
        # A reset must not start a new timer on the same observation.
        if not valid or excluded or release:
            return state

    if state.source:
        return replace(state, samples=((now_s, indoor_c),))

    if candidate is None:
        if state.fast_rise:
            holdoff = max(holdoff, now_s + max(0.0, rise_hours) * 3600)
        return ExcessHeatState(holdoff_until_s=holdoff, target_c=target_c, sensors=sensors)

    samples = tuple((t, v) for t, v in state.samples if now_s - t <= 2 * RATE_WINDOW_S)
    older = [(t, v) for t, v in samples if now_s - t >= RATE_WINDOW_S]
    drift = ((indoor_c - older[-1][1]) * 3600 / (now_s - older[-1][0])) if older else None
    fast = drift is not None and drift >= 1.0 - 1e-9
    if state.fast_rise and not fast:
        holdoff = max(holdoff, now_s + max(0.0, rise_hours) * 3600)
    same_source = candidate == state.candidate_source
    slow_since = state.slow_since_s if same_source else None
    fast_since = state.fast_since_s if same_source else None
    slow_since = (now_s if slow_since is None else slow_since) if (
        indoor_c >= target_c + margin_c
    ) else None
    fast_since = (now_s if fast_since is None else fast_since) if (
        fast and indoor_c >= target_c + 1.0
    ) else None
    triggered = (
        slow_since is not None and now_s - slow_since >= 45 * 60
    ) or (fast_since is not None and now_s - fast_since >= 15 * 60)
    # Coalesce same-time refreshes; history remains bounded under event bursts.
    if not samples or now_s - samples[-1][0] >= 60:
        samples = samples + ((now_s, indoor_c),)
    return ExcessHeatState(
        source=candidate if triggered else None,
        slow_since_s=slow_since, fast_since_s=fast_since,
        samples=samples, target_c=target_c, sensors=sensors,
        candidate_source=candidate, fast_rise=fast, holdoff_until_s=holdoff,
        drift_c_per_h=drift,
    )
