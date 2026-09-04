"""Installing the pinned runtime, against a fake npm rather than a real registry."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import taskspindle
from taskspindle.config import Paths
from taskspindle.setup import ADAPTER_BIN, EXAMPLE_CONFIG, MANIFESTS, SetupError, install_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeNpm:
    """Lays out the tree a successful ``npm ci`` would have left behind."""

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
            binaries = modules / ".bin"
            binaries.mkdir(parents=True, exist_ok=True)
            binaries.joinpath(ADAPTER_BIN).write_text("#!/bin/sh\n", encoding="utf-8")
        return subprocess.CompletedProcess(list(command), self.returncode, "", "npm said no\n")


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config" / "taskspindle" / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "data" / "runtimes" / taskspindle.__version__,
    )


def test_a_successful_install_reports_the_pinned_adapter_and_lays_out_the_dirs(
    paths: Paths,
) -> None:
    npm = FakeNpm()

    report = install_runtime(paths, runner=npm, parent_env={"PATH": "/usr/bin", "HOME": "/home/x"})

    assert report["adapter_version"] == taskspindle.ADAPTER_VERSION
    assert report["runtime_dir"] == str(paths.runtime_dir)
    assert report["created_config"] is True
    for name in MANIFESTS:
        assert (paths.runtime_dir / name).read_bytes()
    for directory in (paths.runtime_dir, paths.state_dir, paths.data_dir):
        assert directory.stat().st_mode & 0o777 == 0o700


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
