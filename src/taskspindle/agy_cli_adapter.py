"""Antigravity CLI resolution, identity, and credential-free startup checks.

TaskSpindle no longer keeps a private copy of the native Antigravity CLI: it resolves and runs
whichever build the operator has installed, the same one an interactive ``agy`` login would run.
A vendor release is a one-line constant update here, not an outage, because only a minimum
version is enforced; a newer build is accepted and reported as advisory, never blocking.

Authentication stays in the terminal CLI's own HOME. TaskSpindle checks that the native
token file its workers mount exists, without reading or copying its contents. A successful
``models`` command then verifies cached catalog access without a model turn. Provider output
is not included in diagnostic failures.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .providers import Profile, ProfileError, build_child_env

ADAPTER_ID = "antigravity-cli"
PROTOCOL = "agy-cli"

#: The oldest Antigravity CLI build TaskSpindle's launch policy and stream parsing are
#: qualified against. A CLI reporting an older version is refused.
AGY_MIN_VERSION = "1.1.26"

#: The newest build TaskSpindle has actually been exercised against. A CLI reporting a newer
#: version is still accepted -- refusing it on every vendor release is exactly the outage this
#: replaces -- but it is surfaced as an advisory note, not silently treated as identical.
AGY_TESTED_MAX = "1.2.3"

#: Back-compat for call sites that still import a single "the adapter version" name. There is
#: no longer one pinned build; this is the qualified floor.
ADAPTER_VERSION = AGY_MIN_VERSION

_TIMEOUT = 30.0
_MODEL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]*\Z")
Runner = Callable[..., subprocess.CompletedProcess[str]]


def _semver(text: str) -> tuple[int, ...]:
    """Parse a dotted-integer version string; anything else is a ``ValueError``."""
    stripped = text.strip()
    parts = stripped.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        raise ValueError(f"not a dotted version number: {text!r}")
    return tuple(int(part) for part in parts)


def newer_than_tested(reported: str) -> bool:
    """Whether ``reported`` is past the newest build TaskSpindle has been tested against."""
    return _semver(reported) > _semver(AGY_TESTED_MAX)


def resolve_binary(home: Path, *, parent_env: Mapping[str, str] = os.environ) -> Path:
    """Find the vendor CLI to run: never raises, so profile construction never fails.

    Resolution order: ``TASKSPINDLE_AGY_SOURCE`` (an explicit override the codex-runtime deploy
    script sets; never required), then whichever ``agy`` is on ``PATH`` -- the same one an
    interactive login would run -- then the location a fresh per-user ``agy`` install leaves at
    ``~/.local/bin/agy``. The last two are not required to exist yet: an unreachable result is a
    normal, later failure at :func:`verify_cli_version` or launch, not a construction-time one.
    """
    override = parent_env.get("TASKSPINDLE_AGY_SOURCE")
    if override:
        return Path(override)
    found = shutil.which("agy", path=parent_env.get("PATH"))
    if found:
        return Path(found)
    return home / ".local/bin/agy"


def adapter_command(home: Path, *, parent_env: Mapping[str, str] = os.environ) -> tuple[str, ...]:
    """Launch whichever vendor CLI is resolved from PATH, never a private copy."""
    return (str(resolve_binary(home, parent_env=parent_env)),)


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
    """Require at least the qualified minimum CLI build, without starting a model or logging in.

    A build newer than :data:`AGY_TESTED_MAX` is accepted, not refused: only an older-than-
    minimum or unparsable report is a failure. Callers that want to surface the "newer than
    tested" note use :func:`newer_than_tested` on the returned string.
    """
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ProfileError(
            "PROFILE_INVALID", "Antigravity needs an existing, executable CLI; install agy or run setup",
        )
    profile = Profile(id="agy", auth="oauth", command=(str(binary),))
    result = _probe((str(binary), "--version"), profile, parent_env, runner)
    reported = (result.stdout or "").strip()
    if result.returncode != 0 or not reported:
        raise ProfileError("PROFILE_INVALID", "Antigravity CLI did not report a version")
    try:
        found = _semver(reported)
    except ValueError as exc:
        raise ProfileError(
            "PROFILE_INVALID", f"Antigravity CLI reported an unparsable version: {reported!r}",
        ) from exc
    if found < _semver(AGY_MIN_VERSION):
        raise ProfileError(
            "PROFILE_INVALID",
            f"Antigravity CLI {reported} is older than the required minimum {AGY_MIN_VERSION}",
        )
    return reported


def validate_cli_profile(profile: Profile) -> Path:
    """Reject a stale ACP command or a malformed profile before any CLI probe."""
    if profile.auth != "oauth" or profile.secret_env or len(profile.command) != 1:
        raise ProfileError("PROFILE_INVALID", "Antigravity requires the native OAuth CLI profile")
    binary = Path(profile.command[0])
    if not binary.is_absolute() or binary.name != "agy":
        raise ProfileError("PROFILE_INVALID", "Antigravity command is not the resolved native CLI")
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


def _verified_catalog(
    profile: Profile, parent_env: Mapping[str, str], *, runner: Runner,
) -> tuple[str, list[tuple[str, str]]]:
    binary = validate_cli_profile(profile)
    if not parent_env.get("HOME"):
        raise ProfileError("OAUTH_REJECTED", "HOME is required for Antigravity's existing CLI login")
    require_cached_token(Path(parent_env["HOME"]))
    version = verify_cli_version(binary, runner=runner, parent_env=parent_env)
    result = _probe((*profile.command, "models"), profile, parent_env, runner)
    if result.returncode != 0:
        raise ProfileError(
            "OAUTH_REJECTED",
            "Antigravity CLI could not list models with its cached login. "
            "Run `agy` interactively in this WSL account to sign in, then retry `taskspindle auth agy`.",
        )
    return version, parse_model_catalog(result.stdout or "")


def model_catalog(
    profile: Profile, parent_env: Mapping[str, str], *, runner: Runner = subprocess.run,
) -> list[tuple[str, str]]:
    """Read advertised models using cached terminal login; do not prompt or sign in."""
    return _verified_catalog(profile, parent_env, runner=runner)[1]


def agy_oauth_evidence(
    profile: Profile, parent_env: Mapping[str, str], *, runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Return safe evidence only; no identity, credentials, or raw stderr is retained."""
    version, rows = _verified_catalog(profile, parent_env, runner=runner)
    evidence: dict[str, Any] = {
        "auth_method_id": "oauth-personal", "cached_credential": True,
        "model_count": sum(model_id.startswith("gemini-") for model_id, _ in rows),
        "model_ids": [model_id for model_id, _ in rows],
        "protocol": PROTOCOL, "version": version,
    }
    if newer_than_tested(version):
        evidence["version_advisory"] = (
            f"{version} is newer than the tested {AGY_TESTED_MAX}; accepted, not independently verified"
        )
    return evidence
