"""``taskspindle discover``: which ACP agents from the community registry are already installed.

The registry is the published list at ``cdn.agentclientprotocol.com``. For every entry it names
the launch form -- a platform binary, an npm package, or both -- and from those this module works
out which binary names to look for on this machine. An agent that is found gets a proposed
``[providers.<id>]`` block for ``config.toml``. Nothing is downloaded, nothing is run, and the
configuration file is never written: the proposal is printed for a person to read and paste.

First-class providers are recognized separately, since they are not configured this way.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import taskspindle

from .providers import FIRST_CLASS

__all__ = [
    "DEFAULT_REGISTRY_URL",
    "DiscoveredAgent",
    "RegistryAgent",
    "RegistryError",
    "detect",
    "fetch_registry",
    "parse_registry",
    "proposal",
    "read_registry",
]

#: The canonical published registry, latest snapshot.
DEFAULT_REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"

#: The CDN answers 403 to Python's default User-Agent.
_USER_AGENT = f"taskspindle/{taskspindle.__version__} (+https://github.com/mastash3ff/taskspindle)"

#: Registry ids that map onto TaskSpindle's own first-class providers.
_FIRST_CLASS_REGISTRY_IDS: dict[str, str] = {
    "claude-acp": "claude", "grok-build": "grok", "antigravity-acp": "agy",
    "antigravity-cli": "agy",
}

#: Suffixes an npm package's bare name commonly drops in its installed bin name. ``-acp`` is not
#: among them on purpose: an ``<x>-acp`` package is an adapter whose bin keeps the suffix, and the
#: bare ``<x>`` is usually the underlying CLI, which does not speak ACP (``codex-acp`` vs ``codex``).
_BIN_SUFFIXES = ("-cli", "-code", "-agent")

#: Names that are interpreters or shims rather than agents; a registry entry that resolves to one
#: of these would launch something that is not the agent, so it is never proposed.
_NOT_AGENTS = frozenset(
    {"node", "npx", "npm", "bun", "deno", "python", "python3", "sh", "bash", "zsh", "uv", "uvx"}
)

#: Where CLIs land off PATH.
_EXTRA_BIN_DIRS = (".local/bin", ".cargo/bin", ".bun/bin", "bin")


class RegistryError(Exception):
    """The registry could not be fetched or parsed."""


@dataclass(frozen=True)
class RegistryAgent:
    """One registry entry, reduced to what detection needs."""

    id: str
    name: str
    description: str
    #: ``(bin_name, args)`` launch candidates, best first.
    candidates: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class DiscoveredAgent:
    """A registry agent found on this machine."""

    agent: RegistryAgent
    path: str
    args: tuple[str, ...]

    @property
    def command(self) -> tuple[str, ...]:
        return (self.path, *self.args)


# -- the registry -----------------------------------------------------------------------------


def _platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(
        machine, machine
    )
    return f"{system}-{arch}"


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _bin_basename(cmd: str) -> str:
    tail = cmd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.lower().endswith(".exe") else tail


def _binary_candidates(binary: Any) -> list[tuple[str, tuple[str, ...]]]:
    if not isinstance(binary, dict) or not binary:
        return []
    spec = binary.get(_platform_key())
    if not isinstance(spec, dict):
        return []
    cmd = spec.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        return []
    return [(_bin_basename(cmd), _strings(spec.get("args")))]


def _npx_bin_names(package: str) -> list[str]:
    """Likely installed bin names for an npm package: the bare name and a suffix-stripped form."""
    bare = package.split("@")[-2] if package.startswith("@") else package.split("@")[0]
    bare = bare.rsplit("/", 1)[-1]
    names = [bare]
    for suffix in _BIN_SUFFIXES:
        if bare.endswith(suffix) and len(bare) > len(suffix):
            names.append(bare[: -len(suffix)])
            break
    return names


def _npx_candidates(npx: Any) -> list[tuple[str, tuple[str, ...]]]:
    if not isinstance(npx, dict):
        return []
    package = npx.get("package")
    if not isinstance(package, str) or not package:
        return []
    args = _strings(npx.get("args"))
    return [(name, args) for name in _npx_bin_names(package)]


def _parse_agent(entry: Any) -> RegistryAgent | None:
    if not isinstance(entry, dict):
        return None
    agent_id = entry.get("id") or entry.get("name")
    if not isinstance(agent_id, str) or not agent_id:
        return None
    distribution = entry.get("distribution")
    candidates: list[tuple[str, tuple[str, ...]]] = []
    if isinstance(distribution, dict):
        candidates.extend(_binary_candidates(distribution.get("binary")))
        candidates.extend(_npx_candidates(distribution.get("npx")))
    candidates = [(name, args) for name, args in candidates if name not in _NOT_AGENTS]
    if not candidates:
        return None
    name = entry.get("name") if isinstance(entry.get("name"), str) else agent_id
    description = entry.get("description") if isinstance(entry.get("description"), str) else ""
    return RegistryAgent(id=agent_id, name=name, description=description, candidates=tuple(candidates))


def parse_registry(raw: bytes) -> list[RegistryAgent]:
    """The registry's agents; entries with no local launch form are dropped."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RegistryError(f"the ACP registry is not valid JSON: {exc}") from exc
    entries = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise RegistryError("the ACP registry has no 'agents' list")
    agents = [agent for agent in (_parse_agent(entry) for entry in entries) if agent is not None]
    return sorted(agents, key=lambda agent: agent.id)


def read_registry(source: str, *, timeout_s: float = 15.0) -> bytes:
    """The registry body from a URL or a local path."""
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RegistryError(f"could not fetch the ACP registry from {source}: {exc}") from exc
    path = Path(source.removeprefix("file://")).expanduser()
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"could not read the ACP registry at {path}: {exc}") from exc


def fetch_registry(
    *,
    source: str = DEFAULT_REGISTRY_URL,
    cache_path: Path | None = None,
    refresh: bool = False,
    timeout_s: float = 15.0,
) -> tuple[list[RegistryAgent], str]:
    """The parsed registry and where it came from: ``network``, ``file`` or ``cache``.

    A fetched body is cached only after it parsed, so a bad response never becomes the copy that
    is served on the next network failure.
    """
    origin = "network" if source.startswith(("http://", "https://")) else "file"
    if cache_path is not None and cache_path.exists() and not refresh and origin == "network":
        try:
            return parse_registry(cache_path.read_bytes()), "cache"
        except (OSError, RegistryError):
            pass
    try:
        raw = read_registry(source, timeout_s=timeout_s)
    except RegistryError:
        if cache_path is not None and cache_path.exists():
            return parse_registry(cache_path.read_bytes()), "cache"
        raise
    agents = parse_registry(raw)
    if origin == "network" and cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            cache_path.write_bytes(raw)
        except OSError:
            pass
    return agents, origin


# -- detection -------------------------------------------------------------------------------------


def _search_dirs(env: Mapping[str, str]) -> list[Path]:
    home = Path(env.get("HOME") or Path.home())
    return [home / entry for entry in _EXTRA_BIN_DIRS]


def _resolve(bin_name: str, env: Mapping[str, str]) -> str | None:
    found = shutil.which(bin_name, path=env.get("PATH"))
    if found:
        return found
    for directory in _search_dirs(env):
        candidate = directory / bin_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def detect(
    agents: Sequence[RegistryAgent], *, env: Mapping[str, str] | None = None
) -> list[DiscoveredAgent]:
    """The registry agents whose launch binary exists on this machine. Nothing is executed."""
    environment = dict(os.environ if env is None else env)
    found: list[DiscoveredAgent] = []
    for agent in agents:
        for bin_name, args in agent.candidates:
            path = _resolve(bin_name, environment)
            if path is not None:
                found.append(DiscoveredAgent(agent=agent, path=path, args=args))
                break
    native_agy = _resolve("agy", environment)
    if native_agy is not None:
        # Native AGY is a built-in outside the ACP registry. Prefer one clear discovery
        # row over proposing a second ACP alias for the same provider family.
        found = [item for item in found if first_class_match(item.agent) != "agy"]
        found.append(DiscoveredAgent(
            agent=RegistryAgent(
                id="antigravity-cli", name="Antigravity CLI",
                description="TaskSpindle native OAuth provider", candidates=(("agy", ()),),
            ), path=native_agy, args=(),
        ))
    return found


# -- the proposal ------------------------------------------------------------------------------------


def profile_id(agent: RegistryAgent) -> str:
    """A ``[providers.<id>]`` key for a registry entry: its id with the ``-acp`` suffix dropped."""
    ident = agent.id.removesuffix("-acp")
    return ident if ident not in FIRST_CLASS else f"{ident}-registry"


def first_class_match(agent: RegistryAgent) -> str | None:
    """The TaskSpindle first-class provider this registry entry corresponds to, if any."""
    return _FIRST_CLASS_REGISTRY_IDS.get(agent.id)


def proposal(found: DiscoveredAgent) -> str:
    """A ``config.toml`` block for one discovered agent, for a person to read and paste."""
    agent = found.agent
    if first_class_match(agent) == "agy":
        return "# Antigravity is built in as agy; run taskspindle setup --provider agy.\n"
    command = ", ".join(json.dumps(part) for part in found.command)
    description = f" -- {agent.description}" if agent.description else ""
    return (
        f"# {agent.name} (registry id {agent.id}){description}\n"
        f"# Found at {found.path}. Not tested: run `taskspindle doctor` after adding it.\n"
        f"[providers.{profile_id(agent)}]\n"
        f'auth = "api_key"          # or "oauth" if this agent signs in with a subscription seat\n'
        f"command = [{command}]\n"
        f'modes = ["consult", "review"]\n'
        f'# secret_env = ["EXAMPLE_API_KEY"]   # names only; required for api_key profiles\n'
    )
