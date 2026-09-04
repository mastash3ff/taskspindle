"""The acceptance unit, run in-process against a real repository.

Every case asserts what happened to the *root* repository, not only to the task row: an accept
that does not land must leave the operator's checkout exactly as it found it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from taskspindle import repos, worktrees
from taskspindle.accept import ROOT_CHECK_PREFIX, run_accept
from taskspindle.config import Paths
from taskspindle.models import (
    AuthMode,
    EventKind,
    Mode,
    StartTaskRequest,
    TaskRecord,
    TaskState,
)
from taskspindle.service import create_task
from taskspindle.store import Store

ENV = {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"}


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


def seed_accepting(
    store: Store,
    paths: Paths,
    repo: Path,
    *,
    content: str = "candidate\n",
    path: str = "src/new.txt",
    verification: list[str] | None = None,
    commit_message: str = "Add the thing",
) -> TaskRecord:
    """Build a real candidate commit and put its task in ACCEPTING with a journal.

    This is what the orchestrator leaves behind when ``accept_task`` has passed every gate.
    """
    identity = repos.resolve_repository(repo)
    repository_id = "repo_seed"
    if store.get_repository(repository_id) is None:
        store.insert_repository(
            repository_id, str(identity.common_dir), identity.root_commit, str(identity.toplevel)
        )
    record = create_task(
        store,
        StartTaskRequest(
            provider="author",
            mode=Mode.IMPLEMENT,
            prompt="add the thing",
            repository=str(repo),
            acceptance_criteria="it exists",
            path_prefixes=["src"],
            verification_commands=verification if verification is not None else ["true"],
            candidate_message=commit_message,
            timeout_s=60,
        ),
        repository_id=repository_id,
        auth_mode=AuthMode.OAUTH,
    )

    base = repos.current_head(repo)
    worktree = worktrees.create_worktree(identity, base, paths.state_dir / "worktrees" / record.id)
    target = worktree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    candidate = worktrees.collapse_candidate(
        identity,
        worktree,
        task_id=record.id,
        revision=1,
        base_sha=base,
        message=commit_message,
    )

    store.update_task(
        record.id,
        None,
        state=TaskState.ACCEPTING,
        base_head=base,
        target_head=repos.current_head(repo),
        worktree_path=str(worktree),
        candidate_sha=candidate.sha,
        candidate_revision=1,
        changed_paths=list(candidate.changed_paths),
        check_summary={"total": 1, "passed": 1, "ok": True},
    )
    store.append_event(
        record.id,
        EventKind.ACCEPT_REQUESTED,
        {"candidate_sha": candidate.sha, "commit_message": commit_message},
    )
    store.write_journal(
        record.id, "probing", target_head=repos.current_head(repo), candidate_sha=candidate.sha
    )
    task = store.get_task(record.id)
    assert task is not None
    return task


def commit_subjects(repo: Path) -> list[str]:
    raw = repos.run_git(["log", "--format=%s"], cwd=repo).stdout
    return raw.decode().strip().splitlines()


def test_a_clean_candidate_lands_as_one_commit(store: Store, paths: Paths, make_repo) -> None:
    repo = make_repo()
    task = seed_accepting(store, paths, repo, commit_message="Add the thing")
    before = repos.current_head(repo)

    outcome = run_accept(store, task.id, paths=paths, parent_env=ENV)

    assert outcome == "accepted"
    final = store.get_task(task.id)
    assert final.state is TaskState.ACCEPTED
    assert final.target_head == repos.current_head(repo)
    assert final.target_head != before
    assert commit_subjects(repo)[0] == "Add the thing"
    assert (repo / "src" / "new.txt").read_text() == "candidate\n"
    assert repos.run_git(["rev-parse", "HEAD^"], cwd=repo).stdout.decode().strip() == before
    assert store.read_journal(task.id) is None
    checks = [check.command for check in store.list_checks(task.id, 1)]
    assert checks == [f"{ROOT_CHECK_PREFIX}true"]


def test_a_conflicting_candidate_leaves_the_repository_untouched(
    store: Store, paths: Paths, make_repo
) -> None:
    repo = make_repo()
    task = seed_accepting(store, paths, repo, path="file.txt", content="candidate one\n")
    # The operator moved the same file on their own branch while the task was running.
    (repo / "file.txt").write_text("operator one\n")
    repos.run_git(["commit", "-aqm", "Edit the file"], cwd=repo)
    before = repos.current_head(repo)
    store.write_journal(task.id, "probing", target_head=before, candidate_sha=task.candidate_sha)

    outcome = run_accept(store, task.id, paths=paths, parent_env=ENV)

    assert outcome == "conflict"
    final = store.get_task(task.id)
    assert final.state is TaskState.RESULT_READY
    assert final.warnings == ["CONFLICT:file.txt"]
    assert repos.current_head(repo) == before
    assert (repo / "file.txt").read_text() == "operator one\n"
    assert not repos.run_git(["status", "--porcelain"], cwd=repo).stdout.strip()
    assert store.read_journal(task.id) is None
    failures = [
        event for event in store.list_events(task.id)
        if event["kind"] == EventKind.ACCEPT_FAILED.value
    ]
    assert failures[-1]["payload"]["paths"] == ["file.txt"]


def test_failing_root_verification_is_undone(store: Store, paths: Paths, make_repo) -> None:
    repo = make_repo()
    task = seed_accepting(store, paths, repo, verification=["false"])
    before = repos.current_head(repo)

    outcome = run_accept(store, task.id, paths=paths, parent_env=ENV)

    assert outcome == "failed"
    final = store.get_task(task.id)
    assert final.state is TaskState.RESULT_READY
    assert final.warnings == ["ACCEPT_FAILED:CHECKS_FAILED"]
    assert repos.current_head(repo) == before
    assert not repos.run_git(["status", "--porcelain"], cwd=repo).stdout.strip()
    assert not (repo / "src" / "new.txt").exists()
    assert store.read_journal(task.id) is None
    checks = store.list_checks(task.id, 1)
    assert [(check.command, check.ok) for check in checks] == [
        (f"{ROOT_CHECK_PREFIX}false", False)
    ]


def test_a_dirty_target_refuses_the_apply(store: Store, paths: Paths, make_repo) -> None:
    repo = make_repo()
    task = seed_accepting(store, paths, repo)
    (repo / "README.md").write_text("edited while the accept was queued\n")

    outcome = run_accept(store, task.id, paths=paths, parent_env=ENV)

    assert outcome == "failed"
    final = store.get_task(task.id)
    assert final.state is TaskState.RESULT_READY
    assert final.warnings == ["ACCEPT_FAILED:TARGET_DIRTY"]
    assert (repo / "README.md").read_text() == "edited while the accept was queued\n"


def test_a_task_that_is_not_accepting_is_refused(store: Store, paths: Paths, make_repo) -> None:
    from taskspindle.service import TaskSpindleError

    repo = make_repo()
    task = seed_accepting(store, paths, repo)
    store.update_task(task.id, None, state=TaskState.RESULT_READY)

    with pytest.raises(TaskSpindleError) as excinfo:
        run_accept(store, task.id, paths=paths, parent_env=ENV)

    assert excinfo.value.code == "ILLEGAL_TRANSITION"
