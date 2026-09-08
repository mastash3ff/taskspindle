"""A recovery permit is checked after native model selection and cannot erase newer evidence."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from taskspindle import provider_recovery, runner
from taskspindle.models import Mode, TaskState
from tests import test_runner as fixtures
from tests.test_runner import (
    PROVIDER,
    profile_for,
    run_task,
    seed_task,
)

paths, store, script = fixtures.paths, fixtures.store, fixtures.script


def claim(store, task, profile, *, model=None, age=timedelta()):
    moment = datetime.now(UTC) - age
    store.set_provider_status(
        PROVIDER, "auth_expired", source="acp_error",
        observed_at=(moment - timedelta(days=30)).isoformat(),
    )
    evidence = provider_recovery.status(store, profile, now=moment, model=model)
    permit = provider_recovery.arm(
        store, profile, now=moment, model=model, evidence_revision=evidence["evidence_revision"],
    )
    provider_recovery.claim(store, profile, permit["permit_id"], task.id, now=moment, model=model)
    return permit


@pytest.mark.parametrize("change", ["account", "model", "expiry", "none"])
async def test_recovery_checks_evidence_immediately_before_prompt(store, paths, script, change):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    marker = paths.state_dir / "recovery-session-started.pid"
    script_path = script({
        "new_delay": 0.25, "started_new_to": str(marker), "response": "required task answer",
        "config_options": [{
            "id": "model", "name": "Model", "type": "select", "currentValue": "claude-opus-4-6",
            "options": [{"value": "claude-opus-4-6", "name": "Model A"}],
        }],
    })
    profile = profile_for(script_path)
    permit = claim(store, task, profile, age=timedelta(hours=25) if change == "expiry" else timedelta())
    worker = asyncio.create_task(run_task(store, paths, task, script_path, profile=profile))
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                assert not worker.done()
                await asyncio.sleep(0.01)
        if change == "account":
            store.set_provider_status(PROVIDER, "access_denied", source="acp_error")
        elif change == "model":
            store.set_provider_model_status(
                PROVIDER, "claude-opus-4-6", "model_unavailable", source="acp_error",
            )
        expected = TaskState.COMPLETED if change == "none" else TaskState.FAILED
        assert await worker is expected
    finally:
        if not worker.done():
            runner.request_cancel(task.id)
            await worker
    final = store.get_task(task.id)
    stored = store.get_recovery_permit(permit["permit_id"])
    assert stored["state"] == ("succeeded" if change == "none" else "failed")
    if change != "none":
        assert final.response is None
        assert final.error["code"].startswith("RECOVERY_")
        assert store.get_provider_status(PROVIDER)["state"] != "ok"
    else:
        assert final.response == "required task answer"
        assert store.get_provider_status(PROVIDER)["state"] == "ok"


@pytest.mark.parametrize("scope", ["account", "model"])
async def test_newer_refusal_during_recovery_survives_old_success(store, paths, script, scope):
    task = seed_task(store, paths, mode=Mode.CONSULT, model="model-a")
    script_path = script({"block_seconds": 0.3, "response": "work succeeded"})
    profile = profile_for(script_path)
    permit = claim(store, task, profile, model="model-a")
    worker = asyncio.create_task(run_task(store, paths, task, script_path, profile=profile))
    try:
        async with asyncio.timeout(10):
            while task.id not in runner._CANCEL_HOOKS:
                assert not worker.done()
                await asyncio.sleep(0.01)
        if scope == "account":
            store.set_provider_status(PROVIDER, "access_denied", source="acp_error")
        else:
            store.set_provider_model_status(PROVIDER, "model-a", "model_unavailable", source="acp_error")
        assert await worker is TaskState.COMPLETED
    finally:
        if not worker.done():
            runner.request_cancel(task.id)
            await worker
    assert store.get_recovery_permit(permit["permit_id"])["state"] == "succeeded"
    remaining = (store.get_provider_status(PROVIDER) if scope == "account" else
                 store.get_provider_model_status(PROVIDER, "model-a"))
    assert remaining["state"] in {"access_denied", "model_unavailable"}
    assert remaining["last_success_at"] is not None


@pytest.mark.parametrize("failure", ["auth", "overloaded"])
async def test_failed_recovery_retains_access_refusal(store, paths, script, failure):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script({"fail_kind": failure})
    profile = profile_for(script_path)
    permit = claim(store, task, profile)
    original = store.get_provider_status(PROVIDER)
    assert await run_task(store, paths, task, script_path, profile=profile) is TaskState.FAILED
    assert store.get_recovery_permit(permit["permit_id"])["state"] == "failed"
    remaining = store.get_provider_status(PROVIDER)
    assert remaining["state"] == "auth_expired"
    if failure == "auth":
        assert remaining["task_id"] == task.id
        assert remaining["observed_at"] != original["observed_at"]
    else:
        assert remaining == original


async def test_cancelled_recovery_does_not_clear_access_refusal(store, paths, script):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script({"block_seconds": 30, "response": "must not finish"})
    profile = profile_for(script_path)
    permit = claim(store, task, profile)
    original = store.get_provider_status(PROVIDER)
    worker = asyncio.create_task(run_task(store, paths, task, script_path, profile=profile))
    try:
        async with asyncio.timeout(10):
            while task.id not in runner._CANCEL_HOOKS:
                assert not worker.done()
                await asyncio.sleep(0.01)
        runner.request_cancel(task.id)
        assert await worker is TaskState.CANCELLED
    finally:
        if not worker.done():
            runner.request_cancel(task.id)
            await worker
    assert store.get_provider_status(PROVIDER) == original
    assert store.get_recovery_permit(permit["permit_id"])["state"] == "failed"
