"""Bounded native evidence checks; never proof of a usable subscription or model turn.

These checks do not update provider selection, persisted health, or task failures.
The only subprocess output retained is the existing evidence parser's safe projection.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from . import agy_cli_adapter, providers
from .providers import Profile


def check_native_access(profile: Profile, parent_env: Mapping[str, str]) -> dict[str, Any]:
    """Inspect approved OAuth CLI evidence without login, inference, or persistence."""
    result: dict[str, Any] = {
        "state": "unsupported",
        "source": "unsupported",
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "account_binding": "unverified",
        "plan": None,
        "model_count": None,
        "detail": "No approved native access check is available for this profile.",
    }
    if profile.auth != "oauth" or profile.secret_env or profile.family not in {"claude", "agy"}:
        return result

    result.update(
        state="check_failed",
        source="claude_auth_status" if profile.family == "claude" else "agy_models",
        detail="The native access check did not establish cached authentication or catalog access.",
    )
    try:
        if profile.family == "claude":
            with TemporaryDirectory(prefix="taskspindle-access-check-") as temporary:
                env = providers.build_child_env(profile, parent_env, task_tmp=Path(temporary))
                # Profile overrides must not make a diagnostic interactive.
                env.update(NO_BROWSER="1", CI="1", TERM="dumb", TMPDIR=temporary)

                def run(command: list[str]) -> subprocess.CompletedProcess[str]:
                    if command != ["claude", "auth", "status"]:
                        raise ValueError("Unsupported native check command")
                    return subprocess.run(
                        command, env=env, cwd=temporary, stdin=subprocess.DEVNULL,
                        capture_output=True, text=True, timeout=15, check=False,
                    )

                evidence = providers.claude_oauth_evidence(run)
            plan = evidence.get("subscriptionType")
            if plan not in {"pro", "max"}:
                return result
            result.update(
                state="cached_auth", plan=plan,
                detail=("Claude reports cached OAuth authentication; "
                        "live access and account binding are unverified."),
            )
        else:
            evidence = agy_cli_adapter.agy_oauth_evidence(profile, parent_env, runner=subprocess.run)
            count = evidence.get("model_count")
            if type(count) is not int or count < 1:
                return result
            result.update(
                state="catalog_access", model_count=count,
                detail=("Antigravity listed its model catalog; "
                        "model-turn access and account binding are unverified."),
            )
    except Exception:
        # Native stderr, JSON, identities, and exception text never cross this boundary.
        pass
    return result
