"""Combine 1-5 indoor sensor readings into the single scalar every downstream
consumer (learner, lag, heuristic) sees.

Pure module: standard library only, so it is importable and unit-testable
without Home Assistant installed — see `lag.py`'s docstring for why that
matters. `coordinator.py` does the per-sensor state lookup, unit conversion
and unusable-sensor filtering; by the time a reading reaches `aggregate()`
here it is already a plain float in °C. See docs/plan_multi_indoor_sensor.md
§2.2 for why only these two modes exist.
"""

from __future__ import annotations

MODE_AVERAGE = "average"
MODE_LOWEST = "lowest"


def aggregate(readings: list[float], mode: str) -> float | None:
    """`None` for an empty list — the caller's "nothing usable" case."""
    if not readings:
        return None
    if mode == MODE_LOWEST:
        return min(readings)
    return sum(readings) / len(readings)
