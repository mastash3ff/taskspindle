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

It never logs in, never reads or copies a credential, and never edits Codex's configuration.
Authentication belongs to the provider CLIs, and registering the MCP server is a command the
operator runs themselves -- both are documented rather than automated, because a tool that
silently rewrites ``~/.codex/config.toml`` is a tool you cannot audit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import taskspindle

from .config import Paths

__all__ = ["ADAPTER_BIN", "EXAMPLE_CONFIG", "MANIFESTS", "NPM_ARGS", "SetupError", "install_runtime"]

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
# Every key is optional. With no configuration at all TaskSpindle offers the two first-class
# providers -- `claude` and `grok` -- both OAuth-only, and that is the supported setup.
#
# A [providers.<id>] table adds a second-class profile. Second-class is a rule, not a label:
# such a profile is never a default, never a fallback, and never chosen as a reviewer on its
# own. A task uses it only when start_task names it and the repository grant lists that exact
# id, and `auth = "api_key"` additionally requires start_task(allow_metered = true).
#
# Keys:
#   base        "claude" or "grok" -- inherit that built-in's launch command and quirks.
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
