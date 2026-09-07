"""The classifier: which refusals mean the provider is throttled or logged out, and which do not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from taskspindle import limits, service
from taskspindle import store as store_module
from taskspindle.acp_client import AcpError
from taskspindle.providers import Profile
from taskspindle.store import Store


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
    assert limits.effective_state({"state": "throttled", "reset_at": past}, now) == "unknown"
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


def test_availability_exposes_stale_account_evidence_without_claiming_success(tmp_path) -> None:
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    profile = Profile(id="claude-fast", auth="oauth", command=("x",), base="claude")
    with Store.open(tmp_path / "state.sqlite3") as store:
        store.set_provider_status(
            "claude",
            "throttled",
            code=limits.PROVIDER_THROTTLED,
            reason="The provider reported a usage limit.",
            reset_at=(now - timedelta(minutes=1)).isoformat(),
            source="acp_error",
        )
        availability = service.provider_availability(store, profile, now=now)

    assert availability == {
        "state": "unknown",
        "status_key": "claude",
        "code": limits.PROVIDER_THROTTLED,
        "window": None,
        "reset_at": (now - timedelta(minutes=1)).isoformat(),
        "reason": "The provider reported a usage limit.",
        "observed_at": availability["observed_at"],
        "suggested_alternative": None,
        "last_success_at": None,
        "source": "acp_error",
        "scope": "account",
        "affected_model": None,
        "stale": True,
        "next_action": "retry",
        "retry_eligible": True,
    }


def test_model_unavailable_only_blocks_the_matching_model(tmp_path) -> None:
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    profile = Profile(id="claude-fast", auth="oauth", command=("x",), base="claude")
    with Store.open(tmp_path / "state.sqlite3") as store:
        store.set_provider_model_status(
            "claude",
            "claude-opus-5",
            "model_unavailable",
            code=limits.PROVIDER_MODEL_UNAVAILABLE,
            reason="The requested model is unavailable.",
            source="acp_error",
        )
        blocked = service.provider_availability(
            store, profile, now=now, model="claude-opus-5",
        )
        other = service.provider_availability(
            store, profile, now=now, model="claude-fable-5",
        )

    assert blocked["state"] == "model_unavailable"
    assert blocked["scope"] == "model"
    assert blocked["affected_model"] == "claude-opus-5"
    assert blocked["next_action"] == "choose_model"
    assert blocked["retry_eligible"] is False
    assert other["state"] == "unknown"
    assert other["scope"] is None


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


def test_schema_four_turn_ok_observation_is_projected_as_last_success(tmp_path, monkeypatch) -> None:
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

    assert projected["state"] == "ok"
    assert projected["last_success_at"] == stamp
