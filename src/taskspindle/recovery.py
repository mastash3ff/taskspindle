"""Reconciling what the store believes against what systemd can still see.

The store is written by workers that can be killed at any moment, so after a server restart -- or
a reboot -- some tasks claim to be running when nothing is. :func:`reconcile` asks systemd about
each of them and settles the difference, using the unit's own post-mortem state where it exists
and the worker's heartbeat where it does not.

Two rules shape every decision here. Work is never thrown away: a task whose worker vanished
becomes INTERRUPTED, which keeps its worktree, its session and its candidate, rather than FAILED.
And ambiguity is never resolved by guessing: a task whose unit systemd has forgotten but whose
heartbeat is fresh becomes RECOVERY_AMBIGUOUS and waits for a person.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import service
from .models import ACTIVE_STATES, TERMINAL_STATES, EventKind, TaskRecord, TaskState
from .service import TaskSpindleError
from .store import Store
from .store import now as _stamp
from .units import UnitBackend, UnitError, UnitState, accept_unit_name, worker_unit_name

__all__ = [
    "NEVER_STARTED_AFTER_S",
    "ReconcileAction",
    "reconcile",
]

#: How long a task may sit in PREPARING or QUEUED without a unit before it is declared dead.
NEVER_STARTED_AFTER_S = 600

#: Most tasks ever examined in one active state; a backlog larger than this is not a thing
#: reconciliation should be quietly hiding.
_SCAN_LIMIT = 1000

_BOOT_CHANGED = "boot_changed"
_EXITED_WITHOUT_RECORD = "worker_exited_without_record"
_OOM_KILLED = "oom_killed"
_SIGNALLED = "signalled"
_WORKER_EXITED = "worker_exited"
_MISSING_STALE = "unit_missing_stale_heartbeat"
_MISSING_FRESH = "unit_missing_fresh_heartbeat"
_NEVER_STARTED = "never_started"
_NO_LEGAL_TARGET = "no_legal_target"
_RECONCILE_FAILED = "reconcile_failed"

#: unit kind -> (reason, state to move to). ``active`` and the heartbeat-dependent kinds are
#: handled separately.
_BY_KIND: dict[str, tuple[str, TaskState]] = {
    "success": (_EXITED_WITHOUT_RECORD, TaskState.INTERRUPTED),
    "oom": (_OOM_KILLED, TaskState.INTERRUPTED),
    "signal": (_SIGNALLED, TaskState.INTERRUPTED),
    "exit": (_WORKER_EXITED, TaskState.FAILED),
}

#: Kinds whose unit is dead and should be forgotten once its state has been read.
_RESET_KINDS = frozenset(_BY_KIND)


@dataclass(frozen=True)
class ReconcileAction:
    """One decision reconciliation made, for the caller to log or report."""

    task_id: str
    from_state: str
    to_state: str | None
    reason: str


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _older_than(stamp: str | None, moment: datetime, seconds: int) -> bool:
    """True when ``stamp`` is missing or older than ``seconds`` before ``moment``."""
    parsed = _parse(stamp)
    if parsed is None:
        return True
    return parsed < moment - timedelta(seconds=seconds)


def _unit_for(task: TaskRecord) -> str:
    if task.state is TaskState.ACCEPTING:
        return task.unit_name or accept_unit_name(task.id)
    return task.unit_name or worker_unit_name(task.id)


def reconcile(
    store: Store,
    backend: UnitBackend,
    *,
    boot: str,
    now: datetime,
    stale_after_s: int = 30,
    accept_recover: Callable[[TaskRecord], str] | None = None,
) -> list[ReconcileAction]:
    """Settle every task that claims to be active but may no longer be.

    ``accept_recover`` is the server's closure around
    :func:`taskspindle.integration.recover_journal`; without it an interrupted accept is left to
    the ordinary unit rules, which never touch the repository.

    One task can never end the sweep: a systemd hiccup, or a task that moved underneath us while
    a worker finalised, is recorded as an action with no target state and the next task is
    examined.
    """
    moment = _aware(now)
    actions: list[ReconcileAction] = []
    for state in sorted(ACTIVE_STATES):
        for task in store.list_tasks(state=state.value, limit=_SCAN_LIMIT):
            try:
                action = _reconcile_task(
                    store,
                    backend,
                    task,
                    boot=boot,
                    moment=moment,
                    stale_after_s=stale_after_s,
                    accept_recover=accept_recover,
                )
            except (TaskSpindleError, UnitError) as exc:
                action = _strand(store, task, f"{_RECONCILE_FAILED}:{exc.code}")
            if action is not None:
                actions.append(action)
    return actions


def _reconcile_task(
    store: Store,
    backend: UnitBackend,
    task: TaskRecord,
    *,
    boot: str,
    moment: datetime,
    stale_after_s: int,
    accept_recover: Callable[[TaskRecord], str] | None,
) -> ReconcileAction | None:
    unit_state: UnitState | None = None
    if task.boot_id and task.boot_id != boot:
        reason, target = _BOOT_CHANGED, TaskState.INTERRUPTED
    elif not task.unit_name and task.state in (TaskState.PREPARING, TaskState.QUEUED):
        # Nothing was ever started: the only question is whether it ever will be.
        if _older_than(task.created_at, moment, NEVER_STARTED_AFTER_S):
            return _apply(store, task, TaskState.FAILED, _NEVER_STARTED, error=_never_started())
        return None
    else:
        unit_state = backend.show(_unit_for(task))
        outcome = _from_unit(unit_state, task, moment=moment, stale_after_s=stale_after_s)
        if outcome is None:
            return None
        reason, target = outcome

    if unit_state is not None and unit_state.kind in _RESET_KINDS:
        backend.reset_failed(_unit_for(task))

    if task.state is TaskState.ACCEPTING and accept_recover is not None:
        return _recover_accept(store, task, reason, accept_recover)
    if task.state is TaskState.CANCELLING:
        # The cancel got what it asked for: the unit is gone.
        return _apply(store, task, TaskState.CANCELLED, reason)
    permitted = _permitted(task.state, target)
    if permitted is None:
        if task.state is TaskState.PREPARING:
            # PREPARING holds nothing worth retaining: no session, no worktree, no candidate.
            return _apply(store, task, TaskState.FAILED, _NEVER_STARTED, error=_never_started())
        return _strand(store, task, f"{_NO_LEGAL_TARGET}:{reason}")
    target = permitted
    if target is TaskState.FAILED:
        status = unit_state.exec_main_status if unit_state else None
        return _apply(
            store,
            task,
            TaskState.FAILED,
            reason,
            error={
                "code": "WORKER_EXITED",
                "message": f"exit status {status}",
                "retryable": True,
                "details": {},
            },
        )
    return _apply(store, task, target, reason)


def _never_started() -> dict[str, Any]:
    return {
        "code": "NEVER_STARTED",
        "message": "no unit was started for this task",
        "retryable": False,
        "details": {},
    }


def _strand(store: Store, task: TaskRecord, reason: str) -> ReconcileAction:
    """Record that a task could not be settled, without moving it.

    The task is left exactly where it was -- reconciliation has nothing safe to do with it -- but
    an operator has to be able to see that it was looked at and skipped, so the reason is logged
    once. A sweep that keeps finding the same task in the same condition stays quiet.
    """
    if _last_recovery_reason(store, task.id) != reason:
        store.append_event(
            task.id,
            EventKind.RECOVERY,
            {"reason": reason, "from": task.state.value, "to": None},
        )
    return ReconcileAction(
        task_id=task.id, from_state=task.state.value, to_state=None, reason=reason
    )


def _last_recovery_reason(store: Store, task_id: str) -> str | None:
    for event in reversed(store.list_events(task_id)):
        if event["kind"] == EventKind.RECOVERY.value:
            reason = (event["payload"] or {}).get("reason")
            return str(reason) if reason is not None else None
    return None


def _permitted(from_state: TaskState, target: TaskState) -> TaskState | None:
    """Keep reconciliation inside the state machine, preferring to retain work.

    A state the machine forbids is not forced: an ACCEPTING task, for instance, has no path to
    FAILED, so an accept unit that exited becomes INTERRUPTED and keeps its candidate.
    """
    allowed = service.LEGAL_TRANSITIONS.get(from_state, frozenset())
    if target in allowed:
        return target
    if TaskState.INTERRUPTED in allowed:
        return TaskState.INTERRUPTED
    return None


def _from_unit(
    unit_state: UnitState,
    task: TaskRecord,
    *,
    moment: datetime,
    stale_after_s: int,
) -> tuple[str, TaskState] | None:
    """Map a unit's post-mortem state to a reason and a target state, or None to do nothing."""
    kind = unit_state.kind
    if kind == "active":
        return None
    mapped = _BY_KIND.get(kind)
    if mapped is not None:
        return mapped
    # not_found, or a state systemd reports that we have no rule for: the heartbeat decides.
    if _older_than(task.heartbeat_at, moment, stale_after_s):
        return _MISSING_STALE, TaskState.INTERRUPTED
    return _MISSING_FRESH, TaskState.RECOVERY_AMBIGUOUS


def _recover_accept(
    store: Store,
    task: TaskRecord,
    reason: str,
    accept_recover: Callable[[TaskRecord], str],
) -> ReconcileAction | None:
    """Let the integration journal decide what an interrupted accept actually did."""
    outcome = accept_recover(task)
    if outcome == "committed":
        return _apply(store, task, TaskState.ACCEPTED, f"accept_committed:{reason}")
    warning = f"ACCEPT_FAILED:{reason}"
    warnings = list(task.warnings or [])
    if warning not in warnings:
        warnings.append(warning)
    store.append_event(
        task.id,
        EventKind.ACCEPT_FAILED,
        {"reason": reason, "candidate_sha": task.candidate_sha},
    )
    return _apply(
        store,
        task,
        TaskState.RESULT_READY,
        f"accept_aborted:{reason}",
        warnings=warnings,
    )


def _apply(
    store: Store,
    task: TaskRecord,
    to_state: TaskState,
    reason: str,
    **fields: Any,
) -> ReconcileAction:
    """Move the task, log a RECOVERY event, and drop the provider lease it was holding."""
    if to_state in TERMINAL_STATES:
        fields.setdefault("finished_at", _stamp())
    service.transition(store, task.id, to_state, reason=f"recovery: {reason}", **fields)
    store.append_event(
        task.id,
        EventKind.RECOVERY,
        {"reason": reason, "from": task.state.value, "to": to_state.value},
    )
    lease = store.get_lease(task.provider)
    if lease is not None and lease["task_id"] == task.id:
        store.release_lease(task.provider, task.id)
    return ReconcileAction(
        task_id=task.id,
        from_state=task.state.value,
        to_state=to_state.value,
        reason=reason,
    )
