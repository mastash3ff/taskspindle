"""The orchestrator: one object holding the store, the paths, the profiles and a unit backend,
with one method per MCP tool.

Everything it does to git, systemd and the filesystem goes through the modules that own those
things, so the rules live in :mod:`taskspindle.service` and the mechanics do not live here either;
this module is only the sequencing between them.
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
from typing import Any

import taskspindle

from . import integration, providers, recovery, repos, units, usage, worktrees
from .config import Paths
from .integration import Journal
from .models import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    AcceptTaskRequest,
    AuthMode,
    CleanupState,
    DiffPage,
    EventKind,
    Mode,
    RecordIntegrationRequest,
    StartTaskRequest,
    TaskRecord,
    TaskState,
    TurnKind,
)
from .providers import Profile, ProfileError
from .repos import GitError, RepositoryIdentity, RootSnapshot
from .runner import compose_prompt
from .service import (
    ACCEPT_BLOCKED,
    ACCEPT_FAILED,
    CANDIDATE_MISMATCH,
    CHECKS_FAILED,
    DIRTY_OVERLAP,
    GRANT_MISSING,
    ILLEGAL_TRANSITION,
    INVALID_REQUEST,
    MANUAL_RECOVERY_REQUIRED,
    METERED_NOT_ALLOWED,
    RESUME_UNAVAILABLE,
    REVIEWER_NOT_INDEPENDENT,
    STALE_STATE_VERSION,
    TARGET_MOVED,
    UNIT_START_FAILED,
    TaskSpindleError,
    apply_acceptance,
    check_acceptance,
    create_task,
    provider_availability,
    record_diff_receipt,
    require_diff_retrieved,
    require_grant,
    require_independent_review,
    require_provider_available,
    require_task,
    task_result,
    task_view,
    transition,
)
from .store import Store, now
from .units import UnitBackend, UnitError

__all__ = [
    "ACP_VERSION",
    "CONCURRENT_TURNS_PER_PROVIDER",
    "DIFF_PAGE_BYTES",
    "DIFF_PAGE_MAX_BYTES",
    "DISPATCHABLE_STATES",
    "ISOLATION",
    "MANUAL_ACTION",
    "TIMEOUT_BOUNDS",
    "USAGE_GROUP_BY",
    "Orchestrator",
    "new_repository_id",
    "utcnow",
]

#: The ACP revision the pinned client and adapter speak.
ACP_VERSION = "0.12.0"

#: The slice of a candidate diff ``task_diff`` returns when no length is named.
DIFF_PAGE_BYTES = worktrees.DEFAULT_DIFF_PAGE

#: Largest slice one ``task_diff`` call will return, whatever length is asked for.
DIFF_PAGE_MAX_BYTES = worktrees.MAX_DIFF_PAGE

#: The groupings ``usage_report`` accepts.
USAGE_GROUP_BY: tuple[str, ...] = ("provider", "day", "provider_day", "model", "mode", "repository_id")

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

#: What a continuation clears on the task, so a turn that loses the dispatch race is visibly
#: waiting for a lease rather than looking like a task whose unit vanished.
_AWAITING_DISPATCH: dict[str, Any] = {"unit_name": None, "worker_pid": None, "boot_id": None}


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

    def _unit_env(self) -> dict[str, str]:
        """The environment a worker or accept unit is started with.

        The declared secrets of every ``api_key`` profile are forwarded by name, because the
        worker rebuilds the agent's own environment from its own and cannot invent a credential
        the unit was never given. Values are never logged.
        """
        return units.unit_env(
            self.parent_env,
            config_file=self.paths.config_file,
            secret_names=self._secret_names(),
        )

    def _secret_names(self) -> tuple[str, ...]:
        """Every secret name any configured ``api_key`` profile declares, in sorted order."""
        return tuple(
            sorted(
                {
                    name
                    for profile in self.profiles.values()
                    if profile.auth == "api_key"
                    for name in profile.secret_env
                }
            )
        )

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
        """Reconcile, do the work, then dispatch whatever the work made runnable.

        The dispatch runs even when the body raised: a tool that refused half way through can
        still have freed a lease, and the next task in line should not wait for a later call.
        """
        self.reconcile()
        try:
            yield
        finally:
            self.dispatch_queued()

    def reconcile(self) -> list[recovery.ReconcileAction]:
        """Settle every task that claims to be active but may no longer be."""
        return recovery.reconcile(
            self.store,
            self.units,
            boot=self.boot,
            now=self.clock(),
            accept_recover=self._recover_accept,
        )

    def _recover_accept(self, task: TaskRecord) -> recovery.AcceptOutcome:
        """What an interrupted accept actually did to the root repository.

        A failure to settle it is never reported as an abort: an abort is a claim about the
        operator's repository, and a recovery that could not look at it, or that refused to touch
        it, has no basis for making one. Such a task goes to a person with its journal intact.
        """
        journal = self.store.read_journal(task.id)
        if journal is None:
            return recovery.AcceptOutcome("aborted")
        try:
            identity = self._identity_for(task.repository_id)
            outcome = integration.recover_journal(
                identity,
                Journal(
                    task_id=task.id,
                    phase=str(journal["phase"]),
                    target_head=str(journal["target_head"] or ""),
                    candidate_sha=str(journal["candidate_sha"] or ""),
                    changed_paths=tuple(journal["changed_paths"] or ()),
                ),
            )
            head = repos.current_head(identity.toplevel) if outcome == "committed" else None
        except (GitError, TaskSpindleError, ValueError):
            return recovery.AcceptOutcome("manual")
        self.store.clear_journal(task.id)
        return recovery.AcceptOutcome(outcome, target_head=head)

    # -- capabilities ---------------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        """Everything a caller needs to compose a valid request, and nothing about a task.

        ``availability`` is the one thing here that changes: what the last turn on each provider
        learned about its seat. A throttled provider is reported, with its reset time and the
        other first-class provider named, so the caller can choose; nothing is chosen for it.
        """
        now = self.clock()
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
                    "availability": provider_availability(self.store, profile, now=now),
                    "windows": self.store.latest_provider_windows(limits_key(profile)),
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
                "diff_page_max_bytes": DIFF_PAGE_MAX_BYTES,
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
        path: str | None = None,
        providers_: Sequence[str] | None = None,
        modes: Sequence[str] | None = None,
        *,
        repository_id: str | None = None,
    ) -> dict[str, Any]:
        """Withdraw matching active grants. Tasks already running are never touched.

        A repository is named by a path inside it, or -- when it no longer exists on disk and
        only its grants remain -- by the ``repository_id`` that ``list_repository_policies`` shows.
        """
        with self._cycle():
            if repository_id is not None:
                row = self.store.get_repository(repository_id)
                if row is None:
                    raise TaskSpindleError(
                        INVALID_REQUEST,
                        f"no such repository: {repository_id}",
                        details={"repository_id": repository_id},
                    )
                display = str(row["display_path"] or Path(row["common_dir"]).parent)
            else:
                if not path:
                    raise TaskSpindleError(
                        INVALID_REQUEST, "revoke_repository needs a path or a repository_id"
                    )
                identity = self._resolve(path)
                row = self.store.find_repository(str(identity.common_dir), identity.root_commit)
                if row is None:
                    raise TaskSpindleError(
                        INVALID_REQUEST,
                        f"repository {path} has never been authorized",
                        details={"repository": str(identity.toplevel)},
                    )
                display = str(identity.toplevel)
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
            result = {
                "repository_id": repository_id,
                "display_path": display,
                "grants": [_grant_view(grant) for grant in self.store.list_grants(repository_id)],
                "revoked": revoked,
            }
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
            require_provider_available(
                self.store, profile, now=self.clock(), ignore=request.ignore_provider_status
            )
            placement = self._placement(request)
            record = create_task(
                self.store,
                request,
                repository_id=placement.repository_id,
                auth_mode=AuthMode(profile.auth),
            )
            self._prepare(record, request, placement)
            self.dispatch_queued()
            final = require_task(self.store, record.id)
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
            subject = require_task(self.store, target.task_id or "")
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
            prompt=compose_prompt(
                task, TurnKind.INITIAL, review_diff=self._review_diff(task, placement)
            ),
        )
        transition(self.store, task.id, TaskState.QUEUED, reason="prepared")

    def _review_diff(self, task: TaskRecord, placement: _Placement) -> bytes | None:
        """The change a review task looks at, so a reviewer that cannot run git still sees it.

        A candidate review reads the subject's recorded diff artifact; a snapshot review diffs the
        expected head against the snapshot commit in the root repository, with read-only plumbing.
        """
        if task.mode is not Mode.REVIEW or not task.review_target:
            return None
        target = task.review_target
        try:
            if target.get("kind") == "candidate":
                subject = self.store.get_task(str(target.get("task_id") or ""))
                if subject is None:
                    return None
                artifact = self.store.get_artifact(
                    subject.id, subject.candidate_revision, "candidate_diff"
                )
                if artifact is None or not artifact["path"]:
                    return None
                return Path(str(artifact["path"])).read_bytes()
            if placement.identity is not None and placement.base and target.get("expected_head"):
                proc = repos.run_git(
                    ["diff", str(target["expected_head"]), placement.base],
                    cwd=placement.identity.toplevel,
                    check=False,
                )
                return proc.stdout if proc.returncode == 0 else None
        except (OSError, GitError):
            return None
        return None

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
        # A review or a consult is no less able to write outside its worktree than an implement
        # is, so every task with a repository behind it gets the snapshot the root check needs.
        self._write_root_snapshot(record.id, repos.snapshot_root(identity.toplevel))
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
        skipped here; the runner signs the lease with its own pid once it is up. A REPAIRING or
        RESUMING task that lost the race for its provider's lease is picked up here too, oldest
        first, which is the only thing that would ever start it.
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
                env=self._unit_env(),
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
            record = require_task(self.store, task_id)
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

    def usage_report(
        self,
        since: str | None = None,
        provider: str | None = None,
        group_by: str = "provider",
    ) -> dict[str, Any]:
        """Tokens, estimated cost, outcomes, timings, violations and windows, rolled up on read."""
        if group_by not in USAGE_GROUP_BY:
            raise TaskSpindleError(
                INVALID_REQUEST,
                f"no such group_by: {group_by}",
                details={"group_by": group_by, "known": list(USAGE_GROUP_BY)},
            )
        if provider is not None and provider not in self.profiles:
            self._known_providers([provider])
        now = self.clock()
        try:
            since_iso = usage.parse_since(since, now)
        except ValueError as exc:
            raise TaskSpindleError(INVALID_REQUEST, str(exc), details={"since": since}) from exc
        with self._cycle():
            result = usage.report(
                self.store,
                since=since_iso,
                provider=provider,
                group_by=group_by,
                profiles=self.profiles,
                now=now,
            )
        return result

    def task_diff(
        self,
        task_id: str,
        offset: int = 0,
        length: int = DIFF_PAGE_BYTES,
    ) -> dict[str, Any]:
        """One page of the current candidate's diff, and a receipt proving it was handed over."""
        with self._cycle():
            record = require_task(self.store, task_id)
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
            digest = str(artifact["digest"])
            if size == 0:
                # An empty diff is fully retrieved the moment it is asked for: the coverage of
                # [0, 0) is already complete, so the page carries no bytes and no receipt.
                return DiffPage(
                    digest=digest, size=0, offset=0, length=0, data="", receipt_id=0
                ).model_dump(mode="json")
            if offset < 0 or offset >= size:
                raise TaskSpindleError(
                    INVALID_REQUEST,
                    f"offset {offset} is outside a diff of {size} bytes",
                    details={"task_id": task_id, "offset": offset, "size": size},
                )
            wanted = max(0, min(length, DIFF_PAGE_MAX_BYTES, size - offset))
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
            record = require_task(self.store, task_id)
            _require_version(record, expected_state_version)
            if record.state is TaskState.RECOVERY_AMBIGUOUS:
                record = self._settle_ambiguous(record, expected_state_version)
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
                text = compose_prompt(record, kind, continuation=prompt or None)
            revision = (
                record.candidate_revision + 1
                if kind is TurnKind.REPAIR
                else len(self.store.list_turns(task_id)) + 1
            )
            self.store.insert_turn(task_id, revision, kind.value, prompt=text)
            # The unit the *previous* turn ran in is gone; clearing it is what tells recovery
            # that this task is waiting for a lease rather than for a worker that vanished.
            updated = transition(
                self.store,
                task_id,
                target,
                reason=f"{kind.value} turn requested",
                expected_state_version=record.state_version,
                **_AWAITING_DISPATCH,
            )
            self._start_worker(updated)
            final = require_task(self.store, task_id)
        return _acknowledge(final)

    def _settle_ambiguous(self, record: TaskRecord, expected_state_version: int) -> TaskRecord:
        """Give recovery one more look before refusing to guess.

        Recovery moves the task, so the caller's ``expected_state_version`` is re-checked against
        what the sweep left behind: a caller acts on the state it was shown, and that state has
        just changed underneath it.
        """
        self.reconcile()
        fresh = require_task(self.store, record.id)
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
        _require_version(fresh, expected_state_version)
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
                    changed_paths=list(record.changed_paths or []),
                )
                updated = apply_acceptance(self.store, request, unit_name=unit)
            try:
                self.units.start(
                    unit,
                    [sys.executable, "-m", "taskspindle.accept", "--task", record.id],
                    working_dir=self.paths.state_dir,
                    env=self._unit_env(),
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
            final = require_task(self.store, request.task_id)
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
        """Record what a person did by hand: a resolved conflict, a manual merge, or a mutation.

        A hand-made integration skips the checks and probe gates -- Codex ran the merge and the
        checks itself -- but not the gates that prove the candidate was *looked at*: the whole
        diff has been retrieved, an independent review covers this candidate, it stayed inside its
        declared scope, and any mutation of the root repository has been acknowledged. The head it
        claims to have landed at must also exist in the repository and differ from the base.
        """
        with self._cycle():
            record = require_task(self.store, request.task_id)
            _require_version(record, request.expected_state_version)
            payload: dict[str, Any] = {
                "kind": request.kind,
                "summary": request.summary,
                "resulting_head": request.resulting_head,
            }
            if request.kind == "root_mutation_acknowledged":
                # Bound to the revision it was signed for: a repair produces a new candidate, and
                # nobody has looked at what *that* one did outside its worktree.
                payload["revision"] = record.candidate_revision
                self.store.append_event(record.id, EventKind.INTEGRATION_RECORDED, payload)
                final = require_task(self.store, request.task_id)
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
            require_diff_retrieved(self.store, record)
            require_independent_review(self.store, record)
            _accept_gates(self.store, record, require_checks=False)
            head = self._resolve_head(record, request.resulting_head)
            payload["resulting_head"] = head
            payload["warnings"] = list(record.warnings or [])
            self.store.append_event(record.id, EventKind.INTEGRATION_RECORDED, payload)
            transition(
                self.store,
                record.id,
                TaskState.ACCEPTING,
                reason=f"{request.kind} recorded",
                expected_state_version=record.state_version,
                target_head=head,
            )
            final = transition(
                self.store,
                record.id,
                TaskState.ACCEPTED,
                reason=f"{request.kind} recorded",
                target_head=head,
                finished_at=now(),
            )
        return _acknowledge(final)

    def _resolve_head(self, record: TaskRecord, head: str) -> str:
        """The commit a hand-made integration claims to have produced, proven to exist."""
        identity = self._identity_for(record.repository_id)
        proc = repos.run_git(
            ["rev-parse", "--verify", "--quiet", f"{head}^{{commit}}"],
            cwd=identity.toplevel,
            check=False,
        )
        resolved = proc.stdout.decode("utf-8", "replace").strip()
        if proc.returncode != 0 or not resolved:
            raise TaskSpindleError(
                INVALID_REQUEST,
                f"{head} does not resolve to a commit in {identity.toplevel}",
                details={"task_id": record.id, "resulting_head": head},
            )
        if record.base_head and resolved == record.base_head:
            raise TaskSpindleError(
                INVALID_REQUEST,
                "the resulting head is the candidate's own base: nothing was integrated",
                details={"task_id": record.id, "resulting_head": resolved},
            )
        return resolved

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
            record = require_task(self.store, task_id)
            _require_version(record, expected_state_version)
            if record.state is TaskState.RECOVERY_AMBIGUOUS:
                record = self._settle_ambiguous(record, expected_state_version)
            cancelling = transition(
                self.store,
                task_id,
                TaskState.CANCELLING,
                reason="cancel requested",
                expected_state_version=record.state_version,
            )
            # The moment is recorded so that reconciliation can tell a worker that is still
            # shutting down from one that is ignoring the signal.
            self.store.append_event(
                task_id,
                EventKind.CANCEL_REQUESTED,
                {"from": record.state.value, "at": now()},
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
            record = require_task(self.store, task_id)
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
            if identity is None and force and self._owns(Path(record.worktree_path)):
                # The repository is gone, so ``git worktree remove`` has nothing to talk to. With
                # ``force`` the directory is TaskSpindle's own to delete: it lives under the state
                # directory and the only thing in it is a checkout nobody can use any more.
                shutil.rmtree(record.worktree_path, ignore_errors=True)
                (removed if not Path(record.worktree_path).exists() else retained).append(
                    record.worktree_path
                )
            elif identity is None:
                # The repository the worktree belongs to cannot be found, so nothing here can
                # remove it safely. That is a failed cleanup, not a complete one.
                retained.append(record.worktree_path)
                return self._cleanup_failed(
                    record,
                    removed,
                    retained,
                    code="REPOSITORY_UNRESOLVABLE",
                    message=(
                        f"the repository of task {record.id} could not be resolved; "
                        "pass force to remove its worktree directory anyway"
                    ),
                )
        if record.worktree_path and Path(record.worktree_path).exists() and identity is not None:
            try:
                worktrees.remove_worktree(
                    identity, Path(record.worktree_path), force=force
                )
                removed.append(record.worktree_path)
            except GitError as exc:
                retained.append(record.worktree_path)
                return self._cleanup_failed(
                    record, removed, retained, code=exc.code, message=str(exc)
                )

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

        if retained:
            return self._cleanup_failed(
                record,
                removed,
                retained,
                code="RESOURCES_RETAINED",
                message="some of the task's resources could not be given back",
            )
        self.store.update_task(record.id, None, cleanup_state=CleanupState.COMPLETE)
        return {
            "task_id": record.id,
            "cleanup_state": CleanupState.COMPLETE.value,
            "removed": removed,
            "retained": retained,
        }

    def _owns(self, path: Path) -> bool:
        """True when ``path`` is inside the state directory: something TaskSpindle made."""
        try:
            return path.resolve().is_relative_to(self.paths.state_dir.resolve())
        except OSError:
            return False

    def _cleanup_failed(
        self,
        record: TaskRecord,
        removed: list[str],
        retained: list[str],
        *,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        """A cleanup that left something behind is FAILED, and says what it left."""
        self.store.update_task(record.id, None, cleanup_state=CleanupState.FAILED)
        return {
            "task_id": record.id,
            "cleanup_state": CleanupState.FAILED.value,
            "removed": removed,
            "retained": retained,
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
                "details": {"task_id": record.id, "retained": retained},
            },
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


def _accept_gates(store: Store, record: TaskRecord, *, require_checks: bool = True) -> None:
    """The gates that need the candidate's own evidence, not just the review.

    They run before anything moves, so a refusal leaves the task exactly as it was.
    ``require_checks`` is dropped for a hand-made integration: Codex ran the checks itself. The
    scope and root-mutation gates are not dropped -- what the agent did outside the ground it was
    given is not something a manual merge has answered for.
    """
    summary = record.check_summary or {}
    if require_checks and summary.get("ok") is not True:
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
    is auditable: someone looked at what the agent did outside its worktree and signed for it. It
    is bound to the candidate revision it was given for -- a repair makes a new candidate, whose
    root mutations nobody has seen yet.
    """
    if not any(warning.startswith(EventKind.ROOT_MUTATION.value) for warning in warnings):
        return False
    for event in store.list_events(record.id):
        payload = event["payload"] or {}
        if (
            event["kind"] == EventKind.INTEGRATION_RECORDED.value
            and payload.get("kind") == "root_mutation_acknowledged"
            and payload.get("revision") == record.candidate_revision
        ):
            return False
    return True


def limits_key(profile: Profile) -> str:
    """The provider row a profile's availability lives under (see :mod:`taskspindle.limits`)."""
    from .limits import status_key

    return status_key(profile)


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
