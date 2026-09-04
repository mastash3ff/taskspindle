"""The ACP client, exercised against the scriptable fake agent as a real subprocess."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from taskspindle.acp_client import AcpError, AcpWorker, PermissionPolicy, sealed_env
from taskspindle.providers import Profile, build_child_env, env_violations, session_options

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_ARGV = (sys.executable, "-m", "tests.fakes.fake_agent")

LEAKY_PARENT = {
    "HOME": "/home/tester",
    "USER": "tester",
    "LOGNAME": "tester",
    "LANG": "C.UTF-8",
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "XAI_API_KEY": "xai-secret",
    "HTTPS_PROXY": "http://proxy.internal:3128",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "OPENAI_BASE_URL": "https://gateway.internal/v1",
}

FAKE_PROFILE = Profile(id="fake", auth="oauth", command=AGENT_ARGV)


def child_env(tmp_path: Path) -> dict[str, str]:
    """The allowlisted environment, plus the one name the ``-m`` import needs."""
    env = build_child_env(FAKE_PROFILE, LEAKY_PARENT, task_tmp=tmp_path / "tmp")
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


@asynccontextmanager
async def running_agent(
    tmp_path: Path,
    script: dict[str, Any] | None = None,
    *,
    allow_writes: bool = False,
) -> AsyncIterator[AcpWorker]:
    env = child_env(tmp_path)
    if script is not None:
        script_path = tmp_path / "script.json"
        script_path.write_text(json.dumps(script), encoding="utf-8")
        env["TASKSPINDLE_FAKE_SCRIPT"] = str(script_path)
    worker = AcpWorker(
        command=AGENT_ARGV,
        env=env,
        cwd=tmp_path,
        stderr_path=tmp_path / "agent.err",
        policy=PermissionPolicy(allow_writes=allow_writes),
    )
    async with worker as running:
        yield running


# -- turns ---------------------------------------------------------------------------------------


async def test_prompt_returns_the_scripted_text(tmp_path: Path) -> None:
    async with running_agent(tmp_path, {"response": "hello world"}) as worker:
        assert worker.init is not None
        assert worker.init.auth_method_ids == ("cached_token",)
        assert worker.init.agent_info["name"] == "taskspindle-fake-agent"

        session_id = await worker.new_session()
        result = await worker.prompt(session_id, "go", timeout=10)

    assert session_id == "fake-session-1"
    assert result.stop_reason == "end_turn"
    assert result.text == "hello world"
    assert result.capture.raw_update_count == 2
    assert len(result.capture.text) == 2
    assert result.capture.violations == []


async def test_meta_reaches_the_agent_through_new_session(tmp_path: Path) -> None:
    meta_file = tmp_path / "meta.json"
    profile = Profile(id="claude-ish", auth="oauth", command=AGENT_ARGV, base="claude", model="opus-x")
    async with running_agent(tmp_path, {"capture_meta_to": str(meta_file)}) as worker:
        await worker.new_session(**session_options(profile))

    assert json.loads(meta_file.read_text(encoding="utf-8")) == session_options(profile)


@pytest.mark.parametrize(
    ("selected", "description", "expected"),
    [
        ("claude-opus-5", "Opus 5", "claude-opus-5"),
        ("default", "claude-opus-5", "claude-opus-5"),
        ("default", "Opus 5", None),
    ],
)
async def test_session_model_comes_from_configuration_on_create_and_load(
    tmp_path: Path, selected: str, description: str, expected: str | None
) -> None:
    script = {
        "load_session": True,
        "config_options": [{
            "id": "model", "name": "Model", "type": "select", "currentValue": selected,
            "options": [{"value": selected, "name": "Selected model", "description": description}],
        }],
    }
    async with running_agent(tmp_path, script) as worker:
        session_id = await worker.new_session()
        assert worker.session_model == expected
        worker.session_model = "stale-model"
        await worker.load_session(session_id)
        assert worker.session_model == expected


# -- permission gate -----------------------------------------------------------------------------


async def test_write_is_allowed_when_the_task_may_write(tmp_path: Path) -> None:
    script = {"response": "done", "write": {"path": "out/note.txt", "content": "written"}}
    async with running_agent(tmp_path, script, allow_writes=True) as worker:
        session_id = await worker.new_session()
        result = await worker.prompt(session_id, "go", timeout=10)

    assert (tmp_path / "out" / "note.txt").read_text(encoding="utf-8") == "written"
    assert result.capture.violations == []
    assert result.capture.permission_events[0]["option_id"] == "allow-once"
    assert result.capture.tool_calls[0]["title"] == "Write file"


async def test_write_is_refused_for_a_read_only_task(tmp_path: Path) -> None:
    script = {"response": "done", "write": {"path": "out/note.txt", "content": "written"}}
    async with running_agent(tmp_path, script, allow_writes=False) as worker:
        session_id = await worker.new_session()
        result = await worker.prompt(session_id, "go", timeout=10)

    assert not (tmp_path / "out" / "note.txt").exists()
    assert result.capture.violations == ["READ_ONLY_VIOLATION"]
    assert result.capture.permission_events[0]["option_id"] == "reject-once"


async def test_delegation_is_refused_even_when_writes_are_allowed(tmp_path: Path) -> None:
    async with running_agent(tmp_path, {"response": "done", "delegate": True}, allow_writes=True) as worker:
        session_id = await worker.new_session()
        result = await worker.prompt(session_id, "go", timeout=10)

    assert result.capture.violations == ["DELEGATION_ATTEMPT"]
    assert result.capture.permission_events[0]["option_id"] == "reject-once"
    assert result.text == "done"


#: (title, kind) pairs the delegation gate must let through: ordinary work whose name merely
#: contains one of the words the gate looks for.
ALLOWED_CALLS = [
    ("Edit src/tasks.py", "edit"),
    ("Run pytest tests/test_task.py", "execute"),
    ("Read agents.md", "read"),
    ("Write taskspindle/units.py", "edit"),
]

#: Titles that name one of the tools an agent would spawn helpers with.
DENIED_CALLS = [
    ("Agent: spawn subagent", "other"),
    ("Task(...)", "other"),
    ("TeamCreate", "other"),
    ("Delegate to a subagent", "other"),
    ("SendMessage to the reviewer", "other"),
]


def _permission_options() -> list[PermissionOption]:
    return [
        PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
        PermissionOption(option_id="reject-once", name="Reject once", kind="reject_once"),
    ]


@pytest.mark.parametrize(("title", "kind"), ALLOWED_CALLS)
def test_ordinary_work_is_not_mistaken_for_delegation(title: str, kind: str) -> None:
    policy = PermissionPolicy(allow_writes=True)

    option_id, violation = policy.select(
        ToolCallUpdate(tool_call_id="tc-1", title=title, kind=kind), _permission_options()
    )

    assert violation is None
    assert option_id == "allow-once"


@pytest.mark.parametrize(("title", "kind"), DENIED_CALLS)
def test_delegation_tools_are_denied_by_name(title: str, kind: str) -> None:
    policy = PermissionPolicy(allow_writes=True)

    option_id, violation = policy.select(
        ToolCallUpdate(tool_call_id="tc-1", title=title, kind=kind), _permission_options()
    )

    assert violation == "DELEGATION_ATTEMPT"
    assert option_id == "reject-once"


# -- cancellation and timeouts ---------------------------------------------------------------------


async def test_cancel_ends_a_blocked_turn(tmp_path: Path) -> None:
    async with running_agent(tmp_path, {"block_seconds": 30, "response": "never"}) as worker:
        session_id = await worker.new_session()
        turn = asyncio.create_task(worker.prompt(session_id, "go", timeout=30))
        await asyncio.sleep(0.2)
        await worker.cancel(session_id)
        result = await asyncio.wait_for(turn, timeout=5)

    assert result.stop_reason == "cancelled"


async def test_a_turn_that_overruns_its_budget_times_out(tmp_path: Path) -> None:
    async with running_agent(tmp_path, {"block_seconds": 30, "response": "never"}) as worker:
        session_id = await worker.new_session()
        with pytest.raises(AcpError) as excinfo:
            await worker.prompt(session_id, "go", timeout=0.5)

    assert excinfo.value.code == "TURN_TIMEOUT"


async def test_a_failing_turn_is_an_acp_turn_error(tmp_path: Path) -> None:
    async with running_agent(tmp_path, {"fail": True}) as worker:
        session_id = await worker.new_session()
        with pytest.raises(AcpError) as excinfo:
            await worker.prompt(session_id, "go", timeout=10)

    assert excinfo.value.code == "ACP_TURN_ERROR"


# -- usage and late updates -------------------------------------------------------------------------


async def test_usage_and_rate_limits_are_captured_from_the_wire(tmp_path: Path) -> None:
    script = {
        "response": "done",
        "usage": {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12, "cachedReadTokens": 5},
        "rate_limit": {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.4},
        "model_id": "fake-model-1",
    }
    async with running_agent(tmp_path, script) as worker:
        session_id = await worker.new_session()
        result = await worker.prompt(session_id, "go", timeout=10)

    assert result.usage == {
        "total_tokens": 12,
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_read_tokens": 5,
    }
    assert result.capture.rate_limits == [
        {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.4}
    ]
    assert result.capture.usage_updates[0]["size"] == 200000
    assert result.capture.model_ids == ["fake-model-1"]


async def test_a_turn_summary_sent_after_the_response_is_captured_within_the_grace(
    tmp_path: Path,
) -> None:
    script = {
        "response": "done",
        "turn_completed": {"usage": {"inputTokens": 7}},
        "late_turn_completed": True,
    }
    env = child_env(tmp_path)
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")
    env["TASKSPINDLE_FAKE_SCRIPT"] = str(script_path)

    def worker_with(grace: float) -> AcpWorker:
        return AcpWorker(
            command=AGENT_ARGV,
            env=env,
            cwd=tmp_path,
            stderr_path=tmp_path / "agent.err",
            policy=PermissionPolicy(allow_writes=False),
            late_update_grace=grace,
        )

    async with worker_with(2.0) as worker:
        session_id = await worker.new_session()
        result = await worker.prompt(session_id, "go", timeout=10)
    assert result.capture.turn_completed == {"sessionUpdate": "turn_completed", "usage": {"inputTokens": 7}}

    # Without the grace the summary lands after capture has closed, and is lost.
    async with worker_with(0.0) as worker:
        session_id = await worker.new_session()
        result = await worker.prompt(session_id, "go", timeout=10)
        await asyncio.sleep(0.5)
    assert result.capture.turn_completed is None


# -- resume --------------------------------------------------------------------------------------


async def test_load_session_discards_the_replay(tmp_path: Path) -> None:
    script = {"load_session": True, "replay_count": 3, "response": "fresh"}
    async with running_agent(tmp_path, script) as worker:
        await worker.load_session("fake-session-1")
        assert worker.replay_update_count == 3
        result = await worker.prompt("fake-session-1", "go", timeout=10)

    assert result.text == "fresh"
    assert result.capture.raw_update_count == 2


async def test_resume_is_refused_when_the_agent_cannot_load(tmp_path: Path) -> None:
    async with running_agent(tmp_path, {"load_session": False}) as worker:
        assert worker.init is not None
        assert worker.init.load_session is False
        with pytest.raises(AcpError) as excinfo:
            await worker.load_session("fake-session-1")

    assert excinfo.value.code == "RESUME_UNAVAILABLE"


# -- environment ---------------------------------------------------------------------------------


async def test_the_child_sees_exactly_the_allowlisted_environment(tmp_path: Path) -> None:
    env_file = tmp_path / "child-env.json"
    expected = child_env(tmp_path)
    script = {"capture_env_to": str(env_file), "response": "done"}

    async with running_agent(tmp_path, script) as worker:
        session_id = await worker.new_session()
        await worker.prompt(session_id, "go", timeout=10)

    captured = json.loads(env_file.read_text(encoding="utf-8"))
    script_path = str(tmp_path / "script.json")

    assert captured == sealed_env({**expected, "TASKSPINDLE_FAKE_SCRIPT": script_path})
    assert env_violations(captured) == []
    assert "sk-ant-secret" not in json.dumps(captured)
    assert captured["TERM"] == "dumb"
    assert captured["TMPDIR"] == str(tmp_path / "tmp")


async def test_spawning_a_missing_binary_fails_cleanly(tmp_path: Path) -> None:
    worker = AcpWorker(
        command=(str(tmp_path / "no-such-agent"),),
        env=child_env(tmp_path),
        cwd=tmp_path,
        stderr_path=tmp_path / "agent.err",
        policy=PermissionPolicy(allow_writes=False),
    )

    with pytest.raises(AcpError) as excinfo:
        async with worker:
            pass

    assert excinfo.value.code == "ACP_SPAWN_FAILED"
