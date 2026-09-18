"""Enums and pydantic models shared by every TaskSpindle component.

Nothing here touches git, systemd, the network or a model provider: these are plain data
shapes plus the validation rules that can be decided from the request alone.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SUBJECT_LENGTH = 72
#: The request-level cap on ``context_files``; the operator's configured cap may be lower.
MAX_CONTEXT_FILES_PER_REQUEST = 32


class Mode(StrEnum):
    """What a task is allowed to do."""

    CONSULT = "consult"
    REVIEW = "review"
    IMPLEMENT = "implement"


class TaskState(StrEnum):
    """Lifecycle state of a task."""

    PREPARING = "PREPARING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    RESULT_READY = "RESULT_READY"
    ACCEPTING = "ACCEPTING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    REPAIRING = "REPAIRING"
    RESUMING = "RESUMING"
    INTERRUPTED = "INTERRUPTED"
    RECOVERY_AMBIGUOUS = "RECOVERY_AMBIGUOUS"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


#: States in which a live worker process or accept unit may exist.
ACTIVE_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.PREPARING,
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.REPAIRING,
        TaskState.RESUMING,
        TaskState.ACCEPTING,
        TaskState.CANCELLING,
    }
)

#: States in which no work is in flight. Only a COMPLETED consult task can be reopened, and only
#: by an advisory follow-up turn.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.ACCEPTED,
        TaskState.REJECTED,
        TaskState.CANCELLED,
        TaskState.FAILED,
    }
)


class CleanupState(StrEnum):
    """Worktree cleanup axis, independent of :class:`TaskState`."""

    RETAINED = "RETAINED"
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


class AuthMode(StrEnum):
    """How the provider process authenticates."""

    OAUTH = "oauth"
    API_KEY = "api_key"


class TurnKind(StrEnum):
    """Why a turn was sent to the agent."""

    INITIAL = "initial"
    REPAIR = "repair"
    CONTINUE = "continue"
    RESUME = "resume"


class Verdict(StrEnum):
    """Overall outcome of a review."""

    PASS = "PASS"
    CONCERN = "CONCERN"
    BLOCK = "BLOCK"


class Severity(StrEnum):
    """Severity of a single review finding."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Disposition(StrEnum):
    """What the accepting session did about a review finding."""

    FIXED = "fixed"
    ACCEPTED_RISK = "accepted_risk"
    NOT_APPLICABLE = "not_applicable"
    OVERRIDDEN = "overridden"


class EventKind(StrEnum):
    """Append-only audit event kinds."""

    TASK_CREATED = "TASK_CREATED"
    STATE_CHANGED = "STATE_CHANGED"
    DIFF_RETRIEVED = "DIFF_RETRIEVED"
    REVIEW_RECORDED = "REVIEW_RECORDED"
    REVIEW_OVERRIDE = "REVIEW_OVERRIDE"
    ACCEPT_REQUESTED = "ACCEPT_REQUESTED"
    ACCEPT_FAILED = "ACCEPT_FAILED"
    INTEGRATION_RECORDED = "INTEGRATION_RECORDED"
    REJECTED = "REJECTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CLEANUP = "CLEANUP"
    ROOT_MUTATION = "ROOT_MUTATION"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    READ_ONLY_VIOLATION = "READ_ONLY_VIOLATION"
    DELEGATION_ATTEMPT = "DELEGATION_ATTEMPT"
    MODE_SWITCH_ATTEMPT = "MODE_SWITCH_ATTEMPT"
    RECOVERY = "RECOVERY"
    WARNING = "WARNING"
    #: A provider refused a turn because of a quota, rate or subscription limit.
    PROVIDER_LIMIT = "PROVIDER_LIMIT"
    #: A provider's availability row changed; not tied to one task.
    PROVIDER_STATUS = "PROVIDER_STATUS"


def _one_line(value: str, field: str) -> str:
    """Return ``value`` if it is a single non-empty line of at most 72 characters."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be empty")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be a single line")
    if len(stripped) > MAX_SUBJECT_LENGTH:
        raise ValueError(f"{field} must be at most {MAX_SUBJECT_LENGTH} characters")
    return stripped


def _check_path_prefix(value: str) -> str:
    """Return ``value`` if it is a safe repository-relative path prefix."""
    if not value:
        raise ValueError("path prefixes must not be empty")
    if value == ".":
        return value
    if value.startswith("/"):
        raise ValueError(f"path prefix {value!r} must be repository-relative")
    if "\\" in value:
        raise ValueError(f"path prefix {value!r} must use forward slashes")
    if any(part == ".." for part in value.split("/")):
        raise ValueError(f"path prefix {value!r} must not contain a '..' segment")
    return value


class ErrorBody(BaseModel):
    """The error half of an :class:`Envelope`."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class Envelope(BaseModel):
    """Uniform response wrapper for every MCP tool."""

    model_config = ConfigDict(extra="forbid")

    api_version: int = 1
    ok: bool
    result: dict[str, Any] | None = None
    error: ErrorBody | None = None


#: The named review stances a ``review`` task can take.
ReviewKind = Literal["standard", "adversarial"]


class ReviewTarget(BaseModel):
    """What a ``review`` task is asked to look at."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["candidate", "snapshot"]
    # kind == "candidate"
    task_id: str | None = None
    candidate_sha: str | None = None
    # kind == "snapshot"
    repository: str | None = None
    expected_head: str | None = None
    paths: list[str] | None = None

    @model_validator(mode="after")
    def _check_kind(self) -> ReviewTarget:
        if self.kind == "candidate":
            if not self.task_id or not self.candidate_sha:
                raise ValueError("a candidate review target needs task_id and candidate_sha")
        elif not self.repository or not self.expected_head or self.paths is None:
            raise ValueError("a snapshot review target needs repository, expected_head and paths")
        return self


class AuthorizeRepositoryRequest(BaseModel):
    """Arguments of the ``authorize_repository`` tool."""

    model_config = ConfigDict(extra="forbid")

    path: str
    providers: list[str] = Field(min_length=1)
    modes: list[Mode] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _non_empty_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must not be empty")
        return value


class StartTaskRequest(BaseModel):
    """Arguments of the ``start_task`` tool."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    mode: Mode
    prompt: str
    repository: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout_s: int = Field(default=1800, ge=60, le=14400)
    allow_metered: bool = False
    #: An explicit coordinator override: admit this task even while the provider's status says
    #: it is not eligible yet. There is no other bypass; a caller that sets this owns the decision.
    ignore_provider_status: bool = False
    #: The dispatch-policy role this task was assigned for; recorded for usage reporting only.
    role: str | None = Field(default=None, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")
    # implement only
    acceptance_criteria: str | None = None
    path_prefixes: list[str] | None = None
    verification_commands: list[str] | None = None
    candidate_message: str | None = None
    # review only
    review_target: ReviewTarget | None = None
    #: The reviewer's stance. ``standard`` is the fixed defect review; ``adversarial`` tells the
    #: reviewer to assume the change is wrong and hunt for the evidence. The output contract and
    #: the acceptance rules are identical for both.
    review_kind: ReviewKind = "standard"
    #: Absolute paths of files whose contents are copied into the worker's first turn, read by
    #: the server under the operator's ``[context_files]`` allowlist. Any mode.
    context_files: list[str] | None = None

    @field_validator("provider", "prompt")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("path_prefixes")
    @classmethod
    def _check_prefixes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [_check_path_prefix(entry) for entry in value]

    @field_validator("context_files")
    @classmethod
    def _check_context_files(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > MAX_CONTEXT_FILES_PER_REQUEST:
            raise ValueError(f"at most {MAX_CONTEXT_FILES_PER_REQUEST} context files per task")
        seen: list[str] = []
        for item in value:
            if not item or "\x00" in item:
                raise ValueError("context file paths must be non-empty text")
            if not item.startswith("/"):
                raise ValueError(f"context file {item!r} must be an absolute path")
            if any(part == ".." for part in item.split("/")):
                raise ValueError(f"context file {item!r} must not contain a '..' segment")
            if item not in seen:
                seen.append(item)
        return seen

    @field_validator("candidate_message")
    @classmethod
    def _check_candidate_message(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _one_line(value, "candidate_message")

    @model_validator(mode="after")
    def _check_mode(self) -> StartTaskRequest:
        if self.mode is Mode.IMPLEMENT:
            missing = []
            if not self.repository:
                missing.append("repository")
            if not self.acceptance_criteria or not self.acceptance_criteria.strip():
                missing.append("acceptance_criteria")
            if not self.path_prefixes:
                missing.append("path_prefixes")
            if self.verification_commands is None:
                missing.append("verification_commands")
            if not self.candidate_message:
                missing.append("candidate_message")
            if missing:
                raise ValueError(f"implement mode requires: {', '.join(missing)}")
        elif self.mode is Mode.REVIEW and self.review_target is None:
            raise ValueError("review mode requires review_target")
        if self.mode is not Mode.REVIEW and self.review_kind != "standard":
            raise ValueError("review_kind is review only")
        return self


class TaskRecord(BaseModel):
    """One row of the ``tasks`` table."""

    model_config = ConfigDict(extra="forbid")

    id: str
    state: TaskState
    state_version: int = 1
    cleanup_state: CleanupState = CleanupState.RETAINED
    repository_id: str | None = None
    provider: str
    provider_family: str | None = None
    auth_mode: AuthMode
    mode: Mode
    prompt: str
    requested_model: str | None = None
    requested_effort: str | None = None
    resolved_model: str | None = None
    resolved_effort: str | None = None
    timeout_s: int = 1800
    allow_metered: bool = False
    ignore_provider_status: bool = False
    role: str | None = None
    acceptance_criteria: str | None = None
    path_prefixes: list[str] | None = None
    verification_commands: list[str] | None = None
    candidate_message: str | None = None
    review_target: dict[str, Any] | None = None
    review_kind: str | None = None
    context_files: list[str] | None = None
    base_head: str | None = None
    target_head: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    scratch_repo: str | None = None
    session_id: str | None = None
    reported_model: str | None = None
    oauth_evidence: dict[str, Any] | None = None
    candidate_sha: str | None = None
    candidate_revision: int = 0
    changed_paths: list[str] | None = None
    diff_digest: str | None = None
    diff_size: int | None = None
    check_summary: dict[str, Any] | None = None
    warnings: list[str] | None = None
    error: dict[str, Any] | None = None
    unit_name: str | None = None
    worker_pid: int | None = None
    boot_id: str | None = None
    heartbeat_at: str | None = None
    response: str | None = None
    transcript_path: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> TaskRecord:
        """Build a record from a ``sqlite3.Row`` (or any mapping) of the ``tasks`` table."""
        data = dict(row)
        for column in TASK_JSON_COLUMNS:
            raw = data.get(column)
            data[column] = json.loads(raw) if isinstance(raw, str) else raw
        for column in TASK_BOOL_COLUMNS:
            if data.get(column) is not None:
                data[column] = bool(data[column])
        return cls(**data)


#: ``tasks`` columns stored as JSON text.
TASK_JSON_COLUMNS: tuple[str, ...] = (
    "path_prefixes",
    "context_files",
    "verification_commands",
    "review_target",
    "oauth_evidence",
    "changed_paths",
    "check_summary",
    "warnings",
    "error",
)

#: ``tasks`` columns stored as 0/1 integers.
TASK_BOOL_COLUMNS: tuple[str, ...] = ("allow_metered", "ignore_provider_status")

#: Every ``tasks`` column, in model order.
TASK_COLUMNS: tuple[str, ...] = tuple(TaskRecord.model_fields)


class TaskView(BaseModel):
    """The public projection returned by ``task_status``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    state: TaskState
    state_version: int
    cleanup_state: CleanupState
    provider: str
    provider_family: str | None = None
    auth_mode: AuthMode
    mode: Mode
    repository_id: str | None = None
    base_head: str | None = None
    target_head: str | None = None
    branch: str | None = None
    worktree_path: str | None = None
    session_id: str | None = None
    requested_model: str | None = None
    resolved_model: str | None = None
    resolved_effort: str | None = None
    role: str | None = None
    review_kind: str | None = None
    context_files: list[str] = Field(default_factory=list)
    reported_model: str | None = None
    oauth_evidence: dict[str, Any] = Field(default_factory=dict)
    candidate_sha: str | None = None
    candidate_revision: int = 0
    changed_paths: list[str] = Field(default_factory=list)
    diff_digest: str | None = None
    diff_size: int | None = None
    check_summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: ErrorBody | None = None
    unit_name: str | None = None
    heartbeat_at: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None


class CheckRecord(BaseModel):
    """One verification command that ran in the worktree."""

    model_config = ConfigDict(extra="forbid")

    command: str
    exit_code: int
    ok: bool
    duration_ms: int
    stdout_tail: str = ""
    stderr_tail: str = ""


class TaskResult(BaseModel):
    """The payload of ``task_result``."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    state: TaskState
    response: str | None = None
    checks: list[CheckRecord] = Field(default_factory=list)
    attribution: dict[str, Any] = Field(default_factory=dict)
    #: The task's warnings, as ``task_status`` shows them.
    warnings: list[str] = Field(default_factory=list)
    #: Every quota, rate or subscription limit a turn of this task ran into.
    quota_warnings: list[dict[str, Any]] = Field(default_factory=list)
    #: Token usage of the task's turns, newest last; empty when the agent reported none.
    usage: list[dict[str, Any]] = Field(default_factory=list)
    transcript_locator: str | None = None
    #: The worker's provider session id, as ``task_status`` shows it.
    session_id: str | None = None
    #: How a human reopens that session natively, when the provider family allows it.
    resume: dict[str, Any] | None = None


class ReviewFinding(BaseModel):
    """A single issue raised by a reviewer."""

    model_config = ConfigDict(extra="forbid")

    id: str
    severity: Severity
    path: str
    line: int = Field(ge=1)
    evidence: str
    remedy: str


class ReviewOutput(BaseModel):
    """The structured verdict a reviewer returns."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)


class FindingDisposition(BaseModel):
    """What the accepting session decided about one finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    disposition: Disposition
    reason: str | None = None

    @model_validator(mode="after")
    def _check_reason(self) -> FindingDisposition:
        if self.disposition is not Disposition.FIXED and not (self.reason or "").strip():
            raise ValueError(f"disposition {self.disposition.value} requires a reason")
        return self


class AcceptTaskRequest(BaseModel):
    """Arguments of the ``accept_task`` tool.

    Every gate that is not about repository safety is opt-in, defaulting to off: a caller states
    what it wants enforced. The merge-tree probe, the integration journal, the scope
    (``path_prefixes``) check and ``state_version`` are never optional.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    expected_state_version: int
    candidate_sha: str
    #: Required, and checked against the candidate, only when ``require_diff_receipts`` is set.
    diff_digest: str | None = None
    inspection_summary: str
    expected_target_head: str
    #: The review covering this candidate. Optional when ``require_review`` is False; when named
    #: it is still checked for being bound to this candidate and independent of its author.
    review_task_id: str | None = None
    dispositions: list[FindingDisposition] = Field(default_factory=list)
    commit_message: str
    #: Require a named, independent review whose blocking findings are all disposed.
    require_review: bool = False
    #: Require the diff digest to match and every byte of it to have been retrieved.
    require_diff_receipts: bool = False
    #: Run the candidate's verification commands again in the root repository, and let that run
    #: decide ``CHECKS_FAILED`` instead of the worker's own ``check_summary``.
    rerun_verification: bool = False
    #: Block acceptance on an unacknowledged ``ROOT_MUTATION`` warning instead of just recording it.
    require_root_stability: bool = False

    @field_validator("inspection_summary")
    @classmethod
    def _check_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("inspection_summary must not be empty")
        return value

    @field_validator("commit_message")
    @classmethod
    def _check_commit_message(cls, value: str) -> str:
        return _one_line(value, "commit_message")


class RecordIntegrationRequest(BaseModel):
    """Arguments of the ``record_integration`` tool.

    The same opt-in gates as :class:`AcceptTaskRequest`, off by default.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    expected_state_version: int
    kind: Literal["conflict_resolved", "manual_integration", "root_mutation_acknowledged"]
    resulting_head: str | None = None
    summary: str
    require_review: bool = False
    require_diff_receipts: bool = False
    rerun_verification: bool = False
    require_root_stability: bool = False


class DiffPage(BaseModel):
    """One page of a candidate diff, as returned by ``task_diff``."""

    model_config = ConfigDict(extra="forbid")

    digest: str
    size: int
    offset: int
    length: int
    data: str
    receipt_id: int
