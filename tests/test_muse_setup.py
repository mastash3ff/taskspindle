"""Muse staging uses only explicit native bytes and offline metadata commands."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from taskspindle import muse_setup
from taskspindle.config import Paths
from taskspindle.muse import MUSE_BUILD, MUSE_SCHEMA_FINGERPRINT, MUSE_VERSION
from taskspindle.setup import SetupError


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config" / "config.toml", state_dir=tmp_path / "state",
        data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime",
    )


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    path = tmp_path / "native-muse"
    path.write_bytes(b"\x7fELFfixture bytes, never executed")
    path.chmod(0o700)
    return path


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1:] == ["--version"]:
            output = f"Muse Code {MUSE_VERSION} ({MUSE_BUILD})\n"
        else:
            assert command[1:4] == ["schema", "generate-json-schema", "--out"]
            root = Path(command[4])
            root.mkdir()
            (root / "manifest.json").write_text(json.dumps({
                "experimental": False, "schemaVersion": 1, "fingerprint": MUSE_SCHEMA_FINGERPRINT,
            }))
            (root / "msp.schema.json").write_text(json.dumps({"methods": {"initialize": {}}}))
            output = "wrote manifest.json, msp.schema.json\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(muse_setup.subprocess, "run", run)
    return calls


def test_verify_exact_build_and_schema_in_isolated_environment(binary, offline, monkeypatch):
    monkeypatch.setenv("META_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("MUSE_BASE_URL", "https://must-not-be-inherited.invalid")
    result = muse_setup.verify_muse_binary(binary)
    assert result == {
        "version": MUSE_VERSION, "build": MUSE_BUILD,
        "schema_fingerprint": MUSE_SCHEMA_FINGERPRINT,
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    assert len(offline) == 2
    for command, kwargs in offline:
        assert command[0] == str(binary)
        assert "META_API_KEY" not in kwargs["env"]
        assert "MUSE_BASE_URL" not in kwargs["env"]
        home = Path(kwargs["env"]["HOME"])
        assert kwargs["cwd"] == home
        assert kwargs["env"]["CODEX_HOME"] == str(home / ".codex")
        assert not home.exists()  # Temporary roots are removed after the checks.


def test_stage_verified_binary_and_leave_config_and_state_untouched(paths, binary, offline):
    paths.config_file.parent.mkdir()
    paths.config_file.write_text("existing config\n")
    result = muse_setup.install_muse_runtime(paths, binary)
    target = paths.runtime_dir / "muse"
    assert target.read_bytes() == binary.read_bytes()
    assert not target.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o700
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["created_config"] is False
    assert result["qualification"]["enabled"] is False
    assert result["command"] == [str(target)]
    assert paths.config_file.read_text() == "existing config\n"
    assert not paths.state_dir.exists()
    assert not paths.data_dir.exists()
    assert len(list(paths.runtime_dir.iterdir())) == 1
    assert all(Path(command[0]).name.startswith(".muse-stage-") for command, _ in offline)


def test_missing_config_is_not_created_and_staging_can_repeat(paths, binary, offline):
    muse_setup.install_muse_runtime(paths, binary)
    muse_setup.install_muse_runtime(paths, paths.runtime_dir / "muse")
    assert not paths.config_file.parent.exists()
    assert (paths.runtime_dir / "muse").read_bytes() == binary.read_bytes()


@pytest.mark.parametrize("kind", ["relative", "wrapper", "symlink", "not-executable"])
def test_refuse_non_native_or_implicit_source_before_execution(tmp_path, binary, offline, kind):
    if kind == "relative":
        binary = Path("muse")
    elif kind == "wrapper":
        binary.write_text("#!/bin/sh\nexec some-other-muse\n")
    elif kind == "symlink":
        link = tmp_path / "muse"
        link.symlink_to(binary)
        binary = link
    else:
        binary.chmod(0o600)
    with pytest.raises(SetupError):
        muse_setup.verify_muse_binary(binary)
    assert not offline


def test_wrong_build_preserves_previous_install(paths, binary, monkeypatch):
    paths.runtime_dir.mkdir(mode=0o700)
    target = paths.runtime_dir / "muse"
    target.write_bytes(b"previous runtime")
    monkeypatch.setattr(muse_setup.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, "Muse Code 999.0.0 (unknown)\n", ""))
    with pytest.raises(SetupError, match="exact pinned build"):
        muse_setup.install_muse_runtime(paths, binary)
    assert target.read_bytes() == b"previous runtime"
    assert list(paths.runtime_dir.iterdir()) == [target]


@pytest.mark.parametrize("damage", ["fingerprint", "experimental", "missing-schema", "malformed"])
def test_refuse_incompatible_or_incomplete_schema(binary, offline, monkeypatch, damage):
    original = muse_setup.subprocess.run

    def run(command, **kwargs):
        result = original(command, **kwargs)
        if command[1] == "schema":
            root = Path(command[4])
            manifest = root / "manifest.json"
            payload = json.loads(manifest.read_text())
            if damage == "missing-schema":
                (root / "msp.schema.json").unlink()
            elif damage == "malformed":
                manifest.write_text("not JSON")
            else:
                payload[damage] = True if damage == "experimental" else "sha256:other"
                manifest.write_text(json.dumps(payload))
        return result

    monkeypatch.setattr(muse_setup.subprocess, "run", run)
    with pytest.raises(SetupError):
        muse_setup.verify_muse_binary(binary)


def test_timeout_is_setup_error_and_does_not_enable_any_route(binary, monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(muse_setup.subprocess, "run", run)
    with pytest.raises(SetupError, match="offline --version check failed"):
        muse_setup.verify_muse_binary(binary)


def test_symlink_runtime_is_refused(paths, binary, offline, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    paths.runtime_dir.symlink_to(other, target_is_directory=True)
    with pytest.raises(SetupError, match="real directory"):
        muse_setup.install_muse_runtime(paths, binary)
    assert not list(other.iterdir())
    assert not offline
