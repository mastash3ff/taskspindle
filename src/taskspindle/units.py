"""Transient systemd user units, and the boot identity that makes them recognisable.

Every worker and every accept runs as its own ``systemd-run --user`` unit so that the MCP server
can exit, crash or be restarted without taking the work with it, and so that a runaway agent hits
a memory ceiling instead of the machine's. The unit name is derived from the task id, so after a
restart the server can ask systemd what happened to a task it has forgotten.

A failed unit is deliberately *not* collected: without ``--collect`` the unit stays loaded after
it dies, so ``show`` can still report ``oom-kill`` or ``signal``. :mod:`taskspindle.recovery`
reads that state and only then calls :meth:`UnitBackend.reset_failed`.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

__all__ = [
    "WORKER_PROPERTIES",
    "CommandRunner",
    "SystemdUserBackend",
    "UnitBackend",
    "UnitError",
    "UnitState",
    "accept_unit_name",
    "boot_id",
    "parse_show",
    "unit_env",
    "worker_argv",
    "worker_unit_name",
]

#: Where Linux publishes the identity of the current boot.
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")

#: What :func:`boot_id` reports when the boot id cannot be read.
UNKNOWN_BOOT_ID = "unknown"

#: The slice every TaskSpindle unit is placed in, so the whole set can be inspected at once.
SLICE = "taskspindle.slice"

#: Properties queried by :meth:`UnitBackend.show`, in the order systemd prints them.
SHOW_PROPERTIES = ("LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "MainPID")

#: Resource limits every worker unit runs under.
WORKER_PROPERTIES: dict[str, str] = {
    "MemoryHigh": "2G",
    "MemoryMax": "3G",
    "MemorySwapMax": "512M",
    "OOMPolicy": "kill",
    "KillMode": "control-group",
    "TimeoutStopSec": "30",
}

#: How long any single systemctl/systemd-run call may take.
COMMAND_TIMEOUT = 30.0

#: Names copied into a unit's environment, plus every ``XDG_*`` name the parent has.
_UNIT_ENV_NAMES = ("PATH", "HOME", "LANG")

UnitKind = Literal["active", "success", "oom", "signal", "exit", "not_found", "unknown"]

_ACTIVE_STATES = frozenset({"activating", "active", "deactivating"})
_SIGNAL_RESULTS = frozenset({"signal", "core-dump"})
_EXIT_RESULTS = frozenset({"exit-code", "timeout"})


class UnitError(Exception):
    """A systemd command failed.

    ``code`` is one of ``UNIT_START_FAILED``, ``UNIT_QUERY_FAILED``, ``UNIT_KILL_FAILED``,
    ``UNIT_STOP_FAILED``, ``UNIT_TIMEOUT``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def boot_id() -> str:
    """Return this boot's identity, or ``"unknown"`` when it cannot be read.

    A task whose recorded boot id differs from this one cannot have a live worker, whatever the
    task row says: the machine rebooted underneath it.
    """
    try:
        raw = BOOT_ID_PATH.read_text(encoding="utf-8")
    except OSError:
        return UNKNOWN_BOOT_ID
    return raw.strip() or UNKNOWN_BOOT_ID


@dataclass(frozen=True)
class UnitState:
    """The handful of unit properties TaskSpindle reasons about."""

    load_state: str
    active_state: str
    sub_state: str
    result: str
    exec_main_status: int | None = None
    main_pid: int | None = None

    @property
    def kind(self) -> UnitKind:
        """Classify the unit into the outcomes recovery has a rule for."""
        if self.active_state in _ACTIVE_STATES:
            return "active"
        if self.load_state == "not-found":
            return "not_found"
        if self.active_state == "inactive" and self.result == "success":
            return "success"
        if self.result == "oom-kill":
            return "oom"
        if self.result in _SIGNAL_RESULTS:
            return "signal"
        if self.result in _EXIT_RESULTS:
            return "exit"
        return "unknown"


def _int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def parse_show(text: str) -> UnitState:
    """Parse the ``KEY=VALUE`` lines of ``systemctl show`` into a :class:`UnitState`."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    pid = _int(values.get("MainPID"))
    return UnitState(
        load_state=values.get("LoadState", "unknown"),
        active_state=values.get("ActiveState", "unknown"),
        sub_state=values.get("SubState", "unknown"),
        result=values.get("Result", "unknown"),
        exec_main_status=_int(values.get("ExecMainStatus")),
        main_pid=pid or None,
    )


class UnitBackend(Protocol):
    """What the rest of TaskSpindle needs from systemd."""

    def start(
        self,
        unit: str,
        argv: Sequence[str],
        *,
        working_dir: Path,
        env: Mapping[str, str],
        properties: Mapping[str, str],
    ) -> None: ...

    def show(self, unit: str) -> UnitState: ...

    def kill(self, unit: str, signal: str) -> None: ...

    def stop(self, unit: str) -> None: ...

    def reset_failed(self, unit: str) -> None: ...


class CommandRunner(Protocol):
    """How :class:`SystemdUserBackend` reaches a subprocess; injected so tests can record argv."""

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def default_runner(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one systemd command with no stdin and capture its output as text."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        check=False,
    )


class SystemdUserBackend(UnitBackend):
    """The real backend: ``systemd-run --user`` and ``systemctl --user``."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> None:
        self._run = runner or default_runner
        self._timeout = timeout

    def _call(self, argv: Sequence[str], *, code: str) -> subprocess.CompletedProcess[str]:
        try:
            proc = self._run(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as exc:
            raise UnitError("UNIT_TIMEOUT", f"{argv[0]} timed out after {self._timeout}s") from exc
        except OSError as exc:
            raise UnitError(code, f"could not run {argv[0]}: {exc}") from exc
        return proc

    @staticmethod
    def _stderr(proc: subprocess.CompletedProcess[str]) -> str:
        return (proc.stderr or "").strip()

    def start(
        self,
        unit: str,
        argv: Sequence[str],
        *,
        working_dir: Path,
        env: Mapping[str, str],
        properties: Mapping[str, str],
    ) -> None:
        """Launch ``argv`` as the transient unit ``unit``.

        ``--collect`` is deliberately absent: the unit must survive its own death so that
        :meth:`show` can still say how it died.
        """
        command = [
            "systemd-run",
            "--user",
            "--quiet",
            f"--unit={unit}",
            f"--slice={SLICE}",
            f"--working-directory={working_dir}",
            *(f"--setenv={name}={value}" for name, value in sorted(env.items())),
            *(f"--property={name}={value}" for name, value in sorted(properties.items())),
            "--",
            *argv,
        ]
        proc = self._call(command, code="UNIT_START_FAILED")
        if proc.returncode != 0:
            raise UnitError(
                "UNIT_START_FAILED",
                f"systemd-run exited {proc.returncode} for {unit}: {self._stderr(proc)}",
            )

    def show(self, unit: str) -> UnitState:
        """Read the unit's current state; a unit systemd never heard of is ``not-found``."""
        proc = self._call(
            ["systemctl", "--user", "show", unit, "-p", ",".join(SHOW_PROPERTIES)],
            code="UNIT_QUERY_FAILED",
        )
        if proc.returncode != 0:
            raise UnitError(
                "UNIT_QUERY_FAILED",
                f"systemctl show exited {proc.returncode} for {unit}: {self._stderr(proc)}",
            )
        return parse_show(proc.stdout or "")

    def kill(self, unit: str, signal: str) -> None:
        proc = self._call(
            ["systemctl", "--user", "kill", f"--signal={signal}", unit],
            code="UNIT_KILL_FAILED",
        )
        if proc.returncode != 0:
            raise UnitError(
                "UNIT_KILL_FAILED",
                f"systemctl kill exited {proc.returncode} for {unit}: {self._stderr(proc)}",
            )

    def stop(self, unit: str) -> None:
        proc = self._call(["systemctl", "--user", "stop", unit], code="UNIT_STOP_FAILED")
        if proc.returncode != 0:
            raise UnitError(
                "UNIT_STOP_FAILED",
                f"systemctl stop exited {proc.returncode} for {unit}: {self._stderr(proc)}",
            )

    def reset_failed(self, unit: str) -> None:
        """Forget a dead unit. Failure is ignored: the state has already been read.

        A systemctl that is slow, missing or simply refuses leaves the unit loaded, which costs
        nothing; raising here would abandon a reconciliation sweep over a piece of tidying.
        """
        with contextlib.suppress(UnitError):
            self._call(["systemctl", "--user", "reset-failed", unit], code="UNIT_QUERY_FAILED")


def worker_unit_name(task_id: str) -> str:
    """The unit a task's worker runs as. Task ids are already unique, so this is too."""
    return f"taskspindle-worker-{task_id}"


def accept_unit_name(task_id: str) -> str:
    """The unit a task's accept runs as."""
    return f"taskspindle-accept-{task_id}"


def worker_argv(task_id: str) -> list[str]:
    """The command line a worker unit runs."""
    return [sys.executable, "-m", "taskspindle.runner", "--task", task_id]


def unit_env(parent: Mapping[str, str], *, config_file: Path) -> dict[str, str]:
    """The environment a unit is started with: enough to find Python, the user and the config.

    This is the *unit's* environment, not the agent's; the agent's is rebuilt from scratch by
    :func:`taskspindle.providers.build_child_env` inside the worker.
    """
    env = {name: parent[name] for name in _UNIT_ENV_NAMES if name in parent}
    env.update({name: value for name, value in parent.items() if name.startswith("XDG_")})
    env["TASKSPINDLE_CONFIG"] = str(config_file)
    return env
