"""The read-only web dashboard: the routes it serves and what it refuses to do."""

from __future__ import annotations

import json
import sqlite3
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    # starlette 1.6's TestClient warns that its httpx transport is deprecated; the project's
    # pytest.ini turns every warning into an error, so the import itself must not raise.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from taskspindle import service, usage
from taskspindle.config import Paths
from taskspindle.models import (
    AuthMode,
    CheckRecord,
    Mode,
    ReviewFinding,
    ReviewOutput,
    Severity,
    StartTaskRequest,
    TaskState,
    Verdict,
)
from taskspindle.providers import Profile
from taskspindle.store import Store
from taskspindle.web.app import build_app
from taskspindle.web.db import ReadOnlyStore

NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)

PROFILES: dict[str, Profile] = {
    "claude": Profile(id="claude", auth="oauth", command=("claude",), first_class=True),
    "grok": Profile(id="grok", auth="oauth", command=("grok",), first_class=True),
}


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


def _client(paths: Paths) -> TestClient:
    app = build_app(paths, PROFILES, clock=lambda: NOW)
    return TestClient(app)


def _seed(paths: Paths) -> dict[str, Any]:
    """One implement task with a diff, a transcript, a check and usage, plus a review of it."""
    db_path = paths.state_dir / "taskspindle.sqlite3"
    store = Store.open(db_path)
    store.insert_repository("repo1", "/repo/.git", "root", "/repo")

    request = StartTaskRequest(
        provider="claude",
        mode=Mode.IMPLEMENT,
        prompt="do the thing",
        repository="/repo",
        acceptance_criteria="it is done",
        path_prefixes=["src"],
        verification_commands=["pytest -q"],
        candidate_message="Add it",
    )
    record = service.create_task(store, request, repository_id="repo1", auth_mode=AuthMode.OAUTH)
    task_id = record.id

    task_dir = paths.state_dir / "tasks" / task_id
    task_dir.mkdir(parents=True)

    turn_id = store.insert_turn(
        task_id,
        1,
        "initial",
        prompt="do the thing",
        started_at="2030-01-02T11:00:00.000000Z",
        session_id="sess-1",
    )
    transcript_path = task_dir / "turn-1.json"
    transcript_path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "revision": 1,
                "kind": "initial",
                "session_id": "sess-1",
                "stop_reason": "end_turn",
                "text": "done",
                "thoughts": [],
                "tool_calls": [{"name": "bash", "input": {"command": "pytest -q"}, "status": "ok"}],
                "permission_events": [],
                "violations": [],
                "update_count": 1,
            }
        ),
        encoding="utf-8",
    )
    store.complete_turn(
        turn_id,
        ended_at="2030-01-02T11:00:04.000000Z",
        stop_reason="end_turn",
        response="done",
        session_id="sess-1",
        attribution={"model": "claude-opus-5"},
    )
    store.insert_turn_usage(
        turn_id,
        task_id,
        "claude",
        model="claude-opus-5",
        input_tokens=100,
        output_tokens=20,
        cost_estimate_usd=0.01,
        cost_is_estimate=True,
        source="acp_prompt_response",
    )
    store.insert_check(
        task_id, 1, CheckRecord(command="pytest -q", exit_code=0, ok=True, duration_ms=1200)
    )
    store.append_event(task_id, "TASK_CREATED", {"provider": "claude"})

    diff_text = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    diff_path = task_dir / "rev-1.diff"
    diff_path.write_text(diff_text, encoding="utf-8")
    store.insert_artifact(task_id, 1, "candidate_diff", "sha256:deadbeef", len(diff_text), str(diff_path))

    log_path = task_dir / "worker.log"
    log_path.write_text("\n".join(f"line {i}" for i in range(1, 250)) + "\n", encoding="utf-8")

    store.update_task(
        task_id,
        None,
        state=TaskState.RESULT_READY,
        candidate_sha="cand1",
        candidate_revision=1,
        changed_paths=["src/x.py"],
        diff_digest="sha256:deadbeef",
        diff_size=len(diff_text),
        check_summary={"total": 1, "passed": 1, "ok": True},
        transcript_path=str(transcript_path),
        response="done",
        warnings=["ROOT_CHECK_SKIPPED"],
    )

    finding = ReviewFinding(
        id="f1", severity=Severity.HIGH, path="src/x.py", line=1, evidence="e", remedy="r"
    )
    reviewer_request = StartTaskRequest(
        provider="grok",
        mode=Mode.REVIEW,
        prompt="review it",
        review_target={"kind": "candidate", "task_id": task_id, "candidate_sha": "cand1"},
    )
    reviewer = service.create_task(store, reviewer_request, repository_id="repo1", auth_mode=AuthMode.OAUTH)
    store.insert_review(
        reviewer.id,
        task_id,
        "cand1",
        "grok",
        ReviewOutput(verdict=Verdict.CONCERN, summary="looks ok", findings=[finding], checks=["pytest -q"]),
    )
    store.update_task(reviewer.id, None, state=TaskState.COMPLETED)

    store.set_provider_status(
        "grok", "throttled", code="PROVIDER_THROTTLED", source="acp_error", reset_at="2030-01-03T00:00:00Z"
    )
    store.insert_provider_window(
        "claude",
        "5h",
        source="acp_error",
        status="allowed",
        used_percent=12.5,
        resets_at="2030-01-02T18:00:00Z",
    )

    store.close()
    return {"task_id": task_id, "reviewer_id": reviewer.id, "diff_text": diff_text}


# -- health ---------------------------------------------------------------------------


def test_health_reports_schema_and_db_state(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    client = _client(paths)

    missing = client.get("/api/health").json()
    assert missing["db_exists"] is False
    assert missing["schema_version"] is None
    assert missing["read_only"] is True

    _seed(paths)
    present = client.get("/api/health").json()
    assert present["db_exists"] is True
    assert present["schema_version"] == 2
    assert present["db_path"] == str(paths.state_dir / "taskspindle.sqlite3")


# -- tasks ----------------------------------------------------------------------------


def test_task_list_and_filters(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    seeded = _seed(paths)
    client = _client(paths)

    all_tasks = client.get("/api/tasks").json()["tasks"]
    assert {t["id"] for t in all_tasks} == {seeded["task_id"], seeded["reviewer_id"]}

    implement_only = client.get("/api/tasks", params={"provider": "claude", "mode": "implement"}).json()
    assert [t["id"] for t in implement_only["tasks"]] == [seeded["task_id"]]

    completed_only = client.get("/api/tasks", params={"state": "COMPLETED"}).json()
    assert [t["id"] for t in completed_only["tasks"]] == [seeded["reviewer_id"]]


def test_task_detail_shape_including_transcript_and_usage(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    seeded = _seed(paths)
    client = _client(paths)

    resp = client.get(f"/api/tasks/{seeded['task_id']}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["task"]["id"] == seeded["task_id"]
    assert body["task"]["state"] == "RESULT_READY"
    assert len(body["turns"]) == 1
    turn = body["turns"][0]
    assert turn["usage"]["model"] == "claude-opus-5"
    assert turn["usage"]["input_tokens"] == 100
    assert turn["transcript"]["text"] == "done"
    assert turn["transcript"]["tool_calls"][0]["name"] == "bash"
    assert len(body["checks"]) == 1
    assert body["checks"][0]["ok"] is True
    assert body["review"]["verdict"] == "CONCERN"
    assert body["review"]["findings"][0]["id"] == "f1"
    assert body["repository"]["id"] == "repo1"
    log_lines = body["worker_log"].splitlines()
    assert len(log_lines) == 200
    assert log_lines[0] == "line 50"
    assert log_lines[-1] == "line 249"

    assert client.get("/api/tasks/does-not-exist").status_code == 404
    assert client.get("/api/tasks/does-not-exist").json() == {"error": "TASK_NOT_FOUND"}


def test_transcript_path_outside_state_dir_is_refused(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    seeded = _seed(paths)
    store = Store.open(paths.state_dir / "taskspindle.sqlite3")
    store.update_task(seeded["task_id"], None, transcript_path="/etc/hostname")
    store.close()

    client = _client(paths)
    body = client.get(f"/api/tasks/{seeded['task_id']}").json()
    assert body["turns"][0]["transcript"] is None


def test_diff_endpoint_and_404_for_a_task_without_one(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    seeded = _seed(paths)
    client = _client(paths)

    resp = client.get(f"/api/tasks/{seeded['task_id']}/diff")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == seeded["diff_text"]

    no_diff = client.get(f"/api/tasks/{seeded['reviewer_id']}/diff")
    assert no_diff.status_code == 404


# -- providers --------------------------------------------------------------------------


def test_providers_shape_and_throttled_availability(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    client = _client(paths)

    resp = client.get("/api/providers")
    assert resp.status_code == 200
    body = resp.json()
    by_id = {p["id"]: p for p in body["providers"]}

    assert by_id["grok"]["availability"]["state"] == "throttled"
    assert by_id["grok"]["availability"]["reset_at"] == "2030-01-03T00:00:00Z"
    assert any(w["window"] == "5h" for w in by_id["claude"]["windows"])
    assert isinstance(body["doctor"], dict)
    assert "checks" in body["doctor"]


# -- usage ----------------------------------------------------------------------------


@pytest.mark.parametrize("group_by", ["provider", "repository_id"])
def test_usage_endpoint_matches_usage_report(tmp_path: Path, group_by: str) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    client = _client(paths)

    resp = client.get("/api/usage", params={"group_by": group_by})
    assert resp.status_code == 200
    body = resp.json()

    store = Store.open(paths.state_dir / "taskspindle.sqlite3")
    expected = usage.report(store, group_by=group_by, profiles=PROFILES, now=NOW)
    store.close()

    assert body["usage"] == expected["usage"]
    assert body["outcomes"] == expected["outcomes"]


def test_usage_endpoint_rejects_a_bad_since_or_group_by(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    client = _client(paths)

    assert client.get("/api/usage", params={"since": "yesterday"}).status_code == 400
    assert client.get("/api/usage", params={"group_by": "colour"}).status_code == 400


# -- method and write guards -------------------------------------------------------------


def test_non_get_methods_are_refused(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    client = _client(paths)

    assert client.post("/api/tasks").status_code == 405
    assert client.put("/api/tasks").status_code == 405
    assert client.delete("/api/tasks").status_code == 405


def test_read_only_store_raises_on_any_write(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)

    store = ReadOnlyStore(paths.state_dir / "taskspindle.sqlite3")
    assert store.exists is True
    with pytest.raises(sqlite3.OperationalError):
        store._conn.execute("DELETE FROM tasks")  # type: ignore[union-attr]
    store.close()


# -- static page ------------------------------------------------------------------------


def test_page_and_static_assets_serve_and_stay_same_origin(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed(paths)
    client = _client(paths)

    page = client.get("/")
    assert page.status_code == 200
    assert '<script src="/static/app.js">' in page.text

    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "cdn" not in js.text.lower()

    css = client.get("/static/style.css")
    assert css.status_code == 200
