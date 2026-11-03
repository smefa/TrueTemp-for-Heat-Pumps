"""Exercise the coordinator's real error wrapper without importing HA."""
import ast
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def coordinator():
    path = Path(__file__).parents[1] / 'custom_components/truetemp/coordinator.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == 'TrueTempCoordinator')
    names = {'_async_update_data', '_async_record_update_event'}
    methods = [n for n in cls.body if getattr(n, 'name', None) in names]
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    clock = [now]
    log = AsyncMock()
    namespace = {'dt_util': SimpleNamespace(utcnow=lambda: clock[0]),
                 'async_log_record': log}
    module = ast.Module(body=[ast.ImportFrom(module='__future__',
                         names=[ast.alias(name='annotations')], level=0), *methods],
                        type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    instance = SimpleNamespace(last_error_at=None, update_events=deque(maxlen=100),
                               data_logging_enabled=True, hass=object(),
                               entry=SimpleNamespace(entry_id='zone'),
                               _async_update_cycle=AsyncMock(), clock=clock)
    for name in names:
        setattr(instance, name, MethodType(namespace[name], instance))
    return instance, log, now


@pytest.mark.parametrize('failure', [ValueError('bad input'),
                                     TimeoutError('source timed out')])
def test_failure_is_saved_and_recovery_keeps_evidence(coordinator, failure):
    instance, log, now = coordinator
    instance._async_update_cycle.side_effect = failure
    with pytest.raises(type(failure)) as raised:
        asyncio.run(instance._async_update_data())
    assert raised.value is failure
    assert instance.last_error_at == now
    error = dict(instance.update_events[0])
    assert error == {'ts': now.isoformat(), 'event': 'error',
                     'error_type': type(failure).__name__, 'error': str(failure)}
    assert log.call_args.args == (instance.hass, 'zone_errors', error)
    result = object()
    instance._async_update_cycle.side_effect = None
    instance._async_update_cycle.return_value = result
    assert asyncio.run(instance._async_update_data()) is result
    assert instance.last_error_at is None
    assert list(instance.update_events) == [error, {'ts': now.isoformat(),
                                                  'event': 'recovered'}]
    asyncio.run(instance._async_update_data())
    assert len(instance.update_events) == 2
    assert log.await_count == 2


def test_logging_off_keeps_bounded_memory_without_writes(coordinator):
    instance, log, _ = coordinator
    instance.data_logging_enabled = False
    instance._async_update_cycle.side_effect = ValueError('unavailable')
    for _ in range(105):
        with pytest.raises(ValueError):
            asyncio.run(instance._async_update_data())
    assert len(instance.update_events) == 100
    log.assert_not_awaited()


def test_repeated_failures_keep_outage_start_and_each_attempt_time(coordinator):
    instance, _, now = coordinator
    instance._async_update_cycle.side_effect = ValueError('unavailable')
    for minute in range(2):
        instance.clock[0] = now + timedelta(minutes=minute)
        with pytest.raises(ValueError):
            asyncio.run(instance._async_update_data())
    assert instance.last_error_at == now
    assert [event['ts'] for event in instance.update_events] == [
        now.isoformat(), (now + timedelta(minutes=1)).isoformat()]


def test_cancellation_is_not_reported_as_sensor_failure(coordinator):
    instance, log, _ = coordinator
    instance._async_update_cycle.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(instance._async_update_data())
    assert not instance.update_events
    log.assert_not_awaited()
