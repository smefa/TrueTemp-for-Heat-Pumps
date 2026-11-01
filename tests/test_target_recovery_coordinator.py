"""Exercise actual coordinator wiring without importing Home Assistant.

Compile the recovery/learning statements and learner method directly from the
coordinator AST. Only HA-facing services and the clock are replaced; control
decisions and the learner's inputs come from the production methods.
"""

from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from loader import load  # noqa: E402

heuristic = load("heuristic")
learner = load("learner")
lag = load("lag")
recovery = load("target_recovery")
load("holiday")
vacation = load("vacation")


@pytest.fixture
def coordinator():
    path = Path(__file__).parent.parent / "custom_components/truetemp/coordinator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    control = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "TrueTempCoordinator")
    advance = next(node for node in control.body
                   if getattr(node, "name", None) == "_advance_learning")
    update = next(node for node in control.body
                  if getattr(node, "name", None) == "_async_update_cycle")
    start = next(
        index for index, node in enumerate(update.body)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "_effective_target_c"
    )
    stop = next(
        index for index, node in enumerate(update.body)
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "_learner_stepped"
    )
    refresh = ast.parse(
        "def refresh(self, indoor_temp_c=21.0, indoor_ok=True, "
        "indoor_contributing=frozenset(), raw_outdoor_temp_c=0.0, "
        "sun_elevation_deg=-10.0, sun_azimuth_deg=180.0, cloud_coverage_pct=100.0):\n"
        "    return self._learner_stepped\n"
    ).body[0]
    refresh.body = update.body[start:stop + 1] + refresh.body
    clock = [1000.0]
    inputs_seen = []

    def step(state, inputs):
        inputs_seen.append(inputs)
        return learner.step(state, inputs)

    namespace = dict(
        time=SimpleNamespace(monotonic=lambda: clock[0]),
        TargetRecovery=recovery.TargetRecovery,
        resolve_target_recovery=recovery.resolve,
        VACATION_ACTIVE_PHASES=vacation.ACTIVE_PHASES,
        SPOOF_PER_INDOOR_C=learner.SPOOF_PER_INDOOR_C,
        resolve_heating_hard_limit_engaged=heuristic.resolve_heating_hard_limit_engaged,
        lag_push=lag.push,
        lag_estimate=lag.estimate,
        learner_step=step,
        LearnerInputs=learner.LearnerInputs,
        _LOGGER=logging.getLogger(__name__),
    )
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")],
                             level=0), advance, refresh],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    instance = SimpleNamespace(
        indoor_target_c=21.0,
        vacation_result=SimpleNamespace(phase="inactive", target_c=21.0),
        _target_recovery=recovery.TargetRecovery(21.0, was_enabled=True),
        _manual_target_adjustment_c=0.0,
        _manual_recovery_since_learning=False,
        _learner_last_step_s=100.0,
        _last_indoor_sensor_set=None,
        is_active=True,
        data=None,
        solar_input_enabled=False,
        heating_type="radiators",
        lag_state=lag.initial_state(15.0),
        learner_state=learner.initial_state(),
        _lag_result=lambda: SimpleNamespace(rise_hours=1.5),
        _schedule_state_save=lambda: None,
    )
    instance._advance_learning = MethodType(namespace["_advance_learning"], instance)
    instance._due_for_learner_step = (
        lambda: clock[0] - instance._learner_last_step_s >= 900.0
    )
    instance.refresh = MethodType(namespace["refresh"], instance)
    return instance, clock, inputs_seen


def test_manual_change_freezes_the_same_learning_step(coordinator):
    instance, _, seen = coordinator
    instance.indoor_target_c = 21.5
    instance.vacation_result.target_c = 21.5
    assert instance.refresh()
    assert instance._manual_target_adjustment_c == -1.0
    assert seen[-1].manual_target_recovery
    assert instance.learner_state.total_samples == 0


@pytest.mark.parametrize("phase", vacation.ACTIVE_PHASES)
def test_vacation_phases_take_precedence_and_do_not_replay_change(coordinator, phase):
    instance, _, _ = coordinator
    instance.vacation_result.phase = phase
    instance.indoor_target_c = 21.5
    instance.refresh()
    assert instance._manual_target_adjustment_c == 0.0
    instance.vacation_result.phase = "inactive"
    instance.refresh()
    assert instance._manual_target_adjustment_c == 0.0


def test_hard_cutoff_cancels_manual_assistance(coordinator):
    instance, _, _ = coordinator
    instance.indoor_target_c = 21.5
    instance.refresh(raw_outdoor_temp_c=30.0)
    assert instance._manual_target_adjustment_c == 0.0
    instance.refresh(raw_outdoor_temp_c=0.0)
    assert instance._manual_target_adjustment_c == 0.0


def test_sensor_loss_suppresses_then_resumes_without_new_deadline(coordinator):
    instance, _, _ = coordinator
    instance.indoor_target_c = 21.5
    instance.refresh()
    deadline = instance._target_recovery.deadline_s
    instance.refresh(indoor_temp_c=None, indoor_ok=False)
    assert instance._manual_target_adjustment_c == 0.0
    assert instance._target_recovery.deadline_s == deadline
    instance.refresh()
    assert instance._manual_target_adjustment_c == -1.0
    assert instance._target_recovery.deadline_s == deadline


def test_recovery_ending_between_learning_ticks_still_freezes_next_step(coordinator):
    instance, clock, seen = coordinator
    instance._learner_last_step_s = clock[0]
    instance.indoor_target_c = 21.5
    instance.refresh()
    assert not seen
    instance.refresh(indoor_temp_c=21.5)
    instance.refresh(indoor_temp_c=21.5)
    assert instance._manual_target_adjustment_c == 0.0
    assert instance._manual_recovery_since_learning
    clock[0] += 900.0
    instance.refresh(indoor_temp_c=21.5)
    assert seen[-1].manual_target_recovery
    assert not instance._manual_recovery_since_learning
    clock[0] += 900.0
    instance.refresh(indoor_temp_c=21.5)
    assert not seen[-1].manual_target_recovery


def test_restored_target_is_consumed_before_manual_changes(coordinator):
    instance, _, _ = coordinator
    instance.is_active = False
    instance.refresh()
    instance.indoor_target_c = 21.5
    instance.is_active = True
    instance.refresh()
    assert instance._manual_target_adjustment_c == 0.0
    instance.indoor_target_c = 22.0
    instance.refresh()
    assert instance._manual_target_adjustment_c < 0.0
