"""Merge-tree probe, journaled apply, verification and the Codex-owned root commit.

The probe never touches the root index or working tree. The only functions that do are
:func:`stage_candidate`, :func:`commit_staged` and :func:`abort_staged`, and each one is guarded
by the journal so an interrupted apply can be recognised and undone.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .repos import GitError, RepositoryIdentity, current_head, porcelain_paths, run_git

__all__ = [
    "CheckResult",
    "Journal",
    "ProbeResult",
    "abort_staged",
    "commit_landed",
    "commit_staged",
    "probe_merge",
    "recover_journal",
    "run_verification",
    "stage_candidate",
]

#: Phases a journal can be in, in the order they are entered. ``committing`` is written before
#: ``git commit`` runs, so a crash inside the commit itself is told apart from one before it.
PHASES = ("probing", "staged", "verified", "committing", "committed")

#: How much of a check's output is retained.
TAIL_CHARS = 4096


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of a dry-run merge: a tree when clean, conflicted paths when not."""

    clean: bool
    tree: str | None
    conflicts: tuple[str, ...]


def probe_merge(
    identity: RepositoryIdentity,
    *,
    base_sha: str,
    target_sha: str,
    candidate_sha: str,
) -> ProbeResult:
    """Merge the candidate onto the target in memory and report whether it applies cleanly.

    ``git merge-tree --write-tree`` "performs a merge, but does not make any new commits and does
    not read from or write to either the working tree or index" (git-merge-tree(1)), so this is
    safe to run against a repository the operator is actively working in.

    With ``-z`` the tree OID "section is always followed by a newline (or NUL if -z is passed)",
    the conflicted filenames are each NUL-terminated, and the informational messages section
    "begin[s] ... with a NUL character instead of a newline" (git-merge-tree(1), OPTIONS and
    OUTPUT). So the fields are: the tree, then the conflicted paths, then an empty field marking
    the start of the messages we discard.
    """
    proc = run_git(
        [
            "merge-tree",
            "--write-tree",
            "--name-only",
            "-z",
            f"--merge-base={base_sha}",
            target_sha,
            candidate_sha,
        ],
        cwd=identity.toplevel,
        check=False,
    )
    fields = proc.stdout.split(b"\0")
    if proc.returncode == 0:
        return ProbeResult(clean=True, tree=fields[0].decode().strip(), conflicts=())
    if proc.returncode == 1:
        conflicts: list[str] = []
        for field in fields[1:]:
            if field == b"":
                break
            conflicts.append(field.decode("utf-8", "surrogateescape"))
        return ProbeResult(clean=False, tree=fields[0].decode().strip(), conflicts=tuple(conflicts))
    raise GitError(
        "GIT_FAILED",
        "merge-tree could not run",
        returncode=proc.returncode,
        stderr=proc.stderr.decode("utf-8", "replace"),
    )


@dataclass(frozen=True)
class Journal:
    """What an in-flight apply is doing, so a crash can be recovered from on the next run."""

    task_id: str
    phase: str
    target_head: str
    candidate_sha: str
    #: The paths the candidate touches. Nothing outside them is ever reset or removed, so an
    #: operator edit made while the accept was in flight can never be discarded by a recovery.
    changed_paths: tuple[str, ...] = ()


def _git_dir(identity: RepositoryIdentity) -> Path:
    raw = run_git(["rev-parse", "--absolute-git-dir"], cwd=identity.toplevel).stdout
    return Path(raw.decode("utf-8", "replace").strip())


def _dirty_paths(identity: RepositoryIdentity) -> tuple[str, ...]:
    """Every path ``git status`` mentions, untracked files included."""
    status = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=identity.toplevel,
    )
    return tuple(porcelain_paths(status.stdout))


def _untracked_paths(identity: RepositoryIdentity) -> tuple[str, ...]:
    proc = run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=identity.toplevel)
    return tuple(f.decode("utf-8", "surrogateescape") for f in proc.stdout.split(b"\0") if f)


def _conflicted_paths(identity: RepositoryIdentity) -> tuple[str, ...]:
    proc = run_git(
        ["diff", "--name-only", "--diff-filter=U", "-z"],
        cwd=identity.toplevel,
        check=False,
    )
    return tuple(f.decode("utf-8", "surrogateescape") for f in proc.stdout.split(b"\0") if f)


def stage_candidate(
    identity: RepositoryIdentity,
    *,
    journal: Journal,
    save: Callable[[Journal], None],
) -> None:
    """Apply the candidate into the root working tree without committing it.

    The journal is written as ``staged`` *before* git is allowed to touch anything, so a crash
    mid-apply is always recoverable.
    """
    if run_git(["status", "--porcelain"], cwd=identity.toplevel).stdout.strip():
        raise GitError("TARGET_DIRTY", "the target repository has uncommitted changes")
    if current_head(identity.toplevel) != journal.target_head:
        raise GitError("TARGET_MOVED", "the target repository moved since the candidate was probed")

    save(replace(journal, phase="staged"))
    proc = run_git(
        ["cherry-pick", "--no-commit", journal.candidate_sha],
        cwd=identity.toplevel,
        check=False,
    )
    if proc.returncode != 0:
        conflicts = _conflicted_paths(identity)
        abort_staged(identity, journal=journal)
        raise GitError(
            "MERGE_CONFLICT",
            "the candidate does not apply cleanly to the target",
            returncode=proc.returncode,
            stderr=proc.stderr.decode("utf-8", "replace"),
            paths=conflicts,
        )


@dataclass(frozen=True)
class CheckResult:
    """One verification command and what it did."""

    command: str
    exit_code: int
    ok: bool
    duration_ms: int
    stdout_tail: str
    stderr_tail: str


def _tail(raw: bytes | None) -> str:
    if not raw:
        return ""
    return raw.decode("utf-8", "replace")[-TAIL_CHARS:]


def run_verification(
    cwd: Path,
    commands: Sequence[str],
    *,
    timeout_s: int,
    env: Mapping[str, str],
) -> list[CheckResult]:
    """Run verification commands in order, stopping at the first failure.

    ``env`` is supplied whole by the caller; nothing is inherited from this process.
    """
    results: list[CheckResult] = []
    for command in commands:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                env=dict(env),
                capture_output=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            results.append(
                CheckResult(
                    command=command,
                    exit_code=-1,
                    ok=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    stdout_tail=_tail(exc.stdout),
                    stderr_tail=f"timed out after {timeout_s}s",
                )
            )
            break
        result = CheckResult(
            command=command,
            exit_code=proc.returncode,
            ok=proc.returncode == 0,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout_tail=_tail(proc.stdout),
            stderr_tail=_tail(proc.stderr),
        )
        results.append(result)
        if not result.ok:
            break
    return results


def commit_staged(
    identity: RepositoryIdentity,
    *,
    journal: Journal,
    save: Callable[[Journal], None],
    message: str,
) -> str:
    """Commit the staged candidate in the root repository and return the new sha.

    Author and committer come from the repository's own configuration: this commit belongs to the
    operator, not to TaskSpindle.

    ``committing`` is journalled before git is asked to commit, so a crash between the two is
    recognisable: the recovery can look at ``HEAD^`` and see whether the commit landed.
    """
    save(replace(journal, phase="committing"))
    run_git(["commit", "-m", message], cwd=identity.toplevel)
    sha = current_head(identity.toplevel)
    save(replace(journal, phase="committed"))
    return sha


def commit_landed(identity: RepositoryIdentity, journal: Journal) -> bool:
    """True when the commit a ``committing`` journal was writing is the repository's HEAD."""
    head = current_head(identity.toplevel)
    if head == journal.target_head:
        return False
    proc = run_git(
        ["rev-parse", "--verify", "--quiet", "HEAD^"],
        cwd=identity.toplevel,
        check=False,
    )
    if proc.returncode != 0:
        return False
    return proc.stdout.decode("utf-8", "replace").strip() == journal.target_head


def abort_staged(identity: RepositoryIdentity, *, journal: Journal) -> None:
    """Undo a staged apply, leaving the repository exactly at ``journal.target_head``.

    A reset is only ever permitted when everything the working tree is dirty with belongs to the
    candidate: if ``git status`` mentions a path the journal does not list, somebody else's work
    is in the tree and discarding it is not this function's decision to make. When the reset is
    permitted, the candidate's own untracked files are removed too -- they are candidate content,
    and ``reset --hard`` leaves them behind.
    """
    if (_git_dir(identity) / "CHERRY_PICK_HEAD").exists():
        proc = run_git(["cherry-pick", "--abort"], cwd=identity.toplevel, check=False)
        if proc.returncode == 0:
            return
    if current_head(identity.toplevel) != journal.target_head:
        raise GitError(
            "JOURNAL_MISMATCH",
            f"HEAD is not at the journalled target {journal.target_head}; "
            "refusing to change anything",
        )
    owned = set(journal.changed_paths)
    foreign = sorted(path for path in _dirty_paths(identity) if path not in owned)
    if foreign:
        raise GitError(
            "JOURNAL_MISMATCH",
            "the target repository is dirty outside the candidate's own paths; "
            "refusing to change anything",
            paths=foreign,
        )
    run_git(["reset", "--hard", journal.target_head], cwd=identity.toplevel)
    for path in _untracked_paths(identity):
        if path in owned:
            (identity.toplevel / path).unlink(missing_ok=True)


def recover_journal(identity: RepositoryIdentity, journal: Journal) -> str:
    """Bring a journal recovered from disk back to a settled state.

    A ``probing`` journal is settled already: the probe is a pure ``merge-tree`` that never
    touches the index or the working tree, so there is nothing to undo and the operator's own
    uncommitted work is left exactly where it is. A ``committing`` journal is the one case where
    the repository has to be asked what happened: the commit either landed or it did not.
    """
    if journal.phase == "committed":
        return "committed"
    if journal.phase == "probing":
        return "aborted"
    if journal.phase == "committing" and commit_landed(identity, journal):
        return "committed"
    if journal.phase in PHASES:
        abort_staged(identity, journal=journal)
        return "aborted"
    raise ValueError(f"unknown journal phase: {journal.phase!r}")
