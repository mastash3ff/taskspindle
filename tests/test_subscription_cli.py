"""Subscription commands preserve read-only inspection and explicit service activation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from taskspindle import cli
from taskspindle.config import Paths
from taskspindle.subscriptions.cli import service_unit


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, child in (("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                        ("XDG_CONFIG_HOME", "config")):
        monkeypatch.setenv(name, str(tmp_path / child))
    monkeypatch.delenv("TASKSPINDLE_CONFIG", raising=False)
    return tmp_path


def test_empty_status_is_read_only_and_contains_native_chatgpt(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["subscriptions", "status", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {row["provider"] for row in result["subscriptions"]} == {
        "chatgpt", "claude", "google_ai", "grok",
    }
    assert result["collector_running"] is False
    assert not list(isolated.iterdir())


def test_connect_only_queues_and_refresh_all_skips_unconnected(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["subscriptions", "connect", "chatgpt"]) == 0
    job = json.loads(capsys.readouterr().out)
    assert job["provider"] == "chatgpt"
    assert cli.main(["subscriptions", "refresh"]) == 0
    assert json.loads(capsys.readouterr().out) == {"jobs": []}
    assert not (isolated / "state" / "taskspindle" / "taskspindle.sqlite3").exists()


def test_service_unit_uses_current_state_and_never_installs(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["subscriptions", "service-unit"]) == 0
    text = capsys.readouterr().out
    assert "-m taskspindle.cli subscriptions watch" in text
    assert f"XDG_STATE_HOME={isolated / 'state'}" in text
    assert "Restart=on-failure" in text
    assert "UMask=0077" in text
    assert not list(isolated.iterdir())


def test_one_shot_collector_reports_failure_and_preserves_safe_error(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskspindle.subscriptions import service

    monkeypatch.setattr(service, "run_browser", lambda *args, **kwargs: {
        "ok": False, "error": {"code": "AUTH_REQUIRED", "message": "private-cookie-value"},
    })
    assert cli.main(["subscriptions", "connect", "chatgpt"]) == 0
    capsys.readouterr()
    assert cli.main(["subscriptions", "watch", "--once"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"]["code"] == "AUTH_REQUIRED"
    assert "private-cookie-value" not in output


def test_service_unit_quotes_paths_and_rejects_line_injection(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "with space%" / "config.toml", tmp_path / "state" / "taskspindle",
                  tmp_path / "data" / "taskspindle", tmp_path / "runtime")
    result = service_unit(paths, env={})
    assert 'with space%%/config.toml"' in result
    with pytest.raises(ValueError, match="line breaks"):
        service_unit(paths, env={"TZ": "UTC\nExecStart=/bin/false"})
