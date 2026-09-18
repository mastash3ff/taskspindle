"""The ``[web]`` remote-access opt-in, and forwarding a live doctor check to a trusted controller.

TaskSpindle's dashboard trusts loopback by default. Running it in a container behind a private
VPN (see ``docs/dashboard.md``) means an operator may deliberately name a handful of exact remote
``Host`` netlocs the dashboard should also answer to (``allowed_hosts``), and opt the
loopback-only policy surfaces into trusting them the same way (``allow_remote_policy``). Both
settings default to the original loopback-only behavior when the table or field is simply absent.
A malformed value that *is* present is a configuration mistake, not a hint to guess at a safer
interpretation: it is rejected with :class:`~taskspindle.config.ConfigError` at startup, the same
way every other malformed table in this config file is, rather than silently degrading a safety
setting or silently falling back to a local check it cannot truthfully perform. The same table
also names an optional Unix control socket (``diagnostics_socket``): a dashboard running in a
container cannot truthfully run the local doctor checks itself, so when configured, ``GET
/api/doctor`` forwards its request to a trusted controller process over that socket instead of
ever probing the container it runs in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import rpc
from ..config import ConfigError, load_config
from .security import parse_netloc

__all__ = ["RemoteAccessConfig", "forward_doctor", "load_remote_access_config"]

_MAX_HOSTS = 32
_DOCTOR_TIMEOUT_SECONDS = 90.0
_CHECK_FIELDS = {"name", "ok", "detail", "advisory"}
_REPORT_FIELDS = {"ok", "checks"}


@dataclass(frozen=True)
class RemoteAccessConfig:
    """The ``[web]`` table's trust settings; every field defaults to its original off state."""

    allowed_hosts: frozenset[str]
    allow_remote_policy: bool
    diagnostics_socket: Path | None


_DEFAULT = RemoteAccessConfig(
    allowed_hosts=frozenset(), allow_remote_policy=False, diagnostics_socket=None
)


def load_remote_access_config(config_file: Path) -> RemoteAccessConfig:
    """Read ``[web]`` from the existing TaskSpindle config file.

    A missing file or a missing ``[web]`` table both yield the loopback-only default: nothing was
    configured, so nothing widens. Once the table is present, every field it names must be
    well-formed; a field it omits keeps its own default. Any of that failing to hold - the table
    itself not being a table, an unparsable host, the wrong type, a relative diagnostics path -
    raises :class:`ConfigError` rather than quietly falling back, exactly as a malformed
    ``[concurrency]`` table already does elsewhere in this file.
    """
    settings = load_config(config_file)
    table = settings.get("web")
    if table is None:
        return _DEFAULT
    if not isinstance(table, dict):
        raise ConfigError("[web] must be a table")
    return RemoteAccessConfig(
        allowed_hosts=_parse_hosts(table),
        allow_remote_policy=_parse_bool(table, "allow_remote_policy"),
        diagnostics_socket=_parse_socket(table),
    )


def _parse_bool(table: dict[str, Any], key: str) -> bool:
    if key not in table:
        return False
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"[web] {key} must be true or false")
    return value


def _parse_hosts(table: dict[str, Any]) -> frozenset[str]:
    if "allowed_hosts" not in table:
        return frozenset()
    value = table["allowed_hosts"]
    if not isinstance(value, list) or len(value) > _MAX_HOSTS:
        raise ConfigError(f"[web] allowed_hosts must be a list of at most {_MAX_HOSTS} host[:port] strings")
    hosts: set[str] = set()
    for item in value:
        if not isinstance(item, str) or parse_netloc(item) is None:
            raise ConfigError("[web] allowed_hosts contains an invalid host[:port] entry")
        hosts.add(item)
    return frozenset(hosts)


def _parse_socket(table: dict[str, Any]) -> Path | None:
    if "diagnostics_socket" not in table:
        return None
    value = table["diagnostics_socket"]
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError("[web] diagnostics_socket must be an absolute path string")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError("[web] diagnostics_socket must be an absolute path")
    return path


def _unavailable(code: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "checks": [{"name": "diagnostics_socket", "ok": False, "detail": detail, "advisory": False}],
        "status": "unavailable",
        "error": code,
    }


def _valid_check(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and set(item) == _CHECK_FIELDS
        and isinstance(item["name"], str)
        and isinstance(item["ok"], bool)
        and isinstance(item["detail"], str)
        and isinstance(item["advisory"], bool)
    )


def _valid_report(value: Any) -> bool:
    """The exact ``run_doctor_async`` shape: nothing more, nothing less, nothing mistyped."""
    return (
        isinstance(value, dict)
        and set(value) == _REPORT_FIELDS
        and isinstance(value["ok"], bool)
        and isinstance(value["checks"], list)
        and all(_valid_check(item) for item in value["checks"])
    )


async def forward_doctor(socket_path: Path, *, live: bool) -> dict[str, Any]:
    """Ask the trusted controller for a doctor report; never run local checks here.

    A control failure (socket missing, refused, malformed reply) is reported as a clear
    ``unavailable`` result with the same ``ok``/``checks`` shape ``run_doctor_async`` returns,
    never as a silent fallback to a check this container cannot truthfully perform.
    """
    try:
        result = await asyncio.to_thread(
            rpc.request, socket_path, "doctor", {"live": live}, timeout=_DOCTOR_TIMEOUT_SECONDS
        )
    except rpc.RemoteError as exc:
        return _unavailable(exc.code, str(exc))
    if not _valid_report(result):
        return _unavailable("CONTROL_PROTOCOL_ERROR", "Malformed control response")
    return result
