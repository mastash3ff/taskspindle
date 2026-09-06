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

__all__ = ["ConfigError", "Paths", "concurrency_limits", "load_config", "paths"]


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
