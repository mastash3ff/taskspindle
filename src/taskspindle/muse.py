"""Muse's qualification boundary, separate from its offline MSP transport.

The installed protocol can be exercised without a model. It does not establish a
subscription billing route or enforcement of TaskSpindle's tool policy. Until both
are qualified, neither a policy edit nor allow_metered may enable this provider.
No credential file is read and no login or inference is attempted here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MUSE_VERSION = "1.3.0"
MUSE_BUILD = "1.3.0-R3401.1"
MUSE_SCHEMA_FINGERPRINT = "sha256:7469c9e352e67def4a59df7e439984d7194fa351e1c8b7abb34060fd977ced81"
MUSE_NOT_QUALIFIED = "MUSE_NOT_QUALIFIED"
QUALIFICATION_REASON = (
    "Muse is disabled: the active subscription-only billing route and nonrecursive "
    "tool containment have not been qualified. Login, a model catalog, and cached "
    "subscription usage are not sufficient evidence."
)


def qualification() -> dict[str, Any]:
    """Machine-readable enablement status, not a configurable attestation."""
    return {
        "enabled": False,
        "code": MUSE_NOT_QUALIFIED,
        "reason": QUALIFICATION_REASON,
        "subscription_route": "unverified",
        "tool_containment": "unverified",
        "qualified_modes": [],
    }


def require_qualified() -> None:
    """Fail before inference, including direct worker and policy-override paths."""
    from .providers import ProfileError

    if not qualification()["enabled"]:
        raise ProfileError(MUSE_NOT_QUALIFIED, QUALIFICATION_REASON)


def command(runtime_dir: Path) -> tuple[str, ...]:
    """Only the explicitly staged binary; never resolve an updating PATH wrapper."""
    return (str(runtime_dir / "muse"),)


def unresolved_commands(store: Any, task: Any) -> bool:
    """An intent survives process death until an authoritative outcome is recorded."""
    if (task.provider_family or task.provider) != "muse":
        return False
    pending: set[str] = set()
    for event in store.list_events(task.id):
        payload = event.get("payload") or {}
        identifier = payload.get("command_id")
        if not isinstance(identifier, str):
            continue
        if payload.get("code") == "MUSE_COMMAND_INTENT":
            pending.add(identifier)
        elif payload.get("code") == "MUSE_COMMAND_OUTCOME":
            pending.discard(identifier)
    return bool(pending)


def isolated_environment(task_dir: Path, env: dict[str, str]) -> dict[str, str]:
    """Use task-owned durable state; never inherit the operator's Muse settings."""
    home = task_dir / "muse-home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        **env,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }
