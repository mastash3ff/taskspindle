"""Pinned native Antigravity CLI identity and credential-free startup checks.

Authentication stays in the terminal CLI's own HOME. TaskSpindle checks that the native
token file its workers mount exists, without reading or copying its contents. A successful
``models`` command then verifies cached catalog access without a model turn. Provider output
is not included in diagnostic failures.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .providers import Profile, ProfileError, build_child_env

ADAPTER_ID = "antigravity-cli"
ADAPTER_VERSION = "1.1.26"
PROTOCOL = "agy-cli"
_TIMEOUT = 30.0
_MODEL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]*\Z")
Runner = Callable[..., subprocess.CompletedProcess[str]]


def adapter_command(runtime_dir: Path) -> tuple[str, ...]:
    """Always launch the installed copy, never the separately updated daily CLI."""
    return (str(runtime_dir / ADAPTER_ID / ADAPTER_VERSION / "agy"),)


def require_cached_token(home: Path) -> Path:
    """Return the native token file shared by preflight and isolated worker launch.

    Only filesystem metadata is inspected. Keyring-only login is insufficient for the
    isolated launcher, which mounts this file in place without copying its credentials.
    """
    token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    if token.is_symlink() or not token.is_file():
        raise ProfileError(
            "OAUTH_REJECTED",
            "Antigravity's native token file is missing or is not a regular file. "
            "Run `agy` interactively in this WSL account to sign in, then retry `taskspindle auth agy`.",
        )
    return token


def _probe(
    command: tuple[str, ...], profile: Profile, parent_env: Mapping[str, str], runner: Runner,
) -> subprocess.CompletedProcess[str]:
    if not parent_env.get("HOME"):
        raise ProfileError("OAUTH_REJECTED", "HOME is required for Antigravity's existing CLI login")
    with tempfile.TemporaryDirectory(prefix="taskspindle-agy-check-") as raw:
        env = build_child_env(profile, parent_env, task_tmp=Path(raw))
        try:
            return runner(
                list(command), env=env, cwd=raw, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProfileError("OAUTH_REJECTED", "Antigravity CLI startup check timed out") from exc
        except OSError as exc:
            raise ProfileError("PROFILE_INVALID", "Antigravity CLI could not be started") from exc


def verify_cli_version(
    binary: Path, *, runner: Runner = subprocess.run,
    parent_env: Mapping[str, str] = os.environ,
) -> str:
    """Require the exact qualified CLI build, without starting a model or logging in."""
    if (binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK)
            or binary.stat().st_uid != os.getuid() or binary.stat().st_mode & 0o022):
        raise ProfileError("PROFILE_INVALID", "Antigravity needs a regular executable CLI pin; run setup")
    profile = Profile(id="agy", auth="oauth", command=(str(binary),))
    result = _probe((str(binary), "--version"), profile, parent_env, runner)
    if result.returncode != 0 or (result.stdout or "").strip() != ADAPTER_VERSION:
        raise ProfileError(
            "PROFILE_INVALID", f"Antigravity CLI must report exactly version {ADAPTER_VERSION}",
        )
    return ADAPTER_VERSION


def validate_cli_profile(profile: Profile) -> Path:
    """Reject a stale ACP command or an unpinned replacement before any CLI probe."""
    if profile.auth != "oauth" or profile.secret_env or len(profile.command) != 1:
        raise ProfileError("PROFILE_INVALID", "Antigravity requires the pinned native OAuth CLI profile")
    binary = Path(profile.command[0])
    if (not binary.is_absolute() or binary.name != "agy"
            or binary.parent.name != ADAPTER_VERSION or binary.parent.parent.name != ADAPTER_ID):
        raise ProfileError("PROFILE_INVALID", "Antigravity command is not the qualified native CLI pin")
    if profile.env:
        raise ProfileError(
            "PROFILE_INVALID", "Antigravity uses the existing HOME and no profile env overrides",
        )
    return binary


def parse_model_catalog(stdout: str) -> list[tuple[str, str]]:
    """Parse native ``agy models`` tab-separated IDs/names; never infer missing IDs."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        model_id, separator, name = line.partition("\t")
        if (not separator or not _MODEL_ID.fullmatch(model_id) or not name.strip()
                or model_id in seen or any(ord(char) < 32 for char in name)):
            raise ProfileError("OAUTH_REJECTED", "Antigravity CLI returned an invalid model catalog")
        seen.add(model_id)
        rows.append((model_id, name.strip()))
    if not any(model_id.startswith("gemini-") for model_id, _ in rows):
        raise ProfileError("OAUTH_REJECTED", "Antigravity CLI did not advertise a Gemini model")
    return rows


def model_catalog(
    profile: Profile, parent_env: Mapping[str, str], *, runner: Runner = subprocess.run,
) -> list[tuple[str, str]]:
    """Read advertised models using cached terminal login; do not prompt or sign in."""
    binary = validate_cli_profile(profile)
    if not parent_env.get("HOME"):
        raise ProfileError("OAUTH_REJECTED", "HOME is required for Antigravity's existing CLI login")
    require_cached_token(Path(parent_env["HOME"]))
    verify_cli_version(binary, runner=runner, parent_env=parent_env)
    result = _probe((*profile.command, "models"), profile, parent_env, runner)
    if result.returncode != 0:
        raise ProfileError(
            "OAUTH_REJECTED",
            "Antigravity CLI could not list models with its cached login. "
            "Run `agy` interactively in this WSL account to sign in, then retry `taskspindle auth agy`.",
        )
    return parse_model_catalog(result.stdout or "")


def agy_oauth_evidence(
    profile: Profile, parent_env: Mapping[str, str], *, runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Return safe evidence only; no identity, credentials, or raw stderr is retained."""
    rows = model_catalog(profile, parent_env, runner=runner)
    return {
        "auth_method_id": "oauth-personal", "cached_credential": True,
        "model_count": sum(model_id.startswith("gemini-") for model_id, _ in rows),
        "protocol": PROTOCOL, "version": ADAPTER_VERSION,
    }
