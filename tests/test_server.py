"""The MCP surface: the sixteen tools, their annotations, and the envelope they all return."""

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


async def test_the_sixteen_tools_are_exposed_with_honest_annotations(server) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == list(TOOL_NAMES)
    assert len(tools) == 16
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
