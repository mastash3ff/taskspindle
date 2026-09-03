"""A scriptable ACP agent, runnable as ``python -m tests.fakes.fake_agent``.

It exists so the ACP client can be exercised end to end -- a real subprocess, real stdio framing,
real permission round trips -- with no model, no network and no credentials. Behaviour comes from
the JSON file named by ``TASKSPINDLE_FAKE_SCRIPT``; with no script it answers ``OK``.

Script keys::

    {"load_session": true,                 # advertised capability
     "session_id": "fake-session-1",
     "response": "text emitted as two agent message chunks",
     "write": {"path": "rel/path", "content": "..."},   # asks permission (kind "edit") first
     "delegate": true,                     # asks permission for an "Agent: spawn subagent" call
     "block_seconds": 30,                  # sleep before responding; cancel -> "cancelled"
     "fail": true,                         # raise a RequestError from prompt
     "malformed_review": true,             # respond with "not json"
     "capture_env_to": "/path/file.json",  # dump os.environ on prompt
     "capture_meta_to": "/path/meta.json", # dump the _meta new_session received
     "replay_count": 2}                    # message chunks emitted during load_session
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

import acp
from acp import RequestError
from acp.helpers import start_tool_call, update_agent_message_text
from acp.schema import (
    AgentCapabilities,
    AuthMethodAgent,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    ToolCallUpdate,
)

DEFAULT_SCRIPT: dict[str, Any] = {"response": "OK"}
_AGENT_INFO = Implementation(name="taskspindle-fake-agent", version="0.0.0")


def load_script() -> dict[str, Any]:
    """Read the script named by ``TASKSPINDLE_FAKE_SCRIPT``, or fall back to answering ``OK``."""
    path = os.environ.get("TASKSPINDLE_FAKE_SCRIPT")
    if not path:
        return dict(DEFAULT_SCRIPT)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _options() -> list[PermissionOption]:
    return [
        PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
        PermissionOption(option_id="allow-always", name="Allow always", kind="allow_always"),
        PermissionOption(option_id="reject-once", name="Reject once", kind="reject_once"),
    ]


def _two_chunks(text: str) -> list[str]:
    half = max(1, len(text) // 2)
    return [text[:half], text[half:]]


class FakeAgent:
    """The ``acp.Agent`` half of the double."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.conn: Any = None
        self._cwds: dict[str, Path] = {}
        self._cancelled: dict[str, asyncio.Event] = {}

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    # -- handshake ---------------------------------------------------------------------------

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(load_session=bool(self.script.get("load_session", False))),
            auth_methods=[AuthMethodAgent(id="cached_token", name="Cached token")],
            agent_info=_AGENT_INFO,
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        session_id = str(self.script.get("session_id", "fake-session-1"))
        self._cwds[session_id] = Path(cwd)
        capture_meta_to = self.script.get("capture_meta_to")
        if capture_meta_to:
            # The router hands a request's ``_meta`` members through as extra keyword arguments.
            meta = {
                name: value
                for name, value in kwargs.items()
                if name not in ("additional_directories", "mcp_servers")
            }
            Path(capture_meta_to).write_text(json.dumps(meta), encoding="utf-8")
        return NewSessionResponse(session_id=session_id)

    async def load_session(self, cwd: str, session_id: str, **kwargs: Any) -> LoadSessionResponse:
        self._cwds[session_id] = Path(cwd)
        for index in range(int(self.script.get("replay_count", 0))):
            await self._emit(session_id, f"replayed {index}")
        return LoadSessionResponse()

    # -- turns -------------------------------------------------------------------------------

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        script = self.script
        capture_env_to = script.get("capture_env_to")
        if capture_env_to:
            Path(capture_env_to).write_text(json.dumps(dict(os.environ)), encoding="utf-8")
        if script.get("fail"):
            raise RequestError.internal_error({"details": "scripted failure"})

        block_seconds = script.get("block_seconds")
        if block_seconds and await self._blocked(session_id, float(block_seconds)):
            return PromptResponse(stop_reason="cancelled")

        write = script.get("write")
        if write:
            await self._maybe_write(session_id, write)
        if script.get("delegate"):
            await self._attempt_delegation(session_id)

        text = "not json" if script.get("malformed_review") else str(script.get("response", "OK"))
        for chunk in _two_chunks(text):
            await self._emit(session_id, chunk)
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._event(session_id).set()

    # -- internals ---------------------------------------------------------------------------

    def _event(self, session_id: str) -> asyncio.Event:
        return self._cancelled.setdefault(session_id, asyncio.Event())

    async def _blocked(self, session_id: str, seconds: float) -> bool:
        """Wait for a cancel; return True if one arrived before the deadline."""
        event = self._event(session_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=seconds)
        return event.is_set()

    async def _emit(self, session_id: str, text: str) -> None:
        await self.conn.session_update(session_id=session_id, update=update_agent_message_text(text))

    async def _ask(self, session_id: str, tool_call_id: str, title: str, kind: str) -> bool:
        await self.conn.session_update(
            session_id=session_id,
            update=start_tool_call(tool_call_id, title, kind=kind, status="pending"),
        )
        response = await self.conn.request_permission(
            session_id=session_id,
            tool_call=ToolCallUpdate(tool_call_id=tool_call_id, title=title, kind=kind),
            options=_options(),
        )
        outcome = response.outcome
        return getattr(outcome, "outcome", None) == "selected" and str(
            getattr(outcome, "option_id", "")
        ).startswith("allow")

    async def _maybe_write(self, session_id: str, write: dict[str, Any]) -> None:
        if not await self._ask(session_id, "tc-write", "Write file", "edit"):
            return
        target = self._cwds.get(session_id, Path.cwd()) / str(write["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(write.get("content", "")), encoding="utf-8")

    async def _attempt_delegation(self, session_id: str) -> None:
        await self._ask(session_id, "tc-delegate", "Agent: spawn subagent", "other")


def main() -> None:
    asyncio.run(acp.run_agent(FakeAgent(load_script())))


if __name__ == "__main__":
    main()
