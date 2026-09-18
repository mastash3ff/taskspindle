"""Private, fixed-command bridge to the host AI policy manager.

Run ``serve --socket /run/taskspindle/ai/control.sock`` in the trusted service;
run ``adapter --socket ...`` as the dashboard's configured adapter command.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import socketserver
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import rpc
from .config import ConfigError, load_config, paths
from .web.ai_policy import (
    HOSTS,
    AdapterError,
    invoke_configured_adapter,
    parse_adapter_payload,
    parse_use_request,
)

MAX_BYTES = 65536
MANAGER_TIMEOUT = 10.0


def validate_request(payload: Any) -> dict[str, Any]:
    """Accept only the manager's two operations and explicit host identities."""
    if not isinstance(payload, dict):
        raise rpc.RemoteError("INVALID_REQUEST", "Request must be an object")
    names = payload.get("hosts")
    if (not isinstance(names, list) or not names or len(names) > len(HOSTS)
            or any(not isinstance(n, str) or n not in HOSTS for n in names)
            or len(set(names)) != len(names)):
        raise rpc.RemoteError("INVALID_REQUEST", "Invalid hosts")
    if payload.get("action") == "status" and set(payload) == {"action", "hosts"}:
        result = payload
    elif payload.get("action") == "use":
        body, error = parse_use_request({k: v for k, v in payload.items() if k != "action"}, HOSTS)
        if error or body is None:
            raise rpc.RemoteError("INVALID_REQUEST", "Invalid mode or revisions")
        result = {"action": "use", **body}
    else:
        raise rpc.RemoteError("INVALID_REQUEST", "Unsupported request")
    if len(json.dumps(result).encode("utf-8")) > MAX_BYTES:
        raise rpc.RemoteError("INVALID_REQUEST", "Request too large")
    return result


def configured_command(settings: Mapping[str, Any]) -> tuple[str, ...]:
    table = settings.get("host_controls")
    value = table.get("command") if isinstance(table, dict) else None
    if (not isinstance(value, list) or not 1 <= len(value) <= 16
            or any(not isinstance(a, str) or not a or len(a) > 4096 or "\0" in a for a in value)
            or not Path(value[0]).is_absolute()):
        raise ConfigError("host_controls.command must be a fixed absolute executable argv")
    return tuple(value)


def invoke(command: tuple[str, ...], payload: Any, *, timeout: float = MANAGER_TIMEOUT) -> dict[str, Any]:
    validated = validate_request(payload)
    try:
        raw = asyncio.run(invoke_configured_adapter(
            command, validated, timeout=timeout, max_stdout=MAX_BYTES, max_stderr=MAX_BYTES,
        ))
        hosts, results = parse_adapter_payload(
            raw, validated["hosts"], require_results=validated["action"] == "use",
            requested_mode=validated.get("mode"),
        )
    except AdapterError as exc:
        raise rpc.RemoteError(exc.code, "Host policy manager failed") from exc
    result: dict[str, Any] = {"hosts": hosts}
    if results is not None:
        result["results"] = results
    return result


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(5)
        try:
            raw = self.rfile.readline(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES or not raw.endswith(b"\n"):
                raise rpc.RemoteError("INVALID_REQUEST", "Missing or oversized request")
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise rpc.RemoteError("INVALID_REQUEST", "Invalid JSON") from exc
            if (not isinstance(message, dict) or set(message) != {"operation", "arguments"}
                    or message["operation"] != "ai_policy"):
                raise rpc.RemoteError("INVALID_REQUEST", "Unsupported operation")
            result = invoke(self.server.command, message["arguments"])
            response = {"ok": True, "result": result}
        except rpc.RemoteError as exc:
            response = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except (OSError, ValueError):
            response = {"ok": False, "error": {"code": "CONTROL_FAILED", "message": "Host control failed"}}
        with contextlib.suppress(OSError):
            rpc.send(self.wfile, response)


class ControlServer(socketserver.UnixStreamServer):
    """One manager invocation at a time; the host manager owns revision checks."""

    def __init__(self, socket_path: Path, command: tuple[str, ...]):
        self.command = command
        super().__init__(str(socket_path), _Handler)
        os.chmod(socket_path, 0o660)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Never print request payloads, manager output, or tracebacks to service logs.
        print("Host control request failed", file=sys.stderr)


def serve(socket_path: Path, command: tuple[str, ...]) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Lock survives socket replacement and prevents restarting over a live listener.
    fd = os.open(str(socket_path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if socket_path.exists() or socket_path.is_symlink():
            metadata = socket_path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ConfigError("Refusing to replace a non-owned control socket")
            socket_path.unlink()
        try:
            with ControlServer(socket_path, command) as server:
                server.serve_forever()
        finally:
            with contextlib.suppress(FileNotFoundError):
                socket_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("serve", "adapter"))
    parser.add_argument("--socket", type=Path)
    args = parser.parse_args(argv)
    try:
        settings = load_config(Path(os.environ.get("TASKSPINDLE_CONFIG", paths().config_file)))
        table = settings.get("host_controls", {})
        configured_socket = table.get("socket") if isinstance(table, dict) else None
        socket_path = args.socket or (Path(configured_socket) if isinstance(configured_socket, str) else None)
        if socket_path is None or not socket_path.is_absolute():
            raise ConfigError("An absolute host-controls socket is required")
        if args.operation == "serve":
            serve(socket_path, configured_command(settings))
        else:
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise rpc.RemoteError("INVALID_REQUEST", "Request too large")
            payload = validate_request(json.loads(raw))
            result = rpc.request(socket_path, "ai_policy", payload, timeout=12)
            print(json.dumps(result))
        return 0
    except (ConfigError, rpc.RemoteError, OSError, ValueError):
        # An unsuccessful invocation is never reported as a successful mutation.
        print(json.dumps({"error": "Host controls unavailable or invalid request"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
