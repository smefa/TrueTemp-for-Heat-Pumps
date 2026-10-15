"""(De)serialization of the learner and lag state for persistence.

Pure module — no `homeassistant.*` imports — for the same reason `learner.py`
and `lag.py` are: so the serialization contract can be unit-tested in isolation
without Home Assistant installed. The actual disk I/O (constructing
`homeassistant.helpers.storage.Store`, `async_load`/`async_delay_save`) lives
in `coordinator.py`, which already depends on Home Assistant.

## One deliberate difference from the store this replaces

The old RC-model store rejected any state whose *layout* did not match today's
config, because that estimator's parameter vector changed shape when an
optional input was toggled — and, worse, several different layouts shared a
length, so a naive count check could silently load a learned wind coefficient
into the solar slot.

Nothing here has that problem. The offset table is a fixed number of bins whose
meaning never depends on configuration, so **toggling solar, wind or price
never discards learning**. Switching the wind term off used to throw away weeks
of accumulated evidence; now it changes only which feedforward terms are added
on top of an offset table that keeps everything it knew.

The remaining gates are genuine incompatibilities: a schema version bump, a
model version bump, or structural corruption. All of them return `None` so the
caller cold-starts cleanly rather than crashing on bad data.

## Additive fields do not need a version bump

The baseline table (`baseline_bins`, the per-bin `seeded` flag, `prev_is_active`)
was added to `LearnerState` without bumping `STATE_SCHEMA_VERSION`. All three
are read with `.get()` and a default, so a payload written before they existed
loads exactly as it always did — empty baseline table, every bin unseeded, and
`prev_is_active=False` — while keeping the far more valuable learned offset
table intact. Bumping the version here would cold-start every existing
install's offset table just to gain a table that starts empty anyway, which is
the same throwaway `learner.py`'s own docstring argues against.

`shared_offset_c` (the whole-house part of the offset) follows the same rule:
a payload without it loads as zero, which is what that install had in effect,
so the upgrade keeps the learned table as it was.

The rolling price-spread/median history (`PriceSpreadHistory` from
`heuristic.py`, added for `heuristic.price_significance()`) follows the exact
same rule: it lives under its own top-level `price_spread_history` key,
entirely absent from any payload written before this feature existed, and
`deserialize` reads it with `.get()` and falls back to an empty history — see
`_parse_price_spread_history` — rather than bumping the schema version. Losing
it is cheap by the same reasoning the lag buffer below already established:
it costs a few weeks of reference-building for the price-significance taper,
not the calibrated offset table, so a corrupt or missing one is dropped on its
own instead of taking the far more valuable learner state down with it.
"""

from __future__ import annotations

import logging
import math
from typing import Any

# Package-relative import at runtime; the absolute fallback is only taken when
# this file is loaded standalone by file path in the test suite, which has no
# package context and pre-registers the sibling modules in sys.modules.
try:  # pragma: no cover - trivial import shim
    from .heuristic import PRICE_SPREAD_HISTORY_DAYS, PriceSpreadHistory
    from .lag import BUFFER_SAMPLES, LagState
    from .learner import (
        MODEL_VERSION,
        N_BINS,
        BaselineBin,
        LearnerState,
        OutdoorBin,
    )
except ImportError:  # pragma: no cover - test path-load fallback
    from heuristic import (  # type: ignore[no-redef]
        PRICE_SPREAD_HISTORY_DAYS,
        PriceSpreadHistory,
    )
    from lag import BUFFER_SAMPLES, LagState  # type: ignore[no-redef]
    from learner import (  # type: ignore[no-redef]
        MODEL_VERSION,
        N_BINS,
        BaselineBin,
        LearnerState,
        OutdoorBin,
    )

_LOGGER = logging.getLogger(__name__)

# On-disk container version passed to homeassistant's Store(...).
STORAGE_VERSION = 1

# Version of the payload shape itself, validated by hand below so an
# incompatible state is discarded and cold-started rather than needing a
# migration hook. Bump whenever the serialized layout changes incompatibly.
STATE_SCHEMA_VERSION = 1

STORAGE_KEY_PREFIX = "truetemp_learner"


def store_key(entry_id: str) -> str:
    """Storage key for one config entry's learned state.

    Keyed by `entry_id` (stable and unique) rather than the entry title, the
    same rename-safety convention `data_logger.py` uses for its per-entry log.
    """
    return f"{STORAGE_KEY_PREFIX}_{entry_id}"


def serialize(
    state: LearnerState,
    lag_state: LagState,
    price_spread_history: PriceSpreadHistory | None = None,
) -> dict[str, Any]:
    """Convert learner + lag + price-spread-history state into a JSON-safe
    dict for the Store.

    `price_spread_history` defaults to None and, when None, is simply omitted
    from the payload rather than serialized as an empty history — the same
    outcome `deserialize` falls back to for a payload that never had the key
    at all (see `_parse_price_spread_history`), so a caller with nothing to
    persist yet (this module's own tests included) does not need to invent a
    placeholder value.
    """
    payload: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "n_bins": len(state.bins),
        "bins": [
            {
                "hold_offset_c": b.hold_offset_c,
                "ceiling_c": b.ceiling_c,
                "useful_offset_c": b.useful_offset_c,
                "close_rate_c_per_h": b.close_rate_c_per_h,
                "samples": b.samples,
                "seeded": b.seeded,
            }
            for b in state.bins
        ],
        "baseline_bins": [
            {"indoor_c": b.indoor_c, "samples": b.samples}
            for b in state.baseline_bins
        ],
        "total_samples": state.total_samples,
        "saturation_streak": state.saturation_streak,
        # Deliberately NOT persisted: `holdoff_until_s`. It is measured against
        # a monotonic clock that resets on restart, so a stored value would be
        # meaningless — and the safe interpretation of "unknown" is no hold-off,
        # since the first cycle after a restart re-establishes it anyway.
        #
        # Also deliberately NOT persisted, for the identical reason: the
        # baseline-dwell tracking pair `prev_bin_index` / `bin_entered_s`. Both
        # are measured against the same monotonic clock, so a stored value would
        # be meaningless across a restart — the safe reading is "unknown", which
        # means "start settling into whatever bin we're in again".
        "prev_indoor_c": state.prev_indoor_c,
        "prev_target_c": state.prev_target_c,
        "prev_offset_c": state.prev_offset_c,
        "prev_is_active": state.prev_is_active,
        "shared_offset_c": state.shared_offset_c,
        "lag": {
            "step_minutes": lag_state.step_minutes,
            "offsets_c": list(lag_state.offsets_c),
            "indoors_c": list(lag_state.indoors_c),
            "targets_c": list(lag_state.targets_c),
        },
    }
    if price_spread_history is not None:
        payload["price_spread_history"] = {
            "daily_spreads_c": list(price_spread_history.daily_spreads_c),
            "daily_medians_c": list(price_spread_history.daily_medians_c),
            "current_date": price_spread_history.current_date,
            "current_date_max_spread_c": price_spread_history.current_date_max_spread_c,
            "current_date_median_c": price_spread_history.current_date_median_c,
        }
    return payload


def _finite_or_none(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, (int, float)) and math.isfinite(value)


def _floats(values: Any, limit: int) -> tuple[float, ...] | None:
    """Coerce a stored list into a bounded tuple of finite floats."""
    if not isinstance(values, (list, tuple)):
        return None
    try:
        out = tuple(float(v) for v in values)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in out):
        return None
    return out[-limit:]


def _parse_price_spread_history(raw: Any) -> PriceSpreadHistory | None:
    """Coerce a stored price-spread history, or `None` if it is not usable as
    one.

    A missing key (`raw is None` — every payload written before this feature
    existed) and a genuinely corrupt one both return `None` here, exactly like
    `_parse_baseline_bins` above: the caller falls back to an empty history
    either way, which just means `heuristic.price_significance()`'s seasonal
    term and auto floor contribute no extra information until fresh days
    accumulate again — far cheaper to lose than the offset table beside it.
    """
    if not isinstance(raw, dict):
        return None
    try:
        daily_spreads_c = _floats(raw.get("daily_spreads_c"), PRICE_SPREAD_HISTORY_DAYS)
        daily_medians_c = _floats(raw.get("daily_medians_c"), PRICE_SPREAD_HISTORY_DAYS)
        if daily_spreads_c is None or daily_medians_c is None:
            raise ValueError("unreadable daily_spreads_c/daily_medians_c")
        if len(daily_spreads_c) != len(daily_medians_c):
            # The two tuples are parallel, one entry per stored date (see
            # PriceSpreadHistory's docstring) — a length mismatch means the
            # pairing itself cannot be trusted, not just one value in it.
            raise ValueError("daily_spreads_c/daily_medians_c length mismatch")
        if any(v < 0 for v in daily_spreads_c):
            raise ValueError("negative stored spread")
        current_date = raw.get("current_date")
        if current_date is not None and not isinstance(current_date, str):
            raise TypeError("current_date is not a string")
        current_date_max_spread_c = float(raw.get("current_date_max_spread_c", 0.0))
        current_date_median_c = float(raw.get("current_date_median_c", 0.0))
        if not math.isfinite(current_date_max_spread_c) or current_date_max_spread_c < 0:
            raise ValueError("bad current_date_max_spread_c")
        if not math.isfinite(current_date_median_c):
            raise ValueError("bad current_date_median_c")
    except (TypeError, ValueError):
        return None
    return PriceSpreadHistory(
        daily_spreads_c=daily_spreads_c,
        daily_medians_c=daily_medians_c,
        current_date=current_date,
        current_date_max_spread_c=current_date_max_spread_c,
        current_date_median_c=current_date_median_c,
    )


def _parse_baseline_bins(raw: Any) -> tuple[BaselineBin, ...] | None:
    """Coerce a stored baseline table, or `None` if it is not usable as one.

    A missing key (`raw is None`, the pre-baseline upgrade path) and a
    genuinely corrupt one both return `None` here — the caller distinguishes
    "never had one" from "had a bad one" for logging, but either way the
    result is the same: fall back to an empty table rather than touch the
    offset table.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != N_BINS:
        return None
    try:
        out = []
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("baseline bin entry is not a dict")
            indoor_c = float(item["indoor_c"])
            samples = int(item["samples"])
            if not math.isfinite(indoor_c):
                raise ValueError("non-finite baseline indoor_c")
            if samples < 0:
                raise ValueError("negative baseline sample count")
            out.append(BaselineBin(indoor_c=indoor_c, samples=samples))
    except (KeyError, TypeError, ValueError):
        return None
    return tuple(out)


def deserialize(
    data: Any,
) -> tuple[LearnerState, LagState, PriceSpreadHistory] | None:
    """Reconstruct (learner state, lag state, price-spread history), or `None`
    if unusable.

    Never raises. `None` means "cold start cleanly", which is always a safe
    outcome: an empty table simply relearns, starting from the minimum
    authority clamp.
    """
    if not isinstance(data, dict):
        _LOGGER.debug("Learner state discarded: not a dict (%r)", type(data))
        return None

    if data.get("schema_version") != STATE_SCHEMA_VERSION:
        _LOGGER.debug(
            "Learner state discarded: schema_version %r != %r",
            data.get("schema_version"),
            STATE_SCHEMA_VERSION,
        )
        return None

    if data.get("model_version") != MODEL_VERSION:
        _LOGGER.debug(
            "Learner state discarded: model_version %r != %r",
            data.get("model_version"),
            MODEL_VERSION,
        )
        return None

    raw_bins = data.get("bins")
    if not isinstance(raw_bins, (list, tuple)) or len(raw_bins) != N_BINS:
        _LOGGER.debug(
            "Learner state discarded: expected %d bins, got %r",
            N_BINS,
            len(raw_bins) if isinstance(raw_bins, (list, tuple)) else raw_bins,
        )
        return None

    bins: list[OutdoorBin] = []
    try:
        for item in raw_bins:
            if not isinstance(item, dict):
                raise TypeError("bin entry is not a dict")
            hold = float(item["hold_offset_c"])
            ceiling = float(item["ceiling_c"])
            useful = float(item["useful_offset_c"])
            close_rate = float(item["close_rate_c_per_h"])
            samples = int(item["samples"])
            if not all(math.isfinite(v) for v in (hold, ceiling, useful, close_rate)):
                raise ValueError("non-finite bin value")
            if samples < 0:
                raise ValueError("negative sample count")
            # Added additively (see module docstring): absent on any payload
            # written before seeding existed, and a missing/malformed flag is
            # not grounds to distrust the rest of an otherwise-good bin, so it
            # defaults False rather than joining the raise above.
            seeded = bool(item.get("seeded", False))
            bins.append(
                OutdoorBin(
                    hold_offset_c=hold,
                    ceiling_c=ceiling,
                    useful_offset_c=useful,
                    close_rate_c_per_h=close_rate,
                    samples=samples,
                    seeded=seeded,
                )
            )
        total_samples = int(data["total_samples"])
        saturation_streak = int(data["saturation_streak"])
        prev_indoor_c = data.get("prev_indoor_c")
        prev_target_c = data.get("prev_target_c")
        prev_offset_c = float(data.get("prev_offset_c", 0.0))
        # Same additive story as `seeded` above: default False so a pre-baseline
        # payload replays as "the install was never mid-active-session", which
        # is exactly the state it was actually in.
        raw_prev_active = data.get("prev_is_active", False)
        prev_is_active = raw_prev_active if isinstance(raw_prev_active, bool) else False
        # Same again: absent before the shared offset existed, and zero is
        # exactly what such an install had in effect. An unreadable value is
        # not worth the table, so it falls back to zero too.
        try:
            shared_offset_c = float(data.get("shared_offset_c", 0.0))
        except (TypeError, ValueError):
            shared_offset_c = 0.0
        if not math.isfinite(shared_offset_c):
            shared_offset_c = 0.0
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.debug("Learner state discarded: corrupt payload (%s)", err)
        return None

    if total_samples < 0 or saturation_streak < 0:
        _LOGGER.debug("Learner state discarded: negative counters")
        return None
    if not math.isfinite(prev_offset_c):
        _LOGGER.debug("Learner state discarded: non-finite prev_offset_c")
        return None
    if not all(_finite_or_none(v) for v in (prev_indoor_c, prev_target_c)):
        _LOGGER.debug("Learner state discarded: non-finite prev_* slot")
        return None

    # The baseline table is open-loop evidence, strictly weaker than the offset
    # table it sits beside — so, exactly like the lag buffer below, a corrupt or
    # missing one is discarded on its own rather than taking the (far more
    # valuable) offset table down with it. Missing entirely is the normal
    # upgrade path from a pre-baseline payload and gets no log line; present but
    # unusable is genuine corruption and does.
    baseline_bins = tuple(BaselineBin() for _ in range(N_BINS))
    raw_baseline = data.get("baseline_bins")
    parsed_baseline = _parse_baseline_bins(raw_baseline)
    if parsed_baseline is not None:
        baseline_bins = parsed_baseline
    elif raw_baseline is not None:
        _LOGGER.debug("Baseline table discarded (offset table kept): corrupt payload")

    learner_state = LearnerState(
        bins=tuple(bins),
        baseline_bins=baseline_bins,
        total_samples=total_samples,
        saturation_streak=saturation_streak,
        holdoff_until_s=None,
        prev_indoor_c=prev_indoor_c,
        prev_target_c=prev_target_c,
        prev_offset_c=prev_offset_c,
        # Deliberately not read from `data` even if a key happens to be present
        # (e.g. a payload replayed from a captured JSONL history): both are
        # dwell-tracking state against a monotonic clock that resets on
        # restart, so `None` is the only safe reading — see `serialize` above.
        prev_bin_index=None,
        bin_entered_s=None,
        prev_is_active=prev_is_active,
        shared_offset_c=shared_offset_c,
    )

    # The lag buffer is a pure optimisation — losing it costs a few days of
    # re-accumulation and nothing else — so a corrupt or missing one drops just
    # the buffer instead of discarding the whole (far more valuable) table.
    lag_state = LagState()
    raw_lag = data.get("lag")
    if isinstance(raw_lag, dict):
        offsets = _floats(raw_lag.get("offsets_c"), BUFFER_SAMPLES)
        indoors = _floats(raw_lag.get("indoors_c"), BUFFER_SAMPLES)
        targets = _floats(raw_lag.get("targets_c"), BUFFER_SAMPLES)
        try:
            step_minutes = float(raw_lag.get("step_minutes", 15.0))
        except (TypeError, ValueError):
            step_minutes = 15.0
        if (
            offsets is not None
            and indoors is not None
            and targets is not None
            and len(offsets) == len(indoors) == len(targets)
            and math.isfinite(step_minutes)
            and step_minutes > 0
        ):
            lag_state = LagState(
                offsets_c=offsets,
                indoors_c=indoors,
                targets_c=targets,
                step_minutes=step_minutes,
            )
        else:
            _LOGGER.debug("Lag buffer discarded (learned table kept)")

    # Same "expendable, drop it on its own" treatment as the lag buffer just
    # above — see `_parse_price_spread_history`'s docstring for why losing it
    # is cheap. Missing entirely is the normal upgrade path from a payload
    # written before this feature existed and gets no log line; present but
    # unusable is genuine corruption and does, mirroring the baseline table's
    # own missing-vs-corrupt distinction above.
    price_spread_history = PriceSpreadHistory()
    raw_price_spread_history = data.get("price_spread_history")
    parsed_price_spread_history = _parse_price_spread_history(raw_price_spread_history)
    if parsed_price_spread_history is not None:
        price_spread_history = parsed_price_spread_history
    elif raw_price_spread_history is not None:
        _LOGGER.debug(
            "Price spread history discarded (learned table kept): corrupt payload"
        )

    return learner_state, lag_state, price_spread_history
