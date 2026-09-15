"""Fixed-argv Codex AI-mode adapter used by the dashboard Policy page.

The browser never supplies an executable, argv, or filesystem path. The only process this module
will spawn is the optional ``[ai_policy]`` command from TaskSpindle's existing config file; a
missing or invalid table is reported as unavailable rather than guessed. The adapter owns Codex
AI-mode truth — this module does not write ``config.toml`` or the task database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import ConfigError, load_config

__all__ = [
    "ADAPTER_UNAVAILABLE",
    "APPLIES_TO",
    "HOSTS",
    "MODES",
    "STATUSES",
    "AdapterCall",
    "AdapterError",
    "AdapterFactory",
    "AiPolicyConfig",
    "apply_succeeded",
    "default_adapter_factory",
    "http_payload",
    "invoke_configured_adapter",
    "load_ai_policy_config",
    "parse_adapter_payload",
    "parse_use_request",
    "unavailable_hosts",
]

HOSTS: tuple[str, ...] = ("windows", "wsl")
MODES: tuple[str, ...] = ("native", "ensemble")
STATUSES: tuple[str, ...] = ("configured", "needs_repair", "unavailable", "update_failed")
APPLIES_TO = "new_sessions"
ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"
ADAPTER_INVALID = "ADAPTER_INVALID"
ADAPTER_CONFIG_INVALID = "ADAPTER_CONFIG_INVALID"

_ADAPTER_TIMEOUT = 15.0
_MAX_ADAPTER_STDOUT = 65536
_MAX_ADAPTER_STDERR = 4096
_MAX_COMMAND_ARGS = 16
_MAX_ARG_CHARS = 4096
_MAX_REVISION_CHARS = 256
_MAX_CHECKS = 32
_MAX_CHECK_CHARS = 1024

AdapterCall = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
AdapterFactory = Callable[[tuple[str, ...] | None], AdapterCall | None]


class AdapterError(Exception):
    """The configured adapter could not be used; ``code`` is a public error token."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AiPolicyConfig:
    """Trusted adapter argv and the hosts it is allowed to address."""

    command: tuple[str, ...]
    hosts: tuple[str, ...]


def load_ai_policy_config(config_file: Path) -> tuple[AiPolicyConfig | None, str]:
    """Read ``[ai_policy]`` from the existing TaskSpindle config file.

    Returns ``(config, missing_error)``. ``config`` is ``None`` when the table is absent or not
    executable as configured; ``missing_error`` is the public token to report instead of spawning.
    """
    try:
        settings = load_config(config_file)
    except ConfigError:
        return None, ADAPTER_UNAVAILABLE
    if "ai_policy" not in settings:
        return None, ADAPTER_UNAVAILABLE
    parsed = parse_ai_policy_table(settings.get("ai_policy"))
    if parsed is None:
        return None, ADAPTER_CONFIG_INVALID
    return parsed, ADAPTER_UNAVAILABLE


def parse_ai_policy_table(table: Any) -> AiPolicyConfig | None:
    """Validate a TOML ``[ai_policy]`` table. Invalid input yields ``None``, never a spawn."""
    if not isinstance(table, dict):
        return None
    command = _parse_command(table.get("command"))
    hosts = _parse_hosts(table.get("hosts", list(HOSTS)))
    if command is None or hosts is None:
        return None
    return AiPolicyConfig(command=command, hosts=hosts)


def _parse_command(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value or len(value) > _MAX_COMMAND_ARGS:
        return None
    parts: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item or len(item) > _MAX_ARG_CHARS:
            return None
        parts.append(item)
    if not Path(parts[0]).is_absolute():
        return None
    return tuple(parts)


def _parse_hosts(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    hosts: list[str] = []
    seen: set[str] = set()
    for item in value:
        if item not in HOSTS or item in seen:
            return None
        seen.add(item)
        hosts.append(item)
    return tuple(hosts)


def unavailable_hosts(hosts: Sequence[str], error: str = ADAPTER_UNAVAILABLE) -> list[dict[str, Any]]:
    """Per-host unavailable rows used when the adapter cannot be consulted."""
    return [
        _host_entry(host, mode=None, status="unavailable", revision=None, checks=[], error=error)
        for host in hosts
    ]


def http_payload(
    hosts: Sequence[Mapping[str, Any]],
    csrf_token: str,
    results: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The dashboard JSON object: adapter host state plus CSRF and session-scope."""
    payload: dict[str, Any] = {
        "hosts": list(hosts),
        "applies_to": APPLIES_TO,
        "csrf_token": csrf_token,
    }
    if results is not None:
        payload["results"] = list(results)
    return payload


def parse_use_request(
    body: Mapping[str, Any], allowed_hosts: Sequence[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a PUT body. Extra keys and browser-supplied argv are refused."""
    if set(body) != {"mode", "hosts", "expected_revisions"}:
        return None, "INVALID_REQUEST"
    mode = body.get("mode")
    if mode not in MODES:
        return None, "INVALID_REQUEST"
    hosts = body.get("hosts")
    parsed_hosts = _parse_hosts(hosts)
    if parsed_hosts is None or any(host not in allowed_hosts for host in parsed_hosts):
        return None, "INVALID_REQUEST"
    revisions = body.get("expected_revisions")
    if not isinstance(revisions, dict) or set(revisions) != set(parsed_hosts):
        return None, "INVALID_REQUEST"
    expected: dict[str, str] = {}
    for host in parsed_hosts:
        value = revisions.get(host)
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value) > _MAX_REVISION_CHARS
        ):
            return None, "INVALID_REQUEST"
        expected[host] = value
    return {"mode": mode, "hosts": list(parsed_hosts), "expected_revisions": expected}, None


def parse_adapter_payload(
    raw: Any,
    requested_hosts: Sequence[str],
    *,
    require_results: bool = False,
    requested_mode: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Normalize adapter stdout. Invalid contracts raise ``AdapterError`` instead of passing through."""
    if not isinstance(raw, dict):
        raise AdapterError(ADAPTER_INVALID)
    hosts_raw = raw.get("hosts")
    if not isinstance(hosts_raw, list):
        raise AdapterError(ADAPTER_INVALID)
    hosts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in hosts_raw:
        entry = _parse_host_entry(item)
        if entry["host"] in seen:
            raise AdapterError(ADAPTER_INVALID)
        seen.add(entry["host"])
        hosts.append(entry)
    by_host = {entry["host"]: entry for entry in hosts}
    ordered = [by_host[host] for host in requested_hosts if host in by_host]
    for host in requested_hosts:
        if host not in by_host:
            ordered.append(_host_entry(host, None, "unavailable", None, [], ADAPTER_INVALID))
    results: list[dict[str, Any]] | None = None
    if "results" in raw:
        results = _parse_results(raw.get("results"), requested_hosts)
    elif require_results:
        results = [_result_entry(host, False, ADAPTER_INVALID) for host in requested_hosts]
    if require_results and results is not None:
        results = _coerce_use_results(ordered, results, requested_hosts, requested_mode)
    return ordered, results


def _parse_host_entry(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise AdapterError(ADAPTER_INVALID)
    host = item.get("host")
    mode = item.get("mode")
    status = item.get("status")
    revision = item.get("revision")
    checks_raw = item.get("checks")
    error = item.get("error")
    if host not in HOSTS or status not in STATUSES:
        raise AdapterError(ADAPTER_INVALID)
    if mode not in MODES and mode is not None:
        raise AdapterError(ADAPTER_INVALID)
    if revision is not None and (not isinstance(revision, str) or "\x00" in revision):
        raise AdapterError(ADAPTER_INVALID)
    if not isinstance(checks_raw, list) or len(checks_raw) > _MAX_CHECKS:
        raise AdapterError(ADAPTER_INVALID)
    checks = [_parse_check(check) for check in checks_raw]
    if error is not None and (not isinstance(error, str) or "\x00" in error):
        raise AdapterError(ADAPTER_INVALID)
    return _host_entry(host, mode, status, revision, checks, error)


def _parse_check(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise AdapterError(ADAPTER_INVALID)
    name = item.get("name")
    ok = item.get("ok")
    detail = item.get("detail")
    if not isinstance(name, str) or type(ok) is not bool:
        raise AdapterError(ADAPTER_INVALID)
    if detail is not None and not isinstance(detail, str):
        raise AdapterError(ADAPTER_INVALID)
    if "\x00" in name or (isinstance(detail, str) and "\x00" in detail):
        raise AdapterError(ADAPTER_INVALID)
    check: dict[str, Any] = {
        "name": name[:_MAX_CHECK_CHARS],
        "ok": ok,
        "detail": (detail or "")[:_MAX_CHECK_CHARS],
    }
    return check


def _parse_results(raw: Any, requested_hosts: Sequence[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise AdapterError(ADAPTER_INVALID)
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise AdapterError(ADAPTER_INVALID)
        host = item.get("host")
        ok = item.get("ok")
        error = item.get("error")
        if host not in HOSTS or host in seen or type(ok) is not bool:
            raise AdapterError(ADAPTER_INVALID)
        if error is not None and (not isinstance(error, str) or "\x00" in error):
            raise AdapterError(ADAPTER_INVALID)
        seen.add(host)
        parsed.append(_result_entry(host, ok, error))
    by_host = {entry["host"]: entry for entry in parsed}
    return [
        by_host.get(host) or _result_entry(host, False, ADAPTER_INVALID) for host in requested_hosts
    ]


def _host_entry(
    host: str,
    mode: str | None,
    status: str,
    revision: str | None,
    checks: list[dict[str, Any]],
    error: str | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "host": host,
        "mode": mode,
        "status": status,
        "revision": revision,
        "checks": checks,
    }
    if error:
        entry["error"] = error
    return entry


def _result_entry(host: str, ok: bool, error: str | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"host": host, "ok": ok}
    if error:
        entry["error"] = error
    return entry


def _coerce_use_results(
    hosts: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
    requested_hosts: Sequence[str],
    requested_mode: str | None,
) -> list[dict[str, Any]]:
    """A use result may stay ok only when that host is present, configured, and in the requested mode."""
    by_host = {entry["host"]: entry for entry in hosts}
    coerced: list[dict[str, Any]] = []
    for host, result in zip(requested_hosts, results, strict=True):
        row = by_host.get(host)
        if result.get("ok") is True and (
            row is None
            or row.get("status") != "configured"
            or (requested_mode is not None and row.get("mode") != requested_mode)
        ):
            coerced.append(_result_entry(host, False, result.get("error") or ADAPTER_INVALID))
        else:
            coerced.append(dict(result))
    return coerced


def apply_succeeded(
    results: Sequence[Mapping[str, Any]] | None,
    hosts: Sequence[Mapping[str, Any]] | None = None,
    requested_mode: str | None = None,
) -> bool:
    """True only when every requested host applied in the requested mode; mixed outcomes are never success."""
    if not results or any(entry.get("ok") is not True for entry in results):
        return False
    if hosts is None:
        return True
    by_host = {row["host"]: row for row in hosts}
    for entry in results:
        row = by_host.get(entry["host"])
        if row is None or row.get("status") != "configured":
            return False
        if requested_mode is not None and row.get("mode") != requested_mode:
            return False
    return True


def default_adapter_factory(command: tuple[str, ...] | None) -> AdapterCall | None:
    """Spawn the configured argv with JSON on stdin. ``None`` command means no process."""
    if not command:
        return None

    async def run(payload: dict[str, Any]) -> dict[str, Any]:
        return await invoke_configured_adapter(command, payload)

    return run


async def invoke_configured_adapter(
    command: Sequence[str],
    payload: Mapping[str, Any],
    *,
    timeout: float = _ADAPTER_TIMEOUT,
    max_stdout: int = _MAX_ADAPTER_STDOUT,
    max_stderr: int = _MAX_ADAPTER_STDERR,
) -> dict[str, Any]:
    """Run ``command`` as argv (never a shell) with bounded JSON stdin/stdout."""
    stdin_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise AdapterError(ADAPTER_UNAVAILABLE) from exc
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None

    async def _read(stream: asyncio.StreamReader, limit: int) -> bytes:
        data = bytearray()
        while chunk := await stream.read(16 * 1024):
            data.extend(chunk)
            if len(data) > limit:
                raise AdapterError(ADAPTER_INVALID)
        return bytes(data)

    async def _exchange() -> tuple[bytes, bytes, int]:
        assert process.stdin is not None
        process.stdin.write(stdin_bytes)
        await process.stdin.drain()
        process.stdin.close()
        stdout, stderr, code = await asyncio.gather(
            _read(process.stdout, max_stdout),
            _read(process.stderr, max_stderr),
            process.wait(),
        )
        return stdout, stderr, code

    try:
        stdout, _stderr, code = await asyncio.wait_for(_exchange(), timeout=timeout)
        if code:
            # Nonzero exit is a failed run even when stdout happens to be well-formed JSON.
            raise AdapterError(ADAPTER_INVALID)
        try:
            parsed = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(ADAPTER_INVALID) from exc
        if not isinstance(parsed, dict):
            raise AdapterError(ADAPTER_INVALID)
        return parsed
    except TimeoutError as exc:
        raise AdapterError(ADAPTER_UNAVAILABLE) from exc
    except OSError as exc:
        raise AdapterError(ADAPTER_UNAVAILABLE) from exc
    finally:
        if process is not None and process.returncode is None:
            await _stop_adapter(process)


async def _stop_adapter(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    killpg = getattr(os, "killpg", None)
    try:
        if killpg is not None and process.pid:
            killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (AttributeError, ProcessLookupError, OSError, PermissionError):
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
    with contextlib.suppress(TimeoutError, ProcessLookupError, OSError):
        await asyncio.wait_for(process.wait(), 0.5)
