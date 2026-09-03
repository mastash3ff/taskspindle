"""Transient unit construction, state parsing and classification."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from taskspindle.units import (
    WORKER_PROPERTIES,
    SystemdUserBackend,
    UnitError,
    UnitState,
    accept_unit_name,
    boot_id,
    parse_show,
    unit_env,
    worker_argv,
    worker_unit_name,
)

SHOW_OUTPUT = """LoadState=loaded
ActiveState=failed
SubState=failed
Result=oom-kill
ExecMainStatus=137
MainPID=0
"""

NOT_FOUND_OUTPUT = """LoadState=not-found
ActiveState=inactive
SubState=dead
Result=success
ExecMainStatus=0
MainPID=0
"""


class RecordingRunner:
    """Stands in for the subprocess call, remembering every argv it was handed."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), self.returncode, self.stdout, "")


def test_start_builds_the_systemd_run_command(tmp_path: Path) -> None:
    runner = RecordingRunner()
    backend = SystemdUserBackend(runner=runner)

    backend.start(
        "taskspindle-worker-ts_1",
        worker_argv("ts_1"),
        working_dir=tmp_path,
        env={"HOME": "/home/tester", "TASKSPINDLE_CONFIG": "/cfg/config.toml"},
        properties=WORKER_PROPERTIES,
    )

    argv = runner.calls[0]
    assert argv[:6] == [
        "systemd-run",
        "--user",
        "--quiet",
        "--unit=taskspindle-worker-ts_1",
        "--slice=taskspindle.slice",
        f"--working-directory={tmp_path}",
    ]
    assert "--setenv=HOME=/home/tester" in argv
    assert "--setenv=TASKSPINDLE_CONFIG=/cfg/config.toml" in argv
    assert "--property=MemoryMax=3G" in argv
    assert "--property=OOMPolicy=kill" in argv
    # A failed unit must stay loaded so that its post-mortem state can still be read.
    assert "--collect" not in argv
    assert argv[argv.index("--") + 1 :] == [
        sys.executable,
        "-m",
        "taskspindle.runner",
        "--task",
        "ts_1",
    ]


def test_show_kill_stop_and_reset_failed_use_systemctl_user() -> None:
    runner = RecordingRunner(stdout=SHOW_OUTPUT)
    backend = SystemdUserBackend(runner=runner)

    state = backend.show("taskspindle-worker-ts_1")
    backend.kill("taskspindle-worker-ts_1", "SIGTERM")
    backend.stop("taskspindle-worker-ts_1")
    backend.reset_failed("taskspindle-worker-ts_1")

    assert runner.calls[0] == [
        "systemctl",
        "--user",
        "show",
        "taskspindle-worker-ts_1",
        "-p",
        "LoadState,ActiveState,SubState,Result,ExecMainStatus,MainPID",
    ]
    assert runner.calls[1] == [
        "systemctl",
        "--user",
        "kill",
        "--signal=SIGTERM",
        "taskspindle-worker-ts_1",
    ]
    assert runner.calls[2] == ["systemctl", "--user", "stop", "taskspindle-worker-ts_1"]
    assert runner.calls[3] == ["systemctl", "--user", "reset-failed", "taskspindle-worker-ts_1"]
    assert state.result == "oom-kill"
    assert state.exec_main_status == 137
    assert state.main_pid is None
    assert state.kind == "oom"


def test_a_failed_systemd_command_raises_but_reset_failed_does_not() -> None:
    backend = SystemdUserBackend(runner=RecordingRunner(returncode=1))

    with pytest.raises(UnitError) as excinfo:
        backend.show("taskspindle-worker-ts_1")
    assert excinfo.value.code == "UNIT_QUERY_FAILED"

    # A unit whose state has already been read is forgotten on a best-effort basis.
    backend.reset_failed("taskspindle-worker-ts_1")


def test_show_of_an_unknown_unit_parses_as_not_found() -> None:
    state = parse_show(NOT_FOUND_OUTPUT)
    assert state.load_state == "not-found"
    assert state.kind == "not_found"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (UnitState("loaded", "activating", "start", "success"), "active"),
        (UnitState("loaded", "active", "running", "success"), "active"),
        (UnitState("loaded", "deactivating", "stop", "success"), "active"),
        (UnitState("not-found", "inactive", "dead", "success"), "not_found"),
        (UnitState("loaded", "inactive", "dead", "success", 0), "success"),
        (UnitState("loaded", "failed", "failed", "oom-kill", 137), "oom"),
        (UnitState("loaded", "failed", "failed", "signal"), "signal"),
        (UnitState("loaded", "failed", "failed", "core-dump"), "signal"),
        (UnitState("loaded", "failed", "failed", "exit-code", 1), "exit"),
        (UnitState("loaded", "failed", "failed", "timeout"), "exit"),
        (UnitState("loaded", "failed", "failed", "resources"), "unknown"),
    ],
)
def test_unit_state_kind_classification(state: UnitState, expected: str) -> None:
    assert state.kind == expected


def test_names_argv_and_unit_environment() -> None:
    assert worker_unit_name("ts_abc") == "taskspindle-worker-ts_abc"
    assert accept_unit_name("ts_abc") == "taskspindle-accept-ts_abc"

    env = unit_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/tester",
            "LANG": "C.UTF-8",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "ANTHROPIC_API_KEY": "sk-secret",
        },
        config_file=Path("/cfg/config.toml"),
    )
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/tester",
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "TASKSPINDLE_CONFIG": "/cfg/config.toml",
    }


def test_boot_id_is_a_stable_non_empty_string() -> None:
    assert boot_id() == boot_id()
    assert boot_id()


@pytest.mark.skipif(
    not os.environ.get("TASKSPINDLE_REAL_SYSTEMD"),
    reason="set TASKSPINDLE_REAL_SYSTEMD to exercise a real systemd user session",
)
def test_a_real_transient_unit_runs_and_reports_success(tmp_path: Path) -> None:
    backend = SystemdUserBackend()
    unit = f"taskspindle-selftest-{os.getpid()}"
    backend.start(unit, ["/bin/true"], working_dir=tmp_path, env={}, properties={})
    try:
        for _ in range(100):
            state = backend.show(unit)
            if state.kind != "active":
                break
            time.sleep(0.1)
        assert state.kind == "success"
    finally:
        backend.reset_failed(unit)
