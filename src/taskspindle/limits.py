"""Classifying a provider's refusal, and what TaskSpindle then believes about that provider.

Nothing here retries, re-queues or switches providers. A quota or auth refusal is *recorded*:
on the task that hit it, on the provider it came from, and in ``capabilities`` for the next
caller to read. Choosing another provider stays a decision the caller makes with that fact in
hand -- the "never a fallback" rule in ``docs/configuration.md`` is unchanged.

Everything in this module is pure: it takes an :class:`~taskspindle.acp_client.AcpError` or a
stored row and returns plain data, so it can be tested without an agent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .acp_client import AcpError
from .providers import Profile, opposite_provider

__all__ = [
    "AUTH_ERROR_KINDS",
    "PROVIDER_AUTH_EXPIRED",
    "PROVIDER_THROTTLED",
    "PROVIDER_UNAVAILABLE",
    "THROTTLE_ERROR_KINDS",
    "USAGE_LIMIT_PREFIXES",
    "Classification",
    "classify_acp_error",
    "effective_state",
    "epoch_to_iso",
    "parse_claude_limit_text",
    "rate_limit_window",
    "status_key",
    "suggested_alternative",
]

#: The turn was refused because a usage, rate or credit limit was reached.
PROVIDER_THROTTLED = "PROVIDER_THROTTLED"
#: The turn was refused because the seat is logged out or not allowed.
PROVIDER_AUTH_EXPIRED = "PROVIDER_AUTH_EXPIRED"
#: ``start_task`` refused because the provider is currently believed to be in one of the above.
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

#: ``errorKind`` values of the Claude Agent SDK (``SDKAssistantMessageError`` in
#: ``@anthropic-ai/claude-agent-sdk`` ``sdk.d.ts``) that mean the seat cannot be used.
AUTH_ERROR_KINDS: frozenset[str] = frozenset({"authentication_failed", "oauth_org_not_allowed"})

#: ``errorKind`` values that mean a limit was reached.
THROTTLE_ERROR_KINDS: frozenset[str] = frozenset({"rate_limit", "billing_error"})

#: Message prefixes the Claude Agent SDK uses when a limit was genuinely reached
#: (``USAGE_LIMIT_ERROR_PREFIXES`` in ``sdk.d.ts``, adapter 0.70.0), plus the older sentinel.
USAGE_LIMIT_PREFIXES: tuple[str, ...] = (
    "You've hit your",
    "You've reached your",
    "You're out of usage credits",
    "Your org is out of usage",
    "Your seat type doesn't include usage",
    "Your usage allocation has been disabled by your admin",
    "Your group's usage limit is set to $0",
    "Fable 5 requires usage credits",
    "You're out of extra usage",
    "Your seat type doesn't include extra usage",
    "Claude AI usage limit reached",
)

_CLAUDE_LIMIT_SENTINEL = re.compile(r"usage limit reached\|(\d{9,13})")
_WINDOW_WORDS: tuple[tuple[str, str], ...] = (
    ("five_hour", "five_hour"),
    ("5-hour", "five_hour"),
    ("5 hour", "five_hour"),
    ("session limit", "five_hour"),
    ("seven_day", "seven_day"),
    ("7-day", "seven_day"),
    ("weekly", "seven_day"),
)
_GROK_THROTTLE_MARKS = ("429", "rate limit", "too many requests", "quota exceeded")
_GROK_AUTH_MARKS = ("401", "unauthorized", "token expired", "not logged in", "not authenticated")

#: ``rateLimitType`` values the SDK's ``SDKRateLimitInfo`` may carry.
_KNOWN_WINDOWS = frozenset(
    {
        "five_hour",
        "seven_day",
        "seven_day_opus",
        "seven_day_sonnet",
        "seven_day_overage_included",
        "overage",
    }
)

_REASON_LIMIT = 200


@dataclass(frozen=True)
class Classification:
    """What a failed turn means for the provider that produced it."""

    #: The code the task's error carries: one of this module's, or the original ``AcpError`` code.
    code: str
    #: ``throttled``, ``auth_expired``, or ``None`` when the failure says nothing about the seat.
    provider_state: str | None
    #: Which usage window was hit, when the provider said; ``unknown`` for a limit without one.
    window: str | None
    #: ISO-8601 time the limit lifts, when the provider said.
    reset_at: str | None
    retryable: bool
    #: One line for a person, cut short and free of anything but the provider's own words.
    reason: str
    source: str = "acp_error"


def epoch_to_iso(value: Any) -> str | None:
    """A Unix timestamp in seconds or milliseconds, as an ISO-8601 ``Z`` string."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1e11:  # milliseconds
        number /= 1000.0
    if number <= 0:
        return None
    return datetime.fromtimestamp(number, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _window_from_words(text: str) -> str | None:
    lowered = text.lower()
    for mark, window in _WINDOW_WORDS:
        if mark in lowered:
            return window
    return None


def parse_claude_limit_text(text: str) -> tuple[str | None, str | None]:
    """``(window, reset_at)`` from a Claude usage-limit message, when the text carries them."""
    reset_at = None
    match = _CLAUDE_LIMIT_SENTINEL.search(text)
    if match:
        reset_at = epoch_to_iso(match.group(1))
    return _window_from_words(text), reset_at


def _is_usage_limit_text(text: str) -> bool:
    # The adapter wraps the SDK's message: "session/prompt failed: You've hit your limit".
    return any(prefix in text for prefix in USAGE_LIMIT_PREFIXES)


def _reason(exc: AcpError) -> str:
    message = str(exc.cause.get("rpc_message") or exc)
    return message.splitlines()[0][:_REASON_LIMIT]


def classify_acp_error(exc: AcpError, *, family: str) -> Classification:
    """Decide what an ``AcpError`` means, from its wire cause, in a fixed order of evidence.

    1. JSON-RPC ``-32000`` is the ACP "authentication required" code.
    2. The Claude adapter's ``data.errorKind`` names the SDK's own failure kind.
    3. The message text names a usage limit (the SDK's prefixes, or the classic sentinel).
    4. For any other family, a few conservative substrings; Grok documents no limit telemetry.
    5. Otherwise the original code, retryable unless it was a timeout.
    """
    cause = exc.cause
    text = str(cause.get("rpc_message") or exc)
    rpc_code = cause.get("rpc_code")
    data = cause.get("rpc_data")
    error_kind = data.get("errorKind") if isinstance(data, dict) else None

    if rpc_code == -32000 or error_kind in AUTH_ERROR_KINDS:
        return Classification(
            PROVIDER_AUTH_EXPIRED, "auth_expired", None, None, False, _reason(exc)
        )
    if error_kind in THROTTLE_ERROR_KINDS:
        window, reset_at = parse_claude_limit_text(text)
        return Classification(
            PROVIDER_THROTTLED, "throttled", window or "unknown", reset_at, True, _reason(exc)
        )
    if _is_usage_limit_text(text):
        window, reset_at = parse_claude_limit_text(text)
        return Classification(
            PROVIDER_THROTTLED, "throttled", window or "unknown", reset_at, True, _reason(exc)
        )
    if family != "claude":
        lowered = text.lower()
        if any(mark in lowered for mark in _GROK_THROTTLE_MARKS):
            return Classification(
                PROVIDER_THROTTLED, "throttled", "unknown", None, True, _reason(exc)
            )
        if any(mark in lowered for mark in _GROK_AUTH_MARKS):
            return Classification(
                PROVIDER_AUTH_EXPIRED, "auth_expired", None, None, False, _reason(exc)
            )
    return Classification(
        exc.code, None, None, None, exc.code != "TURN_TIMEOUT", _reason(exc)
    )


def status_key(profile: Profile) -> str:
    """The provider row a profile's limits are recorded under.

    An OAuth profile derived from ``claude`` uses the same seat as ``claude`` itself, so it shares
    that seat's throttle; an ``api_key`` profile has a key of its own and is tracked by its id.
    """
    return profile.family if profile.auth == "oauth" else profile.id


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def effective_state(row: Mapping[str, Any] | None, now: datetime) -> str:
    """What a stored ``provider_status`` row means right now.

    ``unknown`` when nothing was ever recorded; a throttle whose ``reset_at`` has passed reads as
    ``ok`` again, because the provider said so; everything else is what the row says.
    """
    if row is None:
        return "unknown"
    state = str(row.get("state") or "unknown")
    if state == "throttled":
        reset_at = _parse_iso(row.get("reset_at"))
        if reset_at is not None and reset_at <= now:
            return "ok"
    return state


def rate_limit_window(info: Mapping[str, Any]) -> dict[str, Any]:
    """A ``provider_windows`` row from the SDK's ``SDKRateLimitInfo``.

    ``utilization`` is stored as a percentage; a value at or below one is taken to be a fraction.
    """
    raw_type = info.get("rateLimitType")
    window = raw_type if isinstance(raw_type, str) and raw_type in _KNOWN_WINDOWS else "unknown"
    utilization = info.get("utilization")
    used_percent: float | None = None
    if isinstance(utilization, int | float):
        used_percent = float(utilization) * 100.0 if utilization <= 1.0 else float(utilization)
    status = info.get("status")
    return {
        "window": window,
        "status": status if isinstance(status, str) else None,
        "used_percent": used_percent,
        "resets_at": epoch_to_iso(info.get("resetsAt")),
    }


def suggested_alternative(provider_id: str) -> str | None:
    """The other first-class provider, when there is one; never chosen on the caller's behalf."""
    return opposite_provider(provider_id)
