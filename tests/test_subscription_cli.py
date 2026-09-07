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
    assert result["scheduled_refresh_enabled"] is False
    assert not list(isolated.iterdir())


def test_plain_status_reports_scheduling_default(isolated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["subscriptions", "status"]) == 0
    assert "Scheduled refresh: disabled" in capsys.readouterr().out


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


def test_extension_token_uses_hidden_input_private_storage_and_no_database(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskspindle.subscriptions import cli as subscription_cli

    monkeypatch.setattr(subscription_cli.sys.stdin, "isatty", lambda: True)
    secret = "test-only-extension-connection-secret"
    monkeypatch.setattr(subscription_cli.getpass, "getpass", lambda prompt: secret)
    assert cli.main(["subscriptions", "setup-extension"]) == 0
    token = isolated / "data" / "taskspindle" / "subscriptions" / "extension-token"
    assert token.read_text() == secret
    assert token.stat().st_mode & 0o777 == 0o600
    assert token.parent.stat().st_mode & 0o777 == 0o700
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert not list(isolated.rglob("*.sqlite3"))
    assert not list(token.parent.glob(".extension-token-*"))


def test_extension_setup_refuses_noninteractive_input_and_symlink(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskspindle.subscriptions import cli as subscription_cli

    monkeypatch.setattr(subscription_cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["subscriptions", "setup-extension"]) == 1
    assert not list(isolated.iterdir())
    monkeypatch.setattr(subscription_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(subscription_cli.getpass, "getpass", lambda prompt: "test-secret")
    token = isolated / "data" / "taskspindle" / "subscriptions" / "extension-token"
    token.parent.mkdir(parents=True)
    unrelated = isolated / "unrelated"
    unrelated.write_text("preserve")
    token.symlink_to(unrelated)
    assert cli.main(["subscriptions", "setup-extension"]) == 1
    assert unrelated.read_text() == "preserve"
    output = capsys.readouterr()
    assert "test-secret" not in output.out + output.err


@pytest.mark.parametrize("token", ["", "secret\nnext", "secret value", "s" * 513, "secret\x00"])
def test_extension_setup_rejects_invalid_tokens_without_persisting(
    token: str, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskspindle.subscriptions import cli as subscription_cli

    monkeypatch.setattr(subscription_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(subscription_cli.getpass, "getpass", lambda prompt: token)
    assert cli.main(["subscriptions", "setup-extension"]) == 1
    assert not list(isolated.iterdir())
