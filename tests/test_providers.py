"""Profiles, the child environment allowlist, and OAuth evidence."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from acp.schema import NewSessionRequest
from acp.utils import serialize_params

from taskspindle.providers import (
    Profile,
    ProfileError,
    build_child_env,
    builtin_profiles,
    claude_oauth_evidence,
    env_violations,
    grok_oauth_evidence,
    load_profiles,
    opposite_provider,
    pinned_node,
    profile_for_task,
    reviewer_independent,
    session_options,
    write_grok_overlay,
)

LEAKY_PARENT = {
    "HOME": "/home/tester",
    "USER": "tester",
    "LOGNAME": "tester",
    "PATH": "/usr/bin:/opt/whatever/node_modules/.bin:/bin",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "XAI_API_KEY": "xai-secret",
    "HTTPS_PROXY": "http://proxy.internal:3128",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "OPENAI_BASE_URL": "https://gateway.internal/v1",
    "GITHUB_TOKEN": "ghp-secret",
}


def _dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        "runtime_dir": tmp_path / "runtimes" / "0.1.0",
        "home": tmp_path / "home",
        "state_dir": tmp_path / "state",
    }


def _builtins(tmp_path: Path) -> dict[str, Profile]:
    dirs = _dirs(tmp_path)
    return builtin_profiles(dirs["runtime_dir"], home=dirs["home"], state_dir=dirs["state_dir"])


# -- built-ins -----------------------------------------------------------------------------------


def test_grok_argv_denies_leaders_and_subagents(tmp_path: Path) -> None:
    grok = _builtins(tmp_path)["grok"]

    # ``--no-subagents`` is a top-level flag and must precede the ``agent`` subcommand: Grok 1.0.13
    # rejects it after ``agent``.
    assert grok.command[:3] == ("grok", "--no-subagents", "agent")
    assert grok.command[-1] == "stdio"
    assert "--no-leader" in grok.command
    assert "--model" in grok.command
    assert grok.model == "grok-4.6"
    assert grok.effort == "medium"


def test_grok_env_switches_off_every_vendor_compatibility_source(tmp_path: Path) -> None:
    grok = _builtins(tmp_path)["grok"]

    expected = {
        f"GROK_{vendor}_{source}_ENABLED"
        for vendor in ("CLAUDE", "CURSOR", "CODEX")
        for source in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS", "SESSIONS")
    }
    assert expected <= set(grok.env)
    assert all(grok.env[name] == "false" for name in expected)
    assert grok.env["GROK_DISABLE_API_KEY_AUTH"] == "true"


def test_grok_overlay_is_private_and_backs_up_the_subagent_flag(tmp_path: Path) -> None:
    path = write_grok_overlay(tmp_path / "state")
    first = path.read_text(encoding="utf-8")
    # A rewrite is a no-op in effect.
    assert write_grok_overlay(tmp_path / "state").read_text(encoding="utf-8") == first

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "[subagents]\nenabled = false" in first
    # Grok ignores [compat.*] keys in a GROK_CONFIG overlay; they live in the environment instead.
    assert "[compat." not in first


def test_claude_profile_points_at_the_pinned_adapter(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    claude = _builtins(tmp_path)["claude"]

    assert claude.command == (str(dirs["runtime_dir"] / "node_modules" / ".bin" / "claude-agent-acp"),)
    assert claude.env == {"CLAUDE_CONFIG_DIR": str(dirs["home"] / ".claude")}
    assert claude.model is None
    assert claude.first_class is True


# -- child environment ---------------------------------------------------------------------------


def test_child_env_for_claude_leaks_nothing(tmp_path: Path) -> None:
    claude = _builtins(tmp_path)["claude"]

    env = build_child_env(claude, LEAKY_PARENT, task_tmp=tmp_path / "tasktmp")

    for leaked in ("ANTHROPIC_API_KEY", "XAI_API_KEY", "HTTPS_PROXY", "SSH_AUTH_SOCK", "OPENAI_BASE_URL"):
        assert leaked not in env
    assert env_violations(env) == []
    assert env["CLAUDE_CONFIG_DIR"] == str(_dirs(tmp_path)["home"] / ".claude")
    assert env["TERM"] == "dumb"
    assert env["TMPDIR"] == str(tmp_path / "tasktmp")
    assert env["CI"] == "1"
    assert env["HOME"] == "/home/tester"
    assert "sk-ant-secret" not in "".join(env.values())


def test_child_env_path_drops_foreign_node_modules_and_prepends_the_runtime(tmp_path: Path) -> None:
    claude = _builtins(tmp_path)["claude"]

    entries = build_child_env(claude, LEAKY_PARENT, task_tmp=tmp_path).get("PATH", "").split(":")

    assert entries[0] == str(_dirs(tmp_path)["runtime_dir"] / "node_modules" / ".bin")
    assert "/opt/whatever/node_modules/.bin" not in entries[1:]
    assert entries[1:] == ["/usr/bin", "/bin"]


def _pin_node(runtime_dir: Path, node: Path) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "node-path").write_text(f"{node}\n", encoding="utf-8")


def test_pinned_node_reads_back_what_setup_wrote(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    node = tmp_path / "node-bin" / "node"
    _pin_node(runtime_dir, node)

    assert pinned_node(runtime_dir) == node


def test_pinned_node_is_none_when_setup_never_pinned_one(tmp_path: Path) -> None:
    assert pinned_node(tmp_path / "no-such-runtime") is None


def test_child_env_path_puts_the_pinned_node_ahead_of_the_runtime_bin_for_claude(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    node = tmp_path / "node-bin" / "node"
    _pin_node(dirs["runtime_dir"], node)
    claude = _builtins(tmp_path)["claude"]

    entries = build_child_env(claude, LEAKY_PARENT, task_tmp=tmp_path).get("PATH", "").split(":")

    assert entries[0] == str(node.parent)
    assert entries[1] == str(dirs["runtime_dir"] / "node_modules" / ".bin")
    assert entries[2:] == ["/usr/bin", "/bin"]


def test_child_env_path_puts_the_pinned_node_ahead_for_a_claude_based_profile(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    node = tmp_path / "node-bin" / "node"
    _pin_node(dirs["runtime_dir"], node)
    builtins = _builtins(tmp_path)
    based_on_claude = Profile(
        id="claude-litellm",
        auth="api_key",
        command=builtins["claude"].command,
        env={"ANTHROPIC_BASE_URL": "https://gateway.internal/v1"},
        secret_env=("ANTHROPIC_AUTH_TOKEN",),
        base="claude",
    )
    parent = {**LEAKY_PARENT, "ANTHROPIC_AUTH_TOKEN": "gateway-token"}

    entries = build_child_env(based_on_claude, parent, task_tmp=tmp_path).get("PATH", "").split(":")

    assert entries[0] == str(node.parent)
    assert entries[1] == str(dirs["runtime_dir"] / "node_modules" / ".bin")


def test_child_env_path_leaves_grok_alone_even_when_a_node_is_pinned(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    node = tmp_path / "node-bin" / "node"
    _pin_node(dirs["runtime_dir"], node)
    grok = _builtins(tmp_path)["grok"]

    entries = build_child_env(grok, LEAKY_PARENT, task_tmp=tmp_path).get("PATH", "").split(":")

    assert str(node.parent) not in entries
    assert str(dirs["runtime_dir"] / "node_modules" / ".bin") not in entries
    assert entries == ["/usr/bin", "/bin"]


def test_api_key_profile_passes_exactly_its_declared_secret(tmp_path: Path) -> None:
    profile = Profile(
        id="gateway",
        auth="api_key",
        command=("some-acp-harness",),
        env={"ANTHROPIC_BASE_URL": "https://gateway.internal/v1"},
        secret_env=("ANTHROPIC_AUTH_TOKEN",),
    )
    parent = {**LEAKY_PARENT, "ANTHROPIC_AUTH_TOKEN": "gateway-token"}

    env = build_child_env(profile, parent, task_tmp=tmp_path)

    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.internal/v1"
    assert "ANTHROPIC_API_KEY" not in env
    assert "XAI_API_KEY" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert env_violations(env, allowed=("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")) == []


def test_missing_secret_is_reported_without_its_value(tmp_path: Path) -> None:
    profile = Profile(
        id="gateway",
        auth="api_key",
        command=("some-acp-harness",),
        secret_env=("ANTHROPIC_AUTH_TOKEN",),
    )

    with pytest.raises(ProfileError) as excinfo:
        build_child_env(profile, LEAKY_PARENT, task_tmp=tmp_path)

    assert excinfo.value.code == "PROFILE_INVALID"
    message = str(excinfo.value)
    assert "ANTHROPIC_AUTH_TOKEN" in message
    assert "sk-ant-secret" not in message


def test_oauth_profile_may_not_carry_a_gateway_base_url(tmp_path: Path) -> None:
    profile = Profile(
        id="sneaky",
        auth="oauth",
        command=("some-acp-harness",),
        env={"ANTHROPIC_BASE_URL": "https://gateway.internal/v1"},
    )

    with pytest.raises(ProfileError) as excinfo:
        build_child_env(profile, LEAKY_PARENT, task_tmp=tmp_path)

    assert excinfo.value.code == "PROFILE_INVALID"


def test_env_violations_spots_proxies_in_any_case_but_not_grok_flags() -> None:
    assert env_violations({"https_proxy": "x"}) == ["https_proxy"]
    assert env_violations({"GROK_DISABLE_API_KEY_AUTH": "true"}) == []
    assert env_violations({"NPM_TOKEN": "x", "LANG": "C"}) == ["NPM_TOKEN"]


# -- configured profiles -------------------------------------------------------------------------


def test_load_profiles_rejects_a_reserved_id(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as excinfo:
        load_profiles({"providers": {"claude": {"auth": "api_key", "command": ["x"]}}}, **_dirs(tmp_path))

    assert excinfo.value.code == "PROFILE_INVALID"


def test_load_profiles_rejects_secret_env_on_an_oauth_profile(tmp_path: Path) -> None:
    config = {"providers": {"other": {"auth": "oauth", "command": ["x"], "secret_env": ["SOME_TOKEN"]}}}

    with pytest.raises(ProfileError) as excinfo:
        load_profiles(config, **_dirs(tmp_path))

    assert excinfo.value.code == "PROFILE_INVALID"


def test_load_profiles_requires_a_command_without_a_base(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as excinfo:
        load_profiles({"providers": {"other": {"auth": "api_key"}}}, **_dirs(tmp_path))

    assert excinfo.value.code == "PROFILE_INVALID"


def test_grok_based_profile_substitutes_model_and_effort(tmp_path: Path) -> None:
    config = {
        "providers": {
            "grok-fast": {"base": "grok", "auth": "oauth", "model": "grok-4.6-fast", "effort": "low"}
        }
    }

    profile = load_profiles(config, **_dirs(tmp_path))["grok-fast"]

    assert profile.command == (
        "grok",
        "--no-subagents",
        "agent",
        "--model",
        "grok-4.6-fast",
        "--reasoning-effort",
        "low",
        "--no-leader",
        "stdio",
    )
    assert profile.env["GROK_DISABLE_API_KEY_AUTH"] == "true"
    assert profile.first_class is False
    assert profile.base == "grok"


def test_claude_based_profile_keeps_the_adapter_argv_and_records_the_gateway(tmp_path: Path) -> None:
    config = {
        "providers": {
            "bedrockish": {
                "base": "claude",
                "auth": "api_key",
                "model": "some-cloud-model",
                "secret_env": ["ANTHROPIC_AUTH_TOKEN"],
                "env": {"ANTHROPIC_BASE_URL": "https://gateway.internal/v1"},
            }
        }
    }

    profiles = load_profiles(config, **_dirs(tmp_path))
    profile = profiles["bedrockish"]

    assert profile.command == profiles["claude"].command
    assert profile.gateway_host == "gateway.internal"
    assert "ANTHROPIC_MODEL" not in profile.env
    assert profile.env["CLAUDE_CONFIG_DIR"] == profiles["claude"].env["CLAUDE_CONFIG_DIR"]


def test_load_profiles_writes_the_grok_overlay(tmp_path: Path) -> None:
    dirs = _dirs(tmp_path)
    profiles = load_profiles({}, **dirs)

    overlay = Path(profiles["grok"].env["GROK_CONFIG"])
    assert overlay.is_file()
    assert stat.S_IMODE(overlay.stat().st_mode) == 0o600


# -- selection -----------------------------------------------------------------------------------


def test_profile_for_task_enforces_metering_and_modes(tmp_path: Path) -> None:
    config = {
        "providers": {
            "metered": {"auth": "api_key", "command": ["x"]},
            "reviewer": {"auth": "oauth", "command": ["y"], "modes": ["review"]},
        }
    }
    profiles = load_profiles(config, **_dirs(tmp_path))

    assert profile_for_task(profiles, "metered", mode="review", allow_metered=True).id == "metered"

    with pytest.raises(ProfileError) as metered:
        profile_for_task(profiles, "metered", mode="review", allow_metered=False)
    assert metered.value.code == "METERED_NOT_ALLOWED"

    with pytest.raises(ProfileError) as mode:
        profile_for_task(profiles, "reviewer", mode="implement", allow_metered=False)
    assert mode.value.code == "MODE_NOT_ALLOWED"

    with pytest.raises(ProfileError) as unknown:
        profile_for_task(profiles, "nope", mode="review", allow_metered=True)
    assert unknown.value.code == "PROFILE_UNKNOWN"


def test_opposite_and_independence(tmp_path: Path) -> None:
    builtins = _builtins(tmp_path)

    assert opposite_provider("claude") == "grok"
    assert opposite_provider("grok") == "claude"
    assert opposite_provider("metered") is None

    assert reviewer_independent(builtins["claude"], builtins["grok"]) is True
    assert reviewer_independent(builtins["claude"], builtins["claude"]) is False


# -- session options -----------------------------------------------------------------------------


def test_session_options_deny_delegation_and_travel_as_meta(tmp_path: Path) -> None:
    claude = _builtins(tmp_path)["claude"]

    options = session_options(claude)
    assert options == {
        "claudeCode": {
            "options": {
                "settingSources": [],
                "disallowedTools": ["Agent", "Task", "TeamCreate", "SendMessage"],
                "mcpServers": {},
            }
        }
    }
    assert session_options(_builtins(tmp_path)["grok"]) == {}

    # ClientSideConnection.new_session(**kwargs) folds extra keywords into ``field_meta``, which
    # serializes under the ``_meta`` alias. This is the wire form the adapter reads.
    request = NewSessionRequest(cwd="/w", mcp_servers=[], field_meta=options)
    assert serialize_params(request)["_meta"] == options


def test_session_options_carry_a_configured_model(tmp_path: Path) -> None:
    config = {"providers": {"claude-opus": {"base": "claude", "auth": "oauth", "model": "opus-x"}}}
    profile = load_profiles(config, **_dirs(tmp_path))["claude-opus"]

    assert session_options(profile)["claudeCode"]["options"]["model"] == "opus-x"


# -- OAuth evidence ------------------------------------------------------------------------------


def _completed(payload: Any, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["claude", "auth", "status"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


GOOD_STATUS = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "subscriptionType": "max",
    "apiProvider": "firstParty",
    "email": "tester@example.com",
    "organizationId": "org-1234",
}


def test_claude_oauth_evidence_is_redacted() -> None:
    evidence = claude_oauth_evidence(lambda _command: _completed(GOOD_STATUS))

    assert evidence == {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "subscriptionType": "max",
        "apiProvider": "firstParty",
    }
    assert "tester@example.com" not in json.dumps(evidence)
    assert "org-1234" not in json.dumps(evidence)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("apiProvider", "bedrock"),
        ("subscriptionType", "pro"),
        ("authMethod", "apiKey"),
        ("loggedIn", False),
    ],
)
def test_claude_oauth_evidence_rejects_a_wrong_field(field_name: str, value: Any) -> None:
    payload = {**GOOD_STATUS, field_name: value}

    with pytest.raises(ProfileError) as excinfo:
        claude_oauth_evidence(lambda _command: _completed(payload))

    assert excinfo.value.code == "OAUTH_REJECTED"
    assert field_name in str(excinfo.value)


def test_claude_oauth_evidence_rejects_non_json() -> None:
    broken = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")

    with pytest.raises(ProfileError) as excinfo:
        claude_oauth_evidence(lambda _command: broken)

    assert excinfo.value.code == "OAUTH_REJECTED"


def test_grok_oauth_evidence_requires_a_cached_token_and_auth_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)

    with pytest.raises(ProfileError) as missing_file:
        grok_oauth_evidence([{"id": "cached_token"}], home=home)
    assert missing_file.value.code == "OAUTH_REJECTED"

    (home / ".grok" / "auth.json").write_text("{}", encoding="utf-8")
    assert grok_oauth_evidence([{"id": "cached_token"}], home=home) == {
        "auth_method_ids": ["cached_token"],
        "auth_file_present": True,
    }

    with pytest.raises(ProfileError) as wrong_method:
        grok_oauth_evidence([{"id": "api_key"}], home=home)
    assert wrong_method.value.code == "OAUTH_REJECTED"
