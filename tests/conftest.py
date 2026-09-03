"""Shared fixtures for the git-layer tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from taskspindle.repos import run_git


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Path]:
    """Return a factory that builds a real git repository with two commits."""

    def _make(name: str = "repo") -> Path:
        root = tmp_path / name
        root.mkdir(parents=True)
        run_git(["init", "-q", "-b", "main"], cwd=root)
        run_git(["config", "user.name", "Test User"], cwd=root)
        run_git(["config", "user.email", "test@example.com"], cwd=root)
        (root / "README.md").write_text("readme\n")
        run_git(["add", "-A"], cwd=root)
        run_git(["commit", "-q", "-m", "Add readme"], cwd=root)
        (root / "file.txt").write_text("one\ntwo\nthree\n")
        run_git(["add", "-A"], cwd=root)
        run_git(["commit", "-q", "-m", "Add file"], cwd=root)
        return root

    return _make
