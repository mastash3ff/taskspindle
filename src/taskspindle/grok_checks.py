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
    """Allowlist quota fields; extra usage is parsed separately from account identity."""
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


def _amount(value: Any) -> int | None:
    # Preserve integer USD cents exactly, including in the JavaScript dashboard.
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _cent(value: Any) -> int | None:
    # Grok's native BillingConfig Cent is {val: i64}, in USD cents. The
    # documented proto3 representation of zero is {}, not an absent field.
    if not isinstance(value, dict):
        return None
    return _amount(0 if value == {} else value.get("val"))


def parse_auto_topup(value: Any) -> dict[str, Any] | None:
    """Read the native rule; never create or change a purchase instruction."""
    rule = value.get("rule") if isinstance(value, dict) else None
    if not isinstance(rule, dict):
        return None
    enabled = rule.get("enabled", False)  # proto3 omits false
    return {
        "enabled": enabled if type(enabled) is bool else None,
        "topup_amount": _cent(rule.get("topupAmount")),
        "max_amount_per_month": _cent(rule.get("maxAmountPerMonth")),
    }


def parse_extra_usage(value: Any) -> dict[str, Any]:
    """Allowlist native account billing; these are not per-task charges.

    Units and zero semantics: xai-org/grok-build,
    crates/codegen/xai-grok-shell/src/extensions/billing.rs (Cent).
    on_demand_enabled is a remote feature flag, not user spending permission.
    """
    value = value if isinstance(value, dict) else {}
    config = value.get("config")
    config = config if isinstance(config, dict) else {}
    enabled = value.get("on_demand_enabled")
    return {
        "on_demand_enabled": enabled if type(enabled) is bool else None,
        "prepaid_balance": _cent(config.get("prepaidBalance")),
        "on_demand_cap": _cent(config.get("onDemandCap")),
        "on_demand_used": _cent(config.get("onDemandUsed")),
        "unit": "usd_cents",
        "currency": "USD",
        "auto_topup": None,
    }


def safe_extra_usage(value: Any, observed_at: str | None) -> dict[str, Any] | None:
    """Revalidate persisted normalized observations at the public boundary."""
    if not isinstance(value, dict) or value.get("unit") != "usd_cents" or value.get("currency") != "USD":
        return None
    enabled = value.get("on_demand_enabled")
    rule = value.get("auto_topup")
    topup = None
    if isinstance(rule, dict):
        topup = {
            "enabled": rule.get("enabled") if type(rule.get("enabled")) is bool else None,
            "topup_amount": _amount(rule.get("topup_amount")),
            "max_amount_per_month": _amount(rule.get("max_amount_per_month")),
        }
    return {
        "on_demand_enabled": enabled if type(enabled) is bool else None,
        **{key: _amount(value.get(key)) for key in ("prepaid_balance", "on_demand_cap", "on_demand_used")},
        "unit": "usd_cents", "currency": "USD", "auto_topup": topup,
        "source": "grok_billing", "observed_at": observed_at,
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
        "billing": None,
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
                extra = parse_extra_usage(billing)
                # This extension is optional. Its absence or timeout cannot erase
                # the quota observation, and shares the existing bounded deadline.
                try:
                    topup = _rpc(process, selector, "_x.ai/auto-topup-rule", 3, deadline, buffer, {})
                    extra["auto_topup"] = parse_auto_topup(topup)
                except CheckFailure:
                    pass
                result["billing"] = safe_extra_usage(extra, result["checked_at"])
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
                billing=None,
                detail="The native quota check did not establish current quota.",
            )
    if result["error_code"] in {"METHOD_UNAVAILABLE", "SCHEMA_UNAVAILABLE", "UNSUPPORTED_PROFILE"}:
        result.update(state="unsupported", detail="Native quota is unavailable for this Grok CLI/profile.")
    elif result["error_code"] == "AUTH_REQUIRED":
        result.update(
            state="auth_required", detail="Grok requires existing OAuth authentication; no login was started."
        )
    return result
