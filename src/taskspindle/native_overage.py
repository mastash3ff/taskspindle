"""Native account overage authorization, safe observations and bounded admission.

No inference or billing calls occur here. Unknown billing is never reported as free.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import limits, quota

INCLUDED_WINDOWS = frozenset(
    {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "seven_day_overage_included"}
)


def unknown() -> dict[str, Any]:
    return {
        "policy": None,
        "policy_fingerprint": None,
        "control_scope": None,
        "eligibility": "unknown",
        "admission_reason": "historical_unknown",
        "billing_classification": "unknown",
        "source": None,
        "observed_at": None,
        "observed": {},
    }


def fingerprint(profile: Any) -> str:
    value = {
        "profile": profile.id,
        "family": profile.family,
        "auth": profile.auth,
        "policy": profile.native_overage,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def current_profile(profile: Any, profiles: Mapping[str, Any], config_file: Path) -> Any:
    """Re-read policy on each boundary, including deletion of a prior opt-in."""
    from .config import load_config, native_overage_policies

    policies = native_overage_policies(load_config(config_file), profiles)
    return replace(profile, native_overage=policies[profile.id])


def normalize_observation(info: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    status = info.get("overageStatus")
    if isinstance(status, str) and status in {"allowed", "allowed_warning", "rejected"}:
        result["status"] = status
    reason = info.get("overageDisabledReason")
    if isinstance(reason, str) and reason in {
        "overage_not_provisioned",
        "org_level_disabled",
        "org_level_disabled_until",
        "out_of_credits",
        "seat_tier_level_disabled",
        "member_level_disabled",
        "seat_tier_zero_credit_limit",
        "group_zero_credit_limit",
        "member_zero_credit_limit",
        "org_service_level_disabled",
        "no_limits_configured",
        "fetch_error",
        "unknown",
    }:
        result["disabled_reason"] = reason
    flags = [info[key] for key in ("isUsingOverage", "overageInUse") if key in info]
    if flags and all(type(value) is bool for value in flags) and len(set(flags)) == 1:
        result["in_use"] = flags[0]
    value = info.get("overageResetsAt")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            stamp = limits.epoch_to_iso(value)
        except (ValueError, OverflowError, OSError):
            stamp = None
        if stamp:
            result["resets_at"] = stamp
    return result


def _paid_evidence(store: Any, evidence: dict[str, Any], now: datetime) -> tuple[Any, bool, Any]:
    observed = getattr(store, "latest_native_overage_observation", lambda _key: None)(evidence["status_key"])
    if not observed or observed.get("auth_context") != evidence["auth_context"]["fingerprint"]:
        return None, False, None
    fields = observed["observed"]
    reason = fields.get("disabled_reason")
    rejected = fields.get("status") == "rejected" or reason not in {None, "fetch_error", "unknown"}
    reset = quota._time(fields.get("resets_at"))
    # Only a timed credit window or an explicit disabled-until condition can expire.
    # Permanent administrative/provisioning settings require new affirmative evidence.
    observed_at = quota._time(fields.get("paid_observed_at") or observed.get("observed_at"))
    expired = bool(
        rejected
        and reset is not None
        and reset <= now
        and observed_at is not None
        and observed_at < reset
        and reason in {None, "out_of_credits", "org_level_disabled_until"}
    )
    generation = None
    if expired:
        generation = {"paid_reset": reset.isoformat()}
    elif (
        not rejected
        and fields.get("status") in {"allowed", "allowed_warning"}
        and fields.get("paid_generation", 0)
    ):
        generation = {
            "paid_status": "allowed",
            "transition": fields.get("paid_generation", 0),
        }
    return observed, rejected and not expired, generation


def _claim_identity(profile: Any, evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": evidence["status_key"],
        "auth": evidence["auth_context"]["fingerprint"],
        "model": evidence["model"],
        "evidence": sorted(
            (
                str(row.get("scope")),
                str(row.get("model")),
                str(row.get("window")),
                str(row.get("period_start")),
                str(row.get("reset_at") or row.get("resets_at")),
            )
            for row in evidence["quota_restrictions"]
        ),
        "policy": profile.native_overage,
    }


def _identity_key(identity: Mapping[str, Any], paid_generation: Any = None) -> str:
    value = dict(identity) | {"paid_generation": paid_generation}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _claim_key(profile: Any, evidence: dict[str, Any], paid_generation: Any = None) -> str:
    return _identity_key(_claim_identity(profile, evidence), paid_generation)


def project(
    store: Any,
    profile: Any,
    now: datetime,
    model: str | None = None,
    parent_env: Any = None,
    task_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = evidence or quota.evaluate(store, profile, now, model, parent_env)
    restrictions = evidence["quota_restrictions"]
    result = unknown() | {
        "policy": profile.native_overage,
        "policy_fingerprint": fingerprint(profile),
        "control_scope": "worker" if profile.family == "agy" else "account",
        "admission_reason": "no_included_exhaustion_evidence",
    }
    observed, paid_blocked, paid_generation = _paid_evidence(store, evidence, now)
    if observed:
        result.update({key: observed.get(key) for key in ("observed", "observed_at", "source")})
    hard = bool(evidence["blocking_rows"])
    status = store.get_provider_status(evidence["status_key"])
    # An independent short throttle must survive an older included restriction.
    if status and limits.effective_state(status, now) == "throttled":
        hard |= status.get("window") not in INCLUDED_WINDOWS
    hard |= any(
        row.get("window") not in INCLUDED_WINDOWS and row.get("source") != "native_check"
        for row in restrictions
    )
    if hard:
        result.update(eligibility="blocked", admission_reason="hard_provider_block")
        return result
    if not restrictions:
        # Service success is not included-funds evidence. A fresh native quota
        # check explicitly showing remaining included allowance is narrower proof.
        native = evidence["native"]
        if (
            native.get("state") == "quota"
            and native.get("freshness") == "fresh"
            and native.get("eligible_hint") is True
            and not evidence["auth_context"].get("changed")
        ):
            result.update(eligibility="included", admission_reason="current_native_included_allowance")
        return result
    result.update(eligibility="blocked", admission_reason="included_exhausted_observe_only")
    if profile.auth != "oauth" or profile.native_overage != "provider_managed":
        return result
    if paid_blocked:
        result.update(eligibility="blocked", admission_reason="native_paid_allowance_unavailable")
        return result
    active = getattr(store, "list_native_overage_attempts", lambda: [])()
    for attempt in active:
        if (
            attempt["state"] in {"claimed", "prompting"}
            and attempt["status_key"] == evidence["status_key"]
            and attempt["auth_context"] == evidence["auth_context"]["fingerprint"]
            and attempt["model"] == evidence["model"]
            and (attempt["task_id"] != task_id or attempt["state"] == "prompting")
        ):
            result.update(eligibility="blocked", admission_reason="native_attempt_pending")
            return result
    key = _claim_key(profile, evidence, paid_generation)
    claim = getattr(store, "get_native_overage_attempt", lambda _key: None)(key)
    if claim and claim["state"] not in {"succeeded", "superseded"}:
        own = claim["task_id"] == task_id and claim["state"] == "claimed"
        if not own:
            result["admission_reason"] = (
                "native_attempt_pending"
                if claim["state"] in {"claimed", "prompting"}
                else "native_attempt_refused"
            )
            return result
    result.update(eligibility="overage", admission_reason="provider_enforced_native_attempt")
    return result


def snapshot(store: Any, profile: Any, now: datetime, **kwargs: Any) -> dict[str, Any]:
    """A turn records authorization, not a claim that the account charged it."""
    result = project(store, profile, now, **kwargs)
    # Account observations are useful for admission but cannot classify this new turn.
    result["auth_context"] = quota._auth_context(store, profile, kwargs.get("parent_env"))["fingerprint"]
    result["billing_classification"] = "unknown"
    result["observed"] = {}
    result["source"] = None
    result["observed_at"] = None
    return result


def admit(
    store: Any,
    profile: Any,
    turn: dict[str, Any],
    now: datetime,
    *,
    model: str | None,
    parent_env: Any,
    prompting: bool = False,
) -> dict[str, Any]:
    from .service import TaskSpindleError

    saved = turn.get("native_overage")
    # Historical turns have no spending authority, even if a policy is later added.
    if not saved or not saved.get("policy_fingerprint"):
        saved = (
            unknown()
            | (saved or {})
            | {
                "policy": "observe_only",
                "policy_fingerprint": fingerprint(replace(profile, native_overage="observe_only")),
            }
        )
    if saved["policy_fingerprint"] != fingerprint(profile):
        raise TaskSpindleError(
            "NATIVE_OVERAGE_POLICY_CHANGED", "Native overage policy changed since this turn was authorized."
        )
    evidence = quota.evaluate(store, profile, now, model, parent_env)
    if saved.get("auth_context") and saved["auth_context"] != evidence["auth_context"]["fingerprint"]:
        raise TaskSpindleError(
            "AUTH_CONTEXT_CHANGED", "Authentication context changed since this turn was authorized."
        )
    projection = project(store, profile, now, model, parent_env, turn["task_id"], evidence)
    if projection["eligibility"] == "overage":
        _, _, paid_generation = _paid_evidence(store, evidence, now)
        key = _claim_key(profile, evidence, paid_generation)
        if not store.claim_native_overage(key, turn["task_id"], turn["id"], evidence, fingerprint(profile)):
            raise TaskSpindleError(
                "NATIVE_OVERAGE_PENDING", "This native attempt is already pending or was refused."
            )
        store.update_turn_native_overage(
            turn["id"],
            {
                "eligibility": "overage",
                "admission_reason": projection["admission_reason"],
                "claim_key": key,
                "paid_generation": paid_generation,
                "claim_identity": _claim_identity(profile, evidence),
            },
        )
        if prompting and not store.mark_native_overage_prompting(key, turn["id"]):
            raise TaskSpindleError("NATIVE_OVERAGE_SPENT", "This native attempt has already prompted.")
    elif saved.get("eligibility") == "overage" and projection["eligibility"] == "blocked":
        raise TaskSpindleError("NATIVE_OVERAGE_BLOCKED", projection["admission_reason"])
    return projection


def finish(
    store: Any,
    profile: Any,
    turn_id: int,
    success: bool,
    *,
    now: datetime,
    parent_env: Any,
    code: str | None = None,
) -> None:
    """Fence same-attempt paid metadata together with its original admission key."""
    turn = next((row for row in store.list_native_overage_turns() if row["turn_id"] == turn_id), None)
    saved = turn["native_overage"] if turn else {}
    key = saved.get("claim_key")
    final_key = None
    if key:
        identity = saved.get("claim_identity")
        if not isinstance(identity, dict) and profile is not None:
            # Compatibility for turns created before captured claim identity existed.
            attempt = store.get_native_overage_attempt(key)
            evidence = quota.evaluate(store, profile, now, attempt["model"], parent_env) if attempt else None
            if evidence:
                identity = _claim_identity(profile, evidence)
        if isinstance(identity, dict) and _identity_key(identity, saved.get("paid_generation")) == key:
            # Settlement uses the original account/model/evidence, including after
            # removal or reconfiguration of its profile. It grants no new admission.
            context = {"status_key": identity["provider"], "auth_context": {"fingerprint": identity["auth"]}}
            observed, _, generation = _paid_evidence(store, context, now)
            if observed and observed["observed"].get("paid_turn_id") == turn_id:
                final_key = _identity_key(identity, generation)
    store.finish_native_overage(turn_id, success, code, final_claim_key=final_key)
