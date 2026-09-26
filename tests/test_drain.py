"""Queued work starts when a slot frees, without waiting for the next tool call."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from taskspindle import controller as controller_module
from taskspindle import drain, policy, runner
from taskspindle.config import ConfigError, Paths, capacity_limits, dispatch_config
from taskspindle.models import AuthMode, CleanupState, Mode, TaskRecord, TaskState
from taskspindle.store import Store, now


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml", state_dir=tmp_path / "state",
        data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime",
    )


def _queued(store: Store, task_id: str, provider: str = "claude") -> TaskRecord:
    if store.get_repository("repo1") is None:
        store.insert_repository("repo1", "/repo/.git", "root", "/repo")
    stamp = now()
    return store.insert_task(TaskRecord(
        id=task_id, state=TaskState.QUEUED, cleanup_state=CleanupState.RETAINED,
        repository_id="repo1", provider=provider, auth_mode=AuthMode.OAUTH, mode=Mode.CONSULT,
        prompt="q", created_at=stamp, updated_at=stamp,
    ))


def test_startable_needs_a_queued_task_and_a_free_slot(paths: Paths) -> None:
    database = paths.state_dir / "taskspindle.sqlite3"
    assert drain.startable(database, {}) is False  # nothing has ever run

    with Store.open(database) as store:
        assert drain.startable(database, {}) is False  # an idle pool
        running = _queued(store, "ts_000000000001")
        store.acquire_lease("claude", running.id, "unit", None, "boot")
        assert drain.startable(database, {}) is False  # its only task already holds the slot
        _queued(store, "ts_000000000002")
        assert drain.startable(database, {}) is False  # queued, but the one slot is taken
        assert drain.startable(database, {"concurrency": {"claude": 2}}) is True

        # The stored policy is what sets the limit, under the file's ceiling and pool total.
        document = policy.DispatchPolicy(providers={"claude": policy.ProviderPolicy(max_concurrent=3)})
        policy.save(store, document, updated_by="test", if_revision=None)
        assert drain.startable(database, {}) is True
        assert drain.startable(database, {"capacity": {"per_provider_max": 1}}) is False
        assert drain.startable(database, {"capacity": {"total_max": 1}}) is False
        store.release_lease("claude", running.id)
        assert drain.startable(database, {"capacity": {"total_max": 1}}) is True


def test_drain_once_is_quiet_when_idle_or_switched_off(paths: Paths) -> None:
    env = {"HOME": str(paths.state_dir), "PATH": "/usr/bin"}
    assert drain.drain_once(paths=paths, parent_env=env) == drain.DrainResult()

    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        _queued(store, "ts_000000000001")
    paths.config_file.write_text("[dispatch]\ndrain = false\n", encoding="utf-8")
    assert drain.drain_once(paths=paths, parent_env=env) == drain.DrainResult()

    paths.config_file.write_text("[dispatch]\ndrain = 3\n", encoding="utf-8")
    assert drain.drain_once(paths=paths, parent_env=env).error == "ConfigError"


def test_drain_once_never_raises_and_logs_the_failure(paths: Paths, monkeypatch) -> None:
    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        _queued(store, "ts_000000000001")

    def broken(**_: object) -> None:
        raise RuntimeError("secret /path in the message")

    monkeypatch.setattr("taskspindle.server.build_orchestrator", broken)
    result = drain.drain_once(paths=paths, parent_env={"HOME": str(paths.state_dir)})
    assert (result.ran, result.error) == (False, "RuntimeError")
    text = (paths.state_dir / "dispatch-errors.log").read_text()
    assert re.match(r"--- \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z drain drain_failed: RuntimeError\n", text)
    log = (paths.state_dir / "logs" / "taskspindle.jsonl").read_text()
    records = [json.loads(line) for line in log.splitlines()]
    failure = next(record for record in records if record["event"] == "drain_failed")
    assert (failure["component"], failure["exc_type"], failure["level"]) == ("drain", "RuntimeError", "error")
    assert "secret" not in json.dumps(failure)


def test_a_persistent_drain_failure_is_logged_once_and_then_counted(paths: Paths, monkeypatch) -> None:
    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        _queued(store, "ts_000000000001")

    def broken(**_: object) -> None:
        raise RuntimeError("the same failure")

    monkeypatch.setattr("taskspindle.server.build_orchestrator", broken)
    for _ in range(6):
        drain.drain_once(paths=paths, parent_env={"HOME": str(paths.state_dir)})
    text = (paths.state_dir / "dispatch-errors.log").read_text()
    assert text.count("drain drain_failed: RuntimeError") == 1
    dedup = json.loads((paths.state_dir / "logs" / "dedup.json").read_text())
    assert dedup["drain"]["suppressed"] == 5


def test_drain_once_reconciles_then_dispatches_with_the_configured_backend(paths: Paths, monkeypatch) -> None:
    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        _queued(store, "ts_000000000001")
    calls: list[str] = []

    class Fake:
        def reconcile(self) -> None:
            calls.append("reconcile")

        def dispatch_queued(self) -> list[str]:
            calls.append("dispatch")
            return ["ts_000000000001"]

    class Closing:
        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr("taskspindle.server.build_orchestrator", lambda **_: (Fake(), Closing()))
    result = drain.drain_once(paths=paths, parent_env={"HOME": str(paths.state_dir)})
    assert result == drain.DrainResult(ran=True, started=["ts_000000000001"])
    assert calls == ["reconcile", "dispatch", "close"]


def test_the_controller_loop_backs_off_while_nothing_starts_and_stops_on_shutdown(monkeypatch) -> None:
    waits: list[float] = []
    results = iter([
        drain.DrainResult(), drain.DrainResult(ran=True), drain.DrainResult(ran=True),
        drain.DrainResult(ran=True, started=["ts_1"]), drain.DrainResult(error="RuntimeError"),
    ])

    class Stopped:
        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            return len(waits) > 5

    monkeypatch.setattr("taskspindle.drain.drain_once", lambda **_: next(results))
    fake = type("C", (), {"settings": {"dispatch": {"interval_s": 3}}, "paths": None})()
    controller_module._drain_loop(fake, Stopped())
    # idle -> 3; ran and started nothing -> 6, 12; started -> back to 3; error -> 6
    assert waits == [3.0, 3.0, 6.0, 12.0, 3.0, 6.0]

    off = type("C", (), {"settings": {"dispatch": {"drain": False}}, "paths": None})()
    stopped = threading.Event()
    controller_module._drain_loop(off, stopped)  # returns at once rather than waiting


def test_a_finished_worker_drains_except_inside_a_worker_container(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr("taskspindle.drain.drain_once", lambda **_: seen.append("drained"))
    monkeypatch.delenv("TASKSPINDLE_WORKER_CONTAINER", raising=False)
    runner._drain_after_turn()
    monkeypatch.setenv("TASKSPINDLE_WORKER_CONTAINER", "1")
    runner._drain_after_turn()
    assert seen == ["drained"]

    def broken(**_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.delenv("TASKSPINDLE_WORKER_CONTAINER")
    monkeypatch.setattr("taskspindle.drain.drain_once", broken)
    runner._drain_after_turn()  # a drain failure can never fail the finished turn


def test_capacity_and_dispatch_tables_are_validated() -> None:
    assert capacity_limits({}).per_provider_max == 8 and capacity_limits({}).total_max is None
    assert capacity_limits({"capacity": {"per_provider_max": 3, "total_max": 9}}).total_max == 9
    assert dispatch_config({}).drain is True and dispatch_config({}).interval_s == 2
    for table in (
        {"capacity": {"per_provider_max": 0}}, {"capacity": {"per_provider_max": 17}},
        {"capacity": {"total_max": True}}, {"capacity": {"nope": 1}}, {"capacity": []},
    ):
        with pytest.raises(ConfigError):
            capacity_limits(table)
    for table in ({"dispatch": {"interval_s": 0}}, {"dispatch": {"interval_s": 61}},
                  {"dispatch": {"nope": 1}}, {"dispatch": 1}):
        with pytest.raises(ConfigError):
            dispatch_config(table)


def test_a_real_pass_starts_a_queued_task_when_its_slot_frees(paths: Paths, monkeypatch) -> None:
    """The whole path with only systemd faked: real config, store, orchestrator and leases."""
    from tests.fakes.units import ACTIVE, FakeUnitBackend

    backend = FakeUnitBackend()
    monkeypatch.setattr("taskspindle.execution.unit_backend", lambda *_, **__: backend)
    env = {"HOME": str(paths.state_dir), "PATH": "/usr/bin"}
    from taskspindle import auth_context, providers
    from taskspindle.models import TurnKind

    claude = providers.load_profiles(
        {}, runtime_dir=paths.runtime_dir, home=paths.state_dir, state_dir=paths.state_dir,
        data_dir=paths.data_dir, parent_env=env,
    )["claude"]
    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        running, waiting = _queued(store, "ts_000000000001"), _queued(store, "ts_000000000002")
        # What ``start_task`` leaves behind for a queued task: its first turn and its seat.
        store.insert_turn(waiting.id, 1, TurnKind.INITIAL, prompt="q")
        store.set_task_auth_context(waiting.id, auth_context.fingerprint(claude, env))
        store.acquire_lease("claude", running.id, "taskspindle-worker-ts_000000000001", None, "boot")
        store.update_task(running.id, None, bump_version=False, state=TaskState.RUNNING,
                          unit_name="taskspindle-worker-ts_000000000001")
    backend.states["taskspindle-worker-ts_000000000001"] = ACTIVE

    # One slot, and it is taken: nothing to do, and nothing is even built.
    assert drain.drain_once(paths=paths, parent_env=env) == drain.DrainResult()

    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        store.release_lease("claude", running.id)
    result = drain.drain_once(paths=paths, parent_env=env)
    assert result.ran and result.error is None
    assert result.started == [waiting.id]
    assert [unit for unit, _ in backend.started] == [f"taskspindle-worker-{waiting.id}"]
    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        assert [row["task_id"] for row in store.list_leases("claude")] == [waiting.id]
        held = store.list_lease_history(since="0")[-1]
        assert held["task_id"] == waiting.id and held["limit_at_acquire"] == 1
