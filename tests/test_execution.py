from pathlib import Path

import pytest

from taskspindle import execution
from taskspindle.config import ConfigError, Paths
from taskspindle.rpc import RemoteError
from taskspindle.units import SystemdUserBackend, UnitError


def test_backend_selection_and_config_validation(tmp_path):
    paths = Paths(*(tmp_path for _ in range(4)))
    assert isinstance(execution.unit_backend(paths, {}), SystemdUserBackend)
    assert isinstance(execution.unit_backend(paths, {"execution": {"backend": "docker",
                                                                   "jobs_socket": "/private/jobs.sock"}}),
                      execution.ControllerClient)
    for value in ([], {"backend": "oops"}, {"backend": "docker", "jobs_socket": "relative"}):
        with pytest.raises(ConfigError):
            execution.unit_backend(paths, {"execution": value})


@pytest.mark.parametrize("operation,expected", [("start", "UNIT_START_UNCERTAIN"),
                                                ("show", "CONTROL_UNAVAILABLE")])
def test_transport_failure_is_uncertain_only_for_start(monkeypatch, operation, expected):
    def unavailable(*args, **kwargs):
        raise RemoteError("CONTROL_UNAVAILABLE", "Unavailable")
    monkeypatch.setattr(execution, "request", unavailable)
    client = execution.ControllerClient("/private/jobs.sock")
    with pytest.raises(UnitError) as caught:
        if operation == "start":
            client.start("unit", [], working_dir=Path("/state"), env={}, properties={})
        else:
            client.show("unit")
    assert caught.value.code == expected


def test_remote_error_code_preserved(monkeypatch):
    def fenced(*args, **kwargs):
        raise RemoteError("UNIT_ADMISSION_CLOSED", "Closed")
    monkeypatch.setattr(execution, "request", fenced)
    with pytest.raises(UnitError) as caught:
        execution.ControllerClient("/private/jobs.sock").start("unit", [], working_dir=Path("/state"),
                                                               env={}, properties={})
    assert caught.value.code == "UNIT_ADMISSION_CLOSED"


def test_lost_submission_reply_is_not_safe_to_retry(monkeypatch):
    def unavailable(*args, **kwargs):
        raise RemoteError('CONTROL_UNAVAILABLE', 'Unavailable')
    monkeypatch.setattr(execution, 'request', unavailable)
    with pytest.raises(UnitError) as caught:
        execution.ControllerClient('/private/jobs.sock').submission_begin('a' * 64)
    assert caught.value.code == 'UNIT_SUBMISSION_UNCERTAIN'


@pytest.mark.parametrize("method", ["start_task", "continue_task", "accept_task"])
def test_start_task_submission_fence_precedes_any_task_mutation(method):
    from types import SimpleNamespace

    from taskspindle.orchestrator import Orchestrator
    from taskspindle.service import TaskSpindleError
    orchestrator = Orchestrator.__new__(Orchestrator)
    def closed(token):
        raise UnitError('UNIT_ADMISSION_CLOSED', 'maintenance')
    orchestrator.units = SimpleNamespace(submission_begin=closed)
    with pytest.raises(TaskSpindleError) as caught:
        arguments = [None, 0] if method == "continue_task" else [None]
        getattr(orchestrator, method)(*arguments)
    assert caught.value.code == 'UNIT_ADMISSION_CLOSED'


def test_worker_reservation_is_inside_submission_guard():
    from types import SimpleNamespace

    from taskspindle.orchestrator import Orchestrator
    orchestrator = Orchestrator.__new__(Orchestrator)
    events = []
    orchestrator.units = SimpleNamespace(submission_begin=lambda token: events.append('begin'),
                                        submission_end=lambda token: events.append('end'))
    orchestrator._start_worker_admitted = lambda *args: events.append('claim/start') or True
    assert orchestrator._start_worker(None)
    assert events == ['begin', 'claim/start', 'end']
