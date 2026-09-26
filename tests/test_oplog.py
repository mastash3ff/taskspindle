"""The operational log: record shape, rotation, dedup, sinks, and that it never raises."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskspindle import oplog
from taskspindle.rpc import RemoteError, request
from taskspindle.service import TaskSpindleError
from taskspindle.store import Store

REPO_SRC = Path(__file__).resolve().parent.parent / "src"
STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


def records(state_dir: Path) -> list[dict]:
    path = oplog.log_path(state_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def raise_here(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:
        return caught


# -- records ----------------------------------------------------------------------------------


def test_a_record_has_the_fixed_fields_and_only_the_optional_ones_given(tmp_path: Path) -> None:
    oplog.configure(process="server")
    oplog.emit(tmp_path, "dispatch", "worker_dispatched", task_id="ts_1", provider="claude",
               unit=None, revision=2)

    [record] = records(tmp_path)
    assert STAMP.fullmatch(record["ts"])
    assert list(record)[:4] == ["ts", "level", "component", "event"]
    assert (record["level"], record["component"], record["event"]) == ("info", "dispatch",
                                                                      "worker_dispatched")
    assert (record["process"], record["pid"]) == ("server", os.getpid())
    assert (record["task_id"], record["provider"], record["revision"]) == ("ts_1", "claude", 2)
    assert "unit" not in record and "traceback" not in record
    assert oplog.log_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_messages_and_fields_are_bounded(tmp_path: Path) -> None:
    oplog.emit(tmp_path, "server", "tool_refused", message="m" * 5000, tool="t" * 5000,
               payload={"nested": "x" * 5000})
    [record] = records(tmp_path)
    assert len(record["message"]) == oplog.MESSAGE_MAX
    assert len(record["tool"]) == oplog.FIELD_MAX
    assert isinstance(record["payload"], str) and len(record["payload"]) == oplog.FIELD_MAX


def test_the_level_threshold_drops_quieter_records(tmp_path: Path, monkeypatch) -> None:
    oplog.emit(tmp_path, "controller", "operation", level="debug")
    assert records(tmp_path) == []
    monkeypatch.setenv("TASKSPINDLE_LOG_LEVEL", "debug")
    oplog.emit(tmp_path, "controller", "operation", level="debug")
    monkeypatch.setenv("TASKSPINDLE_LOG_LEVEL", "warning")
    oplog.emit(tmp_path, "controller", "operation", level="info")
    oplog.emit(tmp_path, "controller", "operation", level="error")
    assert [record["level"] for record in records(tmp_path)] == ["debug", "error"]


def test_nothing_in_this_module_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    exc = raise_here(RuntimeError("boom"))

    oplog.emit(blocker, "server", "anything", odd=object())
    assert oplog.exception(blocker, "server", "failed", exc, traceback_file="server.log",
                           dedup_scope="scope") in (True, False)
    oplog.clear(blocker, "server", "scope")
    assert oplog.dedup(blocker, "server", "scope", "key") is True
    assert oplog.rotate(blocker, apply=True) == []
    oplog.emit(None, "server", "no state dir")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "dedup.json").write_text("{not json")
    assert oplog.exception(tmp_path, "drain", "drain_failed", exc, dedup_scope="drain") is True


# -- sinks ------------------------------------------------------------------------------------


def test_stderr_is_silent_unless_the_process_asks_for_it(tmp_path: Path, capsys) -> None:
    oplog.emit(tmp_path, "server", "quiet")
    assert capsys.readouterr().err == ""

    oplog.configure(process="controller", stderr=True)
    oplog.emit(tmp_path, "controller", "loud", level="warning")
    oplog.emit(None, "controller", "only_stderr")
    err = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [(r["event"], r["process"]) for r in err] == [("loud", "controller"),
                                                          ("only_stderr", "controller")]
    assert [r["event"] for r in records(tmp_path)] == ["quiet", "loud"]


def test_the_stdio_server_stays_file_only(tmp_path: Path, monkeypatch, capsys) -> None:
    from taskspindle import server

    oplog.configure(stderr=True)  # whatever came before, the server turns it off

    class Stub:
        def run(self, **_: object) -> None:
            oplog.emit(tmp_path, "server", "tool_failed", level="error")

    closed: list[bool] = []
    monkeypatch.setattr(server, "build_orchestrator",
                        lambda: (object(), SimpleNamespace(close=lambda: closed.append(True))))
    monkeypatch.setattr(server, "build_server", lambda _orchestrator: Stub())
    server.main()

    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")
    assert [r["process"] for r in records(tmp_path)] == ["server"]
    assert closed == [True]


def test_the_controller_serve_process_mirrors_records_to_stderr(tmp_path: Path, monkeypatch,
                                                                capsys) -> None:
    from taskspindle import controller as controller_module
    from taskspindle import docker_units

    resolved = SimpleNamespace(config_file=tmp_path / "config.toml", state_dir=tmp_path)
    monkeypatch.delenv("TASKSPINDLE_CONFIG", raising=False)
    monkeypatch.setattr(controller_module, "paths", lambda: resolved)
    monkeypatch.setattr(controller_module, "load_config",
                        lambda _path: {"execution": {"jobs_socket": "/private/jobs.sock"}})
    monkeypatch.setattr(docker_units, "DockerBackend", lambda *_: object())

    def serve(controller, jobs, diagnostics) -> None:
        oplog.emit(controller.paths.state_dir, "controller", "controller_started")

    monkeypatch.setattr(controller_module, "serve", serve)
    assert controller_module.main(["serve", "--diagnostics-socket", "/private/diag.sock"]) == 0

    [line] = capsys.readouterr().err.splitlines()
    assert json.loads(line)["event"] == "controller_started"
    assert json.loads(line)["process"] == "controller"
    assert [r["event"] for r in records(tmp_path)] == ["controller_started"]


def test_each_controller_operation_is_logged_with_its_outcome(tmp_path: Path) -> None:
    from taskspindle.controller import Controller, ControlServer
    from taskspindle.units import UnitState

    backend = SimpleNamespace(
        status=lambda: {"jobs": []},
        show=lambda unit: UnitState("loaded", "active", "running", "success"),
        set_admission=lambda value: None,
    )
    controller = Controller(backend, SimpleNamespace(state_dir=tmp_path), {})
    path = tmp_path / "control.sock"
    with ControlServer(path, controller) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            request(path, "status")
            request(path, "set_admission", {"open": False})
            with pytest.raises(RemoteError):
                request(path, "run", {"TOKEN": "private-secret"})

            def fail() -> None:
                raise RuntimeError("private-secret")

            backend.status = fail
            with pytest.raises(RemoteError):
                request(path, "status")
        finally:
            server.shutdown()
            thread.join()

    logged = records(tmp_path)
    operations = [r for r in logged if r["event"] == "operation"]
    # A successful poll is a debug record; everything else is at info or above.
    assert [(r["operation"], r["ok"], r.get("code")) for r in operations] == [
        ("set_admission", True, None),
        ("run", False, "CONTROL_FORBIDDEN"),
        ("status", False, "CONTROL_FAILED"),
    ]
    assert all(isinstance(r["duration_ms"], int) and r["socket"] == "jobs" for r in operations)
    [crash] = [r for r in logged if r["event"] == "operation_crashed"]
    assert crash["exc_type"] == "RuntimeError" and "fail" in crash["traceback"]
    assert "private-secret" not in oplog.log_path(tmp_path).read_text()


# -- exceptions and traceback files -----------------------------------------------------------


def test_a_traceback_file_gets_a_timestamped_block_and_the_record_does_not(tmp_path: Path) -> None:
    exc = raise_here(RuntimeError("detail for the operator"))
    assert oplog.exception(tmp_path, "server", "tool_failed", exc, traceback_file="server.log",
                           tool="list_tasks")

    text = (tmp_path / "server.log").read_text()
    assert re.match(r"--- \S+Z server tool_failed: RuntimeError\n", text)
    assert "RuntimeError: detail for the operator" in text
    [record] = records(tmp_path)
    assert (record["exc_type"], record["tool"], record["level"]) == ("RuntimeError", "list_tasks",
                                                                    "error")
    assert "detail for the operator" not in json.dumps(record)
    assert "traceback" not in record


def test_frames_carry_the_stack_but_never_the_exception_text(tmp_path: Path) -> None:
    exc = raise_here(ValueError("sk-ant-secret-token"))
    oplog.exception(tmp_path, "controller", "operation_crashed", exc, frames=True)
    [record] = records(tmp_path)
    assert "raise_here" in record["traceback"]
    assert record["traceback"].rstrip().endswith("ValueError")
    assert "sk-ant" not in json.dumps(record)


def test_an_error_taskspindle_shaped_keeps_its_code_and_message(tmp_path: Path) -> None:
    exc = raise_here(TaskSpindleError("UNIT_START_UNCERTAIN", "Docker create outcome is unsettled"))
    oplog.exception(tmp_path, "dispatch", "dispatch_failed", exc)
    [record] = records(tmp_path)
    assert (record["code"], record["message"]) == ("UNIT_START_UNCERTAIN",
                                                   "Docker create outcome is unsettled")


# -- dedup ------------------------------------------------------------------------------------


def _same_failure() -> BaseException:
    return raise_here(RuntimeError("same"))


def test_identical_failures_are_logged_once_then_counted_until_they_change(tmp_path: Path) -> None:
    for _ in range(4):
        oplog.exception(tmp_path, "drain", "drain_failed", _same_failure(), dedup_scope="drain",
                        traceback_file="dispatch-errors.log")
    assert (tmp_path / "dispatch-errors.log").read_text().count("drain_failed: RuntimeError") == 1

    different = raise_here(KeyError("other"))
    oplog.exception(tmp_path, "drain", "drain_failed", different, dedup_scope="drain")

    events = [(r["event"], r.get("exc_type"), r.get("suppressed"), r.get("reason"))
              for r in records(tmp_path)]
    assert events == [
        ("drain_failed", "RuntimeError", None, None),
        ("repeats_suppressed", None, 3, "changed"),
        ("drain_failed", "KeyError", None, None),
    ]


def test_a_failure_that_outlives_the_window_is_logged_again_with_its_count(
    tmp_path: Path, monkeypatch,
) -> None:
    clock = [1_000_000.0]
    monkeypatch.setattr(oplog.time, "time", lambda: clock[0])
    for _ in range(3):
        oplog.exception(tmp_path, "dispatch", "dispatch_failed", _same_failure(),
                        dedup_scope="dispatch")
    clock[0] += oplog.DEDUP_WINDOW_S + 1
    oplog.exception(tmp_path, "dispatch", "dispatch_failed", _same_failure(),
                    dedup_scope="dispatch")

    events = [(r["event"], r.get("suppressed"), r.get("reason")) for r in records(tmp_path)]
    assert events == [
        ("dispatch_failed", None, None),
        ("repeats_suppressed", 2, "recurred"),
        ("dispatch_failed", None, None),
    ]


def test_clearing_reports_the_count_and_is_free_when_nothing_is_suppressed(tmp_path: Path) -> None:
    oplog.clear(tmp_path, "dispatch", "dispatch")
    assert not oplog.log_dir(tmp_path).exists()

    oplog.exception(tmp_path, "dispatch", "dispatch_failed", _same_failure(), dedup_scope="dispatch")
    oplog.exception(tmp_path, "dispatch", "dispatch_failed", _same_failure(), dedup_scope="dispatch")
    oplog.clear(tmp_path, "dispatch", "dispatch")
    oplog.clear(tmp_path, "dispatch", "dispatch")

    cleared = [r for r in records(tmp_path) if r["event"] == "failure_cleared"]
    assert [(r["scope"], r["suppressed"], r["repeated_event"]) for r in cleared] == [
        ("dispatch", 1, "dispatch_failed"),
    ]
    oplog.exception(tmp_path, "dispatch", "dispatch_failed", _same_failure(), dedup_scope="dispatch")
    assert [r["event"] for r in records(tmp_path)][-1] == "dispatch_failed"


def test_scopes_are_independent(tmp_path: Path) -> None:
    oplog.exception(tmp_path, "server", "tool_failed", _same_failure(), dedup_scope="tool:a")
    oplog.exception(tmp_path, "server", "tool_failed", _same_failure(), dedup_scope="tool:b")
    assert len(records(tmp_path)) == 2


# -- rotation ---------------------------------------------------------------------------------


def test_the_log_rotates_at_its_cap_and_keeps_three_generations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(oplog, "MAX_BYTES", 2048)
    for number in range(400):
        oplog.emit(tmp_path, "worker", "worker_started", task_id=f"ts_{number:04d}")

    path = oplog.log_path(tmp_path)
    generations = sorted(p.name for p in oplog.log_dir(tmp_path).glob("taskspindle.jsonl*"))
    assert generations == ["taskspindle.jsonl", "taskspindle.jsonl.1", "taskspindle.jsonl.2",
                           "taskspindle.jsonl.3"]
    for candidate in [path, *(path.with_name(f"{path.name}.{n}") for n in (1, 2, 3))]:
        assert candidate.stat().st_size <= 2048
    newest = [json.loads(line) for line in path.read_text().splitlines()]
    assert newest[-1]["task_id"] == "ts_0399"


def test_several_processes_can_append_and_rotate_without_tearing_lines(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from taskspindle import oplog\n"
        "oplog.MAX_BYTES = 16384\n"
        "for n in range(150):\n"
        "    oplog.emit(Path(sys.argv[1]), 'worker', 'worker_started', task_id=f'{sys.argv[2]}-{n}',"
        " padding='x' * 60)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_SRC)}
    children = [
        subprocess.Popen([sys.executable, "-c", script, str(tmp_path), f"p{index}"], env=env)
        for index in range(4)
    ]
    assert [child.wait(timeout=120) for child in children] == [0, 0, 0, 0]

    seen: list[str] = []
    for path in oplog.log_dir(tmp_path).glob("taskspindle.jsonl*"):
        assert path.stat().st_size <= 16384
        for line in path.read_text().splitlines():
            seen.append(json.loads(line)["task_id"])  # a torn line would not parse
    assert len(seen) == len(set(seen))
    newest = [json.loads(line) for line in oplog.log_path(tmp_path).read_text().splitlines()]
    assert {record["task_id"].split("-")[0] for record in newest} <= {"p0", "p1", "p2", "p3"}


def test_rotate_reports_then_rotates_oversized_logs_and_prunes_stray_generations(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(oplog, "MAX_BYTES", 100)
    (tmp_path / "server.log").write_text("s" * 150)
    (tmp_path / "server.log.3").write_text("o" * 40)
    (tmp_path / "server.log.7").write_text("p" * 30)
    (tmp_path / "dispatch-errors.log").write_text("small")

    planned = oplog.rotate(tmp_path, apply=False)
    assert {(Path(a["path"]).name, a["action"], a["bytes"]) for a in planned} == {
        ("server.log", "rotate", 40), ("server.log.7", "prune", 30),
    }
    assert (tmp_path / "server.log").stat().st_size == 150

    applied = oplog.rotate(tmp_path, apply=True)
    assert applied == planned
    assert not (tmp_path / "server.log").exists()
    assert (tmp_path / "server.log.1").read_text() == "s" * 150
    assert not (tmp_path / "server.log.7").exists()
    assert (tmp_path / "dispatch-errors.log").read_text() == "small"


# -- leases -----------------------------------------------------------------------------------


def test_lease_records_are_written_only_for_committed_transactions(tmp_path: Path) -> None:
    from taskspindle.models import AuthMode, CleanupState, Mode, TaskRecord, TaskState
    from taskspindle.store import now

    with Store.open(tmp_path / "taskspindle.sqlite3") as store:
        stamp = now()
        for task_id in ("ts_000000000001", "ts_000000000002"):
            store.insert_task(TaskRecord(
                id=task_id, state=TaskState.QUEUED, cleanup_state=CleanupState.RETAINED,
                provider="claude", auth_mode=AuthMode.OAUTH, mode=Mode.CONSULT, prompt="secret prompt",
                created_at=stamp, updated_at=stamp,
            ))
        assert store.acquire_lease("claude", "ts_000000000001", "unit-1", None, "boot", limit=2)
        with pytest.raises(RuntimeError), store.transaction():
            store.acquire_lease("claude", "ts_000000000002", "unit-2", None, "boot", limit=2)
            raise RuntimeError("roll it back")
        assert store.release_lease("claude", "ts_000000000001")
        assert not store.release_lease("claude", "ts_000000000001")

    logged = [(r["event"], r["task_id"], r["component"]) for r in records(tmp_path)]
    assert logged == [
        ("lease_acquired", "ts_000000000001", "lease"),
        ("lease_released", "ts_000000000001", "lease"),
    ]
    assert "secret prompt" not in oplog.log_path(tmp_path).read_text()
