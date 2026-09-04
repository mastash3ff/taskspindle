"""Reconciling tasks that claim to be active against what systemd still knows."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskspindle import service
from taskspindle.models import AuthMode, EventKind, Mode, StartTaskRequest, TaskRecord, TaskState
from taskspindle.recovery import AcceptOutcome, ReconcileAction, reconcile
from taskspindle.store import Store
from taskspindle.units import UnitError, UnitState, accept_unit_name, worker_unit_name
from tests.fakes.units import ACTIVE, EXITED, NOT_FOUND, OOM, SIGNALLED, SUCCESS, FakeUnitBackend

BOOT = "boot-now"
PROVIDER = "claude"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store.open(tmp_path / "taskspindle.sqlite3")
    yield store
    store.close()


def stamp(offset_s: float) -> str:
    return (NOW + timedelta(seconds=offset_s)).isoformat().replace("+00:00", "Z")


def make_task(
    store: Store,
    *,
    state: TaskState,
    mode: Mode = Mode.IMPLEMENT,
    unit: str | None = "worker",
    boot_id: str | None = BOOT,
    heartbeat_offset_s: float = -1,
    created_offset_s: float = -60,
    lease: bool = True,
) -> TaskRecord:
    """A task sitting in an active state, with the lease a live worker would be holding."""
    fields: dict[str, object] = {"provider": PROVIDER, "mode": mode, "prompt": "do the thing"}
    if mode is Mode.IMPLEMENT:
        fields.update(
            repository="/repo",
            acceptance_criteria="it works",
            path_prefixes=["src"],
            verification_commands=[],
            candidate_message="Add the thing",
        )
    record = service.create_task(
        store, StartTaskRequest(**fields), repository_id=None, auth_mode=AuthMode.OAUTH
    )
    names = {"worker": worker_unit_name(record.id), "accept": accept_unit_name(record.id)}
    unit_name = names.get(unit or "")
    task = store.update_task(
        record.id,
        None,
        state=state,
        unit_name=unit_name,
        boot_id=boot_id,
        heartbeat_at=stamp(heartbeat_offset_s),
        created_at=stamp(created_offset_s),
        candidate_sha="c0ffeeba" if state is TaskState.ACCEPTING else None,
    )
    if lease:
        store.acquire_lease(PROVIDER, record.id, unit_name or "", 4242, BOOT)
    return task


def run(store: Store, backend: FakeUnitBackend, **kwargs: object) -> list[ReconcileAction]:
    return reconcile(store, backend, boot=BOOT, now=NOW, **kwargs)


def recovery_reasons(store: Store, task_id: str) -> list[str]:
    return [
        event["payload"]["reason"]
        for event in store.list_events(task_id)
        if event["kind"] == EventKind.RECOVERY.value
    ]


def assert_settled(
    store: Store, task: TaskRecord, actions: list[ReconcileAction], state: TaskState, reason: str
) -> None:
    assert [(action.to_state, action.reason) for action in actions] == [(state.value, reason)]
    assert store.get_task(task.id).state is state
    assert recovery_reasons(store, task.id) == [reason]
    assert store.get_lease(PROVIDER) is None


def test_a_live_unit_is_left_alone(store: Store) -> None:
    task = make_task(store, state=TaskState.RUNNING)
    backend = FakeUnitBackend({worker_unit_name(task.id): ACTIVE})

    assert run(store, backend) == []
    assert store.get_task(task.id).state is TaskState.RUNNING
    assert store.get_lease(PROVIDER) is not None


def test_a_task_from_an_earlier_boot_is_interrupted(store: Store) -> None:
    task = make_task(store, state=TaskState.RUNNING, boot_id="boot-before")
    backend = FakeUnitBackend({worker_unit_name(task.id): ACTIVE})

    actions = run(store, backend)

    assert_settled(store, task, actions, TaskState.INTERRUPTED, "boot_changed")
    # The unit was never consulted: a reboot settles the question on its own.
    assert backend.reset == []


def test_a_worker_that_exited_without_recording_anything_is_interrupted(store: Store) -> None:
    task = make_task(store, state=TaskState.RUNNING)
    backend = FakeUnitBackend({worker_unit_name(task.id): SUCCESS})

    actions = run(store, backend)

    assert_settled(
        store, task, actions, TaskState.INTERRUPTED, "worker_exited_without_record"
    )
    assert backend.reset == [worker_unit_name(task.id)]


@pytest.mark.parametrize(
    ("unit_state", "reason"),
    [(OOM, "oom_killed"), (SIGNALLED, "signalled")],
)
def test_a_killed_worker_is_interrupted(
    store: Store, unit_state: UnitState, reason: str
) -> None:
    task = make_task(store, state=TaskState.RUNNING)
    backend = FakeUnitBackend({worker_unit_name(task.id): unit_state})

    actions = run(store, backend)

    assert_settled(store, task, actions, TaskState.INTERRUPTED, reason)
    assert backend.reset == [worker_unit_name(task.id)]
    # An interruption never touches what the worker left behind.
    assert store.get_task(task.id).finished_at is None


def test_a_worker_that_exited_nonzero_fails_with_its_status(store: Store) -> None:
    task = make_task(store, state=TaskState.RUNNING)
    backend = FakeUnitBackend({worker_unit_name(task.id): EXITED})

    actions = run(store, backend)

    assert_settled(store, task, actions, TaskState.FAILED, "worker_exited")
    assert store.get_task(task.id).error == {
        "code": "WORKER_EXITED",
        "message": "exit status 2",
        "retryable": True,
        "details": {},
    }


def test_a_missing_unit_with_a_stale_heartbeat_is_interrupted(store: Store) -> None:
    task = make_task(store, state=TaskState.RUNNING, heartbeat_offset_s=-120)
    backend = FakeUnitBackend({worker_unit_name(task.id): NOT_FOUND})

    actions = run(store, backend)

    assert_settled(store, task, actions, TaskState.INTERRUPTED, "unit_missing_stale_heartbeat")


def test_a_missing_unit_with_a_fresh_heartbeat_is_ambiguous_and_settled_once(
    store: Store,
) -> None:
    task = make_task(store, state=TaskState.RUNNING, heartbeat_offset_s=-2)
    backend = FakeUnitBackend({worker_unit_name(task.id): NOT_FOUND})

    actions = run(store, backend)

    assert_settled(
        store, task, actions, TaskState.RECOVERY_AMBIGUOUS, "unit_missing_fresh_heartbeat"
    )
    # RECOVERY_AMBIGUOUS waits for a person; a second pass must not pile on.
    assert run(store, backend) == []


def test_a_cancelling_task_whose_unit_is_gone_is_cancelled(store: Store) -> None:
    task = make_task(store, state=TaskState.CANCELLING)
    backend = FakeUnitBackend({worker_unit_name(task.id): SIGNALLED})

    actions = run(store, backend)

    assert_settled(store, task, actions, TaskState.CANCELLED, "signalled")
    assert store.get_task(task.id).finished_at is not None


def test_a_queued_task_that_was_never_started_fails_after_the_grace_period(
    store: Store,
) -> None:
    fresh = make_task(store, state=TaskState.QUEUED, unit=None, boot_id=None, lease=False)
    stale = make_task(
        store,
        state=TaskState.QUEUED,
        unit=None,
        boot_id=None,
        created_offset_s=-3600,
    )
    backend = FakeUnitBackend()

    actions = run(store, backend)

    assert [action.task_id for action in actions] == [stale.id]
    assert store.get_task(fresh.id).state is TaskState.QUEUED
    assert store.get_task(stale.id).state is TaskState.FAILED
    assert store.get_task(stale.id).error["code"] == "NEVER_STARTED"
    assert store.get_lease(PROVIDER) is None


def test_one_task_that_systemd_cannot_answer_for_does_not_end_the_sweep(store: Store) -> None:
    broken = make_task(store, state=TaskState.RUNNING)
    other = make_task(store, state=TaskState.RUNNING, lease=False)
    broken_unit = worker_unit_name(broken.id)

    def explode(unit: str) -> None:
        if unit == broken_unit:
            raise UnitError("UNIT_QUERY_FAILED", "systemctl show exited 1")

    backend = FakeUnitBackend({worker_unit_name(other.id): SUCCESS}, on_show=explode)

    actions = {action.task_id: action for action in run(store, backend)}

    assert actions[broken.id].to_state is None
    assert actions[broken.id].reason == "reconcile_failed:UNIT_QUERY_FAILED"
    assert store.get_task(broken.id).state is TaskState.RUNNING
    assert recovery_reasons(store, broken.id) == ["reconcile_failed:UNIT_QUERY_FAILED"]
    # The next task was still reconciled.
    assert actions[other.id].to_state == TaskState.INTERRUPTED.value
    assert store.get_task(other.id).state is TaskState.INTERRUPTED

    # A sweep that keeps finding the same broken unit reports it again but only logs it once.
    assert run(store, backend)[0].task_id == broken.id
    assert recovery_reasons(store, broken.id) == ["reconcile_failed:UNIT_QUERY_FAILED"]


def test_a_task_that_finishes_underneath_the_sweep_is_skipped(store: Store) -> None:
    moving = make_task(store, state=TaskState.RUNNING)
    other = make_task(store, state=TaskState.RUNNING, lease=False)
    moving_unit = worker_unit_name(moving.id)

    def finish(unit: str) -> None:
        if unit == moving_unit and store.get_task(moving.id).state is TaskState.RUNNING:
            # The worker recorded its outcome between list_tasks and the transition.
            service.transition(store, moving.id, TaskState.COMPLETED, reason="worker finished")

    backend = FakeUnitBackend(
        {moving_unit: SUCCESS, worker_unit_name(other.id): SUCCESS}, on_show=finish
    )

    actions = {action.task_id: action for action in run(store, backend)}

    assert actions[moving.id].to_state is None
    # The version the sweep read is stale, so the transition is refused before the state machine
    # is even consulted: whoever moved the task owns it.
    assert actions[moving.id].reason == f"reconcile_failed:{service.STALE_STATE_VERSION}"
    assert store.get_task(moving.id).state is TaskState.COMPLETED
    assert store.get_task(other.id).state is TaskState.INTERRUPTED


def test_a_preparing_task_from_an_earlier_boot_fails_rather_than_stranding(store: Store) -> None:
    task = make_task(store, state=TaskState.PREPARING, boot_id="boot-before")
    backend = FakeUnitBackend()

    actions = run(store, backend)

    # PREPARING cannot be INTERRUPTED and has no retained work, so it fails outright.
    assert_settled(store, task, actions, TaskState.FAILED, "never_started")
    assert store.get_task(task.id).error["code"] == "NEVER_STARTED"


def test_a_task_with_no_legal_target_is_reported_once_and_left_alone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = make_task(store, state=TaskState.ACCEPTING, unit="accept", heartbeat_offset_s=-2)
    backend = FakeUnitBackend({accept_unit_name(task.id): NOT_FOUND})
    # An accept that the state machine has no settling move for at all.
    monkeypatch.setitem(
        service.LEGAL_TRANSITIONS, TaskState.ACCEPTING, frozenset({TaskState.ACCEPTED})
    )

    actions = run(store, backend)

    assert [(action.to_state, action.reason) for action in actions] == [
        (None, "no_legal_target:unit_missing_fresh_heartbeat")
    ]
    assert store.get_task(task.id).state is TaskState.ACCEPTING
    assert recovery_reasons(store, task.id) == ["no_legal_target:unit_missing_fresh_heartbeat"]
    # Seen again on the next sweep, but the operator is only told once.
    assert len(run(store, backend)) == 1
    assert recovery_reasons(store, task.id) == ["no_legal_target:unit_missing_fresh_heartbeat"]


def test_an_interrupted_accept_that_committed_is_accepted(store: Store) -> None:
    task = make_task(store, state=TaskState.ACCEPTING, unit="accept")
    backend = FakeUnitBackend({accept_unit_name(task.id): SUCCESS})

    actions = run(store, backend, accept_recover=lambda _task: AcceptOutcome("committed"))

    assert [(action.to_state, action.reason) for action in actions] == [
        (TaskState.ACCEPTED.value, "accept_committed:worker_exited_without_record")
    ]
    assert store.get_task(task.id).state is TaskState.ACCEPTED
    assert store.get_lease(PROVIDER) is None


def test_an_interrupted_accept_that_was_aborted_returns_to_result_ready(store: Store) -> None:
    task = make_task(store, state=TaskState.ACCEPTING, unit="accept")
    backend = FakeUnitBackend({accept_unit_name(task.id): OOM})

    actions = run(store, backend, accept_recover=lambda _task: AcceptOutcome("aborted"))

    assert [action.to_state for action in actions] == [TaskState.RESULT_READY.value]
    settled = store.get_task(task.id)
    assert settled.state is TaskState.RESULT_READY
    assert settled.warnings == ["ACCEPT_FAILED:oom_killed"]
    assert settled.candidate_sha == "c0ffeeba"
    kinds = [event["kind"] for event in store.list_events(task.id)]
    assert EventKind.ACCEPT_FAILED.value in kinds
    assert EventKind.RECOVERY.value in kinds


def test_an_accept_that_could_not_be_settled_waits_for_a_person(store: Store) -> None:
    """A recovery that refused to touch the repository is never reported as an abort."""
    task = make_task(store, state=TaskState.ACCEPTING, unit="accept")
    backend = FakeUnitBackend({accept_unit_name(task.id): OOM})

    actions = run(store, backend, accept_recover=lambda _task: AcceptOutcome("manual"))

    assert [(action.to_state, action.reason) for action in actions] == [
        (TaskState.RECOVERY_AMBIGUOUS.value, "accept_recovery_manual:oom_killed")
    ]
    settled = store.get_task(task.id)
    assert settled.state is TaskState.RECOVERY_AMBIGUOUS
    assert settled.candidate_sha == "c0ffeeba"
    assert EventKind.ACCEPT_FAILED.value not in [
        event["kind"] for event in store.list_events(task.id)
    ]


def test_a_committed_accept_records_the_head_it_landed_at(store: Store) -> None:
    task = make_task(store, state=TaskState.ACCEPTING, unit="accept")
    backend = FakeUnitBackend({accept_unit_name(task.id): SUCCESS})

    run(store, backend, accept_recover=lambda _task: AcceptOutcome("committed", "beefcafe"))

    settled = store.get_task(task.id)
    assert settled.state is TaskState.ACCEPTED
    assert settled.target_head == "beefcafe"


def test_an_ambiguous_accept_unit_is_never_asked_about_its_journal(store: Store) -> None:
    """The accept may still be running: consulting the journal would undo an apply in flight."""
    task = make_task(
        store, state=TaskState.ACCEPTING, unit="accept", heartbeat_offset_s=-2
    )
    backend = FakeUnitBackend({accept_unit_name(task.id): NOT_FOUND})
    asked: list[str] = []

    def recover(record: TaskRecord) -> AcceptOutcome:
        asked.append(record.id)
        return AcceptOutcome("aborted")

    actions = run(store, backend, accept_recover=recover)

    assert asked == []
    assert_settled(
        store, task, actions, TaskState.RECOVERY_AMBIGUOUS, "unit_missing_fresh_heartbeat"
    )


def test_a_cancel_the_worker_ignores_is_escalated_to_a_stop(store: Store) -> None:
    task = make_task(store, state=TaskState.CANCELLING)
    unit = worker_unit_name(task.id)
    backend = FakeUnitBackend({unit: ACTIVE})
    store.append_event(
        task.id, EventKind.CANCEL_REQUESTED, {"from": "RUNNING", "at": stamp(-45)}
    )

    actions = run(store, backend)

    assert [(action.to_state, action.reason) for action in actions] == [
        (None, "cancel_escalated")
    ]
    assert backend.stopped == [unit]
    # The unit's own post-mortem settles the task on a later sweep; nothing is guessed here.
    assert store.get_task(task.id).state is TaskState.CANCELLING
    assert recovery_reasons(store, task.id) == ["cancel_escalated"]

    # A sweep that finds it still running says so again, but only tells the operator once.
    assert [action.reason for action in run(store, backend)] == ["cancel_escalated"]
    assert recovery_reasons(store, task.id) == ["cancel_escalated"]
    assert backend.stopped == [unit, unit]


def test_a_cancel_still_within_the_grace_period_is_left_alone(store: Store) -> None:
    task = make_task(store, state=TaskState.CANCELLING)
    unit = worker_unit_name(task.id)
    backend = FakeUnitBackend({unit: ACTIVE})
    store.append_event(
        task.id, EventKind.CANCEL_REQUESTED, {"from": "RUNNING", "at": stamp(-5)}
    )

    assert run(store, backend) == []
    assert backend.stopped == []
    assert store.get_task(task.id).state is TaskState.CANCELLING
