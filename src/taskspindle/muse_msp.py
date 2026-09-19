"""Bounded, single-session MSP stdio client for the qualified Muse stable surface.

Commands are sent once. Admission acknowledgments are not turn completion, and a
broken connection after submission is never interpreted as permission to resend.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import secrets
import signal
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import taskspindle

from .acp_client import AcpError, InitInfo, TurnCapture, TurnResult, progress_snapshot, redact_agent_output
from .muse import MUSE_SCHEMA_FINGERPRINT, MUSE_VERSION

MSP_FINGERPRINT = MUSE_SCHEMA_FINGERPRINT
MSP_VERSION = 1
_FRAME_LIMIT = 1024 * 1024
_STREAM_LIMIT = 32 * 1024 * 1024
_EVENT_LIMIT = 100_000
_STDERR_LIMIT = 256 * 1024


def uuid7() -> str:
    """RFC 9562 UUIDv7 without requiring Python 3.14's uuid.uuid7."""
    value = ((time.time_ns() // 1_000_000) & ((1 << 48) - 1)) << 80
    value |= 7 << 76 | secrets.randbits(12) << 64 | 2 << 62 | secrets.randbits(62)
    return str(uuid.UUID(int=value))


def _protocol(message: str) -> AcpError:
    return AcpError("MSP_PROTOCOL_ERROR", message)


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _protocol("Muse sent an invalid object.")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _protocol("Muse omitted a required identifier.")
    return value


def _turn_error(terminal: dict[str, Any]) -> AcpError:
    """Classify only the pinned structured vocabulary, never human error prose.

    The stable schema has no quota or model-unavailable class. In particular,
    ``modelError`` cannot establish either of those more specific conditions.
    """
    error = _object(terminal.get("error"))
    kind = _string(error.get("kind"))
    cause = {"msp_error_kind": kind,
             "msp_error_message": redact_agent_output(str(error.get("message", "")))[:2048],
             "retryable": error.get("retryable")}
    if kind == "authRequired":
        return AcpError("PROVIDER_AUTH_EXPIRED", "Muse requires authentication.", cause=cause)
    return AcpError("MSP_TURN_ERROR", "Muse reported a failed turn.", cause=cause)


async def _callback(callback: Callable[..., Any] | None, value: Any) -> None:
    if callback is not None:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


class MuseWorker:
    """One process and durable session; the caller owns isolation and qualification.

    ``on_session`` and ``on_command`` must durably save their handles before they
    return. All approvals are denied. This is not itself a workspace sandbox.
    """

    def __init__(self, *, command: Sequence[str], env: Mapping[str, str], cwd: Path,
                 stderr_path: Path, on_session: Callable[..., Any] | None = None,
                 on_command: Callable[..., Any] | None = None, mode: str = "consult",
                 model: str | None = None, effort: str | None = None,
                 on_progress: Callable[[dict[str, Any]], None] | None = None,
                 handshake_timeout: float = 30.0, allow_ephemeral: bool = False) -> None:
        if not command:
            raise AcpError("MSP_SPAWN_FAILED", "Empty Muse command.")
        if effort is not None and effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
        }:
            raise _protocol("Unsupported Muse reasoning effort.")
        self._command, self._env = tuple(command), dict(env)
        self._cwd, self._stderr_path = Path(cwd), Path(stderr_path)
        self._on_session, self._on_command = on_session, on_command
        self.mode, self.model, self.effort = mode, model, effort
        self.on_progress = on_progress
        self._handshake_timeout, self._allow_ephemeral = handshake_timeout, allow_ephemeral
        self.init: InitInfo | None = None
        self.last_result: TurnResult | None = None
        self.session_model: str | None = None
        self.session_effort: str | None = None
        self.command_id: str | None = None
        self.command_settled = False
        self.turn_id: str | None = None
        self._session_id: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._fatal: AcpError | None = None
        self._capture: TurnCapture | None = None
        self._turn_completed: dict[str, Any] | None = None
        self._terminal: asyncio.Future[dict[str, Any]] | None = None
        self._before_ack: list[tuple[str, dict[str, Any]]] = []
        self._items: dict[str, dict[str, Any]] = {}
        self._seen_cursors: set[tuple[str, str, bytes]] = set()
        self._decisions: set[tuple[str, str]] = set()
        self._bytes = self._events = 0
        self._closing = False
        self._stopped = False
        self._session_ready = False

    async def __aenter__(self) -> MuseWorker:
        try:
            self._stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._process = await asyncio.create_subprocess_exec(
                *self._command, cwd=self._cwd, env=self._env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, limit=_FRAME_LIMIT,
            )
            self._reader = asyncio.create_task(self._read())
            self._stderr = asyncio.create_task(self._drain_stderr())
            result = await self._request("initialize", {
                "clientInfo": {"name": "taskspindle", "version": taskspindle.__version__},
                "capabilities": {"experimentalApi": False, "requestedCapabilities": [],
                                 "userInputDialogs": False},
            }, timeout=self._handshake_timeout)
            schema, info = _object(result.get("schema")), _object(result.get("serverInfo"))
            if (schema.get("version") != MSP_VERSION or schema.get("fingerprint") != MSP_FINGERPRINT
                    or info.get("name") != "muse" or info.get("version") != MUSE_VERSION
                    or result.get("experimentalApi") is not False):
                raise _protocol("Muse version or stable protocol fingerprint is incompatible.")
            durability = result.get("sessionDurability", "durable")
            if durability != "durable" and not (self._allow_ephemeral and durability == "ephemeral"):
                raise _protocol("Muse requires durable sessions.")
            self.init = InitInfo(load_session=durability == "durable", auth_method_ids=(),
                                 agent_info={**info, "schema": schema, "sessionDurability": durability})
            await self._write({"jsonrpc": "2.0", "method": "initialized"})
            return self
        except BaseException as exc:
            await self._stop()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AcpError("MSP_HANDSHAKE_FAILED", "Muse MSP initialization failed.",
                           cause={"exception": type(exc).__name__,
                                  "message": redact_agent_output(str(exc))}) from exc

    async def __aexit__(self, *_: Any) -> None:
        await self._stop()

    async def _stop(self) -> None:
        if self._stopped:
            return
        self._closing = True
        process = self._process
        if process is not None:
            # Kill the process group even when its leader already exited.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 2.0)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), 2.0)
        tasks = [t for t in [self._reader, self._stderr, *self._handlers] if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._fail(_protocol("Muse connection closed."))
        self._stopped = True

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        # Persist only a bounded, sanitized diagnostic tail; never raw credentials.
        tail = bytearray()
        while chunk := await self._process.stderr.read(8192):
            tail.extend(chunk)
            del tail[:-_STDERR_LIMIT]
            self._stderr_path.write_text(redact_agent_output(tail.decode("utf-8", "replace")))

    def _fail(self, error: AcpError) -> None:
        self._fatal = self._fatal or error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._fatal)
        if self._terminal is not None and not self._terminal.done():
            self._terminal.set_exception(self._fatal)

    async def _write(self, message: dict[str, Any]) -> None:
        if self._fatal is not None:
            raise self._fatal
        data = (json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(data) > _FRAME_LIMIT:
            raise _protocol("Muse outgoing frame exceeded its bound.")
        assert self._process is not None and self._process.stdin is not None
        async with self._write_lock:
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any], *, timeout: float = 30.0
                       ) -> dict[str, Any]:
        if len(self._pending) >= 32:
            raise _protocol("Muse pending request bound exceeded.")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _read(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                self._bytes += len(line)
                self._events += 1
                if (len(line) > _FRAME_LIMIT or self._bytes > _STREAM_LIMIT
                        or self._events > _EVENT_LIMIT or not line.endswith(b"\n")):
                    raise _protocol("Muse stream exceeded its bound or ended in a partial frame.")
                message = _object(json.loads(line))
                if message.get("jsonrpc") != "2.0":
                    raise _protocol("Muse sent an invalid JSON-RPC envelope.")
                if "method" in message:
                    method = _string(message["method"])
                    params = _object(message.get("params", {}))
                    if "id" in message:
                        if method not in {"approval/request", "userInput/request"}:
                            await self._write({"jsonrpc": "2.0", "id": message["id"],
                                               "error": {"code": -32601, "message": "Unsupported method"}})
                            continue
                        await self._write({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                    if method in {"approval/request", "approval/requested", "approval/updated",
                                  "userInput/request", "userInput/requested"}:
                        self._schedule_decision(method, params)
                    elif method == "session/reasoningEffortChanged":
                        if params.get("sessionId") == self._session_id:
                            self.session_effort = _string(params.get("reasoningEffort"))
                    elif self._capture is not None:
                        if self.turn_id is None:
                            self._before_ack.append((method, params))
                        else:
                            self._event(method, params)
                else:
                    request_id = message.get("id")
                    if type(request_id) is not int or request_id not in self._pending:
                        raise _protocol("Muse sent an unmatched response.")
                    future = self._pending[request_id]
                    if future.done():
                        raise _protocol("Muse sent a duplicate response.")
                    if "error" in message:
                        error = _object(message["error"])
                        if type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                            raise _protocol("Muse emitted an invalid error response.")
                        data = error.get("data", {})
                        data = data if isinstance(data, dict) else {}
                        safe_data = {k: redact_agent_output(str(data[k]))
                                     for k in ("kind", "commandId", "reason") if k in data}
                        future.set_exception(AcpError("MSP_REQUEST_REJECTED", "Muse rejected an MSP request.",
                            cause={"rpc_code": error["code"], "rpc_data": safe_data,
                                   "rpc_message": redact_agent_output(error["message"])[:2048]}))
                    elif "result" in message:
                        future.set_result(_object(message["result"]))
                    else:
                        raise _protocol("Muse response has no result or error.")
            if not self._closing:
                raise _protocol("Muse stream ended before connection teardown.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc if isinstance(exc, AcpError) else _protocol("Muse sent a malformed stream."))

    def _schedule_decision(self, method: str, params: dict[str, Any]) -> None:
        if len(self._handlers) >= 16:
            raise _protocol("Muse approval request bound exceeded.")
        task = asyncio.create_task(self._decide(method, params))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _decide(self, method: str, params: dict[str, Any]) -> None:
        try:
            if params.get("sessionId") != self._session_id:
                raise _protocol("Muse requested a decision for an unknown session.")
            if method.startswith("approval/"):
                approval = _string(params.get("approvalId"))
                requirement = _object(params.get("currentRequirementId"))
                key = (approval, json.dumps(requirement, sort_keys=True))
                if key in self._decisions:
                    return
                self._decisions.add(key)
                choices = params.get("availableChoices")
                if not isinstance(choices, list):
                    raise _protocol("Muse approval omitted its choices.")
                denied = next((c for c in choices
                               if isinstance(c, dict) and c.get("decision") == "denied"), None)
                if self._capture is not None:
                    self._capture.permission_events.append({"approval_id": approval, "decision": "denied"})
                    self._capture.violations.append("MUSE_APPROVAL_DENIED")
                if denied is None:
                    raise _protocol("Muse approval has no safe denial choice.")
                await self._request("approval/decide", {"commandId": uuid7(), "sessionId": self._session_id,
                    "approvalId": approval, "requirementId": requirement,
                    "choiceId": _string(denied.get("choiceId"))})
            else:
                input_id = _string(params.get("userInputId"))
                key = ("userInput", input_id)
                if key in self._decisions:
                    return
                self._decisions.add(key)
                await self._request("userInput/cancel", {"commandId": uuid7(), "sessionId": self._session_id,
                    "userInputId": input_id, "reason": "TaskSpindle has no interactive dialog."})
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail(_protocol("Muse could not safely deny an interactive request."))

    async def new_session(self) -> str:
        if self._session_id is not None:
            raise _protocol("Muse worker already has a session.")
        session_id = uuid7()
        await _callback(self._on_session, session_id)
        self._session_id = session_id
        params: dict[str, Any] = {"commandId": uuid7(), "sessionId": session_id,
                                 "workspaceRoot": str(self._cwd), "approvalMode": "denyUnmatched"}
        if self.model:
            params["modelId"] = self.model
        result = await self._request("session/start", params)
        self._accept_session(result, session_id)
        self._session_ready = True
        return session_id

    def _accept_session(self, result: dict[str, Any], session_id: str) -> None:
        session = _object(result.get("session"))
        if session.get("sessionId") != session_id:
            raise _protocol("Muse substituted a different session identity.")
        if not self._allow_ephemeral and not session.get("path"):
            raise _protocol("Muse session has no durable log.")
        if _object(session.get("approvalMode")).get("mode") != "denyUnmatched":
            raise _protocol("Muse did not retain the required approval mode.")
        if session.get("status") != "idle" or session.get("activeTurnId") is not None:
            raise AcpError("RECOVERY_AMBIGUOUS", "Muse session has an unresolved active turn.")
        self.session_model = session.get("modelId") if isinstance(session.get("modelId"), str) else None
        if self.model and self.session_model != self.model:
            raise _protocol("Muse session did not select the requested model.")

    async def load_session(self, session_id: str) -> None:
        if self._session_id is not None:
            raise AcpError("RESUME_UNAVAILABLE", "Muse worker already has a session.")
        _string(session_id)
        await _callback(self._on_session, session_id)
        self._session_id = session_id
        try:
            result = await self._request("session/resume", {
                "commandId": uuid7(), "sessionId": session_id, "excludeItems": True,
            })
            self._accept_session(result, session_id)
            if result.get("pendingRequests"):
                raise AcpError("RECOVERY_AMBIGUOUS", "Muse session has unresolved requests.")
            self._session_ready = True
        except Exception as exc:
            if isinstance(exc, AcpError) and exc.code == "RECOVERY_AMBIGUOUS":
                raise
            raise AcpError("RESUME_UNAVAILABLE", "Muse could not resume the exact durable session.") from exc

    def _event(self, method: str, params: dict[str, Any]) -> None:
        capture = self._capture
        if capture is None or params.get("sessionId") != self._session_id:
            return
        cursor = params.get("viewCursor")
        if isinstance(cursor, str):
            # The pinned host emits statusChanged and turn/completed at the same
            # cursor. Cursor alone is not an event identity, despite schema prose.
            key = (method, cursor, hashlib.sha256(json.dumps(params, sort_keys=True).encode()).digest())
            if key in self._seen_cursors:
                return
            self._seen_cursors.add(key)
        if method == "session/approvalModeChanged" and params.get("mode") != "denyUnmatched":
            raise _protocol("Muse changed its approval enforcement mode.")
        if method == "view/gap":
            raise _protocol("Muse reported a gap in the turn stream.")
        if method.startswith("item/"):
            if method == "item/delta":
                item = self._items.get(params.get("itemId"))
                if (item is not None and item.get("kind") == "agentMessage"
                        and params.get("field", "text") == "text"):
                    if not isinstance(params.get("delta"), str):
                        raise _protocol("Muse text delta was malformed.")
                    item["text"] = item.get("text", "") + params["delta"]
            else:
                item = _object(params.get("item"))
                if item.get("turnId") != self.turn_id:
                    return
                item_id = _string(item.get("itemId"))
                if (type(item.get("revision")) is not int or item["revision"] < 1
                        or not isinstance(item.get("kind"), str)
                        or not isinstance(item.get("status"), str)
                        or ("text" in item and not isinstance(item["text"], str))):
                    raise _protocol("Muse emitted an invalid item.")
                previous = self._items.get(item_id)
                if previous is None or item.get("revision", 0) > previous.get("revision", 0):
                    self._items[item_id] = dict(item)
                kind = item.get("kind")
                if kind == "reasoning" and previous is None:
                    capture.thoughts += 1
                if kind == "toolCall":
                    capture.tool_calls.append({"tool_call_id": item_id, "title": item.get("tool", ""),
                                               "status": item.get("status"), "kind": "other"})
                if kind in {"subagent", "workflow"}:
                    capture.violations.append("DELEGATION_ATTEMPT")
                    raise _protocol("Muse attempted delegated work.")
            capture.text[:] = [i.get("text", "") for i in self._items.values()
                               if i.get("kind") == "agentMessage"]
        elif params.get("turnId") != self.turn_id:
            return
        elif method == "session/tokenUsage":
            usage = _object(params.get("usage"))
            for name in ("promptTokens", "totalTokens"):
                if type(params.get(name)) is not int or params[name] < 0:
                    raise _protocol("Muse usage counter was invalid.")
            for value in usage.values():
                if type(value) is not int or value < 0:
                    raise _protocol("Muse usage counter was invalid.")
            capture.usage_updates.append(dict(params))
            model = params.get("modelId")
            if isinstance(model, str) and model not in capture.model_ids:
                capture.model_ids.append(model)
        elif method == "turn/completed":
            if (not isinstance(params.get("viewCursor"), str)
                    or not isinstance(params.get("sourceRange"), dict)):
                raise _protocol("Muse terminal omitted its durable provenance.")
            if "usage" in params:
                raw_usage = _object(params["usage"])
                if any(type(value) is not int or value < 0 for value in raw_usage.values()):
                    raise _protocol("Muse terminal usage was invalid.")
            params = dict(params)
            if "reason" in params:
                params["reason"] = redact_agent_output(str(params["reason"]))
            if params.get("terminal") == "failed" and "error" not in params:
                raise _protocol("Muse failed terminal omitted its structured error.")
            if "error" in params:
                error = _object(params["error"])
                if (not isinstance(error.get("kind"), str) or not error["kind"]
                        or not isinstance(error.get("message"), str)
                        or type(error.get("retryable")) is not bool):
                    raise _protocol("Muse failed terminal contained an invalid structured error.")
                params["error"] = {"kind": error.get("kind"),
                                   "message": redact_agent_output(str(error.get("message", ""))),
                                   "retryable": error.get("retryable")}
            if self._terminal is not None and not self._terminal.done():
                self.command_settled = params.get("terminal") in {"completed", "cancelled", "failed"}
                self._terminal.set_result(params)
            self._turn_completed = dict(params)
        capture.raw_update_count += 1
        if self.on_progress:
            with contextlib.suppress(Exception):
                self.on_progress(progress_snapshot(capture))

    def _result(self, stop_reason: str) -> TurnResult:
        capture = self._capture or TurnCapture()
        usage = None
        if capture.usage_updates:
            usage = {"_muse_msp": True,
                     "input_tokens": sum(u["promptTokens"] for u in capture.usage_updates),
                     "output_tokens": sum(u["usage"].get("outputTokens", 0) for u in capture.usage_updates),
                     "total_tokens": sum(u["totalTokens"] for u in capture.usage_updates)}
        elif self._turn_completed and isinstance(self._turn_completed.get("usage"), dict):
            # Raw counters have provider-dependent cache conventions; do not invent a total.
            raw = self._turn_completed["usage"]
            usage = {"_muse_msp": True, "muse_raw_usage": raw}
        if usage is not None:
            usage["muse_terminal"] = self._turn_completed
        self.last_result = TurnResult(stop_reason, "".join(capture.text), capture, usage)
        return self.last_result

    async def prompt(self, session_id: str, text: str, *, timeout: float = 900.0) -> TurnResult:
        if self._fatal is not None:
            raise AcpError("RECOVERY_AMBIGUOUS", "Muse connection cannot submit another turn.")
        if not self._session_ready or session_id != self._session_id or self._capture is not None:
            raise _protocol("Muse prompt requires its idle attached session.")
        self.command_id, self.turn_id = uuid7(), None
        self.command_settled = False
        self._capture = TurnCapture()
        self._turn_completed = None
        self._items, self._seen_cursors, self._before_ack = {}, set(), []
        self._terminal = asyncio.get_running_loop().create_future()
        submitted = False
        try:
            await _callback(self._on_command, self.command_id)
            params: dict[str, Any] = {"commandId": self.command_id, "sessionId": session_id,
                                     "input": [{"type": "text", "text": text}], "ifBusy": "queue"}
            if self.effort is not None:
                params["reasoningEffort"] = self.effort
            async with asyncio.timeout(timeout):
                submitted = True
                ack = await self._request("turn/start", params, timeout=timeout)
                if (ack.get("commandId") != self.command_id or ack.get("status") != "accepted"
                        or ack.get("disposition") != "started" or ack.get("startedNewTurn") is not True):
                    raise _protocol("Muse did not admit a new foreground turn.")
                self.turn_id = _string(ack.get("turnId"))
                for method, event in self._before_ack:
                    self._event(method, event)
                self._before_ack.clear()
                terminal = await asyncio.shield(self._terminal)
            reason = terminal.get("terminal")
            if reason == "failed":
                raise _turn_error(terminal)
            if reason not in {"completed", "cancelled"}:
                raise _protocol("Muse reported an unknown turn terminal.")
            return self._result("end_turn" if reason == "completed" else "cancelled")
        except asyncio.CancelledError:
            await self.cancel(session_id)
            self._result("cancelled")
            raise
        except TimeoutError as exc:
            await self.cancel(session_id)
            self._result("timeout")
            confirmed = (self._terminal.done() and not self._terminal.cancelled()
                         and self._terminal.exception() is None)
            code = "TURN_TIMEOUT" if confirmed else "RECOVERY_AMBIGUOUS"
            raise AcpError(code, "Muse turn timed out; the submitted command must not be resent.") from exc
        except Exception as exc:
            self._result("error")
            if isinstance(exc, AcpError) and exc.code in {"MSP_TURN_ERROR", "PROVIDER_AUTH_EXPIRED"}:
                raise
            if isinstance(exc, AcpError) and exc.code == "MSP_REQUEST_REJECTED":
                code = exc.cause.get("rpc_code")
                data = exc.cause.get("rpc_data", {})
                # Internal, interrupted, and unknown errors do not establish
                # whether durable admission happened. Never settle those.
                definitive = code in {-32600, -32601, -32602, -32002, -32010, -32020, -32024, -32032}
                definitive |= (code == -32030 and data.get("kind") == "commandRejected"
                               and data.get("commandId") == self.command_id)
                if definitive:
                    self.command_settled = True
                    raise
            if submitted:
                await self._stop()
                raise AcpError("RECOVERY_AMBIGUOUS",
                               "Muse turn outcome is unknown; do not resend the command.") from exc
            raise
        finally:
            if self._terminal is not None:
                if not self._terminal.done():
                    self._terminal.cancel()
                elif not self._terminal.cancelled():
                    self._terminal.exception()
            self._capture = None

    async def cancel(self, session_id: str | None) -> None:
        if session_id != self._session_id or self._terminal is None or self._terminal.done():
            return
        try:
            params = {"commandId": uuid7(), "sessionId": session_id}
            if self.turn_id:
                params["turnId"] = self.turn_id
            # Priority interrupt is the operator stop gesture; turn/cancel is the normal lane.
            await self._request("turn/interrupt", params, timeout=2.0)
            await asyncio.wait_for(asyncio.shield(self._terminal), 2.0)
        except Exception:
            pass
        finally:
            await self._stop()
