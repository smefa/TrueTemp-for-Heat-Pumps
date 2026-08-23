"""Unit tests for the pure file-rotation logic in data_logger.py.

Like test_rc_store.py, this loads the module directly by file path so the
suite needs no Home Assistant install (CI only pip-installs pytest).
data_logger.py keeps its `homeassistant.core.HomeAssistant` import behind
`TYPE_CHECKING` specifically so this works, the same trick rc_store.py/
rc_model.py use.

Only `_append_line`/`_rotate`/`MAX_LOG_BYTES`/`_read_recent` are exercised
here — the async wrappers (`async_log_record`/`async_read_recent_records`)
need a real `hass` executor and are out of scope for an offline test.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_CC = (
    Path(__file__).parent.parent
    / "custom_components"
    / "truetemp"
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _CC / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


data_logger = _load("data_logger", "data_logger.py")


def test_append_line_writes_plain_jsonl(tmp_path):
    path = tmp_path / "entry.jsonl"
    data_logger._append_line(path, json.dumps({"a": 1}))
    data_logger._append_line(path, json.dumps({"a": 2}))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"a": 2}]


def test_append_line_below_threshold_does_not_rotate(tmp_path):
    path = tmp_path / "entry.jsonl"
    data_logger._append_line(path, "x" * 100)
    assert path.exists()
    assert list(tmp_path.glob("*.gz")) == []


def test_append_line_rotates_once_threshold_crossed(tmp_path, monkeypatch):
    monkeypatch.setattr(data_logger, "MAX_LOG_BYTES", 10)
    path = tmp_path / "entry.jsonl"

    data_logger._append_line(path, "a" * 20)  # first line already over 10 bytes
    data_logger._append_line(path, "second line")  # this call should rotate first

    # Fresh file holds only the post-rotation line.
    remaining = path.read_text(encoding="utf-8").splitlines()
    assert remaining == ["second line"]

    # Rotated sibling is gzip, named after the original stem+suffix, and
    # holds exactly what was in the file before rotation.
    rotated = list(tmp_path.glob("entry.*.jsonl.gz"))
    assert len(rotated) == 1
    with gzip.open(rotated[0], "rt", encoding="utf-8") as handle:
        assert handle.read().splitlines() == ["a" * 20]


def test_rotate_prunes_old_rotations_beyond_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(data_logger, "MAX_ROTATED_LOGS", 2)
    path = tmp_path / "entry.jsonl"

    # Three pre-existing rotations, oldest to newest by name (the timestamp
    # format sorts lexically).
    old_names = [
        "entry.20260101T000000Z.jsonl.gz",
        "entry.20260102T000000Z.jsonl.gz",
        "entry.20260103T000000Z.jsonl.gz",
    ]
    for name in old_names:
        with gzip.open(tmp_path / name, "wt", encoding="utf-8") as handle:
            handle.write("stale\n")

    path.write_text("fresh\n", encoding="utf-8")
    data_logger._rotate(path)

    remaining = sorted(p.name for p in tmp_path.glob("entry.*.jsonl.gz"))
    # 3 pre-existing + 1 just-created = 4 candidates, capped to the newest 2:
    # the brand-new rotation and the newest pre-existing one.
    assert len(remaining) == 2
    assert "entry.20260101T000000Z.jsonl.gz" not in remaining
    assert "entry.20260102T000000Z.jsonl.gz" not in remaining
    assert "entry.20260103T000000Z.jsonl.gz" in remaining


def test_rotation_preserves_original_content_and_removes_source(tmp_path):
    path = tmp_path / "entry.jsonl"
    path.write_text("line1\nline2\n", encoding="utf-8")

    data_logger._rotate(path)

    assert not path.exists()
    rotated = list(tmp_path.glob("entry.*.jsonl.gz"))
    assert len(rotated) == 1
    with gzip.open(rotated[0], "rt", encoding="utf-8") as handle:
        assert handle.read() == "line1\nline2\n"


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_read_recent_filters_to_window_from_live_file(tmp_path):
    path = tmp_path / "entry.jsonl"
    lines = [
        json.dumps({"ts": _ts(5), "v": "too_old"}),
        json.dumps({"ts": _ts(1), "v": "in_window"}),
        json.dumps({"ts": _ts(0.1), "v": "newest"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = data_logger._read_recent(path, days=3)

    assert [r["v"] for r in records] == ["in_window", "newest"]


def test_read_recent_skips_malformed_lines(tmp_path):
    path = tmp_path / "entry.jsonl"
    path.write_text(
        json.dumps({"ts": _ts(0.1), "v": "good"}) + "\nnot json\n",
        encoding="utf-8",
    )

    records = data_logger._read_recent(path, days=3)

    assert [r["v"] for r in records] == ["good"]


def test_read_recent_missing_file_returns_empty(tmp_path):
    path = tmp_path / "entry.jsonl"
    assert data_logger._read_recent(path, days=3) == []


def test_read_recent_pulls_from_rotated_file_when_live_file_too_short(tmp_path):
    path = tmp_path / "entry.jsonl"
    with gzip.open(
        tmp_path / "entry.20260101T000000Z.jsonl.gz", "wt", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps({"ts": _ts(1), "v": "rotated"}) + "\n")
    path.write_text(json.dumps({"ts": _ts(0.1), "v": "live"}) + "\n", encoding="utf-8")

    records = data_logger._read_recent(path, days=3)

    assert [r["v"] for r in records] == ["rotated", "live"]


def test_read_recent_stops_once_window_is_covered(tmp_path):
    path = tmp_path / "entry.jsonl"
    # This rotation is entirely outside the 3-day window and should never be
    # opened, since the live file's oldest record already reaches past the
    # cutoff on its own.
    with gzip.open(
        tmp_path / "entry.20260101T000000Z.jsonl.gz", "wt", encoding="utf-8"
    ) as handle:
        handle.write("not json - would raise if ever parsed\n")
    lines = [
        json.dumps({"ts": _ts(3.5), "v": "before_window"}),
        json.dumps({"ts": _ts(0.1), "v": "live"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = data_logger._read_recent(path, days=3)

    assert [r["v"] for r in records] == ["live"]


def test_log_file_path_unaffected_by_rotation_change():
    # log_file_path only builds a Path from hass.config.path + entry_id; it
    # doesn't touch disk, so a bare object with the right attribute suffices.
    class FakeConfig:
        def path(self, name):
            return f"/config/{name}"

    class FakeHass:
        config = FakeConfig()

    result = data_logger.log_file_path(FakeHass(), "abc123")
    assert str(result) == str(Path("/config/truetemp_data/abc123.jsonl"))
