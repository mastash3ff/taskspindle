"""The TaskSpindle state machine: the transitions, the validators and the projections.

Everything here is a rule about a task, decided from the store alone. The sequencing that turns
those rules into tool behaviour -- git, systemd, the filesystem -- lives in
:mod:`taskspindle.orchestrator`, which imports this module and not the other way round.
"""

from __future__ import annotations

import secrets
import shlex
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
from .providers import RESUMABLE_FAMILIES, Profile, resume_command
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
POLICY_BUDGET_EXHAUSTED = "POLICY_BUDGET_EXHAUSTED"
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
    TaskState.PREPARING: frozenset({TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLING}),
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
    TaskState.RESULT_READY: frozenset({TaskState.ACCEPTING, TaskState.REJECTED, TaskState.REPAIRING}),
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
                f"task {task_id} is at state_version {record.state_version}, not {expected_state_version}",
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
        if to_state in MODE_FORBIDS_STATES[record.mode] or (modes is not None and record.mode not in modes):
            raise TaskSpindleError(
                MODE_FORBIDS_STATE,
                f"a {record.mode.value} task may not move from {record.state.value} to {to_state.value}",
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
            raise TaskSpindleError(STALE_STATE_VERSION, str(exc), details={"task_id": task_id}) from exc
        store.append_event(
            task_id,
            EventKind.STATE_CHANGED,
            {"from": record.state.value, "to": to_state.value, "reason": reason},
        )
    return updated


def require_task(store: Store, task_id: str) -> TaskRecord:
    record = store.get_task(task_id)
    if record is None:
        raise TaskSpindleError(TASK_NOT_FOUND, f"no such task: {task_id}", details={"task_id": task_id})
    return record


# -- task creation ------------------------------------------------------------------


#: How long a refusal without a provider-reported reset time blocks admission.
_ELIGIBLE_AFTER = timedelta(minutes=15)

#: The per-state code carried in a refusal's details, for a caller or test that still keys off it.
_STATE_CODES: dict[str, str] = {
    "throttled": limits.PROVIDER_THROTTLED,
    "auth_expired": limits.PROVIDER_AUTH_EXPIRED,
    "access_denied": limits.PROVIDER_ACCESS_DENIED,
    "model_unavailable": limits.PROVIDER_MODEL_UNAVAILABLE,
}


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def provider_availability(
    store: ProviderStatusReader,
    profile: Profile,
    *,
    now: datetime,
    model: str | None = None,
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The one rule: what the last turn on this provider reported, and when it is eligible again.

    A provider whose last recorded turn was refused stays refused until the provider's own
    ``reset_at``, or -- when it gave none -- fifteen minutes after the refusal was observed.
    After that it is simply eligible again: one ordinary attempt, and if that attempt is refused
    too the clock restarts from the new observation. ``model`` and ``parent_env`` are accepted for
    call-site symmetry with the rest of the availability surface; the rule itself is per provider.
    """
    del model, parent_env
    key = limits.status_key(profile)
    row = store.get_provider_status(key)
    state = str(row.get("state")) if row and row.get("state") else "ok"
    if state == "ok":
        return {"state": "ok", "reset_at": None, "eligible_at": None, "reason": None}
    reset_at = row.get("reset_at") if row else None
    observed_at = row.get("observed_at") if row else None
    eligible_at = reset_at
    if eligible_at is None:
        observed = _parse_iso(observed_at)
        eligible_at = (
            (observed + _ELIGIBLE_AFTER).isoformat(timespec="seconds").replace("+00:00", "Z")
            if observed is not None
            else None
        )
    return {
        "state": state,
        "reset_at": reset_at,
        "eligible_at": eligible_at,
        "reason": limits.safe_provider_reason(state, row.get("affected_model") if row else None),
    }


def provider_eligible(availability: Mapping[str, Any], now: datetime) -> bool:
    """Whether ``availability`` (as returned by :func:`provider_availability`) admits a turn now."""
    if availability["state"] == "ok":
        return True
    eligible_at = _parse_iso(availability["eligible_at"])
    return eligible_at is not None and eligible_at <= now


def require_provider_available(
    store: Store,
    profile: Profile,
    *,
    now: datetime,
    ignore: bool = False,
    model: str | None = None,
    parent_env: Mapping[str, str] | None = None,
) -> None:
    """Refuse to start on a provider the last turn found throttled or logged out.

    This is the whole of the fallback policy: the refusal names the reset (or eligible) time and
    the other first-class provider, and the caller decides. ``ignore`` is an explicit coordinator
    override that admits the task anyway; there is no other bypass.
    """
    if ignore:
        return
    availability = provider_availability(store, profile, now=now, model=model, parent_env=parent_env)
    if provider_eligible(availability, now):
        return
    raise TaskSpindleError(
        PROVIDER_UNAVAILABLE,
        f"provider {profile.id!r} is {availability['state']} ({availability['reason']})",
        retryable=True,
        details={
            "provider": profile.id,
            "code": _STATE_CODES.get(availability["state"], PROVIDER_UNAVAILABLE),
            **availability,
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
        role=request.role,
        timeout_s=request.timeout_s,
        allow_metered=request.allow_metered,
        ignore_provider_status=request.ignore_provider_status,
        acceptance_criteria=request.acceptance_criteria,
        path_prefixes=request.path_prefixes,
        verification_commands=request.verification_commands,
        candidate_message=request.candidate_message,
        review_target=(request.review_target.model_dump(mode="json") if request.review_target else None),
        review_kind=request.review_kind if request.mode is Mode.REVIEW else None,
        context_files=request.context_files or None,
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
                "role": record.role,
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
            details={
                "task_id": record.id,
                "provider": record.provider,
                "provider_family": family,
                "current_family": profile.family if profile else None,
            },
        )
    return family


# -- diff receipts ------------------------------------------------------------------


def record_diff_receipt(store: Store, task_id: str, digest: str, offset: int, length: int) -> int:
    """Record that a diff byte range was handed to the caller; returns the receipt id."""
    with store.transaction():
        receipt_id = store.add_receipt(task_id, digest, offset, length)
        store.append_event(
            task_id,
            EventKind.DIFF_RETRIEVED,
            {"digest": digest, "offset": offset, "length": length, "receipt_id": receipt_id},
        )
    return receipt_id


def diff_fully_retrieved(store: Store, task_id: str, digest: str, size: int) -> list[tuple[int, int]]:
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
    missing = diff_fully_retrieved(store, record.id, record.diff_digest or "", record.diff_size or 0)
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


def require_independent_review(
    store: Store, record: TaskRecord, *, required: bool = True
) -> dict[str, Any] | None:
    """Return the latest review of *this* candidate, refusing unless one qualifies.

    This is the review gate without the request: an integration a person made by hand names no
    review task, but a review it does find still may not be a self-review. ``required`` is what
    makes the *absence* of a review a refusal; a review that is found is always checked for
    independence, whether or not one was required, so a self-review is never accepted silently.
    """
    review = store.latest_review_for_subject(record.id, record.candidate_sha or "")
    if review is None:
        if required:
            raise TaskSpindleError(
                REVIEW_REQUIRED,
                f"no review covers candidate {record.candidate_sha} of task {record.id}",
                details={"task_id": record.id, "candidate_sha": record.candidate_sha},
            )
        return None
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
    """Every accept precondition the store can decide, and no writes at all.

    ``require_diff_receipts`` and ``require_review`` are opt-in: a caller that does not ask for
    them gets a lighter check, but a review that *is* named is always validated as bound to this
    candidate and independent of its author -- naming a stale or self review is never quietly
    accepted just because it was not required.
    """
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
        if request.require_diff_receipts:
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
        review = _bound_review(store, record, request)
        if review is not None:
            findings = [ReviewFinding(**finding) for finding in review["findings"]]
            verdict = Verdict(review["verdict"])
            if request.require_review:
                _check_dispositions(record.id, verdict, findings, request)
            else:
                _check_unknown_dispositions(record.id, findings, request)
    return record


def _bound_review(
    store: Store, record: TaskRecord, request: AcceptTaskRequest
) -> dict[str, Any] | None:
    """The review named by the request, refusing unless it actually covers this candidate.

    Absence is only an error when ``require_review`` asks for one; a review that *is* named is
    checked regardless, so a stale or self review is never accepted just because it was optional.
    """
    if not request.review_task_id:
        if request.require_review:
            raise TaskSpindleError(
                REVIEW_REQUIRED,
                f"no review covers candidate {record.candidate_sha} of task {record.id}",
                details={"task_id": record.id, "candidate_sha": record.candidate_sha},
            )
        return None
    review = store.get_review_for(request.review_task_id)
    if review is None:
        raise TaskSpindleError(
            REVIEW_REQUIRED,
            f"no review recorded for task {request.review_task_id}",
            details={"review_task_id": request.review_task_id},
        )
    if review["subject_task_id"] != record.id or review["candidate_sha"] != request.candidate_sha:
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
    # ``review["kind"]`` (standard or adversarial) is recorded but not gated: a future
    # ``require_review_kind`` on the acceptance request would compare it here.
    return review


def apply_acceptance(
    store: Store,
    request: AcceptTaskRequest,
    *,
    unit_name: str | None = None,
) -> TaskRecord:
    """Move a checked task to ACCEPTING and record what the acceptance claimed.

    When a review was named but not required, its unresolved blocking findings did not refuse
    the acceptance in :func:`check_acceptance`; they are recorded here as a warning instead, so
    they stay visible on the task.
    """
    fields: dict[str, Any] = {"target_head": request.expected_target_head}
    if unit_name is not None:
        fields["unit_name"] = unit_name
    with store.transaction():
        record = require_task(store, request.task_id)
        if not request.require_review and request.review_task_id:
            review = store.get_review_for(request.review_task_id)
            if (
                review is not None
                and review["subject_task_id"] == record.id
                and review["candidate_sha"] == request.candidate_sha
            ):
                findings = [ReviewFinding(**finding) for finding in review["findings"]]
                verdict = Verdict(review["verdict"])
                unresolved = _undisposed_finding_ids(verdict, findings, request)
                if unresolved:
                    marker = f"{REVIEW_BLOCKED}:{','.join(unresolved)}"
                    warnings = list(record.warnings or [])
                    if marker not in warnings:
                        warnings.append(marker)
                    fields["warnings"] = warnings
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
                "rerun_verification": request.rerun_verification,
                "dispositions": [disposition.model_dump(mode="json") for disposition in request.dispositions],
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


def _check_unknown_dispositions(
    task_id: str, findings: list[ReviewFinding], request: AcceptTaskRequest
) -> None:
    """Refuse dispositions naming a finding the review does not have.

    This is checked whether or not a review is required: a disposition is either input about a
    real finding or a mistake, and that much is never optional.
    """
    by_id = {finding.id for finding in findings}
    unknown = sorted({item.finding_id for item in request.dispositions} - by_id)
    if unknown:
        raise TaskSpindleError(
            INVALID_REQUEST,
            f"dispositions name findings that do not exist: {', '.join(unknown)}",
            details={"task_id": task_id, "finding_ids": unknown},
        )


def _undisposed_finding_ids(
    verdict: Verdict, findings: list[ReviewFinding], request: AcceptTaskRequest
) -> list[str]:
    """Every finding a disposition rule would require that the request left unresolved.

    A ``BLOCK`` verdict with no findings at all is reported under the verdict's own name, since
    there is nothing to override. Order is verdict-empty marker, then blocking/critical findings,
    then the rest of a ``CONCERN`` review's findings, each only once.
    """
    dispositions = {item.finding_id: item for item in request.dispositions}
    must_override = {
        finding.id
        for finding in findings
        if verdict is Verdict.BLOCK or finding.severity is Severity.CRITICAL
    }
    unresolved: list[str] = []
    if verdict is Verdict.BLOCK and not findings:
        unresolved.append(verdict.value)
    unresolved.extend(
        finding_id
        for finding_id in sorted(must_override)
        if finding_id not in dispositions
        or dispositions[finding_id].disposition is not Disposition.OVERRIDDEN
    )
    if verdict is Verdict.CONCERN:
        unresolved.extend(
            finding.id
            for finding in findings
            if finding.id not in dispositions and finding.id not in unresolved
        )
    return unresolved


def _check_dispositions(
    task_id: str,
    verdict: Verdict,
    findings: list[ReviewFinding],
    request: AcceptTaskRequest,
) -> None:
    """Enforce the disposition rules for a verdict and its findings."""
    _check_unknown_dispositions(task_id, findings, request)
    unresolved = _undisposed_finding_ids(verdict, findings, request)
    if verdict is Verdict.BLOCK and not findings:
        raise TaskSpindleError(
            REVIEW_BLOCKED,
            "the review verdict is BLOCK and lists no finding that could be overridden",
            details={"task_id": task_id, "verdict": verdict.value},
        )
    blocking = [finding_id for finding_id in unresolved if finding_id != verdict.value]
    if verdict is not Verdict.CONCERN and blocking:
        raise TaskSpindleError(
            REVIEW_BLOCKED,
            "every blocking or critical finding needs an explicit override with a reason",
            details={"task_id": task_id, "verdict": verdict.value, "finding_ids": sorted(blocking)},
        )
    if verdict is Verdict.CONCERN and blocking:
        raise TaskSpindleError(
            REVIEW_BLOCKED,
            "every finding of a CONCERN review needs a disposition",
            details={"task_id": task_id, "verdict": verdict.value, "finding_ids": sorted(blocking)},
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
        role=record.role,
        review_kind=record.review_kind,
        context_files=list(record.context_files or []),
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


def resume_handle(record: TaskRecord, profile: Profile | None = None) -> dict[str, Any] | None:
    """How a human reopens the worker's session outside TaskSpindle, or None without a session.

    The handle follows the record's immutable provider family, never the live profile, so an
    alias that was re-based since the task ran cannot point the operator at the wrong CLI. What
    happens in a reopened session is outside TaskSpindle's containment: nothing done there is
    recorded and the task's mode restrictions no longer apply.
    """
    if not record.session_id:
        return None
    family = record.provider_family
    if family is None and record.provider in RESUMABLE_FAMILIES:
        family = record.provider
    argv = resume_command(family, record.session_id)
    cwd = record.worktree_path or record.scratch_repo
    env: dict[str, str] = {}
    if argv and family == "claude" and profile is not None:
        config_dir = profile.env.get("CLAUDE_CONFIG_DIR")
        if config_dir:
            env["CLAUDE_CONFIG_DIR"] = config_dir
    note: str | None = None
    if argv is None:
        if family == "agy":
            note = (
                "agy conversations live in the task's private state directory that only the "
                "sandboxed worker mounts; they cannot be reopened from a shell"
            )
        else:
            note = "no native resume command is known for this provider family; session_id is the raw ACP id"
    elif record.cleanup_state is not CleanupState.RETAINED:
        note = "the task workspace was cleaned up; the session is keyed to that directory and may not reopen"
    elif not cwd:
        note = "the task has no recorded workspace; run the command from the directory the worker used"
    return {
        "family": family,
        "session_id": record.session_id,
        "cwd": cwd,
        "argv": list(argv) if argv else None,
        "command": shlex.join(argv) if argv else None,
        "env": env,
        "note": note,
    }


def task_result(store: Store, task_id: str, *, profile: Profile | None = None) -> TaskResult:
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
            "review_kind": record.review_kind,
        },
        warnings=list(record.warnings or []),
        quota_warnings=[
            dict(event["payload"] or {})
            for event in store.list_events(task_id)
            if event["kind"] == EventKind.PROVIDER_LIMIT.value
        ],
        usage=usage_rows,
        transcript_locator=record.transcript_path,
        session_id=record.session_id,
        resume=resume_handle(record, profile),
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
