"""The ``[web]`` remote-access opt-in: an explicit remote Host allowlist, the policy remote
opt-in layered on top of it, and forwarding a live doctor check to a trusted controller.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import threading
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    # starlette 1.6's TestClient warns that its httpx transport is deprecated; the project's
    # pytest.ini turns every warning into an error, so the import itself must not raise.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from taskspindle import policy
from taskspindle.config import ConfigError, Paths
from taskspindle.providers import Profile
from taskspindle.store import Store
from taskspindle.web import app as web_app
from taskspindle.web import remote as remote_module
from taskspindle.web.app import build_app
from taskspindle.web.remote import RemoteAccessConfig, forward_doctor, load_remote_access_config
from taskspindle.web.security import parse_netloc, remote_allowed_host

PROFILES: dict[str, Profile] = {
    "claude": Profile(id="claude", auth="oauth", command=("claude",), first_class=True),
}
REMOTE_HOST = "192.168.0.100:8765"
REMOTE_ORIGIN = f"http://{REMOTE_HOST}"


def _paths(base: Path) -> Paths:
    return Paths(
        config_file=base / "config.toml",
        state_dir=base / "state",
        data_dir=base / "data",
        runtime_dir=base / "runtime",
    )


def _write_config(paths: Paths, body: str) -> None:
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(body, encoding="utf-8")


def _seed_schema_11(paths: Paths) -> None:
    with Store.open(paths.state_dir / "taskspindle.sqlite3"):
        pass


def _client(paths: Paths, *, peer: str, base_url: str) -> TestClient:
    return TestClient(build_app(paths, PROFILES), base_url=base_url, client=(peer, 50000))


def _csrf(client: TestClient) -> str:
    response = client.get("/api/policy")
    assert response.status_code == 200
    return response.json()["csrf_token"]


@pytest.fixture
def socket_dir() -> Any:
    # AF_UNIX paths are capped at ~108 bytes; pytest's own tmp_path is often too deep for that,
    # so a real bound socket needs a short directory of its own regardless of tmp_path's nesting.
    directory = tempfile.mkdtemp(dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# -- parsing helpers used by the middleware and the remote-policy guard -----------------------


def test_parse_netloc_rejects_path_query_fragment_userinfo_and_zero_port() -> None:
    assert parse_netloc("host:8765") == ("host", 8765)
    assert parse_netloc("user@host:8765") is None
    assert parse_netloc("host:8765/path") is None
    assert parse_netloc("host:8765?x=1") is None
    assert parse_netloc("host:0") is None


@pytest.mark.parametrize(
    "value",
    [
        "host\r\nX-Injected: 1:8765",
        "host\x00:8765",
        "host\t:8765",
        "ho st:8765",
        "host:8765\n",
    ],
)
def test_parse_netloc_rejects_raw_whitespace_and_control_characters(value: str) -> None:
    assert parse_netloc(value) is None


def test_remote_allowed_host_matches_by_parsed_host_and_port_not_raw_string() -> None:
    allowed = frozenset({"Example.Test:8765"})
    assert remote_allowed_host("example.test:8765", allowed) is True
    assert remote_allowed_host("example.test:9999", allowed) is False
    assert remote_allowed_host("evil.example/example.test:8765", allowed) is False
    assert remote_allowed_host(None, allowed) is False


# -- global Host allowlist ----------------------------------------------------------------------


def test_allowed_remote_host_reaches_read_routes_from_a_remote_peer(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\n')
    client = _client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)

    response = client.get("/api/health")

    assert response.status_code == 200


def test_arbitrary_remote_host_is_still_denied(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\n')
    client = _client(paths, peer="203.0.113.5", base_url="http://someone-else.example:8765")

    response = client.get("/api/health")

    assert response.status_code == 403
    assert response.json() == {"error": "LOOPBACK_REQUIRED"}


def test_default_loopback_behavior_is_unchanged_without_any_web_config(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    client = _client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)

    response = client.get("/api/health")

    assert response.status_code == 403
    assert response.json() == {"error": "LOOPBACK_REQUIRED"}


@pytest.mark.parametrize("host", ["192.168.0.100", "192.168.0.100:9999", "192.168.0.101:8765"])
def test_allowed_hosts_match_is_exact_not_a_prefix_or_alternate_port(tmp_path: Path, host: str) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\n')
    client = _client(paths, peer="203.0.113.5", base_url=f"http://{host}")

    assert client.get("/api/health").status_code == 403


# -- remote policy opt-in ------------------------------------------------------------------------


def test_remote_policy_opt_in_off_still_requires_a_loopback_peer(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    _write_config(paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\n')
    client = _client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)

    response = client.get("/api/policy")

    assert response.status_code == 403
    assert response.json() == {"error": "LOOPBACK_REQUIRED"}


def test_remote_policy_opt_in_on_allows_get_and_put_from_the_allowed_host(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    _write_config(
        paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\nallow_remote_policy = true\n'
    )
    client = _client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)

    get_response = client.get("/api/policy")
    assert get_response.status_code == 200
    token = get_response.json()["csrf_token"]

    defaults = policy.defaults(PROFILES).model_dump(mode="json")
    put_response = client.put(
        "/api/policy",
        headers={"Origin": REMOTE_ORIGIN, "X-TaskSpindle-CSRF": token},
        json={"if_revision": 0, "policy": defaults},
    )

    assert put_response.status_code == 200
    assert put_response.json()["revision"] == 1


def test_remote_policy_opt_in_on_still_enforces_origin_and_csrf(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    _write_config(
        paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\nallow_remote_policy = true\n'
    )
    client = _client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)
    token = _csrf(client)
    defaults = policy.defaults(PROFILES).model_dump(mode="json")

    wrong_origin = client.put(
        "/api/policy",
        headers={"Origin": "http://attacker.example", "X-TaskSpindle-CSRF": token},
        json={"if_revision": 0, "policy": defaults},
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"error": "ORIGIN_REQUIRED"}

    bad_csrf = client.put(
        "/api/policy",
        headers={"Origin": REMOTE_ORIGIN, "X-TaskSpindle-CSRF": "wrong"},
        json={"if_revision": 0, "policy": defaults},
    )
    assert bad_csrf.status_code == 403
    assert bad_csrf.json() == {"error": "CSRF_INVALID"}


def test_forwarded_headers_never_substitute_for_the_real_peer_or_host(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _seed_schema_11(paths)
    # allow_remote_policy is deliberately left off; only a genuine loopback peer should work.
    _write_config(paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\n')
    client = _client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)

    response = client.get(
        "/api/policy",
        headers={
            "X-Forwarded-For": "127.0.0.1",
            "Forwarded": "for=127.0.0.1",
            "X-Forwarded-Host": "127.0.0.1",
        },
    )

    assert response.status_code == 403
    assert response.json() == {"error": "LOOPBACK_REQUIRED"}


def test_health_policy_writable_matches_the_actual_guard_for_this_caller(tmp_path: Path) -> None:
    opted_in_paths = _paths(tmp_path / "opted-in")
    _seed_schema_11(opted_in_paths)
    _write_config(
        opted_in_paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\nallow_remote_policy = true\n'
    )
    opted_in = _client(opted_in_paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)
    assert opted_in.get("/api/health").json()["policy_writable"] is True

    not_opted_in_paths = _paths(tmp_path / "not-opted-in")
    _seed_schema_11(not_opted_in_paths)
    _write_config(not_opted_in_paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\n')
    not_opted_in = _client(not_opted_in_paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)
    assert not_opted_in.get("/api/health").json()["policy_writable"] is False


# -- remote AI-policy opt-in, exercised through an injected fake adapter (no real process, ----
# -- no live host writes) ------------------------------------------------------------------------


def _fake_ai_policy_adapter_factory(_command: tuple[str, ...] | None) -> Any:
    async def call(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["action"] == "status":
            return {
                "hosts": [
                    {
                        "host": host, "mode": "ensemble", "status": "configured",
                        "revision": "r1", "checks": [],
                    }
                    for host in ("windows", "wsl")
                ]
            }
        hosts = payload["hosts"]
        return {
            "hosts": [
                {
                    "host": host, "mode": payload["mode"], "status": "configured",
                    "revision": "r2", "checks": [],
                }
                for host in hosts
            ],
            "results": [{"host": host, "ok": True} for host in hosts],
        }

    return call


def _remote_ai_policy_client(paths: Paths, *, peer: str, base_url: str) -> TestClient:
    app = build_app(paths, PROFILES, ai_policy_adapter_factory=_fake_ai_policy_adapter_factory)
    return TestClient(app, base_url=base_url, client=(peer, 50000))


def test_remote_ai_policy_opt_in_off_still_requires_a_loopback_peer(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\n')
    client = _remote_ai_policy_client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)

    response = client.get("/api/ai-policy")

    assert response.status_code == 403
    assert response.json() == {"error": "LOOPBACK_REQUIRED"}


def test_remote_ai_policy_opt_in_on_allows_get_and_put_via_the_fake_adapter(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_config(
        paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\nallow_remote_policy = true\n'
    )
    client = _remote_ai_policy_client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)

    get_response = client.get("/api/ai-policy")
    assert get_response.status_code == 200
    body = get_response.json()
    assert all(row["status"] == "configured" for row in body["hosts"])
    token = body["csrf_token"]

    put_response = client.put(
        "/api/ai-policy",
        headers={"Origin": REMOTE_ORIGIN, "X-TaskSpindle-CSRF": token},
        json={
            "mode": "ensemble",
            "hosts": ["windows", "wsl"],
            "expected_revisions": {"windows": "r1", "wsl": "r1"},
        },
    )

    assert put_response.status_code == 200
    assert put_response.json()["results"] == [
        {"host": "windows", "ok": True}, {"host": "wsl", "ok": True},
    ]


def test_remote_ai_policy_opt_in_on_still_enforces_origin_and_csrf(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_config(
        paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\nallow_remote_policy = true\n'
    )
    client = _remote_ai_policy_client(paths, peer="203.0.113.5", base_url=REMOTE_ORIGIN)
    token = client.get("/api/ai-policy").json()["csrf_token"]
    body = {
        "mode": "ensemble",
        "hosts": ["windows", "wsl"],
        "expected_revisions": {"windows": "r1", "wsl": "r1"},
    }

    wrong_origin = client.put(
        "/api/ai-policy",
        headers={"Origin": "http://attacker.example", "X-TaskSpindle-CSRF": token},
        json=body,
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"error": "ORIGIN_REQUIRED"}

    bad_csrf = client.put(
        "/api/ai-policy",
        headers={"Origin": REMOTE_ORIGIN, "X-TaskSpindle-CSRF": "wrong"},
        json=body,
    )
    assert bad_csrf.status_code == 403
    assert bad_csrf.json() == {"error": "CSRF_INVALID"}


# -- malformed [web] config fails clearly at startup, never degrading a safety setting ----------


def test_absent_config_file_and_absent_web_table_both_stay_the_loopback_only_default(
    tmp_path: Path,
) -> None:
    missing_file = _paths(tmp_path)
    assert load_remote_access_config(missing_file.config_file) == RemoteAccessConfig(
        frozenset(), False, None
    )

    empty_table = _paths(tmp_path / "empty")
    _write_config(empty_table, "[web]\n")
    assert load_remote_access_config(empty_table.config_file) == RemoteAccessConfig(
        frozenset(), False, None
    )


@pytest.mark.parametrize(
    "body",
    [
        '[web]\nallowed_hosts = "192.168.0.100:8765"\n',
        '[web]\nallowed_hosts = ["192.168.0.100:8765", "bad host!"]\n',
        '[web]\nallowed_hosts = ["http://192.168.0.100:8765"]\n',
    ],
)
def test_malformed_allowed_hosts_raises_a_config_error(tmp_path: Path, body: str) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, body)

    with pytest.raises(ConfigError):
        load_remote_access_config(paths.config_file)


def test_malformed_allow_remote_policy_raises_a_config_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, '[web]\nallow_remote_policy = "true"\n')

    with pytest.raises(ConfigError):
        load_remote_access_config(paths.config_file)


@pytest.mark.parametrize(
    "body",
    [
        '[web]\ndiagnostics_socket = "relative/path.sock"\n',
        "[web]\ndiagnostics_socket = 7\n",
        '[web]\ndiagnostics_socket = "/has/a/\\u0000/nul.sock"\n',
    ],
)
def test_malformed_diagnostics_socket_raises_a_config_error(tmp_path: Path, body: str) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, body)

    with pytest.raises(ConfigError):
        load_remote_access_config(paths.config_file)


def test_non_table_web_section_raises_a_config_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, "web = 1\n")

    with pytest.raises(ConfigError):
        load_remote_access_config(paths.config_file)


def test_unparseable_toml_raises_a_config_error_rather_than_degrading_silently(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    _write_config(paths, "not valid toml [[[")

    with pytest.raises(ConfigError):
        load_remote_access_config(paths.config_file)


def test_valid_config_still_loads_and_starting_the_server_reports_a_bad_one_clearly(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    _write_config(
        paths, f'[web]\nallowed_hosts = ["{REMOTE_HOST}"]\nallow_remote_policy = true\n'
    )
    assert load_remote_access_config(paths.config_file) == RemoteAccessConfig(
        frozenset({REMOTE_HOST}), True, None
    )

    from taskspindle.web import serve

    bad_paths = _paths(tmp_path / "bad")
    _write_config(bad_paths, '[web]\ndiagnostics_socket = "relative.sock"\n')
    assert serve(bad_paths, PROFILES) == 1


# -- diagnostics forwarding ----------------------------------------------------------------------


def test_forward_doctor_sends_a_bounded_request_to_the_configured_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_request(socket_path: Any, operation: str, arguments: Any, *, timeout: float) -> Any:
        calls.append((socket_path, operation, arguments, timeout))
        return {"ok": True, "checks": []}

    monkeypatch.setattr(remote_module.rpc, "request", fake_request)
    socket_path = tmp_path / "control.sock"

    result = asyncio.run(forward_doctor(socket_path, live=True))

    assert result == {"ok": True, "checks": []}
    assert calls == [(socket_path, "doctor", {"live": True}, 90.0)]


def test_missing_control_socket_reports_unavailable_without_local_fallback(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    missing_socket = socket_dir / "control.sock"
    _write_config(paths, f'[web]\ndiagnostics_socket = "{missing_socket}"\n')

    async def forbidden_doctor(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("a configured diagnostics socket must never fall back to local checks")

    monkeypatch.setattr(web_app, "run_doctor_async", forbidden_doctor)
    client = _client(paths, peer="127.0.0.1", base_url="http://127.0.0.1:8765")

    response = client.get("/api/doctor", params={"live": "1"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "unavailable"
    assert body["error"] == "CONTROL_UNAVAILABLE"


def test_malformed_control_response_reports_unavailable_without_local_fallback(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    socket_path = socket_dir / "control.sock"
    _write_config(paths, f'[web]\ndiagnostics_socket = "{socket_path}"\n')
    # Missing "advisory" on the check entry: the shape is validated field-by-field, not just
    # "the top level is a dict".
    bad_report = {"ok": True, "checks": [{"name": "systemd", "ok": True, "detail": "fine"}]}
    server = _FakeControlServer(socket_path, [bad_report])

    async def forbidden_doctor(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("a malformed control response must never fall back to local checks")

    monkeypatch.setattr(web_app, "run_doctor_async", forbidden_doctor)
    try:
        client = _client(paths, peer="127.0.0.1", base_url="http://127.0.0.1:8765")
        response = client.get("/api/doctor")
    finally:
        server.close()

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["status"] == "unavailable"
    assert body["error"] == "CONTROL_PROTOCOL_ERROR"


class _FakeControlServer:
    """A minimal Unix-socket responder speaking the same newline-JSON protocol as ``rpc.py``."""

    def __init__(self, socket_path: Path, results: list[dict[str, Any]]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._results = results
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(socket_path))
        self._server.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        for _ in range(len(self._results)):
            self._server.settimeout(5)
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            with connection, connection.makefile("rwb") as stream:
                raw = stream.readline()
                request = json.loads(raw)
                self.requests.append(request)
                result = self._results[len(self.requests) - 1]
                stream.write((json.dumps({"ok": True, "result": result}) + "\n").encode("utf-8"))
                stream.flush()

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=5)


def test_successful_doctor_forwarding_preserves_shape_and_the_non_live_cache(
    tmp_path: Path, socket_dir: Path
) -> None:
    paths = _paths(tmp_path)
    socket_path = socket_dir / "control.sock"
    _write_config(paths, f'[web]\ndiagnostics_socket = "{socket_path}"\n')
    report = {"ok": True, "checks": [{"name": "systemd", "ok": True, "detail": "fine", "advisory": False}]}
    server = _FakeControlServer(socket_path, [report, report])
    try:
        client = _client(paths, peer="127.0.0.1", base_url="http://127.0.0.1:8765")

        first = client.get("/api/doctor")
        assert first.status_code == 200
        assert first.json() == report

        # A second non-live request within the cache window must not touch the socket again.
        cached = client.get("/api/doctor")
        assert cached.json() == report

        live = client.get("/api/doctor", params={"live": "1"})
        assert live.json() == report
    finally:
        server.close()

    assert [request["operation"] for request in server.requests] == ["doctor", "doctor"]
    assert [request["arguments"]["live"] for request in server.requests] == [False, True]
