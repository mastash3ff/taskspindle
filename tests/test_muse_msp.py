"""MSP lifecycle tests against the schema exported from Muse R3401.1.

The subprocess fixture validates every client request against that pinned export.
It never starts Muse, accesses credentials, or makes network/model calls.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from taskspindle.acp_client import AcpError
from taskspindle.muse_msp import MSP_FINGERPRINT, MuseWorker, uuid7

SCHEMA = Path(__file__).parent / "fixtures/muse/msp.schema.json"
FAKE = r'''
import json, os, subprocess, sys
from jsonschema import Draft202012Validator
from jsonschema.validators import extend

def open_enum(validator, values, instance, node):
    if node.get("x-msp-openness") != "open":
        yield from Draft202012Validator.VALIDATORS["enum"](validator, values, instance, node)

MSPValidator = extend(Draft202012Validator, {"enum": open_enum})
schema = json.load(open(sys.argv[1]))
scenario, log = sys.argv[2:]
session_id = None
turn_id = None
cursor = 0

def validate(value, ref):
    MSPValidator({"$ref": ref, "$defs": schema["$defs"]}).validate(value)

def send(value):
    if value.get("method") in schema["requests"]:
        ref = schema["requests"][value["method"]]["params"]["$ref"]
        if scenario != "unknown_approval": validate(value["params"], ref)
    print(json.dumps(value), flush=True)

def reply(message, result):
    validate(result, schema["methods"][message["method"]]["result"]["$ref"])
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})

def event(method, params):
    global cursor
    if method != "turn/completed":
        cursor += 1
    p = {"sessionId": session_id, "viewCursor": str(cursor), **params}
    if method != "item/delta":
        p["sourceRange"] = {"first": {"id": "r", "sequence": cursor},
                            "last": {"id": "r", "sequence": cursor}, "stream": "session"}
    # StreamRef is an object; use its exact required fields from the schema.
    if "sourceRange" in p:
        p["sourceRange"]["stream"] = {"kind": "session", "id": session_id}
    validate(p, schema["notifications"][method]["params"]["$ref"])
    send({"jsonrpc": "2.0", "method": method, "params": p})

def complete(terminal="completed"):
    event("session/statusChanged", {"status": "idle"})
    # Observed R3401.1 sends these two distinct facts with the same cursor.
    p = {"turnId": turn_id, "terminal": terminal,
         "usage": {"inputTokens": 10, "outputTokens": 3, "cachedTokens": 4, "reasoningTokens": 0}}
    if terminal == "failed":
        kind = scenario.removeprefix("failure_") if scenario.startswith("failure_") else "authRequired"
        p["error"] = {"kind": kind, "message": "quota rate limit model unavailable token=secret-token",
                      "retryable": False}
    event("turn/completed", p)

def output():
    item = {"itemId": "answer", "kind": "agentMessage", "status": "inProgress",
            "revision": 1, "turnId": turn_id, "text": ""}
    event("item/started", {"item": item})
    event("item/delta", {"itemId": "answer", "delta": "hello "})
    item.update(status="completed", revision=2, text="hello world")
    event("item/completed", {"item": item})
    usage = {"inputTokens": 10, "outputTokens": 3, "cachedTokens": 4, "reasoningTokens": 0}
    event("session/tokenUsage", {"turnId": "historic", "usage": usage,
        "promptTokens": 1000, "totalTokens": 1003,
        "cumulative": {"promptTokens": 1000, "outputTokens": 3, "totalTokens": 1003}})
    event("session/tokenUsage", {"turnId": turn_id, "usage": usage, "modelId": "actual-model",
        "promptTokens": 14, "totalTokens": 17,
        "cumulative": {"promptTokens": 1014, "outputTokens": 6, "totalTokens": 1020}})
    complete()

for line in sys.stdin:
    m = json.loads(line)
    with open(log, "a") as f:
        f.write(json.dumps(m) + "\n")
    if "method" not in m:
        assert m["result"] == {}
        continue
    method, p = m["method"], m.get("params", {})
    if method == "initialized":
        continue
    validate(p, schema["methods"][method]["params"]["$ref"])
    if method == "initialize":
        assert p["capabilities"]["userInputDialogs"] is False
        result = {"experimentalApi": False, "grantedCapabilities": [], "museHome": "/fixture",
                  "platformFamily": "unix", "platformOs": "linux", "userAgent": "fixture",
                  "serverInfo": {"name": "muse", "version": "1.3.0"},
                  "schema": {"version": 1, "fingerprint":
                      "sha256:7469c9e352e67def4a59df7e439984d7194fa351e1c8b7abb34060fd977ced81"},
                  "sessionDurability": "durable"}
        if scenario == "fingerprint": result["schema"]["fingerprint"] = "sha256:" + "0" * 64
        if scenario == "version": result["serverInfo"]["version"] = "1.4.0"
        if scenario == "envelope": result["schema"]["version"] = 2
        if scenario == "ephemeral": result["sessionDurability"] = "ephemeral"
        reply(m, result)
    elif method in ("session/start", "session/resume"):
        session_id = p["sessionId"]
        if scenario == "refuse_resume":
            send({"jsonrpc": "2.0", "id": m["id"],
                  "error": {"code": -32000, "message": "token=do-not-persist"}})
            continue
        session = {"sessionId": session_id, "path": "/fixture/session.jsonl", "status": "idle",
                   "activeTurnId": None, "modelId": p.get("modelId", "requested-model"),
                   "approvalMode": {"mode": "denyUnmatched", "source": "startup", "lastCommandId": None},
                   "providerId": None, "workspaceRoot": os.getcwd(), "forkedFrom": None,
                   "turnCount": 0, "createdAt": "2026-09-19T00:00:00Z", "updatedAt": "2026-09-19T00:00:00Z"}
        if scenario == "wrong_session": session["sessionId"] = "substituted"
        if scenario == "active": session.update(status="running", activeTurnId="old-turn")
        if scenario == "empty_path": session["path"] = ""
        result = {"session": session, "viewCursor": "0"}
        if method == "session/resume":
            assert p["excludeItems"] is True
            result.update(history={"mode": "none", "noneReason": "excluded", "items": None, "snapshot": None},
                          pendingRequests=[])
        reply(m, result)
    elif method == "turn/start":
        turn_id = p["commandId"]
        if scenario in ("rejected", "internal_error"):
            code = -32030 if scenario == "rejected" else -32603
            send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": code, "message": "refused",
                  "data": {"kind": "commandRejected" if scenario == "rejected" else "internal",
                           "commandId": turn_id, "reason": "fixture"}}})
            continue
        if scenario == "lost_ack": sys.exit(0)
        if scenario == "malformed": print("not json", flush=True); continue
        if scenario == "oversized": print("x" * (1024 * 1024 + 1), flush=True); continue
        if scenario == "before_ack": output()
        reply(m, {"commandId": p["commandId"], "status": "accepted", "turnId": turn_id,
                  "disposition": "started", "startedNewTurn": True})
        if scenario == "after_ack": sys.exit(0)
        if scenario == "descendant":
            child = subprocess.Popen([sys.executable, "-c",
                "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])
            with open(log + ".child", "w") as f: f.write(str(child.pid))
            sys.exit(0)
        if scenario == "failed" or scenario.startswith("failure_"): complete("failed"); continue
        if scenario in ("hang", "ignore_interrupt", "before_ack"): continue
        if scenario in ("approval", "unknown_approval"):
            choices = [{"choiceId": "reject", "decision": "denied", "label": "Deny", "scope": "once"}]
            if scenario == "unknown_approval": choices[0]["decision"] = "futureApproval"
            send({"jsonrpc": "2.0", "id": "approval-receipt", "method": "approval/request", "params": {
                "sessionId": session_id, "approvalId": "approval", "turnId": turn_id,
                "currentRequirementId": {"approvalId": "approval", "sourceIndex": 3},
                "availableChoices": choices, "itemId": "tool", "judgeEscalated": False,
                "protectedWrite": False, "rawArgs": "{}", "subject": {"kind": "futureTool"},
                "taskId": "task", "toolCallId": "call", "toolName": "shell", "viewCursor": "approval",
                "sourceRange": {"first": {"id": "r", "sequence": 1},
                    "last": {"id": "r", "sequence": 1}, "stream": {"kind": "session", "id": session_id}}}})
            continue
        if scenario == "user_input":
            send({"jsonrpc": "2.0", "id": "input-receipt", "method": "userInput/request",
                  "params": {"sessionId": session_id, "userInputId": "question", "itemId": "tool",
                    "questions": [], "toolCallId": "call", "toolName": "ask", "turnId": turn_id,
                    "viewCursor": "question"}})
            continue
        output()
    elif method in ("approval/decide", "userInput/cancel"):
        # Admission result is tested through the exported result shape as well.
        result = {"commandId": p["commandId"], "status": "accepted"}
        if method == "approval/decide": result.update(approvalId=p["approvalId"], terminal=True)
        else: result["userInputId"] = p["userInputId"]
        reply(m, result)
        output()
    elif method == "turn/interrupt":
        assert "retract" not in p
        if scenario == "ignore_interrupt": continue
        reply(m, {"commandId": p["commandId"], "status": "accepted", "turnId": turn_id})
        complete("cancelled")
'''


def make_worker(tmp_path: Path, scenario: str = "normal", **kwargs) -> MuseWorker:
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    return MuseWorker(command=(sys.executable, str(script), str(SCHEMA), scenario, str(tmp_path / "wire")),
                      env={"PATH": os.defpath, "HOME": str(tmp_path)}, cwd=tmp_path,
                      stderr_path=tmp_path / "stderr", model="requested-model", effort="high",
                      handshake_timeout=3, **kwargs)


def messages(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "wire").read_text().splitlines()]


def test_uuid7():
    values = [uuid.UUID(uuid7()) for _ in range(100)]
    assert all(value.version == 7 and value.variant == uuid.RFC_4122 for value in values)
    assert len(set(values)) == 100


@pytest.mark.parametrize("scenario", ["fingerprint", "version", "envelope", "ephemeral"])
async def test_rejects_incompatible_host(tmp_path, scenario):
    worker = make_worker(tmp_path, scenario)
    with pytest.raises(AcpError) as error:
        async with worker:
            pytest.fail("handshake unexpectedly succeeded")
    assert error.value.code == "MSP_HANDSHAKE_FAILED"
    assert worker._process.returncode is not None
    assert all(m.get("method") != "session/start" for m in messages(tmp_path))


@pytest.mark.parametrize("scenario", ["normal", "before_ack"])
async def test_session_prompt_capture_and_persistence_order(tmp_path, scenario):
    handles = []

    async def session_callback(value):
        assert not any(m.get("method") == "session/start" for m in messages(tmp_path))
        handles.append(value)

    def command_callback(value):
        assert not any(m.get("method") == "turn/start" for m in messages(tmp_path))
        handles.append(value)

    async with make_worker(tmp_path, scenario, on_session=session_callback, on_command=command_callback) as w:
        sid = await w.new_session()
        result = await w.prompt(sid, "hello", timeout=3)
        assert result.text == "hello world"
        assert result.stop_reason == "end_turn"
        assert {k: result.usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")} == {
            "input_tokens": 14, "output_tokens": 3, "total_tokens": 17}
        assert result.capture.turn_completed is None
        assert result.usage["_muse_msp"] is True
        assert result.capture.model_ids == ["actual-model"]
        assert w.model == w.session_model == "requested-model"
        assert w.session_effort is None
        assert handles == [sid, w.command_id]
        assert w.last_result is result
        assert w.command_settled is True
        assert w.init.agent_info["schema"]["fingerprint"] == MSP_FINGERPRINT
    assert len([m for m in messages(tmp_path) if m.get("method") == "turn/start"]) == 1


async def test_resume_exact_session_no_history_usage(tmp_path):
    sid = uuid7()
    async with make_worker(tmp_path) as w:
        await w.load_session(sid)
        assert w.last_result is None
        result = await w.prompt(sid, "continue", timeout=3)
        assert result.usage["total_tokens"] == 17
    requests = messages(tmp_path)
    assert next(m for m in requests if m.get("method") == "session/resume")["params"]["sessionId"] == sid
    assert not any(m.get("method") == "session/start" for m in requests)


@pytest.mark.parametrize("scenario,code", [("refuse_resume", "RESUME_UNAVAILABLE"),
    ("wrong_session", "RESUME_UNAVAILABLE"), ("empty_path", "RESUME_UNAVAILABLE"),
    ("active", "RECOVERY_AMBIGUOUS")])
async def test_resume_never_substitutes_new_conversation(tmp_path, scenario, code):
    async with make_worker(tmp_path, scenario) as w:
        with pytest.raises(AcpError) as error:
            await w.load_session(uuid7())
        assert error.value.code == code
    assert not any(m.get("method") == "session/start" for m in messages(tmp_path))
    assert "do-not-persist" not in str(error.value)


@pytest.mark.parametrize("scenario", ["lost_ack", "after_ack", "malformed", "oversized", "unknown_approval"])
async def test_uncertain_submission_never_resends(tmp_path, scenario):
    async with make_worker(tmp_path, scenario) as w:
        sid = await w.new_session()
        with pytest.raises(AcpError) as error:
            await w.prompt(sid, "run", timeout=3)
        assert error.value.code == "RECOVERY_AMBIGUOUS"
        assert w.command_settled is False
        assert w.last_result is not None
        assert w._process.returncode is not None
    assert len([m for m in messages(tmp_path) if m.get("method") == "turn/start"]) == 1


@pytest.mark.parametrize("scenario,receipt,decision", [
    ("approval", "approval-receipt", "approval/decide"),
    ("user_input", "input-receipt", "userInput/cancel"),
])
async def test_interactive_receipt_is_separate_from_denial(tmp_path, scenario, receipt, decision):
    async with make_worker(tmp_path, scenario) as w:
        result = await w.prompt(await w.new_session(), "run", timeout=3)
        assert result.text == "hello world"
        if scenario == "approval":
            assert result.capture.violations == ["MUSE_APPROVAL_DENIED"]
    wire = messages(tmp_path)
    receipt_index = next(i for i, m in enumerate(wire) if m.get("id") == receipt)
    decision_index = next(i for i, m in enumerate(wire) if m.get("method") == decision)
    assert wire[receipt_index]["result"] == {}
    assert receipt_index < decision_index
    if scenario == "approval":
        params = wire[decision_index]["params"]
        assert params["choiceId"] == "reject"
        assert params["requirementId"] == {"approvalId": "approval", "sourceIndex": 3}


async def test_timeout_interrupts_exact_turn_and_waits_terminal(tmp_path):
    async with make_worker(tmp_path, "hang") as w:
        with pytest.raises(AcpError) as error:
            await w.prompt(await w.new_session(), "hang", timeout=0.15)
        assert error.value.code == "TURN_TIMEOUT"
        assert w._process.returncode is not None
    interrupt = next(m for m in messages(tmp_path) if m.get("method") == "turn/interrupt")
    assert interrupt["params"]["turnId"] == w.turn_id
    assert not any(m.get("method") == "turn/cancel" for m in messages(tmp_path))


async def test_task_cancellation_stops_process(tmp_path):
    async with make_worker(tmp_path, "hang") as w:
        sid = await w.new_session()
        task = asyncio.create_task(w.prompt(sid, "hang", timeout=3))
        for _ in range(100):
            if w.turn_id is not None:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert w._process.returncode is not None
        assert w.last_result.stop_reason == "cancelled"


async def test_failed_persistence_prevents_submission(tmp_path):
    def fail(_):
        raise OSError("storage unavailable")

    async with make_worker(tmp_path, on_command=fail) as w:
        with pytest.raises(OSError):
            await w.prompt(await w.new_session(), "never submit", timeout=3)
    assert not any(m.get("method") == "turn/start" for m in messages(tmp_path))


async def test_usage_collection_uses_muse_counted_once_counters(tmp_path):
    from taskspindle.providers import Profile
    from taskspindle.usage import collect

    async with make_worker(tmp_path) as w:
        sid = await w.new_session()
        result = await w.prompt(sid, "hello", timeout=3)
        collected = collect(result, profile=Profile(id="muse", auth="oauth", command=("muse",)),
                            cwd=tmp_path, session_id=sid, home=tmp_path, duration_ms=10)
    assert collected.usage.source == "muse_msp"
    assert collected.usage.input_tokens == 14  # raw inputTokens=10 excludes the four cached tokens.
    assert collected.usage.output_tokens == 3
    assert collected.model == "actual-model"
    assert collected.usage.raw["muse_terminal"]["usage"]["inputTokens"] == 10


async def test_terminal_failure_at_shared_status_cursor_is_not_timeout(tmp_path):
    async with make_worker(tmp_path, "failed") as w:
        with pytest.raises(AcpError) as error:
            await w.prompt(await w.new_session(), "hello", timeout=3)
        assert error.value.code == "PROVIDER_AUTH_EXPIRED"
        assert w.command_settled is True
        assert "secret-token" not in json.dumps(w.last_result.usage)
        assert "[redacted]" in json.dumps(w.last_result.usage)


async def test_unconfirmed_cancel_remains_ambiguous(tmp_path):
    async with make_worker(tmp_path, "ignore_interrupt") as w:
        with pytest.raises(AcpError) as error:
            await w.prompt(await w.new_session(), "hang", timeout=0.1)
        assert error.value.code == "RECOVERY_AMBIGUOUS"
        assert w._process.returncode is not None
    assert len([m for m in messages(tmp_path) if m.get("method") == "turn/start"]) == 1


@pytest.mark.parametrize("bound", ["_STREAM_LIMIT", "_EVENT_LIMIT"])
async def test_whole_stream_has_cumulative_bounds(tmp_path, monkeypatch, bound):
    import taskspindle.muse_msp as msp

    async with make_worker(tmp_path) as w:
        sid = await w.new_session()
        monkeypatch.setattr(msp, bound, 1)
        with pytest.raises(AcpError) as error:
            await w.prompt(sid, "hello", timeout=3)
        assert error.value.code == "RECOVERY_AMBIGUOUS"
        assert w._process.returncode is not None


@pytest.mark.parametrize("scenario,code,settled", [
    ("rejected", "MSP_REQUEST_REJECTED", True),
    ("internal_error", "RECOVERY_AMBIGUOUS", False),
])
async def test_only_definitive_rejection_settles_command(tmp_path, scenario, code, settled):
    async with make_worker(tmp_path, scenario) as w:
        with pytest.raises(AcpError) as error:
            await w.prompt(await w.new_session(), "hello", timeout=3)
        assert error.value.code == code
        assert w.command_settled is settled


async def test_teardown_kills_descendants_even_after_leader_exit(tmp_path):
    async with make_worker(tmp_path, "descendant") as w:
        with pytest.raises(AcpError) as error:
            await w.prompt(await w.new_session(), "hang", timeout=0.2)
        assert error.value.code == "RECOVERY_AMBIGUOUS"
    child_pid = int((tmp_path / "wire.child").read_text())
    stat = Path(f"/proc/{child_pid}/stat")
    for _ in range(100):
        if not stat.exists() or stat.read_text().split()[2] == "Z":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Muse process group retained a live descendant")


async def test_refused_resume_cannot_submit_to_unverified_session(tmp_path):
    async with make_worker(tmp_path, "wrong_session") as w:
        sid = uuid7()
        with pytest.raises(AcpError):
            await w.load_session(sid)
        with pytest.raises(AcpError):
            await w.prompt(sid, "must not submit", timeout=3)
    assert not any(m.get("method") == "turn/start" for m in messages(tmp_path))


@pytest.mark.parametrize("kind,code", [
    ("authRequired", "PROVIDER_AUTH_EXPIRED"),
    ("stepLimit", "MSP_TURN_ERROR"),
    ("configError", "MSP_TURN_ERROR"),
    ("projectionError", "MSP_TURN_ERROR"),
    ("logError", "MSP_TURN_ERROR"),
    ("workflowLaunchError", "MSP_TURN_ERROR"),
    ("environmentError", "MSP_TURN_ERROR"),
    ("modelError", "MSP_TURN_ERROR"),
    ("launchError", "MSP_TURN_ERROR"),
    ("futureQuotaError", "MSP_TURN_ERROR"),
])
async def test_terminal_failure_classification_uses_only_structured_kind(tmp_path, kind, code):
    async with make_worker(tmp_path, "failure_" + kind) as w:
        with pytest.raises(AcpError) as error:
            await w.prompt(await w.new_session(), "hello", timeout=3)
        assert error.value.code == code
        assert w.command_settled is True
        assert error.value.cause["msp_error_kind"] == kind
        assert error.value.cause["retryable"] is False
        assert "secret-token" not in json.dumps(error.value.cause)
        assert "[redacted]" in error.value.cause["msp_error_message"]
        from taskspindle.limits import classify_acp_error

        classified = classify_acp_error(error.value, family="muse")
        assert classified.code == code
        assert classified.retryable is False
        if kind == "authRequired":
            assert classified.provider_state == "auth_expired"
            assert classified.source == "muse_msp_terminal"
        else:
            assert classified.provider_state is None
            assert classified.source == "muse_msp"
    assert len([m for m in messages(tmp_path) if m.get("method") == "turn/start"]) == 1
