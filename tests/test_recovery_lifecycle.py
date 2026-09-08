"""Recovery consumes a real task authorization, never a free-standing provider probe."""

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from taskspindle import provider_recovery
from taskspindle.models import Mode, ReviewTarget, StartTaskRequest, TaskState
from taskspindle.service import TaskSpindleError
from taskspindle.units import UnitError
from tests import test_orchestrator as fixtures
from tests.test_orchestrator import (
    AUTHOR,
    Harness,
    authorize,
    build_candidate,
    implement_request,
)

paths, store, script, harness = fixtures.paths, fixtures.store, fixtures.script, fixtures.harness


def arm(harness: Harness, *, provider=AUTHOR, model=None, state="auth_expired"):
    orch = harness.orchestrator
    profile = orch.profiles[provider]
    if state == "model_unavailable":
        orch.store.set_provider_model_status(profile.family, model, state, source="acp_error")
    else:
        orch.store.set_provider_status(profile.family, state, source="acp_error")
    evidence = provider_recovery.status(
        orch.store, profile, now=orch.clock(), model=model, parent_env=orch.parent_env,
    )
    return orch.provider_recovery(
        "arm", provider=provider, model=model, evidence_revision=evidence["evidence_revision"],
    )


@pytest.mark.parametrize("state", ["auth_expired", "access_denied", "model_unavailable", "throttled"])
def test_recovery_runs_one_real_task_and_is_permanently_consumed(harness, make_repo, state):
    repo = make_repo()
    authorize(harness, repo)
    model = "model-a" if state == "model_unavailable" else None
    permit = arm(harness, model=model, state=state)
    request = implement_request(repo, model=model, recovery_permit_id=permit["permit_id"])
    result = harness.orchestrator.start_task(request)
    assert result["state"] == "RESULT_READY"
    stored = harness.orchestrator.store.get_recovery_permit(permit["permit_id"])
    assert stored["task_id"] == result["task_id"]
    assert stored["state"] == "succeeded"
    with pytest.raises(TaskSpindleError, match=r"used|settled"):
        harness.orchestrator.start_task(request)
    assert len(harness.orchestrator.store.list_tasks()) == 1
    events = harness.orchestrator.store.list_events(result["task_id"])
    assert {e["payload"].get("code") for e in events} >= {
        "PROVIDER_RECOVERY_ATTEMPT", "PROVIDER_RECOVERY_OUTCOME",
    }


def test_grant_and_model_validation_do_not_consume_permit(harness, make_repo):
    repo = make_repo()
    permit = arm(harness, model="model-a")
    request = implement_request(repo, model="model-a", recovery_permit_id=permit["permit_id"])
    with pytest.raises(TaskSpindleError) as error:
        harness.orchestrator.start_task(request)
    assert error.value.code == "GRANT_MISSING"
    authorize(harness, repo)
    with pytest.raises(TaskSpindleError) as error:
        harness.orchestrator.start_task(request.model_copy(update={"model": "model-b"}))
    assert error.value.code == "RECOVERY_SCOPE_MISMATCH"
    assert harness.orchestrator.store.get_recovery_permit(permit["permit_id"])["state"] == "armed"
    assert harness.orchestrator.store.list_tasks() == []


def test_reviewer_independence_is_checked_before_claim(harness, make_repo):
    repo = make_repo()
    subject_id = build_candidate(harness, repo)
    subject = harness.orchestrator.store.get_task(subject_id)
    permit = arm(harness)
    with pytest.raises(TaskSpindleError) as error:
        harness.orchestrator.start_task(StartTaskRequest(
            provider=AUTHOR, mode=Mode.REVIEW, prompt="review candidate",
            recovery_permit_id=permit["permit_id"],
            review_target=ReviewTarget(kind="candidate", task_id=subject_id,
                                       candidate_sha=subject.candidate_sha),
        ))
    assert error.value.code == "REVIEWER_NOT_INDEPENDENT"
    assert harness.orchestrator.store.get_recovery_permit(permit["permit_id"])["state"] == "armed"


def test_permit_does_not_authorize_a_paid_profile(harness, make_repo):
    permit = arm(harness)
    harness.orchestrator.profiles[AUTHOR] = replace(
        harness.orchestrator.profiles[AUTHOR], auth="api_key",
    )
    with pytest.raises(TaskSpindleError) as error:
        harness.orchestrator.start_task(StartTaskRequest(
            provider=AUTHOR, mode="consult", prompt="required work",
            recovery_permit_id=permit["permit_id"],
        ))
    assert error.value.code == "METERED_NOT_ALLOWED"
    assert harness.orchestrator.store.get_recovery_permit(permit["permit_id"])["state"] == "armed"


@pytest.mark.parametrize("stage", ["preparation", "startup"])
def test_setup_failure_spends_permit(harness, make_repo, monkeypatch, stage):
    repo = make_repo()
    authorize(harness, repo)
    permit = arm(harness)
    if stage == "preparation":
        def fail(*args, **kwargs):
            raise OSError("fixture workspace unavailable")
        monkeypatch.setattr(harness.orchestrator, "_make_workspace", fail)
    else:
        def fail(*args, **kwargs):
            raise UnitError("UNIT_FAILED", "fixture unit unavailable")
        monkeypatch.setattr(harness.backend, "start", fail)
    request = implement_request(repo, recovery_permit_id=permit["permit_id"])
    if stage == "preparation":
        with pytest.raises(OSError):
            harness.orchestrator.start_task(request)
    else:
        assert harness.orchestrator.start_task(request)["state"] == "FAILED"
    stored = harness.orchestrator.store.get_recovery_permit(permit["permit_id"])
    assert stored["state"] == "failed"
    assert stored["task_id"] is not None
    assert harness.orchestrator.store.get_task(stored["task_id"]).state is TaskState.FAILED


@pytest.mark.parametrize("change", ["account", "model", "expiry"])
def test_change_after_creation_fails_at_dispatch_without_starting(harness, make_repo, monkeypatch, change):
    repo = make_repo()
    authorize(harness, repo)
    model = "model-a"
    permit = arm(harness, model=model)
    prepare = harness.orchestrator._prepare

    def changed(*args):
        prepare(*args)
        if change == "account":
            harness.orchestrator.store.set_provider_status(AUTHOR, "access_denied", source="acp_error")
        elif change == "model":
            harness.orchestrator.store.set_provider_model_status(
                AUTHOR, model, "model_unavailable", source="acp_error",
            )
        else:
            later = harness.orchestrator.clock() + timedelta(hours=25)
            monkeypatch.setattr(harness.orchestrator, "clock", lambda: later)

    monkeypatch.setattr(harness.orchestrator, "_prepare", changed)
    result = harness.orchestrator.start_task(implement_request(
        repo, model=model, recovery_permit_id=permit["permit_id"],
    ))
    task = harness.orchestrator.store.get_task(result["task_id"])
    assert result["state"] == "FAILED"
    assert task.error["code"] in {"RECOVERY_EVIDENCE_CHANGED", "RECOVERY_EXPIRED"}
    assert task.unit_name is None
    assert task.response is None
    assert harness.orchestrator.store.get_recovery_permit(permit["permit_id"])["state"] == "failed"


def test_arm_and_revoke_do_not_dispatch_pending_tasks(harness, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a recovery action must not run checks, reconcile, or dispatch")
    monkeypatch.setattr(harness.orchestrator, "reconcile", forbidden)
    monkeypatch.setattr(harness.orchestrator, "dispatch_queued", forbidden)
    permit = arm(harness)
    revoked = harness.orchestrator.provider_recovery("revoke", permit_id=permit["permit_id"])
    assert revoked["state"] == "revoked"
    assert harness.orchestrator.store.list_tasks() == []


def test_override_and_permit_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        StartTaskRequest(provider="grok", mode="consult", prompt="work",
                         ignore_provider_status=True, recovery_permit_id="permit")


def test_interruption_and_permit_settlement_commit_together_and_survive_restart(harness):
    from taskspindle.service import transition
    from taskspindle.store import Store

    permit = arm(harness)
    harness.defer()
    result = harness.orchestrator.start_task(StartTaskRequest(
        provider=AUTHOR, mode="consult", prompt="required work",
        recovery_permit_id=permit["permit_id"],
    ))
    task_id = result["task_id"]
    store = harness.orchestrator.store
    with pytest.raises(RuntimeError), store.transaction():
        transition(store, task_id, TaskState.INTERRUPTED, reason="fixture unit lost")
        assert store.get_recovery_permit(permit["permit_id"])["state"] == "failed"
        raise RuntimeError("fixture crash before commit")
    assert store.get_task(task_id).state is TaskState.QUEUED
    assert store.get_recovery_permit(permit["permit_id"])["state"] == "claimed"
    transition(store, task_id, TaskState.INTERRUPTED, reason="fixture unit lost")
    with Store.open(store.path) as restarted:
        assert restarted.get_task(task_id).state is TaskState.INTERRUPTED
        assert restarted.get_recovery_permit(permit["permit_id"])["state"] == "failed"


def test_armed_permit_is_not_used_implicitly(harness):
    permit = arm(harness)
    with pytest.raises(TaskSpindleError) as error:
        harness.orchestrator.start_task(StartTaskRequest(
            provider=AUTHOR, mode="consult", prompt="required work",
        ))
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert harness.orchestrator.store.get_recovery_permit(permit["permit_id"])["state"] == "armed"
    assert harness.orchestrator.store.list_tasks() == []


def test_identical_arm_after_claim_returns_same_bound_permit(harness):
    permit = arm(harness)
    harness.defer()
    request = StartTaskRequest(provider=AUTHOR, mode="consult", prompt="required work",
                               recovery_permit_id=permit["permit_id"])
    task = harness.orchestrator.start_task(request)
    repeated = harness.orchestrator.provider_recovery(
        "arm", provider=AUTHOR, evidence_revision=permit["evidence_revision"],
    )
    assert repeated["permit_id"] == permit["permit_id"]
    assert repeated["expires_at"] == permit["expires_at"]
    assert repeated["task_id"] == task["task_id"]
    assert repeated["state"] == "claimed"
    with pytest.raises(TaskSpindleError):
        harness.orchestrator.start_task(request)
    assert len(harness.orchestrator.store.list_tasks()) == 1


def test_successful_access_stays_successful_after_verification_failure(harness, make_repo):
    repo = make_repo()
    authorize(harness, repo)
    permit = arm(harness)
    task = harness.orchestrator.start_task(implement_request(
        repo, recovery_permit_id=permit["permit_id"], verification_commands=["false"],
    ))
    assert task["state"] == "RESULT_READY"
    assert not all(check.ok for check in harness.orchestrator.store.list_checks(task["task_id"]))
    assert harness.orchestrator.store.get_recovery_permit(permit["permit_id"])["state"] == "succeeded"
    assert harness.orchestrator.store.get_provider_status(AUTHOR)["state"] == "ok"


def test_recovery_authorization_does_not_override_a_continuation_refusal(harness):
    permit = arm(harness)
    result = harness.orchestrator.start_task(StartTaskRequest(
        provider=AUTHOR, mode="consult", prompt="required work",
        recovery_permit_id=permit["permit_id"],
    ))
    store = harness.orchestrator.store
    task = store.get_task(result["task_id"])
    assert task.state is TaskState.COMPLETED
    store.set_provider_status(AUTHOR, "access_denied", source="acp_error")
    continued = harness.orchestrator.continue_task(task.id, task.state_version, "necessary follow-up")
    assert continued["state"] == "RESUMING"
    assert len(store.list_turns(task.id)) == 2
    assert store.list_turns(task.id)[1]["ended_at"] is None
    assert store.get_recovery_permit(permit["permit_id"])["state"] == "succeeded"
    assert store.get_provider_status(AUTHOR)["state"] == "access_denied"


def test_new_evidence_during_placement_prevents_task_creation(harness, monkeypatch):
    permit = arm(harness)
    placement = harness.orchestrator._placement

    def changed(request):
        selected = placement(request)
        harness.orchestrator.store.set_provider_status(AUTHOR, "access_denied", source="acp_error")
        return selected

    monkeypatch.setattr(harness.orchestrator, "_placement", changed)
    with pytest.raises(TaskSpindleError) as error:
        harness.orchestrator.start_task(StartTaskRequest(
            provider=AUTHOR, mode="consult", prompt="required work", recovery_permit_id=permit["permit_id"],
        ))
    assert error.value.code == "RECOVERY_EVIDENCE_CHANGED"
    assert harness.orchestrator.store.list_tasks() == []
    assert harness.orchestrator.store.get_recovery_permit(permit["permit_id"])["state"] == "armed"


def _create_in_process(db_path, task_paths, profile, permit_id):
    from taskspindle.orchestrator import Orchestrator
    from taskspindle.store import Store
    from tests.fakes.units import FakeUnitBackend

    with Store.open(db_path) as store:
        orch = Orchestrator(store=store, paths=task_paths, profiles={profile.id: profile},
                            units=FakeUnitBackend(), boot=fixtures.BOOT,
                            parent_env={"PATH": "/usr/bin", "HOME": str(task_paths.state_dir)})
        try:
            return orch.start_task(StartTaskRequest(
                provider=profile.id, mode="consult", prompt="required work",
                recovery_permit_id=permit_id,
            ))["task_id"]
        except TaskSpindleError as error:
            return error.code


def test_two_processes_create_only_one_task_from_a_permit(harness):
    from concurrent.futures import ProcessPoolExecutor

    permit = arm(harness)
    orch = harness.orchestrator
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_create_in_process, orch.store.path, orch.paths,
                               orch.profiles[AUTHOR], permit["permit_id"]) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    tasks = orch.store.list_tasks()
    assert len(tasks) == 1
    assert results.count(tasks[0].id) == 1
    assert "RECOVERY_NOT_AVAILABLE" in results
    assert orch.store.get_recovery_permit(permit["permit_id"])["task_id"] == tasks[0].id
    assert len([event for event in orch.store.list_events(tasks[0].id)
                if event["payload"].get("code") == "PROVIDER_RECOVERY_ATTEMPT"]) == 1
