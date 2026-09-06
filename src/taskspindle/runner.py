"""The detached worker: one process, one turn, one recorded outcome.

A worker is started by the server as a transient systemd unit and is the only thing that talks to
an agent. It runs exactly one turn and then exits, so there is no long-lived process to lose track
of: every fact the turn produced is in the store before the process returns, and anything that
could not be recorded leaves the task in a state :mod:`taskspindle.recovery` knows how to settle.

The bias throughout is to retain work. A resume that the agent cannot honour is INTERRUPTED, not
FAILED, because the worktree and the session are still there; a cancelled turn is CANCELLED with
its transcript written; a scope or root violation is a warning on a recorded candidate, not a
discarded one. Only an outcome that cannot be represented at all becomes FAILED.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import limits, providers, repos, units, usage, worktrees
from .acp_client import AcpError, AcpWorker, PermissionPolicy, TurnResult
from .agy_cli import AgyCliWorker
from .agy_cli import model_catalog as cli_model_catalog
from .config import Paths, load_config
from .config import paths as default_paths
from .integration import run_verification
from .models import (
    TERMINAL_STATES,
    CheckRecord,
    EventKind,
    Mode,
    ReviewTarget,
    TaskRecord,
    TaskState,
    TurnKind,
)
from .providers import Profile, ProfileError
from .repos import GitError, RepositoryIdentity, RootSnapshot
from .review import ReviewParseError, parse_review_output
from .service import LEASE_BUSY, TaskSpindleError, require_task_profile, transition
from .store import Store, StoreError, now

__all__ = [
    "TURN_KINDS_BY_STATE",
    "compose_prompt",
    "main",
    "request_cancel",
    "run_worker",
]

#: How often the worker proves it is alive, in seconds.
HEARTBEAT_INTERVAL_S = 5.0

#: Verification never runs longer than this, whatever the task's own timeout is.
MAX_VERIFICATION_S = 1800

#: How long to keep capturing after a prompt response, per provider family, for the agents that
#: send their turn summary after answering. Grok's ``turn_completed`` arrives within milliseconds.
LATE_UPDATE_GRACE_S: dict[str, float] = {"grok": 2.0}

#: Warning recorded when the pre-dispatch root snapshot is missing, so the root check could not run.
ROOT_CHECK_SKIPPED = "ROOT_CHECK_SKIPPED"

#: Which turn kinds each entry state may start.
TURN_KINDS_BY_STATE: dict[TaskState, frozenset[TurnKind]] = {
    TaskState.QUEUED: frozenset({TurnKind.INITIAL}),
    TaskState.REPAIRING: frozenset({TurnKind.REPAIR}),
    TaskState.RESUMING: frozenset({TurnKind.RESUME, TurnKind.CONTINUE}),
}

#: Live workers in this process, keyed by task id, so a signal or a test can cancel a turn.
_CANCEL_HOOKS: dict[str, Callable[[], None]] = {}


def request_cancel(task_id: str) -> bool:
    """Ask a turn running in this process to cancel. False when no turn is in flight."""
    hook = _CANCEL_HOOKS.get(task_id)
    if hook is None:
        return False
    hook()
    return True


# -- prompts --------------------------------------------------------------------------------

_IMPLEMENT_RULES = """
Working directory: the current directory is a detached git worktree created for this task.
Change only files under these path prefixes: {prefixes}
Acceptance criteria:
{criteria}
Verification commands (TaskSpindle will run these after you finish):
{commands}
Do not spawn subagents and do not delegate any part of this task.
Do not modify anything outside this directory.
Finish with a short summary of what changed.
""".strip()

_REVIEW_RULES = """
You are reviewing code. This is a read-only task: do not edit, create or delete any file, and do
not run anything that changes the working tree. Do not spawn subagents. The working directory is
checked out at the subject, so you can read any file; the change under review is given below.
Subject: {subject}
Output only the JSON object below, matching this schema exactly:
{{"verdict": "PASS" | "CONCERN" | "BLOCK",
  "summary": "one short paragraph",
  "findings": [{{"id": "unique-id",
                "severity": "low" | "medium" | "high" | "critical",
                "path": "repository-relative/path",
                "line": 1,
                "evidence": "what you saw",
                "remedy": "what to do about it"}}],
  "checks": ["what you verified"]}}
Output only the JSON object.
""".strip()


def _review_subject(task: TaskRecord) -> str:
    """A one-line description of what a review task is looking at."""
    raw = task.review_target or {}
    try:
        target = ReviewTarget(**raw)
    except ValueError:
        return "the current worktree"
    if target.kind == "candidate":
        return f"candidate commit {target.candidate_sha} of task {target.task_id}"
    paths = ", ".join(target.paths or ["."])
    return f"working-tree snapshot {target.expected_head} of {target.repository} (paths: {paths})"


#: How much of a candidate diff is put into a review prompt before it is cut and said so.
MAX_REVIEW_DIFF_BYTES = 96 * 1024


def review_diff_section(diff: bytes | None) -> str:
    """The diff a reviewer is shown, capped so the prompt stays bounded."""
    if not diff:
        return ""
    cut = len(diff) > MAX_REVIEW_DIFF_BYTES
    body = diff[:MAX_REVIEW_DIFF_BYTES].decode("utf-8", "replace")
    note = (
        f"\n[diff truncated at {MAX_REVIEW_DIFF_BYTES} bytes of {len(diff)}; the full change is "
        "checked out in the working directory]"
        if cut
        else ""
    )
    return f"\n\nThe change under review, as a unified diff:\n```diff\n{body}\n```{note}"


def compose_prompt(
    task: TaskRecord,
    kind: TurnKind,
    *,
    continuation: str | None = None,
    review_diff: bytes | None = None,
) -> str:
    """Build the text of one turn.

    It lives here rather than in the server so that both sides compose turns the same way: the
    server writes the prompt into the pending turn row, and the worker sends whatever it finds
    there.
    """
    if kind is TurnKind.REPAIR:
        return f"Continue in this same session and worktree.\n\n{continuation or task.prompt}"
    if kind is TurnKind.CONTINUE:
        return f"Continue.\n\n{continuation or task.prompt}"
    if task.mode is Mode.IMPLEMENT:
        commands = task.verification_commands or []
        rules = _IMPLEMENT_RULES.format(
            prefixes=", ".join(task.path_prefixes or ["."]),
            criteria=(task.acceptance_criteria or "").strip(),
            commands="\n".join(f"- {command}" for command in commands) or "- (none)",
        )
        return f"{task.prompt}\n\n{rules}"
    if task.mode is Mode.REVIEW:
        rules = _REVIEW_RULES.format(subject=_review_subject(task))
        return f"{rules}\n\n{task.prompt}{review_diff_section(review_diff)}"
    return task.prompt


# -- internal control flow ------------------------------------------------------------------


class _Failure(Exception):
    """A turn that has to be recorded as FAILED."""

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
        self.body = {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        }


class _Interrupted(Exception):
    """A turn that could not run but left everything it needs to be resumed later."""

    def __init__(self, warning: str, message: str) -> None:
        super().__init__(message)
        self.warning = warning


class _Log:
    """The worker's own append-only log. Codes and states only -- never agent output."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._handle = path.open("a", encoding="utf-8")

    def write(self, message: str) -> None:
        self._handle.write(f"{now()} {message}\n")
        self._handle.flush()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._handle.close()


@dataclass
class _Run:
    """Everything one turn accumulates on its way to a recorded outcome."""

    store: Store
    paths: Paths
    task: TaskRecord
    dir: Path
    log: _Log
    turn_id: int
    revision: int
    kind: TurnKind
    prompt: str
    warnings: list[str] = field(default_factory=list)
    profile: Profile | None = None
    child_env: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None
    result: TurnResult | None = None
    cancelled: bool = False
    turn_completed: bool = False
    #: What the agent said about itself at ``initialize``: its name and version, not a model.
    agent_info: dict[str, Any] = field(default_factory=dict)
    #: The model that actually answered, when the wire or the session file said.
    reported_model: str | None = None
    session_model: str | None = None
    usage: usage.TurnUsage | None = None
    prompt_started_at: str | None = None
    prompt_ended_at: str | None = None
    #: Account state before provider startup; successful completion may clear only this observation.
    provider_status_at_start: dict[str, Any] | None = None

    @property
    def task_id(self) -> str:
        return self.task.id

    def warn(self, warning: str, kind: EventKind, payload: dict[str, Any]) -> None:
        """Record a warning once on the task and always as an event."""
        if warning not in self.warnings:
            self.warnings.append(warning)
        self.store.append_event(self.task_id, kind, payload)
        self.log.write(f"warning {warning}")


#: Warnings that describe what one turn did. A later turn re-earns them or it does not: a scope
#: violation a repair fixed must not follow the task around for the rest of its life.
_PER_TURN_WARNINGS = (
    "SCOPE_VIOLATION:",
    "ROOT_MUTATION:",
    "READ_ONLY_VIOLATION",
    "DELEGATION_ATTEMPT",
    "MODE_SWITCH_ATTEMPT",
    ROOT_CHECK_SKIPPED,
)


def _carried_warnings(warnings: list[str] | None) -> list[str]:
    """The task's warnings minus the ones that belong to the turn that raised them."""
    return [
        warning for warning in warnings or [] if not warning.startswith(_PER_TURN_WARNINGS)
    ]


def _task_dir(paths: Paths, task_id: str) -> Path:
    return paths.state_dir / "tasks" / task_id


def _pending_turn(store: Store, task_id: str) -> dict[str, Any] | None:
    """The turn row the server inserted for this run: the last one still open."""
    open_turns = [turn for turn in store.list_turns(task_id) if turn["ended_at"] is None]
    return open_turns[-1] if open_turns else None


# -- the worker -----------------------------------------------------------------------------


async def run_worker(
    store: Store,
    task_id: str,
    *,
    profiles: Mapping[str, Profile],
    paths: Paths,
    boot: str | None = None,
    oauth_runner: Callable[..., Any] | None = None,
    signals: bool = True,
) -> TaskState:
    """Run the task's pending turn to a recorded outcome and return the state it settled in.

    ``boot`` defaults to :func:`taskspindle.units.boot_id` -- as a ``None`` default rather than a
    call in the signature, so the boot id is read when the worker runs, not when the module is
    imported.
    """
    boot = boot or units.boot_id()
    task = store.get_task(task_id)
    if task is None:
        raise TaskSpindleError("TASK_NOT_FOUND", f"no such task: {task_id}")
    allowed_kinds = TURN_KINDS_BY_STATE.get(task.state)
    if allowed_kinds is None:
        raise TaskSpindleError(
            "ILLEGAL_TRANSITION",
            f"a worker cannot start a task in {task.state.value}",
            details={"task_id": task_id, "from": task.state.value},
        )
    pending = _pending_turn(store, task_id)
    if pending is None:
        raise TaskSpindleError(
            "INVALID_REQUEST",
            f"task {task_id} has no pending turn row",
            details={"task_id": task_id},
        )
    kind = TurnKind(pending["kind"])
    if kind not in allowed_kinds:
        raise TaskSpindleError(
            "INVALID_REQUEST",
            f"a {kind.value} turn cannot start from {task.state.value}",
            details={"task_id": task_id, "kind": kind.value, "from": task.state.value},
        )

    directory = _task_dir(paths, task_id)
    log: _Log | None = None
    run: _Run | None = None
    heartbeat: asyncio.Task[None] | None = None
    handler_installed = False
    running = False
    try:
        log = _Log(directory / "worker.log")
        run = _Run(
            store=store,
            paths=paths,
            task=task,
            dir=directory,
            log=log,
            turn_id=int(pending["id"]),
            revision=int(pending["revision"]),
            kind=kind,
            prompt=pending["prompt"] or compose_prompt(task, kind),
            warnings=_carried_warnings(task.warnings),
        )
        log.write(f"start task={task_id} state={task.state.value} kind={kind.value} boot={boot}")

        stamp = now()
        run.task = transition(
            store,
            task_id,
            TaskState.RUNNING,
            reason=f"{kind.value} turn started",
            unit_name=units.worker_unit_name(task_id),
            worker_pid=os.getpid(),
            boot_id=boot,
            started_at=stamp,
            heartbeat_at=stamp,
        )
        running = True

        cancel_event = asyncio.Event()
        heartbeat = asyncio.create_task(_heartbeat(run))
        handler_installed = _install_signal_handler(cancel_event, run) if signals else False
        return await _run_turn(run, cancel_event, boot=boot, profiles=profiles, oauth_runner=oauth_runner)
    except _Failure as failure:
        if run is None:  # pragma: no cover - _Failure is only raised once the turn is under way
            raise
        run.log.write(f"failed {failure.code}")
        return _settle(run, TaskState.FAILED, failure.code, error=failure.body)
    except _Interrupted as interrupted:
        if run is None:  # pragma: no cover - as above
            raise
        run.warn(
            interrupted.warning,
            EventKind.WARNING,
            {"warning": interrupted.warning, "message": str(interrupted)},
        )
        return _settle(run, TaskState.INTERRUPTED, interrupted.warning)
    except Exception as exc:
        # Anything not already shaped into an outcome: record it rather than leaving the task
        # RUNNING for recovery to puzzle over. Only the class name -- a message may carry secrets.
        if not running or run is None:
            raise
        failure = _Failure("WORKER_ERROR", type(exc).__name__)
        run.log.write(f"failed {failure.code} ({type(exc).__name__})")
        return _settle(run, TaskState.FAILED, failure.code, error=failure.body)
    finally:
        if handler_installed:
            _remove_signal_handler()
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        if running and run is not None:
            _complete_turn(run)
            store.release_lease(run.task.provider, task_id)
        if log is not None:
            log.write("done")
            log.close()


def _install_signal_handler(cancel_event: asyncio.Event, run: _Run) -> bool:
    """Ask for SIGTERM to cancel the turn. False when the loop cannot take signal handlers."""
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, cancel_event.set)
    except (NotImplementedError, RuntimeError, ValueError):
        run.log.write("SIGTERM handler unavailable")
        return False
    return True


def _remove_signal_handler() -> None:
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)


async def _heartbeat(run: _Run) -> None:
    """Prove liveness on the task and on the provider lease until cancelled."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        try:
            run.store.heartbeat(run.task_id)
            run.store.touch_lease(run.task_id)
        except StoreError as exc:  # pragma: no cover - the store is local and open
            run.log.write(f"heartbeat failed: {exc}")


async def _run_turn(
    run: _Run,
    cancel_event: asyncio.Event,
    *,
    boot: str,
    profiles: Mapping[str, Profile],
    oauth_runner: Callable[..., Any] | None,
) -> TaskState:
    """Steps 2 to 8: lease, profile, agent, turn, and the mode-specific finalisation."""
    task = run.task
    _check_lease(run, boot=boot)
    profile = _resolve_profile(run, profiles)
    run.profile = profile
    run.provider_status_at_start = run.store.get_provider_status(limits.status_key(profile))

    task_tmp = run.dir / "tmp"
    task_tmp.mkdir(parents=True, exist_ok=True)
    try:
        run.child_env = providers.build_child_env(profile, os.environ, task_tmp=task_tmp)
    except ProfileError as exc:
        raise _Failure(exc.code, str(exc)) from exc

    evidence = _pre_spawn_evidence(profile, oauth_runner)
    workspace = _workspace(run)

    stderr_path = run.dir / f"agent-{run.revision}.stderr"
    native = profile.family == "agy"
    worker = _native_agy_worker(run, profile, workspace, stderr_path) if native else AcpWorker(
        command=providers.launch_command(profile, task.mode.value),
        env=run.child_env,
        cwd=workspace,
        stderr_path=stderr_path,
        policy=_permission_policy(profile, task, workspace),
        late_update_grace=LATE_UPDATE_GRACE_S.get(profile.family, 0.0),
    )
    try:
        async with worker as agent:
            if native:
                catalog = await cli_model_catalog(agent.command, run.child_env, workspace)
                _configure_native_agy(run, profile, catalog)
                from .agy_cli_adapter import ADAPTER_VERSION

                run.agent_info = {"name": "antigravity-cli", "version": ADAPTER_VERSION}
                evidence = {"auth": "oauth", "auth_method_id": "cached_native_cli",
                            "source": "authenticated_model_catalog"}
                run.task = run.store.update_task(run.task_id, None, oauth_evidence=evidence)
                result = await _prompt(run, agent, cancel_event, native=True)
            else:
                run.agent_info = dict(agent.init.agent_info) if agent.init else {}
                if evidence is None:
                    evidence = _post_init_evidence(profile, agent)
                run.task = run.store.update_task(run.task_id, None, oauth_evidence=evidence)
                await _open_session(run, agent)
                result = await _prompt(run, agent, cancel_event)
    except AcpError as exc:
        # The client retains updates even when prompt/cancel/timeout raises. Capture
        # them before settling so failure never discards useful partial work.
        run.result = worker.last_result
        if run.result is not None:
            await _record_usage(run, profile, workspace, run.result)
            _record_violations(run, run.result)
        if run.cancelled:
            # An agent that errors out because it was cancelled was still cancelled: the outcome
            # the operator asked for is the one to record.
            run.log.write(f"cancel raised {exc.code}")
            return _settle_cancelled(run)
        if exc.code == "RESUME_UNAVAILABLE":
            raise _Interrupted("RESUME_UNAVAILABLE", str(exc)) from exc
        verdict = limits.classify_acp_error(exc, family=profile.family)
        if verdict.provider_state is not None:
            _record_provider_limit(run, profile, verdict)
        raise _Failure(
            verdict.code,
            verdict.reason,
            retryable=verdict.retryable,
            details={
                "provider": run.task.provider,
                "status_key": limits.status_key(profile),
                "window": verdict.window,
                "reset_at": verdict.reset_at,
                "acp_code": exc.code,
                "rpc_data": exc.cause.get("rpc_data"),
            },
        ) from exc

    run.result = result
    _record_provider_health(run, profile, result)
    await _record_usage(run, profile, workspace, result)
    _record_violations(run, result)
    if run.cancelled or result.stop_reason == "cancelled":
        return _settle_cancelled(run)
    return await _finalize(run, workspace, result)


def _native_agy_worker(run: _Run, profile: Profile, workspace: Path, stderr_path: Path) -> AgyCliWorker:
    from .agy_cli_policy import prepare_launch

    if len(profile.command) != 1:
        raise _Failure("PROFILE_INVALID", "Native AGY requires one pinned executable")
    if run.kind is not TurnKind.INITIAL and not run.task.session_id:
        raise _Interrupted("RESUME_UNAVAILABLE", "the task has no native AGY session to resume")
    run.session_id = run.task.session_id
    previous = run.store.list_turn_usage(task_id=run.task_id)
    prior_usage = None
    for row in reversed(previous):
        raw = row.get("raw") or {}
        if isinstance(raw.get("_agy_cli_cumulative"), dict):
            prior_usage = raw["_agy_cli_cumulative"]
            break

    def remember_session(session_id: str) -> None:
        if run.task.session_id and session_id != run.task.session_id:
            raise AcpError("RESUME_UNAVAILABLE", "Native AGY returned a different conversation")
        run.session_id = session_id
        run.task = run.store.update_task(run.task_id, None, session_id=session_id)

    try:
        command = prepare_launch(
            binary=Path(profile.command[0]), workspace=workspace, task_dir=run.dir,
            home=Path(run.child_env["HOME"]), mode=run.task.mode.value,
            allowed_prefixes=run.task.path_prefixes or (),
            verification_commands=run.task.verification_commands or (),
        )
    except (OSError, ValueError) as exc:
        raise _Failure("AGY_POLICY_UNAVAILABLE", str(exc)) from exc
    return AgyCliWorker(
        command=command, env=run.child_env, cwd=workspace, stderr_path=stderr_path,
        on_session=remember_session, prior_usage=prior_usage, mode=run.task.mode.value,
    )


def _configure_native_agy(run: _Run, profile: Profile, catalog: list[tuple[str, str]]) -> None:
    from .agy import ModelSelection, ModelSelectionError, resolve_model

    persisted = (
        ModelSelection(run.task.resolved_model, run.task.resolved_effort)
        if run.task.resolved_model else None
    )
    if run.kind is not TurnKind.INITIAL and persisted is None:
        raise _Interrupted("RESUME_UNAVAILABLE", "AGY session has no recorded model selection")
    defaults = profile if persisted is None else None
    try:
        selection = resolve_model(
            {"models": {"availableModels": [{"modelId": key, "name": name} for key, name in catalog]}},
            model=run.task.requested_model or (defaults.model if defaults else None),
            effort=run.task.requested_effort or (defaults.effort if defaults else None),
            persisted=persisted,
        )
    except ModelSelectionError as exc:
        raise _Failure(exc.code, str(exc)) from exc
    run.session_model = selection.model_id
    run.task = run.store.update_task(
        run.task_id, None, resolved_model=selection.model_id, resolved_effort=selection.effort,
    )


def _permission_policy(profile: Profile, task: TaskRecord, workspace: Path) -> PermissionPolicy:
    if profile.family == "agy":
        from .agy_policy import AgyPermissionPolicy

        return AgyPermissionPolicy(
            allow_writes=task.mode is Mode.IMPLEMENT, workspace=workspace,
            commands=task.verification_commands or (),
        )
    return PermissionPolicy(allow_writes=task.mode is Mode.IMPLEMENT)


def _check_lease(run: _Run, *, boot: str) -> None:
    """The server takes the lease before starting the unit; the worker only signs it."""
    lease = run.store.get_lease(run.task.provider, run.task_id)
    if lease is None or lease["task_id"] != run.task_id:
        holder = lease["task_id"] if lease else None
        raise _Failure(
            LEASE_BUSY,
            f"the {run.task.provider} lease is not held by this task",
            details={"provider": run.task.provider, "held_by": holder},
        )
    run.store.bind_lease(
        run.task.provider,
        run.task_id,
        unit_name=units.worker_unit_name(run.task_id),
        pid=os.getpid(),
        boot_id=boot,
    )


def _resolve_profile(run: _Run, profiles: Mapping[str, Profile]) -> Profile:
    try:
        profile = providers.profile_for_task(
            profiles,
            run.task.provider,
            mode=run.task.mode.value,
            allow_metered=run.task.allow_metered,
        )
        require_task_profile(run.task, profile)
        return profile
    except ProfileError as exc:
        raise _Failure(exc.code, str(exc)) from exc
    except TaskSpindleError as exc:
        raise _Failure(exc.code, exc.message, details=exc.details) from exc


def _pre_spawn_evidence(
    profile: Profile,
    oauth_runner: Callable[..., Any] | None,
) -> dict[str, Any] | None:
    """Evidence that can be gathered before the agent starts; None means ask it later.

    Keyed on the profile *family* rather than its id, so a profile derived from ``claude`` is
    checked the same way the built-in is, and an ``api_key`` profile is never asked for an OAuth
    seat it does not have.
    """
    if profile.auth != "oauth":
        return {"auth": profile.auth}
    if profile.family == "claude":
        try:
            if oauth_runner is None:
                return providers.claude_oauth_evidence()
            return providers.claude_oauth_evidence(oauth_runner)
        except ProfileError as exc:
            raise _Failure(exc.code, str(exc)) from exc
    if profile.family == "grok":
        return None
    if profile.family == "agy":
        # The native CLI validates its existing login while fetching the model catalog.
        return None
    return {"auth": profile.auth}


def _post_init_evidence(profile: Profile, agent: AcpWorker) -> dict[str, Any]:
    """Grok's evidence comes from what it advertised at ``initialize``."""
    method_ids = agent.init.auth_method_ids if agent.init else ()
    try:
        return providers.grok_oauth_evidence(
            [{"id": method_id} for method_id in method_ids],
            home=Path(os.environ.get("HOME", "")),
        )
    except ProfileError as exc:
        raise _Failure(exc.code, str(exc)) from exc


def _workspace(run: _Run) -> Path:
    raw = run.task.worktree_path or run.task.scratch_repo
    if not raw:
        raise _Failure("NO_WORKSPACE", f"task {run.task_id} has no worktree or scratch repository")
    return Path(raw)


async def _open_session(run: _Run, agent: AcpWorker) -> None:
    """Create the session an initial turn runs in, or resume the one a later turn continues.

    A resume the agent cannot honour is an interruption, never a failure: the worktree and the
    session id are untouched, so the turn can be tried again against an agent that can load it.
    """
    options = providers.session_options(run.profile) if run.profile else {}
    if run.profile and run.profile.family == "agy":
        if agent.init is None or "oauth-personal" not in agent.init.auth_method_ids:
            raise _Failure("OAUTH_REJECTED", "AGY adapter did not advertise oauth-personal authentication")
        options = {"mcp_servers": []}
    if run.kind is TurnKind.INITIAL:
        session_id = await agent.new_session(**options)
        run.session_id = session_id
        run.task = run.store.update_task(run.task_id, None, session_id=session_id)
    else:
        if not run.task.session_id:
            raise _Interrupted("RESUME_UNAVAILABLE", "the task has no session to resume")
        await agent.load_session(run.task.session_id, **options)
        run.session_id = run.task.session_id
    if run.profile and run.profile.family == "agy":
        await _configure_agy_session(run, agent)
        return
    await _apply_session_mode(run, agent)
    run.session_model = agent.session_model
    if run.session_model is not None:
        run.log.write(f"session model from ACP: {run.session_model}")


async def _configure_agy_session(run: _Run, agent: AcpWorker) -> None:
    from .agy import ModelSelection, ModelSelectionError, resolve_model

    persisted = None
    if run.task.resolved_model:
        persisted = ModelSelection(run.task.resolved_model, run.task.resolved_effort)
    elif run.kind is not TurnKind.INITIAL:
        raise _Interrupted("RESUME_UNAVAILABLE", "AGY session has no recorded model selection")
    # Profile defaults apply once. A later profile edit cannot renegotiate the
    # model or effort already bound to this task's conversation.
    defaults = run.profile if persisted is None else None
    try:
        selection = resolve_model(
            {"configOptions": agent.session_config_options},
            model=run.task.requested_model or (defaults.model if defaults else None),
            effort=run.task.requested_effort or (defaults.effort if defaults else None),
            persisted=persisted,
        )
    except ModelSelectionError as exc:
        raise _Failure(exc.code, str(exc)) from exc
    session_id = run.session_id or ""
    await agent.set_config_option(session_id, "mode", "default")
    await agent.set_config_option(session_id, "model", selection.model_id)
    run.session_model = selection.model_id
    run.task = run.store.update_task(
        run.task_id, None, resolved_model=selection.model_id, resolved_effort=selection.effort,
    )


async def _apply_session_mode(run: _Run, agent: AcpWorker) -> None:
    """Put a Claude-family session in the mode its task needs, and say so in the log.

    Without this the adapter starts in whatever ``permissions.defaultMode`` the operator's own
    Claude settings name -- ``bypassPermissions`` on a machine that runs Claude Code that way --
    and the permission gate is never consulted.
    """
    mode = providers.session_mode(run.profile, run.task.mode.value) if run.profile else None
    if mode is None or not run.session_id:
        return
    await agent.set_mode(run.session_id, mode)
    run.log.write(f"session mode {mode}")


async def _prompt(
    run: _Run, agent: AcpWorker | AgyCliWorker, cancel_event: asyncio.Event, *, native: bool = False,
) -> TurnResult:
    """Send the turn, racing it against a cancel request."""
    session_id = run.session_id or ""
    _CANCEL_HOOKS[run.task_id] = cancel_event.set
    run.prompt_started_at = now()
    options = {"model": run.task.resolved_model, "effort": run.task.resolved_effort} if native else {}
    turn = asyncio.create_task(agent.prompt(
        session_id or None if native else session_id,
        f"TaskSpindle workspace: {agent.cwd}\n\n{run.prompt}" if native else run.prompt,
        timeout=float(run.task.timeout_s), **options,
    ))
    waiter = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({turn, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if turn not in done:
            run.cancelled = True
            run.log.write("cancel requested")
            await agent.cancel(session_id)
        return await turn
    finally:
        run.prompt_ended_at = now()
        _CANCEL_HOOKS.pop(run.task_id, None)
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter


def _record_provider_limit(run: _Run, profile: Profile, verdict: limits.Classification) -> None:
    """A quota or auth refusal is written on the task, on the provider, and in the event log.

    It is never acted on here: the next ``start_task`` and ``capabilities`` read the provider row
    and say so, and the caller chooses. Nothing is retried on another provider.
    """
    key = limits.status_key(profile)
    observed = now()
    payload = {
        "code": verdict.code,
        "provider": run.task.provider,
        "status_key": key,
        "state": verdict.provider_state,
        "window": verdict.window,
        "reset_at": verdict.reset_at,
        "observed_at": observed,
        "source": verdict.source,
        "message": verdict.reason,
    }
    run.store.set_provider_status(
        key,
        verdict.provider_state or "ok",
        code=verdict.code,
        window=verdict.window,
        reason=verdict.reason,
        reset_at=verdict.reset_at,
        task_id=run.task_id,
        source=verdict.source,
        observed_at=observed,
    )
    run.store.append_event(run.task_id, EventKind.PROVIDER_LIMIT, payload)
    run.store.append_event(None, EventKind.PROVIDER_STATUS, payload)
    if verdict.provider_state == "throttled":
        run.store.insert_provider_window(
            key,
            verdict.window or "unknown",
            status="rejected",
            used_percent=100.0,
            resets_at=verdict.reset_at,
            task_id=run.task_id,
            source="throttle_error",
            observed_at=observed,
        )
    run.log.write(f"provider {key} {verdict.provider_state} ({verdict.code})")


def _record_provider_health(run: _Run, profile: Profile, result: TurnResult) -> None:
    """Record observed windows without clearing a sibling's newer quota or auth failure."""
    key = limits.status_key(profile)
    rejected = None
    for info in result.capture.rate_limits:
        window = limits.rate_limit_window(info)
        run.store.insert_provider_window(
            key,
            window["window"],
            status=window["status"],
            used_percent=window["used_percent"],
            resets_at=window["resets_at"],
            task_id=run.task_id,
            source="rate_limit_event",
        )
        if window["status"] == "rejected":
            rejected = window
    if rejected is not None:
        run.store.set_provider_status(
            key,
            "throttled",
            code=limits.PROVIDER_THROTTLED,
            window=rejected["window"],
            reason="the agent reported its usage window as rejected",
            reset_at=rejected["resets_at"],
            task_id=run.task_id,
            source="rate_limit_event",
        )
    elif not run.cancelled and result.stop_reason != "cancelled":
        run.store.mark_provider_healthy(
            key, expected=run.provider_status_at_start, task_id=run.task_id,
        )


#: How long, and how often, to wait for Claude Code to write the session record that names the
#: model: it lands a few tens of milliseconds after the prompt response does.
_SESSION_FILE_RETRIES = 8
_SESSION_FILE_WAIT_S = 0.25


async def _record_usage(run: _Run, profile: Profile, workspace: Path, result: TurnResult) -> None:
    """What the turn cost and which model answered, from the wire first."""
    duration_ms: int | None = None
    if run.prompt_started_at and run.prompt_ended_at:
        started = datetime.fromisoformat(run.prompt_started_at.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(run.prompt_ended_at.replace("Z", "+00:00"))
        duration_ms = int((ended - started).total_seconds() * 1000)
    home = Path(os.environ.get("HOME", ""))
    collected = usage.collect(
        result,
        profile=profile,
        cwd=workspace,
        session_id=run.session_id,
        home=home,
        duration_ms=duration_ms,
        started_at=run.prompt_started_at,
        # AGY picker IDs describe our selection, not the backend that answered.
        session_model=None if profile.family == "agy" else run.session_model,
    )
    model = collected.model
    session_path = usage.claude_session_path(profile, workspace, run.session_id, home)
    if model is None and session_path is not None:
        for _ in range(_SESSION_FILE_RETRIES):
            await asyncio.sleep(_SESSION_FILE_WAIT_S)
            model = usage.claude_session_model(session_path)
            if model is not None:
                break
    run.usage = usage.with_model(collected.usage, model) if collected.usage else None
    # AGY's selected picker ID is a resolved request, not evidence of which
    # backend actually answered. Preserve the distinction when telemetry is absent.
    run.reported_model = (
        result.capture.model_ids[0] if result.capture.model_ids else None
    ) if profile.family == "agy" else model


def _record_violations(run: _Run, result: TurnResult) -> None:
    """Permission-gate refusals are always warnings and always events."""
    for violation in dict.fromkeys(result.capture.violations):
        kind = EventKind(violation) if violation in EventKind.__members__ else EventKind.WARNING
        run.warn(violation, kind, {"warning": violation, "revision": run.revision})


# -- finalisation ---------------------------------------------------------------------------


async def _finalize(run: _Run, workspace: Path, result: TurnResult) -> TaskState:
    # Every mode is checked against the root snapshot: a consult or a review can write outside
    # its worktree just as easily as an implement can, and it is just as much a warning.
    _check_root(run)
    if run.task.mode is Mode.CONSULT:
        return _settle(run, TaskState.COMPLETED, "consult turn finished", response=result.text)
    if run.task.mode is Mode.REVIEW:
        return _finalize_review(run, workspace, result)
    return await _finalize_implement(run, workspace, result)


def _finalize_review(run: _Run, workspace: Path, result: TurnResult) -> TaskState:
    """A review that touched the worktree is completed without a recorded verdict."""
    if not worktrees.worktree_is_clean(workspace):
        run.warn(
            "READ_ONLY_VIOLATION",
            EventKind.READ_ONLY_VIOLATION,
            {"warning": "READ_ONLY_VIOLATION", "path": str(workspace)},
        )
        return _settle(run, TaskState.COMPLETED, "review discarded", response=result.text)

    try:
        review = parse_review_output(result.text)
    except ReviewParseError as exc:
        raise _Failure(exc.code, exc.detail) from exc

    target = run.task.review_target or {}
    subject_task_id = target.get("task_id") or run.task_id
    candidate_sha = target.get("candidate_sha") or target.get("expected_head")
    run.store.insert_review(
        run.task_id,
        subject_task_id,
        candidate_sha,
        run.task.provider,
        review,
    )
    run.store.append_event(
        run.task_id,
        EventKind.REVIEW_RECORDED,
        {
            "subject_task_id": subject_task_id,
            "candidate_sha": candidate_sha,
            "verdict": review.verdict.value,
            "findings": len(review.findings),
        },
    )
    return _settle(run, TaskState.COMPLETED, "review recorded", response=result.text)


def _agy_control_changes(workspace: Path, base_sha: str) -> list[str]:
    """Controls created in new directories must never enter an AGY candidate."""
    from .agy_cli_policy import CONTROL_DIRS, CONTROL_FILES

    changed = repos.run_git(
        ["diff", "--name-only", "--no-renames", "-z", base_sha], cwd=workspace,
    ).stdout
    untracked = repos.run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], cwd=workspace,
    ).stdout
    protected = CONTROL_DIRS | CONTROL_FILES | {".git"}
    paths = {raw.decode("utf-8", "surrogateescape") for raw in (changed + untracked).split(b"\0") if raw}
    return sorted(path for path in paths if any(part in protected for part in Path(path).parts))


async def _finalize_implement(run: _Run, workspace: Path, result: TurnResult) -> TaskState:
    task = run.task
    identity = _identity(workspace)
    revision = task.candidate_revision + 1
    try:
        if task.provider_family == "agy":
            forbidden = _agy_control_changes(workspace, task.base_head or "HEAD")
            if forbidden:
                run.warn("AGY_CONTROL_PATH_VIOLATION", EventKind.WARNING, {"paths": forbidden})
                raise _Failure("AGY_CONTROL_PATH_VIOLATION", "AGY changed protected agent control paths")
        candidate = worktrees.collapse_candidate(
            identity,
            workspace,
            task_id=run.task_id,
            revision=revision,
            base_sha=task.base_head or "",
            message=task.candidate_message or f"TaskSpindle candidate {revision}",
        )
    except GitError as exc:
        raise _Failure(exc.code, str(exc)) from exc

    prefixes = repos.normalize_prefixes(task.path_prefixes or ["."])
    outside = worktrees.scope_violations(candidate.changed_paths, prefixes)
    if outside:
        run.warn(
            f"SCOPE_VIOLATION:{','.join(outside)}",
            EventKind.SCOPE_VIOLATION,
            {"paths": outside, "prefixes": list(prefixes)},
        )

    diff_path = run.dir / f"rev-{revision}.diff"
    try:
        digest, size = worktrees.write_diff_artifact(
            identity,
            base_sha=task.base_head or "",
            candidate_sha=candidate.sha,
            dest=diff_path,
        )
    except GitError as exc:
        raise _Failure(exc.code, str(exc)) from exc
    run.store.insert_artifact(
        run.task_id, revision, "candidate_diff", digest, size, str(diff_path)
    )

    summary = await _run_checks(run, workspace, revision)
    return _settle(
        run,
        TaskState.RESULT_READY,
        "candidate ready",
        candidate_sha=candidate.sha,
        candidate_revision=revision,
        changed_paths=list(candidate.changed_paths),
        diff_digest=digest,
        diff_size=size,
        check_summary=summary,
        response=result.text,
    )


async def _run_checks(run: _Run, workspace: Path, revision: int) -> dict[str, Any]:
    commands = run.task.verification_commands or []
    # Off the event loop: verification can run for as long as the task's own timeout, and the
    # heartbeat has to keep beating while it does.
    results = await asyncio.to_thread(
        run_verification,
        workspace,
        commands,
        timeout_s=min(run.task.timeout_s, MAX_VERIFICATION_S),
        env=run.child_env,
    )
    for check in results:
        run.store.insert_check(
            run.task_id,
            revision,
            CheckRecord(
                command=check.command,
                exit_code=check.exit_code,
                ok=check.ok,
                duration_ms=check.duration_ms,
                stdout_tail=check.stdout_tail,
                stderr_tail=check.stderr_tail,
            ),
        )
    passed = sum(1 for check in results if check.ok)
    return {
        "total": len(commands),
        "passed": passed,
        "ok": passed == len(commands),
    }


def _identity(path: Path) -> RepositoryIdentity:
    try:
        return repos.resolve_repository(path)
    except GitError as exc:
        raise _Failure(exc.code, str(exc)) from exc


def _check_root(run: _Run) -> None:
    """Compare the root repository against the snapshot the server took before dispatch.

    A task with no repository behind it -- a consult in a scratch repository -- has no root to
    check and is not reported as one that skipped the check.
    """
    if not run.task.repository_id:
        return
    before = _load_root_snapshot(run)
    root = _root_identity(run)
    if before is None or root is None:
        # The check did not run. That is not the same as the check passing, and the only place it
        # can be said so is on the task itself.
        run.warn(
            ROOT_CHECK_SKIPPED,
            EventKind.WARNING,
            {"code": ROOT_CHECK_SKIPPED, "revision": run.revision},
        )
        return
    try:
        after = repos.snapshot_root(root.toplevel)
    except GitError as exc:  # pragma: no cover - the root repository was resolved just above
        run.log.write(f"root snapshot failed: {exc.code}")
        return
    changed = repos.compare_snapshots(before, after)
    if changed:
        run.warn(
            f"ROOT_MUTATION:{','.join(changed)}",
            EventKind.ROOT_MUTATION,
            {"paths": changed, "repository": str(root.toplevel)},
        )


def _root_identity(run: _Run) -> RepositoryIdentity | None:
    if not run.task.repository_id:
        return None
    row = run.store.get_repository(run.task.repository_id)
    if row is None:
        return None
    display = row["display_path"] or str(Path(row["common_dir"]).parent)
    try:
        return repos.resolve_repository(Path(display))
    except GitError:
        return None


def _load_root_snapshot(run: _Run) -> RootSnapshot | None:
    """Read the ``root_snapshot`` artifact, from this turn's revision or the task's first."""
    for revision in dict.fromkeys((run.revision, 0)):
        row = run.store.get_artifact(run.task_id, revision, "root_snapshot")
        if row is None or not row["path"]:
            continue
        try:
            data = json.loads(Path(row["path"]).read_text(encoding="utf-8"))
            return RootSnapshot(
                head=data["head"],
                branch=data.get("branch"),
                dirty=dict(data.get("dirty") or {}),
            )
        except (OSError, ValueError, KeyError, TypeError):
            run.log.write(f"root snapshot artifact at revision {revision} is unreadable")
    return None


# -- recording ------------------------------------------------------------------------------


def _settle_cancelled(run: _Run) -> TaskState:
    """CANCELLED is only ever reached through CANCELLING, whoever asked for it."""
    current = run.store.get_task(run.task_id)
    if current is not None and current.state is TaskState.RUNNING:
        run.task = transition(
            run.store, run.task_id, TaskState.CANCELLING, reason="worker cancelled its turn"
        )
    response = run.result.text if run.result else None
    return _settle(run, TaskState.CANCELLED, "cancelled", response=response)


def _settle(run: _Run, state: TaskState, reason: str, **fields: Any) -> TaskState:
    """Write the transcript, then move the task to its final state for this turn."""
    transcript = _write_transcript(run)
    if transcript is not None:
        fields["transcript_path"] = str(transcript)
    if run.result is not None:
        fields.setdefault("response", run.result.text)
    fields["warnings"] = run.warnings or None
    if run.reported_model:
        fields.setdefault("reported_model", run.reported_model)

    current = run.store.get_task(run.task_id)
    if current is not None and current.state is TaskState.CANCELLING and state not in (
        TaskState.CANCELLED,
        TaskState.FAILED,
    ):
        # A cancel landed while the turn was finishing. Keep everything the turn produced and
        # settle where the state machine allows, rather than dropping the outcome.
        run.log.write(f"cancel overtook {state.value}")
        state = TaskState.CANCELLED
        reason = "cancelled while finishing"
    if state in TERMINAL_STATES:
        fields["finished_at"] = now()
    run.task = transition(run.store, run.task_id, state, reason=reason, **fields)
    run.log.write(f"settled {state.value} ({reason})")
    return state


def _write_transcript(run: _Run) -> Path | None:
    """Persist what the turn produced, without the prompt or any environment."""
    if run.result is None:
        return None
    capture = run.result.capture
    payload = {
        "task_id": run.task_id,
        "revision": run.revision,
        "kind": run.kind.value,
        "session_id": run.session_id,
        "stop_reason": run.result.stop_reason,
        "text": run.result.text,
        "thoughts": capture.thoughts,
        "tool_calls": capture.tool_calls,
        "permission_events": capture.permission_events,
        "violations": capture.violations,
        "update_count": capture.raw_update_count,
    }
    path = run.dir / f"turn-{run.revision}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _complete_turn(run: _Run) -> None:
    """Close the turn row the server opened, whatever happened to the task."""
    if run.turn_completed:
        return
    run.turn_completed = True
    profile = run.profile
    with contextlib.suppress(StoreError):
        run.store.complete_turn(
            run.turn_id,
            session_id=run.session_id,
            stop_reason=(run.result.stop_reason if run.result else None),
            response=(run.result.text if run.result else None),
            attribution={
                "provider": run.task.provider,
                "profile": profile.id if profile else None,
                "provider_family": profile.family if profile else None,
                "model": profile.model if profile else None,
                "resolved_model": run.task.resolved_model,
                "resolved_effort": run.task.resolved_effort,
                "reported_model": run.reported_model,
                "auth": profile.auth if profile else None,
                "gateway_host": profile.gateway_host if profile else None,
                "agent": run.agent_info or None,
            },
        )
    if run.usage is not None:
        with contextlib.suppress(StoreError):
            run.store.insert_turn_usage(
                run.turn_id, run.task_id, run.task.provider, **run.usage.as_fields()
            )


# -- entry point ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """``python -m taskspindle.runner --task <task_id>``."""
    parser = argparse.ArgumentParser(prog="taskspindle-runner")
    parser.add_argument("--task", required=True, help="the task id to run one turn of")
    args = parser.parse_args(argv)

    resolved = default_paths()
    raw_config = os.environ.get("TASKSPINDLE_CONFIG")
    config_file = Path(raw_config) if raw_config else resolved.config_file
    store: Store | None = None
    try:
        settings = load_config(config_file)
        store = Store.open(resolved.state_dir / "taskspindle.sqlite3")
        profiles = providers.load_profiles(
            settings,
            runtime_dir=resolved.runtime_dir,
            home=Path(os.environ.get("HOME", "")),
            state_dir=resolved.state_dir,
            data_dir=resolved.data_dir,
        )
        state = asyncio.run(
            run_worker(store, args.task, profiles=profiles, paths=resolved)
        )
    except Exception as exc:
        # The class name, and a TaskSpindleError's code: enough to tell a config mistake from a
        # crash, with none of the message text, which can carry paths or credentials.
        code = f" {exc.code}" if isinstance(exc, TaskSpindleError) else ""
        print(f"taskspindle-runner: {type(exc).__name__}{code}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
    return 0 if state in TERMINAL_STATES or state is TaskState.RESULT_READY else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
