"""Sun pre-cool hold: keeps a sunny morning's pre-cool going until the sun peaks.

The pre-cool alone fades as the sun arrives (the reactive sun term takes
over), but the house keeps warming for 2-3 h after the sun peaks. So the
highest pre-cool of the spell is held until the sun has passed its peak.
Released early, to avoid cooling the house for sun that did not come, when the
live sun is well below what the forecast said for now, when the house is below
target, or after a long timeout.

Pure (no Home Assistant): the coordinator owns the state between cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Release once live sun is below this fraction of the best of the spell.
PAST_PEAK_FRACTION = 0.8
# Release once live sun is below this fraction of the forecast's value for now,
# when that forecast value is above the minimum.
CLOUD_FRACTION = 0.5
MIN_FORECAST_EFFECT = 0.15
MAX_AGE = timedelta(hours=9)
# A hold at or below this is not a hold (same value as WEATHER_PRERAMP_EPS_C).
MIN_HOLD_C = 0.2


@dataclass(frozen=True)
class SunHoldState:
    hold_c: float = 0.0
    peak_effect: float = 0.0
    since: datetime | None = None


def next_hold(
    state: SunHoldState,
    *,
    now: datetime,
    enabled: bool,
    prev_precool_c: float,
    live_effect: float,
    forecast_now: float | None,
    forecast_ahead_max: float | None,
    indoor_c: float | None,
    target_c: float,
) -> tuple[SunHoldState, float]:
    """`forecast_now` is the forecast's cloud for this hour evaluated at the
    live sun position; `forecast_ahead_max` the best forecast sun still to come.

    Return the new state and the level to hold the pre-cool at (0 = none)."""
    idle = SunHoldState()
    if not enabled:
        return idle, 0.0
    hold_c, since = state.hold_c, state.since
    if prev_precool_c > hold_c:
        hold_c = prev_precool_c
        if since is None:
            since = now
    if hold_c <= MIN_HOLD_C:
        return idle, 0.0
    peak = max(state.peak_effect, live_effect)
    release = (
        (peak > 0.0 and live_effect < PAST_PEAK_FRACTION * peak)
        or (
            forecast_now is not None
            and forecast_now > MIN_FORECAST_EFFECT
            and live_effect < CLOUD_FRACTION * forecast_now
        )
        # Sun not up yet and the forecast no longer shows any coming.
        or (
            peak <= 0.0
            and forecast_ahead_max is not None
            and forecast_ahead_max < MIN_FORECAST_EFFECT
        )
        or (indoor_c is not None and indoor_c < target_c)
        or (since is not None and now - since > MAX_AGE)
    )
    if release:
        return idle, 0.0
    return SunHoldState(hold_c, peak, since), hold_c
