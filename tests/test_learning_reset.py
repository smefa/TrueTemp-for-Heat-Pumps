"""Exercise the production full-reset method without importing Home Assistant."""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

heuristic = load("heuristic")
lag = load("lag")
learner = load("learner")
store = load("learner_store")


@pytest.fixture
def coordinator():
    path = Path(__file__).parent.parent / "custom_components/truetemp/coordinator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    control = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "TrueTempCoordinator")
    reset = next(node for node in control.body
                 if getattr(node, "name", None) == "async_reset_learning")
    namespace = dict(
        learner_initial_state=learner.initial_state,
        lag_initial_state=lag.initial_state,
        learner_serialize=store.serialize,
        UPDATE_INTERVAL_MINUTES=15.0,
    )
    exec(compile(ast.Module(body=[reset], type_ignores=[]), str(path), "exec"), namespace)
    instance = SimpleNamespace(
        learner_state=learner.LearnerState(
            bins=tuple(learner.OutdoorBin(hold_offset_c=-5.0, samples=100)
                       for _ in range(learner.N_BINS)),
            baseline_bins=tuple(learner.BaselineBin(indoor_c=18.0, samples=20)
                                for _ in range(learner.N_BINS)),
            shared_offset_c=-5.0,
            total_samples=800,
        ),
        lag_state=lag.LagState(offsets_c=(-5.0,), indoors_c=(20.0,), targets_c=(21.0,)),
        learner_result=object(),
        lag_result=object(),
        _learner_last_step_s=1000.0,
        price_spread_history=heuristic.PriceSpreadHistory(
            daily_spreads_c=(0.5,), daily_medians_c=(0.3,)
        ),
        _outdoor_reference=lambda: None,
        _store=SimpleNamespace(async_save=AsyncMock()),
        async_request_refresh=AsyncMock(),
    )
    instance.async_reset_learning = MethodType(namespace["async_reset_learning"], instance)
    return instance


def test_full_reset_zeroes_every_band_and_persists_it(coordinator):
    asyncio.run(coordinator.async_reset_learning())

    assert coordinator.learner_state == learner.initial_state()
    assert coordinator.lag_state == lag.initial_state(15.0)
    assert coordinator.learner_result is None
    assert coordinator.lag_result is None
    assert coordinator._learner_last_step_s is None
    coordinator._store.async_save.assert_awaited_once()
    coordinator.async_request_refresh.assert_awaited_once()
    payload = coordinator._store.async_save.call_args.args[0]
    restored, restored_lag, restored_prices = store.deserialize(payload)
    assert restored == coordinator.learner_state
    assert restored_lag == coordinator.lag_state
    assert restored_prices == coordinator.price_spread_history
    for outdoor in (-30.0, -15.0, -7.5, 0.0, 7.5, 15.0, 30.0):
        assert learner.hold_offset_at(restored.bins, restored.shared_offset_c, outdoor) == 0.0


def test_full_reset_save_failure_preserves_existing_learning(coordinator):
    previous = vars(coordinator).copy()
    coordinator._store.async_save.side_effect = OSError("save failed")

    with pytest.raises(OSError, match="save failed"):
        asyncio.run(coordinator.async_reset_learning())

    for attribute in ("learner_state", "lag_state", "learner_result", "lag_result",
                      "_learner_last_step_s", "price_spread_history"):
        assert getattr(coordinator, attribute) is previous[attribute]
    coordinator.async_request_refresh.assert_not_awaited()
