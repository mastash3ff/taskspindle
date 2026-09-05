"""Bounded transport for the native Antigravity CLI's headless NDJSON protocol.

The caller supplies the policy launcher and credential environment. This module never reads
credentials, changes policy, retries, or substitutes a conversation. CLI usage is cumulative;
only differences from a supplied continuation baseline become observed turn counters.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import math
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .acp_client import (
    DELEGATION_ATTEMPT,
    MODE_SWITCH_ATTEMPT,
    READ_ONLY_VIOLATION,
    AcpError,
    TurnCapture,
    TurnResult,
)

_LINE_LIMIT = 1024 * 1024
_OUTPUT_LIMIT = 16 * 1024 * 1024
_EVENT_LIMIT = 10_000
_STDERR_LIMIT = 64 * 1024
_COUNTERS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "thinking_tokens": "thought_tokens",
    "cache_read_tokens": "cached_read_tokens",
    "cache_write_tokens": "cached_write_tokens",
    "total_tokens": "total_tokens",
}
_STATUSES = {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"}
_DELEGATION_TOOLS = {
    "start_subagent",
    "invoke_subagent",
    "define_subagent",
    "spawn_agent",
    "delegate",
    "manage_task",
}
_MODE_TOOLS = {"switch_mode", "set_mode", "change_mode", "enter_plan_mode", "exit_plan_mode"}
_COMMAND_TOOLS = {"run_command", "send_command_input", "execute_command", "terminal"}
SessionCallback = Callable[[str], Awaitable[None] | None]


def _protocol(message: str) -> AcpError:
    return AcpError("AGY_CLI_PROTOCOL_ERROR", message)


def _failure(diagnostics: str, *, status: str | None, exit_code: int | None) -> AcpError:
    """Classify diagnostic text without persisting or reflecting raw backend diagnostics."""
    lowered = diagnostics.lower()
    cause = {"native_status": status, "exit_code": exit_code}
    if any(
        mark in lowered
        for mark in (
            "please sign in",
            "authentication required",
            "not authenticated",
            "not logged in",
            "unauthorized",
            "token expired",
            "sign in to continue",
        )
    ):
        return AcpError(
            "PROVIDER_AUTH_EXPIRED",
            "AGY is not authenticated; sign in with agy, then run taskspindle auth agy.",
            cause=cause,
        )
    if any(
        mark in lowered
        for mark in (
            "quota exceeded",
            "quota exhausted",
            "rate limit",
            "too many requests",
            "resource_exhausted",
            "usage limit",
        )
    ):
        return AcpError("PROVIDER_THROTTLED", "AGY quota exceeded or rate limit reached.", cause=cause)
    if any(
        mark in lowered
        for mark in (
            "unknown model",
            "invalid model",
            "model not found",
            "unsupported model",
        )
    ):
        return AcpError("MODEL_UNAVAILABLE", "The requested AGY model is unavailable.", cause=cause)
    if any(
        mark in lowered
        for mark in (
            "conversation not found",
            "conversation does not exist",
            "failed to load conversation",
            "could not load conversation",
            "invalid conversation",
        )
    ):
        return AcpError("RESUME_UNAVAILABLE", "AGY could not resume the requested conversation.", cause=cause)
    return AcpError("AGY_CLI_ERROR", "AGY did not complete the requested operation.", cause=cause)


def _timeout(value: float) -> str:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("AGY timeout must be positive and finite")
    return f"{value:g}s"


def _group_running(pgid: int) -> bool:
    """Linux exposes orphaned grandchildren here even after the launcher was reaped."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm is parenthesized and can itself contain spaces or parentheses.
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                return True
        except (OSError, ValueError, IndexError):
            continue  # A process can disappear between the directory and stat reads.
    return False


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    # Always address the group, even when its leader exited: descendants can retain the pipes.
    for sig, grace in ((signal.SIGINT, 0.2), (signal.SIGTERM, 0.3), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        if grace:
            await asyncio.sleep(grace)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), 0.5)
    # SIGKILL delivery is asynchronous, and Process.wait() may return immediately for an
    # already-reaped group leader. Confirm that no surviving descendant can still run.
    deadline = asyncio.get_running_loop().time() + 0.5
    while _group_running(process.pid):
        if asyncio.get_running_loop().time() >= deadline:
            raise AcpError("AGY_CLI_STOP_FAILED", "AGY process-group teardown could not be confirmed.")
        await asyncio.sleep(0.01)


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> bytes:
    data = bytearray()
    while chunk := await reader.read(16 * 1024):
        data.extend(chunk)
        if len(data) > limit:
            raise _protocol("AGY output exceeded the configured size bound.")
    return bytes(data)


async def model_catalog(
    command: tuple[str, ...],
    env: Mapping[str, str],
    cwd: Path,
    timeout: float = 30,
) -> list[tuple[str, str]]:
    """List the authenticated CLI's advertised models without making a model request."""
    _timeout(timeout)
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            "models",
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_LINE_LIMIT,
        )
    except (OSError, ValueError) as exc:
        raise AcpError("ACP_SPAWN_FAILED", "Could not start the pinned AGY CLI.") from exc
    assert process.stdout is not None and process.stderr is not None
    jobs = [
        asyncio.create_task(_read_bounded(process.stdout, _OUTPUT_LIMIT)),
        asyncio.create_task(_read_bounded(process.stderr, _STDERR_LIMIT)),
        asyncio.create_task(process.wait()),
    ]
    try:
        stdout, stderr, exit_code = await asyncio.wait_for(asyncio.gather(*jobs), timeout)
        if exit_code:
            raise _failure(stderr.decode("utf-8", errors="replace"), status=None, exit_code=exit_code)
        from .agy_cli_adapter import parse_model_catalog
        from .providers import ProfileError

        try:
            return parse_model_catalog(stdout.decode("utf-8"))
        except (UnicodeDecodeError, ProfileError) as exc:
            raise _protocol("AGY returned an invalid model catalog.") from exc
    except TimeoutError as exc:
        raise AcpError("TURN_TIMEOUT", "AGY model discovery timed out.") from exc
    finally:
        try:
            await _stop_process(process)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise _protocol("AGY usage must be an object.")
    result = {}
    for key in _COUNTERS:
        if key not in value:
            continue
        count = value[key]
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 2**63 - 1:
            raise _protocol("AGY reported an invalid usage counter.")
        result[key] = count
    return result


class AgyCliWorker:
    """One bounded native CLI turn, using an exact conversation ID for continuations."""

    def __init__(
        self,
        command: tuple[str, ...],
        env: Mapping[str, str],
        cwd: Path,
        stderr_path: Path,
        on_session: SessionCallback | None = None,
        *,
        prior_usage: Mapping[str, Any] | None = None,
        mode: str | None = None,
    ) -> None:
        if mode not in {None, "consult", "review", "implement"}:
            raise ValueError("Unsupported AGY worker mode")
        self.command = tuple(command)
        self.env = dict(env)
        self.cwd = cwd
        self.stderr_path = stderr_path
        self.on_session = on_session
        self.prior_usage = _usage(prior_usage) if prior_usage is not None else None
        self.mode = mode
        self.cumulative_usage: dict[str, int] | None = None
        self.last_result: TurnResult | None = None
        self.session_id: str | None = None
        self._requested_session: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stop_lock = asyncio.Lock()
        self._stopped = False
        self._cancelled = False
        self._used = False
        self._initialized = False
        self._terminal: dict[str, Any] | None = None
        self._capture = TurnCapture()
        self._turn_usage: dict[str, Any] | None = None
        self._response: str | None = None
        self._stderr = bytearray()

    async def __aenter__(self) -> AgyCliWorker:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._stop()

    async def _stop(self) -> None:
        async with self._stop_lock:
            if self._process is not None and not self._stopped:
                await _stop_process(self._process)
                self._stopped = True

    async def cancel(self, session_id: str | None) -> None:
        if session_id and self.session_id and session_id != self.session_id:
            raise AcpError("RESUME_UNAVAILABLE", "Cancellation targeted a different AGY conversation.")
        self._cancelled = True
        await self._stop()

    async def _session(self, value: Any) -> None:
        if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise _protocol("AGY reported an invalid conversation identifier.")
        if self.session_id is not None and value != self.session_id:
            code = "RESUME_UNAVAILABLE" if self._requested_session else "AGY_CLI_PROTOCOL_ERROR"
            raise AcpError(code, "AGY reported a different conversation; the original ID was preserved.")
        first_observation = not self._initialized
        self.session_id = value
        self._initialized = True
        if first_observation and self.on_session is not None:
            result = self.on_session(value)
            if inspect.isawaitable(result):
                await result

    def _record_usage(self, value: Any) -> None:
        cumulative = _usage(value)
        deltas: dict[str, Any] = {"_agy_cli_cumulative": cumulative}
        for key, count in cumulative.items():
            if self._requested_session is None:
                baseline = 0
            elif self.prior_usage is not None and key in self.prior_usage:
                baseline = self.prior_usage[key]
            else:
                continue  # Historical totals without a baseline are not new usage.
            if count < baseline:
                raise _protocol("AGY cumulative usage decreased across a continuation.")
            deltas[_COUNTERS[key]] = count - baseline
        self.cumulative_usage = cumulative
        self._turn_usage = deltas

    def _guard_tool(self, step: dict[str, Any]) -> None:
        """Abort after prohibited observed activity; static policy/namespace prevents it."""
        from .agy_cli_policy import EDIT_TOOLS, READ_TOOLS

        tool_name = step.get("tool_name")
        violation = None
        if "subagent_info" in step or tool_name in _DELEGATION_TOOLS:
            violation = DELEGATION_ATTEMPT
        elif tool_name in _MODE_TOOLS:
            violation = MODE_SWITCH_ATTEMPT
        elif self.mode is not None and tool_name is not None:
            allowed = READ_TOOLS + (EDIT_TOOLS if self.mode == "implement" else ())
            if tool_name not in allowed:
                violation = (
                    READ_ONLY_VIOLATION
                    if self.mode != "implement" and tool_name in (*EDIT_TOOLS, *_COMMAND_TOOLS)
                    else "POLICY_VIOLATION"
                )
        if violation is None:
            return
        if violation not in self._capture.violations:
            self._capture.violations.append(violation)
        self._capture.permission_events.append(
            {
                "tool_call_id": str(step["step_index"]),
                "title": tool_name or step["step_type"],
                "kind": "other",
                "option_id": None,
                "violation": violation,
                "source": "agy_cli_observed_tool",
                "action": "abort",
            }
        )
        raise AcpError("POLICY_VIOLATION", "AGY emitted activity outside the worker's fixed tool policy.")

    async def _event(self, event: dict[str, Any]) -> None:
        if self._terminal is not None:
            raise _protocol("AGY emitted output after its terminal result.")
        self._capture.raw_update_count += 1
        kind = event.get("event")
        if kind == "init":
            if self._initialized or not isinstance(event.get("init"), dict):
                raise _protocol("AGY emitted an invalid or duplicate initialization.")
            await self._session(event.get("conversation_id"))
            # init.model is the requested picker value, not an observed backend identity.
        elif kind == "step_update":
            step = event.get("step_update")
            if not self._initialized or not isinstance(step, dict):
                raise _protocol("AGY emitted a step before initialization or with an invalid shape.")
            await self._session(step.get("conversation_id"))
            index, state, step_type = step.get("step_index"), step.get("state"), step.get("step_type")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise _protocol("AGY emitted an invalid step index.")
            if not isinstance(state, str) or not state or not isinstance(step_type, str) or not step_type:
                raise _protocol("AGY emitted an invalid step state or type.")
            delta = step.get("text_delta")
            if delta is not None and not isinstance(delta, str):
                raise _protocol("AGY emitted a non-text response delta.")
            if delta and step_type == "agent_response":
                self._capture.text.append(delta)
            elif delta and step_type in {"agent_thought", "thinking"}:
                self._capture.thoughts += 1
            if step_type == "tool" or step.get("tool_name") is not None:
                tool_name = step.get("tool_name")
                if not isinstance(tool_name, str) or not tool_name:
                    raise _protocol("AGY emitted a tool step without a tool name.")
                tool = {
                    "update": "tool_call_update",
                    "tool_call_id": str(index),
                    "title": tool_name,
                    "status": state,
                    "kind": "other",
                }
                # Keep structured tool evidence, never the entire stdout/backend diagnostic stream.
                for key in ("tool_info", "subagent_info"):
                    if key in step:
                        if not isinstance(step[key], dict):
                            raise _protocol("AGY emitted invalid tool details.")
                        tool[key] = step[key]
                self._capture.tool_calls.append(tool)
            self._guard_tool(step)
        elif kind == "result":
            result = event.get("result")
            if not isinstance(result, dict):
                raise _protocol("AGY emitted an invalid terminal result.")
            status, response = result.get("status"), result.get("response", "")
            if not isinstance(status, str) or status not in _STATUSES or not isinstance(response, str):
                raise _protocol("AGY emitted an unknown terminal status or invalid response.")
            result_session = result.get("conversation_id")
            if result_session:
                await self._session(result_session)
            elif self._initialized or status == "SUCCESS":
                raise _protocol("AGY's terminal result omitted its conversation identifier.")
            if "error" in result and result["error"] is not None and not isinstance(result["error"], str):
                raise _protocol("AGY emitted a non-text terminal error.")
            if status == "SUCCESS" and ("response" not in result or result.get("error")):
                raise _protocol("AGY emitted an inconsistent success result.")
            # The terminal response contains the current turn, not an additional delta.
            text = "".join(self._capture.text)
            if response.startswith(text):
                if response[len(text) :]:
                    self._capture.text.append(response[len(text) :])
            elif response and not text.endswith(response):
                self._capture.text.append(response)
            # A turn may contain intermediate agent messages around tool calls. Keep all of
            # those in the transcript, but use the terminal response as the final answer.
            if response:
                self._response = response
            if "usage" in result and result["usage"] is not None:
                self._record_usage(result["usage"])
            self._terminal = result
        else:
            raise _protocol("AGY emitted an unknown stream event.")

    async def _stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        size = 0
        while True:
            try:
                line = await self._process.stdout.readline()
            except ValueError as exc:
                raise _protocol("AGY emitted an oversized stream line.") from exc
            if not line:
                return
            size += len(line)
            if size > _OUTPUT_LIMIT or self._capture.raw_update_count >= _EVENT_LIMIT:
                raise _protocol("AGY output exceeded the configured stream bound.")
            if not line.endswith(b"\n"):
                raise _protocol("AGY emitted an unterminated stream line.")
            try:
                event = json.loads(
                    line.decode("utf-8"),
                    object_pairs_hook=_object,
                    parse_constant=_constant,
                    parse_float=_finite_float,
                )
            except (ValueError, RecursionError) as exc:
                raise _protocol("AGY emitted malformed NDJSON.") from exc
            if not isinstance(event, dict):
                raise _protocol("AGY emitted a stream value that was not an object.")
            await self._event(event)

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.read(16 * 1024):
            self._stderr.extend(chunk[: max(0, _STDERR_LIMIT - len(self._stderr))])

    async def prompt(
        self,
        session_id: str | None,
        prompt: str,
        *,
        model: str,
        effort: str | None,
        timeout: float,
    ) -> TurnResult:
        duration = _timeout(timeout)
        if self._used:
            raise AcpError("AGY_CLI_ERROR", "An AGY CLI worker can run only one turn.")
        self._used = True
        self._requested_session = session_id
        self.session_id = session_id
        args = [*self.command, "--model", model]
        if effort is not None:
            args.extend(("--effort", effort))
        if session_id is not None:
            if not session_id:
                raise AcpError("RESUME_UNAVAILABLE", "AGY continuation requires the stored conversation ID.")
            args.extend(("--conversation", session_id))
        args.extend(
            (
                "--print-timeout",
                duration,
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
            )
        )
        stop_reason = "error"
        jobs: list[asyncio.Task] = []
        try:
            if self._cancelled:
                stop_reason = "cancelled"
                return self._snapshot(stop_reason)
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=self.cwd,
                    env=self.env,
                    start_new_session=True,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_LINE_LIMIT,
                )
            except (OSError, ValueError) as exc:
                raise AcpError("ACP_SPAWN_FAILED", "Could not start the pinned AGY CLI.") from exc
            assert self._process.stdin is not None
            jobs = [
                asyncio.create_task(self._stdout()),
                asyncio.create_task(self._drain_stderr()),
                asyncio.create_task(self._process.wait()),
            ]
            async with asyncio.timeout(timeout):
                data = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False)
                self._process.stdin.write((data + "\n").encode("utf-8"))
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await self._process.stdin.drain()
                self._process.stdin.close()
                await asyncio.gather(*jobs)
            if self._cancelled:
                stop_reason = "cancelled"
                return self._snapshot(stop_reason)
            terminal = self._terminal
            diagnostics = self._stderr.decode("utf-8", errors="replace")
            if terminal is None:
                if self._process.returncode:
                    raise _failure(diagnostics, status=None, exit_code=self._process.returncode)
                raise _protocol("AGY exited without a terminal result.")
            status = terminal["status"]
            if status in {"CANCELED", "INTERRUPTED"}:
                stop_reason = "cancelled"
            elif status != "SUCCESS" or self._process.returncode != 0:
                raise _failure(
                    str(terminal.get("error") or "") + "\n" + diagnostics,
                    status=status,
                    exit_code=self._process.returncode,
                )
            else:
                stop_reason = "end_turn"
            return self._snapshot(stop_reason)
        except TimeoutError as exc:
            stop_reason = "timeout"
            raise AcpError("TURN_TIMEOUT", "AGY did not finish within the turn timeout.") from exc
        except asyncio.CancelledError:
            stop_reason = "cancelled"
            raise
        except AcpError:
            if self._cancelled:
                stop_reason = "cancelled"
                return self._snapshot(stop_reason)
            raise
        except Exception as exc:
            raise AcpError(
                "AGY_CLI_ERROR",
                "AGY stream handling failed.",
                cause={"exception": type(exc).__name__},
            ) from exc
        finally:
            try:
                await self._stop()
            finally:
                for job in jobs:
                    if not job.done():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                self.last_result = self._snapshot("cancelled" if self._cancelled else stop_reason)
                # Raw stderr can contain authentication URLs or backend payloads. Persist only a
                # normalized disposition; partial response/tool evidence is in last_result.capture.
                try:
                    self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                    self.stderr_path.write_text(
                        f"AGY CLI disposition={self.last_result.stop_reason}; "
                        f"exit_code={self._process.returncode if self._process else None}\n",
                        encoding="utf-8",
                    )
                except OSError:
                    self._capture.violations.append("AGY_DIAGNOSTIC_WRITE_FAILED")

    def _snapshot(self, stop_reason: str) -> TurnResult:
        text = self._response if self._response is not None else "".join(self._capture.text)
        return TurnResult(stop_reason, text, self._capture, self._turn_usage)
