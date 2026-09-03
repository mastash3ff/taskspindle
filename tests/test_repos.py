"""Tests for the git runner, repository identity, snapshots and path scoping."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskspindle.repos import (
    GitError,
    compare_snapshots,
    normalize_prefixes,
    overlapping_dirty_paths,
    path_in_scope,
    resolve_repository,
    run_git,
    snapshot_root,
)


def test_identity_is_stable_and_distinguishes_repositories(make_repo) -> None:
    one = make_repo("one")
    two = make_repo("two")

    first = resolve_repository(one)
    second = resolve_repository(one)
    other = resolve_repository(two)

    assert first == second
    assert first.key == second.key
    assert first.key != other.key
    assert first.toplevel == one.resolve()
    assert first.common_dir == (one / ".git").resolve()


def test_identity_from_a_subdirectory_matches_the_toplevel(make_repo) -> None:
    root = make_repo()
    nested = root / "pkg" / "inner"
    nested.mkdir(parents=True)

    assert resolve_repository(nested) == resolve_repository(root)


def test_non_repository_and_empty_repository_have_distinct_codes(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError) as not_repo:
        resolve_repository(plain)
    assert not_repo.value.code == "NOT_A_REPOSITORY"

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    run_git(["init", "-q", "-b", "main"], cwd=fresh)
    with pytest.raises(GitError) as empty:
        resolve_repository(fresh)
    assert empty.value.code == "EMPTY_REPOSITORY"


def test_failed_git_command_raises_git_failed(make_repo) -> None:
    root = make_repo()
    with pytest.raises(GitError) as excinfo:
        run_git(["rev-parse", "definitely-not-a-ref"], cwd=root)
    assert excinfo.value.code == "GIT_FAILED"
    assert excinfo.value.returncode != 0

    tolerated = run_git(["rev-parse", "definitely-not-a-ref"], cwd=root, check=False)
    assert tolerated.returncode != 0


def test_snapshots_detect_edits_additions_and_head_moves(make_repo) -> None:
    root = make_repo()
    before = snapshot_root(root)
    assert before.branch == "main"
    assert before.dirty == {}

    (root / "file.txt").write_text("one\ntwo\nfour\n")
    (root / "new.txt").write_text("fresh\n")
    after_edit = snapshot_root(root)

    assert compare_snapshots(before, after_edit) == ["file.txt", "new.txt"]
    assert set(after_edit.dirty) == {"file.txt", "new.txt"}

    run_git(["add", "-A"], cwd=root)
    run_git(["commit", "-q", "-m", "Change things"], cwd=root)
    after_commit = snapshot_root(root)

    changed = compare_snapshots(after_edit, after_commit)
    assert changed[0] == "HEAD"
    assert set(changed[1:]) == {"file.txt", "new.txt"}


def test_deleted_files_are_fingerprinted_as_deleted(make_repo) -> None:
    root = make_repo()
    (root / "file.txt").unlink()
    snapshot = snapshot_root(root)
    assert snapshot.dirty == {"file.txt": "deleted"}


def test_path_in_scope_respects_segment_boundaries() -> None:
    assert path_in_scope("src/a.py", ["src"]) is True
    assert path_in_scope("src", ["src"]) is True
    assert path_in_scope("srcfoo/x", ["src"]) is False
    assert path_in_scope("anything/at/all", ["."]) is True
    assert path_in_scope("docs/x", ["src"]) is False


def test_normalize_prefixes_cleans_and_rejects() -> None:
    assert normalize_prefixes(["./src/", "docs", "src"]) == ("src", "docs")
    assert normalize_prefixes(["."]) == (".",)

    for bad in (["../x"], ["/abs/path"], [""], ["src/../etc"]):
        with pytest.raises(ValueError):
            normalize_prefixes(bad)


def test_overlapping_dirty_paths_filters_to_scope(make_repo) -> None:
    root = make_repo()
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a\n")
    (root / "srcfoo").mkdir()
    (root / "srcfoo" / "b.py").write_text("b\n")
    (root / "file.txt").write_text("changed\n")

    snapshot = snapshot_root(root)
    assert overlapping_dirty_paths(snapshot, ["src"]) == ["src/a.py"]
    assert overlapping_dirty_paths(snapshot, ["."]) == ["file.txt", "src/a.py", "srcfoo/b.py"]
