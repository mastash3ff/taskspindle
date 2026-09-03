"""Git process runner, repository identity, root snapshots and path scoping.

This module owns the only ``git`` invocation in the package: every other module calls
:func:`run_git`. Nothing here imports the store or the service layer; these are pure helpers
over filesystem paths.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitError",
    "RepositoryIdentity",
    "RootSnapshot",
    "compare_snapshots",
    "current_branch",
    "current_head",
    "normalize_prefixes",
    "overlapping_dirty_paths",
    "path_in_scope",
    "resolve_repository",
    "run_git",
    "snapshot_root",
]

#: Config overrides forced onto every invocation so that user or system configuration cannot
#: change line endings, run hooks, or block on a signing key.
_FORCED_CONFIG = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgsign=false",
)


class GitError(RuntimeError):
    """A git failure carrying a stable ``code`` for the transport layer.

    Codes raised by this package: ``GIT_FAILED``, ``NOT_A_REPOSITORY``, ``EMPTY_REPOSITORY``,
    ``NO_CHANGES``, ``TARGET_MOVED``, ``TARGET_DIRTY``, ``WORKTREE_DIRTY``, ``MERGE_CONFLICT``,
    ``JOURNAL_MISMATCH``.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        argv: Sequence[str] = (),
        returncode: int | None = None,
        stderr: str = "",
        paths: Sequence[str] = (),
    ) -> None:
        self.code = code
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        self.paths = tuple(paths)
        super().__init__(message or stderr.strip() or code)


def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 60,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run one git command in ``cwd`` and return the completed process.

    The environment is rebuilt from scratch so that git behaves identically regardless of the
    caller's shell. ``check=True`` turns a nonzero exit into ``GitError("GIT_FAILED")``.
    """
    argv = ["git", *_FORCED_CONFIG, "-C", str(cwd), *args]
    child_env = {
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", ""),
    }
    if env:
        child_env.update(env)
    proc = subprocess.run(
        argv,
        env=child_env,
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL if input_bytes is None else None,
        input=input_bytes,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(
            "GIT_FAILED",
            argv=argv,
            returncode=proc.returncode,
            stderr=proc.stderr.decode("utf-8", "replace"),
        )
    return proc


def _text(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


@dataclass(frozen=True)
class RepositoryIdentity:
    """Where a repository lives and which root commit it descends from."""

    toplevel: Path
    common_dir: Path
    root_commit: str

    @property
    def key(self) -> str:
        """Canonical identity string used as the grant key."""
        return f"{self.common_dir}\n{self.root_commit}"


def resolve_repository(path: Path) -> RepositoryIdentity:
    """Resolve ``path`` to the repository that contains it."""
    if not path.exists():
        raise GitError("NOT_A_REPOSITORY", f"path does not exist: {path}")
    probe = run_git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    if probe.returncode != 0 or not probe.stdout.strip():
        raise GitError(
            "NOT_A_REPOSITORY",
            f"not a git repository: {path}",
            stderr=probe.stderr.decode("utf-8", "replace"),
        )
    toplevel = Path(_text(probe)).resolve()

    raw_common = _text(run_git(["rev-parse", "--git-common-dir"], cwd=toplevel))
    common = Path(raw_common)
    if not common.is_absolute():
        common = toplevel / common
    common_dir = common.resolve()

    roots = run_git(["rev-list", "--max-parents=0", "HEAD"], cwd=toplevel, check=False)
    if roots.returncode != 0 or not roots.stdout.strip():
        raise GitError("EMPTY_REPOSITORY", f"repository has no commits: {toplevel}")
    root_commit = sorted(_text(roots).splitlines())[0]
    return RepositoryIdentity(toplevel=toplevel, common_dir=common_dir, root_commit=root_commit)


def current_head(cwd: Path) -> str:
    """Return the commit sha HEAD points at."""
    return _text(run_git(["rev-parse", "HEAD"], cwd=cwd))


def current_branch(cwd: Path) -> str | None:
    """Return the checked out branch name, or ``None`` when HEAD is detached."""
    proc = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    name = _text(proc)
    return name or None


@dataclass(frozen=True)
class RootSnapshot:
    """HEAD, branch and a content fingerprint for every dirty path in the root repository."""

    head: str
    branch: str | None
    dirty: dict[str, str]


def _fingerprint(toplevel: Path, rel: str) -> str:
    target = toplevel / rel
    if target.is_symlink():
        link = os.readlink(target).encode("utf-8", "surrogateescape")
        return hashlib.blake2b(link, digest_size=32).hexdigest()
    if target.is_dir():
        return "dir"
    if not target.exists():
        return "deleted"
    return hashlib.blake2b(target.read_bytes(), digest_size=32).hexdigest()


def _porcelain_paths(raw: bytes) -> list[str]:
    """Parse ``status --porcelain=v1 -z`` output into the paths it mentions.

    In ``-z`` mode rename and copy entries emit the new path first and the original path as the
    next NUL-terminated field; both are reported.
    """
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index].decode("utf-8", "surrogateescape")
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if ("R" in status or "C" in status) and index < len(fields):
            paths.append(fields[index].decode("utf-8", "surrogateescape"))
            index += 1
    return paths


def snapshot_root(toplevel: Path) -> RootSnapshot:
    """Capture HEAD, branch and fingerprints of every dirty path in the repository."""
    status = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=toplevel,
    )
    dirty = {path: _fingerprint(toplevel, path) for path in _porcelain_paths(status.stdout)}
    return RootSnapshot(head=current_head(toplevel), branch=current_branch(toplevel), dirty=dirty)


def compare_snapshots(before: RootSnapshot, after: RootSnapshot) -> list[str]:
    """Return the paths that differ between two snapshots, ``"HEAD"`` first if HEAD moved."""
    changed = sorted(
        path
        for path in set(before.dirty) | set(after.dirty)
        if before.dirty.get(path) != after.dirty.get(path)
    )
    if before.head != after.head:
        return ["HEAD", *changed]
    return changed


def normalize_prefixes(prefixes: Sequence[str]) -> tuple[str, ...]:
    """Normalise repo-relative scope prefixes, rejecting anything that could escape the repo."""
    normalized: list[str] = []
    for raw in prefixes:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("scope prefix must be a non-empty string")
        prefix = raw.strip()
        if prefix.startswith("/"):
            raise ValueError(f"scope prefix must be repository-relative: {raw!r}")
        while prefix.startswith("./"):
            prefix = prefix[2:]
        prefix = prefix.rstrip("/")
        if prefix in {"", "."}:
            normalized.append(".")
            continue
        segments = prefix.split("/")
        if any(segment in {"", "..", "."} for segment in segments):
            raise ValueError(f"invalid scope prefix: {raw!r}")
        normalized.append(prefix)
    seen: dict[str, None] = {}
    for prefix in normalized:
        seen.setdefault(prefix, None)
    return tuple(seen)


def path_in_scope(path: str, prefixes: Sequence[str]) -> bool:
    """True when ``path`` sits at or under one of ``prefixes`` on a path-segment boundary."""
    for prefix in prefixes:
        if prefix == ".":
            return True
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def overlapping_dirty_paths(snapshot: RootSnapshot, prefixes: Sequence[str]) -> list[str]:
    """Return the snapshot's dirty paths that fall inside ``prefixes``."""
    scope = normalize_prefixes(prefixes)
    return sorted(path for path in snapshot.dirty if path_in_scope(path, scope))
