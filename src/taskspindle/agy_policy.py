"""Permission contract for the pinned official Antigravity ACP server.

Tool names and presentation fields follow antigravity-acp 1.1.1's tools.py.
Only one-shot decisions are granted. Workspace trust, MCP, delegation and
unrecognized requests never inherit the generic ACP allow behavior.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from acp.schema import PermissionOption, ToolCallUpdate

from .acp_client import (
    DELEGATION_ATTEMPT,
    MODE_SWITCH_ATTEMPT,
    READ_ONLY_VIOLATION,
    PermissionPolicy,
)

_EDITS = frozenset({
    "client_create_file", "client_edit_file", "create_file", "edit_file", "write_file",
    "replace_file_content", "multi_replace_file_content", "write_to_file",
})
_PATH_KEYS = (
    "TargetFile", "target_file", "FilePath", "file_path", "AbsolutePath", "absolute_path", "path",
)


class AgyPermissionPolicy(PermissionPolicy):
    """Confine edits to the worktree and execution to Codex's verification commands."""

    def __init__(self, *, allow_writes: bool, workspace: Path, commands: Sequence[str] = ()) -> None:
        super().__init__(allow_writes=allow_writes)
        self.workspace = workspace.resolve()
        self.commands = frozenset(commands)

    def _inside(self, value: object) -> bool:
        if not isinstance(value, str) or not value:
            return False
        path = Path(value)
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if not path.is_relative_to(self.workspace):
            return False
        return ".git" not in path.relative_to(self.workspace).parts

    def select(
        self, tool_call: ToolCallUpdate, options: Sequence[PermissionOption],
    ) -> tuple[str | None, str | None]:
        kind = getattr(tool_call, "kind", None) or ""
        title = (getattr(tool_call, "title", None) or "").strip()
        name = title.removeprefix("Run ").removesuffix("?")
        violation = "PERMISSION_DENIED"
        if kind == "switch_mode":
            return self._deny(options), MODE_SWITCH_ATTEMPT
        if name in {"start_subagent", "invoke_subagent"}:
            return self._deny(options), DELEGATION_ATTEMPT
        if not self.allow_writes:
            if kind in {"edit", "delete", "move", "execute"}:
                violation = READ_ONLY_VIOLATION
            return self._deny(options), violation
        raw = getattr(tool_call, "raw_input", None)
        meta = getattr(tool_call, "field_meta", None)
        if not isinstance(raw, dict) or (isinstance(meta, dict) and "mcp" in meta):
            return self._deny(options), violation
        allowed = False
        if kind == "edit" and name in _EDITS:
            paths = [raw[k] for k in _PATH_KEYS if k in raw]
            locations = getattr(tool_call, "locations", None) or []
            paths.extend(getattr(location, "path", None) for location in locations)
            allowed = bool(paths) and all(self._inside(path) for path in paths)
        elif kind == "execute":
            command = raw.get("CommandLine") or raw.get("command_line") or raw.get("command")
            cwd = raw.get("Cwd") or raw.get("cwd") or raw.get("WorkingDirectory")
            allowed = isinstance(command, str) and command in self.commands
            allowed = allowed and (cwd is None or self._inside(cwd))
        if allowed:
            option = next((p.option_id for p in options if str(p.kind) == "allow_once"), None)
            if option is not None:
                return option, None
        return self._deny(options), violation
