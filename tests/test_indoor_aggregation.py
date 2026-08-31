"""Unit tests for the pure indoor-sensor aggregation helper.

`aggregate()` only ever sees plain floats already converted to °C — unit
conversion and unusable-sensor filtering happen in `coordinator.py` before a
reading reaches here. See docs/plan_multi_indoor_sensor.md §2.2 for why
`average` and `lowest` are the only two modes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

indoor_aggregation = load("indoor_aggregation")


def test_empty_list_is_none() -> None:
    assert indoor_aggregation.aggregate([], "average") is None
    assert indoor_aggregation.aggregate([], "lowest") is None


def test_single_reading_is_itself_in_either_mode() -> None:
    assert indoor_aggregation.aggregate([21.5], "average") == 21.5
    assert indoor_aggregation.aggregate([21.5], "lowest") == 21.5


def test_average_of_three() -> None:
    assert indoor_aggregation.aggregate([20.0, 21.0, 22.0], "average") == 21.0


def test_lowest_of_three() -> None:
    assert indoor_aggregation.aggregate([20.0, 21.0, 22.0], "lowest") == 20.0
