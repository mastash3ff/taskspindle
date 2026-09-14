"""The Policy page ships as static source: route wiring, and no external references.

These assertions only look at what `/static/...` serves; they never touch the policy
backend, so they stay valid whether or not the policy API routes exist yet.
"""

from __future__ import annotations

import warnings
from pathlib import Path

with warnings.catch_warnings():
    # starlette 1.6's TestClient warns that its httpx transport is deprecated; the project's
    # pytest.ini turns every warning into an error, so the import itself must not raise.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from taskspindle.config import Paths
from taskspindle.providers import Profile
from taskspindle.web.app import build_app

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


def _client(tmp_path: Path) -> TestClient:
    app = build_app(_paths(tmp_path), PROFILES)
    return TestClient(app)


def test_router_recognizes_the_policy_route(tmp_path: Path) -> None:
    client = _client(tmp_path)
    router = client.get("/static/router.js")
    assert router.status_code == 200
    assert '"policy"' in router.text


def test_index_links_to_the_policy_route(tmp_path: Path) -> None:
    client = _client(tmp_path)
    page = client.get("/static/index.html")
    assert page.status_code == 200
    assert 'href="#/policy"' in page.text


def test_shell_wires_the_policy_renderer_and_hold_poll(tmp_path: Path) -> None:
    client = _client(tmp_path)
    shell = client.get("/static/shell.js")
    assert shell.status_code == 200
    assert "renderPolicy" in shell.text
    assert "holdPoll" in shell.text


def test_policy_view_is_served_as_javascript_with_no_external_references(tmp_path: Path) -> None:
    client = _client(tmp_path)
    view = client.get("/static/views/policy.js")
    assert view.status_code == 200
    assert view.headers["content-type"].startswith("text/javascript")
    lowered = view.text.lower()
    assert "cdn" not in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered


def test_style_sheet_carries_the_policy_page_classes(tmp_path: Path) -> None:
    client = _client(tmp_path)
    css = client.get("/static/style.css")
    assert css.status_code == 200
    assert ".switch" in css.text
    assert ".save-bar" in css.text


def test_api_module_exposes_put_and_post_json(tmp_path: Path) -> None:
    client = _client(tmp_path)
    api = client.get("/static/api.js")
    assert api.status_code == 200
    assert "putJSON" in api.text
    assert "postJSON" in api.text
    assert "X-TaskSpindle-CSRF" in api.text
