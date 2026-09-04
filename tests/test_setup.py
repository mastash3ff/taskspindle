"""Installing the pinned runtime, against a fake npm rather than a real registry."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import taskspindle
from taskspindle import setup as ts_setup
from taskspindle.config import Paths
from taskspindle.setup import ADAPTER_BIN, EXAMPLE_CONFIG, MANIFESTS, SetupError, install_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeNpm:
    """Lays out the tree a successful ``npm ci`` would have left behind.

    That includes the real shape ``npm`` leaves the launcher in: ``dist/index.js`` as the
    adapter's actual entry point, and ``node_modules/.bin/claude-agent-acp`` as a relative
    symlink to it, shebang ``#!/usr/bin/env node`` and all -- the exact thing setup is supposed
    to replace with a pinned-node shim.
    """

    def __init__(self, *, version: str = taskspindle.ADAPTER_VERSION, returncode: int = 0) -> None:
        self.version = version
        self.returncode = returncode
        self.calls: list[tuple[tuple[str, ...], str, dict[str, str]]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        capture_output: bool = True,
        text: bool = True,
        timeout: float = 0.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(command), cwd, dict(env)))
        if self.returncode == 0:
            modules = Path(cwd) / "node_modules"
            package = modules / taskspindle.ADAPTER_PACKAGE
            package.mkdir(parents=True, exist_ok=True)
            package.joinpath("package.json").write_text(
                json.dumps({"name": taskspindle.ADAPTER_PACKAGE, "version": self.version}),
                encoding="utf-8",
            )
            dist = package / "dist"
            dist.mkdir(exist_ok=True)
            dist.joinpath("index.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
            binaries = modules / ".bin"
            binaries.mkdir(parents=True, exist_ok=True)
            link = binaries / ADAPTER_BIN
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(Path("..") / taskspindle.ADAPTER_PACKAGE / "dist" / "index.js")
        return subprocess.CompletedProcess(list(command), self.returncode, "", "npm said no\n")


@pytest.fixture(autouse=True)
def fake_node(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Every test gets a deterministic ``node`` on PATH, standing in for a real nvm install."""
    bin_dir = tmp_path_factory.mktemp("node-bin")
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\necho v24.14.0\n", encoding="utf-8")
    node.chmod(0o755)
    monkeypatch.setattr(
        ts_setup.shutil, "which", lambda name: str(node) if name == "node" else None
    )
    return node


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config" / "taskspindle" / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "data" / "runtimes" / taskspindle.__version__,
    )


def test_a_successful_install_reports_the_pinned_adapter_and_lays_out_the_dirs(
    paths: Paths, fake_node: Path
) -> None:
    npm = FakeNpm()

    report = install_runtime(paths, runner=npm, parent_env={"PATH": "/usr/bin", "HOME": "/home/x"})

    assert report["adapter_version"] == taskspindle.ADAPTER_VERSION
    assert report["runtime_dir"] == str(paths.runtime_dir)
    assert report["node"] == str(fake_node)
    assert report["created_config"] is True
    for name in MANIFESTS:
        assert (paths.runtime_dir / name).read_bytes()
    for directory in (paths.runtime_dir, paths.state_dir, paths.data_dir):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_the_node_path_is_recorded_privately_next_to_the_runtime(paths: Paths, fake_node: Path) -> None:
    install_runtime(paths, runner=FakeNpm(), parent_env={"PATH": "/usr/bin"})

    node_path_file = paths.runtime_dir / "node-path"
    assert node_path_file.read_text(encoding="utf-8") == f"{fake_node}\n"
    assert node_path_file.stat().st_mode & 0o777 == 0o600


def test_the_launcher_symlink_is_replaced_with_a_shim_that_pins_the_node(
    paths: Paths, fake_node: Path
) -> None:
    install_runtime(paths, runner=FakeNpm(), parent_env={"PATH": "/usr/bin"})

    launcher = paths.runtime_dir / "node_modules" / ".bin" / ADAPTER_BIN
    entry = paths.runtime_dir / "node_modules" / taskspindle.ADAPTER_PACKAGE / "dist" / "index.js"

    assert not launcher.is_symlink()
    assert launcher.read_text(encoding="utf-8") == f'#!/bin/sh\nexec "{fake_node}" "{entry}" "$@"\n'
    assert launcher.stat().st_mode & 0o777 == 0o755


def test_rerunning_setup_rewrites_the_shim_and_node_path_idempotently(paths: Paths, fake_node: Path) -> None:
    install_runtime(paths, runner=FakeNpm(), parent_env={"PATH": "/usr/bin"})
    first = (paths.runtime_dir / "node_modules" / ".bin" / ADAPTER_BIN).read_text(encoding="utf-8")

    install_runtime(paths, runner=FakeNpm(), parent_env={"PATH": "/usr/bin"})
    launcher = paths.runtime_dir / "node_modules" / ".bin" / ADAPTER_BIN

    assert not launcher.is_symlink()
    assert launcher.read_text(encoding="utf-8") == first
    assert launcher.stat().st_mode & 0o777 == 0o755


def test_a_missing_node_is_reported_and_npm_still_ran(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts_setup.shutil, "which", lambda name: None)
    npm = FakeNpm()

    with pytest.raises(SetupError, match="node is not on PATH"):
        install_runtime(paths, runner=npm, parent_env={"PATH": "/usr/bin"})

    assert npm.calls  # npm ci already succeeded before node was resolved
    assert not (paths.runtime_dir / "node-path").exists()


def test_a_node_older_than_22_is_refused(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    old_node = paths.runtime_dir.parent / "old-node"
    old_node.parent.mkdir(parents=True, exist_ok=True)
    old_node.write_text("#!/bin/sh\necho v20.11.0\n", encoding="utf-8")
    old_node.chmod(0o755)
    monkeypatch.setattr(
        ts_setup.shutil, "which", lambda name: str(old_node) if name == "node" else None
    )

    with pytest.raises(SetupError, match="older than the required v22"):
        install_runtime(paths, runner=FakeNpm(), parent_env={"PATH": "/usr/bin"})


def test_npm_runs_in_the_runtime_dir_with_nothing_but_path_home_and_lang(paths: Paths) -> None:
    npm = FakeNpm()

    install_runtime(
        paths,
        npm="/usr/bin/npm",
        runner=npm,
        parent_env={"PATH": "/usr/bin", "HOME": "/home/x", "NPM_TOKEN": "secret", "LANG": "C"},
    )

    (command, cwd, env), = npm.calls
    assert command == ("/usr/bin/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    assert cwd == str(paths.runtime_dir)
    assert set(env) == {"PATH", "HOME", "LANG"}


def test_the_example_config_is_written_once_and_never_overwritten(paths: Paths) -> None:
    first = install_runtime(paths, runner=FakeNpm(), parent_env={"PATH": "/usr/bin"})
    assert first["created_config"] is True
    assert paths.config_file.read_text(encoding="utf-8") == EXAMPLE_CONFIG

    paths.config_file.write_text('[providers.mine]\nauth = "oauth"\n', encoding="utf-8")
    second = install_runtime(paths, runner=FakeNpm(), parent_env={"PATH": "/usr/bin"})

    assert second["created_config"] is False
    assert paths.config_file.read_text(encoding="utf-8") == '[providers.mine]\nauth = "oauth"\n'


def test_a_config_directory_that_already_exists_keeps_its_permissions(tmp_path: Path) -> None:
    """TASKSPINDLE_CONFIG can point anywhere, so setup must not re-permission somebody's home."""
    elsewhere = tmp_path / "somebodys-home"
    elsewhere.mkdir(mode=0o755)
    paths = Paths(
        config_file=elsewhere / "taskspindle.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )

    install_runtime(paths, runner=FakeNpm(), parent_env={"PATH": "/usr/bin"})

    assert paths.config_file.read_text(encoding="utf-8") == EXAMPLE_CONFIG
    assert elsewhere.stat().st_mode & 0o777 == 0o755


def test_an_adapter_at_the_wrong_version_is_refused(paths: Paths) -> None:
    with pytest.raises(SetupError, match=taskspindle.ADAPTER_VERSION):
        install_runtime(paths, runner=FakeNpm(version="0.1.0"), parent_env={"PATH": "/usr/bin"})


def test_a_failed_npm_reports_its_own_last_line(paths: Paths) -> None:
    with pytest.raises(SetupError, match="npm said no"):
        install_runtime(paths, runner=FakeNpm(returncode=1), parent_env={"PATH": "/usr/bin"})


def test_the_shipped_example_config_is_the_one_setup_writes() -> None:
    example = REPO_ROOT / "examples" / "config.toml"
    if not example.is_file():  # pragma: no cover - only outside a source checkout
        pytest.skip("examples/ is not present in this installation")
    assert example.read_text(encoding="utf-8") == EXAMPLE_CONFIG
