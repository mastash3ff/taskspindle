"""Real fake-agent turns preserve concurrent quota and authentication observations."""

import asyncio
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from taskspindle import auth_context, providers, quota, runner
from taskspindle.models import Mode, TaskState
from tests import test_runner as fixtures
from tests.test_runner import PROVIDER, profile_for, run_task, seed_task

paths, store, script = fixtures.paths, fixtures.store, fixtures.script


async def _prompt_started(worker, task):
    async with asyncio.timeout(10):
        while task.id not in runner._CANCEL_HOOKS:
            assert not worker.done()
            await asyncio.sleep(0.01)


async def test_success_resolves_captured_quota_but_not_a_newer_restriction(store, paths, script):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    now = datetime.now(UTC)
    store.insert_provider_window(
        PROVIDER, "five_hour", status="rejected", source="rate_limit_event",
        resets_at=(now - timedelta(minutes=1)).isoformat(), period_key="five_hour|old-period",
    )
    previous = store.list_quota_restrictions(PROVIDER)[0]["evidence_fingerprint"]
    script_path = script({"block_seconds": 0.35, "response": "required work completed"})
    profile = profile_for(script_path)
    worker = asyncio.create_task(run_task(store, paths, task, script_path))
    try:
        await _prompt_started(worker, task)
        assert store.get_task_quota_retry_claim(task.id)["state"] == "prompting"
        store.insert_provider_window(
            PROVIDER, "seven_day", status="rejected", source="rate_limit_event",
            resets_at=(now + timedelta(days=2)).isoformat(), period_key="seven_day|new-period",
        )
        newer = store.list_quota_restrictions(PROVIDER)[-1]["evidence_fingerprint"]
        assert await worker is TaskState.COMPLETED
    finally:
        if not worker.done():
            runner.request_cancel(task.id)
            await worker
    remaining = store.list_quota_restrictions(PROVIDER)
    assert {row["evidence_fingerprint"] for row in remaining} == {newer}
    assert previous != newer
    assert quota.evaluate(store, profile, now=datetime.now(UTC))["state"] == "blocked"
    assert store.get_task_quota_retry_claim(task.id)["state"] == "succeeded"


async def test_old_context_success_cannot_clear_new_context_refusal(store, paths, script, monkeypatch):
    home = paths.state_dir / "private-home"
    credential = home / ".claude" / ".credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_bytes(b"not a credential; isolated metadata fixture")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runner, "_pre_spawn_evidence", lambda *_: {"auth": "oauth"})
    task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="claude")
    script_path = script({"block_seconds": 0.35, "response": "completed before reauthentication"})
    profile = replace(profile_for(script_path), base="claude")
    worker = asyncio.create_task(run_task(store, paths, task, script_path, profile=profile))
    try:
        await _prompt_started(worker, task)
        credential.write_bytes(b"changed isolated metadata fixture")
        current = auth_context.fingerprint(profile, os.environ)
        store.set_provider_auth_context("claude", current)
        store.set_provider_status("claude", "auth_expired", source="acp_error")
        expected = store.get_provider_status("claude")
        assert await worker is TaskState.COMPLETED
    finally:
        if not worker.done():
            runner.request_cancel(task.id)
            await worker
    assert store.get_provider_status("claude") == expected
    assert store.get_provider_auth_context("claude") == current
    assert store.get_task(task.id).response == "completed before reauthentication"


async def test_claude_preflight_uses_the_worker_login_environment(store, paths, script, monkeypatch):
    task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="claude")
    script_path = script({"response": "same login context"})
    selected = paths.state_dir / "selected-claude"
    selected.mkdir()
    profile = replace(profile_for(script_path), base="claude", env={
        **profile_for(script_path).env, "CLAUDE_CONFIG_DIR": str(selected),
    })
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(paths.state_dir / "unused-parent"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "forbidden-test-fixture")
    called = []

    def auth_check(command, *, env):
        called.append(command)
        assert env["CLAUDE_CONFIG_DIR"] == str(selected)
        assert "ANTHROPIC_API_KEY" not in env
        assert env["NO_BROWSER"] == "1"
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
            "subscriptionType": "pro",
        }), "")

    monkeypatch.setattr(providers, "default_runner", auth_check)
    assert await run_task(store, paths, task, script_path, profile=profile) is TaskState.COMPLETED
    assert called == [["claude", "auth", "status"]]


async def test_grok_preflight_checks_the_worker_custom_auth_directory(store, paths, script, monkeypatch):
    task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="grok")
    script_path = script({"response": "custom OAuth directory"})
    home = paths.state_dir / "empty-home"
    selected = paths.state_dir / "selected-grok"
    selected.mkdir()
    (selected / "auth.json").write_text("synthetic metadata fixture")
    monkeypatch.setenv("HOME", str(home))
    profile = replace(profile_for(script_path), base="grok", env={
        **profile_for(script_path).env, "GROK_HOME": str(selected),
    })
    assert await run_task(store, paths, task, script_path, profile=profile) is TaskState.COMPLETED
    assert store.get_task(task.id).oauth_evidence["auth_file_present"] is True


async def test_changed_context_stops_before_any_auth_preflight(store, paths, script, monkeypatch):
    task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="claude")
    script_path = script({"response": "must not run"})
    home = paths.state_dir / "home"
    credential = home / ".claude" / ".credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_text("first isolated metadata")
    monkeypatch.setenv("HOME", str(home))
    profile = replace(profile_for(script_path), base="claude")
    store.set_task_auth_context(task.id, auth_context.fingerprint(profile, os.environ))
    credential.write_text("changed isolated metadata")
    calls = []
    monkeypatch.setattr(runner, "_pre_spawn_evidence", lambda *_: calls.append(True))
    assert await run_task(store, paths, task, script_path, profile=profile) is TaskState.FAILED
    assert store.get_task(task.id).error["code"] == "AUTH_CONTEXT_CHANGED"
    assert calls == []
    assert store.get_provider_status("claude") is None


@pytest.mark.parametrize("resolved,allowed", [
    ("claude-opus-4-6", False), ("claude-sonnet-4-6", True), ("unknown-model", False),
])
async def test_omitted_model_is_checked_after_session_resolution(
    store, paths, script, resolved, allowed,
):
    task = seed_task(store, paths, mode=Mode.CONSULT)
    store.insert_provider_window(
        PROVIDER, "seven_day_opus", status="rejected", source="rate_limit_event",
        resets_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    marker = paths.state_dir / "prompt-environment.json"
    script_path = script({
        "response": "necessary ordinary work", "capture_env_to": str(marker),
        "config_options": [{
            "id": "model", "name": "Model", "type": "select", "currentValue": resolved,
            "options": [{"value": resolved, "name": "Fixture model"}],
        }],
    })
    state = await run_task(store, paths, task, script_path)
    assert state is (TaskState.COMPLETED if allowed else TaskState.FAILED)
    assert marker.exists() is allowed
    if not allowed:
        assert store.get_task(task.id).error["code"] == "PROVIDER_UNAVAILABLE"
    assert len(store.list_quota_restrictions(PROVIDER)) == 1
