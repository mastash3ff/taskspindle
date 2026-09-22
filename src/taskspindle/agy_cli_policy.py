"""Filesystem and static tool policy for the native AGY CLI, resolved from PATH.

This launcher must run inside TaskSpindle's control-group-killed systemd unit.
The CLI and its descendants inherit a private mount/PID namespace. Native token
refresh is deliberately not writable: expiration requires an interactive native
CLI login. No credentials are read or copied here.

Native commands are always disabled; TaskSpindle executes verification commands.
Writable directory scopes permit new files. Existing file scopes permit in-place
edits only (atomic replacement of a bind-mounted file fails). Missing scopes fail
closed; callers must declare an existing containing directory for new files.
Existing control paths are masked by mounts. Future control paths also depend
on immutable native deny rules, because mounting absent paths would mutate the
worktree. The native implicit manage_task tool still requires runtime monitoring.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import sys
import sysconfig
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
    from .agy_namespace import mount_table, runtime_sources, validate_source

    broad_roots = {Path(p) for p in ("/", "/etc", "/var", "/opt", "/mnt", "/run",
                                         "/proc", "/sys", "/dev", "/tmp", "/usr", "/home")}
    if workspace in broad_roots or home.is_relative_to(workspace):
        raise ValueError("workspace must not expose a broad home or system runtime")
    inventory = mount_table()
    validate_source(workspace, inventory)
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
    settings = _file(
        policy / "settings.json",
        json.dumps(
            {
                "artifactReviewPolicy": "asks-for-review",
                "enableTerminalSandbox": True,
                "allowNonWorkspaceAccess": False,
                "useG1Credits": False,
                "permissions": {"deny": deny, "ask": [], "allow": allow},
            },
            indent=2,
        )
        + "\n",
    )

    # The transport creates the process group; a second session here would hide
    # native descendants from its SIGINT/SIGTERM cancellation sequence.
    argv = [bwrap, "--proc", "/proc", "--dev", "/dev",
            "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
            "--cap-drop", "ALL", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]
    retained = []

    def mount(option: str, source: Path, destination: Path) -> None:
        validate_source(source, inventory)
        stat = source.stat()
        retained.append({"destination": str(destination), "identity": [stat.st_dev, stat.st_ino],
                         "readonly": option == "--ro-bind", "directory": source.is_dir()})
        argv.extend((option, str(source), str(destination)))

    # Split system runtime directories around ALL nested mounts. A small mount
    # count is a resource bound, never permission to inherit a host mount.
    runtime_roots = [Path(value) for value in (
        "/usr/bin", "/usr/sbin", "/usr/lib64", "/usr/local", "/usr/share",
        "/usr/lib/locale", "/usr/lib/ssl", "/usr/libexec",
        f"/usr/lib/{sysconfig.get_config_var('MULTIARCH')}", sysconfig.get_path("stdlib"),
    )]
    runtime_roots = sorted({path.resolve() for path in runtime_roots if path.exists()})
    runtime_roots = [path for path in runtime_roots
                     if not any(path != other and path.is_relative_to(other) for other in runtime_roots)]
    runtime = [source for root in runtime_roots for source in runtime_sources(root, inventory)]
    if len(runtime) > 64:
        raise ValueError("runtime requires more than 64 explicit mounts")
    for path in runtime:
        if path.is_symlink():
            argv.extend(("--symlink", os.readlink(path), str(path)))
        else:
            mount("--ro-bind", path, path)
    for name in ("bin", "sbin", "lib", "lib64"):
        path = Path("/") / name
        if path.is_symlink():
            argv.extend(("--symlink", os.readlink(path), str(path)))
        elif path.is_dir():
            for source in runtime_sources(path, inventory):
                if source.is_symlink():
                    argv.extend(("--symlink", os.readlink(source), str(source)))
                else:
                    mount("--ro-bind", source, source)
    interpreter = Path(sys.executable).resolve()
    if not interpreter.is_relative_to(Path("/usr")):
        prefix = Path(sys.base_prefix).resolve()
        if (home.is_relative_to(prefix) or task_dir.is_relative_to(prefix)
                or not interpreter.is_relative_to(prefix)):
            raise ValueError("namespace verifier requires an isolated Python runtime prefix")
        mount("--ro-bind", prefix, prefix)
    # Pin the selected executable inode even when its parent runtime is mounted.
    mount("--ro-bind", binary, binary)
    for name in ("resolv.conf", "hosts", "ld.so.cache", "localtime",
                 "ssl/certs/ca-certificates.crt"):
        path = Path("/etc") / name
        if path.is_file():
            mount("--ro-bind", path.resolve(), path)
    nss = _file(policy / "nsswitch.conf", "hosts: files dns\npasswd: files\ngroup: files\n")
    mount("--ro-bind", nss, Path("/etc/nsswitch.conf"))
    # HOME and task ancestors are synthetic. Only explicitly required leaves
    # are exposed, even when the user's installation lives under HOME.
    argv.extend(("--dir", str(home)))
    mount("--ro-bind", workspace, workspace)
    for path in scopes:
        mount("--bind", path, path)
    for path in _controls(workspace):
        if path.is_symlink():
            raise ValueError(f"workspace control path must not be a symlink: {path}")
        # ``.git`` is masked like every other control. A linked worktree's ``.git`` is a
        # pointer file naming the main repository's gitdir, a path outside the workspace that
        # the sandbox then refuses; a model that reads the pointer follows it into that refusal.
        # Nothing inside the namespace runs git: every git command is the host's, before and
        # after the turn.
        mount("--ro-bind", empty if path.is_dir() else blank, path)
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
    # The vendor CLI is resolved from PATH now, not a private copy alongside a companion
    # directory TaskSpindle laid out itself: its real companions live at their native location
    # under the user's own HOME, exactly where an interactive login already put them.
    companion_bin = native_home / "antigravity-cli" / "bin"
    if companion_bin.is_dir():
        if companion_bin.is_symlink():
            raise ValueError("Antigravity companion executable directory must not be a symlink")
        mount("--ro-bind", companion_bin, native_home / "antigravity-cli" / "bin")
    argv.extend(("--clearenv", "--setenv", "HOME", str(home), "--setenv", "USER",
                 pwd.getpwuid(os.getuid()).pw_name, "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
                 "--setenv", "LANG", "C.UTF-8", "--setenv", "SSL_CERT_FILE",
                 "/etc/ssl/certs/ca-certificates.crt"))
    for name, value in AGY_PIN_ENV.items():
        argv.extend(("--setenv", name, value))
    guard = Path(__file__).with_name("agy_namespace.py").resolve()
    mount("--ro-bind", guard, Path("/taskspindle-namespace.py"))
    manifest = _file(policy / "mounts.json")
    # The manifest cannot contain its own inode entry until it exists.
    mount("--ro-bind", manifest, Path("/taskspindle-mounts.json"))
    manifest.write_text(json.dumps({"mounts": retained, "writable_scopes": [str(p) for p in scopes]}),
                        encoding="utf-8")
    argv.extend(("--remount-ro", "/", "--chdir", str(workspace), "--", str(interpreter),
                 "-I", "/taskspindle-namespace.py", "/taskspindle-mounts.json", str(binary),
                 "--agent", agent, "--mode", "accept-edits" if mode == "implement" else "plan",
                 "--sandbox", "--disable-slash-commands", "--add-dir", str(workspace)))
    _file(task_dir / "agy-cli-launch.json", json.dumps({
        "mode": mode, "binary": str(binary), "workspace": str(workspace),
        "host_mount_count": len(inventory), "retained_mount_count": len(retained),
        "runtime_nested_mounts_excluded": [str(p) for p in inventory
                                           if str(p).startswith("/usr/")],
        "namespace_validation": "required before exec", "synthetic_home": True,
        "writable_scopes": [str(path) for path in scopes], "private_state": str(state),
        "credential_refresh": "read-only; reauthenticate with native CLI if expired",
        "verification_commands_external": len(verification_commands), "shell_tools": False,
        "git_masked": True,
        "declared_tools": list(tools), "implicit_tools_require_monitoring": ["manage_task"],
        "future_control_paths_require_native_deny_rules": True,
        "companion_bin": str(companion_bin) if companion_bin.is_dir() else None,
    }, indent=2) + "\n")
    return tuple(argv)


def smoke_launch(argv: Sequence[str]) -> dict:
    """Run the prepared namespace's verifier and stdlib only, with a 10s limit.

    Never invokes the provider, reads tokens, or opens a network connection.
    Timeout cleanup targets only this probe's process group.
    """
    import signal
    import subprocess
    import time
    from contextlib import suppress

    boundary = argv.index("--")
    end = argv.index("/taskspindle-mounts.json", boundary) + 1
    interpreter = argv[boundary + 1]
    code = (
        "import json,pathlib,socket,ssl; "
        "print(json.dumps({'status':'ok','mount_count':"
        "len(pathlib.Path('/proc/self/mountinfo').read_text().splitlines()),"
        "'ca_certificates':len(ssl.create_default_context().get_ca_certs()),"
        "'localhost':bool(socket.getaddrinfo('localhost',443))}))"
    )
    started = time.monotonic()
    process = subprocess.Popen([*argv[:end], interpreter, "-I", "-c", code],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired as exc:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ValueError("AGY namespace smoke exceeded 10 seconds before provider execution") from exc
    if process.returncode:
        raise ValueError(f"AGY namespace smoke failed ({process.returncode}): {stderr.strip()}")
    result = json.loads(stdout)
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result
