"""XDG locations and ``config.toml`` loading.

Nothing here touches the network or a provider process: it resolves where TaskSpindle keeps its
config, state, data and pinned runtimes, and reads the single TOML file the user may write.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import taskspindle

__all__ = [
    "CapacityConfig", "ConfigError", "ContextFilesConfig", "DispatchConfig", "Paths",
    "capacity_limits", "concurrency_limits", "context_files_config", "dispatch_config",
    "load_config", "paths",
]

#: Hard ceilings for ``[context_files]``; the table may lower them, never raise them.
MAX_CONTEXT_FILE_BYTES = 1024 * 1024
MAX_CONTEXT_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CONTEXT_FILES = 32


class ConfigError(Exception):
    """``config.toml`` exists but is not parseable TOML."""


@dataclass(frozen=True)
class Paths:
    """Every filesystem location TaskSpindle owns."""

    config_file: Path
    state_dir: Path
    data_dir: Path
    runtime_dir: Path


def _home(env: Mapping[str, str]) -> Path:
    home = env.get("HOME")
    return Path(home) if home else Path.home()


def _xdg(env: Mapping[str, str], name: str, fallback: str) -> Path:
    raw = env.get(name)
    if raw:
        return Path(raw)
    return _home(env) / fallback


def paths(env: Mapping[str, str] = os.environ, version: str = taskspindle.__version__) -> Paths:
    """Resolve TaskSpindle's directories from the XDG variables in ``env``.

    ``runtime_dir`` is versioned so an upgrade installs its pinned adapter beside the old one
    instead of overwriting a runtime a running task still depends on.
    """
    config_home = _xdg(env, "XDG_CONFIG_HOME", ".config")
    state_home = _xdg(env, "XDG_STATE_HOME", ".local/state")
    data_home = _xdg(env, "XDG_DATA_HOME", ".local/share")
    data_dir = data_home / "taskspindle"
    return Paths(
        config_file=config_home / "taskspindle" / "config.toml",
        state_dir=state_home / "taskspindle",
        data_dir=data_dir,
        runtime_dir=data_dir / "runtimes" / version,
    )


def load_config(path: Path) -> dict[str, Any]:
    """Read ``path`` as TOML. A missing file is an empty config; a broken one is an error."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc


def concurrency_limits(settings: Mapping[str, Any], providers: Iterable[str]) -> dict[str, int]:
    """Validated per-profile limits; an omitted profile remains single flight."""
    configured = settings.get("concurrency", {})
    if not isinstance(configured, dict):
        raise ConfigError("concurrency must be a table of provider limits")
    names = set(providers)
    unknown = set(configured) - names
    if unknown:
        raise ConfigError(f"concurrency contains unknown provider(s): {', '.join(sorted(unknown))}")
    for provider, limit in configured.items():
        if type(limit) is not int or limit < 1:
            raise ConfigError(f"concurrency.{provider} must be a positive integer")
    return {provider: configured.get(provider, 1) for provider in sorted(names)}


#: The most slots ``[capacity] per_provider_max`` may allow; the policy document shares it.
CAPACITY_PROVIDER_CEILING = 16
CAPACITY_TOTAL_CEILING = 48


@dataclass(frozen=True)
class CapacityConfig:
    """The ceiling over whatever concurrency the dispatch policy asks for.

    The policy is edited from the dashboard, which may be reachable from the LAN; this table is
    in ``config.toml``, which the dashboard never writes, so it bounds what a policy edit can do
    to the host. ``total_max`` unset means no ceiling on the pool as a whole.
    """

    per_provider_max: int = 8
    total_max: int | None = None


def capacity_limits(settings: Mapping[str, Any]) -> CapacityConfig:
    """Read ``[capacity]``; an absent table is the default ceiling of eight slots per provider."""
    table = settings.get("capacity")
    if table is None:
        return CapacityConfig()
    if not isinstance(table, dict):
        raise ConfigError("[capacity] must be a table")
    ceilings = {"per_provider_max": CAPACITY_PROVIDER_CEILING, "total_max": CAPACITY_TOTAL_CEILING}
    values: dict[str, int] = {}
    for key, ceiling in ceilings.items():
        if key not in table:
            continue
        value = table[key]
        if type(value) is not int or value < 1 or value > ceiling:
            raise ConfigError(f"[capacity] {key} must be an integer between 1 and {ceiling}")
        values[key] = value
    unknown = set(table) - set(ceilings)
    if unknown:
        raise ConfigError(f"[capacity] contains unknown key(s): {', '.join(sorted(unknown))}")
    return CapacityConfig(**values)


@dataclass(frozen=True)
class DispatchConfig:
    """Whether queued work is started as soon as a slot frees, rather than on the next tool call."""

    drain: bool = True
    interval_s: int = 2


def dispatch_config(settings: Mapping[str, Any]) -> DispatchConfig:
    """Read ``[dispatch]``; an absent table drains every two seconds."""
    table = settings.get("dispatch")
    if table is None:
        return DispatchConfig()
    if not isinstance(table, dict):
        raise ConfigError("[dispatch] must be a table")
    values: dict[str, Any] = {}
    if "drain" in table:
        if type(table["drain"]) is not bool:
            raise ConfigError("[dispatch] drain must be true or false")
        values["drain"] = table["drain"]
    if "interval_s" in table:
        interval = table["interval_s"]
        if type(interval) is not int or interval < 1 or interval > 60:
            raise ConfigError("[dispatch] interval_s must be an integer between 1 and 60")
        values["interval_s"] = interval
    unknown = set(table) - {"drain", "interval_s"}
    if unknown:
        raise ConfigError(f"[dispatch] contains unknown key(s): {', '.join(sorted(unknown))}")
    return DispatchConfig(**values)


@dataclass(frozen=True)
class ContextFilesConfig:
    """The operator's allowlist for ``start_task(context_files=...)``.

    ``roots`` are the only directories a coordinator may hand files from. Under the Docker
    backend the server reads them from inside the runtime container, so each root must also be
    one of ``[execution].mounts`` or lie beneath one.
    """

    roots: tuple[Path, ...]
    max_file_bytes: int = 256 * 1024
    max_total_bytes: int = 1024 * 1024
    max_files: int = 16


def context_files_config(settings: Mapping[str, Any]) -> ContextFilesConfig | None:
    """Read ``[context_files]``; an absent table means the handoff is off.

    Once the table is present every field it names must be well-formed, exactly as ``[web]``
    and ``[concurrency]`` behave: a relative root, a root that is not a directory here, or a
    limit that is not a positive integer within its ceiling raises :class:`ConfigError`.
    """
    table = settings.get("context_files")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[context_files] must be a table")
    raw_roots = table.get("roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        raise ConfigError("[context_files] roots must be a non-empty list of absolute directory paths")
    roots: list[Path] = []
    for item in raw_roots:
        if not isinstance(item, str) or not item or "\x00" in item or not Path(item).is_absolute():
            raise ConfigError("[context_files] roots must be absolute directory paths")
        root = Path(item).resolve()
        if not root.is_dir():
            raise ConfigError(
                f"[context_files] root is not a directory from where the server runs: {item} "
                "(under the Docker backend it must be inside an [execution] mount)"
            )
        roots.append(root)
    limits = {
        "max_file_bytes": MAX_CONTEXT_FILE_BYTES,
        "max_total_bytes": MAX_CONTEXT_TOTAL_BYTES,
        "max_files": MAX_CONTEXT_FILES,
    }
    values: dict[str, int] = {}
    for key, ceiling in limits.items():
        if key not in table:
            continue
        value = table[key]
        if type(value) is not int or value < 1 or value > ceiling:
            raise ConfigError(f"[context_files] {key} must be an integer between 1 and {ceiling}")
        values[key] = value
    unknown = set(table) - {"roots", *limits}
    if unknown:
        raise ConfigError(f"[context_files] contains unknown key(s): {', '.join(sorted(unknown))}")
    return ContextFilesConfig(roots=tuple(roots), **values)
