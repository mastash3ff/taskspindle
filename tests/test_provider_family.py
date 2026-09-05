"""Provider family is immutable task provenance, including historical review gates."""

import sqlite3
from dataclasses import replace

import pytest

from taskspindle import orchestrator as orchestrator_module
from taskspindle import repos, runner, service
from taskspindle.models import Mode, ReviewTarget, StartTaskRequest, TaskState
from taskspindle.store import StoreError
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
from tests.test_store import make_task


def _omit_family(monkeypatch):
    create = orchestrator_module.create_task

    def legacy_create(*args, **kwargs):
        kwargs["provider_family"] = None
        return create(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "create_task", legacy_create)


def _finish(harness, repo, task_id, review_id, operation):
    if operation == "accept":
        request = accept_request(harness, repo, task_id, review_id)
        return lambda: harness.orchestrator.accept_task(request)
    request = manual_integration(harness, task_id, integrate_by_hand(repo)).model_copy(
        update={"kind": operation},
    )
    return lambda: harness.orchestrator.record_integration(request)


def test_creation_persists_family_and_store_forbids_rewriting_it(harness, make_repo):
    task_id = build_candidate(harness, make_repo())
    o = harness.orchestrator
    record = o.store.get_task(task_id)
    assert record.provider_family == AUTHOR
    assert o.task_status(task_id)["provider_family"] == AUTHOR
    assert o.task_result(task_id)["attribution"]["provider_family"] == AUTHOR
    assert o.store.list_turns(task_id)[0]["attribution"]["provider_family"] == AUTHOR
    created = next(e for e in o.store.list_events(task_id) if e["kind"] == "TASK_CREATED")
    assert created["payload"]["provider_family"] == AUTHOR
    with pytest.raises(StoreError, match="unknown task columns"):
        o.store.update_task(task_id, None, provider_family="grok")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        o.store._conn.execute("UPDATE tasks SET provider_family = ? WHERE id = ?", ("grok", task_id))
    assert o.store.get_task(task_id).provider_family == AUTHOR


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_only_legacy_reserved_builtins_have_known_family_without_new_field(store, provider):
    record = make_task(store, provider=provider)
    assert record.provider_family is None
    assert service.task_provider_family(record) == provider


def test_legacy_alias_does_not_infer_family_from_current_configuration(store, harness):
    record = make_task(store, provider=AUTHOR)
    profile = replace(harness.orchestrator.profiles[AUTHOR], base="claude")
    with pytest.raises(service.TaskSpindleError) as caught:
        service.require_task_profile(record, profile)
    assert caught.value.code == service.PROVIDER_FAMILY_UNKNOWN
    assert "new task and independent review" in caught.value.message


@pytest.mark.parametrize("operation", ["accept", "manual_integration", "conflict_resolved"])
@pytest.mark.parametrize("changed", [AUTHOR, REVIEWER])
@pytest.mark.parametrize("provenance", ["bound", "legacy_missing"])
def test_historical_same_family_review_stays_invalid_after_alias_reconfiguration(
    harness, make_repo, monkeypatch, operation, changed, provenance,
):
    o = harness.orchestrator
    for provider in (AUTHOR, REVIEWER):
        o.profiles[provider] = replace(o.profiles[provider], base="claude")
    monkeypatch.setattr(runner, "_pre_spawn_evidence", lambda *args: {"source": "fake_auth"})
    repo = make_repo()
    # Reproduce records admitted by the old command/model rule, before provenance
    # existed. This monkeypatch applies only while producing the historical fixture.
    with monkeypatch.context() as historical:
        historical.setattr(o, "_require_independent", lambda *args: None)
        if provenance == "legacy_missing":
            _omit_family(historical)
            historical.setattr(service, "task_provider_family", lambda record: "claude")
        task_id = build_candidate(harness, repo)
        whole_diff(harness, task_id)
        review_id = review_candidate(harness, repo, task_id)
    o.profiles[changed] = replace(o.profiles[changed], base="grok")
    finish = _finish(harness, repo, task_id, review_id, operation)
    before = o.task_status(task_id)
    events = o.store.list_events(task_id)
    units = list(harness.backend.started)
    head = repos.current_head(repo)

    with pytest.raises(service.TaskSpindleError) as caught:
        finish()

    expected = service.PROVIDER_FAMILY_CHANGED if provenance == "bound" else service.PROVIDER_FAMILY_UNKNOWN
    assert caught.value.code == expected
    assert o.task_status(task_id)["state"] == TaskState.RESULT_READY.value
    assert o.task_status(task_id)["state_version"] == before["state_version"]
    assert o.store.list_events(task_id) == events
    assert o.store.read_journal(task_id) is None
    assert harness.backend.started == units
    assert repos.current_head(repo) == head


@pytest.mark.parametrize("operation", ["accept", "manual_integration", "conflict_resolved"])
def test_legacy_claude_grok_review_remains_acceptable(harness, make_repo, monkeypatch, operation):
    o = harness.orchestrator
    for old, new in ((AUTHOR, "claude"), (REVIEWER, "grok")):
        o.profiles[new] = replace(o.profiles[old], id=new, first_class=True)
    monkeypatch.setattr(runner, "_pre_spawn_evidence", lambda *args: {"source": "fake_auth"})
    _omit_family(monkeypatch)
    repo = make_repo()
    o.authorize_repository(str(repo), ["claude", "grok"], ["implement", "review"])
    task_id = o.start_task(implement_request(repo, provider="claude"))["task_id"]
    assert o.task_status(task_id)["state"] == TaskState.RESULT_READY.value
    whole_diff(harness, task_id)
    review_id = o.start_task(StartTaskRequest(
        provider="grok", mode=Mode.REVIEW, prompt="review", timeout_s=60,
        review_target=ReviewTarget(kind="candidate", task_id=task_id,
                                  candidate_sha=o.task_status(task_id)["candidate_sha"]),
    ))["task_id"]
    assert o.store.get_task(task_id).provider_family is None
    assert o.store.get_task(review_id).provider_family is None
    result = _finish(harness, repo, task_id, review_id, operation)()
    expected = TaskState.ACCEPTING if operation == "accept" else TaskState.ACCEPTED
    assert result["state"] == expected.value


def test_review_creation_rejects_reconfigured_author_before_creating_review(harness, make_repo):
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    o = harness.orchestrator
    before = len(o.store.list_tasks())
    units = list(harness.backend.started)
    o.profiles[AUTHOR] = replace(o.profiles[AUTHOR], base="grok")
    with pytest.raises(service.TaskSpindleError) as caught:
        review_candidate(harness, repo, task_id)
    assert caught.value.code == service.PROVIDER_FAMILY_CHANGED
    assert len(o.store.list_tasks()) == before
    assert harness.backend.started == units


@pytest.mark.parametrize("mode", [Mode.CONSULT, Mode.IMPLEMENT])
def test_continuation_and_repair_reject_family_changes_without_adding_turns(harness, make_repo, mode):
    o = harness.orchestrator
    if mode is Mode.IMPLEMENT:
        task_id = build_candidate(harness, make_repo())
    else:
        task_id = o.start_task(StartTaskRequest(provider=AUTHOR, mode=mode, prompt="consult"))["task_id"]
    before = o.task_status(task_id)
    turns = o.store.list_turns(task_id)
    units = list(harness.backend.started)
    o.profiles[AUTHOR] = replace(o.profiles[AUTHOR], base="claude")
    with pytest.raises(service.TaskSpindleError) as caught:
        o.continue_task(task_id, before["state_version"], "continue")
    assert caught.value.code == service.PROVIDER_FAMILY_CHANGED
    assert o.store.list_turns(task_id) == turns
    assert o.task_status(task_id)["state_version"] == before["state_version"]
    assert harness.backend.started == units


def test_worker_checks_bound_family_again_before_spawning_adapter(harness, monkeypatch):
    harness.defer()
    o = harness.orchestrator
    task_id = o.start_task(StartTaskRequest(provider=AUTHOR, mode=Mode.CONSULT, prompt="consult"))["task_id"]
    o.profiles[AUTHOR] = replace(o.profiles[AUTHOR], base="grok")

    def forbidden_spawn(*args, **kwargs):
        pytest.fail("adapter must not spawn after a provider family change")

    monkeypatch.setattr(runner, "AcpWorker", forbidden_spawn)
    harness.run_pending()
    record = o.store.get_task(task_id)
    assert record.state is TaskState.FAILED
    assert record.error["code"] == service.PROVIDER_FAMILY_CHANGED
    assert record.provider_family == AUTHOR
    assert o.store.get_lease(AUTHOR) is None
