"""Controller-facing core contracts, without a Docker daemon or provider inference."""

import json

import pytest

from taskspindle import doctor, rpc, worker_diagnostics
from taskspindle.config import Paths
from taskspindle.providers import Profile


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "config.toml", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")


def test_docker_doctor_uses_diagnostics_service_not_runtime_tools(paths, monkeypatch):
    calls = []
    expected = {"ok": False, "checks": [{"name": "grok:acp", "ok": False}]}

    def request(socket, operation, arguments, *, timeout):
        calls.append((str(socket), operation, arguments, timeout))
        return expected

    monkeypatch.setattr(rpc, "request", request)
    report = doctor.run_doctor(
        paths=paths, profiles={}, parent_env={},
        settings={"execution": {"backend": "docker", "diagnostics_socket": "/run/probes.sock"}},
        runner=lambda *args, **kwargs: pytest.fail("runtime-local check ran"),
    )
    assert report == expected
    assert calls == [("/run/probes.sock", "doctor", {"live": True}, 300)]


def test_diagnostics_outage_does_not_fall_back_to_systemd(paths, monkeypatch):
    def request(*args, **kwargs):
        raise rpc.RemoteError("CONTROL_UNAVAILABLE", "controller unavailable")

    monkeypatch.setattr(rpc, "request", request)
    report = doctor.run_doctor(
        paths=paths, profiles={}, parent_env={},
        settings={"execution": {"backend": "docker", "diagnostics_socket": "/run/probes.sock"}},
    )
    assert report["ok"] is False
    assert report["checks"][0]["name"] == "worker_diagnostics"


def test_worker_probe_checks_only_selected_profile_and_never_calls_rpc(paths, monkeypatch):
    selected = Profile(id="custom", auth="oauth", command=("/bin/true",))
    seen = []

    async def collect(self):
        seen.append((self.worker_container, tuple(self.profiles)))

    monkeypatch.setattr(doctor._Doctor, "collect", collect)
    monkeypatch.setattr(rpc, "request", lambda *args, **kwargs: pytest.fail("worker called RPC"))
    report = doctor.run_doctor(
        profiles={"custom": selected, "other": selected}, paths=paths,
        parent_env={"TASKSPINDLE_WORKER_CONTAINER": "1", "TASKSPINDLE_PROBE_PROVIDER": "custom"},
        settings={"execution": {"backend": "docker"}},
    )
    assert report["ok"] is True
    assert seen == [(True, ("custom",))]


def test_worker_report_preserves_failed_checks_and_launches_each_provider(paths, monkeypatch):
    profiles = {name: Profile(id=name, auth="oauth", command=("/bin/true",)) for name in ("a", "b")}
    monkeypatch.setattr(worker_diagnostics.providers, "load_profiles", lambda *args, **kwargs: profiles)
    calls = []

    class Backend:
        def run_probe(self, provider, argv, *, env, timeout):
            calls.append((provider, argv, env, timeout))
            check = {"name": "sandbox", "ok": provider == "a", "detail": "observed", "advisory": False}
            return {"exit_code": 0 if provider == "a" else 1,
                    "stdout": json.dumps({"ok": check["ok"], "checks": [check]}), "stderr": "private"}

    report = worker_diagnostics.doctor_report(Backend(), paths, {}, live=False)
    assert report["ok"] is False
    assert [row["name"] for row in report["checks"]] == ["a:sandbox", "b:sandbox"]
    assert all(call[1] == ["taskspindle", "doctor", "--json", "--no-live"] for call in calls)
    assert all(call[2]["TASKSPINDLE_PROBE_PROVIDER"] == call[0] for call in calls)
    assert all(call[3] == 90 for call in calls)
    assert "private" not in json.dumps(report)


def test_worker_probe_failure_is_failure_even_when_controller_is_available(paths, monkeypatch):
    profiles = {"a": Profile(id="a", auth="oauth", command=("/bin/true",))}
    monkeypatch.setattr(worker_diagnostics.providers, "load_profiles", lambda *args, **kwargs: profiles)

    class Backend:
        def run_probe(self, *args, **kwargs):
            return {"exit_code": 127, "stdout": "", "stderr": "private auth contents"}

    report = worker_diagnostics.doctor_report(Backend(), paths, {})
    assert report["ok"] is False
    assert report["checks"][0]["name"] == "a:worker_probe"
    assert "private auth contents" not in json.dumps(report)
