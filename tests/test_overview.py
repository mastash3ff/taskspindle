"""Global, bounded and read-only dashboard overview behavior."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from taskspindle import service
from taskspindle.config import Paths
from taskspindle.models import AuthMode, CleanupState, Mode, StartTaskRequest, TaskState
from taskspindle.providers import Profile
from taskspindle.store import Store
from taskspindle.web.app import build_app

NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
PROFILES = {"claude": Profile(id="claude", auth="oauth", command=("claude",))}


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


def _client(paths: Paths) -> TestClient:
    return TestClient(build_app(paths, PROFILES, clock=lambda: NOW))


def _task(store: Store, state: TaskState, *, cleanup: CleanupState = CleanupState.RETAINED) -> str:
    request = StartTaskRequest(provider="claude", mode=Mode.CONSULT, prompt=f"private {state}")
    record = service.create_task(store, request, repository_id=None, auth_mode=AuthMode.OAUTH)
    store.update_task(record.id, None, state=state, cleanup_state=cleanup)
    return record.id


def test_overview_counts_all_tasks_but_bounds_lists_and_projects_display_repository(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        store.insert_repository("repo1", "/private/common.git", "private-root", "/work/project")
        active_ids = [_task(store, TaskState.RUNNING), _task(store, TaskState.QUEUED)]
        ready_id = _task(store, TaskState.RESULT_READY)
        interrupted_id = _task(store, TaskState.INTERRUPTED)
        ambiguous_id = _task(store, TaskState.RECOVERY_AMBIGUOUS)
        dirty_failed_id = _task(store, TaskState.FAILED, cleanup=CleanupState.FAILED)
        _task(store, TaskState.FAILED, cleanup=CleanupState.COMPLETE)
        _task(store, TaskState.COMPLETED)
        store.update_task(ready_id, None, repository_id="repo1")

    response = _client(paths).get("/api/overview", params={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["generated_at"] == "2030-01-02T12:00:00Z"
    assert body["counts"] == {"total": 8, "active": 2, "attention": 4, "awaiting_review": 1}
    assert len(body["active_tasks"]) == len(body["attention_tasks"]) == 1
    assert body["truncated"] == {"active": True, "attention": True}
    assert body["limit"] == 1

    full = _client(paths).get("/api/overview", params={"limit": 100}).json()
    assert {row["id"] for row in full["active_tasks"]} == set(active_ids)
    assert {row["id"] for row in full["attention_tasks"]} == {
        ready_id, interrupted_id, ambiguous_id, dirty_failed_id,
    }
    ready = next(row for row in full["attention_tasks"] if row["id"] == ready_id)
    assert ready["repository"] == {"id": "repo1", "path": "/work/project"}
    assert "prompt" not in ready
    assert "/private/common.git" not in response.text
    assert "private-root" not in response.text


def test_overview_missing_database_is_empty_and_does_not_create_it(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    db_path = paths.state_dir / "taskspindle.sqlite3"
    response = _client(paths).get("/api/overview")
    assert response.status_code == 200
    assert response.json() == {
        "generated_at": "2030-01-02T12:00:00Z",
        "counts": {"total": 0, "active": 0, "attention": 0, "awaiting_review": 0},
        "active_tasks": [],
        "attention_tasks": [],
        "limit": 20,
        "truncated": {"active": False, "attention": False},
    }
    assert not db_path.exists()


@pytest.mark.parametrize("value", ["wat", "0", "101", "-1"])
def test_overview_rejects_invalid_limits(tmp_path: Path, value: str) -> None:
    response = _client(_paths(tmp_path)).get("/api/overview", params={"limit": value})
    assert response.status_code == 400
    assert response.json() == {"error": "INVALID_LIMIT"}


def test_overview_is_get_only(tmp_path: Path) -> None:
    assert _client(_paths(tmp_path)).post("/api/overview").status_code == 405
