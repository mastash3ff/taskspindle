"""Tests for detached worktrees, candidate collapse, diff artifacts and cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskspindle.repos import GitError, current_head, resolve_repository, run_git
from taskspindle.worktrees import (
    collapse_candidate,
    create_scratch_repo,
    create_worktree,
    delete_task_refs,
    read_diff_page,
    remove_worktree,
    scope_violations,
    snapshot_working_tree,
    worktree_is_clean,
    write_diff_artifact,
)


def _out(args: list[str], cwd: Path) -> str:
    return run_git(args, cwd=cwd).stdout.decode().strip()


def test_worktree_edits_never_reach_the_root(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)

    worktree = create_worktree(identity, base, tmp_path / "wt" / "task")
    (worktree / "file.txt").write_text("worker edit\n")
    (worktree / "brand-new.txt").write_text("new\n")

    assert _out(["status", "--porcelain"], root) == ""
    assert current_head(root) == base
    assert (root / "file.txt").read_text() == "one\ntwo\nthree\n"
    assert not worktree_is_clean(worktree)


def test_collapse_candidate_folds_worker_commits_and_pending_edits(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    worktree = create_worktree(identity, base, tmp_path / "wt" / "task")

    (worktree / "committed.txt").write_text("from a worker commit\n")
    run_git(["add", "-A"], cwd=worktree)
    run_git(["commit", "-q", "-m", "Worker commit"], cwd=worktree)
    (worktree / "pending.txt").write_text("still uncommitted\n")

    candidate = collapse_candidate(
        identity,
        worktree,
        task_id="task-1",
        revision=1,
        base_sha=base,
        message="Candidate revision 1",
    )

    assert candidate.revision == 1
    assert candidate.changed_paths == ("committed.txt", "pending.txt")
    assert _out(["rev-list", "--parents", "-n", "1", candidate.sha], worktree).split()[1:] == [base]
    assert _out(["log", "-1", "--format=%an <%ae>", candidate.sha], worktree) == (
        "TaskSpindle <taskspindle@localhost>"
    )
    assert _out(["rev-parse", "refs/taskspindle/task-1/rev/1"], root) == candidate.sha
    assert current_head(worktree) == candidate.sha
    assert worktree_is_clean(worktree)
    assert _out(["status", "--porcelain"], root) == ""


def test_collapse_candidate_without_changes_raises_no_changes(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    worktree = create_worktree(identity, base, tmp_path / "wt" / "task")

    with pytest.raises(GitError) as excinfo:
        collapse_candidate(
            identity, worktree, task_id="task-1", revision=1, base_sha=base, message="empty"
        )
    assert excinfo.value.code == "NO_CHANGES"


def test_scope_violations_reports_out_of_scope_paths() -> None:
    assert scope_violations(["src/a.py", "docs/b.md"], ["src"]) == ["docs/b.md"]
    assert scope_violations(["src/a.py", "docs/b.md"], ["."]) == []


def test_diff_artifacts_are_byte_identical_across_writes(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    base = current_head(root)
    worktree = create_worktree(identity, base, tmp_path / "wt" / "task")
    (worktree / "file.txt").write_text("one\ntwo\nchanged\n")
    candidate = collapse_candidate(
        identity, worktree, task_id="task-1", revision=1, base_sha=base, message="Candidate"
    )

    first_path = tmp_path / "artifacts" / "one.diff"
    second_path = tmp_path / "artifacts" / "two.diff"
    first = write_diff_artifact(identity, base_sha=base, candidate_sha=candidate.sha, dest=first_path)
    second = write_diff_artifact(identity, base_sha=base, candidate_sha=candidate.sha, dest=second_path)

    assert first == second
    assert first_path.read_bytes() == second_path.read_bytes()
    assert first[1] == first_path.stat().st_size
    assert first[0].startswith("sha256:")
    assert not first_path.with_suffix(".tmp").exists()

    body = first_path.read_bytes()
    assert read_diff_page(first_path, 0, 16) == body[:16]
    assert read_diff_page(first_path, 8, 4096) == body[8:]
    with pytest.raises(ValueError):
        read_diff_page(first_path, 0, 262145)


def test_snapshot_working_tree_leaves_the_real_index_untouched(make_repo) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    (root / "file.txt").write_text("dirty\n")
    (root / "untracked.txt").write_text("also dirty\n")

    status_before = _out(["status", "--porcelain"], root)
    index_before = (root / ".git" / "index").read_bytes()

    sha = snapshot_working_tree(
        identity, expected_head=current_head(root), paths=["."], snapshot_id="snap-1"
    )

    assert (root / ".git" / "index").read_bytes() == index_before
    assert _out(["status", "--porcelain"], root) == status_before
    assert _out(["rev-parse", "refs/taskspindle/snapshots/snap-1"], root) == sha
    listed = _out(["ls-tree", "-r", "--name-only", sha], root).splitlines()
    assert "untracked.txt" in listed
    assert _out(["show", f"{sha}:file.txt"], root) == "dirty"


def test_snapshot_working_tree_rejects_a_stale_head(make_repo) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    with pytest.raises(GitError) as excinfo:
        snapshot_working_tree(identity, expected_head="0" * 40, paths=["."], snapshot_id="snap-1")
    assert excinfo.value.code == "TARGET_MOVED"


def test_remove_worktree_refuses_dirty_unless_forced(make_repo, tmp_path: Path) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    worktree = create_worktree(identity, current_head(root), tmp_path / "wt" / "task")
    (worktree / "scratch.txt").write_text("unsaved\n")

    with pytest.raises(GitError) as excinfo:
        remove_worktree(identity, worktree)
    assert excinfo.value.code == "WORKTREE_DIRTY"
    assert worktree.exists()

    remove_worktree(identity, worktree, force=True)
    assert not worktree.exists()
    assert _out(["worktree", "list", "--porcelain"], root).count("worktree ") == 1


def test_delete_task_refs_touches_only_that_task(make_repo) -> None:
    root = make_repo()
    identity = resolve_repository(root)
    head = current_head(root)
    run_git(["update-ref", "refs/taskspindle/task-1/rev/1", head], cwd=root)
    run_git(["update-ref", "refs/taskspindle/task-1/rev/2", head], cwd=root)
    run_git(["update-ref", "refs/taskspindle/task-2/rev/1", head], cwd=root)

    deleted = delete_task_refs(identity, "task-1")

    assert sorted(deleted) == ["refs/taskspindle/task-1/rev/1", "refs/taskspindle/task-1/rev/2"]
    remaining = _out(["for-each-ref", "--format=%(refname)", "refs/taskspindle/"], root).splitlines()
    assert remaining == ["refs/taskspindle/task-2/rev/1"]


def test_create_scratch_repo_is_usable(tmp_path: Path) -> None:
    identity = create_scratch_repo(tmp_path / "scratch")
    assert identity.toplevel == (tmp_path / "scratch").resolve()
    assert identity.root_commit == current_head(identity.toplevel)
    assert _out(["log", "-1", "--format=%s"], identity.toplevel) == "Create scratch repository"
