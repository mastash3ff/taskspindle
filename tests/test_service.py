"""State machine and store-only tool semantics."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from taskspindle import service
from taskspindle.models import (
    AcceptTaskRequest,
    AuthMode,
    Disposition,
    EventKind,
    FindingDisposition,
    Mode,
    ReviewFinding,
    ReviewOutput,
    Severity,
    StartTaskRequest,
    TaskState,
    Verdict,
)
from taskspindle.service import TaskSpindleError, transition
from taskspindle.store import Store

DIGEST = "sha256:candidate"
CANDIDATE = "c0ffee"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store.open(tmp_path / "taskspindle.sqlite3")
    store.insert_repository("repo1", "/repo/.git", "root", "/repo")
    yield store
    store.close()


def implement_request(**overrides: object) -> StartTaskRequest:
    fields: dict[str, object] = {
        "provider": "claude",
        "mode": Mode.IMPLEMENT,
        "prompt": "add the widget",
        "repository": "/repo",
        "acceptance_criteria": "the widget exists",
        "path_prefixes": ["src/"],
        "verification_commands": ["pytest -q"],
        "candidate_message": "Add the widget",
    }
    fields.update(overrides)
    return StartTaskRequest(**fields)


def new_task(store: Store, request: StartTaskRequest | None = None):
    return service.create_task(
        store,
        request or implement_request(),
        repository_id="repo1",
        auth_mode=AuthMode.OAUTH,
    )


def force_state(store: Store, task_id: str, state: TaskState) -> None:
    store.update_task(task_id, None, state=state)


def test_create_task_starts_in_preparing_with_an_event(store: Store) -> None:
    record = new_task(store)
    assert record.id.startswith("ts_")
    assert len(record.id) == 15
    assert record.state is TaskState.PREPARING
    assert record.state_version == 1
    events = store.list_events(record.id)
    assert [event["kind"] for event in events] == [EventKind.TASK_CREATED.value]
    assert store.get_task(record.id).path_prefixes == ["src/"]


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (from_state, to_state)
        for from_state, targets in service.LEGAL_TRANSITIONS.items()
        for to_state in sorted(targets)
    ],
)
def test_every_legal_transition_is_allowed(
    store: Store, from_state: TaskState, to_state: TaskState
) -> None:
    modes = service.MODE_ONLY_TRANSITIONS.get((from_state, to_state))
    request = None
    if modes is not None and Mode.IMPLEMENT not in modes:
        request = StartTaskRequest(provider="grok", mode=sorted(modes)[0], prompt="what shape?")
    record = new_task(store, request)
    force_state(store, record.id, from_state)
    updated = transition(store, record.id, to_state, reason="test")
    assert updated.state is to_state


def test_only_a_consult_task_can_be_reopened_for_a_follow_up(store: Store) -> None:
    consult = new_task(
        store, StartTaskRequest(provider="grok", mode=Mode.CONSULT, prompt="what shape?")
    )
    force_state(store, consult.id, TaskState.COMPLETED)
    assert transition(store, consult.id, TaskState.RESUMING, reason="follow-up").state is (
        TaskState.RESUMING
    )

    built = new_task(store)
    force_state(store, built.id, TaskState.COMPLETED)
    with pytest.raises(TaskSpindleError) as excinfo:
        transition(store, built.id, TaskState.RESUMING, reason="follow-up")
    assert excinfo.value.code == service.MODE_FORBIDS_STATE


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (TaskState.PREPARING, TaskState.RUNNING),
        (TaskState.QUEUED, TaskState.COMPLETED),
        (TaskState.RESULT_READY, TaskState.ACCEPTED),
        (TaskState.COMPLETED, TaskState.RUNNING),
        (TaskState.ACCEPTED, TaskState.REJECTED),
        (TaskState.CANCELLING, TaskState.RUNNING),
    ],
)
def test_illegal_transitions_are_rejected(
    store: Store, from_state: TaskState, to_state: TaskState
) -> None:
    record = new_task(store)
    force_state(store, record.id, from_state)
    with pytest.raises(TaskSpindleError) as excinfo:
        transition(store, record.id, to_state, reason="test")
    assert excinfo.value.code == service.ILLEGAL_TRANSITION
    assert store.get_task(record.id).state is from_state


def test_consult_task_cannot_enter_result_ready(store: Store) -> None:
    request = StartTaskRequest(provider="grok", mode=Mode.CONSULT, prompt="what shape?")
    record = new_task(store, request)
    force_state(store, record.id, TaskState.RUNNING)
    with pytest.raises(TaskSpindleError) as excinfo:
        transition(store, record.id, TaskState.RESULT_READY, reason="test")
    assert excinfo.value.code == service.MODE_FORBIDS_STATE
    transition(store, record.id, TaskState.COMPLETED, reason="done")


def test_stale_state_version_is_rejected(store: Store) -> None:
    record = new_task(store)
    with pytest.raises(TaskSpindleError) as excinfo:
        transition(
            store, record.id, TaskState.QUEUED, reason="test", expected_state_version=99
        )
    assert excinfo.value.code == service.STALE_STATE_VERSION
    assert store.get_task(record.id).state is TaskState.PREPARING


def test_transition_bumps_version_once_and_logs_one_event(store: Store) -> None:
    record = new_task(store)
    updated = transition(
        store,
        record.id,
        TaskState.QUEUED,
        reason="queued",
        expected_state_version=record.state_version,
        unit_name="ts-worker.service",
    )
    assert updated.state_version == record.state_version + 1
    assert updated.unit_name == "ts-worker.service"
    changes = [
        event for event in store.list_events(record.id) if event["kind"] == "STATE_CHANGED"
    ]
    assert len(changes) == 1
    assert changes[0]["payload"] == {
        "from": "PREPARING",
        "to": "QUEUED",
        "reason": "queued",
    }


def test_missing_task_and_grant(store: Store) -> None:
    with pytest.raises(TaskSpindleError) as excinfo:
        transition(store, "ts_missing", TaskState.QUEUED, reason="test")
    assert excinfo.value.code == service.TASK_NOT_FOUND
    with pytest.raises(TaskSpindleError) as grant:
        service.require_grant(store, "repo1", "claude", Mode.IMPLEMENT)
    assert grant.value.code == service.GRANT_MISSING
    store.upsert_grant("repo1", "claude", "implement")
    service.require_grant(store, "repo1", "claude", Mode.IMPLEMENT)


def test_diff_receipts_track_missing_ranges(store: Store) -> None:
    record = new_task(store)
    assert service.diff_fully_retrieved(store, record.id, DIGEST, 100) == [(0, 100)]
    service.record_diff_receipt(store, record.id, DIGEST, 0, 40)
    service.record_diff_receipt(store, record.id, DIGEST, 60, 40)
    assert service.diff_fully_retrieved(store, record.id, DIGEST, 100) == [(40, 60)]
    service.record_diff_receipt(store, record.id, DIGEST, 30, 40)
    assert service.diff_fully_retrieved(store, record.id, DIGEST, 100) == []
    kinds = [event["kind"] for event in store.list_events(record.id)]
    assert kinds.count(EventKind.DIFF_RETRIEVED.value) == 3


def ready_candidate(store: Store, provider: str = "claude"):
    """A task parked in RESULT_READY with its diff fully retrieved."""
    record = new_task(store, implement_request(provider=provider))
    force_state(store, record.id, TaskState.RUNNING)
    record = transition(
        store,
        record.id,
        TaskState.RESULT_READY,
        reason="candidate staged",
        candidate_sha=CANDIDATE,
        diff_digest=DIGEST,
        diff_size=100,
        candidate_revision=1,
    )
    service.record_diff_receipt(store, record.id, DIGEST, 0, 100)
    return record


def record_review(
    store: Store,
    subject_id: str,
    *,
    provider: str = "grok",
    verdict: Verdict = Verdict.PASS,
    findings: list[ReviewFinding] | None = None,
    candidate_sha: str = CANDIDATE,
) -> str:
    review_task = new_task(
        store,
        StartTaskRequest(
            provider=provider,
            mode=Mode.CONSULT,
            prompt="review it",
        ),
    )
    store.insert_review(
        review_task.id,
        subject_id,
        candidate_sha,
        provider,
        ReviewOutput(verdict=verdict, summary="looked", findings=findings or []),
    )
    return review_task.id


def accept_request(record, review_task_id: str, **overrides: object) -> AcceptTaskRequest:
    fields: dict[str, object] = {
        "task_id": record.id,
        "expected_state_version": record.state_version,
        "candidate_sha": CANDIDATE,
        "diff_digest": DIGEST,
        "inspection_summary": "read the whole diff",
        "expected_target_head": "head1",
        "review_task_id": review_task_id,
        "dispositions": [],
        "commit_message": "Add the widget",
    }
    fields.update(overrides)
    return AcceptTaskRequest(**fields)


def test_validate_acceptance_happy_path(store: Store) -> None:
    record = ready_candidate(store)
    review_id = record_review(store, record.id)
    updated = service.validate_acceptance(store, accept_request(record, review_id))
    assert updated.state is TaskState.ACCEPTING
    assert updated.target_head == "head1"
    assert updated.state_version == record.state_version + 1
    kinds = [event["kind"] for event in store.list_events(record.id)]
    assert kinds[-2:] == ["STATE_CHANGED", EventKind.ACCEPT_REQUESTED.value]


def test_validate_acceptance_needs_the_whole_diff(store: Store) -> None:
    record = new_task(store)
    force_state(store, record.id, TaskState.RUNNING)
    record = transition(
        store,
        record.id,
        TaskState.RESULT_READY,
        reason="candidate staged",
        candidate_sha=CANDIDATE,
        diff_digest=DIGEST,
        diff_size=100,
    )
    service.record_diff_receipt(store, record.id, DIGEST, 0, 40)
    review_id = record_review(store, record.id)
    with pytest.raises(TaskSpindleError) as excinfo:
        service.validate_acceptance(store, accept_request(record, review_id))
    assert excinfo.value.code == service.DIFF_NOT_FULLY_RETRIEVED
    assert excinfo.value.details["missing"] == [[40, 100]]
    assert store.get_task(record.id).state is TaskState.RESULT_READY


def test_validate_acceptance_rejects_a_stale_review(store: Store) -> None:
    record = ready_candidate(store)
    review_id = record_review(store, record.id, candidate_sha="older")
    with pytest.raises(TaskSpindleError) as excinfo:
        service.validate_acceptance(store, accept_request(record, review_id))
    assert excinfo.value.code == service.REVIEW_STALE


def test_validate_acceptance_requires_a_review(store: Store) -> None:
    record = ready_candidate(store)
    other = new_task(store)
    with pytest.raises(TaskSpindleError) as excinfo:
        service.validate_acceptance(store, accept_request(record, other.id))
    assert excinfo.value.code == service.REVIEW_REQUIRED


def test_validate_acceptance_requires_an_independent_reviewer(store: Store) -> None:
    record = ready_candidate(store)
    review_id = record_review(store, record.id, provider="claude")
    with pytest.raises(TaskSpindleError) as excinfo:
        service.validate_acceptance(store, accept_request(record, review_id))
    assert excinfo.value.code == service.REVIEWER_NOT_INDEPENDENT


def test_block_verdict_needs_an_override(store: Store) -> None:
    record = ready_candidate(store)
    finding = ReviewFinding(
        id="f1",
        severity=Severity.HIGH,
        path="src/a.py",
        line=3,
        evidence="unsafe",
        remedy="guard it",
    )
    review_id = record_review(
        store, record.id, verdict=Verdict.BLOCK, findings=[finding]
    )
    with pytest.raises(TaskSpindleError) as excinfo:
        service.validate_acceptance(store, accept_request(record, review_id))
    assert excinfo.value.code == service.REVIEW_BLOCKED
    assert excinfo.value.details["finding_ids"] == ["f1"]

    request = accept_request(
        record,
        review_id,
        dispositions=[
            FindingDisposition(
                finding_id="f1", disposition=Disposition.OVERRIDDEN, reason="false positive"
            )
        ],
    )
    updated = service.validate_acceptance(store, request)
    assert updated.state is TaskState.ACCEPTING
    overrides = [
        event
        for event in store.list_events(record.id)
        if event["kind"] == EventKind.REVIEW_OVERRIDE.value
    ]
    assert len(overrides) == 1
    assert overrides[0]["payload"]["finding_id"] == "f1"


def test_concern_verdict_needs_a_disposition_for_every_finding(store: Store) -> None:
    record = ready_candidate(store)
    finding = ReviewFinding(
        id="f1",
        severity=Severity.MEDIUM,
        path="src/a.py",
        line=9,
        evidence="untested",
        remedy="add a test",
    )
    review_id = record_review(
        store, record.id, verdict=Verdict.CONCERN, findings=[finding]
    )
    with pytest.raises(TaskSpindleError) as excinfo:
        service.validate_acceptance(store, accept_request(record, review_id))
    assert excinfo.value.code == service.REVIEW_BLOCKED

    request = accept_request(
        record,
        review_id,
        dispositions=[FindingDisposition(finding_id="f1", disposition=Disposition.FIXED)],
    )
    assert service.validate_acceptance(store, request).state is TaskState.ACCEPTING


def test_views_and_results(store: Store) -> None:
    record = ready_candidate(store)
    view = service.task_view(store.get_task(record.id))
    assert view.state is TaskState.RESULT_READY
    assert view.candidate_sha == CANDIDATE
    assert view.warnings == []
    assert view.error is None

    store.update_task(
        record.id,
        None,
        response="done",
        warnings=["quota is low"],
        oauth_evidence={"gateway_host": "api.anthropic.com"},
        transcript_path="/tmp/transcript.jsonl",
    )
    result = service.task_result(store, record.id)
    assert result.response == "done"
    assert result.attribution["gateway_host"] == "api.anthropic.com"
    assert result.quota_warnings == ["quota is low"]
    assert result.transcript_locator == "/tmp/transcript.jsonl"
