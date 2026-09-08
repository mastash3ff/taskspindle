"""Session-free Grok billing diagnostics. Provider credentials stay inside Grok."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from . import providers
from .providers import Profile

TIMEOUT_S = 30
MAX_OUTPUT = 1_048_576


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


def parse_billing(value: Any) -> dict[str, Any] | None:
    """Allowlist the current native schema; money, principal IDs and raw data are discarded."""
    if not isinstance(value, dict) or not isinstance(value.get("config"), dict):
        return None
    config = value["config"]
    percent = config.get("creditUsagePercent")
    if type(percent) not in (int, float) or not math.isfinite(percent) or not 0 <= percent <= 100:
        return None
    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        return None
    raw_type = period.get("type")
    window = (
        {"USAGE_PERIOD_TYPE_WEEKLY": "weekly", "USAGE_PERIOD_TYPE_MONTHLY": "monthly"}.get(raw_type)
        if isinstance(raw_type, str)
        else None
    )
    start, end = parse_time(period.get("start")), parse_time(period.get("end"))
    if window is None or end is None or (start is not None and start >= end):
        return None
    return {
        "used_percent": float(percent),
        "window": window,
        "period_start": start.isoformat().replace("+00:00", "Z") if start else None,
        "reset_at": end.isoformat().replace("+00:00", "Z"),
    }


class CheckFailure(Exception):
    def __init__(self, code: str):
        self.code = code


def _rpc(
    process: subprocess.Popen,
    selector: selectors.BaseSelector,
    method: str,
    request_id: int,
    deadline: float,
    buffer: bytearray,
    params: dict,
) -> Any:
    process.stdin.write(
        (json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode()
    )
    process.stdin.flush()
    size = 0
    while True:
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer[:] = rest
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                raise CheckFailure("INVALID_RESPONSE") from None
            if not isinstance(message, dict):
                raise CheckFailure("INVALID_RESPONSE")
            if "method" in message:
                # An initialized billing client supplies no file, terminal, or permission API.
                if "id" in message:
                    process.stdin.write(
                        (
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": message["id"],
                                    "error": {"code": -32601, "message": "Unsupported method"},
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    process.stdin.flush()
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                code = error.get("code") if isinstance(error, dict) else None
                raise CheckFailure(
                    {-32601: "METHOD_UNAVAILABLE", -32000: "AUTH_REQUIRED"}.get(code, "CHECK_FAILED")
                )
            return message.get("result")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CheckFailure("TIMEOUT")
        if not selector.select(remaining):
            raise CheckFailure("TIMEOUT")
        data = os.read(process.stdout.fileno(), 65536)
        if not data:
            raise CheckFailure("CHECK_FAILED")
        size += len(data)
        if size > MAX_OUTPUT:
            raise CheckFailure("INVALID_RESPONSE")
        buffer.extend(data)


def _stop_owned(process: subprocess.Popen) -> bool:
    """Signal only an unreaped owned leader; cleanup failures never leak process diagnostics."""
    cleaned = True
    try:
        if process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        cleaned = False
    finally:
        for stream in (process.stdin, process.stdout):
            if stream:
                with suppress(OSError):
                    stream.close()
    return cleaned


def _version(executable: str, env: dict[str, str], cwd: str, deadline: float) -> str:
    process = subprocess.Popen(
        [executable, "--version"],
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise CheckFailure("TIMEOUT")
                data = os.read(process.stdout.fileno(), 2048)
                if not data:
                    break
                output.extend(data)
                if len(output) > 1024:
                    raise CheckFailure("VERSION_UNAVAILABLE")
        match = re.fullmatch(rb"grok (\d+\.\d+\.\d+)(?: \([a-f0-9]+\))?(?: \[[a-z]+\])?\s*", output)
        if not match or process.wait(timeout=max(0.01, deadline - time.monotonic())):
            raise CheckFailure("VERSION_UNAVAILABLE")
        return match[1].decode("ascii")
    finally:
        if not _stop_owned(process):
            raise CheckFailure("CLEANUP_FAILED")


def check_grok(profile: Profile, parent_env: Mapping[str, str]) -> dict[str, Any]:
    result = {
        "state": "check_failed",
        "source": "grok_billing",
        "checked_at": timestamp(),
        "account_binding": "unverified",
        "plan": None,
        "model_count": None,
        "version": None,
        "used_percent": None,
        "window": None,
        "period_start": None,
        "reset_at": None,
        "error_code": "CHECK_FAILED",
        "detail": "The native quota check did not establish current quota.",
    }
    deadline = time.monotonic() + TIMEOUT_S - 1  # reserve owned-process cleanup within 30 seconds
    process = None
    try:
        if (
            profile.family != "grok"
            or profile.auth != "oauth"
            or profile.secret_env
            or not profile.command
            or Path(profile.command[0]).name != "grok"
        ):
            raise CheckFailure("UNSUPPORTED_PROFILE")
        with TemporaryDirectory(prefix="taskspindle-grok-check-") as temporary:
            env = providers.build_child_env(profile, parent_env, task_tmp=Path(temporary))
            env.update(providers.GROK_COMPAT_ENV)
            env.update(
                NO_BROWSER="1", CI="1", TERM="dumb", TMPDIR=temporary, GROK_DISABLE_API_KEY_AUTH="true"
            )
            result["version"] = _version(profile.command[0], env, temporary, deadline)
            process = subprocess.Popen(
                [profile.command[0], "--no-subagents", "agent", "--no-leader", "stdio"],
                cwd=temporary,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                buffer = bytearray()
                initialized = _rpc(
                    process,
                    selector,
                    "initialize",
                    1,
                    deadline,
                    buffer,
                    {
                        "protocolVersion": 1,
                        "clientCapabilities": {},
                        "clientInfo": {"name": "taskspindle-quota", "version": "1"},
                    },
                )
                methods = initialized.get("authMethods", []) if isinstance(initialized, dict) else []
                if not any(isinstance(m, dict) and m.get("id") == "cached_token" for m in methods):
                    raise CheckFailure("AUTH_REQUIRED")
                billing = _rpc(process, selector, "_x.ai/billing", 2, deadline, buffer, {})
                quota = parse_billing(billing)
                if quota is None:
                    raise CheckFailure("SCHEMA_UNAVAILABLE")
                result.update(
                    quota,
                    state="quota",
                    error_code=None,
                    detail="Grok reported quota usage; model-turn access and account binding are unverified.",
                )
    except CheckFailure as exc:
        result["error_code"] = exc.code
    except subprocess.TimeoutExpired:
        result["error_code"] = "TIMEOUT"
    except Exception:
        pass
    finally:
        # Only the freshly created process group, never any existing Grok or browser.
        if process is not None and not _stop_owned(process):
            result.update(
                state="check_failed",
                error_code="CLEANUP_FAILED",
                used_percent=None,
                window=None,
                period_start=None,
                reset_at=None,
                detail="The native quota check did not establish current quota.",
            )
    if result["error_code"] in {"METHOD_UNAVAILABLE", "SCHEMA_UNAVAILABLE", "UNSUPPORTED_PROFILE"}:
        result.update(state="unsupported", detail="Native quota is unavailable for this Grok CLI/profile.")
    elif result["error_code"] == "AUTH_REQUIRED":
        result.update(
            state="auth_required", detail="Grok requires existing OAuth authentication; no login was started."
        )
    return result
