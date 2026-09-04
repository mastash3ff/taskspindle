"""The MCP surface: the sixteen tools, their annotations, and the envelope they all return."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastmcp import Client

from taskspindle.config import Paths
from taskspindle.server import READ_ONLY_TOOLS, TOOL_NAMES, build_server
from taskspindle.service import Orchestrator
from taskspindle.store import Store
from tests.fakes.units import FakeUnitBackend


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
        result = await client.call_tool(
            "start_task", {"provider": "claude", "mode": "implement", "prompt": "do it"}
        )

    assert result.data["ok"] is False
    assert result.data["result"] is None
    assert result.data["error"]["code"] == "INVALID_REQUEST"
    assert "acceptance_criteria" in result.data["error"]["details"]["errors"][0]["msg"]


async def test_a_missing_task_is_reported_by_code(server) -> None:
    async with Client(server) as client:
        result = await client.call_tool("task_status", {"task_id": "ts_absent"})

    assert result.data["ok"] is False
    assert result.data["error"]["code"] == "TASK_NOT_FOUND"
    assert result.data["error"]["details"]["task_id"] == "ts_absent"


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
