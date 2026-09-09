"""Installing the pinned adapter runtime, and nothing else.

``taskspindle setup`` puts one Node package on disk: the ``claude-agent-acp`` build the ``claude``
profile launches, at the exact version :data:`taskspindle.ADAPTER_VERSION` pins. It copies the
packaged ``package.json`` and ``package-lock.json`` into a versioned runtime directory and runs
``npm ci`` there, so the tree that ends up installed is the one the lock file describes and not
whatever the registry offers today.

It also resolves the ``node`` on the setup operator's own PATH, records its absolute path, and
rewrites ``node_modules/.bin/claude-agent-acp`` from the ``npm``-linked symlink (shebang
``#!/usr/bin/env node``) into a shim that execs that node directly. A worker unit's PATH is
short and has no ``node`` on it at all; the shim needs none.

``taskspindle setup --provider agy`` pins a verified copy of the installed native Linux CLI.
Setup never logs in, reads or copies a credential, or edits Codex's configuration. Antigravity
login stays with the terminal CLI; ``taskspindle auth agy`` checks its cached login.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import taskspindle

from .config import Paths

__all__ = [
    "ADAPTER_BIN", "EXAMPLE_CONFIG", "MANIFESTS", "NPM_ARGS", "SetupError",
    "install_agy_runtime", "install_runtime",
]

#: The oldest Node the pinned adapter is supported on.
MIN_NODE_MAJOR = 22

#: The two files copied out of the package into the runtime directory.
MANIFESTS: tuple[str, ...] = ("package.json", "package-lock.json")

#: The executable ``npm ci`` is expected to link into ``node_modules/.bin``.
ADAPTER_BIN = "claude-agent-acp"

#: The install is reproducible, offline-friendly and runs no package scripts.
NPM_ARGS: tuple[str, ...] = ("ci", "--ignore-scripts", "--no-audit", "--no-fund")

#: Names ``npm`` is allowed to see. A registry token or a proxy setting is not one of them.
_ENV_NAMES: tuple[str, ...] = ("PATH", "HOME", "LANG")

#: How long the install may take before it is abandoned.
_TIMEOUT = 900.0

#: Written to ``config.toml`` on a first setup, and to ``examples/config.toml`` in the repository.
EXAMPLE_CONFIG = """\
# TaskSpindle configuration.
#
# Every key is optional. Built-in providers are `claude`, `grok`, and `agy`, all OAuth-only.
# AGY uses a separately pinned native CLI and its existing personal login; see docs/antigravity.md.
#
# Concurrent turns per provider default to one when omitted. Uncomment only after qualifying
# useful overlapping jobs on this host. All MCP clients sharing the state database share these
# limits; four each permits at most twelve external turns, not twelve per client.
#[concurrency]
#claude = 4
#grok = 4
#agy = 4
#
# Native extra usage is separate from API-key authentication. Every exact profile defaults
# to observe_only. Provider-managed is standing authorization to use native paid allowance
# after included allowance exhaustion; provider account settings control spending.
# Observe-only is NOT a strict no-charge gate for Claude/Grok accounts with extra usage enabled.
# See docs/native-overage.md. These commented examples do not enable spending.
#[native_overage]
#claude = "observe_only"
#grok = "observe_only"
#agy = "observe_only"
#
# A [providers.<id>] table adds a second-class profile. Second-class is a rule, not a label:
# such a profile is never a default, never a fallback, and never chosen as a reviewer on its
# own. A task uses it only when start_task names it and the repository grant lists that exact
# id, and `auth = "api_key"` additionally requires start_task(allow_metered = true).
#
# Keys:
#   base        "claude", "grok", or "agy" -- inherit its launch command and quirks.
#   auth        "oauth" or "api_key". Required.
#   command     argv of an ACP stdio agent. Required unless `base` supplies one.
#   env         plain environment names and values passed to the agent.
#   secret_env  names only, for api_key profiles; values are read from the parent
#               environment at launch and are never stored, logged or returned.
#   model       model id, recorded on every task for attribution.
#   effort      reasoning effort, where the agent understands one.
#   modes       narrows the modes this profile may serve; defaults to all three.
#
# Neither example below was tested live in v0.1.0.

# A Claude adapter pointed at a local LiteLLM gateway. The gateway host is recorded on every
# task so metered work is visible in task_result; the token itself is not.
#[providers.claude-litellm]
#base = "claude"
#auth = "api_key"
#model = "kimi"
#modes = ["consult", "review"]
#env = { ANTHROPIC_BASE_URL = "http://127.0.0.1:4000" }
#secret_env = ["ANTHROPIC_AUTH_TOKEN"]

# A generic ACP agent that is not derived from either built-in, so it declares its own argv.
#[providers.opencode]
#auth = "api_key"
#command = ["opencode", "acp"]
#modes = ["consult", "review"]
#secret_env = ["OPENAI_API_KEY"]
"""

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class SetupError(Exception):
    """The runtime could not be installed. The message is the reason, fit to print."""


def _manifest_bytes(name: str) -> bytes:
    """Read one packaged adapter manifest, from a wheel or from a source checkout alike."""
    source = resources.files("taskspindle").joinpath("_adapter", name)
    try:
        return source.read_bytes()
    except OSError as exc:
        raise SetupError(f"the packaged {name} is missing from this installation: {exc}") from exc


def _private(path: Path) -> Path:
    """Create ``path`` and its parents, readable only by this user."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except OSError as exc:
        raise SetupError(f"could not create {path}: {exc}") from exc
    return path


def _ensure_parent(path: Path) -> None:
    """Create the directory ``path`` will live in, and leave one that already exists alone.

    The configuration file is the one location the operator can point somewhere of their own
    choosing with ``TASKSPINDLE_CONFIG``, so its parent may well be a directory that is not
    TaskSpindle's -- a home directory, say. Creating it privately is right; re-permissioning one
    that was already there is not.
    """
    parent = path.parent
    if parent.is_dir():
        return
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise SetupError(f"could not create {parent}: {exc}") from exc


def npm_env(parent: Mapping[str, str]) -> dict[str, str]:
    """The environment ``npm ci`` runs under: enough to find node, and nothing else."""
    env = {name: parent[name] for name in _ENV_NAMES if name in parent}
    if not env.get("PATH"):
        env["PATH"] = os.defpath
    return env


def _run_npm(runtime_dir: Path, npm: str, runner: Runner, parent_env: Mapping[str, str]) -> None:
    command = [npm, *NPM_ARGS]
    try:
        proc = runner(
            command,
            cwd=str(runtime_dir),
            env=npm_env(parent_env),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SetupError(f"{npm} is not installed or not on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupError(f"{npm} {' '.join(NPM_ARGS)} did not finish within {_TIMEOUT:.0f}s") from exc
    except OSError as exc:
        raise SetupError(f"could not run {npm}: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else "no output"
        raise SetupError(f"{npm} {NPM_ARGS[0]} exited {proc.returncode}: {detail}")


def _verify_adapter(runtime_dir: Path) -> str:
    """Confirm the installed adapter is the pinned one and that its executable is linked."""
    manifest = runtime_dir / "node_modules" / taskspindle.ADAPTER_PACKAGE / "package.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SetupError(f"{taskspindle.ADAPTER_PACKAGE} was not installed: {exc}") from exc
    except ValueError as exc:
        raise SetupError(f"{manifest} is not valid JSON: {exc}") from exc
    found = str(payload.get("version", ""))
    if found != taskspindle.ADAPTER_VERSION:
        raise SetupError(
            f"{taskspindle.ADAPTER_PACKAGE} installed at {found or 'no version'}, "
            f"not the pinned {taskspindle.ADAPTER_VERSION}"
        )
    executable = runtime_dir / "node_modules" / ".bin" / ADAPTER_BIN
    if not executable.exists():
        raise SetupError(f"{executable} is missing after a successful install")
    return found


def _resolve_node() -> Path:
    """Find the node the setup operator has on PATH, resolved past any symlink.

    This deliberately looks at the setup process's own PATH rather than the restricted one a
    worker unit runs with -- an interactive shell or a login profile is where ``nvm`` and
    friends put ``node``, and the whole point of pinning it here is that a worker never has to
    find it again on its own.
    """
    found = shutil.which("node")
    if not found:
        raise SetupError("node is not on PATH; install Node 22+ before running setup")
    return Path(found).resolve()


def _verify_node(node: Path) -> str:
    """Run the resolved node and confirm it reports a supported version."""
    try:
        proc = subprocess.run(
            [str(node), "--version"], capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except FileNotFoundError as exc:
        raise SetupError(f"{node} could not be run: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupError(f"{node} --version did not finish within {_TIMEOUT:.0f}s") from exc
    except OSError as exc:
        raise SetupError(f"could not run {node}: {exc}") from exc
    reported = (proc.stdout or "").strip()
    if proc.returncode != 0 or not reported.startswith("v"):
        raise SetupError(f"{node} --version did not report a usable version: {reported or 'no output'}")
    try:
        major = int(reported[1:].split(".", 1)[0])
    except ValueError as exc:
        raise SetupError(f"{node} --version reported {reported!r}, which is not a version") from exc
    if major < MIN_NODE_MAJOR:
        raise SetupError(f"node {reported} is older than the required v{MIN_NODE_MAJOR}")
    return reported


def _write_node_path(runtime_dir: Path, node: Path) -> Path:
    """Record the pinned node next to the runtime, mode 0600, for :func:`providers.pinned_node`."""
    path = runtime_dir / "node-path"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{node}\n")
    path.chmod(0o600)
    return path


def _write_launcher_shim(runtime_dir: Path, node: Path) -> Path:
    """Replace the npm-linked launcher with a shim that execs the pinned node directly.

    ``npm ci`` links ``node_modules/.bin/claude-agent-acp`` as a symlink to the adapter's entry
    point, whose shebang is ``#!/usr/bin/env node`` -- fine in an interactive shell, useless
    under the restricted PATH a worker unit runs with. The shim hardcodes the node this setup
    just resolved, so the launcher needs no ``node`` on PATH at all. Idempotent: a rerun removes
    whatever is there, symlink or an earlier shim, and writes a fresh one.
    """
    entry = runtime_dir / "node_modules" / taskspindle.ADAPTER_PACKAGE / "dist" / "index.js"
    launcher = runtime_dir / "node_modules" / ".bin" / ADAPTER_BIN
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if launcher.is_symlink() or launcher.exists():
        launcher.unlink()
    launcher.write_text(f'#!/bin/sh\nexec "{node}" "{entry}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def _write_config(config_file: Path) -> bool:
    """Write the example configuration, unless the operator already has one.

    An existing file is never touched, whatever is in it: a config that TaskSpindle overwrote
    would be a config nobody could trust to survive an upgrade.
    """
    if config_file.exists():
        return False
    _ensure_parent(config_file)
    try:
        fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        raise SetupError(f"could not write {config_file}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(EXAMPLE_CONFIG)
    return True


def install_runtime(
    paths: Paths,
    *,
    npm: str = "npm",
    runner: Runner = subprocess.run,
    parent_env: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Install the pinned adapter into ``paths.runtime_dir`` and lay out the other directories.

    Returns what was done: the runtime directory, the adapter version now installed, the node
    pinned into the launcher, the configuration file, and whether this call is the one that
    created it.
    """
    runtime_dir = _private(paths.runtime_dir)
    for name in MANIFESTS:
        (runtime_dir / name).write_bytes(_manifest_bytes(name))

    _run_npm(runtime_dir, npm, runner, parent_env)
    adapter_version = _verify_adapter(runtime_dir)

    node = _resolve_node()
    _verify_node(node)
    _write_node_path(runtime_dir, node)
    _write_launcher_shim(runtime_dir, node)

    _private(paths.state_dir)
    _private(paths.data_dir)
    created = _write_config(paths.config_file)

    return {
        "runtime_dir": str(runtime_dir),
        "adapter_version": adapter_version,
        "node": str(node),
        "config_file": str(paths.config_file),
        "created_config": created,
    }


def _download_agy_archive(url: str, target: Path) -> None:
    """Fetch the pinned Google distribution without inheriting proxy or credential settings."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as response, target.open("wb") as output:
        if response.geturl() != url:
            raise SetupError("the pinned Antigravity download redirected unexpectedly")
        shutil.copyfileobj(response, output)


def install_agy_acp_runtime(
    paths: Paths,
    *,
    downloader: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    """Install Google's separately pinned Linux ACP server; leave the terminal CLI alone."""
    from .agy_adapter import (
        ADAPTER_FILES,
        ADAPTER_ID,
        ADAPTER_VERSION,
        DOWNLOAD_URL,
        adapter_command,
        prepare_agy_home,
    )
    from .providers import ProfileError

    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise SetupError("Antigravity ACP 1.1.1 setup currently supports Linux x86-64 only")
    runtime_dir = _private(paths.runtime_dir)
    destination = runtime_dir / ADAPTER_ID / ADAPTER_VERSION
    installed = destination.is_dir() and not destination.is_symlink() and all(
        (destination / name).is_file() and not (destination / name).is_symlink()
        and (destination / name).stat().st_size > 0 and os.access(destination / name, os.X_OK)
        for name in ADAPTER_FILES
    )
    try:
        if not installed:
            if destination.exists():
                raise SetupError(
                    f"incomplete pinned adapter directory: {destination}; move it aside and rerun setup"
                )
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryDirectory(prefix=".agy-install-", dir=destination.parent) as raw:
                temporary = Path(raw)
                archive = temporary / "adapter.zip"
                (downloader or _download_agy_archive)(DOWNLOAD_URL, archive)
                extracted = temporary / "extracted"
                extracted.mkdir(mode=0o700)
                with zipfile.ZipFile(archive) as bundle:
                    names = bundle.namelist()
                    if len(names) != len(ADAPTER_FILES) or set(names) != set(ADAPTER_FILES):
                        raise SetupError(
                            "the Antigravity archive does not contain exactly the expected executables"
                        )
                    for name in ADAPTER_FILES:
                        member = bundle.getinfo(name)
                        kind = stat.S_IFMT(member.external_attr >> 16)
                        if member.is_dir() or member.file_size == 0 or kind not in (0, stat.S_IFREG):
                            raise SetupError(f"the Antigravity archive has an invalid executable: {name}")
                        target = extracted / name
                        with bundle.open(member) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        target.chmod(0o700)
                extracted.rename(destination)
        _private(paths.state_dir)
        _private(paths.data_dir)
        home = prepare_agy_home(paths.data_dir)
        created = _write_config(paths.config_file)
    except (OSError, ValueError, zipfile.BadZipFile, ProfileError) as exc:
        raise SetupError(f"Antigravity ACP setup failed: {exc}") from exc
    return {
        "provider": "agy", "adapter_package": ADAPTER_ID, "adapter_version": ADAPTER_VERSION,
        "runtime_dir": str(runtime_dir), "adapter_dir": str(destination),
        "command": list(adapter_command(runtime_dir)), "agy_home": str(home),
        "already_installed": installed, "config_file": str(paths.config_file), "created_config": created,
    }


def install_agy_runtime(
    paths: Paths, *, source: Path | None = None, runner: Runner = subprocess.run,
    parent_env: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    """Copy the exact installed native build into the runtime; never copy its credentials."""
    from .agy_cli_adapter import ADAPTER_ID, ADAPTER_VERSION, adapter_command, verify_cli_version
    from .providers import ProfileError

    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise SetupError("Antigravity CLI setup currently supports Linux x86-64 only")
    if not parent_env.get("HOME"):
        raise SetupError("HOME is required to locate the installed Antigravity CLI")
    source = source or Path(parent_env["HOME"]) / ".local/bin/agy"
    destination = paths.runtime_dir / ADAPTER_ID / ADAPTER_VERSION
    binary = destination / "agy"
    installed = destination.exists()
    try:
        for directory in (paths.runtime_dir, destination.parent, destination):
            if directory.is_symlink():
                raise SetupError(f"Antigravity runtime directory must not be a symlink: {directory}")
            if directory.exists() and (not directory.is_dir() or directory.stat().st_uid != os.getuid()):
                raise SetupError(f"Antigravity runtime directory must be owned by this user: {directory}")
        if installed:
            verify_cli_version(binary, runner=runner, parent_env=parent_env)
            companions = destination / "bin"
            if (companions.is_symlink() or not companions.is_dir()
                    or companions.stat().st_uid != os.getuid() or companions.stat().st_mode & 0o022):
                raise SetupError(
                    "incomplete Antigravity CLI pin: companion bin directory is missing or unsafe"
                )
            for companion in companions.iterdir():
                if (companion.is_symlink() or not companion.is_file()
                        or companion.stat().st_uid != os.getuid() or companion.stat().st_mode & 0o022):
                    raise SetupError("Antigravity pinned companions must be regular owned executables")
        else:
            # Verify before touching the destination; an updated daily CLI cannot silently
            # change the build that a qualified TaskSpindle release executes.
            verify_cli_version(source, runner=runner, parent_env=parent_env)
            _private(paths.runtime_dir)
            _private(destination.parent)
            with tempfile.TemporaryDirectory(prefix=".agy-cli-install-", dir=destination.parent) as raw:
                staging = Path(raw) / ADAPTER_VERSION
                staging.mkdir(mode=0o700)
                target = staging / "agy"
                shutil.copyfile(source, target)
                target.chmod(0o700)
                verify_cli_version(target, runner=runner, parent_env=parent_env)
                companion_source = Path(parent_env["HOME"]) / ".gemini/antigravity-cli/bin"
                companion_target = staging / "bin"
                companion_target.mkdir(mode=0o700)
                if companion_source.is_symlink():
                    raise SetupError("Antigravity companion executable directory must not be a symlink")
                if companion_source.exists():
                    for companion in companion_source.iterdir():
                        if (companion.is_symlink() or not companion.is_file()
                                or companion.stat().st_uid != os.getuid()):
                            raise SetupError("Antigravity companion executables must be regular owned files")
                        shutil.copyfile(companion, companion_target / companion.name)
                        (companion_target / companion.name).chmod(0o700)
                staging.rename(destination)
        _private(paths.state_dir)
        _private(paths.data_dir)
        created = _write_config(paths.config_file)
    except (OSError, ProfileError) as exc:
        raise SetupError(f"Antigravity CLI setup failed: {exc}") from exc
    return {
        "provider": "agy", "adapter_package": ADAPTER_ID, "adapter_version": ADAPTER_VERSION,
        "runtime_dir": str(paths.runtime_dir), "adapter_dir": str(destination),
        "command": list(adapter_command(paths.runtime_dir)), "already_installed": installed,
        "config_file": str(paths.config_file), "created_config": created,
    }
