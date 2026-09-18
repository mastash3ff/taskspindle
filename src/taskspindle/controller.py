"""Private Unix socket authority for explicit Docker jobs and bounded diagnostics."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import socket
import socketserver
import stat
import struct
import threading
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config, paths
from .execution import ControllerClient
from .rpc import RemoteError, receive, send
from .units import UnitError


def _arguments(arguments: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) != fields:
        raise RemoteError("CONTROL_INVALID_ARGUMENT", "Invalid control arguments")
    return arguments


class Controller:
    def __init__(self, backend: Any, resolved: Any, settings: Mapping[str, Any]) -> None:
        self.backend, self.paths, self.settings = backend, resolved, settings
        self._mutation_lock = threading.RLock()

    def dispatch(self, operation: Any, arguments: Any, *, diagnostics: bool = False) -> Any:
        # Keep the fence closed across a whole stop/recovery sweep, including against `open`.
        if not diagnostics and operation in {"start", "set_admission", "interrupt_workers", "reconcile"}:
            with self._mutation_lock:
                return self._dispatch(operation, arguments, diagnostics=diagnostics)
        return self._dispatch(operation, arguments, diagnostics=diagnostics)

    def _dispatch(self, operation: Any, arguments: Any, *, diagnostics: bool = False) -> Any:
        if diagnostics:
            if operation != "doctor":
                raise RemoteError("CONTROL_FORBIDDEN", "Operation is not available on this socket")
            args = _arguments(arguments, {"live"})
            if type(args["live"]) is not bool:
                raise RemoteError("CONTROL_INVALID_ARGUMENT", "Diagnostic live flag must be boolean")
            from .worker_diagnostics import doctor_report
            return doctor_report(self.backend, self.paths, self.settings, live=args["live"])
        if operation in {"admission", "status", "interrupt_workers"}:
            _arguments(arguments, set())
            method = "admission_open" if operation == "admission" else operation
            if operation == "interrupt_workers":
                snapshot = self.backend.interrupt_workers()
                if (snapshot.get("admission_open") is not False
                        or snapshot.get("engine_reachable") is not True
                        or snapshot.get("jobs") != [] or snapshot.get("unsettled_integrations") != 0):
                    raise UnitError("UNIT_STOP_FAILED", "Worker interruption is not fully observed")
                from .worker_diagnostics import reconcile_interrupted_workers
                reconcile_interrupted_workers(self.backend, self.paths, self.settings)
                return self.backend.status()
            return getattr(self.backend, method)()
        if operation == "reconcile":
            _arguments(arguments, set())
            snapshot = self.backend.status()
            if snapshot.get("admission_open") is not False:
                raise UnitError("UNIT_ADMISSION_OPEN", "Recovery requires closed admission")
            if snapshot.get("engine_reachable") is not True or snapshot.get("unsettled_integrations") != 0:
                raise UnitError(
                    "UNIT_QUERY_FAILED", "Recovery requires a reachable engine and no integrations",
                )
            if snapshot.get("jobs"):
                return snapshot
            from .worker_diagnostics import reconcile_interrupted_workers
            reconcile_interrupted_workers(self.backend, self.paths, self.settings)
            return self.backend.status()
        if operation == "set_admission":
            args = _arguments(arguments, {"open"})
            if type(args["open"]) is not bool:
                raise RemoteError("CONTROL_INVALID_ARGUMENT", "Admission flag must be boolean")
            return self.backend.set_admission(args["open"])
        if operation in {"show", "stop", "reset_failed", "kill", "start"}:
            fields = {"unit"}
            if operation == "kill":
                fields.add("signal")
            elif operation == "start":
                fields.update({"argv", "working_dir", "env", "properties"})
            args = _arguments(arguments, fields)
            if not isinstance(args["unit"], str) or len(args["unit"]) > 200:
                raise RemoteError("CONTROL_INVALID_ARGUMENT", "Invalid logical job name")
            if operation == "start":
                if (not isinstance(args["argv"], list) or not 1 <= len(args["argv"]) <= 16
                        or any(not isinstance(v, str) or len(v) > 4096 for v in args["argv"])
                        or not isinstance(args["working_dir"], str)):
                    raise RemoteError("CONTROL_INVALID_ARGUMENT", "Invalid launch arguments")
                for key in ("env", "properties"):
                    if (not isinstance(args[key], dict) or len(args[key]) > 128
                            or any(not isinstance(k, str) or not isinstance(v, str)
                                   or len(k) > 256 or len(v) > 32768 for k, v in args[key].items())):
                        raise RemoteError(
                            "CONTROL_INVALID_ARGUMENT", "Invalid launch environment or properties",
                        )
                return self.backend.start(args["unit"], args["argv"], working_dir=Path(args["working_dir"]),
                                          env=args["env"], properties=args["properties"])
            if operation == "kill":
                if not isinstance(args["signal"], str):
                    raise RemoteError("CONTROL_INVALID_ARGUMENT", "Invalid signal")
                return self.backend.kill(args["unit"], args["signal"])
            result = getattr(self.backend, operation)(args["unit"])
            return asdict(result) if operation == "show" else result
        raise RemoteError("CONTROL_FORBIDDEN", "Unknown control operation")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(300 if self.server.diagnostics else 90)
        try:
            # Socket mode protects access; peer credentials additionally reject another UID.
            credentials = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", credentials)
            if uid != os.getuid():
                raise RemoteError("CONTROL_FORBIDDEN", "Control peer is not authorized")
            message = receive(self.rfile)
            if set(message) != {"operation", "arguments"}:
                raise RemoteError("CONTROL_PROTOCOL_ERROR", "Invalid control request")
            result = self.server.controller.dispatch(message["operation"], message["arguments"],
                                                     diagnostics=self.server.diagnostics)
            response = {"ok": True, "result": result}
        except (RemoteError, UnitError) as exc:
            response = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except Exception:
            # SDK and provider exceptions may carry credentials or full request arguments.
            response = {"ok": False, "error": {
                "code": "CONTROL_FAILED", "message": "Control operation failed",
            }}
        with contextlib.suppress(OSError, RemoteError):
            send(self.wfile, response)


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path: Path, controller: Controller, *, diagnostics: bool = False) -> None:
        self.controller, self.diagnostics = controller, diagnostics
        self._slots = threading.BoundedSemaphore(4 if diagnostics else 32)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise ConfigError("Control socket path is occupied by a non-socket")
            # Caller owns the singleton lock before replacing a stale socket.
            path.unlink()
        super().__init__(str(path), _Handler)
        path.chmod(0o600)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(controller: Controller, jobs: Path, diagnostics: Path) -> None:
    if jobs == diagnostics or not jobs.is_absolute() or not diagnostics.is_absolute():
        raise ConfigError("Controller sockets must be distinct absolute paths")
    lock_path = controller.paths.state_dir / "execution" / "controller.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigError("A controller already owns this state directory") from exc
        with ControlServer(jobs, controller) as job_server, ControlServer(
            diagnostics, controller, diagnostics=True,
        ) as diagnostic_server:
            servers = [job_server, diagnostic_server]
            stopped = threading.Event()
            previous = {}
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda *_: stopped.set())
            threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
            try:
                for thread in threads:
                    thread.start()
                stopped.wait()
            finally:
                for server in servers:
                    server.shutdown()
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                for path in (jobs, diagnostics):
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taskspindle-controller")
    parser.add_argument(
        "command", choices=["serve", "status", "fence", "open", "interrupt-workers", "reconcile"],
    )
    parser.add_argument("--jobs-socket", type=Path)
    parser.add_argument("--diagnostics-socket", type=Path)
    args = parser.parse_args(argv)
    try:
        resolved = paths()
        if os.environ.get("TASKSPINDLE_CONFIG"):
            resolved = replace(resolved, config_file=Path(os.environ["TASKSPINDLE_CONFIG"]))
        settings = load_config(resolved.config_file)
        execution = settings.get("execution", {})
        jobs = args.jobs_socket or Path(execution.get("jobs_socket", ""))
        diagnostics = args.diagnostics_socket or Path(execution.get("diagnostics_socket", ""))
        if not jobs.is_absolute():
            raise ConfigError("execution.jobs_socket must be absolute")
        if args.command == "serve":
            from .docker_units import DockerBackend
            serve(Controller(DockerBackend(resolved, settings), resolved, settings), jobs, diagnostics)
            return 0
        client = ControllerClient(jobs)
        if args.command in {"fence", "open"}:
            client.set_admission(args.command == "open")
        if args.command == "interrupt-workers":
            result = client.interrupt_workers()
        elif args.command in {"fence", "reconcile"}:
            result = client.reconcile()
        else:
            result = client.status()
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except (ConfigError, UnitError, RemoteError) as exc:
        print(json.dumps({"error": {"code": getattr(exc, "code", "CONFIG_INVALID"), "message": str(exc)}}))
        return 1
    except Exception:
        print(json.dumps({"error": {"code": "CONTROL_FAILED", "message": "Controller operation failed"}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
