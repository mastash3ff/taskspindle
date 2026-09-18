"""Coordinator-supplied context files, read under the operator's allowlist.

A ``start_task`` request may name files whose contents are copied into the worker's first
turn, the way the Claude Code plugins for Codex and Grok import the host transcript into the
delegated session. The server reads them, not the worker: the worker only ever sees the copied
text, so the allowlist and the hardened read below are the whole defence.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import ContextFilesConfig
from .service import INVALID_REQUEST, TaskSpindleError

__all__ = [
    "CONTEXT_FILES_DISABLED", "CONTEXT_FILES_TOO_MANY", "CONTEXT_FILE_TOO_LARGE",
    "CONTEXT_FILE_UNSAFE", "CONTEXT_PATH_DENIED", "ContextFile", "read_context_files",
]

CONTEXT_FILES_DISABLED = "CONTEXT_FILES_DISABLED"
CONTEXT_PATH_DENIED = "CONTEXT_PATH_DENIED"
CONTEXT_FILE_UNSAFE = "CONTEXT_FILE_UNSAFE"
CONTEXT_FILE_TOO_LARGE = "CONTEXT_FILE_TOO_LARGE"
CONTEXT_FILES_TOO_MANY = "CONTEXT_FILES_TOO_MANY"


@dataclass(frozen=True)
class ContextFile:
    """One file as it will appear in the prompt."""

    path: str
    text: str
    size: int


def _refuse(code: str, message: str, path: str | None = None) -> TaskSpindleError:
    details: dict[str, object] = {"code": code}
    if path is not None:
        details["path"] = path
    return TaskSpindleError(INVALID_REQUEST, message, details=details)


def read_context_files(
    paths: Sequence[str], config: ContextFilesConfig | None
) -> list[ContextFile]:
    """Read every path, or raise ``INVALID_REQUEST`` naming the first one that is refused.

    A path is refused when the handoff is not configured, when its resolved location is not
    under an allowlisted root, when any component of it is a symlink, when it is not a regular
    file with a single link, when it contains a NUL byte, or when it or the running total is
    over the configured size. Nothing is read from a path that fails the location checks.
    """
    if not paths:
        return []
    if config is None:
        raise _refuse(
            CONTEXT_FILES_DISABLED,
            "context_files is not enabled; the operator must configure [context_files] roots",
        )
    if len(paths) > config.max_files:
        raise _refuse(
            CONTEXT_FILES_TOO_MANY,
            f"at most {config.max_files} context files may be handed to one task",
        )
    files: list[ContextFile] = []
    total = 0
    for raw in paths:
        given = Path(raw)
        try:
            resolved = given.resolve(strict=True)
        except OSError as exc:
            raise _refuse(CONTEXT_PATH_DENIED, f"context file cannot be resolved: {raw}", raw) from exc
        if not any(resolved == root or root in resolved.parents for root in config.roots):
            raise _refuse(CONTEXT_PATH_DENIED, f"context file is outside every allowed root: {raw}", raw)
        if resolved != given:
            raise _refuse(
                CONTEXT_FILE_UNSAFE, f"context file path must be canonical, with no symlinks: {raw}", raw
            )
        try:
            fd = os.open(raw, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError as exc:
            raise _refuse(CONTEXT_FILE_UNSAFE, f"context file cannot be opened: {raw}", raw) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _refuse(
                    CONTEXT_FILE_UNSAFE, f"context file must be a regular file with one link: {raw}", raw
                )
            if info.st_size > config.max_file_bytes:
                raise _refuse(
                    CONTEXT_FILE_TOO_LARGE,
                    f"context file exceeds {config.max_file_bytes} bytes: {raw}",
                    raw,
                )
            data = os.read(fd, config.max_file_bytes + 1)
        finally:
            os.close(fd)
        if len(data) > config.max_file_bytes:
            raise _refuse(
                CONTEXT_FILE_TOO_LARGE, f"context file exceeds {config.max_file_bytes} bytes: {raw}", raw
            )
        if b"\x00" in data:
            raise _refuse(CONTEXT_FILE_UNSAFE, f"context file must be text, not binary: {raw}", raw)
        total += len(data)
        if total > config.max_total_bytes:
            raise _refuse(
                CONTEXT_FILE_TOO_LARGE,
                f"context files exceed {config.max_total_bytes} bytes in total",
                raw,
            )
        files.append(ContextFile(path=raw, text=data.decode("utf-8", errors="replace"), size=len(data)))
    return files
