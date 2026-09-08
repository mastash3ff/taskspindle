"""Classifying a provider's refusal, and what TaskSpindle then believes about that provider.

Nothing here retries, re-queues or switches providers. A quota or auth refusal is *recorded*:
on the task that hit it, on the provider it came from, and in ``capabilities`` for the next
caller to read. Choosing another provider stays a decision the caller makes with that fact in
hand -- the "never a fallback" rule in ``docs/configuration.md`` is unchanged.

Everything in this module is pure: it takes an :class:`~taskspindle.acp_client.AcpError` or a
stored row and returns plain data, so it can be tested without an agent.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .acp_client import AcpError, safe_retry_summary
from .providers import Profile, opposite_provider

__all__ = [
    "ACCESS_ERROR_KINDS",
    "AUTH_ERROR_KINDS",
    "MODEL_ERROR_KINDS",
    "PROVIDER_ACCESS_DENIED",
    "PROVIDER_AUTH_EXPIRED",
    "PROVIDER_MODEL_UNAVAILABLE",
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
    "safe_provider_reason",
    "safe_provider_source",
    "safe_rpc_data",
    "safe_status_row",
    "status_fingerprint",
    "status_key",
    "status_override_matches",
    "suggested_alternative",
]

#: The turn was refused because a usage, rate or credit limit was reached.
PROVIDER_THROTTLED = "PROVIDER_THROTTLED"
#: The turn was refused because the seat is logged out or not allowed.
PROVIDER_AUTH_EXPIRED = "PROVIDER_AUTH_EXPIRED"
#: The provider explicitly refused access, without claiming the login itself is invalid.
PROVIDER_ACCESS_DENIED = "PROVIDER_ACCESS_DENIED"
#: The requested model, rather than the account, is unavailable.
PROVIDER_MODEL_UNAVAILABLE = "PROVIDER_MODEL_UNAVAILABLE"
#: ``start_task`` refused because the provider is currently believed to be in one of the above.
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"

#: ``errorKind`` values of the Claude Agent SDK (``SDKAssistantMessageError`` in
#: ``@anthropic-ai/claude-agent-sdk`` ``sdk.d.ts``) that mean the seat cannot be used.
AUTH_ERROR_KINDS: frozenset[str] = frozenset({"authentication_failed", "oauth_org_not_allowed"})

#: An SDK billing refusal does not prove a subscription lapsed.  It does prove that this account
#: may not take the requested turn until a person reviews its access or billing state.
ACCESS_ERROR_KINDS: frozenset[str] = frozenset({"billing_error"})

#: Only the SDK's explicit model-scoped refusal is allowed to disable one model.
MODEL_ERROR_KINDS: frozenset[str] = frozenset({"model_unavailable"})

#: ``errorKind`` values that mean a limit was reached.
THROTTLE_ERROR_KINDS: frozenset[str] = frozenset({"rate_limit"})

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
_GROK_HTTP_STATUSES = frozenset({401, 402, 403, 429})
_GROK_CREDIT_MARK = re.compile(
    r"\b(?:insufficient|out of|no)\s+(?:usage\s+)?credits?\b"
    r"|\bcredits?\s+(?:are\s+)?(?:exhausted|depleted)\b"
    r"|\b(?:team|organization|org|account)\s+(?:has\s+)?run\s+out\s+of\s+(?:usage\s+)?credits?\b"
    r"|\b(?:team|organization|org|account)\b.{0,80}\bspending\s+limit\b",
    re.IGNORECASE,
)

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
    #: ``account`` refusals apply to the OAuth family/API profile; ``model`` to one model only.
    scope: str = "account"
    affected_model: str | None = None


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


def _explicit_reset_at(data: Any) -> str | None:
    """Accept only a provider-supplied absolute reset time; never estimate one."""
    if not isinstance(data, Mapping):
        return None
    for field in ("reset_at", "resetAt"):
        value = data.get(field)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        else:
            if (converted := epoch_to_iso(value)) is not None:
                return converted
    return None


def _grok_http_status(cause: Mapping[str, Any], *, acp_code: str) -> int | None:
    """A structured final provider transport status, never a number scraped from prose."""
    if acp_code in {"TURN_TIMEOUT", "ACP_TURN_ERROR"}:
        retry = safe_retry_summary(cause.get("last_retry"), trusted_summary=True)
        status = retry.get("http_status")
        if retry.get("origin") == "xai_responses" and status in _GROK_HTTP_STATUSES:
            return status
    data = cause.get("rpc_data")
    if isinstance(data, Mapping):
        for field in ("http_status", "httpStatus", "status_code", "statusCode"):
            value = data.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value in _GROK_HTTP_STATUSES:
                return value
    return None


def _grok_retry_classification(cause: Mapping[str, Any], *, acp_code: str) -> Classification | None:
    """The xAI terminal `RetryState::Exhausted` is explicit quota evidence, without prose."""
    if acp_code not in {"TURN_TIMEOUT", "ACP_TURN_ERROR"}:
        return None
    retry = safe_retry_summary(cause.get("last_retry"), trusted_summary=True)
    if retry.get("origin") == "xai_responses" and retry.get("provider_state") == "throttled":
        return Classification(
            PROVIDER_THROTTLED, "throttled", "unknown", None, True,
            _SAFE_REASONS["throttled"],
        )
    if retry.get("origin") == "xai_responses" and retry.get("provider_state") == "access_denied":
        return Classification(
            PROVIDER_ACCESS_DENIED, "access_denied", None, None, False,
            _SAFE_REASONS["access_denied"],
        )
    return None


def _grok_credit_evidence(data: Any, rpc_message: Any, retry: Mapping[str, Any]) -> bool:
    """Whether a source-proven terminal 403's bounded detail says credits were exhausted."""
    if retry.get("origin") != "xai_responses" or retry.get("type") != "failed":
        return False
    # Grok's terminal ``error_data_with_status`` envelope puts the provider message in ``message``.
    # ``rpc_message`` is considered only after the structured status above has established this as
    # an ACP provider transport error, never as freestanding text from a tool call.
    if retry.get("credit_evidence") is True:
        return True
    if not isinstance(data, Mapping):
        return False
    values = (data.get("message"), data.get("error"), data.get("code"), data.get("error_code"), rpc_message)
    return any(isinstance(value, str) and _GROK_CREDIT_MARK.search(value[:512]) for value in values)


def _grok_status_classification(
    status: int, data: Any, rpc_message: Any, retry: Mapping[str, Any],
) -> Classification | None:
    """Map only documented provider-level status facts; ambiguous 403 stays generic."""
    if status == 401:
        return Classification(
            PROVIDER_AUTH_EXPIRED, "auth_expired", None, None, False,
            _SAFE_REASONS["auth_expired"],
        )
    if status == 429:
        return Classification(
            PROVIDER_THROTTLED, "throttled", "unknown", _explicit_reset_at(data), True,
            _SAFE_REASONS["throttled"],
        )
    if status == 402:
        return Classification(
            PROVIDER_ACCESS_DENIED, "access_denied", None, None, False,
            _SAFE_REASONS["access_denied"],
        )
    # xAI uses 403 for several unrelated authorization policies.  It proves account access only
    # when a structured provider error explicitly identifies credits; a bare 403 is no evidence.
    if status == 403 and _grok_credit_evidence(data, rpc_message, retry):
        return Classification(
            PROVIDER_ACCESS_DENIED, "access_denied", None, None, False,
            _SAFE_REASONS["access_denied"],
        )
    return None


_SAFE_REASONS = {
    "auth_expired": "Provider authentication is required.",
    "throttled": "The provider reported a usage limit.",
    "access_denied": "The provider denied account access.",
    "model_unavailable": "The requested model is unavailable.",
}

_SAFE_SOURCES = frozenset({"acp_error", "rate_limit_event", "turn_ok", "native_auth_check"})


def safe_provider_reason(state: Any) -> str | None:
    """A fixed display reason for a provider availability state."""
    return _SAFE_REASONS.get(state) if isinstance(state, str) else None


def safe_provider_source(source: Any) -> str | None:
    """Expose only provenance labels emitted by the availability implementation."""
    if not isinstance(source, str):
        return None
    return source if source in _SAFE_SOURCES else "legacy"


def safe_status_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Preserve a legacy status row's shape while replacing provider-controlled prose."""
    if row is None:
        return None
    result = dict(row)
    result["reason"] = safe_provider_reason(row.get("state"))
    result["source"] = safe_provider_source(row.get("source"))
    return result


_STATUS_IDENTITY_FIELDS = (
    "provider", "model", "state", "code", "window", "reset_at", "observed_at", "task_id", "source",
)


def status_fingerprint(row: Mapping[str, Any] | None) -> str | None:
    """Opaque identity for one availability observation, excluding prose and success metadata."""
    if row is None:
        return None
    identity = {field: row.get(field) for field in _STATUS_IDENTITY_FIELDS}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def status_override_matches(
    payload: Mapping[str, Any], *, status_key_value: str, model: str | None,
    account_status: Mapping[str, Any] | None, model_status: Mapping[str, Any] | None,
) -> bool:
    """Whether a warning still describes the exact evidence a first prompt will use.

    An override created before the agent resolves its default model may follow that resolution only
    while no refusal exists for the resolved model. Explicitly requested models always match exactly.
    """
    if (
        payload.get("code") != "PROVIDER_STATUS_OVERRIDE"
        or payload.get("status_key") != status_key_value
        or payload.get("account_status_fingerprint") != status_fingerprint(account_status)
    ):
        return False
    captured_model = payload.get("model")
    if captured_model is None:
        return (
            payload.get("model_status_fingerprint") is None
            and (model_status is None or model_status.get("state") == "ok")
        )
    return (
        captured_model == model
        and payload.get("model_status_fingerprint") == status_fingerprint(model_status)
    )


def _model(value: Any) -> str | None:
    """A bounded model identifier safe to persist and display."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+/@-]*", value) is None:
        return None
    return value


def safe_rpc_data(value: Any) -> dict[str, str]:
    """Return only bounded classifier fields from provider-controlled RPC data."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    kind = value.get("errorKind")
    known_kinds = AUTH_ERROR_KINDS | ACCESS_ERROR_KINDS | MODEL_ERROR_KINDS | THROTTLE_ERROR_KINDS
    if isinstance(kind, str) and kind in known_kinds:
        result["errorKind"] = kind
    model = _model(value.get("model") or value.get("modelId") or value.get("model_id"))
    if model is not None:
        result["model"] = model
    return result


def classify_acp_error(exc: AcpError, *, family: str, model: str | None = None) -> Classification:
    """Decide what an ``AcpError`` means, from its wire cause, in a fixed order of evidence.

    1. JSON-RPC ``-32000`` is the ACP "authentication required" code.
    2. The Claude adapter's ``data.errorKind`` names the SDK's own failure kind.
    3. The message text names a usage limit (the SDK's prefixes, or the classic sentinel).
    4. Grok accepts only terminal source-proven retry state or structured provider transport
       statuses; generic error text may be a tool result and is never seat evidence.
    5. Otherwise the original code, retryable unless it was a timeout.
    """
    cause = exc.cause
    text = str(cause.get("rpc_message") or exc)
    rpc_code = cause.get("rpc_code")
    data = cause.get("rpc_data")
    raw_error_kind = data.get("errorKind") if isinstance(data, Mapping) else None
    error_kind = raw_error_kind if isinstance(raw_error_kind, str) else None

    if rpc_code == -32000 or error_kind in AUTH_ERROR_KINDS:
        return Classification(
            PROVIDER_AUTH_EXPIRED, "auth_expired", None, None, False,
            _SAFE_REASONS["auth_expired"],
        )
    if error_kind in ACCESS_ERROR_KINDS:
        return Classification(
            PROVIDER_ACCESS_DENIED, "access_denied", None, None, False,
            _SAFE_REASONS["access_denied"],
        )
    if error_kind in MODEL_ERROR_KINDS:
        # Bind the refusal to the model this task actually selected. Provider-controlled metadata
        # may name a fallback or stale model and must never disable a different requested model.
        affected = _model(model)
        return Classification(
            PROVIDER_MODEL_UNAVAILABLE, "model_unavailable", None, None, False,
            _SAFE_REASONS["model_unavailable"], scope="model", affected_model=affected,
        )
    if error_kind in THROTTLE_ERROR_KINDS:
        window, reset_at = parse_claude_limit_text(text)
        return Classification(
            PROVIDER_THROTTLED, "throttled", window or "unknown", reset_at, True,
            _SAFE_REASONS["throttled"],
        )
    if _is_usage_limit_text(text):
        window, reset_at = parse_claude_limit_text(text)
        return Classification(
            PROVIDER_THROTTLED, "throttled", window or "unknown", reset_at, True,
            _SAFE_REASONS["throttled"],
        )
    # Text can be a tool's HTTP error or contain incidental numbers.  The Grok adapter's
    # terminal structured transport status is the only accepted provider-level evidence.
    if family == "grok":
        if (classification := _grok_retry_classification(cause, acp_code=exc.code)) is not None:
            return classification
        retry = safe_retry_summary(cause.get("last_retry"), trusted_summary=True)
        if (
            (status := _grok_http_status(cause, acp_code=exc.code)) is not None
            and (classification := _grok_status_classification(
                status, data, cause.get("rpc_message"), retry,
            )) is not None
        ):
            return classification
    return Classification(
        exc.code, None, None, None, exc.code != "TURN_TIMEOUT", "The provider turn failed."
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

    ``unknown`` when nothing was ever recorded; a throttle whose ``reset_at`` has passed is stale
    and retryable, but is not evidence of a successful turn.  Everything else is what the row says.
    """
    if row is None:
        return "unknown"
    state = str(row.get("state") or "unknown")
    if state == "throttled":
        reset_at = _parse_iso(row.get("reset_at"))
        if reset_at is not None and reset_at <= now:
            return "unknown"
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
