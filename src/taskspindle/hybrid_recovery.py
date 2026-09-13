"""Durable, bounded recovery admission for necessary model work; never launches a probe."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from . import auth_context, limits, quota
from .access_checks import cached_native_check
from .provider_recovery import _stamp, _time

_REFUSALS = {"auth_expired", "access_denied", "throttled", "model_unavailable"}
_DELAYS = (5, 15, 60)


def record_refusal(
    store: Any,
    key: str,
    kind: str,
    observed_at: str,
    *,
    model: str | None = None,
    window: str | None = None,
    quota_scope: str = "account",
) -> None:
    """Continue an unresolved episode instead of refreshing its budget on every refusal."""
    if kind not in _REFUSALS or store.schema_version() < 10:
        return
    if kind == "throttled" and window in {"seven_day_opus", "seven_day_sonnet"} and model is None:
        quota_scope, model = "model_family", window.removeprefix("seven_day_")
    scope = "quota:" + (window or "unknown") if kind == "throttled" else "model" if model else "account"
    now = _time(observed_at)
    if now is None:
        return
    conn = store._conn
    row = conn.execute(
        "SELECT * FROM recovery_episodes WHERE status_key=? AND scope=? AND model=? AND resolved_at IS NULL",
        (key, scope, model or ""),
    ).fetchone()
    if row:
        used = row["attempts_used"]
        next_at = _stamp(now + timedelta(minutes=_DELAYS[used])) if used < 3 else None
        conn.execute(
            "UPDATE recovery_episodes SET refusal_kind=?, observed_at=?, next_attempt_at=? "
            "WHERE episode_id=?",
            (kind, observed_at, next_at, row["episode_id"]),
        )
    else:
        revision = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM recovery_evidence WHERE status_key=?", (key,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO recovery_episodes(episode_id,status_key,scope,model,refusal_kind,"
            "observed_at,next_attempt_at,consumed_revision,quota_scope) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                key,
                scope,
                model or "",
                kind,
                observed_at,
                _stamp(now + timedelta(minutes=5)),
                revision,
                quota_scope,
            ),
        )


def record_native_evidence(store: Any, key: str, result: dict[str, Any], at: str) -> None:
    """Persist semantic edges only; unknown observations retain the last explicit fact."""
    if store.schema_version() < 10:
        return
    conn = store._conn
    row = conn.execute("SELECT payload FROM recovery_native_semantics WHERE status_key=?", (key,)).fetchone()
    before = json.loads(row[0]) if row else {}
    after = dict(before)
    state = result.get("state")
    if not isinstance(state, str):
        return
    positive: list[tuple[str, str]] = []
    authenticated = state in {"cached_auth", "catalog_access", "quota"}
    if state == "auth_required" or authenticated:
        after["auth"] = authenticated
        if before.get("auth") is False and authenticated:
            positive.append(("auth", ""))
    if state == "catalog_access" and isinstance(result.get("model_ids"), list):
        ids = sorted(
            {
                value
                for value in result["model_ids"]
                if isinstance(value, str) and limits._model(value) == value
            }
        )
        if "models" in before:
            positive.extend(("model", value) for value in ids if value not in before["models"])
        after["models"] = ids
    used = result.get("used_percent")
    if state == "quota" and type(used) in {int, float} and 0 <= used <= 100:
        period = result.get("period_start")
        end = _time(result.get("reset_at"))
        start = _time(period)
        checked = _time(at)
        if start and end and checked and start <= checked < end:
            if used == 100 and result.get("window") in {"weekly", "monthly"}:
                existing = conn.execute(
                    "SELECT 1 FROM recovery_episodes WHERE status_key=? AND scope=? AND model='' "
                    "AND resolved_at IS NULL",
                    (key, "quota:" + result["window"]),
                ).fetchone()
                if existing is None:
                    # A diagnostic opens a budget but never consumes it or refreshes an existing
                    # episode. The shared model-turn claim governs recovery once freshness lapses.
                    record_refusal(store, key, "throttled", at, window=result["window"])
            current = {"available": used < 100, "period": period, "window": result.get("window")}
            previous = before.get("quota")
            if (
                previous
                and current["available"]
                and (not previous["available"] or previous["period"] != period)
                and previous["window"] == current["window"]
            ):
                positive.append(("quota", current["window"] or "unknown"))
            after["quota"] = current
    for kind, model in positive:
        conn.execute(
            "INSERT INTO recovery_evidence(status_key,kind,model,observed_at) VALUES(?,?,?,?)",
            (key, kind, model, at),
        )
    conn.execute(
        "INSERT INTO recovery_native_semantics VALUES(?,?) ON CONFLICT(status_key) DO UPDATE "
        "SET payload=excluded.payload",
        (key, json.dumps(after)),
    )


def _read(store: Any, method: str, key: str, default: Any) -> Any:
    reader = getattr(store, method, None)
    return reader(key) if reader else default


def _episodes(store: Any, profile: Any, model: str | None, defer_model: bool = False) -> list[dict[str, Any]]:
    key = limits.status_key(profile)
    rows = _read(store, "list_recovery_episodes", key, [])
    result = []
    for row in rows:
        if row["scope"] == "model" and row["model"] != model:
            continue
        if row["scope"].startswith("quota:") and not quota._scope_applies(
            {"window": row["scope"][6:], "scope": row["quota_scope"], "model": row["model"]},
            model,
            defer_model=defer_model,
        ):
            continue
        # A resolved real turn supersedes its episode, even for old or read-only adapters.
        current = (
            store.get_provider_model_status(key, row["model"])
            if row["scope"] == "model"
            else store.get_provider_status(key)
        )
        if (
            not row["scope"].startswith("quota:")
            and current
            and current.get("state") == "ok"
            and current.get("source") == "turn_ok"
            and (
                (_time(current.get("observed_at")) or datetime.min)
                >= (_time(row["observed_at"]) or datetime.max)
            )
        ):
            continue
        result.append(row)
    return result


def _positive(episode: dict[str, Any], evidence: list[dict[str, Any]]) -> int:
    def relevant(event: dict[str, Any]) -> bool:
        kind = episode["refusal_kind"]
        if kind == "auth_expired":
            return event["kind"] == "auth"
        if kind == "model_unavailable":
            return event["kind"] == "model" and event["model"] == episode["model"]
        if kind in {"throttled", "access_denied"}:
            window = episode["scope"][6:] if episode["scope"].startswith("quota:") else None
            return event["kind"] == "quota" and window in {None, "unknown", event["model"]}
        return False

    return max((event["revision"] for event in evidence if relevant(event)), default=0)


def _native_block(native: dict[str, Any], model: str | None) -> str | None:
    if native.get("freshness") != "fresh":
        return None
    if native.get("state") == "auth_required":
        return "native_auth_required"
    if native.get("eligible_hint") is False:
        return "native_quota_exhausted"
    if (
        model is not None
        and native.get("state") == "catalog_access"
        and isinstance(native.get("model_ids"), list)
        and model not in native["model_ids"]
    ):
        return "native_model_absent"
    return None


def status(
    store: Any,
    profile: Any,
    *,
    now: datetime,
    model: str | None = None,
    parent_env: Any = None,
    task_id: str | None = None,
    defer_model: bool = False,
) -> dict[str, Any]:
    """A read-only projection. Merely inspecting readiness never reserves model work."""
    model = model if model is not None else profile.model
    key = limits.status_key(profile)
    episodes = _episodes(store, profile, model, defer_model)
    evidence = _read(store, "list_recovery_evidence", key, [])
    claim = _read(store, "active_recovery_claim", key, None)
    native = cached_native_check(store, profile, parent_env, now=now)
    block = _native_block(native, model)
    quota_view = quota.evaluate(store, profile, now, model, parent_env, defer_model)
    rows = [store.get_provider_status(key), store.get_provider_model_status(key, model) if model else None]
    if any(
        row
        and (
            row.get("state") != "throttled"
            or quota.window_applies(row.get("window"), model, defer_model=defer_model)
        )
        and row.get("reset_at")
        and (_time(row["reset_at"]) is None or _time(row["reset_at"]) > now)
        for row in rows
    ):
        block = block or "provider_reset_pending"
    if any(
        (_time(row.get("reset_at") or row.get("resets_at")) or now) > now
        for row in quota_view["quota_restrictions"]
    ):
        block = block or "provider_reset_pending"
    held = [
        row
        for row in episodes
        if row["attempts_used"] >= 3 and _positive(row, evidence) <= row["consumed_revision"]
    ]
    dates = [_time(row["next_attempt_at"]) for row in episodes if row["attempts_used"] < 3]
    next_at = max((date for date in dates if date), default=None)
    state = (
        "held"
        if held
        else "cooldown"
        if next_at and next_at > now
        else ("trial_ready" if episodes else "eligible")
    )
    reason = "attempts_exhausted" if held else None
    if block:
        state, reason = "held", block
    if claim:
        state, reason = "trial_running", block
    policy = getattr(profile, "provider_recovery", "manual")
    if policy == "manual" and episodes and not claim:
        state, reason = "held", "manual_policy"
    used = max((row["attempts_used"] for row in episodes), default=0)
    remaining = min(
        (
            max(0, 3 - row["attempts_used"])
            if row["attempts_used"] < 3
            else int(_positive(row, evidence) > row["consumed_revision"])
            for row in episodes
        ),
        default=3,
    )
    identity = [
        (row["episode_id"], row["attempts_used"], row["consumed_revision"], _positive(row, evidence))
        for row in episodes
    ]
    return {
        "policy": policy,
        "state": state,
        "attempts_used": used,
        "attempts_remaining": remaining,
        "next_attempt_at": _stamp(next_at) if state == "cooldown" and next_at else None,
        "hold_reason": reason,
        "active_task_id": claim["task_id"] if claim else None,
        "episode_id": episodes[0]["episode_id"] if episodes else None,
        "evidence_revision": hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
    }


def admit(
    store: Any,
    profile: Any,
    task_id: str,
    *,
    now: datetime,
    model: str | None = None,
    parent_env: Any = None,
    prompting: bool = False,
    defer_model: bool = False,
) -> bool:
    """Claim in the task/turn transaction, then recheck and consume immediately before prompting."""
    from .service import TaskSpindleError

    if getattr(profile, "provider_recovery", "manual") != "hybrid":
        return False
    key = limits.status_key(profile)
    model = model if model is not None else profile.model
    with store.transaction() as conn:
        manual = store.latest_recovery_permit(key)
        if (
            manual
            and manual["state"] in {"armed", "claimed"}
            and (manual["state"] == "claimed" or (_time(manual["expires_at"]) or now) > now)
        ):
            raise TaskSpindleError("RECOVERY_ACTIVE_ATTEMPT", "A manual recovery permit owns this account.")
        quota_claim = store.get_active_quota_retry_claim(key)
        if quota_claim and quota_claim["task_id"] != task_id:
            raise TaskSpindleError("RECOVERY_ACTIVE_ATTEMPT", "A quota retry owns this account.")
        view = status(
            store,
            profile,
            now=now,
            model=model,
            parent_env=parent_env,
            task_id=task_id,
            defer_model=defer_model,
        )
        current = store.active_recovery_claim(key)
        if view["hold_reason"] or view["state"] == "cooldown":
            raise TaskSpindleError("RECOVERY_NOT_READY", "Automatic recovery is not ready.", details=view)
        if current and current["task_id"] != task_id:
            raise TaskSpindleError(
                "RECOVERY_ACTIVE_ATTEMPT", "Another task holds this account's trial.", details=view
            )
        episodes = _episodes(store, profile, model, defer_model)
        context = auth_context.fingerprint(profile, parent_env)
        if current:
            captured = json.loads(current["episodes"])
            if current["provider"] != profile.id or current["auth_context"] != context:
                raise TaskSpindleError(
                    "RECOVERY_CONTEXT_CHANGED", "Recovery identity changed before prompting."
                )
            if current["model"] != model and (
                current["model"] is not None or any(row["scope"] == "model" for row in episodes)
            ):
                raise TaskSpindleError("RECOVERY_SCOPE_MISMATCH", "Recovery model scope changed.")
            if any(row["episode_id"] not in captured for row in episodes):
                raise TaskSpindleError("RECOVERY_EVIDENCE_CHANGED", "New refusal scope blocks this trial.")
            if current["state"] == "claimed" and any(
                captured[row["episode_id"]] != row["observed_at"] for row in episodes
            ):
                raise TaskSpindleError("RECOVERY_EVIDENCE_CHANGED", "A new refusal blocks this trial.")
        elif episodes:
            captured = {row["episode_id"]: row["observed_at"] for row in episodes}
            claim_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO recovery_claims(claim_id,status_key,provider,task_id,model,episodes,"
                "auth_context,state,created_at) VALUES(?,?,?,?,?,?,?,'claimed',?)",
                (claim_id, key, profile.id, task_id, model, json.dumps(captured), context, _stamp(now)),
            )
            current = store.active_recovery_claim(key)
        if current and prompting:
            if current["state"] != "claimed":
                raise TaskSpindleError("RECOVERY_ALREADY_PROMPTED", "The recovery trial already prompted.")
            events = store.list_recovery_evidence(key)
            for row in episodes:
                conn.execute(
                    "UPDATE recovery_episodes SET attempts_used=MIN(3,attempts_used+1), "
                    "consumed_revision=MAX(consumed_revision,?) WHERE episode_id=?",
                    (_positive(row, events), row["episode_id"]),
                )
            conn.execute(
                "UPDATE recovery_claims SET state='prompting' WHERE claim_id=?", (current["claim_id"],)
            )
        return current is not None


def finish(store: Any, task_id: str, outcome: str, *, now: datetime, code: str | None = None) -> None:
    """Settle all terminal paths once; failed diagnostics before prompting spend no attempt."""
    with store.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM recovery_claims WHERE task_id=? AND state IN ('claimed','prompting')", (task_id,)
        ).fetchall()
        for claim in rows:
            captured = json.loads(claim["episodes"])
            for episode_id, observed in captured.items():
                row = conn.execute(
                    "SELECT * FROM recovery_episodes WHERE episode_id=?", (episode_id,)
                ).fetchone()
                if row is None or row["resolved_at"]:
                    continue
                if outcome == "succeeded" and row["observed_at"] == observed:
                    conn.execute(
                        "UPDATE recovery_episodes SET resolved_at=? WHERE episode_id=?",
                        (_stamp(now), episode_id),
                    )
                elif claim["state"] == "prompting":
                    used = row["attempts_used"]
                    next_at = _stamp(now + timedelta(minutes=_DELAYS[used])) if used < 3 else None
                    revision = max(
                        (item["revision"] for item in store.list_recovery_evidence(claim["status_key"])),
                        default=0,
                    )
                    conn.execute(
                        "UPDATE recovery_episodes SET next_attempt_at=?, consumed_revision="
                        "MAX(consumed_revision,?) WHERE episode_id=?",
                        (next_at, revision, episode_id),
                    )
            conn.execute(
                "UPDATE recovery_claims SET state=?,finished_at=?,outcome_code=? WHERE claim_id=?",
                (outcome, _stamp(now), code, claim["claim_id"]),
            )
