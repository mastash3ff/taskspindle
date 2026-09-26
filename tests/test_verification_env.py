"""Verification commands find a Python with pytest, never TaskSpindle's own runtime venv first."""

from __future__ import annotations

import re
from pathlib import Path

from taskspindle import accept, integration

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_BIN = Path("/opt/taskspindle/.venv/bin")
IMAGE_PATH = "/opt/taskspindle/.venv/bin:/usr/local/bin:/usr/bin:/bin"


def test_in_a_container_the_runtime_venv_moves_behind_the_system_python() -> None:
    env = {"PATH": IMAGE_PATH, "HOME": "/home/u", "TMPDIR": "/state/tasks/t/tmp"}
    result = integration.verification_env(env, container=True, own_bin=VENV_BIN)
    assert result["PATH"] == "/usr/local/bin:/usr/bin:/bin:/opt/taskspindle/.venv/bin"
    assert {k: v for k, v in result.items() if k != "PATH"} == {
        "HOME": "/home/u", "TMPDIR": "/state/tasks/t/tmp",
    }
    assert env["PATH"] == IMAGE_PATH  # the caller's mapping is not modified


def test_outside_a_container_nothing_changes() -> None:
    env = {"PATH": f"{VENV_BIN}:/usr/bin"}
    assert integration.verification_env(env, container=False, own_bin=VENV_BIN) == env


def test_a_path_without_the_venv_or_a_process_outside_any_venv_is_left_alone(monkeypatch) -> None:
    env = {"PATH": "/usr/local/bin:/usr/bin"}
    assert integration.verification_env(env, container=True, own_bin=VENV_BIN) == env
    monkeypatch.setattr(integration.sys, "prefix", "/usr")
    monkeypatch.setattr(integration.sys, "base_prefix", "/usr")
    assert integration.verification_env({"PATH": IMAGE_PATH}, container=True)["PATH"] == IMAGE_PATH


def test_duplicate_and_trailing_slash_entries_are_collapsed_to_one_at_the_end() -> None:
    env = {"PATH": f"{VENV_BIN}/:/usr/bin:{VENV_BIN}::/bin"}
    result = integration.verification_env(env, container=True, own_bin=VENV_BIN)
    assert result["PATH"] == f"/usr/bin:/bin:{VENV_BIN}/"


def test_the_default_venv_is_the_one_this_process_runs_from(monkeypatch) -> None:
    monkeypatch.setattr(integration.sys, "prefix", "/opt/taskspindle/.venv")
    monkeypatch.setattr(integration.sys, "base_prefix", "/usr/local")
    result = integration.verification_env({"PATH": IMAGE_PATH}, container=True)
    assert result["PATH"].split(":")[0] == "/usr/local/bin"


def test_root_verification_in_an_accept_container_uses_the_same_order(monkeypatch) -> None:
    monkeypatch.setattr(integration.sys, "prefix", "/opt/taskspindle/.venv")
    monkeypatch.setattr(integration.sys, "base_prefix", "/usr/local")
    inside = accept.check_env({"PATH": IMAGE_PATH, "HOME": "/h",
                               integration.WORKER_CONTAINER_ENV: "1", "SECRET": "x"})
    assert inside == {"PATH": "/usr/local/bin:/usr/bin:/bin:/opt/taskspindle/.venv/bin",
                      "HOME": "/h", "TERM": "dumb", "CI": "1"}
    outside = accept.check_env({"PATH": IMAGE_PATH, "HOME": "/h"})
    assert outside["PATH"] == IMAGE_PATH


def test_worker_checks_run_under_the_reordered_path(tmp_path: Path, monkeypatch) -> None:
    """End to end through run_verification: the command sees the system bin first."""
    fake_venv = tmp_path / "venv" / "bin"
    fake_system = tmp_path / "system" / "bin"
    for directory, label in ((fake_venv, "venv"), (fake_system, "system")):
        directory.mkdir(parents=True)
        tool = directory / "whichpython"
        tool.write_text(f"#!/bin/sh\necho {label}\n")
        tool.chmod(0o755)
    env = {"PATH": f"{fake_venv}:{fake_system}:/usr/bin:/bin"}
    [check] = integration.run_verification(
        tmp_path, ["whichpython"], timeout_s=30,
        env=integration.verification_env(env, container=True, own_bin=fake_venv),
    )
    assert check.stdout_tail.strip() == "system"


def test_the_image_pins_pytest_for_the_system_python_exactly_as_uv_lock_does() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text()
    lock = (REPO_ROOT / "uv.lock").read_text()
    pins = re.findall(r"^([a-z0-9_-]+)==(\S+) --hash=sha256:([0-9a-f]{64})$", dockerfile, re.M)
    assert {name for name, _, _ in pins} == {"pytest", "iniconfig", "packaging", "pluggy",
                                              "pygments"}
    for name, version, digest in pins:
        block = re.search(
            rf'^\[\[package\]\]\nname = "{re.escape(name)}"\nversion = "([^"]+)"\n(.*?)(?=^\[\[package\]\])',
            lock, re.M | re.S,
        )
        assert block is not None, name
        assert block.group(1) == version, name
        assert f"sha256:{digest}" in block.group(2), name
    # Installed into the image's system Python, and never into the runtime venv.
    assert "--python /usr/local/bin/python3" in dockerfile
    assert "! /opt/taskspindle/.venv/bin/python -c 'import pytest'" in dockerfile
