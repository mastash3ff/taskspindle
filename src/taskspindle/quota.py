"""Bounded quota policy derived from recorded, source-specific observations.

This module deliberately does not ask a provider anything.  It projects durable quota
restrictions and makes the distinction between a quota window and an auth/access/model
refusal explicit, so a late five-hour observation cannot hide a weekly restriction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from . import limits
from .access_checks import cached_native_check, native_fingerprint

QUOTA_STATES = frozenset({"throttled", "quota"})
REFUSAL_STATES = frozenset({"auth_expired", "access_denied", "model_unavailable"})


def model_family(model: str | None) -> str | None:
    """Canonical Claude family selector used for model-scoped weekly windows."""
    if not isinstance(model, str) or not model.strip():
        return None
    lowered = model.lower().strip()
    # Names are provider identifiers, not prose: only a supported Claude model
    # token is classified.  Unknown names deliberately remain conservative.
    match = re.fullmatch(
        r"claude-(?:(?:\d+(?:-\d+)?)-)?(opus|sonnet|haiku)"
        r"(?:-(?:\d+(?:-\d+)?|\d{8}))?",
        lowered,
    )
    if match:
        return match.group(1)
    if lowered in {"opus", "sonnet", "haiku"}:
        return lowered
    return None


def window_applies(window: Any, model: str | None, *, defer_model: bool = False) -> bool:
    """Whether a quota window restricts this explicitly selected model.

    An omitted model defers a family-only decision.  An unrecognised *specified*
    model remains conservative: it cannot evade an unresolved family restriction.
    """
    if defer_model and model is None and window in {"seven_day_opus", "seven_day_sonnet"}:
        return False
    if window == "seven_day_opus":
        return model is None or model_family(model) in {"opus", None}
    if window == "seven_day_sonnet":
        return model is None or model_family(model) in {"sonnet", None}
    return True


def _scope_applies(row: Mapping[str, Any], model: str | None, *, defer_model: bool) -> bool:
    """Apply persisted scope first; window literals are only legacy compatibility."""
    scope = row.get("scope")
    selector = row.get("model")
    if scope == "account":
        return True
    if scope == "model":
        return model is not None and selector == model
    if scope == "model_family":
        if model is None:
            return not defer_model
        family = model_family(model)
        # A supplied unknown model cannot evade a family restriction. Known other
        # families (including Haiku) are intentionally unaffected.
        return family is None or family == selector
    return window_applies(row.get("window") or row.get("period_key"), model, defer_model=defer_model)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _fingerprint(row: Mapping[str, Any]) -> str:
    value = {
        key: row.get(key)
        for key in (
            "provider",
            "status_key",
            "scope",
            "model",
            "window",
            "period_key",
            "period_start",
            "reset_at",
            "observed_at",
            "source",
            "status",
            "state",
            "used_percent",
            "evidence_fingerprint",
        )
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _auth_context(store: Any, profile: Any, parent_env: Any) -> dict[str, Any]:
    key = limits.status_key(profile)
    try:
        from .auth_context import fingerprint

        current = fingerprint(profile, parent_env if parent_env is not None else os.environ)
    except Exception:
        # Compatibility for pre-v8 callers.  This is still opaque and never includes secrets.
        current = native_fingerprint(profile, parent_env if parent_env is not None else os.environ)
    getter = getattr(store, "get_provider_auth_context", None)
    observed = getter(key) if getter else None
    previous = observed.get("fingerprint") if isinstance(observed, Mapping) else observed
    return {
        "fingerprint": current,
        "changed": bool(previous and previous != current),
        "observed": previous if isinstance(previous, str) else None,
    }


def _window_rows(store: Any, key: str) -> list[dict[str, Any]]:
    reader = getattr(store, "list_quota_restrictions", None)
    restrictions = [dict(row) for row in reader(key, unresolved_only=True)] if reader else []
    # v8 restrictions are authoritative once recorded, but older durable window rows
    # remain relevant after migration and during a mixed-version rolling upgrade.
    version_reader = getattr(store, "schema_version", None)
    version = version_reader() if version_reader else None
    if reader and (version is None or version >= 8):
        return restrictions
    windows_reader = getattr(store, "latest_provider_windows", None)
    return [dict(row) for row in (windows_reader(key) if windows_reader else [])]


def _is_unresolved(row: Mapping[str, Any], now: datetime) -> bool:
    if row.get("resolved_at") is not None:
        return False
    reset = _time(row.get("reset_at") or row.get("resets_at"))
    # Missing/future reset remains a restriction. Passed reset makes it eligible for
    # a one-shot retry, but it does not prove current access or erase other periods.
    return reset is None or reset > now


def evaluate(
    store: Any,
    profile: Any,
    now: datetime,
    model: str | None = None,
    parent_env: Any = None,
    defer_model: bool = False,
) -> dict[str, Any]:
    """Return the complete quota projection for a provider/model selection."""
    selected_model = profile.model if model is None else model
    family_selector = None if defer_model and model is None and selected_model is None else selected_model
    key = limits.status_key(profile)
    auth = _auth_context(store, profile, parent_env)
    rows = _window_rows(store, key)
    applicable: list[dict[str, Any]] = []
    retry: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        status = row.get("status") or row.get("state")
        # allowed 100% is telemetry, never a block.  Rejected is explicit evidence.
        # v8 restriction rows are themselves explicit rejections; their schema has
        # no separate status/state column.  Legacy windows require `rejected`.
        explicit = (
            status == "rejected"
            or row.get("state") == "throttled"
            or (row.get("period_key") is not None and "state" not in row)
        )
        if not explicit:
            continue
        row["fingerprint"] = row.get("evidence_fingerprint") or _fingerprint(row)
        row["source"] = limits.safe_provider_source(row.get("source"))
        row["model_family"] = row.get("model") if row.get("scope") == "model_family" else None
        row["reset"] = row.get("reset_at") or row.get("resets_at")
        row["observed"] = row.get("observed_at")
        if not _scope_applies(row, family_selector, defer_model=defer_model):
            # Pre-preparation has no resolved model.  Do expose a passed family
            # restriction as retry authority; do not pre-block a family that may
            # not be selected.  Prompt admission evaluates the actual model.
            if defer_model and family_selector is None and not _is_unresolved(row, now):
                retry.append(row)
            continue
        if _is_unresolved(row, now):
            applicable.append(row)
        else:
            retry.append(row)
    native = cached_native_check(store, profile, parent_env, now=now)
    native_exhausted = native.get("eligible_hint") is False
    retained = native.get("last_success") if isinstance(native.get("last_success"), Mapping) else None
    candidate = native if native.get("state") == "quota" else retained
    observed = _time(candidate.get("checked_at")) if isinstance(candidate, Mapping) else None
    reset = _time(candidate.get("reset_at")) if isinstance(candidate, Mapping) else None
    source_stale = observed is None or (now - observed).total_seconds() >= 300
    native_retry = bool(
        not native_exhausted
        and isinstance(candidate, Mapping)
        and candidate.get("used_percent") == 100
        and ((reset is not None and reset <= now) or source_stale)
    )
    if native_exhausted or native_retry:
        sample = native if native_exhausted else candidate
        native_key = native_fingerprint(profile, parent_env if parent_env is not None else os.environ)
        native_row = {
            "source": "native_check",
            "window": sample.get("window"),
            "reset_at": sample.get("reset_at"),
            "observed_at": sample.get("checked_at"),
            "status": "rejected",
            "used_percent": sample.get("used_percent"),
            "scope": "account",
            "fingerprint": hashlib.sha256(
                json.dumps(
                    {
                        "native": native_key,
                        "checked_at": sample.get("checked_at"),
                        "period_start": sample.get("period_start"),
                        "reset_at": sample.get("reset_at"),
                        "used_percent": sample.get("used_percent"),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
        }
        # A fresh native 100 with an active period blocks; an expired period goes to retry.
        if native_exhausted and _is_unresolved(native_row, now):
            applicable.append(native_row)
        else:
            retry.append(native_row)
    account = store.get_provider_status(key)
    model_row = store.get_provider_model_status(key, selected_model) if selected_model else None
    refusals = []
    for row in (account, model_row):
        if row and limits.effective_state(row, now) in REFUSAL_STATES:
            refusals.append(dict(row))
    fingerprints = sorted({str(row["fingerprint"]) for row in applicable + retry})
    active = getattr(store, "get_active_quota_retry_claim", lambda _key: None)(key)
    success = getattr(store, "successful_quota_retry_fingerprints", lambda _key: set())(key)
    retry_fingerprints = [row["fingerprint"] for row in retry if row["fingerprint"] not in success]
    state = "blocked" if applicable or refusals else "retry" if retry_fingerprints else "available"
    return {
        "state": state,
        "status_key": key,
        "model": selected_model,
        "quota_restrictions": applicable,
        "blocking_rows": refusals,
        "retry_fingerprints": retry_fingerprints,
        "evidence_fingerprints": fingerprints,
        "auth_context": auth,
        "native": native,
        "quota_retry": {
            "state": active.get("state") if active else "none",
            "task_id": active.get("task_id") if active else None,
            "fingerprints": retry_fingerprints,
        },
    }


def claim_retry(
    store: Any, profile: Any, task_id: str, *, now: datetime, model: str | None = None, parent_env: Any = None
) -> dict[str, Any] | None:
    """Claim one passed-reset/stale-native retry; ordinary tasks never claim this authority."""
    verdict = evaluate(store, profile, now, model, parent_env)
    if verdict["state"] != "retry" or not verdict["retry_fingerprints"]:
        return None
    claim = getattr(store, "claim_quota_retry", None)
    if not claim:
        return None
    return claim(verdict["status_key"], task_id, restriction_fingerprints=verdict["retry_fingerprints"])


def resolve(
    store: Any,
    profile: Any,
    fingerprints: list[str],
    *,
    now: datetime,
    task_id: str | None = None,
    parent_env: Any = None,
    captured_auth_context: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve only captured restrictions while the account metadata still matches."""
    auth = _auth_context(store, profile, parent_env)
    if captured_auth_context is not None and auth["fingerprint"] != captured_auth_context:
        return []
    writer = getattr(store, "resolve_quota_restrictions", None)
    if not writer:
        return []
    return writer(
        limits.status_key(profile),
        fingerprints,
        task_id=task_id,
        resolved_at=now.isoformat().replace("+00:00", "Z"),
        resolved_context=auth["fingerprint"],
    )
