"""Opaque, metadata-only identities for OAuth credential contexts.

This module deliberately never opens credential files.  It identifies the location selected by
the environment used for a worker, then hashes only that location and filesystem metadata.  The
hash is suitable for durable comparisons; callers must not expose the unhashed payload.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .providers import Profile, ProfileError

__all__ = ["fingerprint", "locator_digest", "validate_contexts"]

_CONTEXT_VERSION = 1
_CONFLICT_MESSAGE = "OAuth profiles in the same provider family must use the same authentication context."


def _path(value: str) -> Path:
    """Return an absolute, symlink-resolved location without requiring it to exist."""
    return Path(os.path.abspath(os.path.expanduser(value))).resolve(strict=False)


def _effective_env(profile: Profile, parent_env: Mapping[str, str]) -> Mapping[str, str]:
    """The small part of the worker environment that can select an auth location.

    ``build_child_env`` allowlists only the parent HOME, then applies ``profile.env``.  Repeating
    just that precedence here avoids both task-temporary settings and non-allowlisted parent
    variables becoming part of the identity.
    """
    selected: dict[str, str] = {}
    if "HOME" in parent_env:
        selected["HOME"] = parent_env["HOME"]
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME"):
        if name in profile.env:
            selected[name] = profile.env[name]
    return selected


def _home(env: Mapping[str, str]) -> Path:
    # An absent HOME must still be deterministic for fixture and configuration validation paths.
    return _path(env.get("HOME", "/nonexistent"))


def _locations(profile: Profile, parent_env: Mapping[str, str]) -> tuple[tuple[str, Path], ...]:
    """Resolve exactly the OAuth files used by the built-in family's worker/preflight path."""
    env = _effective_env(profile, parent_env)
    home = _home(env)
    if profile.family == "claude":
        config_dir = _path(env.get("CLAUDE_CONFIG_DIR", str(home / ".claude")))
        return (("claude_credentials", config_dir / ".credentials.json"),)
    if profile.family == "grok":
        grok_home = _path(env.get("GROK_HOME", str(home / ".grok")))
        return (("grok_auth", grok_home / "auth.json"),)
    if profile.family == "agy":
        # This is the same native file checked by agy_cli_adapter.require_cached_token and
        # mounted by the isolated native worker.  Do not call that helper: missing fixtures are
        # valid inputs for a fingerprint and must not become an authentication probe.
        # agy_cli_adapter's preflight checks parent_env directly.  Valid AGY profiles prohibit
        # profile environment overrides, so this also matches its worker launch context.
        preflight_home = _path(parent_env.get("HOME", "/nonexistent"))
        return (("agy_token", preflight_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"),)
    return ()


def _metadata(path: Path) -> list[Any]:
    """Safe file metadata only; every failure is represented without raising or creating files."""
    try:
        info = path.stat()
    except OSError:
        return [str(path), False, None, None, None, None, None]
    return [
        str(path), True, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    ]


def _payload(profile: Profile, parent_env: Mapping[str, str]) -> dict[str, Any]:
    locations = _locations(profile, parent_env)
    return {
        "version": _CONTEXT_VERSION,
        "family": profile.family,
        "auth": profile.auth,
        # The canonical resolved locations are the selected auth-location configuration.  This
        # accepts equivalent aliases (for example, a symlink) while rejecting a different login.
        "locations": [[kind, str(path)] for kind, path in locations],
        "metadata": [[kind, _metadata(path)] for kind, path in locations],
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def locator_digest(profile: Profile, parent_env: Mapping[str, str]) -> str:
    """Return an opaque digest of only the selected, resolved credential locations."""
    return _digest(_payload(profile, parent_env)["locations"])


def fingerprint(profile: Profile, parent_env: Mapping[str, str]) -> str:
    """Return an opaque SHA-256 context fingerprint without reading credential contents."""
    return _digest(_payload(profile, parent_env))


def validate_contexts(profiles: Mapping[str, Profile], parent_env: Mapping[str, str]) -> None:
    """Reject OAuth aliases in one provider family when they select different credentials.

    Missing credential files are intentionally allowed.  A later preflight can reject a missing
    login, while this function remains safe for fixture construction and cached-state comparison.
    """
    seen: dict[str, str] = {}
    for profile in profiles.values():
        if profile.auth != "oauth":
            continue
        digest = locator_digest(profile, parent_env)
        previous = seen.setdefault(profile.family, digest)
        if previous != digest:
            raise ProfileError("AUTH_CONTEXT_CONFLICT", _CONFLICT_MESSAGE)
