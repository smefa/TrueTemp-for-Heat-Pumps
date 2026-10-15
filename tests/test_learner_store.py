"""Unit tests for learned-state persistence.

Two properties matter most here, and they pull in opposite directions:

  * anything genuinely incompatible must cold-start rather than crash or, far
    worse, load into the wrong shape; and
  * a configuration change must NOT throw away learning. The store this
    replaced discarded weeks of accumulated evidence whenever an optional input
    was toggled, because its parameter vector changed shape. Nothing here has
    that problem, and `test_toggling_inputs_never_discards_learning` is what
    keeps it that way.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

# `heuristic` must load before `learner_store`: learner_store.py's own
# `from .heuristic import ...` falls back to a bare `from heuristic import ...`
# when loaded standalone by path (see loader.py's docstring), and that fallback
# only resolves if "heuristic" is already registered in sys.modules by then.
heuristic = load("heuristic")
lag = load("lag")
learner = load("learner")
store = load("learner_store")

OutdoorBin = learner.OutdoorBin
BaselineBin = learner.BaselineBin


def populated_state() -> learner.LearnerState:
    bins = [
        OutdoorBin(
            hold_offset_c=-0.5 * i,
            ceiling_c=5.0 - 0.1 * i,
            useful_offset_c=0.25 * i,
            close_rate_c_per_h=0.05 * i,
            samples=i * 3,
            # Odd bins seeded, so round-trip tests exercise both.
            seeded=(i % 2 == 1),
        )
        for i in range(learner.N_BINS)
    ]
    baseline_bins = [
        BaselineBin(indoor_c=19.0 + 0.2 * i, samples=i)
        for i in range(learner.N_BINS)
    ]
    return learner.LearnerState(
        bins=tuple(bins),
        baseline_bins=tuple(baseline_bins),
        total_samples=421,
        saturation_streak=2,
        holdoff_until_s=999999.0,
        prev_indoor_c=20.7,
        prev_target_c=21.0,
        prev_offset_c=-1.75,
        prev_bin_index=3,
        bin_entered_s=12345.0,
        prev_is_active=True,
        shared_offset_c=-1.25,
    )


def populated_lag() -> lag.LagState:
    return lag.LagState(
        offsets_c=tuple(-0.1 * i for i in range(40)),
        indoors_c=tuple(20.0 + 0.01 * i for i in range(40)),
        targets_c=tuple(21.0 for _ in range(40)),
        step_minutes=15.0,
    )


def populated_price_spread_history() -> "heuristic.PriceSpreadHistory":
    return heuristic.PriceSpreadHistory(
        daily_spreads_c=tuple(0.1 + 0.02 * i for i in range(12)),
        daily_medians_c=tuple(0.3 + 0.01 * i for i in range(12)),
        current_date="2026-08-16",
        current_date_max_spread_c=0.42,
        current_date_median_c=0.31,
    )


class TestRoundTrip:
    def test_every_learned_field_survives(self):
        state, lag_state = populated_state(), populated_lag()
        price_history = populated_price_spread_history()
        restored, restored_lag, restored_price_history = store.deserialize(
            store.serialize(state, lag_state, price_history)
        )
        assert restored.bins == state.bins
        assert restored.baseline_bins == state.baseline_bins
        assert restored.total_samples == state.total_samples
        assert restored.saturation_streak == state.saturation_streak
        assert restored.prev_indoor_c == state.prev_indoor_c
        assert restored.prev_target_c == state.prev_target_c
        assert restored.prev_offset_c == state.prev_offset_c
        assert restored.prev_is_active == state.prev_is_active
        assert restored.shared_offset_c == state.shared_offset_c
        assert restored_lag == lag_state
        assert restored_price_history == price_history

    def test_payload_is_json_serializable(self):
        """It goes through homeassistant's Store, which is JSON on disk."""
        payload = store.serialize(
            populated_state(), populated_lag(), populated_price_spread_history()
        )
        assert json.loads(json.dumps(payload)) == payload

    def test_a_cold_start_round_trips(self):
        state = learner.initial_state()
        lag_state = lag.initial_state(15.0)
        price_history = heuristic.initial_price_spread_history()
        restored, restored_lag, restored_price_history = store.deserialize(
            store.serialize(state, lag_state, price_history)
        )
        assert restored == state
        assert restored_lag == lag_state
        assert restored_price_history == price_history

    def test_holdoff_is_deliberately_not_persisted(self):
        """It is measured against a monotonic clock that resets on restart, so
        a stored value would be meaningless. No hold-off is the safe reading."""
        payload = store.serialize(populated_state(), populated_lag())
        assert "holdoff_until_s" not in payload
        restored, _, _ = store.deserialize(payload)
        assert restored.holdoff_until_s is None

    def test_store_key_is_entry_scoped(self):
        assert store.store_key("abc") != store.store_key("def")
        assert "abc" in store.store_key("abc")


class TestCompatibility:
    def test_toggling_inputs_never_discards_learning(self):
        """The regression this design exists to prevent. Solar, wind and price
        are all pure composition concerns now — nothing about them changes the
        shape of the table — so a payload written under any configuration loads
        under any other."""
        payload = store.serialize(populated_state(), populated_lag())
        restored, _, _ = store.deserialize(payload)
        assert restored.total_samples == 421
        assert restored.bins[3].samples > 0

    def test_schema_version_mismatch_cold_starts(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["schema_version"] = store.STATE_SCHEMA_VERSION + 1
        assert store.deserialize(payload) is None

    def test_model_version_mismatch_cold_starts(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["model_version"] = "something_else_v9"
        assert store.deserialize(payload) is None

    def test_wrong_bin_count_cold_starts(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["bins"] = payload["bins"][:-1]
        assert store.deserialize(payload) is None


class TestBaselineTable:
    """The open-loop baseline table added alongside the offset table.

    It is purely additive (see the module docstrings on both `learner.py` and
    `learner_store.py`), so the schema/model version did not move. The upgrade
    path — an old payload with none of these keys still loading, keeping its
    offset table, and starting from an empty baseline — is the test that
    matters most here: get it wrong and every existing install's learning is
    thrown away on update.
    """

    def test_old_payload_without_baseline_keys_still_loads(self):
        payload = store.serialize(populated_state(), populated_lag())
        del payload["baseline_bins"]
        for item in payload["bins"]:
            del item["seeded"]
        del payload["prev_is_active"]

        restored, restored_lag, restored_price_history = store.deserialize(payload)
        assert restored is not None
        # The valuable part — the offset table — survives untouched.
        assert restored.total_samples == 421
        assert restored.bins[3].samples > 0
        assert restored_lag.indoors_c != ()
        # The new fields come back at their defaults, not crash or None.
        assert restored.baseline_bins == tuple(
            learner.BaselineBin() for _ in range(learner.N_BINS)
        )
        assert all(not b.seeded for b in restored.bins)
        assert restored.prev_is_active is False
        # `price_spread_history` didn't exist yet either — same upgrade path,
        # same "comes back at its default" expectation.
        assert restored_price_history == heuristic.initial_price_spread_history()

    def test_old_payload_without_shared_offset_loads_as_zero(self):
        """Zero is what an install written before the shared offset had in
        effect, so the table it learned comes back unchanged."""
        payload = store.serialize(populated_state(), populated_lag())
        del payload["shared_offset_c"]
        restored, _, _ = store.deserialize(payload)
        assert restored.shared_offset_c == 0.0
        assert restored.bins == populated_state().bins

    @pytest.mark.parametrize("junk", ["nope", None, float("nan"), [1]])
    def test_unreadable_shared_offset_falls_back_to_zero(self, junk):
        payload = store.serialize(populated_state(), populated_lag())
        payload["shared_offset_c"] = junk
        restored, _, _ = store.deserialize(payload)
        assert restored is not None
        assert restored.shared_offset_c == 0.0

    def test_corrupt_baseline_drops_only_the_baseline(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["baseline_bins"][0]["indoor_c"] = float("nan")

        restored, _, _ = store.deserialize(payload)
        assert restored is not None
        assert restored.total_samples == 421
        assert restored.bins[3].samples > 0
        assert restored.baseline_bins == tuple(
            learner.BaselineBin() for _ in range(learner.N_BINS)
        )

    def test_wrong_length_baseline_drops_only_the_baseline(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["baseline_bins"] = payload["baseline_bins"][:-1]

        restored, _, _ = store.deserialize(payload)
        assert restored is not None
        assert restored.total_samples == 421
        assert restored.baseline_bins == tuple(
            learner.BaselineBin() for _ in range(learner.N_BINS)
        )

    def test_negative_baseline_samples_drops_only_the_baseline(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["baseline_bins"][0]["samples"] = -1

        restored, _, _ = store.deserialize(payload)
        assert restored is not None
        assert restored.total_samples == 421
        assert restored.baseline_bins == tuple(
            learner.BaselineBin() for _ in range(learner.N_BINS)
        )

    def test_dwell_fields_are_never_persisted(self):
        """`prev_bin_index` and `bin_entered_s` are measured against a
        monotonic clock that resets on restart — same reasoning as
        `holdoff_until_s` — so they must come back None even if a payload
        somehow carries values for them (e.g. hand-edited, or replayed from a
        captured history)."""
        payload = store.serialize(populated_state(), populated_lag())
        assert "prev_bin_index" not in payload
        assert "bin_entered_s" not in payload

        payload["prev_bin_index"] = 5
        payload["bin_entered_s"] = 123.0
        restored, _, _ = store.deserialize(payload)
        assert restored.prev_bin_index is None
        assert restored.bin_entered_s is None


class TestCorruption:
    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "not a dict",
            42,
            [],
            {},
        ],
    )
    def test_junk_cold_starts(self, payload):
        assert store.deserialize(payload) is None

    def test_missing_key_cold_starts(self):
        payload = store.serialize(populated_state(), populated_lag())
        del payload["total_samples"]
        assert store.deserialize(payload) is None

    def test_malformed_bin_cold_starts(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["bins"][2] = "not a bin"
        assert store.deserialize(payload) is None

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_values_cold_start(self, bad):
        payload = store.serialize(populated_state(), populated_lag())
        payload["bins"][0]["hold_offset_c"] = bad
        assert store.deserialize(payload) is None

    def test_non_finite_prev_offset_cold_starts(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["prev_offset_c"] = float("nan")
        assert store.deserialize(payload) is None

    def test_negative_counters_cold_start(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["total_samples"] = -1
        assert store.deserialize(payload) is None

    def test_negative_bin_samples_cold_start(self):
        payload = store.serialize(populated_state(), populated_lag())
        payload["bins"][0]["samples"] = -3
        assert store.deserialize(payload) is None


class TestLagBufferIsExpendable:
    """Losing the buffer costs a few days of re-accumulation; losing the table
    costs a season. So a bad buffer must never take the table down with it."""

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p.pop("lag"),
            lambda p: p.update(lag="not a dict"),
            lambda p: p["lag"].update(offsets_c="nope"),
            lambda p: p["lag"].update(indoors_c=[float("nan"), 1.0]),
            lambda p: p["lag"].update(step_minutes=0.0),
            # Mismatched series lengths would misalign every correlation.
            lambda p: p["lag"].update(offsets_c=[1.0, 2.0], indoors_c=[20.0]),
        ],
    )
    def test_bad_buffer_keeps_the_table(self, mutate):
        payload = store.serialize(populated_state(), populated_lag())
        mutate(payload)
        restored = store.deserialize(payload)
        assert restored is not None
        state, lag_state, _ = restored
        assert state.total_samples == 421
        assert lag_state.indoors_c == ()

    def test_unreadable_step_minutes_keeps_the_samples(self):
        """The cadence is a property of the coordinator, not of the data, so an
        unreadable one is recoverable by assuming the default — no reason to
        discard perfectly good samples over it."""
        payload = store.serialize(populated_state(), populated_lag())
        payload["lag"]["step_minutes"] = "fifteen"
        _, lag_state, _ = store.deserialize(payload)
        assert len(lag_state.indoors_c) == 40
        assert lag_state.step_minutes == 15.0

    def test_oversized_buffer_is_truncated_to_the_newest(self):
        big = lag.LagState(
            offsets_c=tuple(float(i) for i in range(lag.BUFFER_SAMPLES + 100)),
            indoors_c=tuple(float(i) for i in range(lag.BUFFER_SAMPLES + 100)),
            targets_c=tuple(21.0 for _ in range(lag.BUFFER_SAMPLES + 100)),
            step_minutes=15.0,
        )
        _, restored, _ = store.deserialize(store.serialize(populated_state(), big))
        assert len(restored.offsets_c) == lag.BUFFER_SAMPLES
        assert restored.offsets_c[-1] == float(lag.BUFFER_SAMPLES + 99)


class TestPriceSpreadHistoryIsExpendable:
    """The rolling price-spread/median history behind
    `heuristic.price_significance()`. Same "expendable, drop it on its own"
    treatment as the lag buffer above: losing it costs weeks of
    reference-building for the price-significance taper, not the calibrated
    offset table beside it."""

    def test_omitted_key_is_a_cold_start_for_history_only(self):
        """`serialize` with no third argument (most calls in this file,
        including every one above this class) simply omits the key — the same
        upgrade path a payload written before this feature existed takes."""
        payload = store.serialize(populated_state(), populated_lag())
        assert "price_spread_history" not in payload
        state, _, price_history = store.deserialize(payload)
        assert state.total_samples == 421
        assert price_history == heuristic.initial_price_spread_history()

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p.update(price_spread_history="not a dict"),
            lambda p: p["price_spread_history"].update(daily_spreads_c="nope"),
            lambda p: p["price_spread_history"].update(
                daily_medians_c=[float("nan"), 0.3]
            ),
            # Parallel arrays (see PriceSpreadHistory's docstring) — a length
            # mismatch breaks the date-by-date pairing itself.
            lambda p: p["price_spread_history"].update(
                daily_spreads_c=[0.1, 0.2], daily_medians_c=[0.3]
            ),
            lambda p: p["price_spread_history"].update(
                current_date_max_spread_c=float("nan")
            ),
            lambda p: p["price_spread_history"].update(
                current_date_median_c=float("nan")
            ),
            lambda p: p["price_spread_history"].update(current_date=123),
        ],
    )
    def test_bad_history_keeps_the_table(self, mutate):
        payload = store.serialize(
            populated_state(), populated_lag(), populated_price_spread_history()
        )
        mutate(payload)
        restored = store.deserialize(payload)
        assert restored is not None
        state, _, price_history = restored
        assert state.total_samples == 421
        assert price_history == heuristic.initial_price_spread_history()

    def test_oversized_history_is_truncated_to_the_newest(self):
        long_history = heuristic.PriceSpreadHistory(
            daily_spreads_c=tuple(
                float(i) for i in range(heuristic.PRICE_SPREAD_HISTORY_DAYS + 10)
            ),
            daily_medians_c=tuple(
                0.3 + 0.001 * i
                for i in range(heuristic.PRICE_SPREAD_HISTORY_DAYS + 10)
            ),
            current_date="2026-08-16",
            current_date_max_spread_c=0.5,
            current_date_median_c=0.3,
        )
        _, _, restored = store.deserialize(
            store.serialize(populated_state(), populated_lag(), long_history)
        )
        assert len(restored.daily_spreads_c) == heuristic.PRICE_SPREAD_HISTORY_DAYS
        assert restored.daily_spreads_c[-1] == float(
            heuristic.PRICE_SPREAD_HISTORY_DAYS + 9
        )

    def test_negative_median_survives_round_trip(self):
        """Negative Nordpool day-ahead prices are routine in spring/summer —
        a negative stored median must not be treated as corrupt (unlike
        `daily_spreads_c`, which is a `max(0.0, ...)` by construction and
        genuinely cannot be negative)."""
        history = heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.1, 0.2, 0.3),
            daily_medians_c=(0.3, -0.05, 0.31),
            current_date="2026-08-16",
            current_date_max_spread_c=0.42,
            current_date_median_c=-0.02,
        )
        _, _, restored = store.deserialize(
            store.serialize(populated_state(), populated_lag(), history)
        )
        assert restored == history
