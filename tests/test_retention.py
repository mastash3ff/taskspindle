"""``taskspindle gc``: finished tasks' scratch goes, evidence and active tasks stay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskspindle import cli, oplog, retention
from taskspindle.models import AuthMode, CleanupState, Mode, TaskRecord, TaskState
from taskspindle.store import Store, now

EVIDENCE = ("turn-1.json", "rev-1.diff", "worker.log")


def _task(store: Store, task_id: str, state: TaskState) -> None:
    stamp = now()
    store.insert_task(TaskRecord(
        id=task_id, state=state, cleanup_state=CleanupState.RETAINED, provider="claude",
        auth_mode=AuthMode.OAUTH, mode=Mode.CONSULT, prompt="q", created_at=stamp, updated_at=stamp,
    ))


def _task_dir(state_dir: Path, task_id: str, scratch_bytes: int) -> Path:
    task_dir = state_dir / "tasks" / task_id
    (task_dir / "tmp" / "nested").mkdir(parents=True)
    (task_dir / "tmp" / "nested" / "blob").write_bytes(b"x" * scratch_bytes)
    for name in EVIDENCE:
        (task_dir / name).write_text("evidence")
    return task_dir


@pytest.fixture
def state(tmp_path: Path) -> tuple[Path, Store]:
    state_dir = tmp_path / "state" / "taskspindle"
    store = Store.open(state_dir / "taskspindle.sqlite3")
    for task_id, task_state in (
        ("ts_done", TaskState.COMPLETED), ("ts_failed", TaskState.FAILED),
        ("ts_accepted", TaskState.ACCEPTED), ("ts_running", TaskState.RUNNING),
        ("ts_ready", TaskState.RESULT_READY), ("ts_leased", TaskState.CANCELLED),
    ):
        _task(store, task_id, task_state)
        _task_dir(state_dir, task_id, 1000)
    _task_dir(state_dir, "ts_unknown", 1000)
    store.acquire_lease("claude", "ts_leased", "unit", None, "boot")
    yield state_dir, store
    store.close()


def _names(items: list[dict]) -> list[str]:
    return sorted(item["task_id"] for item in items)


def test_a_dry_run_reports_and_touches_nothing(state) -> None:
    state_dir, store = state
    report = retention.collect(state_dir, store, apply=False)

    assert report["applied"] is False
    assert _names(report["tmp"]) == ["ts_accepted", "ts_done", "ts_failed"]
    assert {item["task_id"]: item["reason"] for item in report["refused"]} == {
        "ts_leased": "task holds a slot lease",
        "ts_ready": "task is RESULT_READY",
        "ts_running": "task is RUNNING",
        "ts_unknown": "unknown task",
    }
    assert report["reclaimed_bytes"] == 3000
    assert all((state_dir / "tasks" / name / "tmp").exists() for name in (
        "ts_done", "ts_failed", "ts_accepted", "ts_running", "ts_ready", "ts_leased", "ts_unknown",
    ))
    assert "gc_applied" not in oplog.log_path(state_dir).read_text()  # only the fixture's lease


def test_apply_removes_only_terminal_scratch_and_keeps_the_evidence(state) -> None:
    state_dir, store = state
    report = retention.collect(state_dir, store, apply=True)

    assert _names(report["tmp"]) == ["ts_accepted", "ts_done", "ts_failed"]
    assert report["reclaimed_bytes"] == 3000
    for task_id in ("ts_done", "ts_failed", "ts_accepted"):
        task_dir = state_dir / "tasks" / task_id
        assert sorted(path.name for path in task_dir.iterdir()) == sorted(EVIDENCE)
    for task_id in ("ts_running", "ts_ready", "ts_leased", "ts_unknown"):
        assert (state_dir / "tasks" / task_id / "tmp" / "nested" / "blob").exists()
    [record] = [r for r in json.loads("[" + ",".join(
        oplog.log_path(state_dir).read_text().splitlines()) + "]") if r["event"] == "gc_applied"]
    assert (record["removed"], record["refused"], record["reclaimed_bytes"]) == (3, 4, 3000)

    again = retention.collect(state_dir, store, apply=True)
    assert again["tmp"] == [] and again["reclaimed_bytes"] == 0


def test_a_task_that_becomes_active_between_the_scan_and_the_removal_is_left_alone(
    state, monkeypatch,
) -> None:
    state_dir, store = state
    real = retention._verdict
    calls: dict[str, int] = {}

    def racing(store_: Store, task_id: str, leased: set[str]) -> str | None:
        calls[task_id] = calls.get(task_id, 0) + 1
        if task_id == "ts_done" and calls[task_id] >= 3:
            return "task is RESUMING"  # the continuation started after the directory moved
        return real(store_, task_id, leased)

    monkeypatch.setattr(retention, "_verdict", racing)
    report = retention.collect(state_dir, store, apply=True)

    assert "ts_done" not in _names(report["tmp"])
    assert {"task_id": "ts_done", "reason": "task became active"}.items() <= next(
        item for item in report["refused"] if item["task_id"] == "ts_done").items()
    assert (state_dir / "tasks" / "ts_done" / "tmp" / "nested" / "blob").exists()
    assert not list((state_dir / "tasks" / "ts_done").glob("tmp.gc-*"))


def test_a_symlinked_tmp_is_unlinked_not_followed(state, tmp_path: Path) -> None:
    state_dir, store = state
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep me")
    scratch = state_dir / "tasks" / "ts_done" / "tmp"
    retention.remove_tree(scratch)
    scratch.symlink_to(outside)

    retention.collect(state_dir, store, apply=True)

    assert not scratch.exists() and not scratch.is_symlink()
    assert (outside / "precious").read_text() == "keep me"


def test_gc_rotates_oversized_logs(state, monkeypatch) -> None:
    state_dir, store = state
    monkeypatch.setattr(oplog, "MAX_BYTES", 100)
    (state_dir / "server.log").write_text("s" * 500)
    (state_dir / "server.log.3").write_text("o" * 70)

    planned = retention.collect(state_dir, store, apply=False)
    server_log = [item for item in planned["logs"] if item["path"] == str(state_dir / "server.log")]
    assert server_log == [{"action": "rotate", "path": str(state_dir / "server.log"), "bytes": 70}]
    assert planned["reclaimed_bytes"] == 3070

    retention.collect(state_dir, store, apply=True)
    assert (state_dir / "server.log.1").read_text() == "s" * 500
    assert not (state_dir / "server.log").exists()


def test_the_cli_is_a_dry_run_by_default(state, monkeypatch, capsys, tmp_path: Path) -> None:
    state_dir, _ = state
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("TASKSPINDLE_CONFIG", raising=False)

    assert cli.main(["gc"]) == 0
    out = capsys.readouterr().out
    assert f"would remove {state_dir / 'tasks' / 'ts_done' / 'tmp'} (1000 B)" in out
    assert "kept" in out and "task is RUNNING" in out
    assert "reclaimable: 2.9 KiB (dry run; pass --apply)" in out
    assert (state_dir / "tasks" / "ts_done" / "tmp").exists()

    assert cli.main(["gc", "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is True and report["reclaimed_bytes"] == 3000
    assert not (state_dir / "tasks" / "ts_done" / "tmp").exists()


def test_the_cli_refuses_without_a_database(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("TASKSPINDLE_CONFIG", raising=False)
    assert cli.main(["gc", "--apply"]) == 1
    assert "no database" in capsys.readouterr().err
    assert not (tmp_path / "empty").exists()
