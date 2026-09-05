"""Pinned AGY setup and personal authentication without a network or model request."""

import asyncio
import json
import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from taskspindle import acp_client, agy_adapter, agy_cli_adapter, cli, discover, doctor, setup
from taskspindle.acp_client import AcpError, InitInfo
from taskspindle.config import Paths
from taskspindle.providers import Profile, ProfileError


@pytest.fixture
def paths(tmp_path):
    return Paths(
        config_file=tmp_path / "config/config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "data/runtimes/0.2.0",
    )


@pytest.fixture
def profile(paths):
    home = agy_adapter.prepare_agy_home(paths.data_dir)
    return Profile(
        id="agy",
        auth="oauth",
        first_class=True,
        command=agy_adapter.adapter_command(paths.runtime_dir),
        env={"GEMINI_HOME": str(home), "AGY_ACP_DISABLE_WORKSPACE_TRUST": "false"},
    )


def cached_token(profile):
    token = Path(profile.env["GEMINI_HOME"]) / "antigravity-acp/acp_token.json"
    token.write_text("not parsed by TaskSpindle", encoding="utf-8")
    token.chmod(0o600)
    return token


def fake_archive(url, target):
    assert url == agy_adapter.DOWNLOAD_URL
    with zipfile.ZipFile(target, "w") as archive:
        for name in agy_adapter.ADAPTER_FILES:
            archive.writestr(name, b"fake executable")


def test_setup_pins_both_executables_and_preserves_config_and_credential(paths):
    home = agy_adapter.prepare_agy_home(paths.data_dir)
    token = home / "antigravity-acp/acp_token.json"
    token.write_bytes(b"unchanged credential fixture")
    token.chmod(0o600)
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text("# existing configuration\n")

    report = setup.install_agy_acp_runtime(paths, downloader=fake_archive)

    assert report["adapter_version"] == "1.1.1"
    assert report["command"] == [
        str(paths.runtime_dir / "antigravity-acp/1.1.1/agy_acp_server.par"),
        "--uid=",
    ]
    for name in agy_adapter.ADAPTER_FILES:
        target = Path(report["adapter_dir"]) / name
        assert target.read_bytes() == b"fake executable"
        assert os.access(target, os.X_OK)
    assert token.read_bytes() == b"unchanged credential fixture"
    assert paths.config_file.read_text() == "# existing configuration\n"
    assert report["created_config"] is False
    assert not (paths.runtime_dir / "node_modules").exists()


def test_setup_reuses_installed_pin_without_network(paths):
    setup.install_agy_acp_runtime(paths, downloader=fake_archive)

    def forbidden_download(*args):
        pytest.fail("an installed adapter must not be downloaded again")

    assert setup.install_agy_acp_runtime(paths, downloader=forbidden_download)["already_installed"] is True


@pytest.mark.parametrize("bad_name", ["../outside", "subdir/agy_acp_server.par", "unrelated-file"])
def test_setup_rejects_unexpected_archive_members_without_partial_install(paths, bad_name):
    def download(url, target):
        with zipfile.ZipFile(target, "w") as archive:
            for name in (*agy_adapter.ADAPTER_FILES, bad_name):
                archive.writestr(name, b"bytes")

    with pytest.raises(setup.SetupError, match="expected executables"):
        setup.install_agy_acp_runtime(paths, downloader=download)

    assert not (paths.runtime_dir / "antigravity-acp/1.1.1").exists()
    assert not (paths.runtime_dir / "outside").exists()


def test_setup_rejects_unsupported_platform_before_downloading(paths, monkeypatch):
    monkeypatch.setattr(setup.platform, "machine", lambda: "aarch64")
    with pytest.raises(setup.SetupError, match="Linux x86-64"):
        setup.install_agy_acp_runtime(paths, downloader=lambda *args: pytest.fail("unexpected download"))


def test_oauth_evidence_reads_no_token_contents(profile, monkeypatch):
    token = cached_token(profile)
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        if path == token:
            pytest.fail("credential contents were read")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    evidence = agy_adapter.agy_oauth_evidence(profile)
    assert evidence["auth_method_id"] == "oauth-personal"
    assert evidence["credential_validity"] == "unverified"
    assert "not parsed" not in json.dumps(evidence)


def test_missing_or_nonprivate_token_returns_actionable_failure(profile):
    with pytest.raises(ProfileError, match="taskspindle auth agy"):
        agy_adapter.agy_oauth_evidence(profile)
    token = cached_token(profile)
    token.chmod(0o644)
    with pytest.raises(ProfileError) as caught:
        agy_adapter.agy_oauth_evidence(profile)
    assert caught.value.code == "AGY_AUTH_UNSAFE"


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        ("config/mcp_config.json", {"mcpServers": {"inherited": {"command": "bad"}}}),
        ("config/hooks.json", {"hooks": {"before": [{"command": "bad"}]}}),
        ("antigravity-acp/trusted_workspaces.json", {"trusted": ["/workspace"]}),
        ("antigravity-acp/settings.json", {"auth": {"type": "gemini-api-key"}}),
    ],
)
def test_oauth_evidence_refuses_inherited_controls(profile, relative, body):
    cached_token(profile)
    target = Path(profile.env["GEMINI_HOME"]) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(body))
    with pytest.raises(ProfileError) as caught:
        agy_adapter.agy_oauth_evidence(profile)
    assert caught.value.code == "AGY_CONFIG_UNSAFE"


def test_empty_controls_and_explicit_untrusted_workspace_are_allowed(profile):
    cached_token(profile)
    home = Path(profile.env["GEMINI_HOME"])
    (home / "config").mkdir()
    (home / "config/mcp_config.json").write_text('{"mcpServers": {}}')
    (home / "config/hooks.json").write_text('{"hooks": {}}')
    (home / "antigravity-acp/trusted_workspaces.json").write_text('{"trusted": [], "untrusted": ["/work"]}')
    (home / "antigravity-acp/settings.json").write_text('{"auth": {"type": "oauth-personal"}}')
    assert agy_adapter.agy_oauth_evidence(profile)["cached_credential"] is True


def test_oauth_evidence_rejects_symlinked_global_configuration(profile, tmp_path):
    cached_token(profile)
    external = tmp_path / "external-config"
    external.mkdir()
    (Path(profile.env["GEMINI_HOME"]) / "config").symlink_to(external, target_is_directory=True)
    with pytest.raises(ProfileError, match="symlinked"):
        agy_adapter.agy_oauth_evidence(profile)


LOGIN = "https://accounts.google.com/o/oauth2/v2/auth?response_type=code&client_id=public&state=flow"
PREFIX = "Open the following link to authenticate the ACP server: "


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9876/?code=secret",
        LOGIN.replace("accounts.google.com", "accounts.google.com.evil.test"),
        LOGIN.replace("accounts.google.com", "accounts.google.com@evil.test"),
        LOGIN + "&access_token=secret",
        LOGIN + "&client_secret=secret",
        LOGIN + "#secret",
        LOGIN.replace("/o/oauth2/v2/auth", "/callback"),
    ],
)
def test_auth_output_never_emits_callbacks_tokens_or_other_hosts(url):
    assert agy_adapter.login_urls(PREFIX + url) == []
    assert agy_adapter.login_urls("other log " + LOGIN) == []
    assert agy_adapter.login_urls(PREFIX + LOGIN) == [LOGIN]


async def test_interactive_auth_is_explicit_bounded_and_has_no_model_session(paths, profile, monkeypatch):
    calls = []
    output = []

    class FakeWorker:
        def __init__(self, **kwargs):
            self.stderr = kwargs["stderr_path"]
            calls.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def authenticate(self, method, *, timeout):
            calls.append((method, timeout))
            with self.stderr.open("ab") as log:
                log.write((PREFIX + LOGIN + "\nhttp://127.0.0.1/?code=secret\n").encode())
            await asyncio.sleep(0.15)
            cached_token(profile)

    monkeypatch.setattr(acp_client, "AcpWorker", FakeWorker)
    evidence = await agy_adapter.authenticate_personal(
        profile,
        paths,
        {"HOME": str(paths.data_dir), "PATH": "/usr/bin", "META_API_KEY": "secret"},
        emit=output.append,
    )

    assert output == [LOGIN]
    assert calls[1] == ("oauth-personal", 330.0)
    assert "META_API_KEY" not in calls[0]["env"]
    assert str(calls[0]["stderr_path"]).startswith("/proc/self/fd/")
    assert evidence["cached_credential"] is True


@pytest.mark.parametrize(
    ("acp_code", "expected"),
    [("ACP_AUTH_FAILED", "AGY_AUTH_FAILED"), ("ACP_SPAWN_FAILED", "AGY_ADAPTER_MISSING")],
)
async def test_interactive_auth_failure_does_not_print_or_persist_raw_errors(
    paths, profile, monkeypatch, acp_code, expected
):
    output = []

    class FailedWorker:
        def __init__(self, **kwargs):
            self.stderr = kwargs["stderr_path"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def authenticate(self, *args, **kwargs):
            with self.stderr.open("ab") as log:
                log.write(b"http://127.0.0.1/?code=secret\naccess_token=secret\n")
            raise AcpError(acp_code, "callback and access_token=secret")

    monkeypatch.setattr(acp_client, "AcpWorker", FailedWorker)
    with pytest.raises(ProfileError) as caught:
        await agy_adapter.authenticate_personal(profile, paths, {}, emit=output.append)
    assert caught.value.code == expected
    assert "secret" not in str(caught.value)
    assert output == []
    assert list(paths.state_dir.iterdir()) == []


def test_cli_setup_selects_agy_without_installing_claude(paths, monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_paths", lambda runtime_dir=None: paths)
    monkeypatch.setattr(
        setup,
        "install_agy_runtime",
        lambda p: {
            "adapter_package": "antigravity-cli",
            "adapter_version": "1.1.26",
            "runtime_dir": str(p.runtime_dir),
            "config_file": str(p.config_file),
            "created_config": False,
        },
    )
    monkeypatch.setattr(setup, "install_runtime", lambda *a, **k: pytest.fail("Claude installer called"))
    assert cli.main(["setup", "--provider", "agy"]) == 0
    assert "antigravity-cli 1.1.26" in capsys.readouterr().out


@pytest.mark.parametrize("runtime", [None, "/tmp/pinned-agy-runtime"])
def test_cli_auth_does_not_open_state_store(paths, profile, monkeypatch, capsys, runtime):
    from taskspindle import providers
    from taskspindle.store import Store

    def resolve(selected=None):
        assert selected == runtime
        return paths

    monkeypatch.setattr(cli, "resolve_paths", resolve)
    monkeypatch.setattr(providers, "load_profiles", lambda *a, **k: {"agy": profile})
    monkeypatch.setattr(Store, "open", lambda *a, **k: pytest.fail("auth opened the task store"))

    def auth(*args, **kwargs):
        return {"cached_credential": True, "model_count": 2}

    monkeypatch.setattr(agy_cli_adapter, "agy_oauth_evidence", auth)
    args = ["auth", "agy"] + (["--runtime-dir", runtime] if runtime else [])
    assert cli.main(args) == 0
    assert "2 Gemini models advertised" in capsys.readouterr().out


def test_custom_runtime_keeps_credential_home_in_configured_data_dir(paths, tmp_path):
    from taskspindle import providers

    profiles = providers.load_profiles(
        {}, runtime_dir=tmp_path / "custom-adapters", home=tmp_path / "user",
        state_dir=paths.state_dir, data_dir=paths.data_dir,
    )
    assert profiles["agy"].env == {}
    assert profiles["agy"].command == agy_cli_adapter.adapter_command(tmp_path / "custom-adapters")


async def test_doctor_checks_cache_and_initialize_without_starting_login(paths, profile, monkeypatch):
    instance = doctor._Doctor(
        profiles={"agy": profile},
        paths=paths,
        parent_env={},
        live_probes=True,
        runner=lambda *a, **k: pytest.fail("unexpected command"),
    )
    instance.agy_acp_oauth()
    assert instance.checks[-1].ok is False
    assert "taskspindle auth agy" in instance.checks[-1].detail
    cached_token(profile)
    instance.agy_acp_oauth()
    assert instance.checks[-1].ok is True

    async def init(*args):
        return InitInfo(
            load_session=True,
            auth_method_ids=("oauth-personal",),
            agent_info={"name": "antigravity-acp", "version": "agy_acp_server_1.1.1"},
        )

    monkeypatch.setattr(instance, "_init_probe", init)
    assert (await instance._agy_acp()).ok is True


@pytest.mark.parametrize("probe", ["auth", "doctor", "doctor_alias"])
async def test_agy_probes_deny_unknown_trust_mcp_and_delegation_permissions(
    paths, profile, monkeypatch, tmp_path, probe,
):
    options = [
        PermissionOption(option_id="trust", name="Allow once", kind="allow_once"),
        PermissionOption(option_id="always", name="Always allow", kind="allow_always"),
        PermissionOption(option_id="deny", name="Reject", kind="reject_once"),
    ]
    requests = [
        ToolCallUpdate(tool_call_id="trust", title="Trust this workspace?"),
        ToolCallUpdate(tool_call_id="unknown", title="Unknown capability", kind="other"),
        ToolCallUpdate(tool_call_id="mcp", title="external_tool", kind="other",
                       field_meta={"mcp": {"server": "inherited"}}, raw_input={}),
        ToolCallUpdate(tool_call_id="delegate", title="Run start_subagent?", kind="other"),
        ToolCallUpdate(tool_call_id="mode", title="Use yolo", kind="switch_mode"),
        ToolCallUpdate(tool_call_id="edit", title="Run create_file?", kind="edit",
                       raw_input={"path": "sentinel"}),
        ToolCallUpdate(tool_call_id="execute", title="touch sentinel", kind="execute",
                       raw_input={"command": "touch sentinel"}),
    ]
    decisions = []

    class PermissionWorker:
        def __init__(self, **kwargs):
            self.policy = kwargs["policy"]
            self.workspace = kwargs["cwd"]
            self.init = InitInfo(True, ("oauth-personal",), {"name": "antigravity-acp"})

        def _record_permission(self, call, option_id, violation):
            decisions.append((call.tool_call_id, option_id, violation))

        async def ask_permissions(self):
            client = acp_client._WorkerClient(self)
            for call in requests:
                response = await client.request_permission("probe", call, options)
                assert response.outcome.option_id == "deny"
            assert self.policy.workspace == self.workspace.resolve()

        async def __aenter__(self):
            await self.ask_permissions()
            return self

        async def __aexit__(self, *args):
            pass

        async def authenticate(self, method, *, timeout):
            assert probe == "auth"
            assert method == "oauth-personal"
            await self.ask_permissions()
            cached_token(profile)

    if probe == "auth":
        monkeypatch.setattr(acp_client, "AcpWorker", PermissionWorker)
        await agy_adapter.authenticate_personal(profile, paths, {}, emit=lambda _: None)
    else:
        if probe == "doctor_alias":
            profile = replace(profile, id="agy-alias", base="agy", first_class=False)
        monkeypatch.setattr(doctor, "AcpWorker", PermissionWorker)
        instance = doctor._Doctor(
            profiles={profile.id: profile}, paths=paths, parent_env={}, live_probes=True,
            runner=lambda *a, **k: pytest.fail("unexpected command"),
        )
        await instance._init_probe(profile, tmp_path / "probe")
    assert len(decisions) == len(requests) * (2 if probe == "auth" else 1)
    assert all(option == "deny" and violation for _, option, violation in decisions)


@pytest.mark.parametrize("family", ["claude", "grok", "configured"])
async def test_other_doctor_providers_retain_existing_permission_policy(paths, monkeypatch, tmp_path, family):
    class Worker:
        def __init__(self, **kwargs):
            assert type(kwargs["policy"]) is acp_client.PermissionPolicy
            self.init = InitInfo(True, (), {"name": family})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    profile = Profile(id=family, auth="oauth", command=("unused-adapter",))
    monkeypatch.setattr(doctor, "AcpWorker", Worker)
    instance = doctor._Doctor(
        profiles={family: profile}, paths=paths, parent_env={}, live_probes=True,
        runner=lambda *a, **k: pytest.fail("unexpected command"),
    )
    assert (await instance._init_probe(profile, tmp_path / "probe")).agent_info["name"] == family


def test_discovery_maps_official_adapter_to_reserved_agy():
    agent = discover.RegistryAgent(
        id="antigravity-acp",
        name="Google Antigravity",
        description="",
        candidates=(("agy_acp_server.par", ("--uid=",)),),
    )
    assert discover.first_class_match(agent) == "agy"
