"""Give back disk that finished tasks and old logs no longer need.

``taskspindle gc`` is the operator's lever. It only ever touches two kinds of thing:

- ``state_dir/tasks/<id>/tmp``, the worker's ``TMPDIR``, for a task in a terminal state
  (COMPLETED, FAILED, CANCELLED, REJECTED, ACCEPTED) that holds no slot lease. Everything else
  in the task directory -- ``turn-N.json``, ``rev-N.diff``, ``worker.log`` -- is evidence and
  stays. A task that is still active, or that the database does not know, is refused and
  reported, never touched.
- the operational and traceback logs, through :func:`taskspindle.oplog.rotate`.

Worktrees belong to ``cleanup_task``; containers to the Docker backend. Neither is touched here.

It is a dry run unless ``apply`` is set. Paths come from the state directory alone, so the CLI
behaves the same on the host and inside the runtime container, which mounts the state directory
at the same absolute path.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import Any

from . import oplog
from .models import TERMINAL_STATES
from .store import Store

__all__ = ["collect", "remove_tree", "tree_size"]


def tree_size(path: Path) -> int:
    """Bytes held by the files under ``path``, without following symlinks. Never raises."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            info = current.lstat()
        except OSError:
            continue
        if current.is_dir() and not current.is_symlink():
            try:
                stack.extend(current.iterdir())
            except OSError:
                continue
        else:
            total += info.st_size
    return total


def remove_tree(path: Path) -> None:
    """Remove ``path``: unlink a symlink rather than follow it, and never raise."""
    if path.is_symlink():
        with contextlib.suppress(OSError):
            path.unlink()
        return
    shutil.rmtree(path, ignore_errors=True)


def _verdict(store: Store, task_id: str, leased: set[str]) -> str | None:
    """``None`` when ``task_id``'s scratch may go, otherwise why it may not."""
    record = store.get_task(task_id)
    if record is None:
        return "unknown task"
    if record.state not in TERMINAL_STATES:
        return f"task is {record.state.value}"
    if task_id in leased:
        return "task holds a slot lease"
    return None


def _leased(store: Store) -> set[str]:
    return {str(lease["task_id"]) for lease in store.list_leases()}


def collect(state_dir: Path, store: Store, *, apply: bool) -> dict[str, Any]:
    """Find (and with ``apply``, remove) reclaimable scratch and logs; report what happened.

    Before a directory is removed its task is read again and the directory is renamed aside,
    so a continuation that starts in between gets a fresh ``tmp`` rather than a half-deleted one.
    """
    tasks_dir = Path(state_dir) / "tasks"
    leased = _leased(store)
    candidates: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    entries = sorted(tasks_dir.iterdir()) if tasks_dir.is_dir() else []
    for entry in entries:
        scratch = entry / "tmp"
        if not entry.is_dir() or entry.is_symlink() or not (scratch.exists() or scratch.is_symlink()):
            continue
        item = {"task_id": entry.name, "path": str(scratch), "bytes": tree_size(scratch)}
        reason = _verdict(store, entry.name, leased)
        if reason is None:
            candidates.append(item)
        else:
            refused.append({**item, "reason": reason})

    removed: list[dict[str, Any]] = []
    if apply:
        for item in candidates:
            scratch = Path(item["path"])
            reason = _verdict(store, item["task_id"], _leased(store))
            if reason is not None:
                refused.append({**item, "reason": reason})
                continue
            aside = scratch.with_name(f"tmp.gc-{os.getpid()}")
            try:
                os.replace(scratch, aside)
            except OSError:
                refused.append({**item, "reason": "could not move aside"})
                continue
            if _verdict(store, item["task_id"], _leased(store)) is not None:
                # A continuation started in the gap: hand the directory back untouched.
                with contextlib.suppress(OSError):
                    if not scratch.exists():
                        os.replace(aside, scratch)
                refused.append({**item, "reason": "task became active"})
                continue
            remove_tree(aside)
            if aside.exists() or aside.is_symlink():
                refused.append({**item, "reason": "could not be removed"})
            else:
                removed.append(item)
        tmp_listed = removed
    else:
        tmp_listed = candidates

    logs = oplog.rotate(Path(state_dir), apply=apply)
    reclaimed = sum(item["bytes"] for item in tmp_listed) + sum(item["bytes"] for item in logs)
    report = {
        "applied": apply,
        "tmp": tmp_listed,
        "refused": refused,
        "logs": logs,
        "reclaimed_bytes": reclaimed,
    }
    if apply:
        oplog.emit(
            state_dir, "cli", "gc_applied",
            removed=len(removed), refused=len(refused), log_actions=len(logs),
            reclaimed_bytes=reclaimed,
        )
    return report
