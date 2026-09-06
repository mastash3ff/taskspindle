"""Filesystem and static tool policy for the pinned native AGY CLI.

This launcher must run inside TaskSpindle's control-group-killed systemd unit.
The CLI and its descendants inherit a private mount/PID namespace. Native token
refresh is deliberately not writable: expiration requires an interactive native
CLI login. No credentials are read or copied here.

Native commands are always disabled; TaskSpindle executes verification commands.
Writable directory scopes permit new files. Existing file scopes permit in-place
edits only (atomic replacement of a bind-mounted file fails). Missing scopes fail
closed; callers must declare an existing containing directory for new files.
Existing control paths are protected by mounts. Future control paths also depend
on immutable native deny rules, because mounting absent paths would mutate the
worktree. The native implicit manage_task tool still requires runtime monitoring.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
from collections.abc import Sequence
from pathlib import Path

READ_TOOLS = ("view_file", "grep_search", "list_dir", "find_by_name")
EDIT_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content")
CONTROL_DIRS = frozenset({".agents", ".agent", "_agents", "_agent", ".gemini", ".claude", ".codex"})
CONTROL_FILES = frozenset({"AGENTS.md", "GEMINI.md"})


def _directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"launch directory must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"launch directory is not a directory: {path}")
    return path


def _file(path: Path, content: str | None = None) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"launch control must be a regular file: {path}")
    if content is not None:
        path.write_text(content, encoding="utf-8")
    elif not path.exists():
        path.touch(mode=0o600)
    return path


def _scopes(workspace: Path, mode: str, prefixes: Sequence[str]) -> list[Path]:
    if mode != "implement":
        return []
    if not prefixes:
        raise ValueError("implement requires existing writable path prefixes")
    scopes = []
    for value in prefixes:
        relative = Path(value)
        if not value or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid writable path prefix: {value}")
        if any(part in CONTROL_DIRS | CONTROL_FILES | {".git"} for part in relative.parts):
            raise ValueError(f"protected writable path prefix: {value}")
        path = workspace / relative
        if path.resolve() != path:
            raise ValueError(f"writable path prefix traverses a symlink: {value}")
        if not path.exists():
            raise ValueError(f"path prefix must exist; declare its existing containing directory: {value}")
        if not (path.is_dir() or path.is_file()):
            raise ValueError(f"path prefix is not a regular file or directory: {value}")
        # A writable hardlink can mutate a protected inode through another path,
        # despite a read-only mount on that other path. Git checkouts need none.
        files = [path] if path.is_file() else (
            Path(root) / name for root, _, names in os.walk(path, followlinks=False) for name in names
        )
        for candidate in files:
            if not candidate.is_symlink() and candidate.is_file() and candidate.stat().st_nlink > 1:
                raise ValueError(f"writable scope contains a hardlink: {candidate}")
        scopes.append(path)
    return sorted(set(scopes), key=lambda path: (len(path.parts), str(path)))


def _controls(workspace: Path) -> list[Path]:
    paths = []
    for root, directories, files in os.walk(workspace, followlinks=False):
        for name in list(directories):
            if name in CONTROL_DIRS or name == ".git":
                paths.append(Path(root) / name)
                directories.remove(name)
        paths.extend(Path(root) / name for name in files if name in CONTROL_FILES or name == ".git")
    return paths


def prepare_launch(
    binary: Path,
    workspace: Path,
    task_dir: Path,
    home: Path,
    mode: str,
    allowed_prefixes: Sequence[str],
    verification_commands: Sequence[str],
) -> tuple[str, ...]:
    """Prepare persistent private state and return argv before model/stream flags.

    ``agy-cli-launch.json`` provides non-secret policy metadata to diagnostics.
    API keys and inherited CLI settings/environment never enter this namespace.
    No shell is used to interpret any argument.
    """
    from .agy_cli_adapter import require_cached_token
    from .providers import AGY_PIN_ENV, ProfileError

    if mode not in {"consult", "review", "implement"}:
        raise ValueError(f"unsupported native AGY mode: {mode}")
    binary, workspace, task_dir, home = (path.resolve() for path in (binary, workspace, task_dir, home))
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("native AGY binary must be an existing executable")
    if not workspace.is_dir() or not home.is_dir():
        raise ValueError("workspace and native authentication home must exist")
    if task_dir == workspace or task_dir.is_relative_to(workspace):
        raise ValueError("private launch state must be outside the model workspace")
    scopes = _scopes(workspace, mode, allowed_prefixes)
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise ValueError("native AGY isolation requires bubblewrap")
    try:
        credential = require_cached_token(home)
    except ProfileError as exc:
        raise ValueError(str(exc)) from exc

    _directory(task_dir)
    policy = _directory(task_dir / "agy-cli-policy")
    empty = _directory(policy / "empty")
    blank = _file(policy / "empty-file", "")
    state = _directory(task_dir / "agy-cli-state")
    gemini = _directory(policy / "gemini")
    _directory(gemini / "antigravity-cli")
    _directory(gemini / "config")
    cli = _directory(state / "cli")
    _directory(cli / "bin")
    _file(cli / "settings.json")
    _file(cli / credential.name)
    projects = _directory(state / "projects")
    config = _directory(policy / "config")
    _directory(config / "projects")
    agents = _directory(config / "agents")
    agent = f"spindle-{mode}"
    tools = READ_TOOLS + (EDIT_TOOLS if mode == "implement" else ())
    _file(agents / f"{agent}.md", "\n".join([
        "---", f"name: {agent}", "description: TaskSpindle scoped worker",
        "mainAgent: true", "subagent: false", f"tools: {json.dumps(tools)}",
        "commandExecutionPolicy: off", "mcpServers: []", "skills: []", "plugins: []", "---",
        "Use only the declared tools within the assigned workspace. Do not delegate or change controls.",
        "TaskSpindle runs verification commands outside the model process.", "",
    ]))
    deny = ["command(*)", "unsandboxed(*)", "mcp(*)", "read_url(*)", "execute_url(*)"]
    for protected in (home / ".gemini", state, policy):
        deny.append(f"read_file({protected})")
    # Native file rules match a literal path recursively, not shell globs. Protect
    # each existing directory's control paths; newly created trees are masked on
    # the next launch before a resumed conversation can discover their controls.
    roots = [Path(root) for root, directories, _ in os.walk(workspace, followlinks=False)
             if not any(part in CONTROL_DIRS | {".git"} for part in Path(root).relative_to(workspace).parts)]
    for root in roots:
        deny.extend(f"write_file({root / name})" for name in sorted(CONTROL_DIRS | CONTROL_FILES | {".git"}))
    if mode != "implement":
        deny.append("write_file(*)")
    allow = [f"read_file({workspace})"]
    allow.extend(f"write_file({path})" for path in scopes)
    settings = _file(policy / "settings.json", json.dumps({
        "artifactReviewPolicy": "asks-for-review",
        "enableTerminalSandbox": True, "allowNonWorkspaceAccess": False,
        "useG1Credits": False, "permissions": {"deny": deny, "ask": [], "allow": allow},
    }, indent=2) + "\n")

    # The transport creates the process group; a second session here would hide
    # native descendants from its SIGINT/SIGTERM cancellation sequence.
    argv = [bwrap, "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev",
            "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
            "--cap-drop", "ALL", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]

    def mount(option: str, source: Path, destination: Path) -> None:
        argv.extend((option, str(source), str(destination)))

    # Explicit binds keep test/ephemeral paths usable even when hosted below /tmp.
    for path in (home, task_dir, workspace):
        mount("--ro-bind", path, path)
    for path in scopes:
        mount("--bind", path, path)
    for path in _controls(workspace):
        if path.is_symlink():
            raise ValueError(f"workspace control path must not be a symlink: {path}")
        source = path if path.name == ".git" else empty if path.is_dir() else blank
        mount("--ro-bind", source, path)
    global_agents = home / ".agents"
    if global_agents.exists():
        if global_agents.is_symlink():
            raise ValueError("global agents directory must not be a symlink")
        mount("--ro-bind", empty, global_agents)
    native_home = home / ".gemini"
    mount("--ro-bind", gemini, native_home)
    mount("--bind", cli, native_home / "antigravity-cli")
    mount("--ro-bind", config, native_home / "config")
    mount("--bind", projects, native_home / "config" / "projects")
    mount("--ro-bind", settings, native_home / "antigravity-cli" / "settings.json")
    mount("--ro-bind", credential, credential)
    pinned_bin = binary.parent / "bin"
    if pinned_bin.is_dir():
        mount("--ro-bind", pinned_bin, native_home / "antigravity-cli" / "bin")
    argv.extend(("--clearenv", "--setenv", "HOME", str(home), "--setenv", "USER",
                 pwd.getpwuid(os.getuid()).pw_name, "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
                 "--setenv", "LANG", "C.UTF-8"))
    for name, value in AGY_PIN_ENV.items():
        argv.extend(("--setenv", name, value))
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        if os.environ.get(name):
            argv.extend(("--setenv", name, os.environ[name]))
    argv.extend(("--chdir", str(workspace), "--", str(binary), "--agent", agent,
                 "--mode", "accept-edits" if mode == "implement" else "plan",
                 "--sandbox", "--disable-slash-commands", "--add-dir", str(workspace)))
    _file(task_dir / "agy-cli-launch.json", json.dumps({
        "mode": mode, "binary": str(binary), "workspace": str(workspace),
        "writable_scopes": [str(path) for path in scopes], "private_state": str(state),
        "credential_refresh": "read-only; reauthenticate with native CLI if expired",
        "verification_commands_external": len(verification_commands), "shell_tools": False,
        "declared_tools": list(tools), "implicit_tools_require_monitoring": ["manage_task"],
        "future_control_paths_require_native_deny_rules": True,
        "pinned_companion_bin": str(pinned_bin) if pinned_bin.is_dir() else None,
    }, indent=2) + "\n")
    return tuple(argv)
