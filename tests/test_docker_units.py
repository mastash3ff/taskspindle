from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound

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


class Containers:
    def __init__(self):
        self.values = {}
        self.created = []
        self.create_failure = None
        self.start_failure = None
        self.query_failure = False
        self.entered = None
        self.proceed = None

    def get(self, identity):
        if self.query_failure:
            raise OSError("secret engine URL")
        for name, container in self.values.items():
            if identity in {name, container.id}:
                return container
        raise NotFound("not found")

    def create(self, image, command, **options):
        if self.entered:
            self.entered.set()
            assert self.proceed.wait(3)
        if self.create_failure == "before":
            raise OSError("lost create request")
        if self.create_failure == "rejected":
            raise APIError("private secret", response=SimpleNamespace(status_code=400))
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
            CREATE TABLE tasks(id TEXT, provider TEXT, provider_family TEXT);
            CREATE TABLE turns(id INTEGER, task_id TEXT);
            CREATE TABLE integration_journal(task_id TEXT, started_at TEXT, candidate_sha TEXT);
            INSERT INTO tasks VALUES ('ts_one', 'claude', 'claude');
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
                             ping=lambda: True)
    return DockerBackend(paths, settings, client=client), client


def launch(backend, unit="taskspindle-worker-ts_one"):
    module = "accept" if "-accept-" in unit else "runner"
    backend.start(unit, ["/host/python", "-m", f"taskspindle.{module}", "--task", "ts_one"],
                  working_dir=backend.paths.state_dir, env={"TOKEN": "never-public"}, properties={})


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


def test_restrictive_seccomp_applies_only_agy(setup, tmp_path):
    backend, _ = setup
    profile = tmp_path / "seccomp.json"
    profile.write_text(json.dumps({"defaultAction": "SCMP_ACT_ERRNO", "syscalls": []}))
    backend.config["seccomp_profile"] = str(profile)
    assert len(backend._options("agy")["security_opt"]) == 2
    assert len(backend._options("claude")["security_opt"]) == 1
    assert len(backend._options(None)["security_opt"]) == 1


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
