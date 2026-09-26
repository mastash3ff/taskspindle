from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound

from taskspindle import doctor
from taskspindle.config import ConfigError, Paths
from taskspindle.docker_units import DockerBackend
from taskspindle.units import UnitError


class Container:
    def __init__(self, collection, name, options):
        self.collection, self.name = collection, name
        self.id = f"id-{len(collection.created)}"
        self.attrs = {"Config": {"Labels": options["labels"]},
                      "State": {"Status": "created", "ExitCode": 0, "OOMKilled": False, "Pid": 123}}
        self.starts = 0
        self.stops = []
        self.signals = []

    def start(self):
        self.starts += 1
        if self.collection.start_failure == "before":
            raise OSError("secret request contents")
        self.attrs["State"]["Status"] = "running"
        if self.collection.start_failure == "after":
            raise OSError("secret request contents")

    def stop(self, timeout):
        self.stops.append(timeout)
        self.attrs["State"].update(Status="exited", ExitCode=143)

    def kill(self, signal):
        self.signals.append(signal)

    def reload(self):
        return None

    def remove(self):
        del self.collection.values[self.name]

    def wait(self, timeout):
        self.attrs["State"].update(Status="exited", ExitCode=0)
        return {"StatusCode": 0}

    def logs(self, *, stdout, stderr, tail):
        return b"probe output" if stdout else b""


def all_labels(container, wanted):
    labels = container.attrs["Config"]["Labels"]
    return all(labels.get(key) == value for key, value in wanted.items())


class Containers:
    def __init__(self):
        self.values = {}
        self.created = []
        self.create_failure = None
        self.start_failure = None
        self.query_failure = False
        self.entered = None
        self.proceed = None
        self.list_calls = []

    def get(self, identity):
        if self.query_failure:
            raise OSError("secret engine URL")
        for name, container in self.values.items():
            if identity in {name, container.id}:
                return container
        raise NotFound("not found")

    def list(self, all=False, filters=None):
        self.list_calls.append((all, filters))
        if self.query_failure:
            raise OSError("secret engine URL")
        wanted = dict(item.split("=", 1) for item in (filters or {}).get("label", []))
        return [container for container in self.values.values()
                if (all or container.attrs["State"]["Status"] == "running")
                and all_labels(container, wanted)]

    def create(self, image, command, **options):
        if self.entered:
            self.entered.set()
            assert self.proceed.wait(3)
        if self.create_failure == "before":
            raise OSError("lost create request")
        if self.create_failure == "rejected":
            raise APIError("private secret", response=SimpleNamespace(status_code=400))
        if self.create_failure == "no_space":
            raise APIError(
                "private secret TOKEN=never-public",
                response=SimpleNamespace(status_code=500),
                explanation="failed to mount /private/path: no space left on device TOKEN=never-public",
            )
        if self.create_failure == "private_500":
            raise APIError(
                "private secret TOKEN=never-public",
                response=SimpleNamespace(status_code=500),
                explanation="daemon detail includes /private/path and TOKEN=never-public",
            )
        assert options["name"] not in self.values
        container = Container(self, options["name"], options)
        self.created.append((image, command, options, container))
        self.values[container.name] = container
        if self.create_failure == "after":
            raise OSError("lost create response")
        return container


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    config = tmp_path / "config.toml"
    config.write_text("")
    paths = Paths(config, state, tmp_path / "data", tmp_path / "runtime")
    with sqlite3.connect(state / "taskspindle.sqlite3") as db:
        db.executescript("""
            CREATE TABLE tasks(id TEXT, provider TEXT, provider_family TEXT, state TEXT);
            CREATE TABLE turns(id INTEGER, task_id TEXT);
            CREATE TABLE leases(task_id TEXT);
            CREATE TABLE integration_journal(task_id TEXT, started_at TEXT, candidate_sha TEXT);
            INSERT INTO tasks VALUES ('ts_one', 'claude', 'claude', 'RUNNING');
            INSERT INTO turns VALUES (1, 'ts_one');
        """)
    auth = tmp_path / "claude-auth"
    auth.mkdir()
    settings = {"execution": {"worker_image": "sha256:" + "a" * 64,
                              "mounts": [{"source": str(state), "target": str(state), "read_only": False}],
                              "provider_mounts": {"claude": [{"source": str(auth), "target": str(auth),
                                                               "read_only": True}]}}}
    containers = Containers()
    client = SimpleNamespace(containers=containers, images=SimpleNamespace(get=lambda image: object()),
                             ping=lambda: True, event_values=[], event_calls=[])
    client.events = lambda **kwargs: client.event_calls.append(kwargs) or iter(client.event_values)
    return DockerBackend(paths, settings, client=client), client


def launch(backend, unit="taskspindle-worker-ts_one"):
    module = "accept" if "-accept-" in unit else "runner"
    backend.start(unit, ["/host/python", "-m", f"taskspindle.{module}", "--task", "ts_one"],
                  working_dir=backend.paths.state_dir, env={"TOKEN": "never-public"}, properties={})


def destroyed_event(backend, record, *, time_nano=None, **attributes):
    labels = {
        "taskspindle.owner": backend.owner,
        "taskspindle.generation": record["generation"],
        "taskspindle.unit": record["unit"],
        "taskspindle.kind": record["kind"],
        "taskspindle.task": record["task_id"],
        "name": record["name"],
        **attributes,
    }
    return {
        "Action": "destroy",
        "timeNano": time_nano or record.get("reserved_at_ns", 0) + 1,
        "Actor": {"ID": "a" * 64, "Attributes": labels},
    }


def test_one_launch_survives_lost_create_and_start_ack_and_controller_restart(setup):
    backend, client = setup
    client.containers.create_failure = "after"
    client.containers.start_failure = "after"
    launch(backend)
    restarted = DockerBackend(backend.paths, backend.settings, client=client)
    launch(restarted)
    assert restarted.show("taskspindle-worker-ts_one").kind == "active"
    assert len(client.containers.created) == 1
    assert client.containers.created[0][3].starts == 1
    assert "never-public" not in json.dumps(restarted.status())
    assert "TOKEN" not in next(backend.directory.glob("taskspindle-*.json")).read_text()


@pytest.mark.parametrize("failure", ["before", "rejected"])
def test_create_known_failure_vs_uncertain(setup, failure):
    backend, client = setup
    client.containers.create_failure = failure
    expected = "UNIT_START_FAILED" if failure == "rejected" else "UNIT_START_UNCERTAIN"
    with pytest.raises(UnitError) as caught:
        launch(backend)
    assert caught.value.code == expected
    assert "private secret" not in str(caught.value)
    if failure == "before":
        with pytest.raises(UnitError, match="missing without terminal"):
            backend.show("taskspindle-worker-ts_one")
        with pytest.raises(UnitError) as retry:
            launch(backend)
        assert retry.value.code == "UNIT_START_UNCERTAIN"
    assert not client.containers.created


@pytest.mark.parametrize(
    ("failure", "expected_detail"),
    [("no_space", "HTTP 500; no space left on device"), ("private_500", "HTTP 500")],
)
def test_uncertain_create_preserves_only_safe_bounded_diagnostics(setup, failure, expected_detail):
    backend, client = setup
    client.containers.create_failure = failure

    with pytest.raises(UnitError) as caught:
        launch(backend)

    assert caught.value.code == "UNIT_START_UNCERTAIN"
    assert expected_detail in str(caught.value)
    assert "private" not in str(caught.value)
    assert "TOKEN" not in str(caught.value)
    assert not client.containers.created


def test_failed_create_recovery_requires_exact_destroy_and_keeps_fence_closed(setup):
    backend, client = setup
    client.containers.create_failure = "before"
    with pytest.raises(UnitError):
        launch(backend)
    path = backend.directory / "taskspindle-worker-ts_one.json"
    record = json.loads(path.read_text())

    with pytest.raises(UnitError) as unproven:
        backend.recover_failed_create(record["unit"])
    assert unproven.value.code == "UNIT_RECOVERY_UNPROVEN"
    assert backend.admission_open() is False
    assert json.loads(path.read_text())["phase"] == "creating"

    client.event_values = [destroyed_event(backend, record, **{"taskspindle.generation": "wrong"})]
    with pytest.raises(UnitError) as wrong_generation:
        backend.recover_failed_create(record["unit"])
    assert wrong_generation.value.code == "UNIT_RECOVERY_UNPROVEN"

    client.event_values = [
        destroyed_event(backend, record, time_nano=record["reserved_at_ns"] - 1),
    ]
    with pytest.raises(UnitError) as before_reservation:
        backend.recover_failed_create(record["unit"])
    assert before_reservation.value.code == "UNIT_RECOVERY_UNPROVEN"

    client.event_values = [destroyed_event(backend, record)]
    recovered = backend.recover_failed_create(record["unit"])
    assert recovered.kind == "not_found"
    persisted = json.loads(path.read_text())
    assert persisted["phase"] == "failed"
    assert persisted["recovery"] == {
        "kind": "destroy", "time_nano": record["reserved_at_ns"] + 1,
        "container_id": "a" * 64,
    }
    assert backend.show(record["unit"]).kind == "not_found"
    assert client.event_calls[-1]["filters"]["label"] == [
        f"taskspindle.owner={backend.owner}",
        f"taskspindle.generation={record['generation']}",
    ]


def test_failed_create_recovery_accepts_legacy_record_mtime_as_lower_bound(setup):
    backend, client = setup
    client.containers.create_failure = "before"
    with pytest.raises(UnitError):
        launch(backend)
    path = backend.directory / "taskspindle-worker-ts_one.json"
    record = json.loads(path.read_text())
    del record["reserved_at_ns"]
    path.write_text(json.dumps(record))
    lower_bound = path.stat().st_mtime_ns
    client.event_values = [destroyed_event(backend, record, time_nano=lower_bound + 1)]

    assert backend.recover_failed_create(record["unit"]).kind == "not_found"
    assert json.loads(path.read_text())["recovery"]["time_nano"] == lower_bound + 1


def test_failed_create_recovery_never_uses_500_or_absence_without_destroy(setup):
    backend, client = setup
    client.containers.create_failure = "private_500"
    with pytest.raises(UnitError) as create:
        launch(backend)
    assert create.value.code == "UNIT_START_UNCERTAIN"
    client.ping = lambda: (_ for _ in ()).throw(OSError("private engine endpoint"))

    with pytest.raises(UnitError) as unavailable:
        backend.recover_failed_create("taskspindle-worker-ts_one")

    assert unavailable.value.code == "UNIT_QUERY_FAILED"
    assert "private" not in str(unavailable.value)
    assert json.loads(next(backend.directory.glob("taskspindle-*.json")).read_text())["phase"] == "creating"


def test_failed_create_recovery_refuses_a_present_or_reappearing_generation(setup, monkeypatch):
    backend, client = setup
    client.containers.create_failure = "before"
    with pytest.raises(UnitError):
        launch(backend)
    path = backend.directory / "taskspindle-worker-ts_one.json"
    record = json.loads(path.read_text())
    options = {"labels": {
        "taskspindle.owner": backend.owner, "taskspindle.generation": record["generation"],
    }}
    present = Container(client.containers, record["name"], options)
    client.containers.values[record["name"]] = present

    with pytest.raises(UnitError) as still_present:
        backend.recover_failed_create(record["unit"])
    assert still_present.value.code == "UNIT_RECOVERY_UNPROVEN"

    client.containers.values.clear()
    client.event_values = [destroyed_event(backend, record)]
    observations = iter([None, present])
    monkeypatch.setattr(backend, "_container", lambda _record: next(observations))
    with pytest.raises(UnitError) as reappeared:
        backend.recover_failed_create(record["unit"])
    assert reappeared.value.code == "UNIT_RECOVERY_UNPROVEN"
    assert json.loads(path.read_text())["phase"] == "creating"


def test_failed_create_recovery_has_an_absolute_event_deadline(setup, monkeypatch):
    backend, client = setup
    client.containers.create_failure = "before"
    with pytest.raises(UnitError):
        launch(backend)
    record = json.loads(next(backend.directory.glob("taskspindle-*.json")).read_text())

    class SlowEvents:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.closed:
                raise StopIteration
            time.sleep(0.01)
            return {"Action": "partial"}

        def close(self):
            self.closed = True

    stream = SlowEvents()
    client.events = lambda **_kwargs: stream
    monkeypatch.setattr("taskspindle.docker_units._RECOVERY_EVENTS_TIMEOUT_S", 0.05)

    with pytest.raises(UnitError) as timed_out:
        backend.recover_failed_create(record["unit"])

    assert timed_out.value.code == "UNIT_QUERY_FAILED"
    assert "timed out" in str(timed_out.value)
    assert stream.closed is True
    assert json.loads(next(backend.directory.glob("taskspindle-*.json")).read_text())["phase"] == "creating"


def test_unsettled_start_never_retries_or_reports_absence(setup):
    backend, client = setup
    client.containers.start_failure = "before"
    with pytest.raises(UnitError) as caught:
        launch(backend)
    assert caught.value.code == "UNIT_START_UNCERTAIN"
    with pytest.raises(UnitError) as observed:
        backend.show("taskspindle-worker-ts_one")
    assert observed.value.code == "UNIT_START_UNCERTAIN"
    with pytest.raises(UnitError):
        launch(backend)
    assert client.containers.created[0][3].starts == 1


def test_query_outage_is_never_missing(setup):
    backend, client = setup
    launch(backend)
    client.containers.query_failure = True
    with pytest.raises(UnitError) as caught:
        backend.show("taskspindle-worker-ts_one")
    assert caught.value.code == "UNIT_QUERY_FAILED"
    assert "secret" not in str(caught.value)
    assert backend.status()["jobs"][0]["state"] == "unknown"


@pytest.mark.parametrize(("exit_code", "oom", "kind"), [(0, False, "success"), (7, False, "exit"),
                                                          (137, True, "oom"), (143, False, "signal")])
def test_exit_evidence_survives_cleanup(setup, exit_code, oom, kind):
    backend, client = setup
    launch(backend)
    container = client.containers.created[0][3]
    container.attrs["State"].update(Status="exited", ExitCode=exit_code, OOMKilled=oom)
    assert backend.show("taskspindle-worker-ts_one").kind == kind
    backend.reset_failed("taskspindle-worker-ts_one")
    assert backend.show("taskspindle-worker-ts_one").kind == kind
    assert not client.containers.values
    with pytest.raises(UnitError):
        launch(backend)


def test_new_turn_gets_new_generation_without_reusing_stale_id(setup):
    backend, client = setup
    launch(backend)
    backend.stop("taskspindle-worker-ts_one")
    backend.reset_failed("taskspindle-worker-ts_one")
    with sqlite3.connect(backend.paths.state_dir / "taskspindle.sqlite3") as db:
        db.execute("INSERT INTO turns VALUES (2, 'ts_one')")
    launch(backend)
    assert len(client.containers.created) == 2
    assert client.containers.created[0][2]["name"] != client.containers.created[1][2]["name"]
    assert len(list(backend.directory.glob("history-*.json"))) == 1


def test_stop_uses_entire_container_grace_and_preserves_state(setup):
    backend, client = setup
    launch(backend)
    backend.kill("taskspindle-worker-ts_one", "SIGTERM")
    container = client.containers.created[0][3]
    assert container.stops == [30]
    assert backend.show("taskspindle-worker-ts_one").kind == "signal"


def test_fence_serializes_with_launch_and_persists_across_restart(setup):
    backend, client = setup
    client.containers.entered = threading.Event()
    client.containers.proceed = threading.Event()
    launch_thread = threading.Thread(target=launch, args=(backend,))
    launch_thread.start()
    assert client.containers.entered.wait(2)
    fenced = threading.Event()

    def fence():
        backend.set_admission(False)
        fenced.set()

    fence_thread = threading.Thread(target=fence)
    fence_thread.start()
    assert not fenced.wait(0.05)
    client.containers.proceed.set()
    launch_thread.join(3)
    fence_thread.join(3)
    assert fenced.is_set()
    restarted = DockerBackend(backend.paths, backend.settings, client=client)
    assert not restarted.admission_open()
    with pytest.raises(UnitError) as caught:
        launch(restarted)
    assert caught.value.code == "UNIT_ADMISSION_CLOSED"
    assert len(client.containers.created) == 1


def test_accept_gets_no_auth_mount_and_interrupt_refuses_journal(setup):
    backend, client = setup
    with sqlite3.connect(backend.paths.state_dir / "taskspindle.sqlite3") as db:
        db.execute("INSERT INTO integration_journal VALUES ('ts_one', 'now', 'sha')")
    launch(backend, "taskspindle-accept-ts_one")
    mounts = client.containers.created[0][2]["mounts"]
    assert len(mounts) == 1
    with pytest.raises(UnitError) as caught:
        backend.interrupt_workers()
    assert caught.value.code == "ACCEPT_IN_FLIGHT"
    assert not backend.admission_open()
    assert backend.status()["unsettled_integrations"] == 1
    assert not client.containers.created[0][3].stops


def test_muse_worker_is_allowlisted_without_other_provider_auth(setup):
    backend, client = setup
    with sqlite3.connect(backend.paths.state_dir / "taskspindle.sqlite3") as db:
        db.execute("UPDATE tasks SET provider='muse', provider_family='muse'")
    launch(backend)
    options = client.containers.created[0][2]
    assert {mount["Source"] for mount in options["mounts"]} == {str(backend.paths.state_dir)}
    assert options["read_only"] is True
    assert options["cap_drop"] == ["ALL"]
    assert options["privileged"] is False


def test_resource_security_and_image_runtime_contract(setup):
    backend, client = setup
    launch(backend)
    _, command, options, _ = client.containers.created[0]
    assert command[0] == "python"
    assert options["mem_limit"] == 3 * 1024**3
    assert options["memswap_limit"] == int(3.5 * 1024**3)
    assert options["mem_reservation"] == 2 * 1024**3
    assert options["restart_policy"] == {"Name": "no"}
    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert options["privileged"] is False
    assert options["environment"]["XDG_DATA_HOME"] == "/opt/taskspindle/data"


def test_malformed_record_and_wrong_container_generation_fail_closed(setup):
    backend, client = setup
    record = backend.directory / "taskspindle-worker-ts_one.json"
    record.write_text('{"generation":"oops"}')
    with pytest.raises(UnitError) as caught:
        launch(backend)
    assert caught.value.code == "UNIT_RECORD_INVALID"
    record.unlink()
    launch(backend)
    client.containers.created[0][3].attrs["Config"]["Labels"]["taskspindle.generation"] = "stale"
    with pytest.raises(UnitError) as caught:
        backend.stop("taskspindle-worker-ts_one")
    assert caught.value.code == "UNIT_RECORD_INVALID"


def test_mount_sources_are_required_and_socket_ancestors_rejected(setup):
    backend, client = setup
    backend.config["mounts"][0]["source"] += "/missing"
    backend.config["mounts"][0]["target"] += "/missing"
    with pytest.raises(UnitError):
        launch(backend)
    assert not client.containers.created
    path = str(backend.paths.state_dir)
    backend.config["mounts"] = [{"source": path, "target": path, "read_only": False}]
    backend.config["jobs_socket"] = path + "/private/jobs.sock"
    with pytest.raises(ConfigError):
        launch(backend)


def test_probe_uses_auth_and_config_with_private_state_and_cleans_up(setup):
    backend, client = setup
    result = backend.run_probe("claude", ["taskspindle", "doctor", "--json", "--no-live"])
    assert result == {"exit_code": 0, "stdout": "probe output", "stderr": ""}
    options = client.containers.created[0][2]
    assert str(backend.paths.state_dir) in options["tmpfs"]
    assert all(mount["Source"] != str(backend.paths.state_dir) for mount in options["mounts"])
    assert not client.containers.values
    assert not list(backend.directory.glob("taskspindle-*.json"))


def test_muse_probe_uses_task_home_without_other_provider_auth(setup):
    backend, client = setup
    muse_home = backend.paths.state_dir / "ts_muse" / "muse-home"
    environment = {
        "HOME": str(muse_home),
        "XDG_CONFIG_HOME": str(muse_home / ".config"),
        "XDG_CACHE_HOME": str(muse_home / ".cache"),
        "XDG_DATA_HOME": str(muse_home / ".local/share"),
        "XDG_STATE_HOME": str(muse_home / ".local/state"),
    }
    result = backend.run_probe(
        "muse", [str(backend.paths.runtime_dir / "muse"), "serve"], env=environment,
    )
    assert result["exit_code"] == 0
    _, command, options, _ = client.containers.created[0]
    assert command == [str(backend.paths.runtime_dir / "muse"), "serve"]
    assert all(options["environment"][key] == value for key, value in environment.items())
    assert {mount["Source"] for mount in options["mounts"]} == {str(backend.paths.config_file)}
    assert options["read_only"] is True
    assert options["cap_drop"] == ["ALL"]
    assert options["privileged"] is False


@pytest.mark.parametrize("provider", ["agy", "grok", "claude", None])
@pytest.mark.parametrize("probe", [False, True])
def test_restrictive_seccomp_applies_only_namespace_providers(setup, tmp_path, provider, probe):
    backend, _ = setup
    profile = tmp_path / "seccomp.json"
    profile.write_text(json.dumps({"defaultAction": "SCMP_ACT_ERRNO", "syscalls": []}))
    backend.config["seccomp_profile"] = str(profile)
    options = backend._options(provider, probe=probe)
    expected = ["no-new-privileges:true"]
    if provider in {"agy", "grok"}:
        expected.append('seccomp={"defaultAction":"SCMP_ACT_ERRNO","syscalls":[]}')
    assert options["security_opt"] == expected
    assert options["cap_drop"] == ["ALL"]
    assert options["user"] == "1000:1000"
    assert options["read_only"] is True
    assert options["privileged"] is False


def test_malformed_terminal_evidence_cannot_authorize_relaunch_or_cleanup(setup):
    backend, _ = setup
    launch(backend)
    path = backend.directory / "taskspindle-worker-ts_one.json"
    record = json.loads(path.read_text())
    record["phase"] = "finished"
    record["evidence"] = {"result": "success"}
    path.write_text(json.dumps(record))
    for method in (backend.show, backend.reset_failed):
        with pytest.raises(UnitError) as caught:
            method("taskspindle-worker-ts_one")
        assert caught.value.code == "UNIT_RECORD_INVALID"


def test_running_older_turn_is_not_acknowledged_as_new_launch(setup):
    backend, client = setup
    launch(backend)
    with sqlite3.connect(backend.paths.state_dir / "taskspindle.sqlite3") as db:
        db.execute("INSERT INTO turns VALUES (2, 'ts_one')")
    with pytest.raises(UnitError) as caught:
        launch(backend)
    assert caught.value.code == "UNIT_PREVIOUS_TURN_ACTIVE"
    assert len(client.containers.created) == 1


def test_probe_lost_create_reply_is_cleaned_without_inference_replay(setup):
    backend, client = setup
    client.containers.create_failure = "after"
    with pytest.raises(UnitError) as caught:
        backend.run_probe("claude", ["taskspindle", "doctor", "--json", "--no-live"])
    assert caught.value.code == "DIAGNOSTIC_FAILED"
    assert not client.containers.values
    assert client.containers.created[0][3].starts == 0


def test_status_includes_readonly_reservations_and_nonterminal_tasks(setup):
    backend, _ = setup
    with sqlite3.connect(backend.paths.state_dir / "taskspindle.sqlite3") as db:
        db.execute("INSERT INTO leases VALUES ('ts_one')")
    status = backend.status()
    assert status["active_reservations"] == 1
    assert status["nonterminal_worker_tasks"] == 1
    assert status["unsettled_integrations"] == 0
    assert not status["jobs"]


def test_accepting_task_without_journal_still_blocks_worker_interruption(setup):
    backend, _ = setup
    with sqlite3.connect(backend.paths.state_dir / "taskspindle.sqlite3") as db:
        db.execute("UPDATE tasks SET state='ACCEPTING'")
    with pytest.raises(UnitError) as caught:
        backend.interrupt_workers()
    assert caught.value.code == "ACCEPT_IN_FLIGHT"
    assert backend.status()["unsettled_integrations"] == 1


def test_maintenance_survives_restart_and_legacy_boolean_overwrite(setup):
    backend, client = setup
    token = 'a' * 64
    backend.maintenance_acquire(token)
    (backend.directory / 'admission.json').write_text('{"open":true}')
    restarted = DockerBackend(backend.paths, backend.settings, client=client)
    assert restarted.admission_open() is False
    assert restarted.status()['maintenance_protocol'] == 1
    with pytest.raises(UnitError, match='owner'):
        restarted.set_admission(True)
    with pytest.raises(UnitError):
        launch(restarted)
    with pytest.raises(UnitError):
        restarted.submission_begin('b' * 64)
    with pytest.raises(UnitError):
        restarted.maintenance_release('c' * 64, True)
    restarted.maintenance_release(token, True)
    restarted.maintenance_release(token, True)  # lost reply is idempotent
    assert restarted.admission_open()


def test_submission_is_durable_and_blocks_maintenance_release(setup):
    backend, client = setup
    backend.submission_begin('b' * 64)
    backend.maintenance_acquire('a' * 64)
    restarted = DockerBackend(backend.paths, backend.settings, client=client)
    assert restarted.status()['active_submissions'] == 1
    with pytest.raises(UnitError, match='Submissions remain'):
        restarted.maintenance_release('a' * 64, True)
    restarted.submission_end('b' * 64)
    assert restarted.status()['active_submissions'] == 0
    restarted.maintenance_release('a' * 64, True)


def test_maintenance_cannot_steal_operator_fence_or_other_owner(setup):
    backend, _ = setup
    backend.set_admission(False)
    with pytest.raises(UnitError):
        backend.maintenance_acquire('a' * 64)
    backend.set_admission(True)
    backend.maintenance_acquire('a' * 64)
    with pytest.raises(UnitError):
        backend.maintenance_acquire('b' * 64)
    backend.maintenance_acquire('a' * 64)


def test_concurrent_submission_open_and_start_cannot_cross_owned_fence(setup):
    from concurrent.futures import ThreadPoolExecutor
    backend, _ = setup
    backend.maintenance_acquire('a' * 64)
    actions = [lambda: backend.set_admission(True), lambda: launch(backend),
               lambda: backend.submission_begin('b' * 64)]
    def refused(action):
        with pytest.raises(UnitError):
            action()
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(refused, actions))
    assert backend.status()['active_submissions'] == 0
    assert backend.status()['jobs'] == []


def test_crash_during_release_keeps_independent_token_closed(setup, monkeypatch):
    import taskspindle.docker_units as module
    backend, _ = setup
    token = 'a' * 64
    backend.maintenance_acquire(token)
    atomic = module._atomic
    def fail(path, value):
        if path.name == 'maintenance.json' and value['active'] is False:
            raise OSError('crash')
        atomic(path, value)
    monkeypatch.setattr(module, '_atomic', fail)
    with pytest.raises(OSError):
        backend.maintenance_release(token, True)
    assert json.loads((backend.directory / 'admission.json').read_text())['open'] is True
    assert backend.admission_open() is False


def test_maintenance_acquisition_serializes_with_a_launch_already_in_progress(setup):
    backend, client = setup
    client.containers.entered = threading.Event()
    client.containers.proceed = threading.Event()
    launching = threading.Thread(target=launch, args=(backend,))
    launching.start()
    assert client.containers.entered.wait(2)
    done = threading.Event()
    def acquire():
        backend.maintenance_acquire('a' * 64)
        done.set()
    acquiring = threading.Thread(target=acquire)
    acquiring.start()
    assert not done.wait(0.05)
    client.containers.proceed.set()
    launching.join(3)
    acquiring.join(3)
    assert done.is_set()
    assert backend.status()['admission_open'] is False
    assert len(backend.status()['jobs']) == 1


def test_maintenance_acquire_crash_before_legacy_write_is_still_closed(setup, monkeypatch):
    import taskspindle.docker_units as module
    backend, client = setup
    atomic = module._atomic
    def fail(path, value):
        if path.name == 'admission.json':
            raise OSError('crash before legacy flag')
        atomic(path, value)
    monkeypatch.setattr(module, '_atomic', fail)
    with pytest.raises(OSError):
        backend.maintenance_acquire('a' * 64)
    restarted = DockerBackend(backend.paths, backend.settings, client=client)
    assert restarted.admission_open() is False
    with pytest.raises(UnitError):
        restarted.submission_begin('b' * 64)


# -- safe detail for non-API Engine failures -----------------------------------------------------


def test_create_transport_failure_names_only_the_exception_class(setup):
    backend, client = setup
    client.containers.create_failure = "before"

    with pytest.raises(UnitError) as caught:
        launch(backend)

    assert caught.value.code == "UNIT_START_UNCERTAIN"
    assert str(caught.value) == "Docker create outcome is unsettled (OSError)"


def test_start_transport_failure_names_only_the_exception_class(setup):
    backend, client = setup
    client.containers.start_failure = "before"

    with pytest.raises(UnitError) as caught:
        launch(backend)

    assert caught.value.code == "UNIT_START_UNCERTAIN"
    assert str(caught.value) == "Docker start outcome is unsettled (OSError)"
    assert "secret" not in str(caught.value)


def test_image_and_probe_failures_keep_only_safe_detail(setup):
    backend, client = setup
    client.images = SimpleNamespace(get=lambda image: (_ for _ in ()).throw(
        APIError("private registry TOKEN=never-public", response=SimpleNamespace(status_code=404)),
    ))
    with pytest.raises(UnitError) as missing:
        launch(backend)
    assert str(missing.value) == "Worker image is not locally available (HTTP 404)"

    client.images = SimpleNamespace(get=lambda image: (_ for _ in ()).throw(TimeoutError("private URL")))
    with pytest.raises(UnitError) as probe:
        backend.run_probe("claude", ["taskspindle", "doctor", "--json", "--no-live"])
    assert probe.value.code == "DIAGNOSTIC_FAILED"
    assert str(probe.value) == "Isolated worker diagnostic failed (TimeoutError)"


# -- recovery of a launch that provably never ran ------------------------------------------------


def full_schema(backend):
    """Add the production columns absent-launch recovery and doctor read to the small fixture."""
    with sqlite3.connect(backend.paths.state_dir / "taskspindle.sqlite3") as db:
        for table, column in (
            ("tasks", "started_at"), ("tasks", "heartbeat_at"), ("tasks", "worker_pid"),
            ("turns", "ended_at"), ("leases", "provider"), ("leases", "unit_name"),
            ("leases", "acquired_at"), ("leases", "heartbeat_at"),
        ):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")


def age_record(backend, unit="taskspindle-worker-ts_one", seconds=7200, *, mtime=True):
    path = backend.directory / f"{unit}.json"
    record = json.loads(path.read_text())
    past = time.time_ns() - seconds * 1_000_000_000
    record["reserved_at_ns"] = past
    path.write_text(json.dumps(record))
    if mtime:
        os.utime(path, ns=(past, past))
    return path, record


def execute(backend, statement):
    with sqlite3.connect(backend.paths.state_dir / "taskspindle.sqlite3") as db:
        db.execute(statement)


def absent_launch(backend, client, *, seconds=7200):
    """A create that failed before Docker made anything, for a task the operator cancelled."""
    full_schema(backend)
    client.containers.create_failure = "before"
    with pytest.raises(UnitError):
        launch(backend)
    client.containers.create_failure = None
    execute(backend, "UPDATE tasks SET state='CANCELLING' WHERE id='ts_one'")
    return age_record(backend, seconds=seconds)


def test_absent_launch_recovery_retires_a_cancelled_create_that_never_ran(setup):
    backend, client = setup
    path, record = absent_launch(backend, client)

    recovered = backend.recover_absent_launch(record["unit"])

    assert recovered.kind == "not_found"
    assert backend.admission_open() is False
    persisted = json.loads(path.read_text())
    assert persisted["phase"] == "failed"
    evidence = persisted["recovery"]
    assert evidence["kind"] == "absent" and evidence["attested"] is True
    assert evidence["reserved_at_ns"] == record["reserved_at_ns"]
    assert evidence["checked_at_ns"] >= record["reserved_at_ns"] + 3600 * 1_000_000_000
    assert evidence["engine_reachable"] is True and evidence["containers"] == 0
    assert evidence["container_checks"] == ["name", "generation", "unit"]
    assert {key: evidence[key] for key in (
        "task_state", "started_at", "heartbeat_at", "worker_pid", "ended_turns",
    )} == {"task_state": "CANCELLING", "started_at": None, "heartbeat_at": None,
           "worker_pid": None, "ended_turns": 0}
    assert backend.show(record["unit"]).kind == "not_found"
    owner = f"taskspindle.owner={backend.owner}"
    assert client.containers.list_calls == [
        (True, {"label": [owner, f"taskspindle.generation={record['generation']}"]}),
        (True, {"label": [owner, f"taskspindle.unit={record['unit']}"]}),
    ]

    with pytest.raises(UnitError) as again:
        backend.recover_absent_launch(record["unit"])
    assert again.value.code == "UNIT_RECOVERY_INVALID"


def test_absent_launch_recovery_accepts_a_legacy_record_by_its_mtime(setup):
    backend, client = setup
    path, record = absent_launch(backend, client)
    del record["reserved_at_ns"]
    path.write_text(json.dumps(record))
    past = time.time_ns() - 7200 * 1_000_000_000
    os.utime(path, ns=(past, past))

    assert backend.recover_absent_launch(record["unit"]).kind == "not_found"
    assert json.loads(path.read_text())["recovery"]["reserved_at_ns"] == past


@pytest.mark.parametrize(("change", "code"), [
    ("UPDATE tasks SET state='RUNNING'", "UNIT_RECOVERY_NOT_CANCELLED"),
    ("UPDATE tasks SET state='CANCELLED'", "UNIT_RECOVERY_NOT_CANCELLED"),
    ("UPDATE tasks SET started_at='2030-01-01T00:00:00Z'", "UNIT_RECOVERY_TASK_STARTED"),
    ("UPDATE tasks SET heartbeat_at='2030-01-01T00:00:00Z'", "UNIT_RECOVERY_TASK_STARTED"),
    ("UPDATE tasks SET worker_pid=42", "UNIT_RECOVERY_TASK_STARTED"),
    ("UPDATE turns SET ended_at='2030-01-01T00:00:00Z'", "UNIT_RECOVERY_TASK_STARTED"),
    ("DELETE FROM tasks", "UNIT_RECOVERY_INVALID"),
])
def test_absent_launch_recovery_refuses_any_sign_the_task_ran(setup, change, code):
    backend, client = setup
    path, record = absent_launch(backend, client)
    execute(backend, change)

    with pytest.raises(UnitError) as refused:
        backend.recover_absent_launch(record["unit"])

    assert refused.value.code == code
    assert backend.admission_open() is False
    assert json.loads(path.read_text())["phase"] == "creating"
    assert client.containers.list_calls == []


@pytest.mark.parametrize("present", ["name", "foreign_name", "generation", "unit"])
def test_absent_launch_recovery_refuses_any_owned_container_in_any_state(setup, present):
    backend, client = setup
    path, record = absent_launch(backend, client)
    owner = {"taskspindle.owner": backend.owner}
    name, labels = {
        "name": (record["name"], {**owner, "taskspindle.generation": record["generation"]}),
        "foreign_name": (record["name"], {"taskspindle.owner": "someone-else"}),
        "generation": ("renamed", {**owner, "taskspindle.generation": record["generation"]}),
        "unit": ("older", {**owner, "taskspindle.generation": "b" * 32, "taskspindle.unit": record["unit"]}),
    }[present]
    container = Container(client.containers, name, {"labels": labels})
    container.attrs["State"]["Status"] = "exited"
    client.containers.values[name] = container

    with pytest.raises(UnitError) as refused:
        backend.recover_absent_launch(record["unit"])

    assert refused.value.code == "UNIT_RECOVERY_UNPROVEN"
    assert json.loads(path.read_text())["phase"] == "creating"


def test_absent_launch_recovery_requires_a_reachable_engine_and_listing(setup):
    backend, client = setup
    path, record = absent_launch(backend, client)
    client.ping = lambda: (_ for _ in ()).throw(OSError("private engine endpoint"))

    with pytest.raises(UnitError) as unreachable:
        backend.recover_absent_launch(record["unit"])
    assert unreachable.value.code == "UNIT_QUERY_FAILED"
    assert str(unreachable.value) == "Docker Engine is unreachable (OSError)"

    client.ping = lambda: True
    client.containers.query_failure = True
    with pytest.raises(UnitError) as unobservable:
        backend.recover_absent_launch(record["unit"])
    assert unobservable.value.code == "UNIT_QUERY_FAILED"
    assert "secret" not in str(unobservable.value)
    assert json.loads(path.read_text())["phase"] == "creating"


def test_absent_launch_recovery_requires_an_hour_old_creating_worker_record(setup):
    backend, client = setup
    path, record = absent_launch(backend, client, seconds=3500)
    with pytest.raises(UnitError) as recent:
        backend.recover_absent_launch(record["unit"])
    assert recent.value.code == "UNIT_RECOVERY_TOO_RECENT"

    # An old reservation time never outweighs a newer file: the later of the two decides.
    path, record = age_record(backend, seconds=7200, mtime=False)
    with pytest.raises(UnitError) as rewritten:
        backend.recover_absent_launch(record["unit"])
    assert rewritten.value.code == "UNIT_RECOVERY_TOO_RECENT"

    for phase in ("starting", "started", "finished"):
        path.write_text(json.dumps({**record, "phase": phase, "evidence": {
            "load_state": "loaded", "active_state": "inactive", "sub_state": "exited",
            "result": "success", "exec_main_status": 0, "main_pid": None,
        }}))
        with pytest.raises(UnitError) as settled:
            backend.recover_absent_launch(record["unit"])
        assert settled.value.code == "UNIT_RECOVERY_INVALID"

    with pytest.raises(UnitError) as missing:
        backend.recover_absent_launch("taskspindle-worker-ts_none")
    assert missing.value.code == "UNIT_RECOVERY_INVALID"
    with pytest.raises(UnitError) as invalid:
        backend.recover_absent_launch("../escape")
    assert invalid.value.code == "UNIT_INVALID"
    assert client.containers.list_calls == []


def test_absent_launch_recovery_never_applies_to_an_accept_launch(setup):
    backend, _client = setup
    full_schema(backend)
    execute(backend, "UPDATE tasks SET state='CANCELLING'")
    generation = "c" * 32
    path = backend.directory / "taskspindle-accept-ts_one.json"
    path.write_text(json.dumps({
        "unit": "taskspindle-accept-ts_one", "kind": "accept", "task_id": "ts_one", "identity": "[]",
        "generation": generation, "name": f"taskspindle-{backend.owner}-{generation}",
        "phase": "creating", "reserved_at_ns": 1,
    }))
    os.utime(path, ns=(1, 1))

    with pytest.raises(UnitError) as refused:
        backend.recover_absent_launch("taskspindle-accept-ts_one")

    assert refused.value.code == "UNIT_RECOVERY_INVALID"
    assert json.loads(path.read_text())["phase"] == "creating"


def test_absent_launch_recovery_serializes_with_a_launch(setup):
    backend, client = setup
    _path, record = absent_launch(backend, client)
    execute(backend, "INSERT INTO tasks (id, provider, provider_family, state) "
                     "VALUES ('ts_two', 'claude', 'claude', 'RUNNING')")
    execute(backend, "INSERT INTO turns (id, task_id) VALUES (3, 'ts_two')")
    client.containers.entered, client.containers.proceed = threading.Event(), threading.Event()
    launching = threading.Thread(target=lambda: backend.start(
        "taskspindle-worker-ts_two", ["/host/python", "-m", "taskspindle.runner", "--task", "ts_two"],
        working_dir=backend.paths.state_dir, env={}, properties={},
    ))
    launching.start()
    assert client.containers.entered.wait(3)
    results = []
    recovering = threading.Thread(target=lambda: results.append(
        backend.recover_absent_launch(record["unit"]).kind,
    ))
    recovering.start()
    recovering.join(0.2)
    assert recovering.is_alive() and results == []
    client.containers.proceed.set()
    launching.join(3)
    recovering.join(3)
    assert results == ["not_found"]
    assert backend.admission_open() is False


# -- doctor facts ---------------------------------------------------------------------------------


def test_execution_diagnostics_report_fence_stuck_launch_and_its_lease(setup):
    backend, client = setup
    path, record = absent_launch(backend, client, seconds=1800)
    execute(backend, "INSERT INTO leases (task_id, provider, unit_name, acquired_at, heartbeat_at) "
                     "VALUES ('ts_one', 'claude', 'taskspindle-worker-ts_one', "
                     "'2030-01-01T00:00:00Z', '2030-01-01T00:00:00Z')")
    backend.set_admission(False)

    facts = backend.execution_diagnostics()

    assert facts["admission_open"] is False and facts["maintenance_active"] is False
    [stuck] = facts["unsettled"]
    assert {key: stuck[key] for key in ("unit", "task_id", "phase", "container")} == {
        "unit": record["unit"], "task_id": "ts_one", "phase": "creating", "container": "absent",
    }
    assert 1790 <= stuck["age_s"] <= 1900
    assert facts["leases"] == [{"provider": "claude", "task_id": "ts_one",
                                "unit_name": "taskspindle-worker-ts_one",
                                "acquired_at": "2030-01-01T00:00:00Z",
                                "heartbeat_at": "2030-01-01T00:00:00Z"}]
    assert facts["tasks"] == {"ts_one": {"state": "CANCELLING", "heartbeat_at": None}}

    # The lease is fresh by the clock it was written with, yet its unit is unsettled.
    checks = {check.name: check for check in doctor.execution_state_checks(
        facts, datetime(2030, 1, 1, 0, 1, tzinfo=UTC),
    )}
    assert checks["worker_admission"].ok is False
    assert "taskspindle-controller open" in checks["worker_admission"].detail
    assert checks["stuck_launches"].ok is False
    stuck_detail = checks["stuck_launches"].detail
    assert f"{record['unit']} in creating for 30 minutes (container absent)" in stuck_detail
    assert checks["orphan_leases"].ok is False
    assert "claude lease held by ts_one: task is CANCELLING and its launch is unsettled" in (
        checks["orphan_leases"].detail
    )
    assert json.loads(path.read_text())["phase"] == "creating"


def test_execution_diagnostics_ignore_running_and_recent_launches(setup):
    backend, client = setup
    launch(backend)
    age_record(backend, seconds=7200)
    (backend.directory / "taskspindle-worker-ts_bad.json").write_text("[")

    facts = backend.execution_diagnostics()

    assert facts["admission_open"] is True
    assert facts["unsettled"] == [{"unit": "taskspindle-worker-ts_bad", "task_id": None,
                                   "phase": "unreadable", "age_s": None, "container": "unknown"}]
    # The fixture's lease table lacks production columns: reported as unreadable, not empty.
    assert facts["leases"] is None and facts["tasks"] is None

    # A started launch whose container vanished without exit evidence is unsettled too.
    (backend.directory / "taskspindle-worker-ts_bad.json").unlink()
    client.containers.values.clear()
    [vanished] = backend.execution_diagnostics()["unsettled"]
    assert (vanished["phase"], vanished["container"]) == ("started", "absent")

    # A failed create younger than the threshold is still within ordinary retry territory.
    (backend.directory / "taskspindle-worker-ts_one.json").unlink()
    client.containers.create_failure = "before"
    with pytest.raises(UnitError):
        launch(backend)
    assert backend.execution_diagnostics()["unsettled"] == []

def test_fence_and_maintenance_changes_are_logged_without_their_tokens(setup):
    backend, _ = setup
    token = "a" * 64
    backend.set_admission(False)
    backend.set_admission(True)
    backend.maintenance_acquire(token)
    backend.maintenance_release(token, True)
    text = (backend.paths.state_dir / "logs" / "taskspindle.jsonl").read_text()
    records = [json.loads(line) for line in text.splitlines()]
    assert [(r["event"], r["admission_open"]) for r in records] == [
        ("admission_set", False), ("admission_set", True),
        ("maintenance_acquired", False), ("maintenance_released", True),
    ]
    assert {r["component"] for r in records} == {"controller"}
    assert token not in text
