"""The classifier: which refusals mean the provider is throttled or logged out, and which do not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from taskspindle import limits
from taskspindle.acp_client import AcpError
from taskspindle.providers import Profile


def error(message: str, *, rpc_code: int = -32603, data: dict | None = None) -> AcpError:
    return AcpError(
        "ACP_TURN_ERROR",
        f"session/prompt failed: {message}",
        cause={"rpc_code": rpc_code, "rpc_message": message, "rpc_data": data},
    )


AUTH = ("PROVIDER_AUTH_EXPIRED", "auth_expired", None, None)
THROTTLED = ("PROVIDER_THROTTLED", "throttled")

CASES = [
    (error("Authentication required", rpc_code=-32000), "claude", *AUTH),
    (error("Internal error", data={"errorKind": "authentication_failed"}), "claude", *AUTH),
    (error("Internal error", data={"errorKind": "rate_limit"}), "claude", *THROTTLED, "unknown", None),
    (error("Internal error", data={"errorKind": "billing_error"}), "claude", *THROTTLED, "unknown", None),
    (
        error("Claude AI usage limit reached|1893456000"),
        "claude",
        *THROTTLED,
        "unknown",
        "2030-01-01T00:00:00Z",
    ),
    (error("You've hit your weekly limit \u00b7 resets 3pm"), "claude", *THROTTLED, "seven_day", None),
    (error("You've reached your 5-hour limit"), "claude", *THROTTLED, "five_hour", None),
    (error("request failed: 429 Too Many Requests"), "grok", *THROTTLED, "unknown", None),
    (error("401 Unauthorized"), "grok", *AUTH),
]


@pytest.mark.parametrize(("exc", "family", "code", "state", "window", "reset_at"), CASES)
def test_limit_and_auth_refusals_are_classified(exc, family, code, state, window, reset_at) -> None:
    result = limits.classify_acp_error(exc, family=family)

    assert result.code == code
    assert result.provider_state == state
    assert result.window == window
    assert result.reset_at == reset_at
    assert result.retryable is (state == "throttled")
    assert "\n" not in result.reason


def test_an_overloaded_or_unknown_failure_says_nothing_about_the_seat() -> None:
    overloaded = limits.classify_acp_error(
        error("Internal error", data={"errorKind": "overloaded"}), family="claude"
    )
    assert overloaded.code == "ACP_TURN_ERROR"
    assert overloaded.provider_state is None
    assert overloaded.retryable is True

    # A 429 in a Claude message is not a limit: the adapter says so through errorKind instead.
    plain = limits.classify_acp_error(error("something with 429 in it"), family="claude")
    assert plain.provider_state is None

    timeout = limits.classify_acp_error(
        AcpError("TURN_TIMEOUT", "turn exceeded 60s"), family="grok"
    )
    assert timeout.code == "TURN_TIMEOUT"
    assert timeout.retryable is False


def test_effective_state_forgets_a_throttle_once_its_reset_has_passed() -> None:
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    past = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    future = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    assert limits.effective_state(None, now) == "unknown"
    assert limits.effective_state({"state": "ok"}, now) == "ok"
    assert limits.effective_state({"state": "throttled", "reset_at": past}, now) == "ok"
    assert limits.effective_state({"state": "throttled", "reset_at": future}, now) == "throttled"
    assert limits.effective_state({"state": "throttled", "reset_at": None}, now) == "throttled"
    assert limits.effective_state({"state": "auth_expired", "reset_at": past}, now) == "auth_expired"


def test_status_key_is_the_seat_for_oauth_and_the_id_for_a_key() -> None:
    seat = Profile(id="claude-fast", auth="oauth", command=("x",), base="claude")
    key = Profile(id="claude-litellm", auth="api_key", command=("x",), base="claude")
    assert limits.status_key(seat) == "claude"
    assert limits.status_key(key) == "claude-litellm"
    assert limits.suggested_alternative("claude") == "grok"
    assert limits.suggested_alternative("claude-litellm") is None


def test_rate_limit_window_reads_the_sdk_shape() -> None:
    info = {
        "status": "allowed_warning",
        "rateLimitType": "five_hour",
        "utilization": 0.83,
        "resetsAt": 1893456000,
    }
    row = limits.rate_limit_window(info)
    assert row == {
        "window": "five_hour",
        "status": "allowed_warning",
        "used_percent": pytest.approx(83.0),
        "resets_at": "2030-01-01T00:00:00Z",
    }
    assert limits.rate_limit_window({"status": "rejected", "utilization": 100})["used_percent"] == 100.0
    assert limits.rate_limit_window({"rateLimitType": "something_new"})["window"] == "unknown"
    assert limits.epoch_to_iso(1893456000000) == "2030-01-01T00:00:00Z"
    assert limits.epoch_to_iso("nope") is None
