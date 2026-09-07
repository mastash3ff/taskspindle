"""Native diagnostic checks use synthetic evidence and never launch a provider."""

import json
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from taskspindle import access_checks, agy_cli_adapter, providers


@pytest.fixture
def profiles(tmp_path):
    return providers.builtin_profiles(
        tmp_path / "runtime", home=tmp_path / "home", state_dir=tmp_path / "state",
    )


@pytest.fixture
def parent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home), "PATH": "/usr/bin:/bin", "USER": "synthetic-user",
        "ANTHROPIC_API_KEY": "PRIVATE_KEY", "OPENAI_API_KEY": "PRIVATE_KEY",
        "GEMINI_API_KEY": "PRIVATE_KEY", "PLAYWRIGHT_MCP_EXTENSION_TOKEN": "PRIVATE_TOKEN",
        "HTTP_PROXY": "PRIVATE_PROXY", "CLAUDE_CONFIG_DIR": "/wrong/config",
        "NO_BROWSER": "0", "CI": "0",
    }


def status(plan="max"):
    return {
        "loggedIn": True, "authMethod": "claude.ai", "subscriptionType": plan,
        "apiProvider": "firstParty", "email": "PRIVATE_EMAIL", "organizationId": "PRIVATE_ORG",
    }


def assert_safe(result):
    assert set(result) == {
        "state", "source", "checked_at", "account_binding", "plan", "model_count", "detail",
    }
    assert result["account_binding"] == "unverified"
    assert datetime.fromisoformat(result["checked_at"]).utcoffset().total_seconds() == 0
    assert "PRIVATE" not in json.dumps(result)


def test_claude_exact_command_uses_selected_environment_and_removes_temporary_files(
    profiles, parent, tmp_path, monkeypatch,
):
    profile = replace(profiles["claude"], id="claude-alias", base="claude", first_class=False,
                      env={"CLAUDE_CONFIG_DIR": str(tmp_path / "selected-config"), "NO_BROWSER": "0"})
    original = dict(parent)
    temporary = []

    def run(argv, **kwargs):
        assert argv == ["claude", "auth", "status"]
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["check"] is False
        assert kwargs["timeout"] == 15
        env = kwargs["env"]
        assert env["CLAUDE_CONFIG_DIR"] == profile.env["CLAUDE_CONFIG_DIR"]
        assert env["HOME"] == parent["HOME"]
        assert env["NO_BROWSER"] == "1" and env["CI"] == "1"
        assert not providers.env_violations(env)
        assert env["TMPDIR"] == kwargs["cwd"]
        temporary.append(Path(kwargs["cwd"]))
        assert temporary[-1].is_dir()
        return subprocess.CompletedProcess(argv, 0, json.dumps(status()), "PRIVATE_STDERR")

    monkeypatch.setattr(access_checks.subprocess, "run", run)
    result = access_checks.check_native_access(profile, parent)
    assert result["state"] == "cached_auth" and result["source"] == "claude_auth_status"
    assert result["plan"] == "max" and result["model_count"] is None
    assert_safe(result)
    assert all(not directory.exists() for directory in temporary)
    assert parent == original
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "selected-config").exists()


def test_claude_accepts_pro_evidence_without_turn_or_subscription_claim(profiles, parent, monkeypatch):
    monkeypatch.setattr(providers, "claude_oauth_evidence", lambda run: {"subscriptionType": "pro"})
    result = access_checks.check_native_access(profiles["claude"], parent)
    assert result["state"] == "cached_auth" and result["plan"] == "pro"
    assert "unverified" in result["detail"]
    assert_safe(result)


@pytest.mark.parametrize("failure", ["nonzero", "malformed", "logged_out", "api", "timeout", "missing"])
def test_claude_failure_is_fixed_and_redacted(profiles, parent, monkeypatch, failure):
    temporary = []

    def run(argv, **kwargs):
        temporary.append(Path(kwargs["cwd"]))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 15, output="PRIVATE_OUTPUT", stderr="PRIVATE_STDERR")
        if failure == "missing":
            raise FileNotFoundError("PRIVATE_PATH")
        payload = status()
        if failure == "logged_out":
            payload["loggedIn"] = False
        if failure == "api":
            payload["authMethod"] = "apiKey"
        return subprocess.CompletedProcess(
            argv, 1 if failure == "nonzero" else 0,
            "PRIVATE_OUTPUT" if failure == "malformed" else json.dumps(payload), "PRIVATE_STDERR",
        )

    monkeypatch.setattr(access_checks.subprocess, "run", run)
    result = access_checks.check_native_access(profiles["claude"], parent)
    assert result["state"] == "check_failed" and result["source"] == "claude_auth_status"
    assert result["plan"] is None and result["model_count"] is None
    assert_safe(result)
    assert all(not directory.exists() for directory in temporary)


def test_unsupported_profiles_never_probe(profiles, parent, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported profile attempted a native probe")

    monkeypatch.setattr(access_checks.subprocess, "run", forbidden)
    monkeypatch.setattr(providers, "build_child_env", forbidden)
    for profile in [profiles["grok"], replace(profiles["claude"], auth="api_key"),
                    replace(profiles["claude"], secret_env=("PRIVATE_TOKEN",)),
                    providers.Profile(id="custom", auth="oauth", command=("PRIVATE_COMMAND",))]:
        result = access_checks.check_native_access(profile, parent)
        assert result["state"] == result["source"] == "unsupported"
        assert_safe(result)


def agy_fixtures(profile, parent):
    binary = Path(profile.command[0])
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"synthetic executable; never launched")
    binary.chmod(0o700)
    token = Path(parent["HOME"]) / ".gemini/antigravity-cli/antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_bytes(b"PRIVATE_TOKEN")
    return binary, token


def test_agy_reuses_pin_and_cached_catalog_check_without_reading_token_or_persisting(
    profiles, parent, monkeypatch,
):
    profile = replace(profiles["agy"], id="agy-alias", base="agy", first_class=False)
    binary, token = agy_fixtures(profile, parent)
    before = token.stat()
    commands = []
    temporary = []
    read_bytes = Path.read_bytes
    read_text = Path.read_text

    def no_token_read(method):
        def read(path, *args, **kwargs):
            assert path != token, "token contents must not be read"
            return method(path, *args, **kwargs)
        return read

    monkeypatch.setattr(Path, "read_bytes", no_token_read(read_bytes))
    monkeypatch.setattr(Path, "read_text", no_token_read(read_text))

    def run(argv, **kwargs):
        commands.append(argv)
        assert argv in [[str(binary), "--version"], [str(binary), "models"]]
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 30
        assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["check"] is False
        env = kwargs["env"]
        assert env["NO_BROWSER"] == "1" and env["CI"] == "1"
        assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        assert not providers.env_violations(env)
        temporary.append(Path(kwargs["cwd"]))
        assert temporary[-1].is_dir() and env["TMPDIR"] == kwargs["cwd"]
        output = (agy_cli_adapter.ADAPTER_VERSION if argv[-1] == "--version" else
                  "gemini-private-one\tPRIVATE_MODEL_ONE\ngemini-private-two\tPRIVATE_MODEL_TWO\n")
        return subprocess.CompletedProcess(argv, 0, output, "PRIVATE_STDERR")

    monkeypatch.setattr(access_checks.subprocess, "run", run)
    result = access_checks.check_native_access(profile, parent)
    assert commands == [[str(binary), "--version"], [str(binary), "models"]]
    assert result["state"] == "catalog_access" and result["source"] == "agy_models"
    assert result["model_count"] == 2 and result["plan"] is None
    assert_safe(result)
    assert all(not directory.exists() for directory in temporary)
    assert token.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("failure", ["missing_token", "wrong_pin", "version", "catalog", "timeout"])
def test_agy_failures_do_not_launch_login_or_return_raw_output(profiles, parent, monkeypatch, failure):
    profile = profiles["agy"]
    binary, token = agy_fixtures(profile, parent)
    if failure == "missing_token":
        token.unlink()
    if failure == "wrong_pin":
        profile = replace(profile, command=("/unqualified/agy",))
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        assert argv in [[str(binary), "--version"], [str(binary), "models"]]
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30, output="PRIVATE_OUTPUT")
        output = "PRIVATE_INVALID" if failure == "version" or argv[-1] == "models" else "1.1.26"
        return subprocess.CompletedProcess(argv, 0, output, "PRIVATE_STDERR")

    monkeypatch.setattr(access_checks.subprocess, "run", run)
    result = access_checks.check_native_access(profile, parent)
    assert result["state"] == "check_failed" and result["source"] == "agy_models"
    assert result["plan"] is None and result["model_count"] is None
    assert_safe(result)
    if failure in {"missing_token", "wrong_pin"}:
        assert not commands


def test_forbidden_profile_environment_fails_before_probe(profiles, parent, monkeypatch):
    profile = replace(profiles["claude"], env={"ANTHROPIC_API_KEY": "PRIVATE_KEY"})
    monkeypatch.setattr(access_checks.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not run"))
    result = access_checks.check_native_access(profile, parent)
    assert result["state"] == "check_failed"
    assert_safe(result)
