"""A scripted stand-in for systemd, so recovery can be tested without a session bus."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    """Answers ``show`` from a script and records everything else it was asked to do."""

    def __init__(self, states: Mapping[str, UnitState] | None = None) -> None:
        self.states: dict[str, UnitState] = dict(states or {})
        self.started: list[tuple[str, tuple[str, ...]]] = []
        self.killed: list[tuple[str, str]] = []
        self.stopped: list[str] = []
        self.reset: list[str] = []

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
        self.states.setdefault(unit, ACTIVE)

    def show(self, unit: str) -> UnitState:
        return self.states.get(unit, NOT_FOUND)

    def kill(self, unit: str, signal: str) -> None:
        self.killed.append((unit, signal))

    def stop(self, unit: str) -> None:
        self.stopped.append(unit)

    def reset_failed(self, unit: str) -> None:
        self.reset.append(unit)
        self.states.pop(unit, None)
