"""Quota retry admission across the orchestrator's create, prepare, and dispatch boundaries."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskspindle import provider_recovery, providers, units
from taskspindle.config import Paths
from taskspindle.models import EventKind, Mode, StartTaskRequest, TaskState
from taskspindle.orchestrator import Orchestrator
from taskspindle.service import TaskSpindleError
from taskspindle.store import Store
from taskspindle.units import UnitError
from tests.fakes.units import SUCCESS, FakeUnitBackend
from tests.test_orchestrator import BOOT, Harness, fake_profile


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml", state_dir=tmp_path / "state",
        data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime",
    )


@pytest.fixture
def store(paths: Paths) -> Iterator[Store]:
    opened = Store.open(paths.state_dir / "taskspindle.sqlite3")
    yield opened
    opened.close()


@pytest.fixture
def script(tmp_path: Path) -> Callable[[dict[str, object]], Path]:
    written: list[Path] = []

    def write(body: dict[str, object]) -> Path:
        path = tmp_path / f"script-{len(written)}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        written.append(path)
        return path

    return write


def _request(provider: str, **changes: object) -> StartTaskRequest:
    fields: dict[str, object] = {
        "provider": provider,
        "mode": Mode.CONSULT,
        "prompt": "make one bounded observation",
        "timeout_s": 60,
    }
    fields.update(changes)
    return StartTaskRequest(**fields)


def _expired_window(store, *, window: str = "five_hour") -> None:
    store.insert_provider_window(
        "claude", window, status="rejected", used_percent=100.0,
        resets_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        source="test_quota_admission",
    )


def _orchestrator(store, paths, profiles, backend) -> Orchestrator:
    return Orchestrator(
        store=store, paths=paths, profiles=profiles, units=backend, boot=BOOT,
        parent_env={"HOME": str(paths.state_dir / "home"), "PATH": "/usr/bin:/bin"},
    )


def _claude_alias(profile, provider: str, *, model: str | None = None):
    return profile.__class__(
        id=provider, auth="oauth", command=profile.command, env=profile.env,
        model=model, base="claude",
    )


def _claim_retry_in_process(db_path: Path, task_paths: Paths, profile) -> str:
    """A separately opened SQLite connection, as a second taskspindle server would use."""
    with Store.open(db_path) as process_store:
        orch = _orchestrator(process_store, task_paths, {profile.id: profile}, FakeUnitBackend())
        try:
            return str(orch.start_task(_request(profile.id))["task_id"])
        except TaskSpindleError as error:
            return error.code


def test_aliases_in_separate_orchestrators_share_one_post_reset_retry_claim(store, paths, script) -> None:
    """Separate in-memory coordinators still see one durable claim through their shared store."""
    first = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    second = _claude_alias(fake_profile("second", script({"response": "second"})), "second")
    left = _orchestrator(store, paths, {"first": first, "second": second}, FakeUnitBackend())
    right = _orchestrator(store, paths, {"first": first, "second": second}, FakeUnitBackend())
    _expired_window(store)

    created = left.start_task(_request("first"))
    claim = store.get_task_quota_retry_claim(created["task_id"])

    assert claim is not None and claim["state"] == "claimed"
    with pytest.raises(TaskSpindleError) as blocked:
        right.start_task(_request("second"))
    assert blocked.value.code == "QUOTA_RETRY_PENDING"
    assert store.get_active_quota_retry_claim("claude")["task_id"] == created["task_id"]


def test_process_race_allows_one_alias_to_claim_one_post_reset_retry(store, paths, script) -> None:
    from concurrent.futures import ProcessPoolExecutor

    first = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    second = _claude_alias(fake_profile("second", script({"response": "second"})), "second")
    _expired_window(store)

    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_claim_retry_in_process, store.path, paths, profile)
            for profile in (first, second)
        ]
        results = [future.result(timeout=20) for future in futures]

    tasks = store.list_tasks()
    assert len(tasks) == 1
    assert results.count(tasks[0].id) == 1
    assert results.count("QUOTA_RETRY_PENDING") == 1
    assert store.get_active_quota_retry_claim("claude")["task_id"] == tasks[0].id


def test_preparation_failure_finishes_claim_and_later_task_can_claim(
    store, paths, script, monkeypatch,
) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    orch = _orchestrator(store, paths, {"first": profile}, FakeUnitBackend())
    _expired_window(store)

    def fail_prepare(*args, **kwargs):
        raise RuntimeError("synthetic preparation failure")

    monkeypatch.setattr(orch, "_prepare", fail_prepare)
    with pytest.raises(RuntimeError, match="synthetic preparation failure"):
        orch.start_task(_request("first"))
    failed = store.list_tasks(state=TaskState.FAILED.value, limit=1)[0]
    first_claim = store.get_task_quota_retry_claim(failed.id)
    assert first_claim is not None and first_claim["state"] == "failed"

    monkeypatch.undo()
    later = orch.start_task(_request("first"))
    second_claim = store.get_task_quota_retry_claim(later["task_id"])
    assert second_claim is not None and second_claim["state"] == "claimed"
    assert second_claim["task_id"] != first_claim["task_id"]


def test_unit_start_failure_finishes_the_automatic_retry_claim(store, paths, script, monkeypatch) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    backend = FakeUnitBackend()
    orch = _orchestrator(store, paths, {"first": profile}, backend)
    _expired_window(store)

    def fail_start(*args, **kwargs):
        raise UnitError("UNIT_START_FAILED", "synthetic unit failure")

    monkeypatch.setattr(backend, "start", fail_start)
    result = orch.start_task(_request("first"))
    claim = store.get_task_quota_retry_claim(result["task_id"])

    assert result["state"] == TaskState.FAILED.value
    assert claim is not None and claim["state"] == "failed"
    assert claim["outcome_code"] == "UNIT_START_FAILED"


def test_cancelling_an_unstarted_trial_finishes_and_releases_its_claim(
    store, paths, script, monkeypatch,
) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    orch = _orchestrator(store, paths, {"first": profile}, FakeUnitBackend())
    _expired_window(store)
    monkeypatch.setattr(orch, "dispatch_queued", lambda: [])

    queued = orch.start_task(_request("first"))
    cancelled = orch.cancel_task(queued["task_id"], queued["state_version"])

    claim = store.get_task_quota_retry_claim(queued["task_id"])
    assert cancelled["state"] == TaskState.CANCELLED.value
    assert claim is not None and claim["state"] == "failed"
    assert store.get_active_quota_retry_claim("claude") is None


def test_restart_keeps_live_claim_until_reconciliation_observes_the_worker_end(
    store, paths, script,
) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    backend = FakeUnitBackend()
    first = _orchestrator(store, paths, {"first": profile}, backend)
    _expired_window(store)
    created = first.start_task(_request("first"))
    unit = units.worker_unit_name(created["task_id"])
    assert store.bind_lease("first", created["task_id"], unit_name=unit, pid=4242, boot_id=BOOT)

    restarted = _orchestrator(store, paths, {"first": profile}, backend)
    assert restarted.reconcile() == []
    assert store.get_active_quota_retry_claim("claude")["task_id"] == created["task_id"]

    backend.set(unit, SUCCESS)
    restarted.reconcile()
    claim = store.get_task_quota_retry_claim(created["task_id"])
    assert claim is not None and claim["state"] == "failed"
    assert store.get_active_quota_retry_claim("claude") is None


def test_missing_live_unit_becomes_ambiguous_and_never_times_out_its_retry_claim(
    store, paths, script,
) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    backend = FakeUnitBackend()
    first = _orchestrator(store, paths, {"first": profile}, backend)
    _expired_window(store)
    created = first.start_task(_request("first"))
    unit = units.worker_unit_name(created["task_id"])
    assert store.bind_lease("first", created["task_id"], unit_name=unit, pid=4242, boot_id=BOOT)
    store.heartbeat(created["task_id"])
    backend.states.pop(unit)
    clock = [datetime.now(UTC)]
    restarted = Orchestrator(
        store=store, paths=paths, profiles={"first": profile}, units=backend, boot=BOOT,
        parent_env={"HOME": str(paths.state_dir / "home"), "PATH": "/usr/bin:/bin"},
        clock=lambda: clock[0],
    )

    restarted.reconcile()
    assert store.get_task(created["task_id"]).state is TaskState.RECOVERY_AMBIGUOUS
    assert store.get_task_quota_retry_claim(created["task_id"])["state"] == "claimed"
    clock[0] += timedelta(days=30)

    assert restarted.reconcile() == []
    assert store.get_task(created["task_id"]).state is TaskState.RECOVERY_AMBIGUOUS
    assert store.get_task_quota_retry_claim(created["task_id"])["state"] == "claimed"


def test_changed_auth_metadata_invalidates_an_armed_permit_without_clearing_refusal(
    store, paths, script,
) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    orch = _orchestrator(store, paths, {"first": profile}, FakeUnitBackend())
    credential = paths.state_dir / "home" / ".claude" / ".credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_bytes(b"first fixture login")
    store.set_provider_status("claude", "auth_expired", source="test")
    evidence = provider_recovery.status(
        store, profile, now=orch.clock(), parent_env=orch.parent_env,
    )
    armed = orch.provider_recovery(
        "arm", provider="first", evidence_revision=evidence["evidence_revision"],
    )
    credential.write_bytes(b"changed fixture login metadata")

    with pytest.raises(TaskSpindleError) as raised:
        provider_recovery.validate(
            store, profile, armed["permit_id"], now=orch.clock(), parent_env=orch.parent_env,
        )
    assert raised.value.code == "RECOVERY_AUTH_CONTEXT_CHANGED"
    assert store.get_recovery_permit(armed["permit_id"])["state"] == "armed"
    assert store.get_provider_status("claude")["state"] == "auth_expired"


def test_newer_restriction_before_dispatch_stops_an_unstarted_trial(
    store, paths, script, monkeypatch,
) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    backend = FakeUnitBackend()
    orch = _orchestrator(store, paths, {"first": profile}, backend)
    _expired_window(store)
    monkeypatch.setattr(orch, "dispatch_queued", lambda: [])
    queued = orch.start_task(_request("first"))
    store.insert_provider_window(
        "claude", "seven_day", status="rejected", used_percent=100.0,
        resets_at=(datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        source="newer_before_dispatch",
    )
    monkeypatch.undo()

    assert orch.dispatch_queued() == []
    record = store.get_task(queued["task_id"])
    claim = store.get_task_quota_retry_claim(queued["task_id"])
    assert record.state is TaskState.FAILED
    assert record.error["code"] == "PROVIDER_UNAVAILABLE"
    assert claim is not None and claim["state"] == "failed"
    assert backend.started == []


def test_newer_restriction_before_initial_prompt_stops_a_deferred_fake_worker(
    store, paths, script, monkeypatch,
) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    backend = FakeUnitBackend()
    orch = _orchestrator(store, paths, {"first": profile}, backend)
    harness = Harness(orch, backend)
    harness.defer()
    monkeypatch.setenv("HOME", str(paths.state_dir / "home"))
    monkeypatch.setattr(providers, "claude_oauth_evidence", lambda *args: {"auth": "oauth"})
    _expired_window(store)

    queued = orch.start_task(_request("first"))
    store.insert_provider_window(
        "claude", "seven_day", status="rejected", used_percent=100.0,
        resets_at=(datetime.now(UTC) + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        source="newer_before_prompt",
    )
    harness.run_pending()

    record = store.get_task(queued["task_id"])
    claim = store.get_task_quota_retry_claim(queued["task_id"])
    assert record.state is TaskState.FAILED
    assert record.error["code"] == "PROVIDER_UNAVAILABLE"
    assert claim is not None and claim["state"] == "failed"


def test_default_model_keeps_opus_retry_scope_out_of_sonnet_admission(store, paths, script) -> None:
    opus = _claude_alias(
        fake_profile("opus", script({"response": "opus"})), "opus", model="claude-opus-5",
    )
    sonnet = _claude_alias(
        fake_profile("sonnet", script({"response": "sonnet"})), "sonnet", model="claude-sonnet-5",
    )
    orch = _orchestrator(store, paths, {"opus": opus, "sonnet": sonnet}, FakeUnitBackend())
    _expired_window(store, window="seven_day_opus")

    sonnet_task = orch.start_task(_request("sonnet"))
    opus_task = orch.start_task(_request("opus"))

    assert store.get_task_quota_retry_claim(sonnet_task["task_id"]) is None
    claim = store.get_task_quota_retry_claim(opus_task["task_id"])
    assert claim is not None and claim["state"] == "claimed"


def test_context_change_after_queue_refuses_initial_dispatch_and_finishes_retry(
    store, paths, script, monkeypatch,
) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    backend = FakeUnitBackend()
    orch = _orchestrator(store, paths, {"first": profile}, backend)
    _expired_window(store)
    credential = paths.state_dir / "home" / ".claude" / ".credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_bytes(b"first fixture")

    monkeypatch.setattr(orch, "dispatch_queued", lambda: [])
    queued = orch.start_task(_request("first"))
    assert store.get_task(queued["task_id"]).state is TaskState.QUEUED
    credential.write_bytes(b"changed fixture credential metadata")
    monkeypatch.undo()

    assert orch.dispatch_queued() == []
    record = store.get_task(queued["task_id"])
    claim = store.get_task_quota_retry_claim(queued["task_id"])
    assert record.state is TaskState.FAILED
    assert record.error["code"] == "AUTH_CONTEXT_CHANGED"
    assert claim is not None and claim["state"] == "failed"
    assert backend.started == []


def test_context_change_before_fake_worker_prompt_stops_the_initial_attempt(
    store, paths, script, monkeypatch,
) -> None:
    prompt_marker = paths.state_dir / "fake-prompt-env.json"
    profile = _claude_alias(fake_profile("first", script({
        "response": "first", "capture_env_to": str(prompt_marker),
    })), "first")
    backend = FakeUnitBackend()
    orch = _orchestrator(store, paths, {"first": profile}, backend)
    harness = Harness(orch, backend)
    harness.defer()
    monkeypatch.setenv("HOME", str(paths.state_dir / "home"))
    monkeypatch.setattr(providers, "claude_oauth_evidence", lambda *args: {"auth": "oauth"})
    _expired_window(store)
    credential = paths.state_dir / "home" / ".claude" / ".credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_bytes(b"before queued worker")

    queued = orch.start_task(_request("first"))
    credential.write_bytes(b"after queued worker")
    harness.run_pending()

    record = store.get_task(queued["task_id"])
    claim = store.get_task_quota_retry_claim(queued["task_id"])
    assert record.state is TaskState.FAILED
    assert record.error["code"] == "AUTH_CONTEXT_CHANGED"
    assert claim is not None and claim["state"] == "failed"
    assert not prompt_marker.exists()


def test_unrelated_trial_failure_finishes_its_claim_before_a_later_serial_task(
    store, paths, script, monkeypatch,
) -> None:
    failed_profile = _claude_alias(
        fake_profile("first", script({"fail": True})), "first",
    )
    backend = FakeUnitBackend()
    orch = _orchestrator(store, paths, {"first": failed_profile}, backend)
    Harness(orch, backend)
    monkeypatch.setenv("HOME", str(paths.state_dir / "home"))
    monkeypatch.setattr(providers, "claude_oauth_evidence", lambda *args: {"auth": "oauth"})
    _expired_window(store)

    first = orch.start_task(_request("first"))
    first_claim = store.get_task_quota_retry_claim(first["task_id"])
    assert first["state"] == TaskState.FAILED.value
    assert first_claim is not None and first_claim["state"] == "failed"

    succeeding = _claude_alias(
        fake_profile("first", script({"response": "later success"})), "first",
    )
    orch.profiles["first"] = succeeding
    later = orch.start_task(_request("first"))
    later_claim = store.get_task_quota_retry_claim(later["task_id"])
    assert later_claim is not None and later_claim["task_id"] != first_claim["task_id"]
    assert later_claim["state"] == "succeeded"


def test_legacy_ignore_status_and_a_warning_never_start_a_prompt(store, paths, script) -> None:
    profile = _claude_alias(fake_profile("first", script({"response": "first"})), "first")
    backend = FakeUnitBackend()
    orch = _orchestrator(store, paths, {"first": profile}, backend)
    store.append_event(None, EventKind.WARNING, {
        "code": "PROVIDER_STATUS_OVERRIDE", "provider": "first", "status_key": "claude",
    })

    with pytest.raises(TaskSpindleError) as retired:
        orch.start_task(_request("first", ignore_provider_status=True))

    assert retired.value.code == "LEGACY_OVERRIDE_RETIRED"
    assert backend.started == []
