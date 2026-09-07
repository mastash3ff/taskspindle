"""Validated subscription observations and presentation calculations.

This module is deliberately IO-free.  Billing dates are accepted only as values
reported by a provider collector; the helpers here validate and present those
values without inventing renewal cycles from unrelated clocks.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

PROVIDERS: dict[str, dict[str, str]] = {
    "chatgpt": {
        "label": "ChatGPT",
        "billing_url": "https://chatgpt.com/#settings/Billing",
    },
    "claude": {
        "label": "Claude",
        "billing_url": "https://claude.ai/new#settings/billing",
    },
    "google_ai": {
        "label": "Google AI",
        "billing_url": "https://one.google.com/settings",
    },
    "grok": {
        "label": "Grok",
        "billing_url": "https://grok.com/?_s=usage",
    },
}
PROVIDER_IDS = tuple(PROVIDERS)
ACTIONS = frozenset({"connect", "refresh"})
BILLING_CHANNELS = frozenset(
    {"provider_web", "apple", "google_play", "x_premium", "unknown"}
)
SUBSCRIPTION_STATUSES = frozenset(
    {"renewing", "cancelled", "expired", "free", "none", "unknown"}
)
DATE_PRECISIONS = frozenset({"date", "datetime"})
PLAN_PATTERNS = {
    "chatgpt": re.compile(r"(?:(?:ChatGPT )?(?:Plus|Pro|Go|Free)|None)", re.IGNORECASE),
    "claude": re.compile(
        r"(?:(?:Claude )?(?:Pro|Max(?:\s*\((?:5x|20x)\))?|Free)(?: plan)?|None)",
        re.IGNORECASE,
    ),
    "google_ai": re.compile(
        r"(?:(?:Google AI (?:Pro|Ultra|Plus)|AI Premium)(?:\s*\(\d+\s*(?:GB|TB)\))?|None)",
        re.IGNORECASE,
    ),
    "grok": re.compile(r"(?:(?:SuperGrok(?: Heavy)?|Free)(?: plan)?|None)", re.IGNORECASE),
}

# Collector errors are reduced to this allowlist before persistence or display.
# Raw exception and network text must never cross the browser process boundary.
ERROR_MESSAGES: dict[str, str] = {
    "AUTH_REQUIRED": "Sign in is required to check this subscription.",
    "ACCOUNT_MISMATCH": "The signed-in account does not match the connected account.",
    "UNSUPPORTED_BILLING_CHANNEL": "This billing channel must be managed separately.",
    "PARSE_CHANGED": "The provider subscription details could not be read.",
    "INVALID_REQUEST": "The subscription collector request was invalid.",
    "BROWSER_UNAVAILABLE": "Chrome is unavailable for this subscription check.",
    "PROFILE_BUSY": "A subscription browser operation is already running.",
    "TIMEOUT": "The subscription check timed out.",
    "SETUP_REQUIRED": (
        "Install the Playwright Chrome extension and run taskspindle subscriptions setup-extension."
    ),
    "CONFIG_INVALID": "The subscription browser configuration is invalid.",
    "RUNTIME_UNAVAILABLE": "The subscription collector runtime is unavailable.",
    "COLLECTOR_PROTOCOL_ERROR": "The subscription collector returned an invalid response.",
    "COLLECTOR_FAILED": "The subscription check failed.",
}
BLOCKING_ERROR_CODES = frozenset(
    {"AUTH_REQUIRED", "ACCOUNT_MISMATCH", "UNSUPPORTED_BILLING_CHANNEL"}
)


def validate_provider(provider: str) -> str:
    """Return a supported provider id."""

    if provider not in PROVIDERS:
        raise ValueError("unsupported subscription provider")
    return provider


def validate_action(action: str) -> str:
    """Return a supported queue action."""

    if action not in ACTIONS:
        raise ValueError("unsupported subscription action")
    return action


def _aware_datetime(value: datetime | str, *, field: str = "now") -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def timestamp(value: datetime | str) -> str:
    """Serialize an aware timestamp in stable UTC ISO-8601 form."""

    return _aware_datetime(value).astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: str) -> datetime:
    """Parse one timestamp previously returned by :func:`timestamp`."""

    return _aware_datetime(value, field="timestamp").astimezone(UTC)


class SubscriptionObservation(BaseModel):
    """One directly observed provider billing state."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["chatgpt", "claude", "google_ai", "grok"]
    account_id: str
    account_label: str
    billing_channel: Literal["provider_web", "apple", "google_play", "x_premium", "unknown"]
    plan: str
    status: Literal["renewing", "cancelled", "expired", "free", "none", "unknown"]
    renews_at: str | None = None
    access_ends_at: str | None = None
    date_precision: Literal["date", "datetime"] | None = None
    timezone: str
    source_url: str
    collector_version: str

    @field_validator("account_id")
    @classmethod
    def _stable_account_hash(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("account_id must be a lowercase SHA-256 hash")
        return value

    @field_validator("account_label", "collector_version")
    @classmethod
    def _bounded_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 256 or "\n" in value or "\r" in value:
            raise ValueError("value must be a bounded non-empty single line")
        return value

    @field_validator("collector_version")
    @classmethod
    def _safe_collector_version(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", value) is None:
            raise ValueError("collector_version has an invalid format")
        return value

    @field_validator("account_label")
    @classmethod
    def _masked_account_label(cls, value: str) -> str:
        # The collector's label is one visible character plus three literal
        # asterisks on each side of '@', or a non-identifying fallback.
        masked_email = re.fullmatch(r"[^@\s]\*{3}@[^.\s]\*{3}\.[A-Za-z0-9.-]{2,63}", value)
        if value != "Connected account" and masked_email is None:
            raise ValueError("account_label must be masked")
        return value

    @field_validator("plan")
    @classmethod
    def _bounded_plan(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128 or "\n" in value or "\r" in value:
            raise ValueError("plan must be a bounded non-empty single line")
        return value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("source_url")
    @classmethod
    def _safe_source_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("source_url must be an HTTPS URL")
        if parsed.query or parsed.fragment:
            raise ValueError("source_url must not contain a query or fragment")
        return value

    @model_validator(mode="after")
    def _dates_match_precision(self) -> SubscriptionObservation:
        expected_sources = {
            "chatgpt": ("chatgpt.com", frozenset({"/"})),
            "claude": ("claude.ai", frozenset({"/new", "/settings/billing"})),
            "google_ai": ("one.google.com", frozenset({"/settings"})),
            "grok": ("grok.com", frozenset({"/"})),
        }
        parsed_source = urlsplit(self.source_url)
        expected_host, expected_paths = expected_sources[self.provider]
        if parsed_source.hostname != expected_host or parsed_source.path not in expected_paths:
            raise ValueError("source_url does not match the provider billing source")
        if PLAN_PATTERNS[self.provider].fullmatch(self.plan) is None:
            raise ValueError("plan is not recognized for provider")
        plan_is_free = re.search(r"\bFree(?: plan)?$", self.plan, re.IGNORECASE) is not None
        plan_is_none = self.plan.lower() == "none"
        if (self.status == "free") != plan_is_free or (self.status == "none") != plan_is_none:
            raise ValueError("plan and status do not agree")
        values = (self.renews_at, self.access_ends_at)
        if self.renews_at is not None and self.access_ends_at is not None:
            raise ValueError("an observation must not contain both billing dates")
        if self.status == "renewing" and (
            self.renews_at is None or self.access_ends_at is not None
        ):
            raise ValueError("renewing status requires only renews_at")
        if self.status == "cancelled" and (
            self.access_ends_at is None or self.renews_at is not None
        ):
            raise ValueError("cancelled status requires only access_ends_at")
        if self.status in {"free", "none"} and any(values):
            raise ValueError("free and none statuses must not contain billing dates")
        if self.status == "expired" and self.renews_at is not None:
            raise ValueError("expired status must not contain renews_at")
        if not any(values):
            if self.date_precision is not None:
                raise ValueError("date_precision must be null when billing dates are unavailable")
            return self
        if self.date_precision is None:
            raise ValueError("date_precision is required when a billing date is present")

        for value in values:
            if value is None:
                continue
            if self.date_precision == "date":
                try:
                    parsed_date = date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError("billing dates must be ISO-8601 dates") from exc
                if parsed_date.isoformat() != value:
                    raise ValueError("billing dates must be ISO-8601 dates")
            else:
                _aware_datetime(value, field="billing date")
        return self


def validate_observation(value: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize an observation for persistence."""

    return SubscriptionObservation.model_validate(value).model_dump(mode="json")


def safe_error(value: object) -> dict[str, str]:
    """Reduce an untrusted collector error to the public safe allowlist."""

    code = value.get("code") if isinstance(value, dict) else None
    if not isinstance(code, str) or code not in ERROR_MESSAGES:
        code = "COLLECTOR_FAILED"
    return {"code": code, "message": ERROR_MESSAGES[code]}


def validate_result(value: object) -> dict[str, Any]:
    """Normalize a browser result without retaining an untrusted error message."""

    if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
        return {"ok": False, "error": safe_error(None)}
    if value["ok"] is False:
        return {"ok": False, "error": safe_error(value.get("error"))}
    observation = value.get("observation")
    if not isinstance(observation, dict):
        return {"ok": False, "error": safe_error({"code": "PARSE_CHANGED"})}
    try:
        return {"ok": True, "observation": validate_observation(observation)}
    except (ValidationError, ValueError, TypeError):
        return {"ok": False, "error": safe_error({"code": "PARSE_CHANGED"})}


def _billing_instant(value: str) -> datetime:
    return _aware_datetime(value, field="billing date").astimezone(UTC)


def _remaining_days(value: str, precision: str, timezone: str, now: datetime) -> int:
    if precision == "date":
        return (date.fromisoformat(value) - now.astimezone(ZoneInfo(timezone)).date()).days
    seconds = (_billing_instant(value) - now.astimezone(UTC)).total_seconds()
    if seconds == 0:
        return 0
    return math.ceil(seconds / 86_400) if seconds > 0 else math.floor(seconds / 86_400)


def presentation_values(
    observation: dict[str, Any] | None,
    *,
    last_success_at: str | None,
    now: datetime | str,
) -> dict[str, Any]:
    """Calculate freshness and expiry hints from separately recorded clocks."""

    current = _aware_datetime(now)
    if last_success_at is None:
        freshness = "never_verified"
    else:
        age_seconds = (current.astimezone(UTC) - parse_timestamp(last_success_at)).total_seconds()
        freshness = "stale" if age_seconds >= 24 * 60 * 60 else "fresh"

    days_remaining: int | None = None
    end_passed_unverified = False
    if observation is not None and observation.get("date_precision"):
        precision = observation["date_precision"]
        timezone = observation["timezone"]
        access_end = observation.get("access_ends_at")
        if access_end and last_success_at:
            days_remaining = _remaining_days(access_end, precision, timezone, current)
            if precision == "date":
                zone = ZoneInfo(timezone)
                end_date = date.fromisoformat(access_end)
                end_passed = current.astimezone(zone).date() > end_date
            else:
                end_instant = _billing_instant(access_end)
                end_passed = current.astimezone(UTC) > end_instant
            end_passed_unverified = end_passed and observation.get("status") != "expired"

    return {
        "freshness": freshness,
        "days_remaining": days_remaining,
        "end_passed_unverified": end_passed_unverified,
    }
