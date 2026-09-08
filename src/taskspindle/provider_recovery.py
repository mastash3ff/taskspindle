"""Explicit, single-use recovery against captured refusal evidence; never probes providers."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from . import limits
from .access_checks import cached_native_check

_REFUSALS = frozenset({"throttled", "auth_expired", "access_denied", "model_unavailable"})


def _error(code: str, message: str) -> None:
    from .service import TaskSpindleError

    raise TaskSpindleError("RECOVERY_" + code, message)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _scope(profile: Any, model: str | None) -> str:
    key = limits.status_key(profile)
    if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", x) is None for x in (profile.id, key)):
        _error("INVALID_SCOPE", "Recovery requires a valid named provider.")
    if model is not None and (limits._model(model) != model):
        _error("INVALID_SCOPE", "Recovery requires a valid model identifier.")
    return key


def _evidence(store: Any, profile: Any, now: datetime, model: str | None, parent_env: Any) -> dict[str, Any]:
    key = _scope(profile, model)
    account = store.get_provider_status(key)
    selected = store.get_provider_model_status(key, model) if model else None
    native = cached_native_check(store, profile, parent_env, now=now)
    from .quota import evaluate as evaluate_quota

    quota = evaluate_quota(store, profile, now, model=model, parent_env=parent_env)
    rows = [row for row in (account, selected) if row is not None]
    future_reset = any((_time(row.get("reset_at")) or now) > now for row in rows)
    exhausted = native.get("eligible_hint") is False
    # A bare throttle is deliberately recoverable only through this one-shot permit.
    # A supplied future reset, or current fresh native exhaustion, is never bypassable.
    quota_future = [
        row
        for row in quota["quota_restrictions"]
        if _time(row.get("reset_at") or row.get("resets_at")) is not None
    ]
    restrictions = {
        "future_reset": future_reset or bool(quota_future),
        "native_exhausted": exhausted,
        "quota_fingerprints": quota["evidence_fingerprints"],
    }
    if exhausted:
        restrictions.update(
            {name: native.get(name) for name in ("checked_at", "reset_at", "window", "used_percent")}
        )
    identity = {
        "provider": profile.id,
        "status_key": key,
        "model": model,
        "account_fingerprint": limits.status_fingerprint(account),
        "model_fingerprint": limits.status_fingerprint(selected),
        "restrictions": restrictions,
        "auth_context": quota["auth_context"]["fingerprint"],
    }
    # The model name binds the permit scope separately. With no model observation, the
    # account evidence revision must also be usable for an explicitly named unseen model.
    # A real model fingerprint already includes its exact model identity.
    observed = {key: value for key, value in identity.items() if key != "model"}
    revision = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
    account_observed = {
        **observed,
        "model_fingerprint": limits.status_fingerprint(None),
        "restrictions": {
            **restrictions,
            "future_reset": bool(account and (_time(account.get("reset_at")) or now) > now),
        },
    }
    account_revision = hashlib.sha256(json.dumps(account_observed, sort_keys=True).encode()).hexdigest()
    refusals = [row for row in rows if limits.effective_state(row, now) in _REFUSALS]
    recoverable_quota = any(
        (row.get("reset_at") or row.get("resets_at")) is None for row in quota["quota_restrictions"]
    )
    malformed = (
        any(row.get("state") not in _REFUSALS | {"ok", "unknown"} for row in rows)
        or any(row.get("reset_at") is not None and _time(row["reset_at"]) is None for row in rows)
        or any(
            _time(row.get("observed_at")) is None or _time(row.get("observed_at")) > now for row in refusals
        )
    )
    eligible = (bool(refusals) or recoverable_quota) and not (
        restrictions["future_reset"] or exhausted or malformed
    )
    return {
        **identity,
        "evidence_revision": revision,
        "account_evidence_revision": account_revision,
        "eligible": eligible,
        "account": account,
        "selected": selected,
        "next_action": "wait"
        if future_reset or exhausted
        else "inspect_evidence"
        if malformed
        else "arm"
        if eligible
        else "start",
    }


def _project(
    row: dict[str, Any] | None, evidence: dict[str, Any], now: datetime, model: str | None
) -> dict[str, Any]:
    result = {
        name: row.get(name) if row else None
        for name in (
            "permit_id",
            "provider",
            "status_key",
            "model",
            "created_at",
            "expires_at",
            "task_id",
            "outcome",
            "outcome_code",
        )
    }
    if not row:
        result.update(provider=evidence["provider"], status_key=evidence["status_key"], model=model)
    state = row["state"] if row else "none"
    if row and state == "armed" and (_time(row["expires_at"]) or now) <= now:
        state = "expired"
    active = state in ("armed", "claimed")
    can_arm = evidence["eligible"] and not active
    action = (
        "await_result"
        if state == "claimed"
        else "use_permit"
        if state == "armed"
        else evidence["next_action"]
    )
    if active and (
        row["provider"] != evidence["provider"]
        or row["model"] != model
        or row["evidence_revision"] != evidence["evidence_revision"]
    ):
        action = "inspect_permit"
    context_changed = bool(row and row.get("auth_context") != evidence["auth_context"])
    command = None
    if can_arm:
        args = ["taskspindle", "providers", "--retry-next", "--provider", evidence["provider"]]
        if model is not None:
            args.extend(["--model", model])
        command = shlex.join(args)
    return {
        **result,
        "state": state,
        "can_arm": can_arm,
        "next_action": action,
        "cli_command": command,
        "evidence_revision": evidence["evidence_revision"],
        "account_evidence_revision": evidence["account_evidence_revision"],
        "warning": "AUTH_CONTEXT_CHANGED" if context_changed else None,
    }


def status(
    store: Any, profile: Any, *, now: datetime, model: str | None = None, parent_env: Any = None
) -> dict[str, Any]:
    model = model if model is not None else profile.model
    from .service import TaskSpindleError

    try:
        evidence = _evidence(store, profile, now, model, parent_env)
    except TaskSpindleError:
        return {
            **dict.fromkeys(
                (
                    "permit_id",
                    "provider",
                    "status_key",
                    "model",
                    "created_at",
                    "expires_at",
                    "task_id",
                    "outcome",
                    "outcome_code",
                    "cli_command",
                    "evidence_revision",
                    "account_evidence_revision",
                )
            ),
            "state": "none",
            "can_arm": False,
            "next_action": "inspect_evidence",
        }
    reader = getattr(store, "latest_recovery_permit", None)
    row = reader(evidence["status_key"]) if reader else None
    return _project(row, evidence, now, model)


def arm(
    store: Any,
    profile: Any,
    *,
    evidence_revision: str,
    now: datetime,
    model: str | None = None,
    parent_env: Any = None,
) -> dict[str, Any]:
    model = model if model is not None else profile.model
    with store.transaction() as conn:
        evidence = _evidence(store, profile, now, model, parent_env)
        if evidence_revision != evidence["evidence_revision"]:
            _error("EVIDENCE_CHANGED", "Availability evidence changed; inspect it before arming.")
        if not evidence["eligible"]:
            _error("NOT_ELIGIBLE", "Current evidence does not allow a recovery attempt.")
        row = store.latest_recovery_permit(evidence["status_key"])
        if row and row["state"] == "armed" and (_time(row["expires_at"]) or now) <= now:
            conn.execute(
                "UPDATE provider_recovery_permits SET state = 'expired' WHERE permit_id = ?",
                (row["permit_id"],),
            )
            row = None
        if row and row["state"] in ("armed", "claimed"):
            if (
                row["provider"] == profile.id
                and row["model"] == model
                and row["evidence_revision"] == evidence_revision
            ):
                return _project(row, evidence, now, model)
            _error("ACTIVE_ATTEMPT", "A recovery attempt already exists for this provider account.")
        permit_id = "recovery-" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO provider_recovery_permits "
            "(permit_id, provider, status_key, model, state, evidence_revision, "
            "account_fingerprint, model_fingerprint, auth_context, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, 'armed', ?, ?, ?, ?, ?, ?)",
            (
                permit_id,
                profile.id,
                evidence["status_key"],
                model,
                evidence_revision,
                evidence["account_fingerprint"],
                evidence["model_fingerprint"],
                evidence["auth_context"],
                _stamp(now),
                _stamp(now + timedelta(hours=24)),
            ),
        )
        return _project(store.get_recovery_permit(permit_id), evidence, now, model)


def revoke(store: Any, permit_id: str, *, now: datetime) -> dict[str, Any]:
    with store.transaction() as conn:
        row = store.get_recovery_permit(permit_id)
        if row is None:
            _error("NOT_FOUND", "No such recovery permit.")
        if row["state"] == "claimed":
            _error("ALREADY_CLAIMED", "A claimed recovery attempt cannot be revoked.")
        if row["state"] == "armed":
            state = "expired" if (_time(row["expires_at"]) or now) <= now else "revoked"
            conn.execute(
                "UPDATE provider_recovery_permits SET state = ? WHERE permit_id = ?", (state, permit_id)
            )
            row["state"] = state
        return {
            **{
                name: row.get(name)
                for name in (
                    "permit_id",
                    "provider",
                    "status_key",
                    "model",
                    "state",
                    "created_at",
                    "expires_at",
                    "task_id",
                    "outcome",
                    "outcome_code",
                    "evidence_revision",
                )
            },
            "can_arm": False,
            "next_action": "inspect_evidence",
            "cli_command": None,
        }


def validate(
    store: Any,
    profile: Any,
    permit_id: str,
    *,
    now: datetime,
    model: str | None = None,
    task_id: str | None = None,
    parent_env: Any = None,
) -> dict[str, Any]:
    model = model if model is not None else profile.model
    key = _scope(profile, model)
    row = store.get_recovery_permit(permit_id)
    if row is None:
        _error("NOT_FOUND", "No such recovery permit.")
    if row["provider"] != profile.id or row["status_key"] != key:
        _error("SCOPE_MISMATCH", "Recovery is bound to a different named provider.")
    if row["model"] != model and not (row["model"] is None and task_id is not None):
        _error("SCOPE_MISMATCH", "Recovery is bound to a different model.")
    if row["state"] != ("claimed" if task_id is not None else "armed") or (
        task_id is not None and row["task_id"] != task_id
    ):
        _error("NOT_AVAILABLE", "Recovery permit has already been used or settled.")
    if (_time(row["expires_at"]) or now) <= now:
        _error("EXPIRED", "Recovery permit expired before prompt admission.")
    evidence = _evidence(store, profile, now, row["model"], parent_env)
    if not row.get("auth_context"):
        _error("AUTH_CONTEXT_UNBOUND", "Recovery permit predates auth-context binding; revoke and re-arm.")
    if row["auth_context"] != evidence["auth_context"]:
        _error("AUTH_CONTEXT_CHANGED", "Authentication metadata changed; revoke and re-arm recovery.")
    if evidence["evidence_revision"] != row["evidence_revision"]:
        _error("EVIDENCE_CHANGED", "Availability evidence changed after recovery was armed.")
    if not evidence["eligible"]:
        _error("NOT_ELIGIBLE", "Current evidence does not allow recovery.")
    if row["model"] is None and model is not None:
        resolved = store.get_provider_model_status(key, model)
        if resolved is not None and resolved.get("state") != "ok":
            _error("SCOPE_MISMATCH", "Default-model recovery cannot bypass a resolved-model refusal.")
    return row


def claim(
    store: Any,
    profile: Any,
    permit_id: str,
    task_id: str,
    *,
    now: datetime,
    model: str | None = None,
    parent_env: Any = None,
) -> dict[str, Any]:
    with store.transaction() as conn:
        validate(store, profile, permit_id, now=now, model=model, parent_env=parent_env)
        conn.execute(
            "UPDATE provider_recovery_permits SET state = 'claimed', task_id = ? "
            "WHERE permit_id = ? AND state = 'armed'",
            (task_id, permit_id),
        )
        return store.get_recovery_permit(permit_id)


def finish(store: Any, task_id: str, outcome: str, *, now: datetime, code: str | None = None) -> None:
    if outcome not in ("succeeded", "failed"):
        _error("INVALID_OUTCOME", "Recovery outcome must be succeeded or failed.")
    safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", code) else None
    with store.transaction() as conn:
        row = store.get_task_recovery_permit(task_id)
        if row is None or row["state"] != "claimed":
            return
        conn.execute(
            "UPDATE provider_recovery_permits SET state = ?, outcome = ?, outcome_code = ? "
            "WHERE task_id = ? AND state = 'claimed'",
            (outcome, outcome, safe_code, task_id),
        )
        from .models import EventKind

        store.append_event(
            task_id,
            EventKind.WARNING,
            {
                "code": "PROVIDER_RECOVERY_OUTCOME",
                "permit_id": row["permit_id"],
                "outcome": outcome,
                "outcome_code": safe_code,
            },
        )
