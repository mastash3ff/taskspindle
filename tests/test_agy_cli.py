"""Native AGY transport checks against disposable Python processes, never a Google seat."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from taskspindle import agy_cli
from taskspindle.acp_client import DELEGATION_ATTEMPT, MODE_SWITCH_ATTEMPT, READ_ONLY_VIOLATION, AcpError
from taskspindle.agy_cli import AgyCliWorker, model_catalog

SID = "conversation-original"
MODEL = "gemini-3.8-flash-medium"
COUNTS = {
    "input_tokens": 16414,
    "output_tokens": 52,
    "thinking_tokens": 42,
    "cache_read_tokens": 0,
    "total_tokens": 16466,
}
NEXT_COUNTS = {
    "input_tokens": 20900,
    "output_tokens": 126,
    "thinking_tokens": 106,
    "cache_read_tokens": 12203,
    "total_tokens": 21026,
}

FAKE = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text())
if sys.argv[-1] == "models":
    print(config.get("catalog", ""), end="", flush=True)
else:
    raw = sys.stdin.read()
    Path(config["input_record"]).write_text(json.dumps({
        "args": sys.argv[2:], "input": raw, "cwd": os.getcwd(),
        "ambient": os.environ.get("TASKSPINDLE_FAKE_AMBIENT"),
    }))
    if config.get("ignore_signals"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    for action in config.get("actions", []):
        if "sleep" in action:
            time.sleep(action["sleep"])
        elif "child" in action:
            ready = Path(action["child"] + ".ready")
            code = ("import signal,sys,time; from pathlib import Path; signal.signal(2,signal.SIG_IGN); "
                    "signal.signal(15,signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)")
            child = subprocess.Popen([sys.executable, "-c", code, str(ready)])
            while not ready.exists():
                time.sleep(0.001)
            Path(action["child"]).write_text(str(child.pid))
        elif "raw" in action:
            print(action["raw"], end="", flush=True)
        else:
            print(json.dumps(action), flush=True)
print(config.get("stderr", ""), file=sys.stderr, end="", flush=True)
time.sleep(config.get("exit_delay", 0))
sys.exit(config.get("exit_code", 0))
"""


def initial(sid=SID):
    return {
        "event": "init",
        "conversation_id": sid,
        "init": {"model": MODEL, "permission_mode": "request-review", "tools": ["read_file"]},
    }


def step(text="partial", *, sid=SID, index=1, **extra):
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": sid,
            "step_index": index,
            "state": "DONE",
            "step_type": "agent_response",
            "text_delta": text,
            **extra,
        },
    }


def terminal(response="partial", *, sid=SID, status="SUCCESS", **extra):
    return {
        "event": "result",
        "result": {
            "conversation_id": sid,
            "status": status,
            "response": response,
            **extra,
        },
    }


def fake(tmp_path, **config):
    script, scenario, record = tmp_path / "fake.py", tmp_path / "scenario.json", tmp_path / "input.json"
    script.write_text(FAKE)
    scenario.write_text(json.dumps({"input_record": str(record), **config}))
    return (sys.executable, str(script), str(scenario))


def worker(tmp_path, *, prior_usage=None, on_session=None, mode=None, **config):
    return AgyCliWorker(
        fake(tmp_path, **config),
        {},
        tmp_path,
        tmp_path / "stderr.log",
        on_session,
        prior_usage=prior_usage,
        mode=mode,
    )


async def prompt(agent, sid=None, timeout=3):
    return await agent.prompt(sid, "hello\nworld", model=MODEL, effort="medium", timeout=timeout)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_catalog_exact_command_and_advertised_names(tmp_path):
    command = fake(tmp_path, catalog=f"{MODEL}\tGemini 3.8 Flash (Medium)\nclaude-x\tClaude X\n")
    assert await model_catalog(command, {}, tmp_path) == [
        (MODEL, "Gemini 3.8 Flash (Medium)"),
        ("claude-x", "Claude X"),
    ]
    assert not (tmp_path / "input.json").exists()


@pytest.mark.parametrize(
    "catalog", ["", "garbage\n", "claude-x\tClaude\n", f"{MODEL}\tName\n{MODEL}\tName\n"]
)
async def test_catalog_rejects_malformed_or_duplicate_rows(tmp_path, catalog):
    with pytest.raises(AcpError, match="invalid model catalog"):
        await model_catalog(fake(tmp_path, catalog=catalog), {}, tmp_path)


async def test_catalog_auth_failure_is_actionable_without_diagnostic_leak(tmp_path):
    command = fake(tmp_path, exit_code=1, stderr="Please sign in to view available models. token=SECRET")
    with pytest.raises(AcpError) as caught:
        await model_catalog(command, {}, tmp_path)
    assert caught.value.code == "PROVIDER_AUTH_EXPIRED"
    assert "taskspindle auth agy" in str(caught.value)
    assert "SECRET" not in str(caught.value) + str(caught.value.cause)


async def test_catalog_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_cli, "_OUTPUT_LIMIT", 32)
    with pytest.raises(AcpError, match="size bound"):
        await model_catalog(fake(tmp_path, catalog="x" * 200), {}, tmp_path)
    with pytest.raises(AcpError) as caught:
        await model_catalog(fake(tmp_path, exit_delay=30), {}, tmp_path, timeout=0.1)
    assert caught.value.code == "TURN_TIMEOUT"


async def test_stream_protocol_eof_exact_flags_and_selected_model_not_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("TASKSPINDLE_FAKE_AMBIENT", "must not inherit")
    sessions = []

    async def observed(sid):
        sessions.append(sid)

    agent = worker(
        tmp_path,
        on_session=observed,
        actions=[
            initial(),
            step("hel"),
            step("lo", index=2),
            terminal("hello", usage=COUNTS),
        ],
    )
    async with agent:
        result = await prompt(agent)
    assert sessions == [SID]
    assert agent.session_id == SID
    assert result.text == "hello" and result.stop_reason == "end_turn"
    assert result.capture.text == ["hel", "lo"]
    assert result.capture.raw_update_count == 4
    assert result.capture.model_ids == []
    assert agent.last_result == result
    assert agent.cumulative_usage == COUNTS
    assert result.usage == {
        "input_tokens": 16414,
        "output_tokens": 52,
        "thought_tokens": 42,
        "cached_read_tokens": 0,
        "total_tokens": 16466,
        "_agy_cli_cumulative": COUNTS,
    }
    record = json.loads((tmp_path / "input.json").read_text())
    assert record["input"] == '{"event": "user", "message": {"content": "hello\\nworld"}}\n'
    assert record["ambient"] is None
    assert record["cwd"] == str(tmp_path)
    assert record["args"] == [
        "--model",
        MODEL,
        "--effort",
        "medium",
        "--print-timeout",
        "3s",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
    ]


async def test_session_is_captured_before_turn_finishes_and_sync_callback_works(tmp_path):
    sessions = []
    agent = worker(tmp_path, on_session=sessions.append, actions=[initial(), {"sleep": 0.3}, terminal()])
    turn = asyncio.create_task(prompt(agent))
    await until(lambda: bool(sessions))
    assert sessions == [SID] and not turn.done()
    await turn


async def test_result_can_supply_first_real_session(tmp_path):
    sessions = []
    agent = worker(tmp_path, on_session=sessions.append, actions=[terminal("complete")])
    assert (await prompt(agent)).text == "complete"
    assert sessions == [SID]


async def test_continuation_uses_exact_id_and_only_usage_deltas(tmp_path):
    agent = worker(tmp_path, prior_usage=COUNTS, actions=[initial(), terminal(usage=NEXT_COUNTS)])
    result = await prompt(agent, SID)
    args = json.loads((tmp_path / "input.json").read_text())["args"]
    assert args[args.index("--conversation") + 1] == SID
    assert "--continue" not in args
    assert result.usage == {
        "input_tokens": 4486,
        "output_tokens": 74,
        "thought_tokens": 64,
        "cached_read_tokens": 12203,
        "total_tokens": 4560,
        "_agy_cli_cumulative": NEXT_COUNTS,
    }


@pytest.mark.parametrize("baseline", [None, {"input_tokens": 20900}])
async def test_missing_baseline_never_recounts_historical_totals(tmp_path, baseline):
    agent = worker(tmp_path, prior_usage=baseline, actions=[initial(), terminal(usage=NEXT_COUNTS)])
    result = await prompt(agent, SID)
    assert result.usage == {"_agy_cli_cumulative": NEXT_COUNTS, **({"input_tokens": 0} if baseline else {})}


async def test_replayed_usage_counts_zero_and_regressed_counts_fail(tmp_path):
    agent = worker(tmp_path, prior_usage=COUNTS, actions=[initial(), terminal(usage=COUNTS)])
    result = await prompt(agent, SID)
    assert result.usage["input_tokens"] == result.usage["output_tokens"] == 0
    agent = worker(tmp_path, prior_usage=NEXT_COUNTS, actions=[initial(), step(), terminal(usage=COUNTS)])
    with pytest.raises(AcpError, match="decreased"):
        await prompt(agent, SID)
    assert agent.last_result.text == "partial"


@pytest.mark.parametrize(
    "bad_usage", [{"input_tokens": -1}, {"output_tokens": True}, {"thinking_tokens": 1.5}, [1, 2]]
)
async def test_invalid_usage_is_rejected_without_losing_partial_text(tmp_path, bad_usage):
    agent = worker(tmp_path, actions=[initial(), step(), terminal(usage=bad_usage)])
    with pytest.raises(AcpError):
        await prompt(agent)
    assert agent.last_result.text == "partial"


@pytest.mark.parametrize(
    "events",
    [
        [initial("replacement")],
        [initial(), step(), terminal(sid="replacement")],
        [initial(), step(sid="replacement")],
    ],
)
async def test_resume_mismatch_preserves_original_id_and_never_restarts(tmp_path, events):
    seen = []
    agent = worker(tmp_path, on_session=seen.append, actions=events)
    with pytest.raises(AcpError) as caught:
        await prompt(agent, SID)
    assert caught.value.code == "RESUME_UNAVAILABLE"
    assert agent.session_id == SID
    assert "replacement" not in seen
    assert agent.last_result.stop_reason == "error"


@pytest.mark.parametrize(
    "actions",
    [
        [{"raw": "not-json\n"}],
        [{"raw": "[]\n"}],
        [{"raw": '{"event":"x","event":"y"}\n'}],
        [{"raw": '{"event":"init","bad":NaN}\n'}],
        [{"raw": '{"event":"init","bad":1e999}\n'}],
        [{"raw": "{}"}],
        [{"event": "unknown"}],
        [initial(), initial()],
        [step()],
        [terminal(status="FUTURE_STATUS")],
        [terminal(sid="")],
        [terminal(error="failure")],
        [initial(), terminal(), terminal()],
        [initial()],
        [initial(), step(text=3)],
        [initial(), terminal(response=3)],
    ],
)
async def test_malformed_streams_fail_closed(tmp_path, actions):
    agent = worker(tmp_path, actions=actions)
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "AGY_CLI_PROTOCOL_ERROR"
    assert agent.last_result is not None


@pytest.mark.parametrize(
    "bound,value,actions",
    [
        ("_LINE_LIMIT", 64, [{"raw": "x" * 100 + "\n"}]),
        ("_OUTPUT_LIMIT", 10, [initial()]),
        ("_EVENT_LIMIT", 1, [initial(), terminal()]),
    ],
)
async def test_ndjson_limits(tmp_path, monkeypatch, bound, value, actions):
    monkeypatch.setattr(agy_cli, bound, value)
    agent = worker(tmp_path, actions=actions)
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "AGY_CLI_PROTOCOL_ERROR"


@pytest.mark.parametrize(
    "status,exit_code,code",
    [
        ("SUCCESS", 7, "AGY_CLI_ERROR"),
        ("ERROR", 1, "AGY_CLI_ERROR"),
        ("INVALID", 0, "AGY_CLI_ERROR"),
        ("WAITING", 0, "AGY_CLI_ERROR"),
        ("RUNNING", 0, "AGY_CLI_ERROR"),
    ],
)
async def test_status_and_actual_process_exit_both_checked(tmp_path, status, exit_code, code):
    agent = worker(tmp_path, actions=[initial(), terminal(status=status)], exit_code=exit_code)
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == code
    assert caught.value.cause == {"native_status": status, "exit_code": exit_code}


@pytest.mark.parametrize("status", ["CANCELED", "INTERRUPTED"])
async def test_native_cancellation_status_retains_partial(tmp_path, status):
    agent = worker(tmp_path, actions=[initial(), step(), terminal(status=status)], exit_code=1)
    result = await prompt(agent)
    assert result.stop_reason == "cancelled" and result.text == "partial"


@pytest.mark.parametrize(
    "message,code",
    [
        ("Please sign in to continue", "PROVIDER_AUTH_EXPIRED"),
        ("Rate limit exceeded", "PROVIDER_THROTTLED"),
        ("Unknown model selected", "MODEL_UNAVAILABLE"),
        ("Conversation not found", "RESUME_UNAVAILABLE"),
    ],
)
async def test_early_errors_are_classified_without_a_new_session_or_raw_diagnostics(tmp_path, message, code):
    agent = worker(
        tmp_path,
        actions=[terminal("", sid="", status="ERROR", error=message + " secret=TOKEN")],
        stderr="backend=SECRET",
        exit_code=1,
    )
    with pytest.raises(AcpError) as caught:
        await prompt(agent, SID)
    assert caught.value.code == code
    assert agent.session_id == SID
    assert not any(secret in str(caught.value) + str(caught.value.cause) for secret in ("SECRET", "TOKEN"))
    assert "SECRET" not in (tmp_path / "stderr.log").read_text()


async def test_tool_events_and_intermediate_messages_survive_final_answer(tmp_path):
    info = {"name": "read_file", "parameters": {"path": "README.md"}, "output": "contents"}
    agent = worker(
        tmp_path,
        actions=[
            initial(),
            step("Inspecting the file.\n"),
            step("", index=2, step_type="tool", tool_name="read_file", tool_info=info),
            step("The answer.", index=3),
            terminal("The answer."),
        ],
    )
    result = await prompt(agent)
    assert result.text == "The answer."
    assert result.capture.text == ["Inspecting the file.\n", "The answer."]
    assert result.capture.tool_calls[0]["tool_info"] == info


async def test_observed_delegation_is_recorded(tmp_path):
    agent = worker(
        tmp_path,
        actions=[
            initial(),
            step(
                "",
                step_type="tool",
                tool_name="start_subagent",
                subagent_info={"subagents": [{"conversation_id": "child"}]},
            ),
            terminal(),
        ],
    )
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "POLICY_VIOLATION"
    assert agent.last_result.capture.violations == [DELEGATION_ATTEMPT]


@pytest.mark.parametrize("mode", ["consult", "review", "implement"])
@pytest.mark.parametrize("tool_name", ["invoke_subagent", "define_subagent", "delegate", "manage_task"])
async def test_delegation_guard_aborts_every_worker_mode_immediately(tmp_path, mode, tool_name):
    agent = worker(
        tmp_path,
        mode=mode,
        actions=[initial(), step(), step("", step_type="tool", tool_name=tool_name), {"sleep": 30}],
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert asyncio.get_running_loop().time() - started < 2
    assert caught.value.code == "POLICY_VIOLATION"
    assert agent.last_result.text == "partial" and agent.session_id == SID
    assert agent.last_result.capture.violations == [DELEGATION_ATTEMPT]
    assert agent.last_result.capture.tool_calls[0]["title"] == tool_name
    assert agent.last_result.capture.permission_events[0]["action"] == "abort"


async def test_any_subagent_metadata_aborts_even_on_non_tool_steps(tmp_path):
    agent = worker(tmp_path, mode="implement", actions=[initial(), step(subagent_info={})])
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "POLICY_VIOLATION"
    assert agent.last_result.capture.violations == [DELEGATION_ATTEMPT]


@pytest.mark.parametrize(
    "mode,tool_name,violation",
    [
        ("consult", "write_to_file", READ_ONLY_VIOLATION),
        ("review", "replace_file_content", READ_ONLY_VIOLATION),
        ("review", "run_command", READ_ONLY_VIOLATION),
        ("consult", "switch_mode", MODE_SWITCH_ATTEMPT),
        ("implement", "set_mode", MODE_SWITCH_ATTEMPT),
        ("implement", "run_command", "POLICY_VIOLATION"),
        ("implement", "unknown_tool", "POLICY_VIOLATION"),
        ("review", "unknown_tool", "POLICY_VIOLATION"),
        ("consult", "mcp__read", "POLICY_VIOLATION"),
    ],
)
async def test_observed_tools_outside_fixed_policy_abort(tmp_path, mode, tool_name, violation):
    agent = worker(
        tmp_path,
        mode=mode,
        actions=[initial(), step(), step("", step_type="tool", tool_name=tool_name), terminal()],
    )
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "POLICY_VIOLATION"
    assert agent.last_result.capture.violations == [violation]
    assert agent.last_result.capture.permission_events[0]["violation"] == violation
    assert agent.last_result.capture.raw_update_count == 3  # Stop before processing a queued success.


@pytest.mark.parametrize(
    "mode,tool_name",
    [
        ("consult", "view_file"),
        ("review", "grep_search"),
        ("review", "find_by_name"),
        ("implement", "list_dir"),
        ("implement", "write_to_file"),
        ("implement", "replace_file_content"),
        ("implement", "multi_replace_file_content"),
    ],
)
async def test_fixed_tool_policy_allows_declared_tools_without_filtering_init_inventory(
    tmp_path, mode, tool_name
):
    init = initial()
    init["init"]["tools"] = ["run_command", "invoke_subagent", "mcp_tool"]
    agent = worker(
        tmp_path, mode=mode, actions=[init, step("", step_type="tool", tool_name=tool_name), terminal()]
    )
    result = await prompt(agent)
    assert result.stop_reason == "end_turn"
    assert not result.capture.violations


async def test_timeout_keeps_partial_response_and_session(tmp_path):
    agent = worker(tmp_path, actions=[initial(), step(), {"sleep": 30}])
    with pytest.raises(AcpError) as caught:
        await prompt(agent, timeout=0.15)
    assert caught.value.code == "TURN_TIMEOUT"
    assert agent.last_result.stop_reason == "timeout"
    assert agent.last_result.text == "partial" and agent.session_id == SID


async def test_timeout_after_terminal_result_still_requires_process_exit(tmp_path):
    agent = worker(tmp_path, actions=[initial(), terminal(usage=COUNTS)], exit_delay=30)
    with pytest.raises(AcpError) as caught:
        await prompt(agent, timeout=0.15)
    assert caught.value.code == "TURN_TIMEOUT"
    assert agent.last_result.stop_reason == "timeout"
    assert agent.last_result.text == "partial"
    assert agent.last_result.usage["_agy_cli_cumulative"] == COUNTS


async def test_auth_failure_without_ndjson_does_not_create_a_session(tmp_path):
    agent = worker(tmp_path, stderr="Please sign in to view available models.", exit_code=1)
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "PROVIDER_AUTH_EXPIRED"
    assert agent.session_id is None
    assert agent.last_result.capture.raw_update_count == 0


async def test_cancel_stops_stubborn_descendants_and_keeps_partial(tmp_path):
    child_file = tmp_path / "child.pid"
    agent = worker(
        tmp_path,
        ignore_signals=True,
        actions=[
            initial(),
            step(),
            step("", step_type="tool", tool_name="read_file"),
            {"child": str(child_file)},
            {"sleep": 30},
        ],
    )
    turn = asyncio.create_task(prompt(agent, timeout=10))
    await until(lambda: child_file.exists() and bool(agent._capture.text))
    await agent.cancel(SID)
    result = await asyncio.wait_for(turn, 2)
    assert result.stop_reason == "cancelled" and result.text == "partial"
    assert result.capture.tool_calls[0]["title"] == "read_file"
    child_pid = int(child_file.read_text())
    stat = Path(f"/proc/{child_pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


async def test_descendants_are_stopped_even_after_the_leader_exits(tmp_path):
    child_file = tmp_path / "child.pid"
    agent = worker(tmp_path, actions=[initial(), step(), {"child": str(child_file)}, terminal()])
    with pytest.raises(AcpError) as caught:
        await prompt(agent, timeout=0.15)
    assert caught.value.code == "TURN_TIMEOUT"
    child_pid = int(child_file.read_text())
    stat = Path(f"/proc/{child_pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
    assert agent.last_result.text == "partial"


async def test_python_task_cancellation_retains_partial_and_reaps_process(tmp_path):
    agent = worker(tmp_path, actions=[initial(), step(), {"sleep": 30}])
    turn = asyncio.create_task(prompt(agent, timeout=10))
    await until(lambda: bool(agent._capture.text))
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert agent.last_result.stop_reason == "cancelled"
    assert agent.last_result.text == "partial"
    assert agent._process.returncode is not None


async def test_missing_executable_is_an_acp_error_with_partial_capture(tmp_path):
    agent = AgyCliWorker((str(tmp_path / "missing"),), {}, tmp_path, tmp_path / "stderr")
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "ACP_SPAWN_FAILED"
    assert agent.last_result.text == ""


async def test_session_callback_failure_preserves_identifier_and_normalizes_error(tmp_path):
    async def callback(sid):
        raise RuntimeError("private database error payload")

    agent = worker(tmp_path, on_session=callback, actions=[initial(), step(), terminal()])
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "AGY_CLI_ERROR"
    assert "private" not in str(caught.value) + str(caught.value.cause)
    assert agent.last_result is not None and agent.session_id == SID


async def test_diagnostic_write_failure_keeps_partial_result(tmp_path, monkeypatch):
    agent = worker(tmp_path, actions=[initial(), step(), terminal()])
    original = Path.write_text

    def denied(path, *args, **kwargs):
        if path == agent.stderr_path:
            raise PermissionError("diagnostic path is not writable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", denied)
    result = await prompt(agent)
    assert result.stop_reason == "end_turn" and result.text == "partial"
    assert agent.last_result == result and agent.session_id == SID
    assert result.capture.violations == ["AGY_DIAGNOSTIC_WRITE_FAILED"]


async def test_teardown_confirmation_failure_keeps_result(tmp_path, monkeypatch):
    agent = worker(tmp_path, actions=[initial(), step(), terminal()])
    monkeypatch.setattr(agy_cli, "_group_running", lambda pgid: True)
    with pytest.raises(AcpError) as caught:
        await prompt(agent)
    assert caught.value.code == "AGY_CLI_STOP_FAILED"
    assert agent.last_result.text == "partial" and agent.session_id == SID


async def test_prompt_without_effort_and_single_turn_guard(tmp_path):
    agent = worker(tmp_path, actions=[initial(), terminal()])
    await agent.prompt(None, "one", model=MODEL, effort=None, timeout=3)
    assert "--effort" not in json.loads((tmp_path / "input.json").read_text())["args"]
    with pytest.raises(AcpError, match="only one turn"):
        await prompt(agent)
