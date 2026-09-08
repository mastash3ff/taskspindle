"""The TaskSpindle state machine: the transitions, the validators and the projections.

Everything here is a rule about a task, decided from the store alone. The sequencing that turns
those rules into tool behaviour -- git, systemd, the filesystem -- lives in
:mod:`taskspindle.orchestrator`, which imports this module and not the other way round.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from . import limits
from .models import (
    AcceptTaskRequest,
    AuthMode,
    CheckRecord,
    CleanupState,
    Disposition,
    ErrorBody,
    EventKind,
    Mode,
    ReviewFinding,
    Severity,
    StartTaskRequest,
    TaskRecord,
    TaskResult,
    TaskState,
    TaskView,
    Verdict,
)
from .providers import Profile
from .store import ProviderStatusReader, StaleStateVersionError, Store, now

# -- error codes --------------------------------------------------------------------

ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
STALE_STATE_VERSION = "STALE_STATE_VERSION"
TASK_NOT_FOUND = "TASK_NOT_FOUND"
MODE_FORBIDS_STATE = "MODE_FORBIDS_STATE"
GRANT_MISSING = "GRANT_MISSING"
LEASE_BUSY = "LEASE_BUSY"
DIFF_NOT_FULLY_RETRIEVED = "DIFF_NOT_FULLY_RETRIEVED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
REVIEW_STALE = "REVIEW_STALE"
REVIEW_BLOCKED = "REVIEW_BLOCKED"
CANDIDATE_MISMATCH = "CANDIDATE_MISMATCH"
TARGET_MOVED = "TARGET_MOVED"
INVALID_REQUEST = "INVALID_REQUEST"
METERED_NOT_ALLOWED = "METERED_NOT_ALLOWED"
REVIEWER_NOT_INDEPENDENT = "REVIEWER_NOT_INDEPENDENT"
PROVIDER_FAMILY_UNKNOWN = "PROVIDER_FAMILY_UNKNOWN"
PROVIDER_FAMILY_CHANGED = "PROVIDER_FAMILY_CHANGED"
MANUAL_RECOVERY_REQUIRED = "MANUAL_RECOVERY_REQUIRED"
RESUME_UNAVAILABLE = "RESUME_UNAVAILABLE"
ROOT_MUTATION = "ROOT_MUTATION"
ACCEPT_FAILED = "ACCEPT_FAILED"
ACCEPT_BLOCKED = "ACCEPT_BLOCKED"
CHECKS_FAILED = "CHECKS_FAILED"
UNIT_START_FAILED = "UNIT_START_FAILED"
DIRTY_OVERLAP = "DIRTY_OVERLAP"
CLEANUP_FAILED = "CLEANUP_FAILED"
PROVIDER_UNAVAILABLE = limits.PROVIDER_UNAVAILABLE
PROVIDER_THROTTLED = limits.PROVIDER_THROTTLED
PROVIDER_AUTH_EXPIRED = limits.PROVIDER_AUTH_EXPIRED


class TaskSpindleError(Exception):
    """Every failure a tool reports; carries the code that goes into the envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_error_body(self) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            details=self.details,
        )


# -- the state machine --------------------------------------------------------------

LEGAL_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PREPARING: frozenset(
        {TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLING}
    ),
    TaskState.QUEUED: frozenset(
        {
            TaskState.RUNNING,
            TaskState.FAILED,
            TaskState.CANCELLING,
            TaskState.INTERRUPTED,
            TaskState.RECOVERY_AMBIGUOUS,
        }
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.RESULT_READY,
            TaskState.FAILED,
            TaskState.CANCELLING,
            TaskState.INTERRUPTED,
            TaskState.RECOVERY_AMBIGUOUS,
        }
    ),
    TaskState.COMPLETED: frozenset({TaskState.RESUMING}),
    TaskState.RESULT_READY: frozenset(
        {TaskState.ACCEPTING, TaskState.REJECTED, TaskState.REPAIRING}
    ),
    TaskState.ACCEPTING: frozenset(
        {
            TaskState.ACCEPTED,
            TaskState.RESULT_READY,
            TaskState.INTERRUPTED,
            TaskState.RECOVERY_AMBIGUOUS,
        }
    ),
    TaskState.REPAIRING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.FAILED,
            TaskState.CANCELLING,
            TaskState.INTERRUPTED,
            TaskState.RECOVERY_AMBIGUOUS,
        }
    ),
    TaskState.INTERRUPTED: frozenset({TaskState.RESUMING, TaskState.CANCELLING}),
    TaskState.RESUMING: frozenset(
        {
            TaskState.RUNNING,
            TaskState.FAILED,
            TaskState.CANCELLING,
            TaskState.INTERRUPTED,
            TaskState.RECOVERY_AMBIGUOUS,
        }
    ),
    TaskState.RECOVERY_AMBIGUOUS: frozenset({TaskState.RESUMING, TaskState.CANCELLING}),
    TaskState.CANCELLING: frozenset({TaskState.CANCELLED, TaskState.FAILED}),
}

#: States only an ``implement`` task may enter; consult and review finish at COMPLETED.
IMPLEMENT_ONLY_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.RESULT_READY,
        TaskState.ACCEPTING,
        TaskState.ACCEPTED,
        TaskState.REJECTED,
        TaskState.REPAIRING,
    }
)

#: mode -> the states that mode may never enter.
MODE_FORBIDS_STATES: dict[Mode, frozenset[TaskState]] = {
    Mode.CONSULT: IMPLEMENT_ONLY_STATES,
    Mode.REVIEW: IMPLEMENT_ONLY_STATES,
    Mode.IMPLEMENT: frozenset(),
}

#: Transitions restricted to particular modes, beyond what :data:`MODE_FORBIDS_STATES` says.
#: Reopening a finished task is only ever an advisory follow-up: a consult can be asked one more
#: question in its own session, while an implement task that is done is done and any further work
#: goes through REPAIRING on its candidate.
MODE_ONLY_TRANSITIONS: dict[tuple[TaskState, TaskState], frozenset[Mode]] = {
    (TaskState.COMPLETED, TaskState.RESUMING): frozenset({Mode.CONSULT}),
}


def new_task_id() -> str:
    """Return a fresh task id: ``ts_`` plus 12 lowercase hex characters."""
    return f"ts_{secrets.token_hex(6)}"


def transition(
    store: Store,
    task_id: str,
    to_state: TaskState,
    *,
    reason: str,
    expected_state_version: int | None = None,
    **fields: Any,
) -> TaskRecord:
    """Move a task to ``to_state``, applying ``fields`` and the audit event atomically."""
    with store.transaction():
        record = require_task(store, task_id)
        if expected_state_version is not None and record.state_version != expected_state_version:
            raise TaskSpindleError(
                STALE_STATE_VERSION,
                f"task {task_id} is at state_version {record.state_version}, "
                f"not {expected_state_version}",
                details={
                    "task_id": task_id,
                    "expected": expected_state_version,
                    "actual": record.state_version,
                },
            )
        allowed = LEGAL_TRANSITIONS.get(record.state, frozenset())
        if to_state not in allowed:
            raise TaskSpindleError(
                ILLEGAL_TRANSITION,
                f"cannot move task {task_id} from {record.state.value} to {to_state.value}",
                details={
                    "task_id": task_id,
                    "from": record.state.value,
                    "to": to_state.value,
                    "allowed": sorted(state.value for state in allowed),
                },
            )
        modes = MODE_ONLY_TRANSITIONS.get((record.state, to_state))
        if to_state in MODE_FORBIDS_STATES[record.mode] or (
            modes is not None and record.mode not in modes
        ):
            raise TaskSpindleError(
                MODE_FORBIDS_STATE,
                f"a {record.mode.value} task may not move from {record.state.value} "
                f"to {to_state.value}",
                details={
                    "task_id": task_id,
                    "mode": record.mode.value,
                    "from": record.state.value,
                    "to": to_state.value,
                },
            )
        try:
            updated = store.update_task(
                task_id,
                expected_state_version=record.state_version,
                state=to_state,
                **fields,
            )
        except StaleStateVersionError as exc:  # pragma: no cover - guarded by the read above
            raise TaskSpindleError(
                STALE_STATE_VERSION, str(exc), details={"task_id": task_id}
            ) from exc
        store.append_event(
            task_id,
            EventKind.STATE_CHANGED,
            {"from": record.state.value, "to": to_state.value, "reason": reason},
        )
        if to_state in {TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED}:
            from .provider_recovery import finish

            error = fields.get("error") or {}
            finish(
                store, task_id, "failed", now=datetime.now(UTC),
                code=error.get("code") or to_state.value,
            )
    return updated


def require_task(store: Store, task_id: str) -> TaskRecord:
    record = store.get_task(task_id)
    if record is None:
        raise TaskSpindleError(
            TASK_NOT_FOUND, f"no such task: {task_id}", details={"task_id": task_id}
        )
    return record


# -- task creation ------------------------------------------------------------------


_AVAILABILITY_ACTIONS: dict[str, tuple[str, bool]] = {
    "ok": ("start", True),
    "unknown": ("retry", True),
    "throttled": ("wait", False),
    "auth_expired": ("sign_in", False),
    "access_denied": ("review_access", False),
    "model_unavailable": ("choose_model", False),
}

_AVAILABILITY_FRESH_FOR = timedelta(hours=24)


def _observation_stale(row: dict[str, Any] | None, current: datetime) -> bool:
    if row is None:
        return False
    raw = row.get("observed_at")
    if not isinstance(raw, str) or not raw:
        return True
    try:
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if observed.tzinfo is None or observed.utcoffset() is None:
        return True
    age = current.astimezone(UTC) - observed.astimezone(UTC)
    return age < timedelta(0) or age >= _AVAILABILITY_FRESH_FOR


def _last_success_at(row: dict[str, Any] | None) -> str | None:
    """Read v5 success metadata, with the exact equivalent from a schema-4 success row."""
    if row is None:
        return None
    value = row.get("last_success_at")
    if isinstance(value, str) and value:
        return value
    if row.get("state") == "ok" and row.get("source") == "turn_ok":
        observed = row.get("observed_at")
        return observed if isinstance(observed, str) and observed else None
    return None


def provider_availability(
    store: ProviderStatusReader, profile: Profile, *, now: datetime, model: str | None = None,
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """What TaskSpindle currently believes about a provider's willingness to take a turn."""
    from .provider_recovery import status as recovery_status

    model = model if model is not None else profile.model
    recovery = recovery_status(store, profile, now=now, model=model, parent_env=parent_env)
    key = limits.status_key(profile)
    account_row = store.get_provider_status(key)
    account_state = limits.effective_state(account_row, now)
    model_row = store.get_provider_model_status(key, model) if model else None
    selected = account_row
    state = account_state
    scope = "account" if account_row is not None else None
    affected_model = None
    if account_state in ("ok", "unknown") and model_row is not None:
        model_state = limits.effective_state(model_row, now)
        if model_state == "model_unavailable":
            selected = model_row
            state = model_state
            scope = "model"
            affected_model = model
    stale = _observation_stale(selected, now) or bool(
        selected is not None
        and selected.get("state") == "throttled"
        and state == "unknown"
    )
    # Native quota may add a temporary throttle, never remove task/model refusal evidence.
    from .access_checks import cached_native_check

    native = cached_native_check(store, profile, parent_env, now=now)
    if state in ("ok", "unknown") and native["eligible_hint"] is False:
        return {
            "evidence_revision": recovery["evidence_revision"], "recovery": recovery,
            "state": "throttled", "status_key": key, "code": "NATIVE_QUOTA_EXHAUSTED",
            "window": native["window"], "reset_at": native["reset_at"],
            "reason": "Grok reported its current quota fully used.",
            "observed_at": native["checked_at"],
            "suggested_alternative": limits.suggested_alternative(profile.id),
            "last_success_at": _last_success_at(account_row), "source": "native_check",
            "scope": "account", "affected_model": None, "stale": False,
            "next_action": "wait", "retry_eligible": False,
        }
    next_action, retry_eligible = _AVAILABILITY_ACTIONS.get(state, ("retry", True))
    stored_state = selected.get("state") if selected else None
    return {
        "evidence_revision": recovery["evidence_revision"], "recovery": recovery,
        "state": state,
        "status_key": key,
        "code": selected.get("code") if selected and state != "ok" else None,
        "window": selected.get("window") if selected and state != "ok" else None,
        "reset_at": selected.get("reset_at") if selected and state != "ok" else None,
        "reason": limits.safe_provider_reason(stored_state) if state != "ok" else None,
        "observed_at": selected.get("observed_at") if selected else None,
        "suggested_alternative": (
            limits.suggested_alternative(profile.id)
            if scope == "account" and state not in ("ok", "unknown") else None
        ),
        "last_success_at": _last_success_at(selected),
        "source": limits.safe_provider_source(selected.get("source")) if selected else None,
        "scope": scope,
        "affected_model": affected_model,
        "stale": stale,
        "next_action": next_action,
        "retry_eligible": retry_eligible,
    }


def model_availability(
    store: ProviderStatusReader, profile: Profile, *, now: datetime,
    parent_env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Safe model-scoped observations a caller can use before choosing an override."""
    key = limits.status_key(profile)
    from .provider_recovery import status as recovery_status

    result: list[dict[str, Any]] = []
    rows = list(store.list_provider_model_status(key))
    permit_reader = getattr(store, "latest_recovery_permit", None)
    permit = permit_reader(key) if permit_reader else None
    if (permit and permit["provider"] == profile.id and permit["model"]
            and not any(row["model"] == permit["model"] for row in rows)):
        # Keep explicitly named retries discoverable before that model has reported any
        # evidence. This remains an unknown observation, never entitlement proof.
        rows.append({"model": permit["model"], "state": "unknown"})
    for row in sorted(rows, key=lambda row: row["model"]):
        state = limits.effective_state(row, now)
        next_action, retry_eligible = _AVAILABILITY_ACTIONS.get(state, ("retry", True))
        model = str(row["model"])
        recovery = recovery_status(store, profile, now=now, model=model, parent_env=parent_env)
        result.append({
            "evidence_revision": recovery["evidence_revision"], "recovery": recovery,
            "state": state,
            "status_key": key,
            "code": row.get("code") if state != "ok" else None,
            "window": None,
            "reset_at": None,
            "reason": limits.safe_provider_reason(row.get("state")) if state != "ok" else None,
            "observed_at": row.get("observed_at"),
            "suggested_alternative": None,
            "last_success_at": _last_success_at(row),
            "source": limits.safe_provider_source(row.get("source")),
            "scope": "model",
            "affected_model": model,
            "stale": _observation_stale(row, now) if "observed_at" in row else False,
            "next_action": next_action,
            "retry_eligible": retry_eligible,
        })
    return result


def require_provider_available(
    store: Store, profile: Profile, *, now: datetime, ignore: bool = False,
    model: str | None = None, parent_env: Mapping[str, str] | None = None,
) -> None:
    """Refuse to start on a provider the last turn found throttled or logged out.

    This is the whole of the fallback policy: the refusal names the reset time and the other
    first-class provider, and the caller decides. ``ignore`` starts the task anyway.
    """
    availability = provider_availability(store, profile, now=now, model=model, parent_env=parent_env)
    if ignore or availability["state"] in ("ok", "unknown"):
        return
    raise TaskSpindleError(
        PROVIDER_UNAVAILABLE,
        f"provider {profile.id!r} is {availability['state']} "
        f"({availability['reason'] or availability['code']})",
        retryable=True,
        details={
            "provider": profile.id,
            **availability,
            "override": "set ignore_provider_status=true to start anyway",
        },
    )


def require_grant(store: Store, repository_id: str, provider: str, mode: Mode | str) -> None:
    """Raise ``GRANT_MISSING`` unless an active grant covers this repository/provider/mode."""
    mode_value = mode.value if isinstance(mode, Mode) else mode
    if not store.grant_active(repository_id, provider, mode_value):
        raise TaskSpindleError(
            GRANT_MISSING,
            f"no active grant for {provider}/{mode_value} on repository {repository_id}",
            details={"repository_id": repository_id, "provider": provider, "mode": mode_value},
        )


def create_task(
    store: Store,
    request: StartTaskRequest,
    *,
    repository_id: str | None,
    auth_mode: AuthMode,
    provider_family: str | None = None,
) -> TaskRecord:
    """Insert a new task in PREPARING with its TASK_CREATED event."""
    stamp = now()
    record = TaskRecord(
        id=new_task_id(),
        state=TaskState.PREPARING,
        state_version=1,
        cleanup_state=CleanupState.RETAINED,
        repository_id=repository_id,
        provider=request.provider,
        provider_family=provider_family,
        auth_mode=auth_mode,
        mode=request.mode,
        prompt=request.prompt,
        requested_model=request.model,
        requested_effort=request.effort,
        timeout_s=request.timeout_s,
        allow_metered=request.allow_metered,
        acceptance_criteria=request.acceptance_criteria,
        path_prefixes=request.path_prefixes,
        verification_commands=request.verification_commands,
        candidate_message=request.candidate_message,
        review_target=(
            request.review_target.model_dump(mode="json") if request.review_target else None
        ),
        created_at=stamp,
        updated_at=stamp,
    )
    with store.transaction():
        store.insert_task(record)
        store.append_event(
            record.id,
            EventKind.TASK_CREATED,
            {
                "provider": record.provider,
                "provider_family": record.provider_family,
                "mode": record.mode.value,
                "auth_mode": record.auth_mode.value,
                "repository_id": record.repository_id,
            },
        )
    return record


def task_provider_family(record: TaskRecord) -> str:
    """Read immutable provenance; only old reserved built-in IDs have a safe fallback."""
    if record.provider_family:
        return record.provider_family
    if record.provider in {"claude", "grok"}:
        return record.provider
    raise TaskSpindleError(
        PROVIDER_FAMILY_UNKNOWN,
        f"task {record.id} has no recorded provider family; start a new task and independent review "
        "with the intended providers. Current alias settings cannot establish historical provenance",
        details={"task_id": record.id, "provider": record.provider},
    )


def require_task_profile(record: TaskRecord, profile: Profile | None) -> str:
    """The task's original family must still be the family its profile would run."""
    family = task_provider_family(record)
    if profile is None or profile.id != record.provider or profile.family != family:
        raise TaskSpindleError(
            PROVIDER_FAMILY_CHANGED,
            f"task {record.id} is bound to provider family {family!r}; restore that profile "
            "or start a new task and independent review with the intended providers",
            details={"task_id": record.id, "provider": record.provider, "provider_family": family,
                     "current_family": profile.family if profile else None},
        )
    return family


# -- diff receipts ------------------------------------------------------------------


def record_diff_receipt(
    store: Store, task_id: str, digest: str, offset: int, length: int
) -> int:
    """Record that a diff byte range was handed to the caller; returns the receipt id."""
    with store.transaction():
        receipt_id = store.add_receipt(task_id, digest, offset, length)
        store.append_event(
            task_id,
            EventKind.DIFF_RETRIEVED,
            {"digest": digest, "offset": offset, "length": length, "receipt_id": receipt_id},
        )
    return receipt_id


def diff_fully_retrieved(
    store: Store, task_id: str, digest: str, size: int
) -> list[tuple[int, int]]:
    """Return the still-missing ``[start, end)`` ranges of the diff; empty means fully covered."""
    missing: list[tuple[int, int]] = []
    cursor = 0
    for start, end in store.receipt_coverage(task_id, digest):
        if start > cursor:
            missing.append((cursor, min(start, size)))
        cursor = max(cursor, end)
        if cursor >= size:
            break
    if cursor < size:
        missing.append((cursor, size))
    return [(start, end) for start, end in missing if start < end]


# -- acceptance ---------------------------------------------------------------------


def require_diff_retrieved(store: Store, record: TaskRecord) -> None:
    """Refuse unless every byte of the candidate's diff has been handed over and receipted."""
    missing = diff_fully_retrieved(
        store, record.id, record.diff_digest or "", record.diff_size or 0
    )
    if missing:
        raise TaskSpindleError(
            DIFF_NOT_FULLY_RETRIEVED,
            "the whole diff must be retrieved before the candidate can be accepted",
            details={
                "task_id": record.id,
                "digest": record.diff_digest,
                "missing": [[start, end] for start, end in missing],
            },
        )


def require_independent_review(store: Store, record: TaskRecord) -> dict[str, Any]:
    """Refuse unless some other provider has reviewed *this* candidate, and return that review.

    This is the review gate without the request: an integration a person made by hand names no
    review task, but the candidate still may not land unreviewed.
    """
    review = store.latest_review_for_subject(record.id, record.candidate_sha or "")
    if review is None:
        raise TaskSpindleError(
            REVIEW_REQUIRED,
            f"no review covers candidate {record.candidate_sha} of task {record.id}",
            details={"task_id": record.id, "candidate_sha": record.candidate_sha},
        )
    if review["provider"] == record.provider:
        raise TaskSpindleError(
            REVIEWER_NOT_INDEPENDENT,
            "the reviewer must be a different provider from the author",
            details={"provider": record.provider, "review_task_id": review["review_task_id"]},
        )
    return review


def validate_acceptance(store: Store, request: AcceptTaskRequest) -> TaskRecord:
    """Check every accept precondition and move the task to ACCEPTING.

    Only the store is consulted: the accept unit does the git work later and re-checks
    ``expected_target_head``, which this function records on the task.

    The two halves are separable so that the orchestrator can run its own gates -- which need the
    checked record but must refuse *before* anything moves -- between them; the task still makes
    exactly one transition.
    """
    with store.transaction():
        check_acceptance(store, request)
        return apply_acceptance(store, request)


def check_acceptance(store: Store, request: AcceptTaskRequest) -> TaskRecord:
    """Every accept precondition the store can decide, and no writes at all."""
    with store.transaction():
        record = require_task(store, request.task_id)
        if record.state is not TaskState.RESULT_READY:
            raise TaskSpindleError(
                ILLEGAL_TRANSITION,
                f"task {record.id} is {record.state.value}, not RESULT_READY",
                details={"task_id": record.id, "from": record.state.value, "to": "ACCEPTING"},
            )
        if record.state_version != request.expected_state_version:
            raise TaskSpindleError(
                STALE_STATE_VERSION,
                f"task {record.id} is at state_version {record.state_version}, "
                f"not {request.expected_state_version}",
                details={
                    "task_id": record.id,
                    "expected": request.expected_state_version,
                    "actual": record.state_version,
                },
            )
        if record.candidate_sha != request.candidate_sha:
            raise TaskSpindleError(
                CANDIDATE_MISMATCH,
                "the candidate has moved since it was inspected",
                details={
                    "task_id": record.id,
                    "expected": request.candidate_sha,
                    "actual": record.candidate_sha,
                },
            )
        if record.diff_digest != request.diff_digest:
            raise TaskSpindleError(
                CANDIDATE_MISMATCH,
                "the diff digest does not match the candidate",
                details={
                    "task_id": record.id,
                    "expected": request.diff_digest,
                    "actual": record.diff_digest,
                },
            )
        require_diff_retrieved(store, record)
        review = store.get_review_for(request.review_task_id)
        if review is None:
            raise TaskSpindleError(
                REVIEW_REQUIRED,
                f"no review recorded for task {request.review_task_id}",
                details={"review_task_id": request.review_task_id},
            )
        if (
            review["subject_task_id"] != record.id
            or review["candidate_sha"] != request.candidate_sha
        ):
            raise TaskSpindleError(
                REVIEW_STALE,
                "the review does not cover this candidate",
                details={
                    "review_task_id": request.review_task_id,
                    "subject_task_id": review["subject_task_id"],
                    "candidate_sha": review["candidate_sha"],
                },
            )
        if review["provider"] == record.provider:
            raise TaskSpindleError(
                REVIEWER_NOT_INDEPENDENT,
                "the reviewer must be a different provider from the author",
                details={"provider": record.provider, "review_task_id": request.review_task_id},
            )
        findings = [ReviewFinding(**finding) for finding in review["findings"]]
        verdict = Verdict(review["verdict"])
        _check_dispositions(record.id, verdict, findings, request)
    return record


def apply_acceptance(
    store: Store,
    request: AcceptTaskRequest,
    *,
    unit_name: str | None = None,
) -> TaskRecord:
    """Move a checked task to ACCEPTING and record what the acceptance claimed."""
    fields: dict[str, Any] = {"target_head": request.expected_target_head}
    if unit_name is not None:
        fields["unit_name"] = unit_name
    with store.transaction():
        record = require_task(store, request.task_id)
        updated = transition(
            store,
            record.id,
            TaskState.ACCEPTING,
            reason="accept requested",
            expected_state_version=record.state_version,
            **fields,
        )
        store.append_event(
            record.id,
            EventKind.ACCEPT_REQUESTED,
            {
                "candidate_sha": request.candidate_sha,
                "diff_digest": request.diff_digest,
                "expected_target_head": request.expected_target_head,
                "review_task_id": request.review_task_id,
                "inspection_summary": request.inspection_summary,
                "commit_message": request.commit_message,
                "dispositions": [
                    disposition.model_dump(mode="json") for disposition in request.dispositions
                ],
            },
        )
        for disposition in request.dispositions:
            if disposition.disposition is Disposition.OVERRIDDEN:
                store.append_event(
                    record.id,
                    EventKind.REVIEW_OVERRIDE,
                    {
                        "review_task_id": request.review_task_id,
                        "finding_id": disposition.finding_id,
                        "reason": disposition.reason,
                    },
                )
    return updated


def _check_dispositions(
    task_id: str,
    verdict: Verdict,
    findings: list[ReviewFinding],
    request: AcceptTaskRequest,
) -> None:
    """Enforce the disposition rules for a verdict and its findings."""
    by_id = {finding.id: finding for finding in findings}
    dispositions = {item.finding_id: item for item in request.dispositions}
    unknown = sorted(set(dispositions) - set(by_id))
    if unknown:
        raise TaskSpindleError(
            INVALID_REQUEST,
            f"dispositions name findings that do not exist: {', '.join(unknown)}",
            details={"task_id": task_id, "finding_ids": unknown},
        )
    must_override = {
        finding.id
        for finding in findings
        if verdict is Verdict.BLOCK or finding.severity is Severity.CRITICAL
    }
    if verdict is Verdict.BLOCK and not findings:
        raise TaskSpindleError(
            REVIEW_BLOCKED,
            "the review verdict is BLOCK and lists no finding that could be overridden",
            details={"task_id": task_id, "verdict": verdict.value},
        )
    unhandled = sorted(
        finding_id
        for finding_id in must_override
        if finding_id not in dispositions
        or dispositions[finding_id].disposition is not Disposition.OVERRIDDEN
    )
    if unhandled:
        raise TaskSpindleError(
            REVIEW_BLOCKED,
            "every blocking or critical finding needs an explicit override with a reason",
            details={"task_id": task_id, "verdict": verdict.value, "finding_ids": unhandled},
        )
    if verdict is Verdict.CONCERN:
        undisposed = sorted(
            finding.id for finding in findings if finding.id not in dispositions
        )
        if undisposed:
            raise TaskSpindleError(
                REVIEW_BLOCKED,
                "every finding of a CONCERN review needs a disposition",
                details={"task_id": task_id, "verdict": verdict.value, "finding_ids": undisposed},
            )


# -- projections --------------------------------------------------------------------


def task_view(record: TaskRecord) -> TaskView:
    """Project a stored record into the public ``task_status`` view."""
    return TaskView(
        id=record.id,
        state=record.state,
        state_version=record.state_version,
        cleanup_state=record.cleanup_state,
        provider=record.provider,
        provider_family=record.provider_family,
        auth_mode=record.auth_mode,
        mode=record.mode,
        repository_id=record.repository_id,
        base_head=record.base_head,
        target_head=record.target_head,
        branch=record.branch,
        worktree_path=record.worktree_path,
        session_id=record.session_id,
        requested_model=record.requested_model,
        resolved_model=record.resolved_model,
        resolved_effort=record.resolved_effort,
        reported_model=record.reported_model,
        oauth_evidence=record.oauth_evidence or {},
        candidate_sha=record.candidate_sha,
        candidate_revision=record.candidate_revision,
        changed_paths=record.changed_paths or [],
        diff_digest=record.diff_digest,
        diff_size=record.diff_size,
        check_summary=record.check_summary or {},
        warnings=record.warnings or [],
        error=ErrorBody(**record.error) if record.error else None,
        unit_name=record.unit_name,
        heartbeat_at=record.heartbeat_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def task_result(store: Store, task_id: str) -> TaskResult:
    """Assemble the ``task_result`` payload for a task."""
    record = require_task(store, task_id)
    checks: list[CheckRecord] = store.list_checks(task_id, record.candidate_revision)
    turns = store.list_turns(task_id)
    latest = turns[-1]["attribution"] or {} if turns else {}
    usage_rows = [
        {key: value for key, value in row.items() if key not in ("id", "turn_id", "task_id")}
        for row in store.list_turn_usage(task_id=task_id)
    ]
    return TaskResult(
        task_id=record.id,
        state=record.state,
        response=record.response,
        checks=checks,
        attribution={
            "provider": record.provider,
            "provider_family": record.provider_family,
            "auth_mode": record.auth_mode.value,
            "requested_model": record.requested_model,
            "resolved_model": record.resolved_model,
            "resolved_effort": record.resolved_effort,
            "reported_model": record.reported_model or latest.get("reported_model"),
            "gateway_host": latest.get("gateway_host"),
            "agent": latest.get("agent"),
        },
        warnings=list(record.warnings or []),
        quota_warnings=[
            dict(event["payload"] or {})
            for event in store.list_events(task_id)
            if event["kind"] == EventKind.PROVIDER_LIMIT.value
        ],
        usage=usage_rows,
        transcript_locator=record.transcript_path,
    )


# -- collaborator protocols ---------------------------------------------------------


class RepositoryResolver(Protocol):
    """Resolves a user-supplied path to a repository identity.

    ``resolve(path) -> (repository_id, common_dir, root_commit)`` is implemented in a later
    task on top of ``git rev-parse``.
    """

    def resolve(self, path: str) -> tuple[str, str, str]: ...


class WorktreeManager(Protocol):
    """Creates and removes the detached worktree a task runs in.

    ``create(task) -> worktree_path`` is implemented in a later task.
    """

    def create(self, task: TaskRecord) -> str: ...


class UnitManager(Protocol):
    """Starts and inspects the transient units that run workers and accepts.

    ``start_worker(task) -> unit_name`` and ``unit_state(unit_name)`` are implemented in a
    later task on top of ``systemd-run``.
    """

    def start_worker(self, task: TaskRecord) -> str: ...

    def unit_state(self, unit_name: str) -> str: ...
