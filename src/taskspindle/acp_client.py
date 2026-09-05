"""The thin ACP client a TaskSpindle worker drives one agent process with.

One :class:`AcpWorker` owns one subprocess for its lifetime: spawn, initialize, create or resume a
session, prompt, cancel, tear down. It is deliberately thin -- no retries, no policy beyond the
permission gate, no knowledge of tasks or the store -- because the worker composes it.

Two decisions are load-bearing:

* **Capture runs on the synchronous stream observer**, not on the async ``session/update``
  handler. ``acp.Connection`` resolves a response future inline in its receive loop but publishes
  notifications to a queue that a dispatcher drains into separate tasks, so a ``session/update``
  sent immediately before the prompt response can be *handled* after ``prompt()`` has already
  returned. The observer is called inline in receive order, before the message is processed at
  all, so every update belonging to a turn is captured before that turn's response resolves.
* **The client advertises no filesystem and no terminal capability.** The agent must use its own
  tools, which run inside the task worktree and go through :class:`PermissionPolicy`, rather than
  asking TaskSpindle to write files on its behalf.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acp import (
    PROTOCOL_VERSION,
    RequestError,
    spawn_agent_process,
    text_block,
)
from acp.connection import StreamDirection, StreamEvent
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    FileSystemCapabilities,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallUpdate,
)
from acp.transports import DEFAULT_INHERITED_ENV_VARS

import taskspindle

__all__ = [
    "AcpError",
    "AcpWorker",
    "InitInfo",
    "PermissionPolicy",
    "TurnCapture",
    "TurnResult",
    "error_cause",
    "sealed_env",
]

_CLIENT_INFO = Implementation(name="taskspindle", version=taskspindle.__version__)
_STREAM_LIMIT = 16 * 1024 * 1024
_CANCEL_TIMEOUT = 2.0
_CLAUDE_MODEL_ID = re.compile(r"claude-[A-Za-z0-9][A-Za-z0-9._:\[\]-]*")

#: Whole words in a tool-call title that mean the agent is trying to spawn helpers of its own.
#: Whole words only: ``Edit src/tasks.py`` and ``Run pytest tests/test_task.py`` are ordinary work.
_DELEGATION_WORDS = re.compile(r"\b(?:subagent|agent|team)\b", re.IGNORECASE)

#: Tool names that are delegation whatever they are titled with, matched at the start of the
#: title so that a path or a sentence merely containing one of them is not caught.
_DELEGATION_TOOLS = re.compile(r"^(?:Agent|Task|TeamCreate|SendMessage)\b")

_DELEGATION_EXEMPT_KINDS = ("read", "fetch")
_WRITE_KINDS = ("edit", "delete", "move", "execute")

READ_ONLY_VIOLATION = "READ_ONLY_VIOLATION"
DELEGATION_ATTEMPT = "DELEGATION_ATTEMPT"
#: The agent asked to leave the session mode it was put in (Claude's "Ready to code?" prompt).
MODE_SWITCH_ATTEMPT = "MODE_SWITCH_ATTEMPT"


class AcpError(Exception):
    """An ACP-level failure.

    ``code`` is one of ``ACP_SPAWN_FAILED``, ``ACP_HANDSHAKE_FAILED``, ``RESUME_UNAVAILABLE``,
    ``TURN_TIMEOUT``, ``ACP_TURN_ERROR``. ``cause`` carries what the wire said -- the JSON-RPC
    code, message and data of a ``RequestError``, or the class name of anything else -- so that
    whoever records the failure can tell a quota refusal from a crash. The client itself does not
    interpret it.
    """

    def __init__(self, code: str, message: str, *, cause: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.cause: dict[str, Any] = dict(cause or {})


def error_cause(exc: BaseException) -> dict[str, Any]:
    """What an exception out of the ACP connection said, as plain data."""
    if isinstance(exc, RequestError):
        data = exc.data if isinstance(exc.data, dict) else None
        return {"rpc_code": exc.code, "rpc_message": str(exc), "rpc_data": data}
    return {"exception": type(exc).__name__, "message": str(exc)}


def sealed_env(env: Mapping[str, str]) -> dict[str, str]:
    """Close the hole ``acp``'s ``default_environment()`` would otherwise open.

    ``spawn_stdio_transport`` merges a small set of parent variables (``HOME``, ``PATH``,
    ``SHELL``, ``TERM``, ``USER``, ``LOGNAME`` on POSIX) *under* the environment we hand it, so a
    name we deliberately left out would still be inherited from the TaskSpindle process. Pinning
    each such name to the empty string when we did not set it keeps the allowlist authoritative.
    """
    sealed = dict(env)
    for name in DEFAULT_INHERITED_ENV_VARS:
        sealed.setdefault(name, "")
    return sealed


def _is_delegation(title: str) -> bool:
    """True when a tool-call title names one of the agent-spawning tools."""
    return bool(_DELEGATION_TOOLS.match(title) or _DELEGATION_WORDS.search(title))


class PermissionPolicy:
    """TaskSpindle's answer to ``session/request_permission``.

    Delegation is refused unconditionally: a worker agent may not spawn agents of its own,
    whatever the task mode. Writes are refused unless the task was started with write intent.
    """

    def __init__(self, *, allow_writes: bool) -> None:
        self.allow_writes = allow_writes

    def select(
        self,
        tool_call: ToolCallUpdate,
        options: Sequence[PermissionOption],
    ) -> tuple[str | None, str | None]:
        """Return ``(option_id, violation)``; ``option_id`` of ``None`` cancels the request."""
        kind = getattr(tool_call, "kind", None) or ""
        title = (getattr(tool_call, "title", None) or "").strip()

        if kind == "switch_mode":
            # The mode was chosen for the task; a worker does not renegotiate it.
            return self._deny(options), MODE_SWITCH_ATTEMPT
        if kind not in _DELEGATION_EXEMPT_KINDS and _is_delegation(title):
            return self._deny(options), DELEGATION_ATTEMPT
        if not self.allow_writes and kind in _WRITE_KINDS:
            return self._deny(options), READ_ONLY_VIOLATION

        allowed = self._pick(options, "allow")
        if allowed is not None:
            return allowed, None
        return self._deny(options), None

    def _deny(self, options: Sequence[PermissionOption]) -> str | None:
        return self._pick(options, "reject")

    @staticmethod
    def _pick(options: Sequence[PermissionOption], prefix: str) -> str | None:
        candidates = [option for option in options if str(option.kind).startswith(prefix)]
        for option in candidates:
            if str(option.kind).endswith("_once"):
                return option.option_id
        return candidates[0].option_id if candidates else None


@dataclass
class TurnCapture:
    """Everything one turn produced, in receive order."""

    text: list[str] = field(default_factory=list)
    thoughts: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    permission_events: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    raw_update_count: int = 0
    #: Every ``usage_update`` the agent sent: context occupancy, and whatever it put in ``_meta``.
    usage_updates: list[dict[str, Any]] = field(default_factory=list)
    #: Rate-limit facts the Claude adapter attaches to ``usage_update`` under ``_claude/rateLimit``.
    rate_limits: list[dict[str, Any]] = field(default_factory=list)
    #: Grok's non-standard ``turn_completed`` update, which carries the turn's token usage.
    turn_completed: dict[str, Any] | None = None
    #: Model ids seen in any update's ``_meta.modelId``, in the order they appeared.
    model_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InitInfo:
    """What the agent said about itself at ``initialize``."""

    load_session: bool | None
    auth_method_ids: tuple[str, ...]
    agent_info: dict[str, Any]


@dataclass(frozen=True)
class TurnResult:
    """The outcome of one ``session/prompt``."""

    stop_reason: str
    text: str
    capture: TurnCapture
    #: The prompt response's ``usage`` (unstable in ACP 0.12), as snake_case plain data.
    usage: dict[str, Any] | None = None


class _WorkerClient:
    """The ACP ``Client`` half: permission decisions, and nothing else.

    ``session/update`` is *not* handled here -- it is captured by the stream observer -- but the
    method must exist so the agent's notifications are not rejected. Filesystem and terminal
    methods refuse: the client advertises neither capability, so a well-behaved agent never calls
    them, and a misbehaving one gets a method-not-found rather than a service.
    """

    def __init__(self, worker: AcpWorker) -> None:
        self._worker = worker

    def on_connect(self, conn: Any) -> None:
        return None

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        return None

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        option_id, violation = self._worker.policy.select(tool_call, options)
        self._worker._record_permission(tool_call, option_id, violation)
        if option_id is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=option_id))

    async def read_text_file(self, session_id: str, path: str, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("fs/write_text_file")

    async def create_terminal(self, session_id: str, command: str, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        raise RequestError.method_not_found("terminal/kill")


class AcpWorker:
    """One agent subprocess, from spawn to teardown."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        env: Mapping[str, str],
        cwd: Path,
        stderr_path: Path,
        policy: PermissionPolicy,
        handshake_timeout: float = 60.0,
        late_update_grace: float = 0.0,
    ) -> None:
        if not command:
            raise AcpError("ACP_SPAWN_FAILED", "empty command")
        self._command = tuple(command)
        self._env = sealed_env(env)
        self._cwd = cwd
        self._stderr_path = stderr_path
        self.policy = policy
        self._handshake_timeout = handshake_timeout
        #: How long :meth:`prompt` keeps capturing after the response, for an agent that sends
        #: its turn summary *after* answering. Grok 1.0.13 sends ``turn_completed`` -- the update
        #: with the turn's token usage -- that way; the wait ends the moment it arrives.
        self._late_update_grace = late_update_grace

        self._stack = contextlib.AsyncExitStack()
        self._conn: Any = None
        self._process: Any = None
        self._capture: TurnCapture | None = None
        self._sessions: list[str] = []
        self.init: InitInfo | None = None
        #: How many ``session/update`` notifications the last ``load_session`` replayed.
        self.replay_update_count = 0
        #: The session mode last set through :meth:`set_mode`, if any.
        self.mode: str | None = None
        #: A canonical Claude model from the session configuration, when the adapter supplies one.
        self.session_model: str | None = None
        #: Exact advertised configuration, kept independently of model attribution.
        self.session_config_options: list[dict[str, Any]] = []
        self._expected_config: dict[str, str] = {}
        self._config_violation = False
        #: The last prompt's capture, including a prompt that raised or was cancelled.
        self.last_result: TurnResult | None = None

    # -- lifecycle ---------------------------------------------------------------------------

    async def __aenter__(self) -> AcpWorker:
        stderr = self._stack.enter_context(self._stderr_path.open("ab"))
        command, *args = self._command
        try:
            conn, process = await self._stack.enter_async_context(
                spawn_agent_process(
                    _WorkerClient(self),
                    command,
                    *args,
                    env=self._env,
                    cwd=self._cwd,
                    transport_kwargs={"limit": _STREAM_LIMIT, "stderr": stderr},
                    observers=[self._observe],
                )
            )
        except OSError as exc:
            await self._stack.aclose()
            raise AcpError("ACP_SPAWN_FAILED", f"could not launch {command!r}: {exc}") from exc
        except BaseException:
            await self._stack.aclose()
            raise

        self._conn = conn
        self._process = process
        try:
            response = await asyncio.wait_for(
                conn.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=_capabilities(),
                    client_info=_CLIENT_INFO,
                ),
                timeout=self._handshake_timeout,
            )
        except BaseException as exc:
            await self._stack.aclose()
            if isinstance(exc, Exception):
                raise AcpError(
                    "ACP_HANDSHAKE_FAILED", f"initialize failed: {exc}", cause=error_cause(exc)
                ) from exc
            raise
        self.init = _init_info(response)
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        for session_id in list(self._sessions):
            await self.cancel(session_id)
        self._capture = None
        # The exit stack tears the agent down in order: close the connection, EOF its stdin,
        # wait, terminate, wait, kill -- then close the stderr file.
        await self._stack.aclose()
        self._conn = None
        self._process = None

    # -- capture -----------------------------------------------------------------------------

    def _observe(self, event: StreamEvent) -> None:
        """Synchronous, inline in receive order: see the module docstring."""
        if event.direction is not StreamDirection.INCOMING:
            return
        params = event.message.get("params")
        update = params.get("update") if isinstance(params, dict) else None
        if not _is_session_update(event.message.get("method"), update):
            return
        if isinstance(update, dict):
            self._observe_configuration(update)
        capture = self._capture
        if capture is None:
            return
        capture.raw_update_count += 1
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        _note_model_id(capture, update, params)
        if kind == "agent_message_chunk":
            content = update.get("content")
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str):
                capture.text.append(text)
        elif kind == "agent_thought_chunk":
            capture.thoughts += 1
        elif kind in ("tool_call", "tool_call_update"):
            capture.tool_calls.append(
                {
                    "update": kind,
                    "tool_call_id": update.get("toolCallId"),
                    "title": update.get("title"),
                    "kind": update.get("kind"),
                    "status": update.get("status"),
                }
            )
        elif kind == "usage_update":
            meta = update.get("_meta")
            capture.usage_updates.append(
                {"used": update.get("used"), "size": update.get("size"), "_meta": meta}
            )
            rate_limit = meta.get("_claude/rateLimit") if isinstance(meta, dict) else None
            if isinstance(rate_limit, dict):
                capture.rate_limits.append(dict(rate_limit))
        elif kind == "turn_completed":
            capture.turn_completed = dict(update)

    def _observe_configuration(self, update: dict[str, Any]) -> None:
        """Protect selected values even between configuration requests and turns."""
        if not self._expected_config:
            return
        kind = update.get("sessionUpdate")
        changed = False
        if kind == "current_mode_update" and "mode" in self._expected_config:
            changed = update.get("currentModeId") != self._expected_config["mode"]
        elif kind == "config_option_update":
            options = update.get("configOptions")
            if not isinstance(options, list):
                changed = True
            else:
                for option in options:
                    if not isinstance(option, dict) or not isinstance(option.get("id"), str):
                        changed = True
                    elif option["id"] in self._expected_config:
                        changed |= option.get("currentValue") != self._expected_config[option["id"]]
        if changed and not self._config_violation:
            self._config_violation = True
            if self._capture is not None:
                self._capture.violations.append(MODE_SWITCH_ATTEMPT)
            else:
                self.last_result = TurnResult(
                    stop_reason="error", text="",
                    capture=TurnCapture(violations=[MODE_SWITCH_ATTEMPT]),
                )
            # Stop a host that changed a protected option without client approval.
            # The worker unit's control-group teardown also stops its descendants.
            if self._process is not None:
                with contextlib.suppress(ProcessLookupError):
                    self._process.terminate()

    def _require_configuration_intact(self) -> None:
        if self._config_violation:
            raise AcpError(MODE_SWITCH_ATTEMPT, "agent changed a protected session configuration value")

    def _record_permission(
        self,
        tool_call: ToolCallUpdate,
        option_id: str | None,
        violation: str | None,
    ) -> None:
        capture = self._capture
        if capture is None:
            return
        capture.permission_events.append(
            {
                "tool_call_id": getattr(tool_call, "tool_call_id", None),
                "title": getattr(tool_call, "title", None),
                "kind": getattr(tool_call, "kind", None),
                "option_id": option_id,
                "violation": violation,
            }
        )
        if violation is not None:
            capture.violations.append(violation)

    # -- sessions ----------------------------------------------------------------------------

    async def authenticate(self, method_id: str, *, timeout: float = 60.0) -> None:
        """Run an explicitly requested authentication operation with a bounded lifetime.

        Background workers must check cached authentication instead of using this to start a
        browser login. The interactive CLI is the caller for personal OAuth sign-in.
        """
        if self.init is None or method_id not in self.init.auth_method_ids:
            raise AcpError("ACP_AUTH_FAILED", f"agent does not offer authentication method {method_id!r}")
        try:
            await asyncio.wait_for(self._connection().authenticate(method_id=method_id), timeout=timeout)
        except Exception as exc:
            raise AcpError(
                "ACP_AUTH_FAILED", f"authentication failed: {exc}", cause=error_cause(exc)
            ) from exc

    def _remember_session_configuration(self, response: Any) -> None:
        self.session_model = _claude_config_model(response)
        self.session_config_options = _config_options(response)

    async def new_session(self, **session_kwargs: Any) -> str:
        """Create a session in the worker's cwd. Extra kwargs travel as the request's ``_meta``."""
        try:
            response = await asyncio.wait_for(
                self._connection().new_session(cwd=str(self._cwd), **session_kwargs),
                timeout=self._handshake_timeout,
            )
        except Exception as exc:
            raise AcpError(
                "ACP_SESSION_FAILED", f"session/new failed: {exc}", cause=error_cause(exc)
            ) from exc
        session_id = getattr(response, "session_id", None)
        if not isinstance(session_id, str) or not session_id.strip():
            raise AcpError("ACP_SESSION_FAILED", "session/new returned no usable session identifier")
        self._remember_session_configuration(response)
        self._sessions.append(session_id)
        return session_id

    async def load_session(self, session_id: str, **session_kwargs: Any) -> None:
        """Resume a prior session, discarding whatever the agent replays.

        The replay is history the caller already has; counting it is enough to prove the resume
        landed. Because capture is synchronous, every replayed update is seen before this returns.
        """
        if self.init is None or self.init.load_session is not True:
            raise AcpError("RESUME_UNAVAILABLE", "agent does not support session/load")
        replay = TurnCapture()
        self._capture = replay
        try:
            response = await asyncio.wait_for(
                self._connection().load_session(cwd=str(self._cwd), session_id=session_id, **session_kwargs),
                timeout=self._handshake_timeout,
            )
            self._remember_session_configuration(response)
        except Exception as exc:
            raise AcpError(
                "RESUME_UNAVAILABLE", f"session/load failed: {exc}", cause=error_cause(exc)
            ) from exc
        finally:
            self._capture = None
        self.replay_update_count = replay.raw_update_count
        self._sessions.append(session_id)

    async def set_mode(self, session_id: str, mode_id: str) -> None:
        """Put the session in ``mode_id`` (ACP ``session/set_mode``); the agent must offer it."""
        try:
            await asyncio.wait_for(
                self._connection().set_session_mode(session_id=session_id, mode_id=mode_id),
                timeout=self._handshake_timeout,
            )
        except Exception as exc:
            raise AcpError(
                "MODE_UNAVAILABLE",
                f"the agent refused session mode {mode_id!r}: {exc}",
                cause=error_cause(exc),
            ) from exc
        self.mode = mode_id

    async def set_config_option(self, session_id: str, config_id: str, value: str) -> None:
        """Confirm the selection and preserve every previously selected protected value."""
        self._require_configuration_intact()
        previous = self._expected_config
        # A server can publish the approved selection before returning its response.
        self._expected_config = {**previous, config_id: value}
        try:
            response = await asyncio.wait_for(
                self._connection().set_config_option(session_id=session_id, config_id=config_id, value=value),
                timeout=self._handshake_timeout,
            )
            self._require_configuration_intact()
            options = _config_options(response)
            self._observe_configuration({"sessionUpdate": "config_option_update", "configOptions": options})
            self._require_configuration_intact()
            for key, expected in self._expected_config.items():
                selected = [option for option in options if option.get("id") == key]
                if len(selected) != 1 or selected[0].get("currentValue") != expected:
                    raise ValueError(f"agent did not confirm protected configuration value {key!r}")
        except BaseException as exc:
            self._expected_config = previous
            if not isinstance(exc, Exception):
                raise
            raise AcpError(
                "CONFIG_UNAVAILABLE", f"agent refused {config_id!r} = {value!r}: {exc}",
                cause=error_cause(exc),
            ) from exc
        self.session_config_options = options

    async def prompt(self, session_id: str, text: str, *, timeout: float) -> TurnResult:
        """Send one turn and capture everything it produced."""
        capture = TurnCapture()
        self.last_result = None
        self._capture = capture
        stop_reason = "error"
        response = None
        try:
            self._require_configuration_intact()
            response = await asyncio.wait_for(
                self._connection().prompt(session_id=session_id, prompt=[text_block(text)]),
                timeout=timeout,
            )
            stop_reason = response.stop_reason
            await self._await_late_updates(capture)
            self._require_configuration_intact()
        except TimeoutError as exc:
            self._require_configuration_intact()
            stop_reason = "timeout"
            await self.cancel(session_id)
            raise AcpError("TURN_TIMEOUT", f"turn exceeded {timeout}s") from exc
        except Exception as exc:
            self._require_configuration_intact()
            raise AcpError(
                "ACP_TURN_ERROR", f"session/prompt failed: {exc}", cause=error_cause(exc)
            ) from exc
        except BaseException:
            stop_reason = "cancelled"
            await self.cancel(session_id)
            raise
        finally:
            if self._config_violation:
                stop_reason = "error"
                if MODE_SWITCH_ATTEMPT not in capture.violations:
                    capture.violations.append(MODE_SWITCH_ATTEMPT)
            self.last_result = TurnResult(
                stop_reason=stop_reason, text="".join(capture.text), capture=capture,
                usage=_usage_data(getattr(response, "usage", None)),
            )
            self._capture = None
        return self.last_result

    async def _await_late_updates(self, capture: TurnCapture) -> None:
        """Keep the capture open a little after the response, until the turn summary lands."""
        if self._late_update_grace <= 0 or capture.turn_completed is not None:
            return
        deadline = asyncio.get_running_loop().time() + self._late_update_grace
        while capture.turn_completed is None:
            if self._config_violation:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.05, remaining))

    async def cancel(self, session_id: str) -> None:
        """Ask the agent to stop the current turn. Bounded, and never raises."""
        conn = self._conn
        if conn is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.cancel(session_id=session_id), timeout=_CANCEL_TIMEOUT)

    def _connection(self) -> Any:
        if self._conn is None:
            raise AcpError("ACP_TURN_ERROR", "worker is not running")
        return self._conn


def _is_session_update(method: Any, update: Any) -> bool:
    """``session/update``, or a vendor notification shaped like one.

    ACP lets an agent prefix a method with ``_<vendor>/`` for what the spec does not cover. Grok
    1.0.13 sends its ``turn_completed`` update -- the one carrying the turn's token usage -- as
    ``_x.ai/session_notification`` with the same ``{sessionId, update: {sessionUpdate: ...}}``
    params (verified on the wire; its own session log names the method differently), and a client
    that only listens for the plain method never sees it.
    """
    if method == "session/update":
        return True
    return (
        isinstance(method, str)
        and method.startswith("_")
        and isinstance(update, dict)
        and isinstance(update.get("sessionUpdate"), str)
    )


def _note_model_id(capture: TurnCapture, update: Mapping[str, Any], params: Any) -> None:
    """Remember a model id an update carried, on the update or on the notification."""
    for holder in (update, params if isinstance(params, dict) else {}):
        meta = holder.get("_meta")
        model_id = meta.get("modelId") if isinstance(meta, dict) else None
        if isinstance(model_id, str) and model_id and model_id not in capture.model_ids:
            capture.model_ids.append(model_id)


def _claude_config_model(response: Any) -> str | None:
    """Read canonical ids only: the adapter may describe ``default`` with a display name."""
    for option in getattr(response, "config_options", None) or []:
        if getattr(option, "id", None) != "model":
            continue
        selected = getattr(option, "current_value", None)
        if isinstance(selected, str) and _CLAUDE_MODEL_ID.fullmatch(selected):
            return selected
        if selected == "default":
            for choice in getattr(option, "options", []):
                description = getattr(choice, "description", None)
                if (
                    getattr(choice, "value", None) == selected
                    and isinstance(description, str)
                    and _CLAUDE_MODEL_ID.fullmatch(description)
                ):
                    return description
    return None


def _config_options(response: Any) -> list[dict[str, Any]]:
    """Keep only well-shaped select/boolean options; callers validate their required options."""
    raw = getattr(response, "config_options", None)
    if not isinstance(raw, list):
        return []
    result = []
    for option in raw:
        if hasattr(option, "model_dump"):
            option = option.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(option, dict) and isinstance(option.get("id"), str):
            result.append(dict(option))
    return result


def _usage_data(usage: Any) -> dict[str, Any] | None:
    """The prompt response's usage as plain snake_case data, whatever the SDK salvaged it into."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    if hasattr(usage, "model_dump"):
        return usage.model_dump(mode="json", by_alias=False, exclude_none=True)
    return None


def _capabilities() -> ClientCapabilities:
    return ClientCapabilities(
        fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
        terminal=False,
    )


def _init_info(response: Any) -> InitInfo:
    """Read the initialize response defensively: the SDK salvages bad payloads into raw dicts."""
    capabilities = getattr(response, "agent_capabilities", None)
    if isinstance(capabilities, dict):
        raw_load = capabilities.get("loadSession", capabilities.get("load_session"))
    else:
        raw_load = getattr(capabilities, "load_session", None)
    load_session = raw_load if isinstance(raw_load, bool) else None

    method_ids: list[str] = []
    for method in getattr(response, "auth_methods", None) or []:
        raw_id = method.get("id") if isinstance(method, dict) else getattr(method, "id", None)
        if isinstance(raw_id, str):
            method_ids.append(raw_id)

    info = getattr(response, "agent_info", None)
    if isinstance(info, dict):
        agent_info = dict(info)
    elif info is not None and hasattr(info, "model_dump"):
        agent_info = info.model_dump(mode="json", by_alias=True, exclude_none=True)
    else:
        agent_info = {}
    return InitInfo(load_session=load_session, auth_method_ids=tuple(method_ids), agent_info=agent_info)
