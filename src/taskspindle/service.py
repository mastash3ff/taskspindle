"""The TaskSpindle state machine, and the orchestrator that gives every tool its semantics.

The bottom half of this module is the :class:`Orchestrator`: one object holding the store, the
paths, the provider profiles and a unit backend, with one method per MCP tool. Everything it does
to git, systemd and the filesystem goes through the modules that own those things, so the rules
live here and the mechanics do not.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import taskspindle

from . import integration, providers, repos, units, worktrees
from .config import Paths
from .integration import Journal
from .models import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    AcceptTaskRequest,
    AuthMode,
    CheckRecord,
    CleanupState,
    DiffPage,
    Disposition,
    ErrorBody,
    EventKind,
    Mode,
    RecordIntegrationRequest,
    ReviewFinding,
    Severity,
    StartTaskRequest,
    TaskRecord,
    TaskResult,
    TaskState,
    TaskView,
    TurnKind,
    Verdict,
)
from .providers import Profile, ProfileError
from .repos import GitError, RepositoryIdentity, RootSnapshot
from .store import StaleStateVersionError, Store, now
from .units import UnitBackend, UnitError

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
MANUAL_RECOVERY_REQUIRED = "MANUAL_RECOVERY_REQUIRED"
RESUME_UNAVAILABLE = "RESUME_UNAVAILABLE"
ROOT_MUTATION = "ROOT_MUTATION"
ACCEPT_FAILED = "ACCEPT_FAILED"
ACCEPT_BLOCKED = "ACCEPT_BLOCKED"
CHECKS_FAILED = "CHECKS_FAILED"
UNIT_START_FAILED = "UNIT_START_FAILED"
DIRTY_OVERLAP = "DIRTY_OVERLAP"
CLEANUP_FAILED = "CLEANUP_FAILED"


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
        record = _require_task(store, task_id)
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
    return updated


def _require_task(store: Store, task_id: str) -> TaskRecord:
    record = store.get_task(task_id)
    if record is None:
        raise TaskSpindleError(
            TASK_NOT_FOUND, f"no such task: {task_id}", details={"task_id": task_id}
        )
    return record


# -- task creation ------------------------------------------------------------------


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
                "mode": record.mode.value,
                "auth_mode": record.auth_mode.value,
                "repository_id": record.repository_id,
            },
        )
    return record


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
        record = _require_task(store, request.task_id)
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
        record = _require_task(store, request.task_id)
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
        auth_mode=record.auth_mode,
        mode=record.mode,
        repository_id=record.repository_id,
        base_head=record.base_head,
        target_head=record.target_head,
        branch=record.branch,
        worktree_path=record.worktree_path,
        session_id=record.session_id,
        requested_model=record.requested_model,
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
    record = _require_task(store, task_id)
    checks: list[CheckRecord] = store.list_checks(task_id, record.candidate_revision)
    evidence = record.oauth_evidence or {}
    return TaskResult(
        task_id=record.id,
        state=record.state,
        response=record.response,
        checks=checks,
        attribution={
            "provider": record.provider,
            "auth_mode": record.auth_mode.value,
            "requested_model": record.requested_model,
            "reported_model": record.reported_model,
            "gateway_host": evidence.get("gateway_host"),
        },
        quota_warnings=list(record.warnings or []),
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


# -- the orchestrator ---------------------------------------------------------------

#: The ACP revision the pinned client and adapter speak.
ACP_VERSION = "0.12.0"

#: Largest slice of a candidate diff one ``task_diff`` call will return.
DIFF_PAGE_BYTES = worktrees.MAX_DIFF_PAGE

#: Inclusive bounds on a task's own timeout, mirroring ``StartTaskRequest``.
TIMEOUT_BOUNDS = (60, 14400)

#: How many turns one provider may have in flight; the lease enforces it.
CONCURRENT_TURNS_PER_PROVIDER = 1

#: What ``capabilities`` says about isolation, so a caller is never misled about it.
ISOLATION = (
    "Every task runs in its own detached git worktree under the TaskSpindle state directory, "
    "created from the repository's HEAD and never checked out on a branch the operator uses; the "
    "agent process is launched as a transient systemd user unit with a memory ceiling and an "
    "environment built by allowlist, so it inherits no credentials, no proxy settings and no "
    "agent configuration beyond what its profile declares. That is containment by construction, "
    "not an OS sandbox: the agent runs as your user with your filesystem permissions, so it can "
    "read anything you can read and can write outside its worktree if it tries. TaskSpindle "
    "detects such a write by comparing a root snapshot taken before dispatch and reports it as a "
    "ROOT_MUTATION warning that blocks acceptance until you acknowledge it."
)

#: The states ``dispatch_queued`` will start a worker unit for.
DISPATCHABLE_STATES: tuple[TaskState, ...] = (
    TaskState.QUEUED,
    TaskState.REPAIRING,
    TaskState.RESUMING,
)

#: What a caller is told to do about a task recovery could not settle on its own.
MANUAL_ACTION = (
    "A worker unit for this task could not be found but its heartbeat was recent, so TaskSpindle "
    "will not guess whether it is still running. Check the unit with 'systemctl --user status', "
    "then call continue_task to retry the turn or cancel_task to abandon it."
)

#: Most tasks one dispatch pass or listing will consider.
_SCAN_LIMIT = 1000

_RESUME_DEFAULT_PROMPT = "Continue where you left off."


def utcnow() -> datetime:
    """The clock the orchestrator reconciles against; injectable so tests can move time."""
    return datetime.now(UTC)


def new_repository_id() -> str:
    """Return a fresh repository id: ``repo_`` plus 12 lowercase hex characters."""
    return f"repo_{secrets.token_hex(6)}"


def _mkdir(path: Path) -> Path:
    """Create ``path`` (and its parents) private to this user."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _grant_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": row["provider"],
        "mode": row["mode"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "revoked_at": row["revoked_at"],
    }


class Orchestrator:
    """One method per MCP tool, with every collaborator injected.

    Two invariants hold across the whole surface. Every method except :meth:`capabilities`
    reconciles what the store believes against what systemd can still see *before* it acts, and
    dispatches whatever became runnable *after* it acts -- so a tool call is also the heartbeat
    that keeps a restarted server honest. And nothing here decides on a candidate's behalf: an
    acceptance that any gate cannot clear is refused, never downgraded to a warning.
    """

    def __init__(
        self,
        *,
        store: Store,
        paths: Paths,
        profiles: dict[str, Profile],
        units: UnitBackend,
        boot: str,
        parent_env: Mapping[str, str],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.paths = paths
        self.profiles = dict(profiles)
        self.units = units
        self.boot = boot
        self.parent_env = dict(parent_env)
        self.clock = clock
        _mkdir(paths.state_dir)

    # -- state-dir layout -----------------------------------------------------------

    def task_dir(self, task_id: str) -> Path:
        """``state_dir/tasks/<task_id>``: logs, diffs, transcripts and the root snapshot."""
        return _mkdir(self.paths.state_dir / "tasks" / task_id)

    def worktree_dir(self, task_id: str) -> Path:
        """Where a task's detached worktree goes. Not created: ``git worktree add`` makes it."""
        _mkdir(self.paths.state_dir / "worktrees")
        return self.paths.state_dir / "worktrees" / task_id

    def scratch_dir(self, task_id: str) -> Path:
        """Where a repository-less consult task gets its throwaway repository."""
        _mkdir(self.paths.state_dir / "scratch")
        return self.paths.state_dir / "scratch" / task_id

    # -- the cycle every tool runs inside --------------------------------------------

    @contextmanager
    def _cycle(self) -> Any:
        """Reconcile, do the work, then dispatch whatever the work made runnable."""
        self.reconcile()
        yield
        self.dispatch_queued()

    def reconcile(self) -> list[Any]:
        """Settle every task that claims to be active but may no longer be."""
        from . import recovery  # imported here: recovery imports this module.

        return recovery.reconcile(
            self.store,
            self.units,
            boot=self.boot,
            now=self.clock(),
            accept_recover=self._recover_accept,
        )

    def _recover_accept(self, task: TaskRecord) -> str:
        """What an interrupted accept actually did to the root repository."""
        journal = self.store.read_journal(task.id)
        if journal is None:
            return "aborted"
        try:
            identity = self._identity_for(task.repository_id)
            outcome = integration.recover_journal(
                identity,
                Journal(
                    task_id=task.id,
                    phase=str(journal["phase"]),
                    target_head=str(journal["target_head"] or ""),
                    candidate_sha=str(journal["candidate_sha"] or ""),
                ),
            )
        except (GitError, TaskSpindleError, ValueError):
            # The repository could not be inspected: report the outcome that keeps the candidate.
            return "aborted"
        self.store.clear_journal(task.id)
        return outcome

    # -- capabilities ---------------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        """Everything a caller needs to compose a valid request, and nothing about a task."""
        return {
            "providers": [
                {
                    "id": profile.id,
                    "first_class": profile.first_class,
                    "second_class": not profile.first_class,
                    "auth": profile.auth,
                    "modes": sorted(profile.modes),
                    "model": profile.model,
                    "gateway_host": profile.gateway_host,
                }
                for profile in sorted(self.profiles.values(), key=lambda item: item.id)
            ],
            "modes": [mode.value for mode in Mode],
            "versions": {
                "taskspindle": taskspindle.__version__,
                "api": taskspindle.API_VERSION,
                "schema": taskspindle.SCHEMA_VERSION,
                "adapter_package": taskspindle.ADAPTER_PACKAGE,
                "adapter_version": taskspindle.ADAPTER_VERSION,
                "acp": ACP_VERSION,
            },
            "limits": {
                "timeout_s": list(TIMEOUT_BOUNDS),
                "diff_page_bytes": DIFF_PAGE_BYTES,
                "concurrent_turns_per_provider": CONCURRENT_TURNS_PER_PROVIDER,
            },
            "states": [state.value for state in TaskState],
            "cleanup_states": [state.value for state in CleanupState],
            "isolation": ISOLATION,
        }

    # -- repository policy ----------------------------------------------------------

    def authorize_repository(
        self,
        path: str,
        providers_: Sequence[str],
        modes: Sequence[str],
    ) -> dict[str, Any]:
        """Grant a set of providers a set of modes on one repository."""
        with self._cycle():
            identity = self._resolve(path)
            wanted_providers = self._known_providers(providers_)
            wanted_modes = self._known_modes(modes)
            repository_id = self._repository_row(identity)
            for provider in wanted_providers:
                for mode in wanted_modes:
                    self.store.upsert_grant(repository_id, provider, mode.value)
            result = self._policy(repository_id, identity)
        return result

    def revoke_repository(
        self,
        path: str,
        providers_: Sequence[str] | None = None,
        modes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Withdraw matching active grants. Tasks already running are never touched."""
        with self._cycle():
            identity = self._resolve(path)
            row = self.store.find_repository(str(identity.common_dir), identity.root_commit)
            if row is None:
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    f"repository {path} has never been authorized",
                    details={"repository": str(identity.toplevel)},
                )
            repository_id = str(row["id"])
            targets = set(self._known_providers(providers_)) if providers_ else None
            wanted = {mode.value for mode in self._known_modes(modes)} if modes else None
            revoked = 0
            for grant in self.store.list_grants(repository_id):
                if not grant["active"]:
                    continue
                if targets is not None and grant["provider"] not in targets:
                    continue
                if wanted is not None and grant["mode"] not in wanted:
                    continue
                if self.store.revoke_grant(repository_id, grant["provider"], grant["mode"]):
                    revoked += 1
            result = self._policy(repository_id, identity)
            result["revoked"] = revoked
        return result

    def list_repository_policies(self) -> dict[str, Any]:
        """Every repository TaskSpindle knows about and the grants it holds."""
        with self._cycle():
            repositories = [
                {
                    "repository_id": row["id"],
                    "display_path": row["display_path"],
                    "common_dir": row["common_dir"],
                    "root_commit": row["root_commit"],
                    "grants": [_grant_view(grant) for grant in self.store.list_grants(row["id"])],
                }
                for row in self.store.list_repositories()
            ]
        return {"repositories": repositories}

    def _policy(self, repository_id: str, identity: RepositoryIdentity) -> dict[str, Any]:
        return {
            "repository_id": repository_id,
            "display_path": str(identity.toplevel),
            "grants": [_grant_view(grant) for grant in self.store.list_grants(repository_id)],
        }

    def _known_providers(self, wanted: Sequence[str]) -> list[str]:
        unknown = sorted({name for name in wanted if name not in self.profiles})
        if unknown:
            raise TaskSpindleError(
                INVALID_REQUEST,
                f"no such provider: {', '.join(unknown)}",
                details={
                    "code": "PROFILE_UNKNOWN",
                    "providers": unknown,
                    "known": sorted(self.profiles),
                },
            )
        return list(dict.fromkeys(wanted))

    @staticmethod
    def _known_modes(wanted: Sequence[str] | None) -> list[Mode]:
        resolved: list[Mode] = []
        for name in wanted or ():
            try:
                resolved.append(Mode(name))
            except ValueError as exc:
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    f"no such mode: {name}",
                    details={"mode": name, "known": [mode.value for mode in Mode]},
                ) from exc
        return list(dict.fromkeys(resolved))

    def _resolve(self, path: str) -> RepositoryIdentity:
        try:
            return repos.resolve_repository(Path(path).expanduser())
        except GitError as exc:
            raise TaskSpindleError(
                INVALID_REQUEST,
                f"{path}: {exc}",
                details={"code": exc.code, "repository": path},
            ) from exc

    def _repository_row(self, identity: RepositoryIdentity) -> str:
        row = self.store.find_repository(str(identity.common_dir), identity.root_commit)
        if row is not None:
            return str(row["id"])
        return self.store.insert_repository(
            new_repository_id(),
            str(identity.common_dir),
            identity.root_commit,
            str(identity.toplevel),
        )

    def _identity_for(self, repository_id: str | None) -> RepositoryIdentity:
        """The on-disk repository a stored repository row points at."""
        row = self.store.get_repository(repository_id or "")
        if row is None:
            raise TaskSpindleError(
                INVALID_REQUEST,
                f"no such repository: {repository_id}",
                details={"repository_id": repository_id},
            )
        display = row["display_path"] or str(Path(row["common_dir"]).parent)
        return self._resolve(display)

    # -- starting work ---------------------------------------------------------------

    def start_task(self, request: StartTaskRequest) -> dict[str, Any]:
        """Create, prepare and queue one task."""
        with self._cycle():
            profile = self._profile_for(request)
            placement = self._placement(request)
            record = create_task(
                self.store,
                request,
                repository_id=placement.repository_id,
                auth_mode=AuthMode(profile.auth),
            )
            self._prepare(record, request, placement)
            self.dispatch_queued()
            final = _require_task(self.store, record.id)
        return _acknowledge(final)

    def _profile_for(self, request: StartTaskRequest) -> Profile:
        try:
            return providers.profile_for_task(
                self.profiles,
                request.provider,
                mode=request.mode.value,
                allow_metered=request.allow_metered,
            )
        except ProfileError as exc:
            if exc.code == "METERED_NOT_ALLOWED":
                raise TaskSpindleError(METERED_NOT_ALLOWED, str(exc)) from exc
            raise TaskSpindleError(
                INVALID_REQUEST, str(exc), details={"code": exc.code, "provider": request.provider}
            ) from exc

    def _placement(self, request: StartTaskRequest) -> _Placement:
        """Resolve the repository a task will work in and prove it may work there."""
        if request.mode is Mode.IMPLEMENT:
            identity = self._resolve(request.repository or "")
            repository_id = self._granted(identity, request.provider, Mode.IMPLEMENT)
            return _Placement(identity=identity, repository_id=repository_id)
        if request.mode is Mode.REVIEW:
            return self._review_placement(request)
        if request.repository:
            identity = self._resolve(request.repository)
            repository_id = self._granted(identity, request.provider, Mode.CONSULT)
            return _Placement(identity=identity, repository_id=repository_id)
        return _Placement()

    def _review_placement(self, request: StartTaskRequest) -> _Placement:
        target = request.review_target
        assert target is not None  # StartTaskRequest refuses a review without one.
        if target.kind == "candidate":
            subject = _require_task(self.store, target.task_id or "")
            if subject.mode is not Mode.IMPLEMENT or subject.state is not TaskState.RESULT_READY:
                raise TaskSpindleError(
                    CANDIDATE_MISMATCH,
                    f"task {subject.id} is a {subject.mode.value} task in {subject.state.value}",
                    details={"task_id": subject.id, "state": subject.state.value},
                )
            if subject.candidate_sha != target.candidate_sha:
                raise TaskSpindleError(
                    CANDIDATE_MISMATCH,
                    "the candidate has moved since it was named",
                    details={
                        "task_id": subject.id,
                        "expected": target.candidate_sha,
                        "actual": subject.candidate_sha,
                    },
                )
            self._require_independent(subject.provider, request.provider)
            identity = self._identity_for(subject.repository_id)
            repository_id = self._granted(identity, request.provider, Mode.REVIEW)
            return _Placement(
                identity=identity, repository_id=repository_id, base=subject.candidate_sha
            )

        identity = self._resolve(target.repository or "")
        repository_id = self._granted(identity, request.provider, Mode.REVIEW)
        try:
            sha = worktrees.snapshot_working_tree(
                identity,
                expected_head=target.expected_head or "",
                paths=target.paths or ["."],
                snapshot_id=secrets.token_hex(6),
            )
        except GitError as exc:
            code = TARGET_MOVED if exc.code == "TARGET_MOVED" else INVALID_REQUEST
            raise TaskSpindleError(code, str(exc), details={"code": exc.code}) from exc
        return _Placement(identity=identity, repository_id=repository_id, base=sha)

    def _require_independent(self, author_id: str, reviewer_id: str) -> None:
        """A reviewer must be a genuinely different agent from the author."""
        author = self.profiles.get(author_id)
        reviewer = self.profiles.get(reviewer_id)
        if author is None or reviewer is None:
            raise TaskSpindleError(
                REVIEWER_NOT_INDEPENDENT,
                "independence cannot be established: one of the profiles is not configured",
                details={"author": author_id, "reviewer": reviewer_id},
            )
        if author.first_class:
            independent = reviewer.id == providers.opposite_provider(author.id)
        else:
            independent = providers.reviewer_independent(author, reviewer)
        if not independent:
            raise TaskSpindleError(
                REVIEWER_NOT_INDEPENDENT,
                f"{reviewer_id} is not an independent reviewer of {author_id}",
                details={"author": author_id, "reviewer": reviewer_id},
            )

    def _granted(self, identity: RepositoryIdentity, provider: str, mode: Mode) -> str:
        row = self.store.find_repository(str(identity.common_dir), identity.root_commit)
        if row is None:
            raise TaskSpindleError(
                GRANT_MISSING,
                f"repository {identity.toplevel} has not been authorized",
                details={"repository": str(identity.toplevel), "provider": provider,
                         "mode": mode.value},
            )
        repository_id = str(row["id"])
        require_grant(self.store, repository_id, provider, mode)
        return repository_id

    def _prepare(
        self,
        record: TaskRecord,
        request: StartTaskRequest,
        placement: _Placement,
    ) -> None:
        """Put the task's workspace on disk, open its first turn, and queue it."""
        try:
            fields = self._make_workspace(record, request, placement)
        except TaskSpindleError as exc:
            self._fail(record.id, exc.code, exc.message, exc.details)
            raise
        except GitError as exc:
            self._fail(record.id, exc.code, str(exc))
            raise TaskSpindleError(INVALID_REQUEST, str(exc), details={"code": exc.code}) from exc

        task = self.store.update_task(record.id, None, **fields)
        self.store.insert_turn(
            task.id,
            task.candidate_revision + 1,
            TurnKind.INITIAL.value,
            prompt=_compose_prompt(task, TurnKind.INITIAL),
        )
        transition(self.store, task.id, TaskState.QUEUED, reason="prepared")

    def _make_workspace(
        self,
        record: TaskRecord,
        request: StartTaskRequest,
        placement: _Placement,
    ) -> dict[str, Any]:
        if request.mode is Mode.IMPLEMENT:
            identity = placement.require_identity()
            base = repos.current_head(identity.toplevel)
            branch = repos.current_branch(identity.toplevel)
            snapshot = repos.snapshot_root(identity.toplevel)
            self._write_root_snapshot(record.id, snapshot)
            overlap = repos.overlapping_dirty_paths(snapshot, request.path_prefixes or ["."])
            if overlap:
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    "the repository has uncommitted changes inside the task's own path prefixes",
                    details={"code": DIRTY_OVERLAP, "paths": overlap},
                )
            worktree = worktrees.create_worktree(identity, base, self.worktree_dir(record.id))
            return {"base_head": base, "branch": branch, "worktree_path": str(worktree)}

        if placement.identity is None:
            scratch = worktrees.create_scratch_repo(self.scratch_dir(record.id))
            return {
                "base_head": repos.current_head(scratch.toplevel),
                "scratch_repo": str(scratch.toplevel),
            }

        identity = placement.identity
        base = placement.base or repos.current_head(identity.toplevel)
        worktree = worktrees.create_worktree(identity, base, self.worktree_dir(record.id))
        return {
            "base_head": base,
            "branch": repos.current_branch(identity.toplevel),
            "worktree_path": str(worktree),
        }

    def _write_root_snapshot(self, task_id: str, snapshot: RootSnapshot) -> None:
        """Record the root repository as it was before dispatch, for the worker to compare to."""
        payload = json.dumps(
            {"head": snapshot.head, "branch": snapshot.branch, "dirty": snapshot.dirty},
            sort_keys=True,
        )
        path = self.task_dir(task_id) / "root_snapshot.json"
        path.write_text(payload, encoding="utf-8")
        self.store.insert_artifact(
            task_id,
            0,
            "root_snapshot",
            "sha256:" + _digest(payload.encode("utf-8")),
            len(payload),
            str(path),
        )

    def _fail(
        self,
        task_id: str,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        *,
        reason: str = "preparation failed",
    ) -> None:
        """Record a task as FAILED with the reason it never got going."""
        with contextlib.suppress(TaskSpindleError):
            transition(
                self.store,
                task_id,
                TaskState.FAILED,
                reason=f"{reason}: {code}",
                error={
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "details": dict(details or {}),
                },
                finished_at=now(),
            )

    # -- dispatch ---------------------------------------------------------------------

    def dispatch_queued(self) -> list[str]:
        """Start a worker unit for every runnable task whose provider lease is free.

        The lease is taken *before* the unit, so a task that is already running holds it and is
        skipped here; the runner signs the lease with its own pid once it is up.
        """
        started: list[str] = []
        runnable: list[TaskRecord] = []
        for state in DISPATCHABLE_STATES:
            runnable.extend(self.store.list_tasks(state=state.value, limit=_SCAN_LIMIT))
        for task in sorted(runnable, key=lambda item: (item.created_at, item.id)):
            if self._start_worker(task):
                started.append(task.id)
        return started

    def _start_worker(self, task: TaskRecord) -> bool:
        """Take the provider lease and run the task's pending turn as a transient unit."""
        unit = units.worker_unit_name(task.id)
        if not self.store.acquire_lease(task.provider, task.id, unit, None, self.boot):
            return False
        try:
            self.units.start(
                unit,
                units.worker_argv(task.id),
                working_dir=self.paths.state_dir,
                env=units.unit_env(self.parent_env, config_file=self.paths.config_file),
                properties=units.WORKER_PROPERTIES,
            )
        except UnitError as exc:
            self.store.release_lease(task.provider, task.id)
            self._fail(
                task.id,
                UNIT_START_FAILED,
                str(exc),
                {"unit": unit},
                reason="dispatch failed",
            )
            return False
        current = self.store.get_task(task.id)
        if current is not None and current.unit_name != unit:
            self.store.update_task(task.id, None, unit_name=unit)
        return True

    # -- reading ----------------------------------------------------------------------

    def list_tasks(
        self,
        repository_id: str | None = None,
        provider: str | None = None,
        mode: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """The public view of every matching task, newest first."""
        with self._cycle():
            records = self.store.list_tasks(
                repository_id=repository_id,
                provider=provider,
                mode=mode,
                state=state,
                limit=limit,
            )
            views = [task_view(record).model_dump(mode="json") for record in records]
        return {"tasks": views}

    def task_status(self, task_id: str) -> dict[str, Any]:
        """One task's public view, plus what to do about it when nothing else can."""
        with self._cycle():
            record = _require_task(self.store, task_id)
            view = task_view(record).model_dump(mode="json")
            if record.state is TaskState.RECOVERY_AMBIGUOUS:
                view["evidence"] = self._last_recovery(task_id)
                view["manual_action"] = MANUAL_ACTION
        return view

    def _last_recovery(self, task_id: str) -> dict[str, Any]:
        for event in reversed(self.store.list_events(task_id)):
            if event["kind"] == EventKind.RECOVERY.value:
                return dict(event["payload"] or {})
        return {}

    def task_result(self, task_id: str) -> dict[str, Any]:
        """What a finished turn produced: its answer, its checks and its attribution."""
        with self._cycle():
            result = task_result(self.store, task_id).model_dump(mode="json")
        return result

    def task_diff(
        self,
        task_id: str,
        offset: int = 0,
        length: int = DIFF_PAGE_BYTES,
    ) -> dict[str, Any]:
        """One page of the current candidate's diff, and a receipt proving it was handed over."""
        with self._cycle():
            record = _require_task(self.store, task_id)
            artifact = self.store.get_artifact(
                task_id, record.candidate_revision, "candidate_diff"
            )
            if artifact is None or not artifact["path"]:
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    f"task {task_id} has no candidate diff at revision "
                    f"{record.candidate_revision}",
                    details={"task_id": task_id, "revision": record.candidate_revision},
                )
            size = int(artifact["size"])
            if offset < 0 or offset >= max(size, 1):
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    f"offset {offset} is outside a diff of {size} bytes",
                    details={"task_id": task_id, "offset": offset, "size": size},
                )
            wanted = max(0, min(length, DIFF_PAGE_BYTES, size - offset))
            if wanted <= 0:
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    "length must be positive",
                    details={"task_id": task_id, "length": length},
                )
            try:
                chunk = worktrees.read_diff_page(Path(artifact["path"]), offset, wanted)
            except OSError as exc:
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    f"the diff artifact could not be read: {exc}",
                    details={"task_id": task_id},
                ) from exc
            digest = str(artifact["digest"])
            receipt = record_diff_receipt(self.store, task_id, digest, offset, len(chunk))
            page = DiffPage(
                digest=digest,
                size=size,
                offset=offset,
                length=len(chunk),
                data=base64.b64encode(chunk).decode("ascii"),
                receipt_id=receipt,
            ).model_dump(mode="json")
        return page

    # -- continuing --------------------------------------------------------------------

    def continue_task(
        self,
        task_id: str,
        expected_state_version: int,
        prompt: str = "",
    ) -> dict[str, Any]:
        """Send one more turn to a task that can take one."""
        with self._cycle():
            record = _require_task(self.store, task_id)
            _require_version(record, expected_state_version)
            if record.state is TaskState.RECOVERY_AMBIGUOUS:
                record = self._settle_ambiguous(record)
            kind, target = _CONTINUATIONS.get(
                (record.state, record.mode), (None, None)
            )
            if kind is None or target is None:
                raise TaskSpindleError(
                    ILLEGAL_TRANSITION,
                    f"a {record.mode.value} task in {record.state.value} cannot be continued",
                    details={"task_id": task_id, "from": record.state.value},
                )
            if kind is TurnKind.RESUME and not record.session_id:
                raise TaskSpindleError(
                    RESUME_UNAVAILABLE,
                    f"task {task_id} has no session to resume",
                    details={"task_id": task_id},
                )
            if kind is TurnKind.RESUME:
                text = prompt.strip() or _RESUME_DEFAULT_PROMPT
            else:
                text = _compose_prompt(record, kind, continuation=prompt or None)
            revision = (
                record.candidate_revision + 1
                if kind is TurnKind.REPAIR
                else len(self.store.list_turns(task_id)) + 1
            )
            self.store.insert_turn(task_id, revision, kind.value, prompt=text)
            updated = transition(
                self.store,
                task_id,
                target,
                reason=f"{kind.value} turn requested",
                expected_state_version=record.state_version,
            )
            self._start_worker(updated)
            final = _require_task(self.store, task_id)
        return _acknowledge(final)

    def _settle_ambiguous(self, record: TaskRecord) -> TaskRecord:
        """Give recovery one more look before refusing to guess."""
        self.reconcile()
        fresh = _require_task(self.store, record.id)
        if fresh.state is TaskState.RECOVERY_AMBIGUOUS:
            raise TaskSpindleError(
                MANUAL_RECOVERY_REQUIRED,
                f"task {record.id} needs a person to decide what happened to its worker",
                details={
                    "task_id": record.id,
                    "evidence": self._last_recovery(record.id),
                    "manual_action": MANUAL_ACTION,
                },
            )
        return fresh

    # -- acceptance ----------------------------------------------------------------------

    def accept_task(self, request: AcceptTaskRequest) -> dict[str, Any]:
        """Gate an acceptance, journal it, and hand the root commit to the accept unit."""
        with self._cycle():
            unit = units.accept_unit_name(request.task_id)
            with self.store.transaction():
                record = check_acceptance(self.store, request)
                _accept_gates(self.store, record)
                self.store.write_journal(
                    record.id,
                    "probing",
                    target_head=request.expected_target_head,
                    candidate_sha=request.candidate_sha,
                )
                updated = apply_acceptance(self.store, request, unit_name=unit)
            try:
                self.units.start(
                    unit,
                    [sys.executable, "-m", "taskspindle.accept", "--task", record.id],
                    working_dir=self.paths.state_dir,
                    env=units.unit_env(self.parent_env, config_file=self.paths.config_file),
                    properties=units.WORKER_PROPERTIES,
                )
            except UnitError as exc:
                self._abandon_accept(updated, exc)
                raise TaskSpindleError(
                    UNIT_START_FAILED,
                    f"the accept unit could not be started: {exc}",
                    retryable=True,
                    details={"task_id": record.id, "unit": unit},
                ) from exc
            final = _require_task(self.store, request.task_id)
        return _acknowledge(final)

    def _abandon_accept(self, record: TaskRecord, exc: UnitError) -> None:
        """Nothing touched the repository, so put the candidate back where it was."""
        self.store.clear_journal(record.id)
        warning = f"{ACCEPT_FAILED}:{UNIT_START_FAILED}"
        warnings = list(record.warnings or [])
        if warning not in warnings:
            warnings.append(warning)
        self.store.append_event(
            record.id,
            EventKind.ACCEPT_FAILED,
            {"reason": UNIT_START_FAILED, "message": str(exc)},
        )
        with contextlib.suppress(TaskSpindleError):
            transition(
                self.store,
                record.id,
                TaskState.RESULT_READY,
                reason="the accept unit could not be started",
                warnings=warnings,
            )

    def record_integration(self, request: RecordIntegrationRequest) -> dict[str, Any]:
        """Record what a person did by hand: a resolved conflict, a manual merge, or a mutation."""
        with self._cycle():
            record = _require_task(self.store, request.task_id)
            _require_version(record, request.expected_state_version)
            payload = {
                "kind": request.kind,
                "summary": request.summary,
                "resulting_head": request.resulting_head,
            }
            if request.kind == "root_mutation_acknowledged":
                self.store.append_event(record.id, EventKind.INTEGRATION_RECORDED, payload)
                final = _require_task(self.store, request.task_id)
                return _acknowledge(final)

            if not request.resulting_head:
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    f"{request.kind} requires the resulting_head it left the repository at",
                    details={"task_id": record.id, "kind": request.kind},
                )
            if record.state is not TaskState.RESULT_READY:
                raise TaskSpindleError(
                    ILLEGAL_TRANSITION,
                    f"task {record.id} is {record.state.value}, not RESULT_READY",
                    details={"task_id": record.id, "from": record.state.value},
                )
            self.store.append_event(record.id, EventKind.INTEGRATION_RECORDED, payload)
            transition(
                self.store,
                record.id,
                TaskState.ACCEPTING,
                reason=f"{request.kind} recorded",
                expected_state_version=record.state_version,
                target_head=request.resulting_head,
            )
            final = transition(
                self.store,
                record.id,
                TaskState.ACCEPTED,
                reason=f"{request.kind} recorded",
                target_head=request.resulting_head,
                finished_at=now(),
            )
        return _acknowledge(final)

    def reject_task(
        self, task_id: str, expected_state_version: int, reason: str
    ) -> dict[str, Any]:
        """Discard a candidate without integrating it. The worktree is kept until cleanup."""
        with self._cycle():
            final = transition(
                self.store,
                task_id,
                TaskState.REJECTED,
                reason=reason or "rejected",
                expected_state_version=expected_state_version,
                finished_at=now(),
            )
            self.store.append_event(task_id, EventKind.REJECTED, {"reason": reason})
        return _acknowledge(final)

    # -- stopping ------------------------------------------------------------------------

    def cancel_task(self, task_id: str, expected_state_version: int) -> dict[str, Any]:
        """Ask a task to stop, and settle it here when there is nothing left to ask."""
        with self._cycle():
            record = _require_task(self.store, task_id)
            _require_version(record, expected_state_version)
            if record.state is TaskState.RECOVERY_AMBIGUOUS:
                self.reconcile()
                record = _require_task(self.store, task_id)
            cancelling = transition(
                self.store,
                task_id,
                TaskState.CANCELLING,
                reason="cancel requested",
                expected_state_version=record.state_version,
            )
            self.store.append_event(
                task_id, EventKind.CANCEL_REQUESTED, {"from": record.state.value}
            )
            if record.state in ACTIVE_STATES and cancelling.unit_name:
                with contextlib.suppress(UnitError):
                    self.units.kill(cancelling.unit_name, "SIGTERM")
                final = cancelling
            else:
                self.store.release_lease(record.provider, task_id)
                final = transition(
                    self.store,
                    task_id,
                    TaskState.CANCELLED,
                    reason="nothing was running",
                    finished_at=now(),
                )
        return _acknowledge(final)

    def cleanup_task(self, task_id: str, force: bool = False) -> dict[str, Any]:
        """Give back a finished task's worktree, refs and scratch space."""
        with self._cycle():
            record = _require_task(self.store, task_id)
            if record.state not in TERMINAL_STATES:
                raise TaskSpindleError(
                    ILLEGAL_TRANSITION,
                    f"task {task_id} is {record.state.value} and is still working",
                    details={"task_id": task_id, "from": record.state.value},
                )
            self.store.update_task(task_id, None, cleanup_state=CleanupState.PENDING)
            result = self._cleanup(record, force=force)
            self.store.append_event(task_id, EventKind.CLEANUP, result)
        return result

    def _cleanup(self, record: TaskRecord, *, force: bool) -> dict[str, Any]:
        removed: list[str] = []
        retained: list[str] = []
        identity: RepositoryIdentity | None = None
        if record.repository_id:
            try:
                identity = self._identity_for(record.repository_id)
            except TaskSpindleError:
                identity = None

        if record.worktree_path and Path(record.worktree_path).exists():
            if identity is None:
                retained.append(record.worktree_path)
            else:
                try:
                    worktrees.remove_worktree(
                        identity, Path(record.worktree_path), force=force
                    )
                    removed.append(record.worktree_path)
                except GitError as exc:
                    retained.append(record.worktree_path)
                    self.store.update_task(
                        record.id, None, cleanup_state=CleanupState.FAILED
                    )
                    return {
                        "task_id": record.id,
                        "cleanup_state": CleanupState.FAILED.value,
                        "removed": removed,
                        "retained": retained,
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            "retryable": False,
                            "details": {"task_id": record.id},
                        },
                    }

        if identity is not None:
            removed.extend(worktrees.delete_task_refs(identity, record.id))

        for path in (self.paths.state_dir / "tasks" / record.id / "tmp", record.scratch_repo):
            target = Path(path) if path else None
            if target is not None and target.exists():
                shutil.rmtree(target, ignore_errors=True)
                (removed if not target.exists() else retained).append(str(target))

        if record.unit_name:
            with contextlib.suppress(UnitError):
                self.units.reset_failed(record.unit_name)

        self.store.update_task(record.id, None, cleanup_state=CleanupState.COMPLETE)
        return {
            "task_id": record.id,
            "cleanup_state": CleanupState.COMPLETE.value,
            "removed": removed,
            "retained": retained,
        }


class _Placement:
    """Where a task will do its work, once the repository rules have been applied."""

    __slots__ = ("base", "identity", "repository_id")

    def __init__(
        self,
        identity: RepositoryIdentity | None = None,
        repository_id: str | None = None,
        base: str | None = None,
    ) -> None:
        self.identity = identity
        self.repository_id = repository_id
        self.base = base

    def require_identity(self) -> RepositoryIdentity:
        if self.identity is None:  # pragma: no cover - guarded by StartTaskRequest
            raise TaskSpindleError(INVALID_REQUEST, "this mode requires a repository")
        return self.identity


#: (state, mode) -> the turn kind a continuation sends and the state it enters.
_CONTINUATIONS: dict[tuple[TaskState, Mode], tuple[TurnKind, TaskState]] = {
    (TaskState.RESULT_READY, Mode.IMPLEMENT): (TurnKind.REPAIR, TaskState.REPAIRING),
    (TaskState.COMPLETED, Mode.CONSULT): (TurnKind.CONTINUE, TaskState.RESUMING),
    (TaskState.INTERRUPTED, Mode.CONSULT): (TurnKind.RESUME, TaskState.RESUMING),
    (TaskState.INTERRUPTED, Mode.REVIEW): (TurnKind.RESUME, TaskState.RESUMING),
    (TaskState.INTERRUPTED, Mode.IMPLEMENT): (TurnKind.RESUME, TaskState.RESUMING),
}


def _accept_gates(store: Store, record: TaskRecord) -> None:
    """The gates that need the candidate's own evidence, not just the review.

    They run before anything moves, so a refusal leaves the task exactly as it was.
    """
    summary = record.check_summary or {}
    if summary.get("ok") is not True:
        raise TaskSpindleError(
            CHECKS_FAILED,
            "the candidate's verification commands did not all pass",
            details={"task_id": record.id, "check_summary": summary},
        )
    warnings = list(record.warnings or [])
    blocking = [
        warning
        for warning in warnings
        if warning.startswith(f"{EventKind.SCOPE_VIOLATION.value}:")
        or warning == EventKind.SCOPE_VIOLATION.value
    ]
    if _unacknowledged_root_mutation(store, record, warnings):
        blocking.extend(
            warning for warning in warnings if warning.startswith(EventKind.ROOT_MUTATION.value)
        )
    if blocking:
        raise TaskSpindleError(
            ACCEPT_BLOCKED,
            "the candidate carries warnings that must be resolved before it can be accepted",
            details={"task_id": record.id, "warnings": blocking},
        )


def _unacknowledged_root_mutation(
    store: Store, record: TaskRecord, warnings: Sequence[str]
) -> bool:
    """True when the task mutated the root repository and nobody has said that is fine.

    The acknowledgement is a ``root_mutation_acknowledged`` integration record, so the decision
    is auditable: someone looked at what the agent did outside its worktree and signed for it.
    """
    if not any(warning.startswith(EventKind.ROOT_MUTATION.value) for warning in warnings):
        return False
    for event in store.list_events(record.id):
        payload = event["payload"] or {}
        if (
            event["kind"] == EventKind.INTEGRATION_RECORDED.value
            and payload.get("kind") == "root_mutation_acknowledged"
        ):
            return False
    return True


def _acknowledge(record: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": record.id,
        "state": record.state.value,
        "state_version": record.state_version,
    }


def _require_version(record: TaskRecord, expected: int) -> None:
    if record.state_version != expected:
        raise TaskSpindleError(
            STALE_STATE_VERSION,
            f"task {record.id} is at state_version {record.state_version}, not {expected}",
            details={
                "task_id": record.id,
                "expected": expected,
                "actual": record.state_version,
            },
        )


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _compose_prompt(
    task: TaskRecord, kind: TurnKind, *, continuation: str | None = None
) -> str:
    """The worker composes turns; this is the same function, imported where it is needed."""
    from .runner import compose_prompt  # imported here: runner imports this module.

    return compose_prompt(task, kind, continuation=continuation)
