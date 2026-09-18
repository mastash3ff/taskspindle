"""The classifier: which refusals mean the provider is throttled or logged out, and which do not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from taskspindle import limits, service
from taskspindle import store as store_module
from taskspindle.acp_client import AcpError
from taskspindle.providers import Profile
from taskspindle.store import Store


@pytest.mark.parametrize("text,state,code", [
    ("not authenticated", "auth_expired", "PROVIDER_AUTH_EXPIRED"),
    ("quota exceeded", "throttled", "PROVIDER_THROTTLED"),
    ("unknown model", "model_unavailable", "PROVIDER_MODEL_UNAVAILABLE"),
])
def test_native_agy_terminal_error_preserves_provider_scope(text, state, code):
    from taskspindle.agy_cli import _failure
    from taskspindle.limits import classify_acp_error

    error = _failure(text, status="error", exit_code=1)
    classified = classify_acp_error(error, family="agy", model="gemini-pro")
    assert classified.provider_state == state
    assert classified.code == code
    if state == "model_unavailable":
        assert classified.scope == "model"
        assert classified.affected_model == "gemini-pro"

def error(message: str, *, rpc_code: int = -32603, data: dict | None = None) -> AcpError:
    return AcpError(
        "ACP_TURN_ERROR",
        f"session/prompt failed: {message}",
        cause={"rpc_code": rpc_code, "rpc_message": message, "rpc_data": data},
    )


AUTH = ("PROVIDER_AUTH_EXPIRED", "auth_expired", None, None)
THROTTLED = ("PROVIDER_THROTTLED", "throttled")
ACCESS = ("PROVIDER_ACCESS_DENIED", "access_denied", None, None)

CASES = [
    (error("Authentication required", rpc_code=-32000), "claude", *AUTH),
    (error("Internal error", data={"errorKind": "authentication_failed"}), "claude", *AUTH),
    (error("Internal error", data={"errorKind": "rate_limit"}), "claude", *THROTTLED, "unknown", None),
    (error("Internal error", data={"errorKind": "billing_error"}), "claude", *ACCESS),
    (
        error("Claude AI usage limit reached|1893456000"),
        "claude",
        *THROTTLED,
        "unknown",
        "2030-01-01T00:00:00Z",
    ),
    (error("You've hit your weekly limit \u00b7 resets 3pm"), "claude", *THROTTLED, "seven_day", None),
    (error("You've reached your 5-hour limit"), "claude", *THROTTLED, "five_hour", None),
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


@pytest.mark.parametrize(("status", "data", "code", "state", "retryable"), [
    (401, {}, limits.PROVIDER_AUTH_EXPIRED, "auth_expired", False),
    (402, {}, limits.PROVIDER_ACCESS_DENIED, "access_denied", False),
    (429, {}, limits.PROVIDER_THROTTLED, "throttled", True),
])
def test_grok_structured_provider_http_refusals_are_classified(
    status: int, data: dict, code: str, state: str, retryable: bool,
) -> None:
    result = limits.classify_acp_error(
        error("provider text must not be retained", data={"http_status": status, **data}),
        family="grok",
    )

    assert (result.code, result.provider_state, result.retryable) == (code, state, retryable)
    assert result.reason == limits.safe_provider_reason(state)


def test_grok_ambiguous_or_tool_http_statuses_do_not_change_account_access() -> None:
    generic = limits.classify_acp_error(
        error("tool reported HTTP 401 while processing document 429", data={"http_status": 403}),
        family="grok",
    )
    assert generic.code == "ACP_TURN_ERROR"
    assert generic.provider_state is None

    incidental = limits.classify_acp_error(
        error("ticket 401 was mentioned near a 429 item"), family="grok",
    )
    assert incidental.code == "ACP_TURN_ERROR"
    assert incidental.provider_state is None


def test_grok_credit_403_accepts_only_the_structured_terminal_error_envelope() -> None:
    # xAI's terminal Failed notification establishes the source; its error_data_with_status then
    # supplies the corresponding 403. Neither free-text channel is retained in the result.
    result = limits.classify_acp_error(
        AcpError("ACP_TURN_ERROR", "provider turn failed", cause={
            "rpc_data": {"http_status": 403, "message": "Your team has run out of credits; token=private"},
            "last_retry": {
                "origin": "xai_responses", "type": "failed", "error_type": "api",
                "credit_evidence": True,
            },
        }),
        family="grok",
    )
    assert (result.code, result.provider_state, result.reason) == (
        limits.PROVIDER_ACCESS_DENIED, "access_denied", "The provider denied account access.",
    )

    # A structured status supplies the transport provenance; the returned reason remains fixed.
    rpc_message = limits.classify_acp_error(
        AcpError("ACP_TURN_ERROR", "provider turn failed", cause={
            "rpc_message": "Your account has reached its spending limit",
            "rpc_data": {"http_status": 403},
            "last_retry": {"origin": "xai_responses", "type": "failed", "error_type": "api"},
        }),
        family="grok",
    )
    assert rpc_message.provider_state == "access_denied"
    assert "spending limit" not in rpc_message.reason

    # A tool could produce the same words. Without the source-proven terminal notification it is
    # not account evidence, even if the terminal RPC happens to contain a 403-shaped object.
    tool_like = limits.classify_acp_error(
        error(
            "tool said: Your team has run out of credits",
            data={"http_status": 403, "message": "Your team has run out of credits"},
        ),
        family="grok",
    )
    assert tool_like.provider_state is None


def test_grok_explicit_terminal_quota_overrides_a_simultaneous_402_or_403() -> None:
    exhausted = AcpError(
        "ACP_TURN_ERROR", "provider turn failed", cause={
            "last_retry": {
                "origin": "xai_responses", "type": "exhausted", "is_rate_limited": True,
                "reason": "private provider detail",
            },
            "rpc_data": {"http_status": 402, "message": "private access detail"},
        },
    )
    result = limits.classify_acp_error(exhausted, family="grok")
    assert (result.code, result.provider_state) == (limits.PROVIDER_THROTTLED, "throttled")


@pytest.mark.parametrize("malformed", [{"nested": "value"}, ["authentication_failed"]])
def test_malformed_error_kind_is_not_a_classifier_or_membership_crash(malformed: object) -> None:
    result = limits.classify_acp_error(
        error("Internal error", data={"errorKind": malformed}), family="claude",
    )
    assert (result.code, result.provider_state) == ("ACP_TURN_ERROR", None)


def test_grok_terminal_retry_status_is_safe_evidence_but_nonterminal_is_not() -> None:
    timeout = AcpError(
        "TURN_TIMEOUT", "turn exceeded 60s",
        cause={"last_retry": {
            "origin": "xai_responses", "type": "exhausted", "is_rate_limited": True,
            "reason": "private-provider-detail",
        }},
    )
    classified = limits.classify_acp_error(timeout, family="grok")
    assert classified.provider_state == "throttled"
    assert classified.reason == "The provider reported a usage limit."

    unrelated = AcpError(
        "ACP_HANDSHAKE_FAILED", "initialize failed",
        cause={"last_retry": {"kind": "http", "http_status": 401}},
    )
    assert limits.classify_acp_error(unrelated, family="grok").provider_state is None


def test_grok_provider_reset_must_be_explicit_and_absolute() -> None:
    result = limits.classify_acp_error(
        error("ignored", data={"http_status": 429, "resetAt": 1893456000}), family="grok",
    )
    assert result.reset_at == "2030-01-01T00:00:00Z"
    assert limits.classify_acp_error(
        error("ignored", data={"http_status": 429, "retryAfter": 60}), family="grok",
    ).reset_at is None


def test_grok_model_refusal_is_bound_to_the_requested_model_not_provider_metadata() -> None:
    result = limits.classify_acp_error(
        error(
            "untrusted provider text",
            data={"errorKind": "model_unavailable", "model": "other-model"},
        ),
        family="grok",
        model="requested-model",
    )
    assert (result.provider_state, result.scope, result.affected_model) == (
        "model_unavailable", "model", "requested-model",
    )


def test_effective_state_forgets_a_throttle_once_its_reset_has_passed() -> None:
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    past = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    future = (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    assert limits.effective_state(None, now) == "unknown"
    assert limits.effective_state({"state": "ok"}, now) == "ok"
    assert limits.effective_state({"state": "throttled", "reset_at": past}, now) == "unknown"
    assert limits.effective_state({"state": "throttled", "reset_at": future}, now) == "throttled"
    assert limits.effective_state({"state": "throttled", "reset_at": None}, now) == "throttled"
    assert limits.effective_state({"state": "auth_expired", "reset_at": past}, now) == "auth_expired"


def test_status_key_is_the_seat_for_oauth_and_the_id_for_a_key() -> None:
    seat = Profile(id="claude-fast", auth="oauth", command=("x",), base="claude")
    key = Profile(id="claude-litellm", auth="api_key", command=("x",), base="claude")
    assert limits.status_key(seat) == "claude"
    assert limits.status_key(key) == "claude-litellm"


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


def test_explicit_model_failure_is_scoped_to_a_safe_model_identifier() -> None:
    result = limits.classify_acp_error(
        error(
            "provider text is not persisted",
            data={"errorKind": "model_unavailable", "model": "claude-opus-5"},
        ),
        family="claude",
        model="fallback-model",
    )
    assert result.code == limits.PROVIDER_MODEL_UNAVAILABLE
    assert result.provider_state == "model_unavailable"
    assert result.scope == "model"
    assert result.affected_model == "fallback-model"
    assert result.reason == "The requested model is unavailable."
    assert limits.safe_rpc_data({
        "errorKind": "model_unavailable", "model": "claude-opus-5", "secret": "do not keep",
    }) == {"errorKind": "model_unavailable", "model": "claude-opus-5"}

    conflict = limits.classify_acp_error(
        error("wrong fallback", data={"errorKind": "model_unavailable", "model": "model-b"}),
        family="claude",
        model="model-a",
    )
    assert conflict.affected_model == "model-a"

    unknown = limits.classify_acp_error(
        error("unbound", data={"errorKind": "model_unavailable", "model": "model-b"}),
        family="claude",
    )
    assert unknown.affected_model is None


def test_availability_reports_a_passed_reset_as_already_eligible(tmp_path) -> None:
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    profile = Profile(id="claude-fast", auth="oauth", command=("x",), base="claude")
    reset_at = (now - timedelta(minutes=1)).isoformat()
    with Store.open(tmp_path / "state.sqlite3") as store:
        store.set_provider_status(
            "claude",
            "throttled",
            code=limits.PROVIDER_THROTTLED,
            reason="The provider reported a usage limit.",
            reset_at=reset_at,
            source="acp_error",
        )
        availability = service.provider_availability(store, profile, now=now)

    assert availability == {
        "state": "throttled",
        "reset_at": reset_at,
        "eligible_at": reset_at,
        "reason": "The provider reported a usage limit.",
    }
    assert service.provider_eligible(availability, now) is True


def test_a_refusal_without_a_reported_reset_is_eligible_after_fifteen_minutes(tmp_path) -> None:
    observed = datetime(2030, 1, 1, 11, 50, 0, tzinfo=UTC)
    eligible_at = observed + timedelta(minutes=15)
    profile = Profile(id="claude-fast", auth="oauth", command=("x",), base="claude")
    with Store.open(tmp_path / "state.sqlite3") as store:
        store.set_provider_status(
            "claude", "auth_expired", code=limits.PROVIDER_AUTH_EXPIRED, source="acp_error",
            observed_at=observed.isoformat().replace("+00:00", "Z"),
        )
        just_before = service.provider_availability(store, profile, now=eligible_at - timedelta(seconds=1))
        just_after = service.provider_availability(store, profile, now=eligible_at + timedelta(seconds=1))

    assert just_before["reset_at"] is None
    assert just_before["eligible_at"] == "2030-01-01T12:05:00Z"
    assert service.provider_eligible(just_before, eligible_at - timedelta(seconds=1)) is False
    assert service.provider_eligible(just_after, eligible_at + timedelta(seconds=1)) is True


def test_a_model_scoped_refusal_collapses_into_the_provider_wide_status(tmp_path) -> None:
    """There is no separate per-model row any more: the model is named in ``reason``."""
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    profile = Profile(id="claude-fast", auth="oauth", command=("x",), base="claude")
    with Store.open(tmp_path / "state.sqlite3") as store:
        store.set_provider_status(
            "claude",
            "model_unavailable",
            code=limits.PROVIDER_MODEL_UNAVAILABLE,
            reason="The requested model is unavailable.",
            source="acp_error",
            affected_model="claude-opus-5",
        )
        blocked = service.provider_availability(
            store, profile, now=now, model="claude-opus-5",
        )
        other_model = service.provider_availability(
            store, profile, now=now, model="claude-fable-5",
        )

    assert blocked["state"] == other_model["state"] == "model_unavailable"
    assert "claude-opus-5" in blocked["reason"]


def test_account_override_can_follow_default_model_resolution_but_not_new_model_evidence() -> None:
    account = {"provider": "claude", "state": "auth_expired", "observed_at": "2030-01-01T00:00:00Z"}
    payload = {
        "code": "PROVIDER_STATUS_OVERRIDE",
        "status_key": "claude",
        "model": None,
        "account_status_fingerprint": limits.status_fingerprint(account),
        "model_status_fingerprint": None,
    }
    assert limits.status_override_matches(
        payload, status_key_value="claude", model="model-a",
        account_status=account, model_status=None,
    )
    assert limits.status_override_matches(
        payload, status_key_value="claude", model="model-a", account_status=account,
        model_status={
            "provider": "claude", "model": "model-a", "state": "ok",
            "observed_at": "2030-01-01T00:01:00Z",
        },
    )
    assert not limits.status_override_matches(
        payload, status_key_value="claude", model="model-a", account_status=account,
        model_status={
            "provider": "claude", "model": "model-a", "state": "model_unavailable",
            "observed_at": "2030-01-01T00:01:00Z",
        },
    )

    explicit = {**payload, "model": "model-a"}
    assert not limits.status_override_matches(
        explicit, status_key_value="claude", model="model-b",
        account_status=account, model_status=None,
    )


def test_reading_a_pre_last_success_column_database_still_works(tmp_path, monkeypatch) -> None:
    from taskspindle.web.db import ReadOnlyStore

    path = tmp_path / "schema4.sqlite3"
    stamp = "2030-01-01T10:00:00Z"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:4])
        with Store.open(path) as store:
            store.set_provider_status(
                "claude", "ok", source="turn_ok", observed_at=stamp,
            )
    profile = Profile(id="claude", auth="oauth", command=("x",))
    with ReadOnlyStore(path) as store:
        projected = service.provider_availability(
            store, profile, now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
        )

    assert projected == {"state": "ok", "reset_at": None, "eligible_at": None, "reason": None}
