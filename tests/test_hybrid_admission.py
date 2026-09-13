"""Exercise hybrid recovery through real task/turn creation and fake local workers."""

# ruff: noqa: F811

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskspindle.models import Mode, StartTaskRequest, TaskState
from taskspindle.orchestrator import Orchestrator
from taskspindle.service import TaskSpindleError
from tests.fakes.units import FakeUnitBackend
from tests.test_orchestrator import BOOT, Harness, fake_profile, paths, script, store  # noqa: F401


def setup(store, paths, script):
    profile = replace(
        fake_profile("synthetic", script({"response": "useful answer", "load_session": True})),
        provider_recovery="hybrid",
    )
    backend = FakeUnitBackend()
    orch = Orchestrator(
        store=store,
        paths=paths,
        profiles={profile.id: profile},
        units=backend,
        boot=BOOT,
        parent_env=dict(os.environ),
    )
    return Harness(orch, backend), profile


def request():
    return StartTaskRequest(provider="synthetic", mode=Mode.CONSULT, prompt="Do useful work", timeout_s=60)


def refusal(store):
    store.set_provider_status(
        "synthetic",
        "access_denied",
        source="acp_error",
        observed_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
    )


def test_start_claims_once_before_worker_dispatch_and_terminal_success_resolves(store, paths, script):
    harness, _profile = setup(store, paths, script)
    refusal(store)
    harness.defer()
    task = harness.orchestrator.start_task(request())
    claim = store.active_recovery_claim("synthetic")
    assert claim is not None and claim["task_id"] == task["task_id"]
    assert harness.orchestrator.task_status(task["task_id"])["automatic_recovery"]["state"] == "trial_running"
    with pytest.raises(TaskSpindleError):
        harness.orchestrator.start_task(request())
    harness.run_pending()
    assert store.get_task(task["task_id"]).state == TaskState.COMPLETED
    assert store.active_recovery_claim("synthetic") is None
    assert store.get_provider_status("synthetic")["state"] == "ok"


def test_new_refusal_before_queued_prompt_blocks_without_spending_trial(store, paths, script):
    harness, _profile = setup(store, paths, script)
    refusal(store)
    harness.defer()
    task = harness.orchestrator.start_task(request())
    store.set_provider_status(
        "synthetic", "access_denied", source="acp_error", observed_at=datetime.now(UTC).isoformat()
    )
    harness.run_pending()
    assert store.get_task(task["task_id"]).state == TaskState.FAILED
    assert store.active_recovery_claim("synthetic") is None
    assert store.list_recovery_episodes("synthetic")[0]["attempts_used"] == 0


def test_continuation_claim_is_atomic_and_success_settles_second_turn(store, paths, script):
    harness, _profile = setup(store, paths, script)
    first = harness.orchestrator.start_task(request())
    record = store.get_task(first["task_id"])
    refusal(store)
    harness.defer()
    continued = harness.orchestrator.continue_task(record.id, record.state_version, "Continue useful work")
    claim = store.active_recovery_claim("synthetic")
    assert claim is not None and claim["task_id"] == record.id
    assert len(store.list_turns(record.id)) == 2
    harness.run_pending()
    assert store.get_task(continued["task_id"]).state == TaskState.COMPLETED
    assert store.active_recovery_claim("synthetic") is None


def test_continuation_in_cooldown_does_not_create_turn(store, paths, script):
    harness, _profile = setup(store, paths, script)
    first = harness.orchestrator.start_task(request())
    record = store.get_task(first["task_id"])
    store.set_provider_status(
        "synthetic", "access_denied", source="acp_error", observed_at=datetime.now(UTC).isoformat()
    )
    with pytest.raises(TaskSpindleError):
        harness.orchestrator.continue_task(record.id, record.state_version, "Continue useful work")
    assert len(store.list_turns(record.id)) == 1


def test_crashed_trial_reconciliation_settles_claim_without_worker_finally(store, paths, script):
    from taskspindle import hybrid_recovery, recovery

    harness, profile = setup(store, paths, script)
    refusal(store)
    harness.defer()
    started = harness.orchestrator.start_task(request())
    record = store.get_task(started["task_id"])
    hybrid_recovery.admit(
        store, profile, record.id, now=datetime.now(UTC), parent_env=dict(os.environ), prompting=True
    )
    store.update_task(record.id, None, state=TaskState.RUNNING, boot_id="old-boot")
    recovery.reconcile(store, harness.backend, boot=BOOT, now=datetime.now(UTC))
    assert store.get_task(record.id).state == TaskState.INTERRUPTED
    assert store.active_recovery_claim("synthetic") is None
    assert store.list_recovery_episodes("synthetic")[0]["attempts_used"] == 1


def test_failed_access_turn_can_resume_original_session_under_hybrid(store, paths, script):
    from tests.fakes.units import SUCCESS

    harness, profile = setup(store, paths, script)
    script_path = Path(profile.env["TASKSPINDLE_FAKE_SCRIPT"])
    script_path.write_text(
        json.dumps({"response": "partial result", "model_id": "claude-sonnet-4-6", "load_session": True})
    )
    first = harness.orchestrator.start_task(request())
    record = store.get_task(first["task_id"])
    script_path.write_text(json.dumps({"fail_kind": "auth", "load_session": True}))
    harness.orchestrator.continue_task(record.id, record.state_version, "More useful work")
    failed = store.get_task(record.id)
    assert failed.state == TaskState.FAILED
    harness.backend.set(failed.unit_name, SUCCESS)
    store.set_provider_status(
        "synthetic",
        "auth_expired",
        source="acp_error",
        observed_at=(datetime.now(UTC) - timedelta(minutes=6)).isoformat(),
    )
    script_path.write_text(
        json.dumps(
            {
                "response": "finished result",
                "model_id": "claude-sonnet-4-6",
                "load_session": True,
                "config_options": [
                    {
                        "id": "model",
                        "name": "Model",
                        "type": "select",
                        "currentValue": "claude-sonnet-4-6",
                        "options": [{"value": "claude-sonnet-4-6", "name": "Fake"}],
                    }
                ],
            }
        )
    )
    resumed = harness.orchestrator.continue_task(record.id, failed.state_version, "Finish useful work")
    assert resumed["state"] == TaskState.COMPLETED.value
    assert store.get_task(record.id).session_id == record.session_id
    assert store.active_recovery_claim("synthetic") is None
