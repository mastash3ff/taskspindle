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
