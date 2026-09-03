"""Tests for the merge probe, the journaled apply and verification."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from taskspindle.integration import (
    Journal,
    abort_staged,
    commit_staged,
    probe_merge,
    recover_journal,
    run_verification,
    stage_candidate,
)
from taskspindle.repos import GitError, RepositoryIdentity, current_head, resolve_repository, run_git
from taskspindle.worktrees import collapse_candidate, create_worktree


def _out(args: list[str], cwd: Path) -> str:
    return run_git(args, cwd=cwd).stdout.decode().strip()


def _candidate(identity: RepositoryIdentity, base: str, dest: Path, content: str) -> str:
    """Build a candidate commit that rewrites ``file.txt`` in a detached worktree."""
    worktree = create_worktree(identity, base, dest)
    (worktree / "file.txt").write_text(content)
    return collapse_candidate(
        identity,
        worktree,
        task_id="task-1",
        revision=1,
        base_sha=base,
        message="Candidate revision 1",
    ).sha


class _Recorder:
    """Stand-in for the store's journal persistence."""

    def __init__(self) -> None:
        self.phases: list[str] = []

    def __call__(self, journal: Journal) -> None:
        self.phases.append(journal.phase)


def test_clean_probe_returns_a_tree(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    candidate = _candidate(identity, base, tmp_path / "wt" / "a", "one\ntwo\ncandidate\n")

    probe = probe_merge(identity, base_sha=base, target_sha=base, candidate_sha=candidate)

    assert probe.clean is True
    assert probe.conflicts == ()
    assert probe.tree == _out(["rev-parse", f"{candidate}^{{tree}}"], root)


def test_conflicting_probe_names_the_path_and_touches_nothing(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    candidate = _candidate(identity, base, tmp_path / "wt" / "a", "candidate\ntwo\nthree\n")

    (root / "file.txt").write_text("target\ntwo\nthree\n")
    run_git(["add", "-A"], cwd=root)
    run_git(["commit", "-q", "-m", "Diverge in the root"], cwd=root)
    target = current_head(root)

    status_before = _out(["status", "--porcelain"], root)
    index_before = (root / ".git" / "index").read_bytes()

    probe = probe_merge(identity, base_sha=base, target_sha=target, candidate_sha=candidate)

    assert probe.clean is False
    assert probe.conflicts == ("file.txt",)
    assert probe.tree
    assert (root / ".git" / "index").read_bytes() == index_before
    assert _out(["status", "--porcelain"], root) == status_before
    assert current_head(root) == target


def test_stage_then_commit_produces_the_probed_tree(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    candidate = _candidate(identity, base, tmp_path / "wt" / "a", "one\ntwo\ncandidate\n")

    probe = probe_merge(identity, base_sha=base, target_sha=base, candidate_sha=candidate)
    journal = Journal(task_id="task-1", phase="probing", target_head=base, candidate_sha=candidate)
    save = _Recorder()

    stage_candidate(identity, journal=journal, save=save)
    assert save.phases == ["staged"]
    assert current_head(root) == base

    sha = commit_staged(
        identity,
        journal=Journal(task_id="task-1", phase="verified", target_head=base, candidate_sha=candidate),
        save=save,
        message="Accept candidate",
    )

    assert save.phases == ["staged", "committed"]
    assert current_head(root) == sha
    assert _out(["rev-parse", f"{sha}^{{tree}}"], root) == probe.tree
    assert _out(["log", "-1", "--format=%an <%ae>"], root) == "Test User <test@example.com>"
    assert _out(["status", "--porcelain"], root) == ""


def test_staging_refuses_a_dirty_target(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    candidate = _candidate(identity, base, tmp_path / "wt" / "a", "one\ntwo\ncandidate\n")
    (root / "stray.txt").write_text("in the way\n")

    journal = Journal(task_id="task-1", phase="probing", target_head=base, candidate_sha=candidate)
    save = _Recorder()
    with pytest.raises(GitError) as excinfo:
        stage_candidate(identity, journal=journal, save=save)

    assert excinfo.value.code == "TARGET_DIRTY"
    assert save.phases == []


def test_staging_refuses_a_moved_target(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    candidate = _candidate(identity, base, tmp_path / "wt" / "a", "one\ntwo\ncandidate\n")

    (root / "other.txt").write_text("unrelated\n")
    run_git(["add", "-A"], cwd=root)
    run_git(["commit", "-q", "-m", "Move the root ahead"], cwd=root)

    journal = Journal(task_id="task-1", phase="probing", target_head=base, candidate_sha=candidate)
    save = _Recorder()
    with pytest.raises(GitError) as excinfo:
        stage_candidate(identity, journal=journal, save=save)

    assert excinfo.value.code == "TARGET_MOVED"
    assert save.phases == []


def test_conflicting_stage_leaves_the_root_clean(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    candidate = _candidate(identity, base, tmp_path / "wt" / "a", "candidate\ntwo\nthree\n")

    (root / "file.txt").write_text("target\ntwo\nthree\n")
    run_git(["add", "-A"], cwd=root)
    run_git(["commit", "-q", "-m", "Diverge in the root"], cwd=root)
    target = current_head(root)

    journal = Journal(task_id="task-1", phase="probing", target_head=target, candidate_sha=candidate)
    save = _Recorder()
    with pytest.raises(GitError) as excinfo:
        stage_candidate(identity, journal=journal, save=save)

    assert excinfo.value.code == "MERGE_CONFLICT"
    assert excinfo.value.paths == ("file.txt",)
    assert save.phases == ["staged"]
    assert current_head(root) == target
    assert _out(["status", "--porcelain"], root) == ""
    assert (root / "file.txt").read_text() == "target\ntwo\nthree\n"


def test_abort_refuses_when_the_head_does_not_match(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    candidate = _candidate(identity, base, tmp_path / "wt" / "a", "one\ntwo\ncandidate\n")

    (root / "other.txt").write_text("unrelated\n")
    run_git(["add", "-A"], cwd=root)
    run_git(["commit", "-q", "-m", "Move the root ahead"], cwd=root)
    moved = current_head(root)

    journal = Journal(task_id="task-1", phase="staged", target_head=base, candidate_sha=candidate)
    with pytest.raises(GitError) as excinfo:
        abort_staged(identity, journal=journal)

    assert excinfo.value.code == "JOURNAL_MISMATCH"
    assert current_head(root) == moved


def test_recover_journal_aborts_a_staged_apply(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    candidate = _candidate(identity, base, tmp_path / "wt" / "a", "one\ntwo\ncandidate\n")

    journal = Journal(task_id="task-1", phase="probing", target_head=base, candidate_sha=candidate)
    stage_candidate(identity, journal=journal, save=_Recorder())
    assert _out(["status", "--porcelain"], root) != ""

    staged = replace(journal, phase="staged")
    assert recover_journal(identity, staged) == "aborted"
    assert current_head(root) == base
    assert _out(["status", "--porcelain"], root) == ""
    assert recover_journal(identity, Journal("task-1", "committed", base, candidate)) == "committed"


def test_run_verification_stops_at_the_first_failure(tmp_path: Path) -> None:
    env = {"PATH": os.environ["PATH"]}
    results = run_verification(
        tmp_path,
        ["echo hello", "echo boom >&2; exit 3", "echo never"],
        timeout_s=30,
        env=env,
    )

    assert [r.exit_code for r in results] == [0, 3]
    assert results[0].ok is True
    assert results[0].stdout_tail.strip() == "hello"
    assert results[1].ok is False
    assert results[1].stderr_tail.strip() == "boom"
    assert all(r.duration_ms >= 0 for r in results)


def test_run_verification_records_a_timeout(tmp_path: Path) -> None:
    env = {"PATH": os.environ["PATH"]}
    results = run_verification(tmp_path, ["sleep 5"], timeout_s=1, env=env)

    assert len(results) == 1
    assert results[0].exit_code == -1
    assert results[0].ok is False
    assert results[0].stderr_tail == "timed out after 1s"
