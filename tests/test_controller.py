from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from taskspindle.controller import Controller, ControlServer
from taskspindle.rpc import RemoteError, request
from taskspindle.units import UnitError, UnitState


@pytest.fixture
def controller():
    backend = SimpleNamespace(status=lambda: {"jobs": []}, admission_open=lambda: True,
                              show=lambda unit: UnitState("loaded", "active", "running", "success"))
    return Controller(backend, None, {})


@pytest.mark.parametrize(("operation", "arguments", "diagnostics"), [
    ("run", {"argv": ["sh"]}, False), ("start", {}, True), ("doctor", {"live": True}, False),
    ("status", {"extra": "secret"}, False), ("set_admission", {"open": "yes"}, False),
    ("doctor", {"live": "yes"}, True), ("show", {"unit": []}, False),
    ("kill", {"unit": "a", "signal": {}}, False),
])
def test_operation_and_argument_allowlists(controller, operation, arguments, diagnostics):
    with pytest.raises(RemoteError):
        controller.dispatch(operation, arguments, diagnostics=diagnostics)


def test_private_socket_round_trip_and_error_redaction(controller, tmp_path):
    path = tmp_path / "control.sock"
    with ControlServer(path, controller) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            assert path.stat().st_mode & 0o777 == 0o600
            assert request(path, "status") == {"jobs": []}
            assert request(path, "show", {"unit": "job"})["active_state"] == "active"
            with pytest.raises(RemoteError) as caught:
                request(path, "secret-invalid-operation", {"TOKEN": "private-secret"})
            assert "private-secret" not in str(caught.value)

            def fail():
                raise RuntimeError("private-secret")
            controller.backend.status = fail
            with pytest.raises(RemoteError) as caught:
                request(path, "status")
            assert str(caught.value) == "Control operation failed"
        finally:
            server.shutdown()
            thread.join()


def test_worker_interruption_reconciles_before_returning_status(controller, monkeypatch):
    calls = []
    controller.backend.interrupt_workers = lambda: calls.append("stop")
    controller.backend.status = lambda: calls.append("status") or {"active_reservations": 0}
    module = SimpleNamespace(reconcile_interrupted_workers=lambda *args: calls.append("reconcile"))
    monkeypatch.setitem(sys.modules, "taskspindle.worker_diagnostics", module)
    assert controller.dispatch("interrupt_workers", {}) == {"active_reservations": 0}
    assert calls == ["stop", "reconcile", "status"]


@pytest.mark.parametrize("field,value", [("admission_open", True), ("engine_reachable", False),
                                         ("unsettled_integrations", None), ("unsettled_integrations", 1)])
def test_reconcile_requires_fence_engine_and_no_unsettled_accept(controller, field, value):
    snapshot = {"admission_open": False, "engine_reachable": True, "unsettled_integrations": 0, "jobs": []}
    snapshot[field] = value
    controller.backend.status = lambda: snapshot
    with pytest.raises(UnitError):
        controller.dispatch("reconcile", {})


def test_reconcile_keeps_active_workers_and_does_not_dispatch(controller):
    snapshot = {"admission_open": False, "engine_reachable": True, "unsettled_integrations": 0,
                "jobs": [{"kind": "worker", "state": "active"}]}
    controller.backend.status = lambda: snapshot
    assert controller.dispatch("reconcile", {}) is snapshot
