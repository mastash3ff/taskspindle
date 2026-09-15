"""Dispatch policy dashboard routes: the only mutating surface of ``taskspindle web``."""

from __future__ import annotations

import json
import sqlite3
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    # starlette 1.6's TestClient warns that its httpx transport is deprecated; the project's
    # pytest.ini turns every warning into an error, so the import itself must not raise.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from taskspindle import policy
from taskspindle import store as store_module
from taskspindle.config import Paths
from taskspindle.providers import Profile
from taskspindle.store import Store
from taskspindle.web.app import build_app
from taskspindle.web.policy_store import PolicyStore

PROFILES: dict[str, Profile] = {
    "claude": Profile(id="claude", auth="oauth", command=("claude",), first_class=True),
    "grok": Profile(id="grok", auth="oauth", command=("grok",), first_class=True),
}
TRUSTED_ORIGIN = "http://127.0.0.1:8765"
DEFAULTS_JSON = policy.defaults(PROFILES).model_dump(mode="json")
VALID_BODY = json.dumps({"if_revision": 0, "policy": DEFAULTS_JSON})
HUGE_BODY = json.dumps({"if_revision": 0, "policy": {"note": "x" * 70_000}})


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


def _db_path(paths: Paths) -> Path:
    return paths.state_dir / "taskspindle.sqlite3"


def _client(paths: Paths, *, peer: str = "127.0.0.1", base_url: str = TRUSTED_ORIGIN) -> TestClient:
    app = build_app(paths, PROFILES)
    return TestClient(app, base_url=base_url, client=(peer, 50000))


def _csrf(client: TestClient) -> str:
    response = client.get("/api/policy")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def _headers(token: str, *, origin: str = TRUSTED_ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "X-TaskSpindle-CSRF": token}


def _seed_schema_11(paths: Paths) -> None:
    with Store.open(_db_path(paths)):
        pass


# -- GET ------------------------------------------------------------------------------------


def test_policy_get_shape_before_anything_is_saved(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths)

    response = client.get("/api/policy")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()

    assert body["revision"] == 0
    assert body["source"] == "defaults"
    assert body["document_error"] is None
    assert body["writable"] is True
    assert isinstance(body["csrf_token"], str) and body["csrf_token"]
    assert body["defaults"] == DEFAULTS_JSON
    assert body["policy"] == DEFAULTS_JSON
    assert {p["id"] for p in body["profiles"]} == {"claude", "grok"}
    for entry in body["profiles"]:
        assert entry.keys() >= {"id", "family", "first_class", "auth", "modes"}
    assert body["status"]["share_window"] == "week"
    file_managed = body["file_managed"]
    assert file_managed["config_file"] == str(paths.config_file)
    assert file_managed["concurrency"] == {"claude": 1, "grok": 1}
    assert file_managed["native_overage"] == {"claude": "observe_only", "grok": "observe_only"}
    assert file_managed["provider_recovery"] == {"claude": "manual", "grok": "manual"}


def test_get_from_non_loopback_peer_is_refused_without_a_csrf_token(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths, peer="203.0.113.5")

    response = client.get("/api/policy")

    assert response.status_code == 403
    assert response.json() == {"error": "LOOPBACK_REQUIRED"}
    assert "csrf_token" not in response.json()


# -- PUT --------------------------------------------------------------------------------------


def test_put_saves_a_new_revision_recorded_in_history(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths)
    token = _csrf(client)

    response = client.put(
        "/api/policy", headers=_headers(token), json={"if_revision": 0, "policy": DEFAULTS_JSON}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 1
    assert body["updated_by"] == "web"
    assert body["source"] == "store"
    assert body["policy"] == DEFAULTS_JSON

    history = client.get("/api/policy/history").json()["history"]
    assert len(history) == 1
    assert history[0]["revision"] == 1
    assert history[0]["updated_by"] == "web"
    assert history[0]["reason"] is None


def test_put_conflict_on_a_stale_revision_reports_the_current_one(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths)
    token = _csrf(client)

    response = client.put(
        "/api/policy", headers=_headers(token), json={"if_revision": 5, "policy": DEFAULTS_JSON}
    )

    assert response.status_code == 409
    assert response.json() == {"error": "POLICY_REVISION_CONFLICT", "current_revision": 0}
    assert client.get("/api/policy/history").json()["history"] == []


def test_put_rejects_a_document_whose_enabled_shares_sum_above_100(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths)
    token = _csrf(client)
    doc = json.loads(json.dumps(DEFAULTS_JSON))
    doc["providers"]["claude"]["target_share"] = 100
    doc["providers"]["grok"]["target_share"] = 50

    response = client.put("/api/policy", headers=_headers(token), json={"if_revision": 0, "policy": doc})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "POLICY_INVALID"
    errors = body["details"]["errors"]
    assert errors[0]["loc"] == ["providers"]
    assert errors[0]["code"] == "share_sum"
    assert client.get("/api/policy/history").json()["history"] == []


def test_put_rejects_an_effort_on_a_model_that_does_not_accept_one(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths)
    token = _csrf(client)
    doc = json.loads(json.dumps(DEFAULTS_JSON))
    doc["roles"]["mechanic"]["selections"]["claude"] = {"model": "haiku", "effort": "low"}

    response = client.put("/api/policy", headers=_headers(token), json={"if_revision": 0, "policy": doc})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "POLICY_INVALID"
    errors = body["details"]["errors"]
    assert errors[0]["loc"] == ["roles", "mechanic", "selections", "claude", "effort"]
    assert errors[0]["code"] == "effort_unsupported"


def test_put_against_a_schema_10_database_is_unavailable_and_never_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    db_path = _db_path(paths)
    with monkeypatch.context() as patched:
        patched.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:10])
        with Store.open(db_path):
            pass

    client = _client(paths)
    token = _csrf(client)
    response = client.put(
        "/api/policy", headers=_headers(token), json={"if_revision": 0, "policy": DEFAULTS_JSON}
    )

    assert response.status_code == 503
    assert response.json() == {"error": "POLICY_UNAVAILABLE"}
    with sqlite3.connect(db_path) as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert version == 10


@pytest.mark.parametrize(
    (
        "case", "peer", "base_url", "origin", "token", "content_type", "content",
        "expected_status", "expected_error",
    ),
    [
        ("remote peer", "203.0.113.5", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "good",
         "application/json", VALID_BODY, 403, "LOOPBACK_REQUIRED"),
        ("non-loopback host", "127.0.0.1", "http://example.test:8765", "http://example.test:8765", "good",
         "application/json", VALID_BODY, 403, "LOOPBACK_REQUIRED"),
        ("cross origin", "127.0.0.1", TRUSTED_ORIGIN, "http://localhost:8765", "good",
         "application/json", VALID_BODY, 403, "ORIGIN_REQUIRED"),
        ("missing origin", "127.0.0.1", TRUSTED_ORIGIN, None, "good",
         "application/json", VALID_BODY, 403, "ORIGIN_REQUIRED"),
        ("bad csrf", "127.0.0.1", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "wrong",
         "application/json", VALID_BODY, 403, "CSRF_INVALID"),
        ("wrong content type", "127.0.0.1", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "good",
         "text/plain", "plain body", 415, "JSON_REQUIRED"),
        ("invalid json", "127.0.0.1", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "good",
         "application/json", "{", 400, "INVALID_JSON"),
        ("json array", "127.0.0.1", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "good",
         "application/json", "[]", 400, "INVALID_JSON"),
        ("body over 64 KiB", "127.0.0.1", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "good",
         "application/json", HUGE_BODY, 413, "REQUEST_TOO_LARGE"),
    ],
)
def test_put_rejects_invalid_security_context_before_any_write(
    tmp_path: Path,
    case: str,
    peer: str,
    base_url: str,
    origin: str | None,
    token: str,
    content_type: str,
    content: str,
    expected_status: int,
    expected_error: str,
) -> None:
    del case
    paths = _paths(tmp_path)
    db_path = _db_path(paths)
    _seed_schema_11(paths)
    before = db_path.read_bytes()

    client = _client(paths, peer=peer, base_url=base_url)
    if token == "good":
        token = _csrf(client) if peer == "127.0.0.1" and "127.0.0.1" in base_url else "unused"
    headers = {"X-TaskSpindle-CSRF": token, "Content-Type": content_type}
    if origin is not None:
        headers["Origin"] = origin

    response = client.put("/api/policy", headers=headers, content=content)

    assert response.status_code == expected_status
    assert response.json() == {"error": expected_error}
    assert db_path.read_bytes() == before


# -- reset ----------------------------------------------------------------------------------


def test_reset_saves_the_defaults_as_a_new_revision(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths)
    token = _csrf(client)
    doc = json.loads(json.dumps(DEFAULTS_JSON))
    doc["share_window"] = "day"
    saved = client.put(
        "/api/policy", headers=_headers(token), json={"if_revision": 0, "policy": doc}
    ).json()
    assert saved["revision"] == 1
    assert saved["policy"]["share_window"] == "day"

    response = client.post("/api/policy/reset", headers=_headers(token), json={"if_revision": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["revision"] == 2
    assert body["policy"] == DEFAULTS_JSON

    history = client.get("/api/policy/history").json()["history"]
    assert [row["revision"] for row in history] == [2, 1]
    assert next(row for row in history if row["revision"] == 2)["reason"] == "reset"


def test_reset_also_guards_origin_csrf_and_writability(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths)
    token = _csrf(client)

    bad_csrf = client.post(
        "/api/policy/reset",
        headers={"Origin": TRUSTED_ORIGIN, "X-TaskSpindle-CSRF": "wrong"},
        json={"if_revision": 0},
    )
    assert bad_csrf.status_code == 403
    assert bad_csrf.json() == {"error": "CSRF_INVALID"}

    conflict = client.post(
        "/api/policy/reset", headers=_headers(token), json={"if_revision": 7}
    )
    assert conflict.status_code == 409
    assert conflict.json() == {"error": "POLICY_REVISION_CONFLICT", "current_revision": 0}


# -- history ----------------------------------------------------------------------------------


def test_history_list_and_single_revision_lookup(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths)
    token = _csrf(client)
    first = json.loads(json.dumps(DEFAULTS_JSON))
    first["share_window"] = "day"
    client.put("/api/policy", headers=_headers(token), json={"if_revision": 0, "policy": first})
    client.put("/api/policy", headers=_headers(token), json={"if_revision": 1, "policy": DEFAULTS_JSON})

    history = client.get("/api/policy/history").json()["history"]
    assert [row["revision"] for row in history] == [2, 1]

    limited = client.get("/api/policy/history", params={"limit": 1}).json()["history"]
    assert [row["revision"] for row in limited] == [2]

    single = client.get("/api/policy/history/1").json()
    assert single["revision"] == 1
    assert single["policy"] == first
    assert single["updated_by"] == "web"
    assert "fingerprint" in single

    missing = client.get("/api/policy/history/999")
    assert missing.status_code == 404
    assert missing.json() == {"error": "POLICY_REVISION_NOT_FOUND"}

    not_numeric = client.get("/api/policy/history/not-a-number")
    assert not_numeric.status_code == 404
    assert not_numeric.json() == {"error": "POLICY_REVISION_NOT_FOUND"}


def test_history_routes_are_not_loopback_gated(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    client = _client(paths, peer="203.0.113.5")

    assert client.get("/api/policy/history").status_code == 200
    assert client.get("/api/policy/history/1").status_code == 404


# -- PolicyStore authorizer ---------------------------------------------------------------------


def test_policy_store_reports_unavailable_for_a_missing_or_pre_schema_11_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    db_path = _db_path(paths)

    missing = PolicyStore(db_path)
    assert missing.available is False
    missing.close()

    with monkeypatch.context() as patched:
        patched.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:10])
        with Store.open(db_path):
            pass
    with PolicyStore(db_path) as old_schema:
        assert old_schema.available is False


def test_policy_store_authorizer_blocks_writes_outside_the_two_policy_tables(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    db_path = _db_path(paths)
    _seed_schema_11(paths)

    with PolicyStore(db_path) as store:
        assert store.available is True
        assert store._conn is not None
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("DELETE FROM tasks")
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute(
                "INSERT INTO turns (task_id, revision, kind) VALUES ('x', 1, 'initial')"
            )
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("CREATE TABLE evil (id INTEGER)")
        with pytest.raises(sqlite3.DatabaseError):
            store._conn.execute("DROP TABLE dispatch_policy")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


# -- Codex AI policy (fixed adapter; no database write) ---------------------------------------

VALID_AI_BODY = json.dumps(
    {"mode": "native", "hosts": ["windows"], "expected_revisions": {"windows": "r1"}}
)
HUGE_AI_BODY = json.dumps(
    {"mode": "native", "hosts": ["windows"], "expected_revisions": {"windows": "x" * 70_000}}
)


def _ai_client(
    paths: Paths,
    factory=None,
    *,
    peer: str = "127.0.0.1",
    base_url: str = TRUSTED_ORIGIN,
) -> TestClient:
    app = build_app(paths, PROFILES, ai_policy_adapter_factory=factory)
    return TestClient(app, base_url=base_url, client=(peer, 50000))


def _ai_csrf(client: TestClient) -> str:
    response = client.get("/api/ai-policy")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def _host_row(
    host: str,
    *,
    mode: str = "ensemble",
    status: str = "configured",
    revision: str = "r1",
    error: str | None = None,
) -> dict:
    row = {
        "host": host,
        "mode": mode,
        "status": status,
        "revision": revision,
        "checks": [{"name": "ready", "ok": True, "detail": "ok"}],
    }
    if error:
        row["error"] = error
    return row


def _fake_factory(handler):
    def factory(_command):
        async def run(payload):
            return handler(payload)

        return run

    return factory


def test_ai_policy_get_missing_adapter_is_unavailable_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    spawned: list[object] = []

    async def forbidden(*_args: object, **_kwargs: object) -> object:
        spawned.append(1)
        raise AssertionError("missing adapter must not spawn")

    monkeypatch.setattr("taskspindle.web.ai_policy.asyncio.create_subprocess_exec", forbidden)
    client = _ai_client(paths)
    response = client.get("/api/ai-policy")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["applies_to"] == "new_sessions"
    assert isinstance(body["csrf_token"], str) and body["csrf_token"]
    statuses = {row["host"]: row["status"] for row in body["hosts"]}
    assert statuses == {"windows": "unavailable", "wsl": "unavailable"}
    assert all(row.get("error") == "ADAPTER_UNAVAILABLE" for row in body["hosts"])
    assert spawned == []
    assert not (paths.state_dir / "taskspindle.sqlite3").exists()


def test_ai_policy_get_from_non_loopback_peer_is_refused(tmp_path: Path) -> None:
    client = _ai_client(_paths(tmp_path), peer="203.0.113.5")
    response = client.get("/api/ai-policy")
    assert response.status_code == 403
    assert response.json() == {"error": "LOOPBACK_REQUIRED"}
    assert "csrf_token" not in response.json()


def test_ai_policy_get_returns_adapter_status(tmp_path: Path) -> None:
    def handler(payload: dict) -> dict:
        assert payload == {"action": "status", "hosts": ["windows", "wsl"]}
        return {"hosts": [_host_row("windows"), _host_row("wsl", mode="native", revision="r2")]}

    client = _ai_client(_paths(tmp_path), _fake_factory(handler))
    body = client.get("/api/ai-policy").json()
    assert body["applies_to"] == "new_sessions"
    assert body["hosts"][0]["mode"] == "ensemble"
    assert body["hosts"][1]["mode"] == "native"
    assert "results" not in body


def test_ai_policy_put_applies_mode_for_new_sessions_only(tmp_path: Path) -> None:
    calls: list[dict] = []

    def handler(payload: dict) -> dict:
        calls.append(payload)
        if payload["action"] == "status":
            return {"hosts": [_host_row("windows"), _host_row("wsl")]}
        return {
            "hosts": [
                _host_row("windows", mode=payload["mode"], revision="r2"),
                _host_row("wsl", mode=payload["mode"], revision="r2"),
            ],
            "results": [{"host": "windows", "ok": True}, {"host": "wsl", "ok": True}],
        }

    paths = _paths(tmp_path)
    paths.config_file.write_text("[ai_policy]\ncommand = [\"/usr/bin/true\"]\n", encoding="utf-8")
    before = paths.config_file.read_bytes()
    client = _ai_client(paths, _fake_factory(handler))
    token = _ai_csrf(client)
    response = client.put(
        "/api/ai-policy",
        headers=_headers(token),
        json={
            "mode": "native",
            "hosts": ["windows", "wsl"],
            "expected_revisions": {"windows": "r1", "wsl": "r1"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["applies_to"] == "new_sessions"
    assert [row["ok"] for row in body["results"]] == [True, True]
    assert all(row["mode"] == "native" for row in body["hosts"])
    assert calls[-1]["action"] == "use"
    assert "command" not in calls[-1]
    assert paths.config_file.read_bytes() == before
    assert not (paths.state_dir / "taskspindle.sqlite3").exists()


def test_ai_policy_put_stale_revision_returns_failed_result_with_fresh_status(tmp_path: Path) -> None:
    def handler(payload: dict) -> dict:
        if payload["action"] == "status":
            return {"hosts": [_host_row("windows", revision="fresh")]}
        assert payload["expected_revisions"] == {"windows": "stale"}
        return {
            "hosts": [_host_row("windows", mode="ensemble", revision="fresh")],
            "results": [{"host": "windows", "ok": False, "error": "STALE_REVISION"}],
        }

    client = _ai_client(_paths(tmp_path), _fake_factory(handler))
    token = _ai_csrf(client)
    response = client.put(
        "/api/ai-policy",
        headers=_headers(token),
        json={"mode": "native", "hosts": ["windows"], "expected_revisions": {"windows": "stale"}},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "AI_POLICY_APPLY_FAILED"
    assert body["hosts"][0]["revision"] == "fresh"
    assert body["hosts"][0]["status"] == "configured"
    assert body["results"] == [{"host": "windows", "ok": False, "error": "STALE_REVISION"}]
    assert body.get("ok") is not True


def test_ai_policy_put_partial_failure_preserves_fresh_state_and_does_not_claim_success(
    tmp_path: Path,
) -> None:
    def handler(payload: dict) -> dict:
        if payload["action"] == "status":
            return {"hosts": [_host_row("windows"), _host_row("wsl")]}
        return {
            "hosts": [
                _host_row("windows", mode="native", revision="r2"),
                _host_row("wsl", mode="ensemble", status="update_failed", revision="r1", error="WSL_FAILED"),
            ],
            "results": [
                {"host": "windows", "ok": True},
                {"host": "wsl", "ok": False, "error": "WSL_FAILED"},
            ],
        }

    client = _ai_client(_paths(tmp_path), _fake_factory(handler))
    token = _ai_csrf(client)
    response = client.put(
        "/api/ai-policy",
        headers=_headers(token),
        json={
            "mode": "native",
            "hosts": ["windows", "wsl"],
            "expected_revisions": {"windows": "r1", "wsl": "r1"},
        },
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "AI_POLICY_APPLY_FAILED"
    by_host = {row["host"]: row for row in body["hosts"]}
    assert by_host["windows"]["mode"] == "native" and by_host["windows"]["revision"] == "r2"
    assert by_host["wsl"]["status"] == "update_failed"
    assert {row["host"]: row["ok"] for row in body["results"]} == {"windows": True, "wsl": False}


def test_use_payload_without_host_coverage_is_not_success() -> None:
    from taskspindle.web.ai_policy import apply_succeeded, parse_adapter_payload

    hosts, results = parse_adapter_payload(
        {"hosts": [], "results": [{"host": "windows", "ok": True}]},
        ["windows"],
        require_results=True,
        requested_mode="native",
    )
    assert hosts[0]["host"] == "windows"
    assert hosts[0]["status"] == "unavailable"
    assert results is not None and results[0]["ok"] is False
    assert apply_succeeded(results, hosts, "native") is False


def test_use_payload_rejects_success_when_mode_or_status_does_not_match_request() -> None:
    from taskspindle.web.ai_policy import apply_succeeded, parse_adapter_payload

    hosts, results = parse_adapter_payload(
        {
            "hosts": [_host_row("windows", mode="ensemble")],
            "results": [{"host": "windows", "ok": True}],
        },
        ["windows"],
        require_results=True,
        requested_mode="native",
    )
    assert hosts[0]["mode"] == "ensemble"
    assert results is not None and results[0]["ok"] is False
    assert apply_succeeded(results, hosts, "native") is False


def test_ai_policy_put_empty_hosts_with_ok_result_is_not_success(tmp_path: Path) -> None:
    def handler(payload: dict) -> dict:
        if payload["action"] == "status":
            return {"hosts": [_host_row("windows")]}
        return {"hosts": [], "results": [{"host": "windows", "ok": True}]}

    client = _ai_client(_paths(tmp_path), _fake_factory(handler))
    token = _ai_csrf(client)
    response = client.put(
        "/api/ai-policy",
        headers=_headers(token),
        json={"mode": "native", "hosts": ["windows"], "expected_revisions": {"windows": "r1"}},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "AI_POLICY_APPLY_FAILED"
    assert body["hosts"][0]["status"] == "unavailable"
    assert body["results"][0]["ok"] is False


def test_ai_policy_put_malformed_contract_is_unavailable_not_success(tmp_path: Path) -> None:
    def handler(payload: dict) -> dict:
        if payload["action"] == "status":
            return {"hosts": [_host_row("windows")]}
        return {"hosts": "not-a-list"}

    client = _ai_client(_paths(tmp_path), _fake_factory(handler))
    token = _ai_csrf(client)
    response = client.put(
        "/api/ai-policy",
        headers=_headers(token),
        json={"mode": "native", "hosts": ["windows"], "expected_revisions": {"windows": "r1"}},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "AI_POLICY_APPLY_FAILED"
    assert body["hosts"][0]["status"] == "unavailable"
    assert body["hosts"][0]["error"] == "ADAPTER_INVALID"
    assert body["results"][0]["ok"] is False


def test_ai_policy_put_rejects_browser_argv_and_does_not_call_adapter(tmp_path: Path) -> None:
    calls: list[dict] = []

    def handler(payload: dict) -> dict:
        calls.append(payload)
        return {"hosts": [_host_row("windows")]}

    client = _ai_client(_paths(tmp_path), _fake_factory(handler))
    token = _ai_csrf(client)
    calls.clear()
    response = client.put(
        "/api/ai-policy",
        headers=_headers(token),
        json={
            "mode": "native",
            "hosts": ["windows"],
            "expected_revisions": {"windows": "r1"},
            "command": ["/bin/true"],
        },
    )
    assert response.status_code == 400
    assert response.json() == {"error": "INVALID_REQUEST"}
    assert calls == []


def test_ai_policy_relative_command_is_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.config_file.write_text("[ai_policy]\ncommand = [\"relative-adapter\"]\n", encoding="utf-8")
    spawned: list[object] = []

    async def forbidden(*_args: object, **_kwargs: object) -> object:
        spawned.append(1)
        raise AssertionError("relative command must not spawn")

    monkeypatch.setattr("taskspindle.web.ai_policy.asyncio.create_subprocess_exec", forbidden)
    client = _ai_client(paths)
    body = client.get("/api/ai-policy").json()
    assert body["hosts"][0]["status"] == "unavailable"
    assert body["hosts"][0]["error"] == "ADAPTER_CONFIG_INVALID"
    assert spawned == []


@pytest.mark.parametrize(
    (
        "case", "peer", "base_url", "origin", "token", "content_type", "content",
        "expected_status", "expected_error",
    ),
    [
        ("remote peer", "203.0.113.5", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "good",
         "application/json", VALID_AI_BODY, 403, "LOOPBACK_REQUIRED"),
        ("cross origin", "127.0.0.1", TRUSTED_ORIGIN, "http://localhost:8765", "good",
         "application/json", VALID_AI_BODY, 403, "ORIGIN_REQUIRED"),
        ("missing origin", "127.0.0.1", TRUSTED_ORIGIN, None, "good",
         "application/json", VALID_AI_BODY, 403, "ORIGIN_REQUIRED"),
        ("bad csrf", "127.0.0.1", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "wrong",
         "application/json", VALID_AI_BODY, 403, "CSRF_INVALID"),
        ("wrong content type", "127.0.0.1", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "good",
         "text/plain", "plain body", 415, "JSON_REQUIRED"),
        ("body over 64 KiB", "127.0.0.1", TRUSTED_ORIGIN, TRUSTED_ORIGIN, "good",
         "application/json", HUGE_AI_BODY, 413, "REQUEST_TOO_LARGE"),
    ],
)
def test_ai_policy_put_rejects_invalid_security_context(
    tmp_path: Path,
    case: str,
    peer: str,
    base_url: str,
    origin: str | None,
    token: str,
    content_type: str,
    content: str,
    expected_status: int,
    expected_error: str,
) -> None:
    del case
    paths = _paths(tmp_path)
    config = paths.config_file
    config.write_text("[ai_policy]\ncommand = [\"/usr/bin/true\"]\n", encoding="utf-8")
    before = config.read_bytes()
    factory = _fake_factory(lambda payload: {"hosts": [_host_row("windows")]})
    client = _ai_client(paths, factory, peer=peer, base_url=base_url)
    if token == "good":
        token = _ai_csrf(client) if peer == "127.0.0.1" and "127.0.0.1" in base_url else "unused"
    headers = {"X-TaskSpindle-CSRF": token, "Content-Type": content_type}
    if origin is not None:
        headers["Origin"] = origin
    response = client.put("/api/ai-policy", headers=headers, content=content)
    assert response.status_code == expected_status
    assert response.json() == {"error": expected_error}
    assert config.read_bytes() == before


def test_ai_policy_configured_adapter_uses_stdin_stdout_json_without_a_shell(tmp_path: Path) -> None:
    import sys

    from taskspindle.web.ai_policy import invoke_configured_adapter

    script = tmp_path / "adapter.py"
    script.write_text(
        "import json, sys\n"
        "req = json.load(sys.stdin)\n"
        "assert req['action'] == 'status'\n"
        "json.dump({'hosts': [{'host': 'windows', 'mode': 'ensemble', 'status': 'configured',"
        " 'revision': 'r1', 'checks': [{'name': 'ready', 'ok': True, 'detail': 'ok'}]}]}, sys.stdout)\n",
        encoding="utf-8",
    )
    source = Path("src/taskspindle/web/ai_policy.py").read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "create_subprocess_shell" not in source

    async def _run() -> dict:
        return await invoke_configured_adapter(
            (sys.executable, str(script)), {"action": "status", "hosts": ["windows"]}
        )

    import asyncio

    payload = asyncio.run(_run())
    assert payload["hosts"][0]["status"] == "configured"


@pytest.mark.asyncio
async def test_ai_policy_nonzero_exit_rejects_valid_success_json(tmp_path: Path) -> None:
    import sys

    from taskspindle.web.ai_policy import AdapterError, invoke_configured_adapter

    script = tmp_path / "nonzero.py"
    script.write_text(
        "import json, sys\n"
        "json.dump({'hosts': [{'host': 'windows', 'mode': 'ensemble', 'status': 'configured',"
        " 'revision': 'r1', 'checks': []}]}, sys.stdout)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    with pytest.raises(AdapterError) as caught:
        await invoke_configured_adapter((sys.executable, str(script)), {"action": "status"})
    assert caught.value.code == "ADAPTER_INVALID"


def test_ai_policy_nonzero_adapter_cannot_yield_http_success(tmp_path: Path) -> None:
    import sys

    paths = _paths(tmp_path)
    script = tmp_path / "nonzero-http.py"
    script.write_text(
        "import json, sys\n"
        "json.dump({'hosts': [{'host': 'windows', 'mode': 'ensemble', 'status': 'configured',"
        " 'revision': 'r1', 'checks': []}], 'results': [{'host': 'windows', 'ok': True}]},"
        " sys.stdout)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    command = json.dumps([sys.executable, str(script)])
    paths.config_file.write_text(
        f"[ai_policy]\ncommand = {command}\nhosts = [\"windows\"]\n", encoding="utf-8"
    )
    client = _ai_client(paths)
    body = client.get("/api/ai-policy").json()
    assert body["hosts"][0]["status"] == "unavailable"
    assert body["hosts"][0]["error"] == "ADAPTER_INVALID"
    assert body.get("ok") is not True

    response = client.put(
        "/api/ai-policy",
        headers=_headers(body["csrf_token"]),
        json={"mode": "native", "hosts": ["windows"], "expected_revisions": {"windows": "r1"}},
    )
    assert response.status_code == 409
    put_body = response.json()
    assert put_body["error"] == "AI_POLICY_APPLY_FAILED"
    assert put_body["results"][0]["ok"] is False
    assert put_body["hosts"][0]["status"] != "configured"


@pytest.mark.asyncio
async def test_ai_policy_adapter_timeout_does_not_return_success(tmp_path: Path) -> None:
    import sys

    from taskspindle.web.ai_policy import AdapterError, invoke_configured_adapter

    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    with pytest.raises(AdapterError) as caught:
        await invoke_configured_adapter((sys.executable, str(script)), {"action": "status"}, timeout=0.05)
    assert caught.value.code == "ADAPTER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_ai_policy_adapter_cancel_stops_the_process(tmp_path: Path) -> None:
    import asyncio
    import sys

    from taskspindle.web.ai_policy import invoke_configured_adapter

    script = tmp_path / "cancel.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    task = asyncio.create_task(
        invoke_configured_adapter((sys.executable, str(script)), {"action": "status"}, timeout=10)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stop_adapter_falls_back_when_killpg_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import sys

    from taskspindle.web import ai_policy as ai_policy_mod
    from taskspindle.web.ai_policy import AdapterError, invoke_configured_adapter

    script = tmp_path / "killpg.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    def missing_killpg(*_args: object, **_kwargs: object) -> None:
        raise AttributeError("killpg")

    monkeypatch.setattr(os, "killpg", missing_killpg)
    monkeypatch.setattr(ai_policy_mod.os, "killpg", missing_killpg)
    with pytest.raises(AdapterError) as caught:
        await invoke_configured_adapter((sys.executable, str(script)), {"action": "status"}, timeout=0.05)
    assert caught.value.code == "ADAPTER_UNAVAILABLE"
