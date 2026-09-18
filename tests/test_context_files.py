"""The hardened read behind ``start_task(context_files=...)``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from taskspindle import context_files as cf
from taskspindle.config import ContextFilesConfig
from taskspindle.service import INVALID_REQUEST, TaskSpindleError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    base.mkdir()
    (base / "notes.md").write_text("design notes\n", encoding="utf-8")
    return base


def config(root: Path, **limits: int) -> ContextFilesConfig:
    return ContextFilesConfig(roots=(root.resolve(),), **limits)


def refused(paths: list[str], cfg: ContextFilesConfig | None) -> dict[str, object]:
    with pytest.raises(TaskSpindleError) as caught:
        cf.read_context_files(paths, cfg)
    assert caught.value.code == INVALID_REQUEST
    return dict(caught.value.details or {})


def test_an_allowed_file_is_read_with_its_size(root: Path) -> None:
    [item] = cf.read_context_files([str(root / "notes.md")], config(root))
    assert item == cf.ContextFile(path=str(root / "notes.md"), text="design notes\n", size=13)


def test_no_paths_means_no_files_even_when_disabled() -> None:
    assert cf.read_context_files([], None) == []


def test_disabled_config_refuses_before_touching_the_path(root: Path) -> None:
    details = refused([str(root / "missing.md")], None)
    assert details["code"] == cf.CONTEXT_FILES_DISABLED


def test_a_file_outside_every_root_is_denied(root: Path, tmp_path: Path) -> None:
    other = tmp_path / "elsewhere.md"
    other.write_text("x", encoding="utf-8")
    details = refused([str(other)], config(root))
    assert details == {"code": cf.CONTEXT_PATH_DENIED, "path": str(other)}


def test_a_sibling_directory_sharing_the_root_prefix_is_denied(root: Path, tmp_path: Path) -> None:
    evil = tmp_path / "root-evil"
    evil.mkdir()
    (evil / "notes.md").write_text("x", encoding="utf-8")
    details = refused([str(evil / "notes.md")], config(root))
    assert details["code"] == cf.CONTEXT_PATH_DENIED


def test_a_missing_file_is_denied(root: Path) -> None:
    details = refused([str(root / "nope.md")], config(root))
    assert details["code"] == cf.CONTEXT_PATH_DENIED


def test_a_symlinked_file_is_unsafe_even_inside_the_root(root: Path) -> None:
    link = root / "link.md"
    link.symlink_to(root / "notes.md")
    details = refused([str(link)], config(root))
    assert details["code"] == cf.CONTEXT_FILE_UNSAFE


def test_a_symlinked_parent_is_unsafe(root: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    details = refused([str(alias / "notes.md")], config(root))
    assert details["code"] in {cf.CONTEXT_FILE_UNSAFE, cf.CONTEXT_PATH_DENIED}


def test_a_symlink_escaping_the_root_is_denied(root: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("s", encoding="utf-8")
    link = root / "escape.md"
    link.symlink_to(secret)
    details = refused([str(link)], config(root))
    assert details["code"] == cf.CONTEXT_PATH_DENIED


def test_a_hardlinked_file_is_unsafe(root: Path, tmp_path: Path) -> None:
    twin = root / "twin.md"
    os.link(root / "notes.md", twin)
    details = refused([str(twin)], config(root))
    assert details["code"] == cf.CONTEXT_FILE_UNSAFE


def test_a_directory_is_unsafe(root: Path) -> None:
    details = refused([str(root)], config(root))
    assert details["code"] == cf.CONTEXT_FILE_UNSAFE


def test_binary_content_is_unsafe(root: Path) -> None:
    blob = root / "blob.bin"
    blob.write_bytes(b"ab\x00cd")
    details = refused([str(blob)], config(root))
    assert details["code"] == cf.CONTEXT_FILE_UNSAFE


def test_a_file_over_the_per_file_cap_is_too_large(root: Path) -> None:
    big = root / "big.md"
    big.write_text("x" * 100, encoding="utf-8")
    details = refused([str(big)], config(root, max_file_bytes=99))
    assert details == {"code": cf.CONTEXT_FILE_TOO_LARGE, "path": str(big)}


def test_files_over_the_total_cap_are_too_large(root: Path) -> None:
    second = root / "second.md"
    second.write_text("y" * 10, encoding="utf-8")
    cfg = config(root, max_file_bytes=64, max_total_bytes=20)
    details = refused([str(root / "notes.md"), str(second)], cfg)
    assert details["code"] == cf.CONTEXT_FILE_TOO_LARGE
    assert details["path"] == str(second)


def test_more_files_than_allowed_are_refused_before_reading(root: Path) -> None:
    details = refused([str(root / "a"), str(root / "b")], config(root, max_files=1))
    assert details["code"] == cf.CONTEXT_FILES_TOO_MANY


def test_invalid_utf8_is_replaced_not_refused(root: Path) -> None:
    latin = root / "latin.txt"
    latin.write_bytes(b"caf\xe9")
    [item] = cf.read_context_files([str(latin)], config(root))
    assert item.text == "caf�"
    assert item.size == 4
