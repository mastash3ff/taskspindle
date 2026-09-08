"""Readable task labels are bounded, redacted, additive, and entirely read-only."""

import json

import pytest

from taskspindle.models import TaskState
from taskspindle.service import task_view
from taskspindle.store import Store
from taskspindle.web.app import _task_summary
from tests.test_store import make_task
from tests.test_web import _client, _paths


def test_summary_uses_first_meaningful_line_and_stays_bounded():
    assert (
        _task_summary("\n\n```\n# Fix the sorting bug\nFull instructions stay private")
        == "Fix the sorting bug"
    )
    assert _task_summary("\n ---\n \n") == "Untitled task"
    summary = _task_summary("A" * 1000 + "\nSECOND_LINE_PRIVATE")
    assert len(summary) == 160 and summary.endswith("…")
    assert "SECOND_LINE_PRIVATE" not in summary
    assert _task_summary("\n" * 9000 + "outside scan bound") == "Untitled task"


@pytest.mark.parametrize(
    "line",
    [
        "Fix API_KEY=PRIVATE_VALUE before retrying",
        "Fix ANTHROPIC_API_KEY='PRIVATE_VALUE' before retrying",
        'Fix "apiKey": "PRIVATE_VALUE" before retrying',
        "Fix client_secret: PRIVATE_VALUE before retrying",
        "Fix AWS_ACCESS_KEY_ID=PRIVATE_VALUE before retrying",
        'Fix password="PRIVATE_VALUE WITH SPACES" before retrying',
        "Fix refresh_token='PRIVATE_VALUE WITH SPACES' before retrying",
        "Fix Bearer PRIVATE_VALUE before retrying",
        "Fix Authorization: Bearer PRIVATE_VALUE before retrying",
        "Fix token=Bearer PRIVATE_VALUE before retrying",
        "Fix Basic PRIVATE_VALUE before retrying",
    ],
)
def test_summary_redacts_common_credentials(line):
    summary = _task_summary(line)
    assert "PRIVATE_VALUE" not in summary
    assert "[redacted]" in summary
    assert summary.startswith("Fix ")


def test_private_key_block_and_unterminated_values_never_become_summary():
    prompt = ("\n-----BEGIN OPENSSH PRIVATE KEY-----\nPRIVATE_KEY_MATERIAL"
              "\n-----END OPENSSH PRIVATE KEY-----\nFix login")
    assert _task_summary(prompt) == "Fix login"
    assert "PRIVATE_KEY_MATERIAL" not in _task_summary(
        prompt.replace("-----END OPENSSH PRIVATE KEY-----", "")
    )
    assert "PRIVATE_VALUE" not in _task_summary('Fix password="PRIVATE_VALUE and more')
    assert "PRIVATE_VALUE" not in _task_summary("Fix password='PRIVATE_VALUE and more")


def test_task_display_is_additive_consistent_and_repository_metadata_is_scoped(tmp_path):
    paths = _paths(tmp_path)
    database = paths.state_dir / "taskspindle.sqlite3"
    with Store.open(database) as store:
        record = make_task(store)
        store._conn.execute(
            "UPDATE repositories SET common_dir=?, root_commit=?, display_path=? WHERE id='repo1'",
            ("/PRIVATE_GIT_DIR/.git", "PRIVATE_ROOT_COMMIT", "/projects/Readable repo"),
        )
        record = store.update_task(
            record.id, None, prompt="\n# Fix dropdown spacing\nPRIVATE_PROMPT_BODY", state=TaskState.QUEUED
        )
        expected = task_view(record).model_dump(mode="json")
    before = database.read_bytes()
    client = _client(paths)
    listing = client.get("/api/tasks").json()
    overview = client.get("/api/overview").json()
    detail = client.get(f"/api/tasks/{record.id}").json()
    projections = [listing["tasks"][0], overview["active_tasks"][0], detail["task"]]
    for task in projections:
        assert task["summary"] == "Fix dropdown spacing"
        assert task["repository"] == {"id": "repo1", "path": "/projects/Readable repo"}
        assert {key: value for key, value in task.items() if key not in {"summary", "repository"}} == expected
        assert "PRIVATE" not in json.dumps(task)
    assert "PRIVATE_GIT_DIR" not in json.dumps(listing)
    assert "PRIVATE_GIT_DIR" not in json.dumps(overview)
    # Existing detail metadata is deliberately unchanged; new display projections are narrower.
    assert detail["repository"]["common_dir"] == "/PRIVATE_GIT_DIR/.git"
    assert database.read_bytes() == before


def test_missing_database_stays_missing_and_no_repository_fallback(tmp_path):
    paths = _paths(tmp_path)
    client = _client(paths)
    assert client.get("/api/tasks").json() == {"tasks": []}
    assert client.get("/api/overview").json()["active_tasks"] == []
    assert client.get("/api/tasks/ts_000000000001").status_code == 404
    assert not paths.state_dir.exists()
    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        record = make_task(store)
        store._conn.execute("UPDATE repositories SET display_path=NULL WHERE id='repo1'")
    task = client.get("/api/tasks").json()["tasks"][0]
    assert task["id"] == record.id
    assert task["repository"] == {"id": "repo1", "path": None}
    assert "/repo/.git" not in json.dumps(task)
