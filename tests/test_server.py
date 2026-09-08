"""The MCP surface: the seventeen tools, their annotations, and the envelope they all return."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import taskspindle
from taskspindle import doctor
from taskspindle.acp_client import InitInfo
from taskspindle.config import Paths
from taskspindle.orchestrator import Orchestrator
from taskspindle.providers import Profile
from taskspindle.server import READ_ONLY_TOOLS, TOOL_NAMES, build_server
from taskspindle.store import Store
from tests.fakes.units import FakeUnitBackend
from tests.test_doctor import HEALTHY, RecordedRunner, install_adapter


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


@pytest.fixture
def store(paths: Paths) -> Iterator[Store]:
    store = Store.open(paths.state_dir / "taskspindle.sqlite3")
    yield store
    store.close()


@pytest.fixture
def server(store: Store, paths: Paths):
    orchestrator = Orchestrator(
        store=store,
        paths=paths,
        profiles={},
        units=FakeUnitBackend(),
        boot="boot-under-test",
        parent_env={"HOME": str(paths.state_dir), "PATH": "/usr/bin"},
    )
    return build_server(orchestrator)


async def test_the_seventeen_tools_are_exposed_with_honest_annotations(server) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == list(TOOL_NAMES)
    assert len(tools) == 17
    read_only = {tool.name for tool in tools if tool.annotations.readOnlyHint}
    assert read_only == set(READ_ONLY_TOOLS)
    assert "task_diff" not in read_only


async def test_capabilities_returns_an_envelope(server) -> None:
    async with Client(server) as client:
        result = await client.call_tool("capabilities", {})

    assert result.data["api_version"] == 1
    assert result.data["ok"] is True
    assert result.data["error"] is None
    assert result.data["result"]["versions"]["api"] == 1
    assert result.data["result"]["modes"] == ["consult", "review", "implement"]


async def test_a_bad_start_task_payload_is_an_invalid_request(server) -> None:
    async with Client(server) as client:
        incomplete = await client.call_tool(
            "start_task",
            {"request": {"provider": "claude", "mode": "implement", "prompt": "do it"}},
        )
        nonsense = await client.call_tool("start_task", {"request": {"mode": "bogus"}})

    assert incomplete.data["ok"] is False
    assert incomplete.data["result"] is None
    assert incomplete.data["error"]["code"] == "INVALID_REQUEST"
    assert "acceptance_criteria" in incomplete.data["error"]["details"]["errors"][0]["msg"]

    # A value the schema itself rejects still comes back inside the envelope, not as a fault.
    assert nonsense.data["ok"] is False
    assert nonsense.data["error"]["code"] == "INVALID_REQUEST"
    locations = {
        tuple(error["loc"]) for error in nonsense.data["error"]["details"]["errors"]
    }
    assert ("mode",) in locations


async def test_the_request_object_tools_describe_their_fields(server) -> None:
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("start_task", "accept_task", "record_integration", "authorize_repository"):
        assert list(tools[name].inputSchema["properties"]) == ["request"]
        assert tools[name].inputSchema["properties"]["request"]["type"] == "object"
    assert "- acceptance_criteria: string | null (optional)" in tools["start_task"].description
    assert "- provider: string (required)" in tools["start_task"].description
    assert "- modes: array of" in tools["authorize_repository"].description


async def test_usage_report_and_the_diff_page_default_are_on_the_wire(server) -> None:
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        result = await client.call_tool("usage_report", {"since": "7d", "group_by": "repository_id"})

    assert tools["task_diff"].inputSchema["properties"]["length"]["default"] == 16384
    assert result.data["ok"] is True
    assert result.data["result"]["group_by"] == "repository_id"
    assert result.data["result"]["usage"] == []
    assert result.data["result"]["turns"]["count"] == 0
    assert "cost_note" in result.data["result"]


async def test_a_missing_task_is_reported_by_code(server) -> None:
    async with Client(server) as client:
        result = await client.call_tool("task_status", {"task_id": "ts_absent"})

    assert result.data["ok"] is False
    assert result.data["error"]["code"] == "TASK_NOT_FOUND"
    assert result.data["error"]["details"]["task_id"] == "ts_absent"


async def test_doctor_runs_its_live_probes_on_the_servers_own_loop(
    store: Store, paths: Paths, monkeypatch
) -> None:
    """The Grok handshake is awaited, not run in a second event loop inside the running one."""
    home = paths.state_dir / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "auth.json").write_text(json.dumps({"token": "cached"}), encoding="utf-8")
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    grok = Profile(id="grok", auth="oauth", command=("/bin/sh",))
    recorded = RecordedRunner(HEALTHY)

    def run(self: object, argv: Sequence[str], *, timeout: float = 30.0) -> Any:
        return recorded(argv, timeout=timeout)

    async def handshake(self: object, profile: Profile, workspace: Path) -> InitInfo:
        return InitInfo(load_session=True, auth_method_ids=("cached_token",), agent_info={})

    monkeypatch.setattr(doctor._Doctor, "run", run)
    monkeypatch.setattr(doctor._Doctor, "_init_probe", handshake)
    orchestrator = Orchestrator(
        store=store,
        paths=paths,
        profiles={"grok": grok},
        units=FakeUnitBackend(),
        boot="boot-under-test",
        parent_env={"HOME": str(home), "PATH": "/usr/bin"},
    )

    async with Client(build_server(orchestrator)) as client:
        result = await client.call_tool("doctor", {"live_probes": True})

    assert result.data["ok"] is True
    report = result.data["result"]
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["grok_acp"] == {
        "name": "grok_acp",
        "ok": True,
        "detail": "load_session and cached_token",
        "advisory": False,
    }
    assert checks["transient_unit"]["ok"] is True
    assert report["ok"] is True


async def test_the_server_log_captures_a_traceback(server, paths: Paths, monkeypatch) -> None:
    def explode(*_: object, **__: object) -> dict[str, object]:
        raise RuntimeError("the store fell over")

    monkeypatch.setattr(Store, "list_repositories", explode)
    async with Client(server) as client:
        result = await client.call_tool("list_repository_policies", {})

    assert result.data["ok"] is False
    assert result.data["error"]["code"] == "INTERNAL"
    assert result.data["error"]["details"] == {"exception": "RuntimeError"}
    assert "the store fell over" not in result.data["error"]["message"]
    log = (paths.state_dir / "server.log").read_text(encoding="utf-8")
    assert "RuntimeError: the store fell over" in log


@pytest.mark.parametrize("choice", ["retry", "cancel"])
async def test_ambiguous_recovery_elicits_without_changing_the_callers_version(
    server, monkeypatch, choice: str,
) -> None:
    from taskspindle.service import MANUAL_RECOVERY_REQUIRED, TaskSpindleError

    calls = []

    def attempt(self, task_id, version, prompt=""):
        calls.append(("retry", task_id, version, prompt))
        if len(calls) == 1:
            raise TaskSpindleError(MANUAL_RECOVERY_REQUIRED, "ambiguous", details={"evidence": "worker"})
        return {"state": "QUEUED"}

    def cancel(self, task_id, version):
        calls.append(("cancel", task_id, version))
        return {"state": "CANCELLED"}

    async def answer(message, response_type, params, context):
        assert "worker" in message
        return {"value": choice}

    monkeypatch.setattr(Orchestrator, "continue_task", attempt)
    monkeypatch.setattr(Orchestrator, "cancel_task", cancel)
    async with Client(server, elicitation_handler=answer) as client:
        tools = await client.list_tools()
        schema = next(t.inputSchema for t in tools if t.name == "continue_task")
        assert "ctx" not in schema["properties"]
        result = await client.call_tool("continue_task", {
            "task_id": "ts_example", "expected_state_version": 7, "prompt": "resume",
        })
    assert result.data["ok"] is True
    assert calls[0] == ("retry", "ts_example", 7, "resume")
    assert calls[1] == (("retry", "ts_example", 7, "resume") if choice == "retry"
                        else ("cancel", "ts_example", 7))


@pytest.mark.parametrize("action", [None, "decline", "cancel", "broken", "invalid"])
async def test_ambiguous_recovery_keeps_the_error_without_an_accepted_choice(
    server, monkeypatch, action,
) -> None:
    from fastmcp.client.elicitation import ElicitResult

    from taskspindle.service import MANUAL_RECOVERY_REQUIRED, TaskSpindleError

    calls = []

    def attempt(self, *args):
        calls.append(args)
        raise TaskSpindleError(MANUAL_RECOVERY_REQUIRED, "ambiguous", details={"evidence": "worker"})

    async def answer(*args):
        if action == "broken":
            raise RuntimeError("client could not ask")
        if action == "invalid":
            return {"value": "neither"}
        return ElicitResult(action=action)

    monkeypatch.setattr(Orchestrator, "continue_task", attempt)
    async with Client(server, elicitation_handler=answer if action else None) as client:
        result = await client.call_tool("continue_task", {
            "task_id": "ts_example", "expected_state_version": 7,
        })
    assert result.data["error"]["code"] == MANUAL_RECOVERY_REQUIRED
    assert result.data["error"]["details"] == {"evidence": "worker"}
    assert len(calls) == 1


@pytest.mark.parametrize("next_error", ["STALE_STATE_VERSION", "MANUAL_RECOVERY_REQUIRED"])
async def test_elicited_retry_preserves_recovery_guards(server, monkeypatch, next_error) -> None:
    from taskspindle.service import MANUAL_RECOVERY_REQUIRED, TaskSpindleError

    version = 7

    def attempt(self, task_id, expected, prompt):
        assert expected == 7
        if version != expected:
            raise TaskSpindleError(next_error, "state changed or still ambiguous")
        raise TaskSpindleError(MANUAL_RECOVERY_REQUIRED, "ambiguous")

    async def answer(*args):
        nonlocal version
        version = 8
        return {"value": "retry"}

    monkeypatch.setattr(Orchestrator, "continue_task", attempt)
    async with Client(server, elicitation_handler=answer) as client:
        result = await client.call_tool("continue_task", {
            "task_id": "ts_example", "expected_state_version": 7,
        })
    assert result.data["error"]["code"] == next_error



async def test_url_only_client_keeps_the_recovery_error(server, monkeypatch) -> None:
    from mcp.server.session import ServerSession
    from mcp.types import ElicitationCapability, UrlElicitationCapability

    from taskspindle.service import MANUAL_RECOVERY_REQUIRED, TaskSpindleError

    original = ServerSession.client_params.fget

    def url_only(session):
        params = original(session)
        if params is None:
            return None
        capability = ElicitationCapability(url=UrlElicitationCapability())
        return params.model_copy(update={"capabilities": params.capabilities.model_copy(
            update={"elicitation": capability},
        )})

    def attempt(self, *args):
        raise TaskSpindleError(MANUAL_RECOVERY_REQUIRED, "ambiguous", details={"evidence": "worker"})

    asked = False

    async def answer(*args):
        nonlocal asked
        asked = True
        return {"value": "retry"}

    monkeypatch.setattr(ServerSession, "client_params", property(url_only))
    monkeypatch.setattr(Orchestrator, "continue_task", attempt)
    async with Client(server, elicitation_handler=answer) as client:
        result = await client.call_tool("continue_task", {
            "task_id": "ts_example", "expected_state_version": 7,
        })
    assert asked is False
    assert result.data["error"]["code"] == MANUAL_RECOVERY_REQUIRED
    assert result.data["error"]["details"] == {"evidence": "worker"}


def test_build_orchestrator_loads_configured_capacity(paths, monkeypatch):
    from taskspindle.server import build_orchestrator

    paths.config_file.write_text("[concurrency]\nclaude = 4\ngrok = 4\nagy = 4\n")
    orchestrator, store = build_orchestrator(paths=paths, parent_env={"HOME": str(paths.state_dir)})
    try:
        assert orchestrator.concurrency == {"claude": 4, "grok": 4, "agy": 4}
        assert orchestrator.capabilities()["limits"]["concurrent_turns_per_provider"] == 4
    finally:
        store.close()


async def test_capabilities_checks_only_selected_provider_and_persists_cache(store, paths, monkeypatch):
    from taskspindle import grok_checks, providers
    from tests.test_grok_checks import observation
    profiles = providers.builtin_profiles(paths.runtime_dir, home=paths.state_dir, state_dir=paths.state_dir)
    orchestrator = Orchestrator(store=store, paths=paths, profiles=profiles,
        units=FakeUnitBackend(), boot="test", parent_env={"HOME": str(paths.state_dir), "PATH": "/usr/bin"})
    calls = []
    def check(profile, env):
        calls.append(profile.id)
        return observation()
    monkeypatch.setattr(grok_checks, "check_grok", check)
    async with Client(build_server(orchestrator)) as client:
        default = await client.call_tool("capabilities", {})
        assert default.data["ok"] and calls == []
        checked = await client.call_tool("capabilities", {"check_providers": ["grok"]})
        assert checked.data["ok"]
        native = next(row["native_check"] for row in checked.data["result"]["providers"]
                      if row["id"] == "grok")
        assert native["state"] == "quota" and calls == ["grok"]
        await client.call_tool("capabilities", {"check_providers": ["grok"]})
        assert calls == ["grok"]
        tools = await client.list_tools()
        assert next(tool for tool in tools if tool.name == "capabilities").annotations.readOnlyHint is False
    assert store.list_provider_status() == []
    assert store.list_tasks() == []
