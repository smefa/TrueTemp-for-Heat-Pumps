"""Local JSONL data logging, for offline model testing/backtesting later.

Unlike heuristic.py/learner.py/lag.py, this is NOT a pure module — it writes
to disk, so it needs `homeassistant` for executor-job scheduling (blocking
file I/O must never run directly on the event loop).

Why this exists: HA's recorder purges history by default (commonly ~10
days), and even its long-term statistics only keep hourly min/mean/max
aggregates — too coarse to judge a controller or replay a candidate change.
This appends one full-resolution record per coordinator cycle to a local
file, so a future session can drive a modified controller through real
history instead of waiting months for new live data per attempt. This is
exactly how the previous thermal model was evaluated — and disproved — so
it is the mechanism by which the current one gets held to the same standard.
Opt-in, off by default (see CONF_ENABLE_DATA_LOGGING) — purely local,
nothing is transmitted anywhere.

Scope note: each record carries the raw physical inputs, the learner's
internal state (which moves every cycle and is not reconstructible from
stored config), and the price forecast the decision actually used. That last
one matters because forecasts get revised: the realised prices are not a
substitute for what was known at decision time.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Only needed for type hints; kept out of the runtime import so this
    # module stays loadable (and unit-testable) without homeassistant
    # installed, same as learner_store.py/learner.py — safe because
    # `from __future__ import annotations` never evaluates these at runtime.
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DATA_DIR_NAME = "truetemp_data"

# Rotate a log once it crosses this size, so a long-running install doesn't
# accumulate one unbounded file. Gzip on rotation rather than shrinking the
# original in place, since key names (repeated on every JSONL line) compress
# extremely well and this keeps every past line intact and independently
# replayable, just under a .gz extension.
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB

# Keep only the N most recent rotated files per entry, so an install with
# logging left on for months doesn't grow `truetemp_data/` unboundedly.
# Oldest-first pruning happens inside `_rotate`, right after a new one lands.
MAX_ROTATED_LOGS = 5


def _rotate(path: Path) -> None:
    """Gzip the current log to a timestamped sibling and remove the
    original, so the next append starts a fresh file. The timestamp is UTC
    and to-the-second, matching this project's other rename-safety
    convention (see learner_store.py). Also prunes old rotations beyond
    `MAX_ROTATED_LOGS`, oldest first — the timestamp format sorts
    lexically, so a plain name sort is chronological."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rotated = path.with_name(f"{path.stem}.{stamp}{path.suffix}.gz")
    with path.open("rb") as src, gzip.open(rotated, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()

    siblings = sorted(path.parent.glob(f"{path.stem}.*{path.suffix}.gz"))
    for stale in siblings[:-MAX_ROTATED_LOGS]:
        stale.unlink()


def _append_line(path: Path, line: str) -> None:
    """Blocking file append — only ever call via the executor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
        _rotate(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def log_file_path(hass: HomeAssistant, entry_id: str) -> Path:
    """The JSONL file for one config entry.

    Keyed by entry_id (stable and unique) rather than the entry's title, so
    renaming a zone later never orphans or collides with its history.
    """
    return Path(hass.config.path(DATA_DIR_NAME)) / f"{entry_id}.jsonl"


async def async_log_record(
    hass: HomeAssistant, entry_id: str, record: dict[str, Any]
) -> None:
    """Append one record as a JSON line. Never raises — logs and swallows
    on failure, since a full disk or permissions issue here must not affect
    the real output. Best-effort, exactly like the output push.

    `allow_nan=False` is a backstop, not the primary defence: the caller
    (`coordinator._build_log_record`) already sanitizes non-finite floats to
    `None` so the common case never hits this. Bare `NaN`/`Infinity` tokens
    are invalid JSON and would otherwise break any replay parser reading
    this file — better to drop one record than silently emit a file no
    standard JSON reader can load.
    """
    path = log_file_path(hass, entry_id)
    try:
        line = json.dumps(record, default=str, allow_nan=False)
        await hass.async_add_executor_job(_append_line, path, line)
    except Exception as err:  # noqa: BLE001 - logging must never break output
        _LOGGER.warning("Could not write TrueTemp data log %s: %s", path, err)


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    """Parse JSONL text, silently dropping any line that isn't valid JSON.

    A truncated final line is possible if a rotation or process kill lands
    mid-write; skipping it is preferable to failing the whole read.
    """
    records = []
    for line in text.splitlines():
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _read_recent(path: Path, days: int) -> list[dict[str, Any]]:
    """Blocking read of the last `days` of records, oldest first.

    Reads the live file first, then walks backward through rotated `.gz`
    siblings (newest rotation first, matching `_rotate`'s naming) only until
    the window is covered — a rotation just past the cutoff need not be
    opened. Only ever call via the executor, same as `_append_line`.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records: list[dict[str, Any]] = []
    if path.exists():
        records = _parse_jsonl(path.read_text(encoding="utf-8"))

    rotated = sorted(
        path.parent.glob(f"{path.stem}.*{path.suffix}.gz"), reverse=True
    )
    for rotated_path in rotated:
        if records and datetime.fromisoformat(records[0]["ts"]) <= cutoff:
            break
        with gzip.open(rotated_path, "rt", encoding="utf-8") as handle:
            records = _parse_jsonl(handle.read()) + records

    return [r for r in records if datetime.fromisoformat(r["ts"]) >= cutoff]


async def async_read_recent_records(
    hass: HomeAssistant, entry_id: str, days: int
) -> list[dict[str, Any]]:
    """Return this entry's logged records from the last `days`, oldest first.

    Empty if data logging was never enabled (no file) or has no records
    inside the window yet. Used by diagnostics to attach a trend window
    without requiring a separate replay session.
    """
    path = log_file_path(hass, entry_id)
    return await hass.async_add_executor_job(_read_recent, path, days)
