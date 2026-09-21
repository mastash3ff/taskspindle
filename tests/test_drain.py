"""Queued work starts when a slot frees, without waiting for the next tool call."""

from __future__ import annotations

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
    assert "--- drain: RuntimeError" in (paths.state_dir / "dispatch-errors.log").read_text()


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
