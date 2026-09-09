"""Bounded native evidence checks; never proof of a usable subscription or model turn.

Checks never change task refusal evidence. Fresh, explicit native quota exhaustion can
add a temporary availability gate. Only allowlisted diagnostic projections are retained.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from . import agy_cli_adapter, limits, providers
from .providers import Profile


def check_native_access(profile: Profile, parent_env: Mapping[str, str]) -> dict[str, Any]:
    """Inspect approved OAuth CLI evidence without login, inference, or task persistence."""
    if _grok_eligible(profile):
        from .grok_checks import check_grok

        return check_grok(profile, parent_env)
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
                        command,
                        env=env,
                        cwd=temporary,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )

                evidence = providers.claude_oauth_evidence(run)
            plan = evidence.get("subscriptionType")
            if plan not in {"pro", "max"}:
                return result
            result.update(
                state="cached_auth",
                plan=plan,
                detail=(
                    "Claude reports cached OAuth authentication; "
                    "live access and account binding are unverified."
                ),
            )
        else:
            evidence = agy_cli_adapter.agy_oauth_evidence(profile, parent_env, runner=subprocess.run)
            count = evidence.get("model_count")
            if type(count) is not int or count < 1:
                return result
            result.update(
                state="catalog_access",
                model_count=count,
                detail=(
                    "Antigravity listed its model catalog; "
                    "model-turn access and account binding are unverified."
                ),
            )
    except Exception:
        # Native stderr, JSON, identities, and exception text never cross this boundary.
        pass
    return result


# Grok diagnostics use a separate persistent cache; task evidence is never updated here.
NATIVE_TTL_S = 300


def native_fingerprint(profile: Profile, parent_env: Mapping[str, str]) -> str:
    """Hash account-relevant metadata, sharing quota across model aliases of the same OAuth seat."""
    import hashlib
    import json
    import shutil

    def metadata(path: Path) -> list[Any]:
        try:
            stat = path.stat()
            return [
                str(path.resolve()),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            ]
        except OSError:
            return [str(path), None]

    command = profile.command[0] if profile.command else ""
    search_path = profile.env.get("PATH", parent_env.get("PATH", ""))
    executable = shutil.which(command, path=search_path) or command
    home = Path(profile.env.get("HOME", parent_env.get("HOME", "/nonexistent")))
    grok_home = Path(profile.env.get("GROK_HOME", str(home / ".grok")))
    payload = [
        limits.status_key(profile),
        profile.auth,
        metadata(Path(executable)),
        metadata(grok_home / "auth.json"),
        metadata(grok_home / "config.toml"),
        metadata(Path(profile.env["GROK_CONFIG"])) if profile.env.get("GROK_CONFIG") else None,
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _grok_eligible(profile: Profile) -> bool:
    return (
        profile.family == "grok"
        and profile.auth == "oauth"
        and not profile.secret_env
        and bool(profile.command)
        and Path(profile.command[0]).name == "grok"
    )


def _safe_grok_result(value: Any) -> dict[str, Any] | None:
    import re

    from .grok_checks import parse_billing, parse_time, safe_extra_usage

    if not isinstance(value, dict):
        return None
    state = value.get("state")
    details = {
        "quota": "Grok reported quota usage; model-turn access and account binding are unverified.",
        "unsupported": "Native quota is unavailable for this Grok CLI/profile.",
        "auth_required": "Grok requires existing OAuth authentication; no login was started.",
        "check_failed": "The native quota check did not establish current quota.",
    }
    if not isinstance(state, str) or state not in details:
        return None
    errors = {
        "METHOD_UNAVAILABLE",
        "SCHEMA_UNAVAILABLE",
        "UNSUPPORTED_PROFILE",
        "AUTH_REQUIRED",
        "TIMEOUT",
        "CHECK_FAILED",
        "VERSION_UNAVAILABLE",
        "INVALID_RESPONSE",
        "CLEANUP_FAILED",
    }
    error = value.get("error_code")
    version = value.get("version")
    checked = parse_time(value.get("checked_at"))
    result = {
        "state": state,
        "source": "grok_billing",
        "detail": details[state],
        "error_code": error if isinstance(error, str) and error in errors else None,
        "version": version
        if isinstance(version, str) and re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version)
        else None,
        "checked_at": checked.isoformat().replace("+00:00", "Z") if checked else None,
        "account_binding": "unverified",
        "plan": None,
        "model_count": None,
        "used_percent": None,
        "window": None,
        "period_start": None,
        "reset_at": None,
        "billing": None,
    }
    if state == "quota":
        raw_window = value.get("window")
        window = (
            {"weekly": "USAGE_PERIOD_TYPE_WEEKLY", "monthly": "USAGE_PERIOD_TYPE_MONTHLY"}.get(raw_window)
            if isinstance(raw_window, str)
            else None
        )
        quota = parse_billing(
            {
                "config": {
                    "creditUsagePercent": value.get("used_percent"),
                    "currentPeriod": {
                        "type": window,
                        "start": value.get("period_start"),
                        "end": value.get("reset_at"),
                    },
                }
            }
        )
        if quota is None:
            return None
        result.update(quota)
        result["billing"] = safe_extra_usage(value.get("billing"), result["checked_at"])
    return result


def cached_native_check(
    store: Any, profile: Profile, parent_env: Mapping[str, str] | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """A read-only, stable public projection; invalidated results never cross identities."""
    import os

    from .grok_checks import parse_time

    current = now or datetime.now(UTC)
    result: dict[str, Any] = {
        "state": "not_checked" if _grok_eligible(profile) else "unsupported",
        "source": "grok_billing" if _grok_eligible(profile) else "unsupported",
        "version": None,
        "checked_at": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "account_binding": "unverified",
        "plan": None,
        "model_count": None,
        "used_percent": None,
        "window": None,
        "period_start": None,
        "reset_at": None,
        "error_code": None,
        "freshness": "unknown",
        "eligible_hint": None,
        "billing": None,
        "last_success": None,
        "checking": False,
        "detail": "No current native quota observation is available.",
    }
    if not _grok_eligible(profile):
        return result
    reader = getattr(store, "get_native_check", None)
    row = reader(limits.status_key(profile)) if reader else None
    if row is None or row["fingerprint"] != native_fingerprint(
        profile, parent_env if parent_env is not None else os.environ
    ):
        return result
    latest = _safe_grok_result(row.get("result_json"))
    if latest is not None:
        result.update({key: value for key, value in latest.items() if key in result})
    attempt, success = parse_time(row.get("attempt_at")), parse_time(row.get("success_at"))
    prior = _safe_grok_result(row.get("success_json"))
    result.update(
        last_attempt_at=attempt.isoformat().replace("+00:00", "Z") if attempt else None,
        last_success_at=success.isoformat().replace("+00:00", "Z") if success else None,
        last_success=prior if prior and prior["state"] == "quota" else None,
    )
    lease_end = parse_time(row.get("lease_until"))
    result["checking"] = bool(row.get("lease_owner") and lease_end and lease_end > current)
    checked = parse_time(row.get("attempt_at"))
    fresh = bool(checked and 0 <= (current - checked).total_seconds() < NATIVE_TTL_S)
    result["freshness"] = "fresh" if fresh else "stale" if checked else "unknown"
    if result["state"] == "quota" and fresh:
        start, end = parse_time(result["period_start"]), parse_time(result["reset_at"])
        observed = parse_time(result["checked_at"])
        if (
            start
            and end
            and observed
            and start <= current < end
            and 0 <= (current - observed).total_seconds() < NATIVE_TTL_S
        ):
            result["eligible_hint"] = result["used_percent"] < 100
    return result


def refresh_native_check(store: Any, profile: Profile, parent_env: Mapping[str, str]) -> dict[str, Any]:
    """Check at most once per five minutes across CLI/MCP processes (30-second CLI budget)."""
    import time
    import uuid
    from datetime import timedelta

    from .grok_checks import check_grok

    if not _grok_eligible(profile):
        return check_native_access(profile, parent_env)
    fingerprint = native_fingerprint(profile, parent_env)
    owner = uuid.uuid4().hex
    wait_until = time.monotonic() + 31
    while True:
        current = datetime.now(UTC)

        def iso(date: datetime) -> str:
            return date.isoformat().replace("+00:00", "Z")

        if store.claim_native_check(
            limits.status_key(profile),
            fingerprint,
            owner,
            iso(current),
            iso(current + timedelta(seconds=40)),
            iso(current - timedelta(seconds=NATIVE_TTL_S)),
        ):
            result = check_grok(profile, parent_env)
            at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            # Silent native refresh can rotate auth metadata. Never attach the previous account's
            # success to new metadata; the next request invalidates and checks afresh.
            store.finish_native_check(limits.status_key(profile), fingerprint, owner, at, result)
            return cached_native_check(store, profile, parent_env)
        result = cached_native_check(store, profile, parent_env)
        if not result["checking"] or time.monotonic() >= wait_until:
            return result
        time.sleep(0.1)
