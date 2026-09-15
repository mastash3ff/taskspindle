"""Bounded native evidence checks; never proof of a usable subscription or model turn.

Checks never change task refusal evidence. Fresh, explicit native quota exhaustion can
add a temporary availability gate. Only allowlisted diagnostic projections are retained.
"""

from __future__ import annotations

import json
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
        "model_ids": None,
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
                    if command != list(providers.CLAUDE_AUTH_STATUS_COMMAND):
                        raise ValueError("Unsupported native check command")
                    completed = subprocess.run(
                        command,
                        env=env,
                        cwd=temporary,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    try:
                        payload = json.loads(completed.stdout or "")
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict) and payload.get("loggedIn") is False:
                        result.update(
                            state="auth_required", detail="Claude reports no cached authentication."
                        )
                    return completed

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
            def agy_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                from .agy_cli import _failure

                completed = subprocess.run(command, **kwargs)
                if command[-1] == "models" and completed.returncode != 0:
                    failure = _failure((completed.stdout or "") + (completed.stderr or ""),
                                       status="error", exit_code=completed.returncode)
                    if failure.code == limits.PROVIDER_AUTH_EXPIRED:
                        result.update(state="auth_required", detail="Antigravity requires authentication.")
                return completed

            evidence = agy_cli_adapter.agy_oauth_evidence(profile, parent_env, runner=agy_run)
            count = evidence.get("model_count")
            if type(count) is not int or count < 1:
                return result
            result.update(
                state="catalog_access",
                model_count=count,
                model_ids=evidence.get("model_ids"),
                detail=(
                    "Antigravity listed its model catalog; "
                    "model-turn access and account binding are unverified."
                ),
            )
    except Exception:
        # Native stderr, JSON, identities, and exception text never cross this boundary.
        pass
    return result


# Diagnostics share a persistent cache; task evidence is never updated here.
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
    if profile.family != "grok":
        from .auth_context import fingerprint

        payload = [profile.family, fingerprint(profile, parent_env), metadata(Path(executable))]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _grok_eligible(profile: Profile) -> bool:
    return (
        profile.family == "grok"
        and profile.auth == "oauth"
        and not profile.secret_env
        and bool(profile.command)
        and Path(profile.command[0]).name == "grok"
    )


def _eligible(profile: Profile) -> bool:
    return _grok_eligible(profile) or (
        profile.family in {"claude", "agy"} and profile.auth == "oauth" and not profile.secret_env
    )


def _safe_native_result(value: Any, profile: Profile) -> dict[str, Any] | None:
    if profile.family == "grok":
        return _safe_grok_result(value)
    if not isinstance(value, dict):
        return None
    from .grok_checks import parse_time

    states = {"cached_auth", "catalog_access", "auth_required", "check_failed", "unsupported"}
    if not isinstance(value.get("state"), str) or value["state"] not in states:
        return None
    checked = parse_time(value.get("checked_at"))
    ids = value.get("model_ids")
    ids = (sorted({item for item in ids if isinstance(item, str) and limits._model(item) == item})
           if isinstance(ids, list) else None)
    return {
        "state": value["state"],
        "source": "claude_auth_status" if profile.family == "claude" else "agy_models",
        "checked_at": checked.isoformat().replace("+00:00", "Z") if checked else None,
        "account_binding": "unverified",
        "plan": value.get("plan") if value.get("plan") in ("pro", "max") else None,
        "model_count": len(ids) if ids is not None else None,
        "model_ids": ids,
        "detail": "Cached native diagnostic evidence; model-turn access is unverified.",
    }


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
        "state": "not_checked" if _eligible(profile) else "unsupported",
        "source": {"grok": "grok_billing", "claude": "claude_auth_status", "agy": "agy_models"}.get(
            profile.family, "unsupported"
        ) if _eligible(profile) else "unsupported",
        "version": None,
        "checked_at": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "account_binding": "unverified",
        "plan": None,
        "model_count": None,
        "model_ids": None,
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
    if not _eligible(profile):
        return result
    reader = getattr(store, "get_native_check", None)
    row = reader(limits.status_key(profile)) if reader else None
    if row is None or row["fingerprint"] != native_fingerprint(
        profile, parent_env if parent_env is not None else os.environ
    ):
        return result
    latest = _safe_native_result(row.get("result_json"), profile)
    if latest is not None:
        result.update({key: value for key, value in latest.items() if key in result})
    attempt, success = parse_time(row.get("attempt_at")), parse_time(row.get("success_at"))
    prior = _safe_native_result(row.get("success_json"), profile)
    result.update(
        last_attempt_at=attempt.isoformat().replace("+00:00", "Z") if attempt else None,
        last_success_at=success.isoformat().replace("+00:00", "Z") if success else None,
        last_success=(prior if prior and prior["state"] in {"quota", "cached_auth", "catalog_access"}
                      else None),
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

    if not _eligible(profile):
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
            result = check_native_access(profile, parent_env)
            at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            # Silent native refresh can rotate auth metadata. Never attach the previous account's
            # success to new metadata; the next request invalidates and checks afresh.
            store.finish_native_check(limits.status_key(profile), fingerprint, owner, at, result)
            return cached_native_check(store, profile, parent_env)
        result = cached_native_check(store, profile, parent_env)
        if not result["checking"] or time.monotonic() >= wait_until:
            return result
        time.sleep(0.1)
