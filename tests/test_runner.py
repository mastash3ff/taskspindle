"""The worker, run in-process against the scriptable fake agent and a real git repository."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskspindle import provider_recovery, repos, runner, service, units, worktrees
from taskspindle.config import Paths
from taskspindle.models import (
    AuthMode,
    EventKind,
    Mode,
    StartTaskRequest,
    TaskRecord,
    TaskState,
    TurnKind,
)
from taskspindle.providers import Profile
from taskspindle.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_ARGV = (sys.executable, "-m", "tests.fakes.fake_agent")
BOOT = "boot-under-test"
PROVIDER = "fake"

VALID_REVIEW = {
    "verdict": "CONCERN",
    "summary": "one thing to look at",
    "findings": [
        {
            "id": "F1",
            "severity": "medium",
            "path": "src/new.txt",
            "line": 1,
            "evidence": "no trailing newline",
            "remedy": "add one",
        }
    ],
    "checks": ["read the candidate"],
}


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


@pytest.fixture
def store(paths: Paths) -> Iterator[Store]:
    store = Store.open(paths.state_dir / "taskspindle.sqlite3")
    yield store
    store.close()


@pytest.fixture
def script(tmp_path: Path) -> Callable[[dict[str, object]], Path]:
    """Return a factory writing fake-agent scripts, one file per call."""
    written: list[Path] = []

    def _write(body: dict[str, object]) -> Path:
        path = tmp_path / f"script-{len(written)}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        written.append(path)
        return path

    return _write


def profile_for(script_path: Path, provider: str = PROVIDER) -> Profile:
    """The fake agent as a provider profile; its script travels in the profile environment."""
    return Profile(
        id=provider,
        auth="oauth",
        command=AGENT_ARGV,
        env={"PYTHONPATH": str(REPO_ROOT), "TASKSPINDLE_FAKE_SCRIPT": str(script_path)},
    )


async def run_task(
    store: Store, paths: Paths, task: TaskRecord, script_path: Path,
    *, profile: Profile | None = None,
) -> TaskState:
    return await runner.run_worker(
        store,
        task.id,
        profiles={task.provider: profile or profile_for(script_path, task.provider)},
        paths=paths,
        boot=BOOT,
        signals=False,
    )


def seed_task(
    store: Store,
    paths: Paths,
    *,
    mode: Mode,
    repo: Path | None = None,
    prompt: str = "do the thing",
    path_prefixes: Sequence[str] = ("src",),
    verification: Sequence[str] = ("true",),
    review_target: dict[str, object] | None = None,
    state: TaskState = TaskState.QUEUED,
    kind: TurnKind = TurnKind.INITIAL,
    session_id: str | None = None,
    provider_family: str = PROVIDER,
    lease_limit: int = 1,
    model: str | None = None,
) -> TaskRecord:
    """Set a task up exactly the way the server will before it starts a worker unit.

    A task with a ``repo`` gets a detached worktree and a pre-dispatch root snapshot artifact;
    without one it gets a scratch repository, the way a consult task does.
    """
    fields: dict[str, object] = {
        "provider": PROVIDER,
        "mode": mode,
        "prompt": prompt,
        "timeout_s": 60,
    }
    if model is not None:
        fields["model"] = model
    if mode is Mode.IMPLEMENT:
        fields.update(
            repository=str(repo),
            acceptance_criteria="the thing exists",
            path_prefixes=list(path_prefixes),
            verification_commands=list(verification),
            candidate_message="Add the thing",
        )
    if review_target is not None:
        fields["review_target"] = review_target

    identity = repos.resolve_repository(repo) if repo is not None else None
    repository_id = None
    if identity is not None:
        repository_id = f"repo-{identity.root_commit[:8]}"
        if store.get_repository(repository_id) is None:
            store.insert_repository(
                repository_id,
                str(identity.common_dir),
                identity.root_commit,
                str(identity.toplevel),
            )

    record = service.create_task(
        store,
        StartTaskRequest(**fields),
        repository_id=repository_id,
        auth_mode=AuthMode.OAUTH,
        provider_family=provider_family,
    )

    placement: dict[str, object] = {}
    if identity is not None:
        base = repos.current_head(identity.toplevel)
        worktree = worktrees.create_worktree(
            identity, base, paths.state_dir / "worktrees" / record.id
        )
        placement.update(base_head=base, worktree_path=str(worktree))
    else:
        scratch = worktrees.create_scratch_repo(paths.state_dir / "scratch" / record.id)
        placement.update(
            base_head=repos.current_head(scratch.toplevel), scratch_repo=str(scratch.toplevel)
        )
    if session_id is not None:
        placement["session_id"] = session_id

    if state is TaskState.QUEUED:
        service.transition(store, record.id, TaskState.QUEUED, reason="seeded", **placement)
    else:
        store.update_task(record.id, None, state=state, **placement)

    task = store.get_task(record.id)
    assert task is not None
    store.insert_turn(task.id, 1, kind.value, prompt=runner.compose_prompt(task, kind))
    store.acquire_lease(PROVIDER, task.id, units.worker_unit_name(task.id), 0, BOOT, limit=lease_limit)

    if identity is not None:
        record_root_snapshot(store, paths, task.id, identity.toplevel, revision=1)
    return task


def attach_recovery_permit(
    store: Store, task: TaskRecord, profile: Profile, *, model: str | None = None,
) -> None:
    """Bind a seeded task to the same single-use permit an authorized start would claim."""
    stamp = datetime.now(UTC)
    evidence = provider_recovery.status(store, profile, now=stamp, model=model, parent_env=os.environ)
    permit = provider_recovery.arm(
        store, profile, evidence_revision=evidence["evidence_revision"], now=stamp,
        model=model, parent_env=os.environ,
    )
    provider_recovery.claim(
        store, profile, permit["permit_id"], task.id, now=stamp, model=model, parent_env=os.environ,
    )


def record_root_snapshot(
    store: Store, paths: Paths, task_id: str, toplevel: Path, *, revision: int
) -> None:
    """Store the pre-dispatch root snapshot the server takes before every turn."""
    snapshot = repos.snapshot_root(toplevel)
    payload = json.dumps(
        {"head": snapshot.head, "branch": snapshot.branch, "dirty": snapshot.dirty}
    )
    artifact = paths.state_dir / "tasks" / task_id / f"root-snapshot-{revision}.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(payload, encoding="utf-8")
    store.insert_artifact(
        task_id,
        revision,
        "root_snapshot",
        "sha256:" + hashlib.sha256(payload.encode()).hexdigest(),
        len(payload),
        str(artifact),
    )


def event_kinds(store: Store, task_id: str) -> list[str]:
    return [event["kind"] for event in store.list_events(task_id)]


# -- implement -------------------------------------------------------------------------------


async def test_an_implement_turn_records_a_candidate(
    store: Store, paths: Paths, make_repo, script
) -> None:
    repo = make_repo()
    task = seed_task(store, paths, mode=Mode.IMPLEMENT, repo=repo)
    script_path = script(
        {"response": "added the file", "write": {"path": "src/new.txt", "content": "hello\n"}}
    )

    state = await run_task(store, paths, task, script_path)

    assert state is TaskState.RESULT_READY
    final = store.get_task(task.id)
    assert final.state is TaskState.RESULT_READY
    assert final.changed_paths == ["src/new.txt"]
    assert final.response == "added the file"
    assert final.warnings is None

    parent = repos.run_git(
        ["rev-parse", f"{final.candidate_sha}^"], cwd=repo
    ).stdout.decode().strip()
    assert parent == final.base_head

    artifact = store.get_artifact(task.id, 1, "candidate_diff")
    body = Path(artifact["path"]).read_bytes()
    assert final.diff_digest == "sha256:" + hashlib.sha256(body).hexdigest()
    assert final.diff_size == len(body)
    assert b"src/new.txt" in body

    checks = store.list_checks(task.id, 1)
    assert [check.ok for check in checks] == [True]
    assert final.check_summary == {"total": 1, "passed": 1, "ok": True}

    assert store.get_lease(PROVIDER) is None
    assert Path(final.transcript_path).exists()
    turn = store.list_turns(task.id)[-1]
    assert turn["ended_at"] is not None
    assert turn["stop_reason"] == "end_turn"


async def test_a_write_outside_the_declared_prefixes_is_a_scope_violation(
    store: Store, paths: Paths, make_repo, script
) -> None:
    repo = make_repo()
    task = seed_task(store, paths, mode=Mode.IMPLEMENT, repo=repo, path_prefixes=("src",))
    script_path = script(
        {"response": "wrote docs", "write": {"path": "docs/x.txt", "content": "notes\n"}}
    )

    state = await run_task(store, paths, task, script_path)

    assert state is TaskState.RESULT_READY
    final = store.get_task(task.id)
    assert final.warnings == ["SCOPE_VIOLATION:docs/x.txt"]
    assert final.changed_paths == ["docs/x.txt"]
    assert EventKind.SCOPE_VIOLATION.value in event_kinds(store, task.id)


async def test_a_write_into_the_root_repository_is_a_root_mutation(
    store: Store, paths: Paths, make_repo, script
) -> None:
    repo = make_repo()
    task = seed_task(store, paths, mode=Mode.IMPLEMENT, repo=repo)
    script_path = script(
        {
            "response": "touched the root",
            "write": {"path": "src/new.txt", "content": "hello\n"},
            "write_abs": {"path": str(repo / "intruder.txt"), "content": "sneaky\n"},
        }
    )

    state = await run_task(store, paths, task, script_path)

    assert state is TaskState.RESULT_READY
    final = store.get_task(task.id)
    assert final.warnings == ["ROOT_MUTATION:intruder.txt"]
    assert EventKind.ROOT_MUTATION.value in event_kinds(store, task.id)


async def test_failing_verification_still_records_the_candidate(
    store: Store, paths: Paths, make_repo, script
) -> None:
    repo = make_repo()
    task = seed_task(
        store, paths, mode=Mode.IMPLEMENT, repo=repo, verification=("false", "true")
    )
    script_path = script(
        {"response": "added the file", "write": {"path": "src/new.txt", "content": "hello\n"}}
    )

    state = await run_task(store, paths, task, script_path)

    # The work is not thrown away because a check failed; the summary says so instead.
    assert state is TaskState.RESULT_READY
    final = store.get_task(task.id)
    assert final.check_summary == {"total": 2, "passed": 0, "ok": False}
    checks = store.list_checks(task.id, 1)
    # Verification stops at the first failure, so the second command never ran.
    assert [(check.command, check.ok) for check in checks] == [("false", False)]


async def test_a_missing_root_snapshot_is_a_recorded_warning_not_a_silent_skip(
    store: Store, paths: Paths, make_repo, script
) -> None:
    repo = make_repo()
    task = seed_task(store, paths, mode=Mode.IMPLEMENT, repo=repo)
    Path(store.get_artifact(task.id, 1, "root_snapshot")["path"]).unlink()
    script_path = script(
        {"response": "added the file", "write": {"path": "src/new.txt", "content": "hello\n"}}
    )

    state = await run_task(store, paths, task, script_path)

    assert state is TaskState.RESULT_READY
    assert store.get_task(task.id).warnings == ["ROOT_CHECK_SKIPPED"]
    payloads = [
        event["payload"]
        for event in store.list_events(task.id)
        if event["kind"] == EventKind.WARNING.value
    ]
    assert any(payload.get("code") == "ROOT_CHECK_SKIPPED" for payload in payloads)


async def test_a_violation_warning_does_not_survive_into_the_next_revision(
    store: Store, paths: Paths, make_repo, script
) -> None:
    repo = make_repo()
    task = seed_task(store, paths, mode=Mode.IMPLEMENT, repo=repo)
    first = script(
        {
            "response": "touched the root",
            "write": {"path": "src/new.txt", "content": "hello\n"},
            "write_abs": {"path": str(repo / "intruder.txt"), "content": "sneaky\n"},
        }
    )

    assert await run_task(store, paths, task, first) is TaskState.RESULT_READY
    assert store.get_task(task.id).warnings == ["ROOT_MUTATION:intruder.txt"]

    # The server opens a repair turn: a new revision, a fresh snapshot, the lease taken again.
    service.transition(store, task.id, TaskState.REPAIRING, reason="repair requested")
    repairing = store.get_task(task.id)
    store.insert_turn(
        task.id,
        2,
        TurnKind.REPAIR.value,
        prompt=runner.compose_prompt(repairing, TurnKind.REPAIR, continuation="stay in scope"),
    )
    store.acquire_lease(PROVIDER, task.id, units.worker_unit_name(task.id), 0, BOOT)
    record_root_snapshot(store, paths, task.id, repos.resolve_repository(repo).toplevel, revision=2)
    second = script(
        {
            "load_session": True,
            "response": "behaved",
            "write": {"path": "src/ok.txt", "content": "tidy\n"},
        }
    )

    assert await run_task(store, paths, repairing, second) is TaskState.RESULT_READY
    final = store.get_task(task.id)
    # The revision-1 violation was not re-earned, so it is gone.
    assert final.warnings is None
    assert final.candidate_revision == 2


async def test_a_turn_that_changes_nothing_fails_with_no_changes(
    store: Store, paths: Paths, make_repo, script
) -> None:
    repo = make_repo()
    task = seed_task(store, paths, mode=Mode.IMPLEMENT, repo=repo)

    state = await run_task(store, paths, task, script({"response": "I did nothing"}))

    assert state is TaskState.FAILED
    final = store.get_task(task.id)
    assert final.error["code"] == "NO_CHANGES"
    assert final.finished_at is not None
    assert store.get_lease(PROVIDER) is None


# -- review ----------------------------------------------------------------------------------


def seed_subject(store: Store) -> TaskRecord:
    """A finished implement task for a review to point at."""
    record = service.create_task(
        store,
        StartTaskRequest(
            provider="other",
            mode=Mode.IMPLEMENT,
            prompt="build it",
            repository="/repo",
            acceptance_criteria="it works",
            path_prefixes=["src"],
            verification_commands=[],
            candidate_message="Add it",
        ),
        repository_id=None,
        auth_mode=AuthMode.OAUTH,
        provider_family="other",
    )
    return store.update_task(record.id, None, candidate_sha="c0ffeeba")


async def test_a_review_records_its_verdict_against_the_subject_candidate(
    store: Store, paths: Paths, make_repo, script
) -> None:
    subject = seed_subject(store)
    task = seed_task(
        store,
        paths,
        mode=Mode.REVIEW,
        repo=make_repo(),
        review_target={
            "kind": "candidate",
            "task_id": subject.id,
            "candidate_sha": subject.candidate_sha,
        },
    )

    state = await run_task(store, paths, task, script({"response": json.dumps(VALID_REVIEW)}))

    assert state is TaskState.COMPLETED
    review = store.get_review_for(task.id)
    assert review["subject_task_id"] == subject.id
    assert review["candidate_sha"] == subject.candidate_sha
    assert review["verdict"] == "CONCERN"
    assert [finding["id"] for finding in review["findings"]] == ["F1"]
    assert EventKind.REVIEW_RECORDED.value in event_kinds(store, task.id)


async def test_a_review_that_dirties_its_worktree_is_completed_without_a_verdict(
    store: Store, paths: Paths, make_repo, script
) -> None:
    subject = seed_subject(store)
    task = seed_task(
        store,
        paths,
        mode=Mode.REVIEW,
        repo=make_repo(),
        review_target={
            "kind": "candidate",
            "task_id": subject.id,
            "candidate_sha": subject.candidate_sha,
        },
    )
    script_path = script(
        {
            "response": json.dumps(VALID_REVIEW),
            "write": {"path": "asked.txt", "content": "refused\n"},
            "write_abs": {"path": str(Path(task.worktree_path) / "sneaky.txt"), "content": "x\n"},
        }
    )

    state = await run_task(store, paths, task, script_path)

    assert state is TaskState.COMPLETED
    assert store.get_review_for(task.id) is None
    final = store.get_task(task.id)
    assert final.warnings == ["READ_ONLY_VIOLATION"]
    assert not (Path(task.worktree_path) / "asked.txt").exists()
    assert event_kinds(store, task.id).count(EventKind.READ_ONLY_VIOLATION.value) == 2


async def test_a_review_that_is_not_json_fails_as_malformed(
    store: Store, paths: Paths, make_repo, script
) -> None:
    subject = seed_subject(store)
    task = seed_task(
        store,
        paths,
        mode=Mode.REVIEW,
        repo=make_repo(),
        review_target={
            "kind": "candidate",
            "task_id": subject.id,
            "candidate_sha": subject.candidate_sha,
        },
    )

    state = await run_task(store, paths, task, script({"malformed_review": True}))

    assert state is TaskState.FAILED
    assert store.get_task(task.id).error["code"] == "REVIEW_MALFORMED"
    assert store.get_review_for(task.id) is None


# -- consult, resume and cancellation ---------------------------------------------------------


async def test_a_consult_turn_completes_with_its_answer(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT, prompt="what shape?")

    state = await run_task(store, paths, task, script({"response": "a hexagon"}))

    assert state is TaskState.COMPLETED
    final = store.get_task(task.id)
    assert final.response == "a hexagon"
    assert final.session_id == "fake-session-1"
    assert final.finished_at is not None
    assert json.loads(Path(final.transcript_path).read_text())["stop_reason"] == "end_turn"


async def test_a_resumed_turn_loads_the_existing_session(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(
        store,
        paths,
        mode=Mode.CONSULT,
        state=TaskState.RESUMING,
        kind=TurnKind.RESUME,
        session_id="fake-session-1",
    )

    state = await run_task(
        store, paths, task, script({"load_session": True, "response": "resumed"})
    )

    assert state is TaskState.COMPLETED
    final = store.get_task(task.id)
    assert final.response == "resumed"
    assert final.session_id == "fake-session-1"
    transitions = [
        event["payload"]["to"]
        for event in store.list_events(task.id)
        if event["kind"] == EventKind.STATE_CHANGED.value
    ]
    assert transitions[-2:] == [TaskState.RUNNING.value, TaskState.COMPLETED.value]


async def test_an_agent_that_cannot_resume_leaves_the_task_interrupted(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(
        store,
        paths,
        mode=Mode.CONSULT,
        state=TaskState.RESUMING,
        kind=TurnKind.RESUME,
        session_id="fake-session-1",
    )

    state = await run_task(store, paths, task, script({"load_session": False}))

    assert state is TaskState.INTERRUPTED
    final = store.get_task(task.id)
    assert final.warnings == ["RESUME_UNAVAILABLE"]
    # Nothing the task would need to try again was discarded.
    assert final.session_id == "fake-session-1"
    assert final.scratch_repo == task.scratch_repo
    assert final.finished_at is None


async def cancel_mid_turn(store: Store, paths: Paths, task: TaskRecord, script_path: Path) -> TaskState:
    """Start a turn, ask it to cancel as soon as it is in flight, and return where it settled."""
    turn = asyncio.create_task(run_task(store, paths, task, script_path))
    for _ in range(200):
        if runner.request_cancel(task.id):
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - the worker never reached its turn
        turn.cancel()
        pytest.fail("the worker never registered a cancel hook")
    return await asyncio.wait_for(turn, timeout=30)


async def test_cancelling_a_running_turn_settles_as_cancelled(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script({"block_seconds": 30, "response": "never"})

    state = await cancel_mid_turn(store, paths, task, script_path)

    assert state is TaskState.CANCELLED
    transitions = [
        event["payload"]["to"]
        for event in store.list_events(task.id)
        if event["kind"] == EventKind.STATE_CHANGED.value
    ]
    assert transitions[-2:] == [TaskState.CANCELLING.value, TaskState.CANCELLED.value]
    assert store.get_lease(PROVIDER) is None


async def test_an_agent_that_errors_out_when_cancelled_is_still_cancelled(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script({"block_seconds": 30, "fail_on_cancel": True, "response": "never"})

    state = await cancel_mid_turn(store, paths, task, script_path)

    # The agent fell over on its way out; what was asked for was a cancel, not a failure.
    assert state is TaskState.CANCELLED
    final = store.get_task(task.id)
    assert final.state is TaskState.CANCELLED
    assert final.error is None
    assert store.get_lease(PROVIDER) is None


async def test_cancelled_turn_is_not_evidence_that_a_previous_throttle_cleared(
    store: Store, paths: Paths, script,
) -> None:
    store.set_provider_status(PROVIDER, "throttled", source="earlier_failure")
    observed = store.get_provider_status(PROVIDER)
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script({"block_seconds": 30})
    attach_recovery_permit(store, task, profile_for(script_path))
    state = await cancel_mid_turn(store, paths, task, script_path)
    assert state is TaskState.CANCELLED
    assert store.get_provider_status(PROVIDER) == observed


@pytest.mark.parametrize("operation", ["initialize", "new"])
@pytest.mark.parametrize("cancel_requested", [False, True])
async def test_transport_close_during_startup_honors_only_requested_cancellation(
    store: Store, paths: Paths, script, operation: str, cancel_requested: bool,
) -> None:
    victim = seed_task(store, paths, mode=Mode.CONSULT, lease_limit=2)
    sibling = seed_task(store, paths, mode=Mode.CONSULT, lease_limit=2)
    started = paths.state_dir / "startup-child.pid"
    victim_turn = asyncio.create_task(run_task(store, paths, victim, script({
        f"{operation}_delay": 30, f"started_{operation}_to": str(started),
    })))
    sibling_turn = asyncio.create_task(run_task(store, paths, sibling, script({
        "block_seconds": 1, "response": "independent sibling finished",
    })))
    try:
        async with asyncio.timeout(10):
            while not started.exists():
                await asyncio.sleep(0.01)
        assert store.get_task(victim.id).state is TaskState.RUNNING
        assert store.get_task(victim.id).session_id is None
        if cancel_requested:
            service.transition(store, victim.id, TaskState.CANCELLING, reason="cancel requested")
        # systemd cancels the entire worker unit: the ACP child can lose its
        # transport before the runner reaches _prompt's cancellation watcher.
        os.kill(int(started.read_text()), signal.SIGTERM)
        victim_state, sibling_state = await asyncio.wait_for(
            asyncio.gather(victim_turn, sibling_turn), timeout=15,
        )
    finally:
        for turn in (victim_turn, sibling_turn):
            if not turn.done():
                turn.cancel()
        await asyncio.gather(victim_turn, sibling_turn, return_exceptions=True)

    final = store.get_task(victim.id)
    if cancel_requested:
        assert victim_state is TaskState.CANCELLED
        assert final.error is None
    else:
        assert victim_state is TaskState.FAILED
        expected = "ACP_HANDSHAKE_FAILED" if operation == "initialize" else "ACP_SESSION_FAILED"
        assert final.error["code"] == expected
    assert store.list_turns(victim.id)[0]["ended_at"] is not None
    assert sibling_state is TaskState.COMPLETED
    assert store.get_task(sibling.id).response == "independent sibling finished"
    assert store.get_task(sibling.id).error is None
    assert store.list_leases() == []


# -- provider limits and usage ------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["account", "model"])
async def test_new_refusal_before_first_prompt_blocks_the_turn(
    store: Store, paths: Paths, script, scope: str,
) -> None:
    task = seed_task(
        store, paths, mode=Mode.CONSULT, model="model-a" if scope == "model" else None,
    )
    marker = paths.state_dir / f"{scope}-session-started.pid"
    script_path = script({
        "new_delay": 0.5,
        "started_new_to": str(marker),
        "response": "must not run",
        "config_options": [{
            "id": "model", "name": "Model", "type": "select", "currentValue": "model-a",
            "options": [{"value": "model-a", "name": "Model A"}],
        }],
    })
    worker = asyncio.create_task(run_task(
        store, paths, task, script_path,
        profile=replace(profile_for(script_path), model="model-a"),
    ))
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                assert not worker.done()
                await asyncio.sleep(0.01)
        if scope == "account":
            store.set_provider_status(PROVIDER, "auth_expired", source="acp_error")
        else:
            store.set_provider_model_status(
                PROVIDER, "model-a", "model_unavailable", source="acp_error",
            )
        assert await worker is TaskState.FAILED
    finally:
        if not worker.done():
            runner.request_cancel(task.id)
            await worker

    final = store.get_task(task.id)
    assert final.error["code"] == service.PROVIDER_UNAVAILABLE
    assert final.response is None


@pytest.mark.parametrize("new_model_refusal", [False, True])
async def test_recovery_permit_follows_default_model_resolution_only_without_model_refusal(
    store: Store, paths: Paths, script, new_model_refusal: bool,
) -> None:
    store.set_provider_status(PROVIDER, "auth_expired", source="acp_error")
    task = seed_task(store, paths, mode=Mode.CONSULT)
    marker = paths.state_dir / "default-model-session-started.pid"
    script_path = script({
        "new_delay": 0.5,
        "started_new_to": str(marker),
        "response": "allowed explicit retry",
        "config_options": [{
            "id": "model", "name": "Model", "type": "select", "currentValue": "model-a",
            "options": [{"value": "model-a", "name": "Model A"}],
        }],
    })
    profile = replace(profile_for(script_path), model="model-a")
    attach_recovery_permit(store, task, profile, model=None)
    worker = asyncio.create_task(run_task(
        store, paths, task, script_path,
        profile=profile,
    ))
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                assert not worker.done()
                await asyncio.sleep(0.01)
        if new_model_refusal:
            store.set_provider_model_status(
                PROVIDER, "model-a", "model_unavailable", source="acp_error",
            )
        expected = TaskState.FAILED if new_model_refusal else TaskState.COMPLETED
        assert await worker is expected
    finally:
        if not worker.done():
            runner.request_cancel(task.id)
            await worker


async def test_a_usage_limit_refusal_marks_the_provider_throttled(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)

    state = await run_task(store, paths, task, script({"fail_kind": "usage_limit"}))

    assert state is TaskState.FAILED
    final = store.get_task(task.id)
    assert final.error["code"] == "PROVIDER_THROTTLED"
    assert final.error["retryable"] is True
    assert final.error["details"]["reset_at"] == "2030-01-01T00:00:00Z"
    assert final.error["details"]["status_key"] == PROVIDER
    availability = service.provider_availability(
        store, profile_for(script({})), now=datetime.now(UTC), parent_env=os.environ,
    )
    assert availability["state"] == "throttled"
    assert [(row["window"], row["reset"], row["source"]) for row in availability["quota_restrictions"]] == [
        ("unknown", "2030-01-01T00:00:00Z", "legacy"),
    ]
    assert store.get_provider_status(PROVIDER) is None
    assert EventKind.PROVIDER_LIMIT.value in event_kinds(store, task.id)
    windows = store.latest_provider_windows(PROVIDER)
    assert [(row["window"], row["status"], row["used_percent"]) for row in windows] == [
        ("unknown", "rejected", 100.0)
    ]
    assert store.get_lease(PROVIDER) is None


async def test_an_auth_refusal_is_recorded_and_not_retryable(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)

    state = await run_task(store, paths, task, script({"fail_kind": "auth"}))

    assert state is TaskState.FAILED
    final = store.get_task(task.id)
    assert final.error["code"] == "PROVIDER_AUTH_EXPIRED"
    assert final.error["retryable"] is False
    assert store.get_provider_status(PROVIDER)["state"] == "auth_expired"


async def test_native_auth_failure_records_its_source_without_provider_output(
    store: Store, paths: Paths, script, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)

    def refuse(*_args):
        raise runner._Failure("OAUTH_REJECTED", "private-provider-output")

    monkeypatch.setattr(runner, "_pre_spawn_evidence", refuse)
    assert await run_task(store, paths, task, script({})) is TaskState.FAILED
    status = store.get_provider_status(PROVIDER)
    assert status["source"] == "native_auth_check"
    assert status["state"] == "auth_expired"
    final = store.get_task(task.id)
    assert "private-provider-output" not in json.dumps(final.error)
    assert "private-provider-output" not in json.dumps(store.list_events(task.id))


async def test_a_turn_that_runs_clears_the_throttle_and_records_its_usage(
    store: Store, paths: Paths, script
) -> None:
    store.set_provider_status(PROVIDER, "throttled", code="PROVIDER_THROTTLED", source="acp_error")
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script(
        {
            "response": "fine now",
            "usage": {"inputTokens": 120, "outputTokens": 30, "cachedReadTokens": 1000, "totalTokens": 1150},
            "rate_limit": {"status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.8},
        }
    )
    attach_recovery_permit(store, task, profile_for(script_path))

    state = await run_task(store, paths, task, script_path)

    assert state is TaskState.COMPLETED
    assert store.get_provider_status(PROVIDER)["state"] == "ok"
    windows = store.latest_provider_windows(PROVIDER)
    assert [(row["window"], row["status"], row["used_percent"]) for row in windows] == [
        ("five_hour", "allowed_warning", 80.0)
    ]
    turn = store.list_turns(task.id)[-1]
    assert turn["attribution"]["agent"] == {"name": "taskspindle-fake-agent", "version": "0.0.0"}
    assert turn["attribution"]["gateway_host"] is None
    recorded = store.get_turn_usage(turn["id"])
    assert recorded["source"] == "acp_prompt_response"
    assert (recorded["input_tokens"], recorded["output_tokens"], recorded["cache_read_tokens"]) == (
        120,
        30,
        1000,
    )
    assert recorded["duration_ms"] is not None
    # The fake profile is neither a Claude seat nor an API key, so nothing is priced.
    assert recorded["cost_estimate_usd"] is None


@pytest.mark.parametrize("failure", ["throttled", "auth_expired"])
async def test_older_success_preserves_new_sibling_failure(
    store: Store, paths: Paths, script, failure: str,
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    marker = paths.state_dir / "prompt-started.json"
    worker = asyncio.create_task(run_task(store, paths, task, script({
        "response": "completed useful work", "capture_env_to": str(marker), "block_seconds": 0.5,
    })))
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                assert not worker.done()
                await asyncio.sleep(0.01)
        sibling_store = Store.open(store.path)
        try:
            sibling_store.set_provider_status(PROVIDER, failure, source="sibling_failure")
            failed_status = sibling_store.get_provider_status(PROVIDER)
        finally:
            sibling_store.close()
        assert await worker is TaskState.COMPLETED
        final_status = store.get_provider_status(PROVIDER)
        assert {key: value for key, value in final_status.items() if key != "last_success_at"} == {
            key: value for key, value in failed_status.items() if key != "last_success_at"
        }
        assert final_status["last_success_at"] is not None
    finally:
        if not worker.done():
            runner.request_cancel(task.id)
            await worker


async def test_rejected_window_is_not_misattributed_to_a_later_allowed_window(
    store: Store, paths: Paths, script,
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    rejected = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1893456000}
    state = await run_task(store, paths, task, script({
        "prompt_updates": [{"sessionUpdate": "usage_update", "used": 1, "size": 10,
                            "_meta": {"_claude/rateLimit": rejected}}],
        "rate_limit": {"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.1},
    }))
    assert state is TaskState.COMPLETED
    availability = service.provider_availability(
        store, profile_for(script({})), now=datetime.now(UTC), parent_env=os.environ,
    )
    assert availability["state"] == "throttled"
    assert [(row["window"], row["reset"], row["source"]) for row in availability["quota_restrictions"]] == [
        ("five_hour", "2030-01-01T00:00:00Z", "rate_limit_event"),
    ]


async def test_a_grok_style_turn_completed_update_is_recorded_with_its_model(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script(
        {
            "response": "done",
            "model_id": "fake-model-9",
            "turn_completed": {
                "usage": {
                    "inputTokens": 500,
                    "outputTokens": 40,
                    "cachedReadTokens": 200,
                    "reasoningTokens": 12,
                    "modelCalls": 2,
                    "costUsdTicks": 43475800,
                    "modelUsage": {"fake-model-9-build": {"inputTokens": 500}},
                }
            },
        }
    )

    state = await run_task(store, paths, task, script_path)

    assert state is TaskState.COMPLETED
    final = store.get_task(task.id)
    assert final.reported_model == "fake-model-9"
    turn = store.list_turns(task.id)[-1]
    assert turn["attribution"]["reported_model"] == "fake-model-9"
    recorded = store.get_turn_usage(turn["id"])
    assert recorded["source"] == "acp_turn_completed"
    assert recorded["model"] == "fake-model-9"
    assert (recorded["input_tokens"], recorded["reasoning_tokens"], recorded["model_calls"]) == (500, 12, 2)
    assert recorded["raw"]["costUsdTicks"] == 43475800
    assert "fake-model-9-build" in recorded["raw"]["modelUsage"]
    assert recorded["cost_estimate_usd"] is None


async def test_session_configuration_model_reaches_turn_attribution(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script({
        "usage": {"total_tokens": 110, "input_tokens": 100, "output_tokens": 10},
        "config_options": [{
            "id": "model", "name": "Model", "type": "select", "currentValue": "claude-opus-5",
            "options": [{"value": "claude-opus-5", "name": "Opus 5"}],
        }],
    })
    assert await run_task(store, paths, task, script_path) is TaskState.COMPLETED
    assert store.get_task(task.id).reported_model == "claude-opus-5"
    turn = store.list_turns(task.id)[-1]
    assert turn["attribution"]["reported_model"] == "claude-opus-5"
    assert store.get_turn_usage(turn["id"])["model"] == "claude-opus-5"


# -- entry conditions --------------------------------------------------------------------------


async def test_a_task_whose_lease_is_held_elsewhere_fails(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    store.release_lease(PROVIDER, task.id)

    state = await run_task(store, paths, task, script({"response": "never asked"}))

    assert state is TaskState.FAILED
    assert store.get_task(task.id).error["code"] == "LEASE_BUSY"


async def test_a_task_in_the_wrong_state_is_refused_without_being_touched(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    store.update_task(task.id, None, state=TaskState.COMPLETED)

    with pytest.raises(service.TaskSpindleError) as excinfo:
        await run_task(store, paths, task, script({"response": "never asked"}))

    assert excinfo.value.code == service.ILLEGAL_TRANSITION
    assert store.get_task(task.id).state is TaskState.COMPLETED


def test_compose_prompt_states_the_rules_each_mode_needs(
    store: Store, paths: Paths, make_repo
) -> None:
    consult = seed_task(store, paths, mode=Mode.CONSULT, prompt="what shape?")
    assert runner.compose_prompt(consult, TurnKind.INITIAL) == "what shape?"
    assert runner.compose_prompt(consult, TurnKind.CONTINUE, continuation="and why?").startswith(
        "Continue.\n\nand why?"
    )
    assert runner.compose_prompt(consult, TurnKind.REPAIR, continuation="fix it").startswith(
        "Continue in this same session and worktree."
    )

    built = seed_task(
        store, paths, mode=Mode.IMPLEMENT, repo=make_repo("other"), verification=("pytest -q",)
    )
    text = runner.compose_prompt(built, TurnKind.INITIAL)
    assert text.startswith("do the thing")
    assert "Change only files under these path prefixes: src" in text
    assert "- pytest -q" in text
    assert "Do not spawn subagents" in text
