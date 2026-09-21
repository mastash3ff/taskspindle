"""Start queued work as soon as a slot frees, instead of on the next tool call.

``Orchestrator.dispatch_queued`` only ever ran inside a tool call, so a slot a worker had just
given back sat empty until the coordinator next spoke to the server. :func:`drain_once` is the
same dispatch, run by whichever process can start workers: the controller under the Docker
backend, and the exiting worker under the systemd one.

It invents nothing. It starts tasks the coordinator already queued, under the same leases,
limits, provider availability and admission fence as any other dispatch, and an idle pool is
left alone: the cheap check below is a read-only query, and only a queued task with a free slot
leads to a reconcile and dispatch.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import policy
from .config import (
    ConfigError,
    Paths,
    capacity_limits,
    concurrency_limits,
    dispatch_config,
    load_config,
)
from .config import paths as default_paths
from .models import TaskState

__all__ = ["DrainResult", "drain_once", "startable"]

_DISPATCHABLE = (TaskState.QUEUED.value, TaskState.REPAIRING.value, TaskState.RESUMING.value)


@dataclass(frozen=True)
class DrainResult:
    """``ran`` is whether a dispatch pass happened at all; ``started`` is what it started."""

    ran: bool = False
    started: list[str] = field(default_factory=list)
    error: str | None = None


def _resolve(paths: Paths | None, env: Mapping[str, str]) -> Paths:
    resolved = paths or default_paths(env)
    raw = env.get("TASKSPINDLE_CONFIG")
    if not raw:
        return resolved
    return Paths(
        config_file=Path(raw), state_dir=resolved.state_dir,
        data_dir=resolved.data_dir, runtime_dir=resolved.runtime_dir,
    )


def startable(database: Path, settings: Mapping[str, Any]) -> bool:
    """Whether some queued task's provider has a free slot, from a read-only look at the database.

    This is deliberately less than a dispatch decides: it knows nothing of provider throttles
    or the admission fence. It only has to be cheap and never wrong in the quiet direction, so a
    drain loop costs an idle pool one small query and nothing else.
    """
    if not database.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        marks = ",".join("?" * len(_DISPATCHABLE))
        queued = {
            row[0] for row in conn.execute(
                f"SELECT DISTINCT t.provider FROM tasks t WHERE t.state IN ({marks}) "
                "AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.task_id = t.id)",
                _DISPATCHABLE,
            )
        }
        if not queued:
            return False
        active = {row[0]: int(row[1]) for row in conn.execute(
            "SELECT provider, COUNT(*) FROM leases GROUP BY provider"
        )}
        document = None
        with contextlib.suppress(sqlite3.Error):
            row = conn.execute("SELECT document FROM dispatch_policy WHERE id = 1").fetchone()
            document = row[0] if row else None
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    try:
        current = policy.parse(json.loads(document)) if document else policy.DispatchPolicy()
    except policy.PolicyError:
        current = policy.DispatchPolicy()
    capacity = capacity_limits(settings)
    limits = policy.effective_limits(
        current, concurrency_limits(settings, queued | set(settings.get("concurrency", {}))),
        per_provider_max=capacity.per_provider_max, total_max=capacity.total_max,
    )
    total = limits["total"]["limit"]
    if total is not None and sum(active.values()) >= total:
        return False
    return any(active.get(name, 0) < limits["providers"][name]["limit"] for name in queued)


def _log(state_dir: Path, exc: BaseException) -> None:
    """The class name and traceback, to the state directory; a log failure is never raised."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (state_dir / "dispatch-errors.log").open("a", encoding="utf-8") as handle:
            handle.write(f"--- drain: {type(exc).__name__}\n")
            handle.write("".join(traceback.format_exception(exc)))
    except OSError:  # pragma: no cover - the log is best effort by design
        pass


def drain_once(
    *, paths: Paths | None = None, parent_env: Mapping[str, str] | None = None,
) -> DrainResult:
    """One reconcile-and-dispatch pass when something queued could start. Never raises.

    The pass builds its own orchestrator and store, so it is safe from any thread or process,
    and its unit backend is the configured one: under Docker that is a client of the controller
    socket, which keeps the admission fence and the controller's mutation lock in force.
    """
    env = dict(parent_env if parent_env is not None else os.environ)
    resolved = _resolve(paths, env)
    try:
        settings = load_config(resolved.config_file)
        if not dispatch_config(settings).drain:
            return DrainResult()
        if not startable(resolved.state_dir / "taskspindle.sqlite3", settings):
            return DrainResult()
    except ConfigError as exc:
        return DrainResult(error=type(exc).__name__)
    try:
        from .server import build_orchestrator

        orchestrator, store = build_orchestrator(paths=resolved, parent_env=env)
    except Exception as exc:
        _log(resolved.state_dir, exc)
        return DrainResult(error=type(exc).__name__)
    try:
        orchestrator.reconcile()
        return DrainResult(ran=True, started=orchestrator.dispatch_queued())
    except Exception as exc:
        _log(resolved.state_dir, exc)
        return DrainResult(ran=True, error=type(exc).__name__)
    finally:
        store.close()
