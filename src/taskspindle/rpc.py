"""Bounded JSON requests over private Unix sockets used by container controls."""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_MESSAGE_BYTES = 1_048_576


class RemoteError(Exception):
    """A control request failed without exposing its arguments in the error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def receive(stream: Any) -> dict[str, Any]:
    raw = stream.readline(MAX_MESSAGE_BYTES + 1)
    if not raw or len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
        raise RemoteError("CONTROL_PROTOCOL_ERROR", "Missing or oversized control message")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RemoteError("CONTROL_PROTOCOL_ERROR", "Invalid control JSON") from exc
    if not isinstance(value, dict):
        raise RemoteError("CONTROL_PROTOCOL_ERROR", "Control message must be an object")
    return value


def send(stream: Any, value: Mapping[str, Any]) -> None:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_MESSAGE_BYTES:
        raise RemoteError("CONTROL_PROTOCOL_ERROR", "Oversized control message")
    stream.write(raw)
    stream.flush()


def request(
    socket_path: Path | str,
    operation: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    """Send one operation; servers separately restrict their permitted operations."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(socket_path))
            with connection.makefile("rwb") as stream:
                send(stream, {"operation": operation, "arguments": dict(arguments or {})})
                response = receive(stream)
    except (OSError, TimeoutError) as exc:
        raise RemoteError("CONTROL_UNAVAILABLE", "Container control service is unavailable") from exc
    if response.get("ok") is not True:
        error = response.get("error")
        if not isinstance(error, dict):
            raise RemoteError("CONTROL_PROTOCOL_ERROR", "Invalid control error response")
        raise RemoteError(str(error.get("code", "CONTROL_FAILED")), str(error.get("message", "Control failed")))
    if "result" not in response:
        raise RemoteError("CONTROL_PROTOCOL_ERROR", "Missing control result")
    return response["result"]
