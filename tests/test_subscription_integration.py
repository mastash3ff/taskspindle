"""Exercise the actual dashboard, queue, collector and observation store together."""

from __future__ import annotations

import json
import shutil
import subprocess
import warnings
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from taskspindle.config import Paths
from taskspindle.store import Store
from taskspindle.subscriptions.models import PROVIDERS, validate_result
from taskspindle.subscriptions.service import SubscriptionService
from taskspindle.web.app import build_app


def test_connect_refresh_and_failed_verification_preserve_task_database(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "config.toml", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    task_db = paths.state_dir / "taskspindle.sqlite3"
    with Store.open(task_db) as store:
        store.insert_repository("repo", "/repo/.git", "root", "/repo")
    task_bytes = task_db.read_bytes()
    now = [datetime(2030, 1, 2, 12, tzinfo=UTC)]
    calls = []
    replies = iter([
        {
            "ok": True,
            "observation": {
                "provider": "chatgpt", "account_id": "a" * 64,
                "account_label": "a***@e***.test", "billing_channel": "provider_web",
                "plan": "ChatGPT Plus", "status": "cancelled", "renews_at": None,
                "access_ends_at": "2030-01-05", "date_precision": "date",
                "timezone": "America/Chicago", "source_url": "https://chatgpt.com/",
                "collector_version": "1",
            },
        },
        {"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "secret-cookie-value"}},
    ])

    def runner(provider: str, action: str, expected: str | None) -> dict:
        calls.append((provider, action, expected))
        return next(replies)

    service = SubscriptionService(paths, clock=lambda: now[0], runner=runner)
    app = build_app(paths, {}, subscription_service=service)
    origin = "http://127.0.0.1:8766"
    client = TestClient(app, base_url=origin, client=("127.0.0.1", 52000))
    initial = client.get("/api/subscriptions").json()
    assert [row["label"] for row in initial["subscriptions"]] == ["ChatGPT", "Claude", "Google AI", "Grok"]
    assert initial["scheduled_refresh_enabled"] is False
    assert not service.database.exists()
    headers = {"Origin": origin, "X-TaskSpindle-CSRF": initial["csrf_token"]}

    first = client.post("/api/subscriptions/chatgpt/connect", headers=headers, json={})
    duplicate = client.post("/api/subscriptions/chatgpt/connect", headers=headers, json={})
    assert first.status_code == duplicate.status_code == 202
    assert first.json()["job"]["id"] == duplicate.json()["job"]["id"]
    service.run_once()
    row = client.get("/api/subscriptions").json()["subscriptions"][0]
    assert row["connected"] and row["plan"] == "ChatGPT Plus"
    assert row["status"] == "cancelled" and row["days_remaining"] == 3
    assert row["upcoming_end_warning"] == "within_7_days"
    assert row["operation"] is None and row["error"] is None
    success_at = row["last_success_at"]

    now[0] += timedelta(days=4)
    assert client.post("/api/subscriptions/chatgpt/refresh", headers=headers, json={}).status_code == 202
    service.run_once()
    failed = client.get("/api/subscriptions")
    row = failed.json()["subscriptions"][0]
    assert row["status"] == "cancelled" and row["access_ends_at"] == "2030-01-05"
    assert row["last_success_at"] == success_at and row["freshness"] == "stale"
    assert row["end_passed_unverified"] is True
    assert row["upcoming_end_warning"] is None
    assert row["error"]["code"] == "AUTH_REQUIRED"
    assert "secret-cookie-value" not in failed.text
    assert calls == [("chatgpt", "connect", None), ("chatgpt", "refresh", "a" * 64)]
    assert task_db.read_bytes() == task_bytes
    assert client.post("/api/tasks", json={}).status_code == 405


def test_packaged_javascript_observations_satisfy_python_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the packaged browser contract check")
    extractor = resources.files("taskspindle").joinpath("_subscription_browser", "extractors.mjs")
    script = """
      const {normalize, URLS} = await import(process.argv[1]);
      const base = {account_id: 'a'.repeat(64), account_label: 'a***@e***.test',
        billing_channel: 'provider_web'};
      const rows = [
        normalize('chatgpt', {...base, plan:'ChatGPT Plus',
          subscription:{active_until:'2030-01-09T12:30:00Z', will_renew:false}}, null, 'UTC'),
        normalize('claude', {...base, plan:'Claude Pro',
          billing:{status:'renewing',renews_at:'2030-01-09'}}, null, 'America/Chicago'),
        normalize('google_ai', {...base, plan:'None',
          billing:{status:'none'}}, null, 'UTC'),
        normalize('grok', {...base, plan:'SuperGrok',
          billing:{status:'expired'}}, null, 'UTC'),
      ];
      process.stdout.write(JSON.stringify({rows, urls: URLS}));
    """
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script, Path(str(extractor)).as_uri()],
        capture_output=True, text=True, timeout=15, check=True,
    )
    payload = json.loads(completed.stdout)
    # Connect's Python launcher and the authenticated JavaScript collector must
    # agree exactly: ChatGPT's settings tab identifier is case-sensitive.
    assert payload["urls"] == {provider: details["billing_url"] for provider, details in PROVIDERS.items()}
    rows = payload["rows"]
    assert len(rows) == 4
    for result in rows:
        assert result["ok"] is True
        assert validate_result(result) == result
