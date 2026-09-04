"""A scripted stand-in for systemd, so recovery can be tested without a session bus."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from taskspindle.units import UnitState

#: The states a unit is ever found in, named the way the rules talk about them.
ACTIVE = UnitState("loaded", "active", "running", "success", None, 4242)
SUCCESS = UnitState("loaded", "inactive", "dead", "success", 0, None)
OOM = UnitState("loaded", "failed", "failed", "oom-kill", 137, None)
SIGNALLED = UnitState("loaded", "failed", "failed", "signal", None, None)
EXITED = UnitState("loaded", "failed", "failed", "exit-code", 2, None)
NOT_FOUND = UnitState("not-found", "inactive", "dead", "success", None, None)


class FakeUnitBackend:
    """Answers ``show`` from a script and records everything else it was asked to do.

    ``on_start`` is the hook the orchestrator tests use to run a worker in-process: systemd would
    have detached the unit, so the fake stands in for the whole detour and runs the turn inline.
    """

    def __init__(
        self,
        states: Mapping[str, UnitState] | None = None,
        *,
        on_start: Callable[[str, tuple[str, ...]], None] | None = None,
        on_show: Callable[[str], None] | None = None,
    ) -> None:
        self.states: dict[str, UnitState] = dict(states or {})
        #: Called with the unit name before ``show`` answers, so a test can make systemd
        #: misbehave or move the world underneath the sweep.
        self.on_show = on_show
        self.started: list[tuple[str, tuple[str, ...]]] = []
        #: The environment each unit was started with, keyed by unit name.
        self.envs: dict[str, dict[str, str]] = {}
        self.killed: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.reset: list[str] = []
        self.on_start = on_start

    def set(self, unit: str, state: UnitState) -> None:
        self.states[unit] = state

    def start(
        self,
        unit: str,
        argv: Sequence[str],
        *,
        working_dir: Path,
        env: Mapping[str, str],
        properties: Mapping[str, str],
    ) -> None:
        self.started.append((unit, tuple(argv)))
        self.envs[unit] = dict(env)
        self.states.setdefault(unit, ACTIVE)
        if self.on_start is not None:
            self.on_start(unit, tuple(argv))

    def show(self, unit: str) -> UnitState:
        if self.on_show is not None:
            self.on_show(unit)
        return self.states.get(unit, NOT_FOUND)

    def kill(self, unit: str, signal: str) -> None:
        self.killed.append((unit, signal))

    def stop(self, unit: str) -> None:
        self.stopped.append(unit)

    def reset_failed(self, unit: str) -> None:
        self.reset.append(unit)
        self.states.pop(unit, None)
