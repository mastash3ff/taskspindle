"""Detached worktrees, candidate collapse, diff artifacts and safe cleanup.

Everything here runs against a worker's own worktree or against read-only plumbing in the root
repository. The one place the root repository's index is written is :func:`snapshot_working_tree`,
and it writes to an alternate index file so the real one is never touched.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .repos import GitError, RepositoryIdentity, current_head, path_in_scope, resolve_repository, run_git

__all__ = [
    "CandidateCommit",
    "collapse_candidate",
    "create_scratch_repo",
    "create_worktree",
    "delete_task_refs",
    "read_diff_page",
    "remove_worktree",
    "scope_violations",
    "snapshot_working_tree",
    "worktree_is_clean",
    "write_diff_artifact",
]

#: Identity stamped on commits TaskSpindle creates on the worker's behalf. Root commits made by
#: the operator keep the repository's own configured identity.
_SPINDLE_IDENTITY = {
    "GIT_AUTHOR_NAME": "TaskSpindle",
    "GIT_AUTHOR_EMAIL": "taskspindle@localhost",
    "GIT_COMMITTER_NAME": "TaskSpindle",
    "GIT_COMMITTER_EMAIL": "taskspindle@localhost",
}

#: Largest slice :func:`read_diff_page` will hand back in one call.
MAX_DIFF_PAGE = 262144

#: The page ``task_diff`` returns when the caller names no length. Small on purpose: the MCP client
#: truncates tool output at its own token limit (Codex's ``tool_output_token_limit`` defaults to a
#: few thousand tokens), and a receipt for a page the session never saw in full would let the
#: "whole diff retrieved" gate pass on bytes nobody read. 16 KiB of diff is about 22 KB of base64.
DEFAULT_DIFF_PAGE = 16384


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip()


def _nul_paths(raw: bytes) -> list[str]:
    return [f.decode("utf-8", "surrogateescape") for f in raw.split(b"\0") if f]


def create_worktree(identity: RepositoryIdentity, base_sha: str, dest: Path) -> Path:
    """Add a detached worktree at ``dest`` checked out at ``base_sha``."""
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_git(["worktree", "add", "--detach", str(dest), base_sha], cwd=identity.toplevel)
    return dest.resolve()


def create_scratch_repo(dest: Path) -> RepositoryIdentity:
    """Create a throwaway repository with a single empty commit and return its identity."""
    dest.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q", "-b", "main"], cwd=dest)
    run_git(["config", "user.name", "TaskSpindle"], cwd=dest)
    run_git(["config", "user.email", "taskspindle@localhost"], cwd=dest)
    run_git(["commit", "--allow-empty", "-q", "-m", "Create scratch repository"], cwd=dest)
    return resolve_repository(dest)


@dataclass(frozen=True)
class CandidateCommit:
    """One collapsed candidate: everything the worker did, as a single commit on top of base."""

    sha: str
    revision: int
    changed_paths: tuple[str, ...]


def collapse_candidate(
    identity: RepositoryIdentity,
    worktree: Path,
    *,
    task_id: str,
    revision: int,
    base_sha: str,
    message: str,
) -> CandidateCommit:
    """Collapse the worktree's whole state into one commit parented on ``base_sha``.

    Any commits the worker made itself are irrelevant: the resulting tree is the worktree as it
    stands, so nothing the worker produced is lost and nothing it committed is replayed twice.
    """
    run_git(["add", "-A"], cwd=worktree)
    tree = _text(run_git(["write-tree"], cwd=worktree).stdout)
    base_tree = _text(run_git(["rev-parse", f"{base_sha}^{{tree}}"], cwd=worktree).stdout)
    if tree == base_tree:
        raise GitError("NO_CHANGES", "the worktree is identical to its base commit")

    sha = _text(
        run_git(
            ["commit-tree", tree, "-p", base_sha, "-m", message],
            cwd=worktree,
            env=_SPINDLE_IDENTITY,
        ).stdout
    )
    run_git(["update-ref", f"refs/taskspindle/{task_id}/rev/{revision}", sha], cwd=worktree)
    # The index and files already match the new commit, so a soft reset only moves HEAD.
    run_git(["reset", "--soft", sha], cwd=worktree)
    # --no-renames: a rename is two paths, and the source is one of them. Letting git collapse
    # the pair into the destination alone would hide a file moved *out* of the task's scope.
    changed = sorted(
        _nul_paths(
            run_git(
                ["diff", "--name-only", "--no-renames", "-z", base_sha, sha], cwd=worktree
            ).stdout
        )
    )
    return CandidateCommit(sha=sha, revision=revision, changed_paths=tuple(changed))


def scope_violations(changed_paths: Sequence[str], prefixes: Sequence[str]) -> list[str]:
    """Return the changed paths that fall outside the declared scope prefixes."""
    return sorted(path for path in changed_paths if not path_in_scope(path, prefixes))


def write_diff_artifact(
    identity: RepositoryIdentity,
    *,
    base_sha: str,
    candidate_sha: str,
    dest: Path,
) -> tuple[str, int]:
    """Write the base..candidate diff to ``dest`` and return its ``sha256:`` digest and size.

    The flags pin every input git would otherwise take from configuration, so repeated calls
    produce byte-identical artifacts.
    """
    diff = run_git(
        [
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            f"{base_sha}..{candidate_sha}",
            "--",
        ],
        cwd=identity.toplevel,
    ).stdout
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(diff)
    os.replace(tmp, dest)
    return "sha256:" + hashlib.sha256(diff).hexdigest(), len(diff)


def read_diff_page(path: Path, offset: int, length: int) -> bytes:
    """Read at most ``length`` bytes of a diff artifact starting at ``offset``."""
    if offset < 0:
        raise ValueError("offset must not be negative")
    if length <= 0:
        raise ValueError("length must be positive")
    if length > MAX_DIFF_PAGE:
        raise ValueError(f"length must not exceed {MAX_DIFF_PAGE}")
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def snapshot_working_tree(
    identity: RepositoryIdentity,
    *,
    expected_head: str,
    paths: Sequence[str],
    snapshot_id: str,
) -> str:
    """Record the root working tree as a commit under ``refs/taskspindle/snapshots/``.

    An alternate ``GIT_INDEX_FILE`` is used throughout, so the repository's real index and its
    ``git status`` output are byte-for-byte unchanged by this call.
    """
    if current_head(identity.toplevel) != expected_head:
        raise GitError("TARGET_MOVED", "the repository moved since the snapshot was requested")

    scratch = Path(tempfile.mkdtemp(prefix="taskspindle-index-"))
    try:
        env = {"GIT_INDEX_FILE": str(scratch / "index")}
        run_git(["read-tree", "HEAD"], cwd=identity.toplevel, env=env)
        add_args = ["add", "-A"] if list(paths) == ["."] else ["add", "-A", "--", *paths]
        run_git(add_args, cwd=identity.toplevel, env=env)
        tree = _text(run_git(["write-tree"], cwd=identity.toplevel, env=env).stdout)
    finally:
        index = scratch / "index"
        if index.exists():
            index.unlink()
        scratch.rmdir()

    sha = _text(
        run_git(
            ["commit-tree", tree, "-p", "HEAD", "-m", "TaskSpindle review snapshot"],
            cwd=identity.toplevel,
            env=_SPINDLE_IDENTITY,
        ).stdout
    )
    run_git(["update-ref", f"refs/taskspindle/snapshots/{snapshot_id}", sha], cwd=identity.toplevel)
    return sha


def worktree_is_clean(path: Path) -> bool:
    """True when ``git status --porcelain`` in ``path`` is empty."""
    return not run_git(["status", "--porcelain"], cwd=path).stdout.strip()


def remove_worktree(identity: RepositoryIdentity, path: Path, *, force: bool = False) -> None:
    """Remove a worktree, refusing to discard uncommitted work unless ``force`` is set."""
    if not force and not worktree_is_clean(path):
        raise GitError("WORKTREE_DIRTY", f"worktree has uncommitted changes: {path}")
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    run_git(args, cwd=identity.toplevel)
    run_git(["worktree", "prune"], cwd=identity.toplevel)


def delete_task_refs(identity: RepositoryIdentity, task_id: str) -> list[str]:
    """Delete every ref under ``refs/taskspindle/<task_id>/`` and return the names removed."""
    listing = run_git(
        ["for-each-ref", "--format=%(refname)", f"refs/taskspindle/{task_id}/"],
        cwd=identity.toplevel,
    )
    names = [line for line in _text(listing.stdout).splitlines() if line]
    for name in names:
        run_git(["update-ref", "-d", name], cwd=identity.toplevel)
    return names
