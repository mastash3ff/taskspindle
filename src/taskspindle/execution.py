"""Select execution authority without giving MCP or web access to Docker."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import ConfigError, Paths
from .rpc import RemoteError, request
from .units import SystemdUserBackend, UnitError, UnitState


class ControllerClient:
    requires_inactive_previous_turn = True

    def __init__(self, socket_path: Path | str) -> None:
        self.socket_path = Path(socket_path)

    def _call(self, operation: str, arguments: Mapping[str, Any] | None = None) -> Any:
        try:
            return request(self.socket_path, operation, arguments, timeout=90)
        except RemoteError as exc:
            code = exc.code
            if operation == "start" and code in {
                "CONTROL_UNAVAILABLE", "CONTROL_PROTOCOL_ERROR", "CONTROL_FAILED",
            }:
                code = "UNIT_START_UNCERTAIN"
            raise UnitError(code, str(exc)) from exc

    def start(self, unit: str, argv: Sequence[str], *, working_dir: Path,
              env: Mapping[str, str], properties: Mapping[str, str]) -> None:
        self._call("start", {"unit": unit, "argv": list(argv), "working_dir": str(working_dir),
                             "env": dict(env), "properties": dict(properties)})

    def show(self, unit: str) -> UnitState:
        try:
            result = self._call("show", {"unit": unit})
            if not isinstance(result, dict) or set(result) != set(asdict(UnitState("", "", "", ""))):
                raise ValueError
            return UnitState(**result)
        except (TypeError, ValueError) as exc:
            raise UnitError("UNIT_QUERY_FAILED", "Invalid controller unit state") from exc

    def kill(self, unit: str, signal: str) -> None:
        self._call("kill", {"unit": unit, "signal": signal})

    def stop(self, unit: str) -> None:
        self._call("stop", {"unit": unit})

    def reset_failed(self, unit: str) -> None:
        self._call("reset_failed", {"unit": unit})

    def admission_open(self) -> bool:
        return self._call("admission") is True

    def set_admission(self, value: bool) -> None:
        self._call("set_admission", {"open": value})

    def status(self) -> dict[str, Any]:
        return self._call("status")

    def interrupt_workers(self) -> dict[str, Any]:
        return self._call("interrupt_workers")

    def reconcile(self) -> dict[str, Any]:
        return self._call("reconcile")


def unit_backend(paths: Paths, settings: Mapping[str, Any],
                 parent_env: Mapping[str, str] | None = None) -> SystemdUserBackend | ControllerClient:
    execution = settings.get("execution", {})
    if not isinstance(execution, Mapping):
        raise ConfigError("execution must be a table")
    backend = execution.get("backend", "systemd")
    if backend == "systemd":
        return SystemdUserBackend()
    if backend != "docker":
        raise ConfigError("execution.backend must be systemd or docker")
    socket_path = execution.get("jobs_socket")
    if not isinstance(socket_path, str) or not Path(socket_path).is_absolute():
        raise ConfigError("execution.jobs_socket must be an absolute path")
    return ControllerClient(socket_path)
