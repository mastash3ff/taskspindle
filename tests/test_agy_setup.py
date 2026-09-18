"""Pinned AGY setup and personal authentication without a network or model request."""

from dataclasses import replace

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from taskspindle import acp_client, agy_cli_adapter, cli, discover, doctor, setup
from taskspindle.acp_client import InitInfo
from taskspindle.config import Paths
from taskspindle.providers import Profile


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
    return Profile(
        id="agy",
        auth="oauth",
        first_class=True,
        command=agy_cli_adapter.adapter_command(paths.data_dir),
        env={},
    )


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

    # agy's command no longer depends on ``runtime_dir`` at all -- it is resolved from
    # ``home``/``PATH``, so a controlled environment keeps this deterministic regardless of
    # what is actually installed on the machine running the test.
    home = tmp_path / "user"
    env = {"PATH": "/usr/bin:/bin"}
    profiles = providers.load_profiles(
        {}, runtime_dir=tmp_path / "custom-adapters", home=home,
        state_dir=paths.state_dir, data_dir=paths.data_dir, parent_env=env,
    )
    assert profiles["agy"].env == {}
    assert profiles["agy"].command == agy_cli_adapter.adapter_command(home, parent_env=env)


@pytest.mark.parametrize("probe", ["doctor", "doctor_alias"])
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

    if probe == "doctor_alias":
        profile = replace(profile, id="agy-alias", base="agy", first_class=False)
    monkeypatch.setattr(doctor, "AcpWorker", PermissionWorker)
    instance = doctor._Doctor(
        profiles={profile.id: profile}, paths=paths, parent_env={}, live_probes=True,
        runner=lambda *a, **k: pytest.fail("unexpected command"),
    )
    await instance._init_probe(profile, tmp_path / "probe")
    assert len(decisions) == len(requests)
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
