"""Subscription dashboard actions and their loopback security boundary."""

from __future__ import annotations

import copy
import sqlite3
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from taskspindle import service
from taskspindle.config import Paths
from taskspindle.models import AuthMode, Mode, StartTaskRequest
from taskspindle.providers import Profile
from taskspindle.store import Store
from taskspindle.web.app import build_app

PROFILES = {
    "claude": Profile(id="claude", auth="oauth", command=("claude",), first_class=True),
}
TRUSTED_ORIGIN = "http://127.0.0.1:8765"


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


def _row(provider: str, **updates: Any) -> dict[str, Any]:
    row = {
        "provider": provider,
        "label": {
            "chatgpt": "ChatGPT",
            "claude": "Claude",
            "google_ai": "Google AI",
            "grok": "Grok",
        }[provider],
        "connected": True,
        "account_id": "a" * 64,
        "account_label": "b***@example.com",
        "billing_channel": "provider_web",
        "plan": "Pro",
        "status": "renewing",
        "renews_at": "2030-01-12",
        "access_ends_at": None,
        "date_precision": "date",
        "timezone": "America/Chicago",
        "source_url": {
            "chatgpt": "https://chatgpt.com/",
            "claude": "https://claude.ai/settings/billing",
            "google_ai": "https://one.google.com/settings",
            "grok": "https://grok.com/",
        }[provider],
        "collector_version": "test",
        "last_attempt_at": "2030-01-02T12:00:00Z",
        "last_success_at": "2030-01-02T12:00:00Z",
        "error": None,
        "freshness": "fresh",
        "days_remaining": 10,
        "end_passed_unverified": False,
        "upcoming_end_warning": None,
        "operation": None,
    }
    row.update(updates)
    return row


def _operation(provider: str, action: str, status: str) -> dict[str, Any]:
    return {
        "id": 1,
        "provider": provider,
        "action": action,
        "status": status,
        "expected_account_id": "a" * 64,
        "created_at": "2030-01-02T12:00:00Z",
        "claimed_at": "2030-01-02T12:00:01Z" if status == "running" else None,
        "heartbeat_at": "2030-01-02T12:00:01Z" if status == "running" else None,
        "lease_expires_at": "2030-01-02T12:05:01Z" if status == "running" else None,
        "finished_at": None,
        "error": None,
    }


class StubSubscriptionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.payload = {
            "subscriptions": [
                _row("chatgpt", operation=_operation("chatgpt", "refresh", "queued")),
                _row(
                    "claude",
                    status="cancelled",
                    renews_at=None,
                    access_ends_at="2030-01-07",
                    days_remaining=5,
                    operation=_operation("claude", "connect", "running"),
                ),
                _row(
                    "google_ai",
                    connected=False,
                    account_id=None,
                    account_label=None,
                    billing_channel=None,
                    status=None,
                    plan=None,
                    renews_at=None,
                    date_precision=None,
                    timezone=None,
                    source_url=None,
                    collector_version=None,
                    last_success_at=None,
                    error={
                        "code": "SETUP_REQUIRED",
                        "message": (
                            "Install the Playwright Chrome extension and run "
                            "taskspindle subscriptions setup-extension."
                        ),
                    },
                    days_remaining=None,
                ),
                _row(
                    "grok",
                    freshness="stale",
                    error={
                        "code": "AUTH_REQUIRED",
                        "message": "Sign in is required to check this subscription.",
                    },
                ),
            ],
            "collector_running": True,
            "collector_last_seen_at": "2030-01-02T11:59:00Z",
            "scheduled_refresh_enabled": False,
        }

    def status(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)

    def request(self, provider: str, action: str) -> dict[str, Any]:
        self.calls.append((provider, action))
        return {
            "id": "job-1",
            "provider": provider,
            "action": action,
            "status": "queued",
            "expected_account_id": "a" * 64,
            "created_at": "2030-01-02T12:00:00Z",
            "claimed_at": None,
            "heartbeat_at": None,
            "lease_expires_at": None,
            "finished_at": None,
            "error": None,
        }


class FailingSubscriptionService(StubSubscriptionService):
    def __init__(self, error: Exception, *, fail_status: bool = False) -> None:
        super().__init__()
        self.error = error
        self.fail_status = fail_status

    def status(self) -> dict[str, Any]:
        if self.fail_status:
            raise self.error
        return super().status()

    def request(self, provider: str, action: str) -> dict[str, Any]:
        raise self.error


def _client(
    paths: Paths,
    subscription_service: StubSubscriptionService,
    *,
    peer: str = "127.0.0.1",
    base_url: str = TRUSTED_ORIGIN,
) -> TestClient:
    app = build_app(paths, PROFILES, subscription_service=subscription_service)
    return TestClient(app, base_url=base_url, client=(peer, 50000))


def _csrf(client: TestClient) -> str:
    response = client.get("/api/subscriptions")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def _post_headers(token: str, *, origin: str = TRUSTED_ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "X-TaskSpindle-CSRF": token}


def test_subscription_status_returns_cached_rows_and_ephemeral_csrf_without_writes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    subscriptions = StubSubscriptionService()
    client = _client(paths, subscriptions)

    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    response = client.get("/api/subscriptions")
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers
    body = response.json()
    assert [row["provider"] for row in body["subscriptions"]] == [
        "chatgpt",
        "claude",
        "google_ai",
        "grok",
    ]
    assert body["collector_running"] is True
    assert body["scheduled_refresh_enabled"] is False
    assert all("upcoming_end_warning" in row for row in body["subscriptions"])
    assert isinstance(body["csrf_token"], str) and len(body["csrf_token"]) >= 32
    assert before == after == []


def test_default_subscription_status_uses_real_read_only_rows_without_creating_database(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    app = build_app(paths, PROFILES)
    client = TestClient(
        app,
        base_url=TRUSTED_ORIGIN,
        client=("127.0.0.1", 50000),
    )

    response = client.get("/api/subscriptions")

    assert response.status_code == 200
    assert [
        (row["provider"], row["label"], row["connected"]) for row in response.json()["subscriptions"]
    ] == [
        ("chatgpt", "ChatGPT", False),
        ("claude", "Claude", False),
        ("google_ai", "Google AI", False),
        ("grok", "Grok", False),
    ]
    assert not paths.state_dir.exists()


@pytest.mark.parametrize(
    ("peer", "base_url"),
    [
        ("192.0.2.10", TRUSTED_ORIGIN),
        ("127.0.0.1", "http://taskspindle.example:8765"),
    ],
)
def test_subscription_status_rejects_untrusted_peer_or_host(tmp_path: Path, peer: str, base_url: str) -> None:
    subscriptions = StubSubscriptionService()
    response = _client(_paths(tmp_path), subscriptions, peer=peer, base_url=base_url).get(
        "/api/subscriptions"
    )

    assert response.status_code == 403
    assert response.json() == {"error": "LOOPBACK_REQUIRED"}
    assert response.headers["cache-control"] == "no-store"
    assert subscriptions.calls == []


def test_subscription_action_enqueues_and_does_not_mutate_task_database(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    subscriptions = StubSubscriptionService()
    db_path = paths.state_dir / "taskspindle.sqlite3"
    with Store.open(db_path) as store:
        store.insert_repository("repo", "/repo/.git", "root", "/repo")
        service.create_task(
            store,
            StartTaskRequest(
                provider="claude",
                mode=Mode.CONSULT,
                prompt="read only",
                repository="/repo",
            ),
            repository_id="repo",
            auth_mode=AuthMode.OAUTH,
        )
    before = db_path.read_bytes()
    client = _client(paths, subscriptions)
    token = _csrf(client)

    response = client.post(
        "/api/subscriptions/claude/refresh",
        headers=_post_headers(token),
        json={},
    )

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "job": {
            "id": "job-1",
            "provider": "claude",
            "action": "refresh",
            "status": "queued",
            "expected_account_id": "a" * 64,
            "created_at": "2030-01-02T12:00:00Z",
            "claimed_at": None,
            "heartbeat_at": None,
            "lease_expires_at": None,
            "finished_at": None,
            "error": None,
        }
    }
    assert subscriptions.calls == [("claude", "refresh")]
    assert db_path.read_bytes() == before


@pytest.mark.parametrize(
    ("case", "peer", "base_url", "origin", "token", "content", "expected_status", "expected_error"),
    [
        (
            "remote peer",
            "192.0.2.10",
            TRUSTED_ORIGIN,
            TRUSTED_ORIGIN,
            "good",
            "{}",
            403,
            "LOOPBACK_REQUIRED",
        ),
        (
            "remote host",
            "127.0.0.1",
            "http://example.test:8765",
            "http://example.test:8765",
            "good",
            "{}",
            403,
            "LOOPBACK_REQUIRED",
        ),
        (
            "cross origin",
            "127.0.0.1",
            TRUSTED_ORIGIN,
            "http://localhost:8765",
            "good",
            "{}",
            403,
            "ORIGIN_REQUIRED",
        ),
        (
            "missing origin",
            "127.0.0.1",
            TRUSTED_ORIGIN,
            None,
            "good",
            "{}",
            403,
            "ORIGIN_REQUIRED",
        ),
        (
            "bad csrf",
            "127.0.0.1",
            TRUSTED_ORIGIN,
            TRUSTED_ORIGIN,
            "wrong",
            "{}",
            403,
            "CSRF_INVALID",
        ),
        (
            "not json",
            "127.0.0.1",
            TRUSTED_ORIGIN,
            TRUSTED_ORIGIN,
            "good",
            "plain",
            415,
            "JSON_REQUIRED",
        ),
        (
            "invalid json",
            "127.0.0.1",
            TRUSTED_ORIGIN,
            TRUSTED_ORIGIN,
            "good",
            "{",
            400,
            "INVALID_JSON",
        ),
        (
            "json array",
            "127.0.0.1",
            TRUSTED_ORIGIN,
            TRUSTED_ORIGIN,
            "good",
            "[]",
            400,
            "INVALID_JSON",
        ),
    ],
)
def test_subscription_action_rejects_invalid_security_context_before_enqueue(
    tmp_path: Path,
    case: str,
    peer: str,
    base_url: str,
    origin: str | None,
    token: str,
    content: str,
    expected_status: int,
    expected_error: str,
) -> None:
    del case
    subscriptions = StubSubscriptionService()
    client = _client(_paths(tmp_path), subscriptions, peer=peer, base_url=base_url)
    if token == "good":
        token = _csrf(client) if peer == "127.0.0.1" and "127.0.0.1" in base_url else "unused"
    headers = {"X-TaskSpindle-CSRF": token}
    if origin is not None:
        headers["Origin"] = origin
    if content != "plain":
        headers["Content-Type"] = "application/json"

    response = client.post("/api/subscriptions/claude/connect", headers=headers, content=content)

    assert response.status_code == expected_status
    assert response.json() == {"error": expected_error}
    assert subscriptions.calls == []


def test_subscription_routes_reject_invalid_provider_action_and_method(tmp_path: Path) -> None:
    subscriptions = StubSubscriptionService()
    client = _client(_paths(tmp_path), subscriptions)
    token = _csrf(client)
    headers = _post_headers(token)

    invalid_provider = client.post("/api/subscriptions/openrouter/connect", headers=headers, json={})
    invalid_action = client.post("/api/subscriptions/claude/cancel", headers=headers, json={})
    invalid_method = client.put("/api/subscriptions/claude/connect", headers=headers, json={})

    assert invalid_provider.status_code == 404
    assert invalid_provider.json() == {"error": "INVALID_PROVIDER"}
    assert invalid_action.status_code == 404
    assert invalid_action.json() == {"error": "INVALID_ACTION"}
    assert invalid_method.status_code == 405
    assert subscriptions.calls == []


def test_subscription_refresh_of_disconnected_provider_returns_safe_conflict(tmp_path: Path) -> None:
    subscriptions = FailingSubscriptionService(ValueError("provider is not connected"))
    client = _client(_paths(tmp_path), subscriptions)
    token = _csrf(client)

    response = client.post(
        "/api/subscriptions/google_ai/refresh",
        headers=_post_headers(token),
        json={},
    )

    assert response.status_code == 409
    assert response.json() == {"error": "ACTION_NOT_AVAILABLE"}


@pytest.mark.parametrize("fail_status", [False, True])
def test_subscription_service_failures_do_not_expose_raw_errors(tmp_path: Path, fail_status: bool) -> None:
    secret = "/private/account/profile.sqlite3"
    subscriptions = FailingSubscriptionService(OSError(secret), fail_status=fail_status)
    client = _client(_paths(tmp_path), subscriptions)

    if fail_status:
        response = client.get("/api/subscriptions")
    else:
        token = _csrf(client)
        response = client.post(
            "/api/subscriptions/claude/connect",
            headers=_post_headers(token),
            json={},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "SUBSCRIPTION_UNAVAILABLE"}
    assert secret not in response.text


def test_subscription_action_rejects_an_oversized_body_before_enqueue(tmp_path: Path) -> None:
    subscriptions = StubSubscriptionService()
    client = _client(_paths(tmp_path), subscriptions)
    token = _csrf(client)

    response = client.post(
        "/api/subscriptions/claude/connect",
        headers={**_post_headers(token), "Content-Type": "application/json"},
        content="{" + (" " * 2048) + "}",
    )

    assert response.status_code == 413
    assert response.json() == {"error": "REQUEST_TOO_LARGE"}
    assert subscriptions.calls == []


def test_health_describes_mixed_read_and_action_capabilities(tmp_path: Path) -> None:
    response = _client(_paths(tmp_path), StubSubscriptionService()).get("/api/health")

    assert response.status_code == 200
    assert response.json()["read_only"] is False
    assert response.json()["task_database_read_only"] is True
    assert response.json()["subscription_actions_enabled"] is True


def test_subscription_page_exposes_navigation_and_local_assets(tmp_path: Path) -> None:
    client = _client(_paths(tmp_path), StubSubscriptionService())

    page = client.get("/")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert 'href="#/subscriptions"' in page.text
    assert 'data-route="subscriptions"' in page.text
    assert script.status_code == 200


def test_provider_status_for_subscription_view_is_read_only_and_uses_shared_projection(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    client = _client(paths, StubSubscriptionService())

    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    response = client.get("/api/providers")
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert response.status_code == 200
    availability = response.json()["providers"][0]["availability"]
    assert {
        "state",
        "last_success_at",
        "source",
        "scope",
        "affected_model",
        "stale",
        "next_action",
        "retry_eligible",
    } <= availability.keys()
    assert response.json()["providers"][0]["model_availability"] == []
    assert before == after == []


def test_provider_api_sanitizes_legacy_status_reason_and_source_in_entire_response(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    reason_secret = "PRIVATE legacy CLI refusal with bearer credential"
    source_secret = "PRIVATE /home/account/oauth.json"
    with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
        store.set_provider_status(
            "claude",
            "access_denied",
            code="PROVIDER_ACCESS_DENIED",
            reason=reason_secret,
            source=source_secret,
            observed_at="2030-01-02T12:00:00Z",
        )
    response = _client(paths, StubSubscriptionService()).get("/api/providers")

    assert response.status_code == 200
    assert reason_secret not in response.text
    assert source_secret not in response.text
    status = response.json()["status"][0]
    assert status["provider"] == "claude"
    assert status["state"] == "access_denied"
    assert status["code"] == "PROVIDER_ACCESS_DENIED"
    assert status["reason"] == "The provider denied account access."
    assert status["source"] == "legacy"


def test_provider_api_requests_model_scoped_shared_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str | None]] = []
    projection = {
        "state": "model_unavailable",
        "status_key": "agy",
        "code": "MODEL_UNAVAILABLE",
        "window": None,
        "reset_at": None,
        "reason": "The selected model is unavailable.",
        "observed_at": "2030-01-02T12:00:00Z",
        "suggested_alternative": None,
        "last_success_at": "2030-01-02T11:00:00Z",
        "source": "worker_error",
        "scope": "model",
        "affected_model": "gemini-test",
        "stale": False,
        "next_action": "choose_model",
        "retry_eligible": False,
    }

    def fake_availability(
        store: Any, profile: Profile, *, now: Any, model: str | None = None
    ) -> dict[str, Any]:
        del store, now
        seen.append((profile.id, model))
        return projection

    monkeypatch.setattr("taskspindle.web.app.provider_availability", fake_availability)
    model_projection = [
        {
            **projection,
            "affected_model": "gemini-other",
            "observed_at": "2030-01-02T10:00:00Z",
        }
    ]
    monkeypatch.setattr(
        "taskspindle.web.app.model_availability",
        lambda store, profile, *, now: model_projection,
    )
    profile = Profile(
        id="agy", auth="oauth", command=("agy",), first_class=True, model="gemini-test"
    )
    app = build_app(
        _paths(tmp_path), {"agy": profile}, subscription_service=StubSubscriptionService()
    )
    response = TestClient(app).get("/api/providers")

    assert response.status_code == 200
    assert response.json()["providers"][0]["availability"] == projection
    assert response.json()["providers"][0]["model_availability"] == model_projection
    assert seen == [("agy", "gemini-test")]


def test_provider_api_reads_a_pre_model_status_database_without_migrating_it(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    db_path = paths.state_dir / "taskspindle.sqlite3"
    with Store.open(db_path):
        pass
    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TABLE provider_model_status")
    before = db_path.read_bytes()
    profile = Profile(
        id="agy", auth="oauth", command=("agy",), first_class=True, model="gemini-test"
    )
    client = TestClient(
        build_app(paths, {"agy": profile}, subscription_service=StubSubscriptionService())
    )

    response = client.get("/api/providers")

    assert response.status_code == 200
    assert response.json()["providers"][0]["availability"]["state"] == "unknown"
    assert db_path.read_bytes() == before


def test_subscription_script_separates_worker_access_and_never_posts_while_rendering(
    tmp_path: Path,
) -> None:
    client = _client(_paths(tmp_path), StubSubscriptionService())
    script = client.get("/static/views/subscriptions.js").text

    assert 'getJSON("/api/subscriptions"' in script
    assert 'getJSON("/api/providers"' in script
    assert 'button.addEventListener("click"' in script
    assert "postJSON(" in script
    assert "access.reason" not in script
    assert "command_name" not in script
    assert "gateway_host" not in script
    for text in [
        "Subscription",
        "Worker access",
        "Coordinator only",
        "ChatGPT is used by the Codex coordinator.",
        "Product association only",
        "Scheduled browser checks: Off (default)",
        "Browser subscription and worker CLI accounts are not assumed to match",
        "Access is scheduled to end within one day",
        "Access is scheduled to end within seven days",
        "Verification needed — the recorded access end has passed",
        "Model · ",
        "model-specific observation",
        "Use Connect or Refresh now to check billing.",
    ]:
        assert text in script
