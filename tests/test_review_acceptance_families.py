"""Final acceptance rechecks the configured families of an already-recorded review."""

from dataclasses import replace

import pytest

from taskspindle import repos, runner, service
from taskspindle.models import Mode, ReviewTarget, StartTaskRequest, TaskState
from taskspindle.service import TaskSpindleError
from tests.test_orchestrator import (
    AUTHOR,
    REVIEWER,
    accept_request,
    build_candidate,
    implement_request,
    integrate_by_hand,
    manual_integration,
    review_candidate,
    whole_diff,
)
from tests.test_orchestrator import (
    harness as harness,
)
from tests.test_orchestrator import (
    paths as paths,
)
from tests.test_orchestrator import (
    script as script,
)
from tests.test_orchestrator import (
    store as store,
)


@pytest.fixture
def reviewed(harness, make_repo):
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    whole_diff(harness, task_id)
    review_id = review_candidate(harness, repo, task_id)
    return harness, repo, task_id, review_id


def finish_request(reviewed, operation):
    harness, repo, task_id, review_id = reviewed
    if operation == "accept":
        request = accept_request(harness, repo, task_id, review_id)
        return lambda: harness.orchestrator.accept_task(request)
    head = integrate_by_hand(repo)
    request = manual_integration(harness, task_id, head).model_copy(update={"kind": operation})
    return lambda: harness.orchestrator.record_integration(request)


@pytest.mark.parametrize("operation", ["accept", "manual_integration", "conflict_resolved"])
@pytest.mark.parametrize("family", ["claude", "grok", "agy", "muse"])
def test_recorded_review_cannot_land_after_profiles_resolve_to_the_same_family(
    reviewed, operation, family
):
    harness, repo, task_id, _ = reviewed
    finish = finish_request(reviewed, operation)
    orchestrator = harness.orchestrator
    # This is also the state of a historical review created before same-family aliases were
    # refused. Different ids, commands and models cannot establish independent families.
    orchestrator.profiles[AUTHOR] = replace(orchestrator.profiles[AUTHOR], base=family)
    orchestrator.profiles[REVIEWER] = replace(
        orchestrator.profiles[REVIEWER], base=family, model="a-different-model"
    )
    before = orchestrator.task_status(task_id)
    head = repos.current_head(repo)
    events = orchestrator.store.list_events(task_id)
    units = list(harness.backend.started)

    with pytest.raises(TaskSpindleError) as caught:
        finish()

    assert caught.value.code == service.REVIEWER_NOT_INDEPENDENT
    assert orchestrator.task_status(task_id)["state"] == TaskState.RESULT_READY.value
    assert orchestrator.task_status(task_id)["state_version"] == before["state_version"]
    assert orchestrator.store.read_journal(task_id) is None
    assert orchestrator.store.list_events(task_id) == events
    assert harness.backend.started == units
    assert repos.current_head(repo) == head


@pytest.mark.parametrize("operation", ["accept", "manual_integration", "conflict_resolved"])
@pytest.mark.parametrize("missing", [AUTHOR, REVIEWER])
def test_recorded_review_requires_both_profiles_to_establish_independence(
    reviewed, operation, missing
):
    harness, _, task_id, _ = reviewed
    finish = finish_request(reviewed, operation)
    del harness.orchestrator.profiles[missing]

    with pytest.raises(TaskSpindleError) as caught:
        finish()

    assert caught.value.code == service.REVIEWER_NOT_INDEPENDENT
    assert "not configured" in caught.value.message
    assert harness.orchestrator.task_status(task_id)["state"] == TaskState.RESULT_READY.value
    assert harness.orchestrator.store.read_journal(task_id) is None


@pytest.mark.parametrize("operation", ["accept", "manual_integration", "conflict_resolved"])
@pytest.mark.parametrize("api_provider", [AUTHOR, REVIEWER])
def test_different_builtin_families_remain_independent_with_api_auth(
    harness, make_repo, monkeypatch, operation, api_provider
):
    monkeypatch.setattr(runner, "_pre_spawn_evidence", lambda *args: {"source": "fake_auth"})
    for provider in (AUTHOR, REVIEWER):
        harness.orchestrator.profiles[provider] = replace(
            harness.orchestrator.profiles[provider],
            first_class=True,
            base="muse" if provider == api_provider else "claude",
            auth="api_key" if provider == api_provider else "oauth",
        )
    repo = make_repo()
    harness.orchestrator.authorize_repository(str(repo), [AUTHOR, REVIEWER], ["implement", "review"])
    task_id = harness.orchestrator.start_task(implement_request(repo, allow_metered=True))["task_id"]
    whole_diff(harness, task_id)
    review_id = harness.orchestrator.start_task(StartTaskRequest(
        provider=REVIEWER, mode=Mode.REVIEW, prompt="review", allow_metered=True, timeout_s=60,
        review_target=ReviewTarget(kind="candidate", task_id=task_id,
                                  candidate_sha=harness.orchestrator.task_status(task_id)["candidate_sha"]),
    ))["task_id"]
    finish = finish_request((harness, repo, task_id, review_id), operation)
    result = finish()

    expected = TaskState.ACCEPTING if operation == "accept" else TaskState.ACCEPTED
    assert result["state"] == expected.value
    assert harness.orchestrator.task_status(task_id)["state"] == expected.value
