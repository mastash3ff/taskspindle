"""The acceptance unit: the one process allowed to change the root repository.

It runs detached, exactly like a worker, and for the same reason -- the commit it makes belongs to
the operator, and it must survive the MCP server exiting halfway through. Every step is journalled
before it happens, so an accept that is killed can be recognised on the next reconcile and undone.

Nothing here decides whether a candidate *should* land: the orchestrator's gates already did that.
This module only finds out whether it *can*, and says so. A candidate that does not apply, dirties
the target, or fails its checks in the root goes back to RESULT_READY with the reason recorded as a
warning, and the repository is left byte-for-byte where it started.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import integration, repos
from .config import Paths, load_config
from .config import paths as default_paths
from .integration import Journal
from .models import CheckRecord, EventKind, TaskRecord, TaskState
from .service import ACCEPT_FAILED, INVALID_REQUEST, TaskSpindleError, transition
from .store import Store, now

__all__ = ["ROOT_CHECK_PREFIX", "main", "run_accept"]

#: Marks a check that ran in the root repository rather than in the task's worktree.
ROOT_CHECK_PREFIX = "[root] "

#: The environment root verification runs under. Nothing else is inherited: a check that needs a
#: credential is a check that does not belong in an automated acceptance.
_CHECK_ENV_NAMES = ("HOME", "PATH", "LANG")

#: Root verification never runs longer than this, whatever the task's own timeout is.
MAX_VERIFICATION_S = 1800


def check_env(parent: Mapping[str, str]) -> dict[str, str]:
    """The minimal environment the root verification commands see."""
    env = {name: parent[name] for name in _CHECK_ENV_NAMES if name in parent}
    env["TERM"] = "dumb"
    env["CI"] = "1"
    return env


def run_accept(
    store: Store,
    task_id: str,
    *,
    paths: Paths,
    parent_env: Mapping[str, str],
) -> str:
    """Apply one accepted candidate to the root repository.

    Returns ``"accepted"``, ``"conflict"`` or ``"failed"``; ``paths`` is accepted for symmetry
    with the worker and so that a future artifact has somewhere to go.
    """
    task = store.get_task(task_id)
    if task is None:
        raise TaskSpindleError("TASK_NOT_FOUND", f"no such task: {task_id}")
    if task.state is not TaskState.ACCEPTING:
        raise TaskSpindleError(
            "ILLEGAL_TRANSITION",
            f"task {task_id} is {task.state.value}, not ACCEPTING",
            details={"task_id": task_id, "from": task.state.value},
        )
    stored = store.read_journal(task_id)
    if stored is None:
        raise TaskSpindleError(
            INVALID_REQUEST,
            f"task {task_id} is ACCEPTING without an integration journal",
            details={"task_id": task_id},
        )

    identity = _identity(store, task)
    journal = Journal(
        task_id=task_id,
        phase=str(stored["phase"]),
        target_head=str(stored["target_head"] or ""),
        candidate_sha=str(stored["candidate_sha"] or task.candidate_sha or ""),
    )

    probe = integration.probe_merge(
        identity,
        base_sha=task.base_head or "",
        target_sha=journal.target_head,
        candidate_sha=journal.candidate_sha,
    )
    if not probe.clean:
        return _conflict(store, task, list(probe.conflicts))

    def save(updated: Journal) -> None:
        store.write_journal(
            task_id,
            updated.phase,
            target_head=updated.target_head,
            candidate_sha=updated.candidate_sha,
        )

    try:
        integration.stage_candidate(identity, journal=journal, save=save)
    except repos.GitError as exc:
        if exc.code == "MERGE_CONFLICT":
            return _conflict(store, task, list(exc.paths))
        store.clear_journal(task_id)
        return _abandon(store, task, exc.code, {"message": str(exc)})

    staged = replace(journal, phase="staged")
    results = integration.run_verification(
        identity.toplevel,
        list(task.verification_commands or []),
        timeout_s=min(task.timeout_s, MAX_VERIFICATION_S),
        env=check_env(parent_env),
    )
    for result in results:
        store.insert_check(
            task_id,
            task.candidate_revision,
            CheckRecord(
                command=f"{ROOT_CHECK_PREFIX}{result.command}",
                exit_code=result.exit_code,
                ok=result.ok,
                duration_ms=result.duration_ms,
                stdout_tail=result.stdout_tail,
                stderr_tail=result.stderr_tail,
            ),
        )
    failed = [result.command for result in results if not result.ok]
    if failed:
        integration.abort_staged(identity, journal=staged)
        store.clear_journal(task_id)
        return _abandon(store, task, "CHECKS_FAILED", {"commands": failed})

    head = integration.commit_staged(
        identity,
        journal=staged,
        save=save,
        message=_commit_message(store, task),
    )
    transition(
        store,
        task_id,
        TaskState.ACCEPTED,
        reason="candidate committed to the target",
        target_head=head,
        finished_at=now(),
    )
    store.clear_journal(task_id)
    return "accepted"


def _identity(store: Store, task: TaskRecord) -> repos.RepositoryIdentity:
    """Resolve the repository the candidate is being applied to."""
    row = store.get_repository(task.repository_id or "")
    if row is None:
        raise TaskSpindleError(
            INVALID_REQUEST,
            f"task {task.id} has no repository to accept into",
            details={"task_id": task.id, "repository_id": task.repository_id},
        )
    display = row["display_path"] or str(Path(row["common_dir"]).parent)
    return repos.resolve_repository(Path(display))


def _commit_message(store: Store, task: TaskRecord) -> str:
    """The message the accepting session signed for, read back from its own request."""
    for event in reversed(store.list_events(task.id)):
        if event["kind"] == EventKind.ACCEPT_REQUESTED.value:
            message = (event["payload"] or {}).get("commit_message")
            if message:
                return str(message)
    return task.candidate_message or f"Accept candidate {task.candidate_sha}"


def _conflict(store: Store, task: TaskRecord, conflicts: list[str]) -> str:
    """The candidate does not apply. Nothing was staged, so nothing has to be undone."""
    store.clear_journal(task.id)
    warning = f"CONFLICT:{','.join(conflicts)}"
    store.append_event(
        task.id,
        EventKind.ACCEPT_FAILED,
        {"reason": "CONFLICT", "paths": conflicts, "candidate_sha": task.candidate_sha},
    )
    _back_to_result_ready(store, task, warning, "the candidate does not apply to the target")
    return "conflict"


def _abandon(store: Store, task: TaskRecord, code: str, payload: dict[str, Any]) -> str:
    """The apply was undone; record why and give the candidate back to its owner."""
    store.append_event(
        task.id,
        EventKind.ACCEPT_FAILED,
        {"reason": code, "candidate_sha": task.candidate_sha, **payload},
    )
    _back_to_result_ready(store, task, f"{ACCEPT_FAILED}:{code}", f"accept failed: {code}")
    return "failed"


def _back_to_result_ready(store: Store, task: TaskRecord, warning: str, reason: str) -> None:
    warnings = list(task.warnings or [])
    if warning not in warnings:
        warnings.append(warning)
    transition(store, task.id, TaskState.RESULT_READY, reason=reason, warnings=warnings)


def main(argv: list[str] | None = None) -> int:
    """``python -m taskspindle.accept --task <task_id>``."""
    parser = argparse.ArgumentParser(prog="taskspindle-accept")
    parser.add_argument("--task", required=True, help="the task id to accept")
    args = parser.parse_args(argv)

    resolved = default_paths()
    raw_config = os.environ.get("TASKSPINDLE_CONFIG")
    config_file = Path(raw_config) if raw_config else resolved.config_file
    store: Store | None = None
    try:
        load_config(config_file)
        store = Store.open(resolved.state_dir / "taskspindle.sqlite3")
        outcome = run_accept(store, args.task, paths=resolved, parent_env=os.environ)
    except Exception as exc:
        # The message may quote paths and commands, so only the shape of the failure is printed;
        # the task's own event log carries the rest.
        code = f": {exc.code}" if isinstance(exc, TaskSpindleError) else ""
        print(f"accept of {args.task} failed: {type(exc).__name__}{code}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
    return 0 if outcome == "accepted" else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
