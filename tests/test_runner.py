"""The worker, run in-process against the scriptable fake agent and a real git repository."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from taskspindle import repos, runner, service, units, worktrees
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


async def run_task(store: Store, paths: Paths, task: TaskRecord, script_path: Path) -> TaskState:
    return await runner.run_worker(
        store,
        task.id,
        profiles={task.provider: profile_for(script_path, task.provider)},
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
    store.acquire_lease(PROVIDER, task.id, units.worker_unit_name(task.id), 0, BOOT)

    if identity is not None:
        snapshot = repos.snapshot_root(identity.toplevel)
        payload = json.dumps(
            {"head": snapshot.head, "branch": snapshot.branch, "dirty": snapshot.dirty}
        )
        artifact = paths.state_dir / "tasks" / task.id / "root-snapshot.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(payload, encoding="utf-8")
        store.insert_artifact(
            task.id,
            1,
            "root_snapshot",
            "sha256:" + hashlib.sha256(payload.encode()).hexdigest(),
            len(payload),
            str(artifact),
        )
    return task


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


async def test_cancelling_a_running_turn_settles_as_cancelled(
    store: Store, paths: Paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT)
    script_path = script({"block_seconds": 30, "response": "never"})

    turn = asyncio.create_task(run_task(store, paths, task, script_path))
    for _ in range(200):
        if runner.request_cancel(task.id):
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - the worker never reached its turn
        turn.cancel()
        pytest.fail("the worker never registered a cancel hook")

    state = await asyncio.wait_for(turn, timeout=30)

    assert state is TaskState.CANCELLED
    transitions = [
        event["payload"]["to"]
        for event in store.list_events(task.id)
        if event["kind"] == EventKind.STATE_CHANGED.value
    ]
    assert transitions[-2:] == [TaskState.CANCELLING.value, TaskState.CANCELLED.value]
    assert store.get_lease(PROVIDER) is None


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
