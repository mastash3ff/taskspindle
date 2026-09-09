"""Native overage exercises real worker boundaries using only the fake ACP subprocess."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskspindle import auth_context, native_overage, runner
from taskspindle.models import Mode, TaskState
from taskspindle.orchestrator import Orchestrator
from taskspindle.service import TaskSpindleError
from tests import test_runner as fixtures
from tests.fakes.units import ACTIVE, FakeUnitBackend
from tests.test_quota_runner import _prompt_started
from tests.test_runner import BOOT, profile_for, run_task, seed_task

paths, store, script = fixtures.paths, fixtures.store, fixtures.script


def _managed(store, task, profile):
    profile = replace(profile, native_overage="provider_managed")
    store.set_task_auth_context(task.id, auth_context.fingerprint(profile, os.environ))
    turn = store.list_turns(task.id)[-1]
    store.update_turn_native_overage(
        turn["id"], native_overage.snapshot(store, profile, datetime.now(UTC), parent_env=os.environ)
    )
    return profile


async def test_overage_service_success_preserves_included_exhaustion(store, paths, script):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    fixture = script({"response": "continued work"})
    store.insert_provider_window(task.provider, "five_hour", status="rejected", source="rate_limit_event")
    profile = _managed(store, task, profile_for(fixture))
    before = store.list_quota_restrictions(task.provider)
    assert await run_task(store, paths, task, fixture, profile=profile) is TaskState.COMPLETED
    assert store.list_quota_restrictions(task.provider) == before
    assert store.list_native_overage_attempts()[-1]["state"] == "succeeded"
    assert store.list_native_overage_turns()[0]["native_overage"]["billing_classification"] == "unknown"


async def test_observe_only_still_records_native_charge(store, paths, script):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    fixture = script(
        {
            "response": "account supplied usage",
            "rate_limit": {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "isUsingOverage": True,
                "overageStatus": "allowed",
                "arbitrary": "not persisted",
            },
        }
    )
    assert await run_task(store, paths, task, fixture) is TaskState.COMPLETED
    view = store.list_native_overage_turns()[0]["native_overage"]
    assert view["billing_classification"] == "native_overage"
    assert view["observed"] == {"in_use": True, "status": "allowed"}


async def test_cancelled_first_attempt_remains_spent(store, paths, script):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    fixture = script({"block_seconds": 10, "response": "must cancel"})
    store.insert_provider_window(task.provider, "five_hour", status="rejected", source="rate_limit_event")
    profile = _managed(store, task, profile_for(fixture))
    worker = asyncio.create_task(run_task(store, paths, task, fixture, profile=profile))
    await _prompt_started(worker, task)
    assert runner.request_cancel(task.id)
    assert await worker is TaskState.CANCELLED
    assert store.list_native_overage_attempts()[-1]["state"] == "refused"
    assert (
        native_overage.project(store, profile, datetime.now(UTC), parent_env=os.environ)["eligibility"]
        == "blocked"
    )


def _failed_task(store, paths, script, family=None):
    fixture = script(
        {
            "response": "resume partial work",
            "load_session": True,
            "config_options": [
                {
                    "id": "model",
                    "name": "Model",
                    "type": "select",
                    "currentValue": "claude-sonnet-4-6",
                    "options": [{"value": "claude-sonnet-4-6", "name": "Original"}],
                }
            ],
        }
    )
    profile = profile_for(fixture)
    if family:
        profile = replace(profile, base=family)
    task = seed_task(
        store, paths, mode=Mode.CONSULT, session_id="original-session", provider_family=profile.family
    )
    store.set_task_auth_context(task.id, auth_context.fingerprint(profile, os.environ))
    turn = store.list_turns(task.id)[-1]
    store.update_turn_native_overage(
        turn["id"], native_overage.snapshot(store, profile, datetime.now(UTC), parent_env=os.environ)
    )
    store.complete_turn(
        turn["id"],
        response="partial answer",
        session_id="original-session",
        attribution={"reported_model": "claude-sonnet-4-6"},
    )
    store.release_lease(task.provider, task.id)
    store.update_task(
        task.id,
        None,
        state=TaskState.FAILED,
        response="partial answer",
        error={
            "code": "PROVIDER_THROTTLED",
            "message": "usage exhausted",
            "details": {"window": "five_hour"},
        },
    )
    store.append_event(task.id, "task_failed", {"code": "PROVIDER_THROTTLED"})
    store.insert_provider_window(
        runner.limits.status_key(profile), "five_hour", status="rejected", source="rate_limit_event"
    )
    return task, replace(profile, native_overage="provider_managed")


def _orch(store, paths, profile, backend):
    return Orchestrator(
        store=store,
        paths=paths,
        profiles={profile.id: profile},
        units=backend,
        boot=BOOT,
        parent_env=os.environ,
    )


def test_failed_quota_continuation_retains_session_partial_work_and_history(store, paths, script):
    task, profile = _failed_task(store, paths, script)
    partial = Path(task.scratch_repo) / "partial.txt"
    partial.write_text("unfinished work")
    backend = FakeUnitBackend()
    orch = _orch(store, paths, profile, backend)
    before = store.get_task(task.id)
    ack = orch.continue_task(task.id, before.state_version, "Finish from the partial result.")
    resumed = store.get_task(task.id)
    turns = store.list_turns(task.id)
    assert ack["task_id"] == task.id
    assert resumed.session_id == "original-session"
    assert resumed.scratch_repo == before.scratch_repo
    assert resumed.response == "partial answer"
    assert partial.read_text() == "unfinished work"
    assert turns[0]["native_overage"]["policy"] == "observe_only"
    assert turns[1]["native_overage"]["policy"] == "provider_managed"
    assert turns[1]["prompt"] == "Finish from the partial result."
    assert turns[1]["kind"] == "resume"
    with pytest.raises(TaskSpindleError):
        orch.continue_task(task.id, before.state_version, "duplicate")
    assert len(store.list_turns(task.id)) == 2


@pytest.mark.parametrize(
    "failure",
    ["missing_session", "live_worker", "generic_error", "revoked", "missing_workspace", "unrelated_quota"],
)
def test_failed_continuation_rejects_missing_authority_or_liveness(store, paths, script, failure):
    task, profile = _failed_task(store, paths, script)
    backend = FakeUnitBackend()
    if failure == "missing_session":
        store.update_task(task.id, None, session_id=None)
    elif failure == "live_worker":
        from taskspindle.units import worker_unit_name

        backend.states[worker_unit_name(task.id)] = ACTIVE
    elif failure == "generic_error":
        store.update_task(task.id, None, error={"code": "ARBITRARY_FAILURE"})
    elif failure == "missing_workspace":
        store.update_task(task.id, None, scratch_repo=str(paths.state_dir / "missing-workspace"))
    elif failure == "unrelated_quota":
        store.update_task(
            task.id, None, error={"code": "PROVIDER_UNAVAILABLE", "details": {"window": "unknown"}}
        )
    elif failure == "revoked":
        paths.config_file.write_text("[native_overage]\n")
    orch = _orch(store, paths, profile, backend)
    with pytest.raises(TaskSpindleError):
        orch.continue_task(task.id, store.get_task(task.id).state_version)
    assert len(store.list_turns(task.id)) == 1


async def test_quota_continuation_really_loads_original_session(store, paths, script):
    task, profile = _failed_task(store, paths, script)
    orch = _orch(store, paths, profile, FakeUnitBackend())
    status = orch.task_status(task.id)
    assert status["native_overage"]["continuation"]["eligible"] is True
    assert status["native_overage"]["policy"] == "observe_only"
    orch.continue_task(task.id, status["state_version"], "Continue the partial work only.")
    fixture = Path(profile.env["TASKSPINDLE_FAKE_SCRIPT"])
    assert await run_task(store, paths, task, fixture, profile=profile) is TaskState.COMPLETED
    assert store.get_task(task.id).session_id == "original-session"
    turns = store.list_turns(task.id)
    assert turns[1]["session_id"] == "original-session"
    assert turns[0]["response"] == "partial answer"
    assert turns[1]["prompt"] == "Continue the partial work only."


async def test_policy_revocation_before_prompt_stops_inference(store, paths, script, monkeypatch):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    fixture = script({"response": "must not prompt"})
    profile = _managed(store, task, profile_for(fixture))
    paths.config_file.write_text(f'[native_overage]\n{profile.id} = "provider_managed"\n')
    original = runner._open_session

    async def revoke_after_session(run, agent):
        await original(run, agent)
        paths.config_file.write_text("[native_overage]\n")

    monkeypatch.setattr(runner, "_open_session", revoke_after_session)
    assert await run_task(store, paths, task, fixture, profile=profile) is TaskState.FAILED
    assert store.get_task(task.id).error["code"] == "NATIVE_OVERAGE_POLICY_CHANGED"
    assert store.list_turns(task.id)[-1]["response"] is None


def test_restart_settles_dead_claim_but_keeps_live_claim(store, paths, script):
    from taskspindle.units import worker_unit_name
    from tests.fakes.units import SUCCESS

    task, profile = _failed_task(store, paths, script)
    backend = FakeUnitBackend()
    orch = _orch(store, paths, profile, backend)
    orch.continue_task(task.id, store.get_task(task.id).state_version)
    turn = store.list_turns(task.id)[-1]
    store.update_task(task.id, None, state=TaskState.FAILED)
    backend.states[worker_unit_name(task.id)] = ACTIVE
    orch.reconcile()
    assert store.list_native_overage_attempts()[-1]["state"] == "claimed"
    backend.states[worker_unit_name(task.id)] = SUCCESS
    orch.reconcile()
    assert store.list_native_overage_attempts()[-1]["state"] == "refused"
    assert store.list_native_overage_attempts()[-1]["turn_id"] == turn["id"]


@pytest.mark.parametrize(
    "events,expected,in_use",
    [
        ([{"isUsingOverage": False}, {"isUsingOverage": "no"}], "unknown", None),
        ([{"isUsingOverage": False}, {"isUsingOverage": False, "overageInUse": True}], "unknown", None),
        ([{"isUsingOverage": False}, {"overageStatus": "allowed"}], "unknown", None),
        ([{"isUsingOverage": True}, {"isUsingOverage": "bad"}], "unknown", True),
        ([{"isUsingOverage": True}, {"isUsingOverage": False}], "mixed", True),
    ],
)
def test_ambiguous_multi_event_billing_is_not_included(store, paths, events, expected, in_use):
    from types import SimpleNamespace

    task = seed_task(store, paths, mode=Mode.CONSULT)
    turn = store.list_turns(task.id)[0]
    profile = profile_for(Path("/unused"))
    run = SimpleNamespace(
        store=store, task_id=task.id, turn_id=turn["id"], provider_auth_context_at_start=None
    )
    result = SimpleNamespace(capture=SimpleNamespace(rate_limits=events))
    runner._record_native_observations(run, profile, result)
    view = store.list_native_overage_turns()[0]["native_overage"]
    assert view["billing_classification"] == expected
    assert view["observed"].get("in_use") is in_use


@pytest.mark.parametrize("restored", ["claude-opus-4-6", None])
async def test_failed_quota_continuation_rejects_changed_or_unknown_session_model(
    store, paths, script, restored
):
    import json

    task, profile = _failed_task(store, paths, script)
    fixture = Path(profile.env["TASKSPINDLE_FAKE_SCRIPT"])
    body = json.loads(fixture.read_text())
    if restored:
        body["config_options"][0]["currentValue"] = restored
        body["config_options"][0]["options"] = [{"value": restored, "name": "Changed"}]
    else:
        body.pop("config_options")
    fixture.write_text(json.dumps(body))
    orch = _orch(store, paths, profile, FakeUnitBackend())
    orch.continue_task(task.id, store.get_task(task.id).state_version)
    assert await run_task(store, paths, task, fixture, profile=profile) is TaskState.FAILED
    assert store.get_task(task.id).error["code"] == "RESUME_MODEL_CHANGED"
    assert store.list_turns(task.id)[-1]["response"] is None
    assert {row["model"] for row in store.list_native_overage_attempts()} == {"claude-sonnet-4-6"}


async def test_grok_continuation_uses_advertised_original_model(store, paths, script, monkeypatch):
    import json

    task, profile = _failed_task(store, paths, script, family="grok")
    original = store.list_turns(task.id)[0]
    store.complete_turn(original["id"], attribution={"reported_model": "grok-4.6"})
    fixture = Path(profile.env["TASKSPINDLE_FAKE_SCRIPT"])
    body = json.loads(fixture.read_text())
    body["config_options"][0]["currentValue"] = "grok-4.6"
    body["config_options"][0]["options"] = [{"value": "grok-4.6", "name": "Grok"}]
    fixture.write_text(json.dumps(body))
    monkeypatch.setattr(runner, "_pre_spawn_evidence", lambda *_: {"auth": "oauth"})
    orch = _orch(store, paths, profile, FakeUnitBackend())
    orch.continue_task(task.id, store.get_task(task.id).state_version)
    assert await run_task(store, paths, task, fixture, profile=profile) is TaskState.COMPLETED
    assert store.get_task(task.id).session_id == "original-session"
    assert {attempt["model"] for attempt in store.list_native_overage_attempts()} == {"grok-4.6"}


@pytest.mark.parametrize("profile_present", [True, False])
def test_dead_worker_fences_same_attempt_paid_generation(store, paths, script, profile_present):
    from taskspindle.units import worker_unit_name
    from tests.fakes.units import SUCCESS

    task, profile = _failed_task(store, paths, script)
    backend = FakeUnitBackend()
    orch = _orch(store, paths, profile, backend)
    orch.continue_task(task.id, store.get_task(task.id).state_version)
    turn = store.list_turns(task.id)[-1]
    stamp = datetime.now(UTC)
    native_overage.admit(
        store, profile, turn, stamp, model="claude-sonnet-4-6", parent_env=os.environ, prompting=True
    )
    context = auth_context.fingerprint(profile, os.environ)
    for observed in ({"status": "rejected", "disabled_reason": "out_of_credits"}, {"status": "allowed"}):
        store.record_native_overage_observation(
            runner.limits.status_key(profile), task.id, turn["id"], observed, stamp.isoformat(), context
        )
    assert (
        native_overage.project(store, profile, stamp, model="claude-sonnet-4-6", parent_env=os.environ)[
            "admission_reason"
        ]
        == "native_attempt_pending"
    )
    store.update_task(task.id, None, state=TaskState.FAILED)
    backend.states[worker_unit_name(task.id)] = SUCCESS
    if not profile_present:
        orch.profiles.clear()
    orch.reconcile()
    assert (
        native_overage.project(store, profile, stamp, model="claude-sonnet-4-6", parent_env=os.environ)[
            "admission_reason"
        ]
        == "native_attempt_refused"
    )
    assert {attempt["state"] for attempt in store.list_native_overage_attempts()} == {"refused"}
