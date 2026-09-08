"""Quota windows are independent restrictions, not a last-observation status flag."""

from datetime import UTC, datetime, timedelta

from taskspindle import quota
from taskspindle.providers import Profile
from taskspindle.service import LEGACY_OVERRIDE_RETIRED, TaskSpindleError, require_provider_available
from taskspindle.store import Store

NOW = datetime(2026, 9, 7, tzinfo=UTC)
CLAUDE = Profile(id="claude", auth="oauth", command=("unused",))


def _window(store, window, *, reset, status="rejected", percent=100.0):
    if status == "rejected":
        scope = "model_family" if window in {"seven_day_opus", "seven_day_sonnet"} else "account"
        family = "opus" if window == "seven_day_opus" else "sonnet" if window == "seven_day_sonnet" else None
        store.record_quota_restriction(
            "claude",
            scope=scope,
            model=family,
            period_key=window,
            reset_at=reset,
            source="test",
            evidence_fingerprint=f"test-{window}",
            observed_at=NOW.isoformat().replace("+00:00", "Z"),
        )
    else:
        store.insert_provider_window(
            "claude",
            window,
            status=status,
            used_percent=percent,
            resets_at=reset,
            source="test",
            observed_at=NOW.isoformat().replace("+00:00", "Z"),
        )


def test_every_unresolved_window_blocks_not_only_the_last_window(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        _window(store, "seven_day", reset=(NOW + timedelta(days=4)).isoformat().replace("+00:00", "Z"))
        _window(store, "five_hour", reset=(NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
        result = quota.evaluate(store, CLAUDE, NOW, model="claude-sonnet-5")
    assert {row["window"] for row in result["quota_restrictions"]} == {"seven_day", "five_hour"}
    assert result["state"] == "blocked"


def test_opus_weekly_restriction_does_not_throttle_sonnet(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        _window(store, "seven_day_opus", reset=(NOW + timedelta(days=4)).isoformat().replace("+00:00", "Z"))
        sonnet = quota.evaluate(store, CLAUDE, NOW, model="claude-sonnet-5")
        opus = quota.evaluate(store, CLAUDE, NOW, model="claude-opus-5")
    assert sonnet["quota_restrictions"] == []
    assert opus["state"] == "blocked"


def test_unknown_model_text_cannot_bypass_a_family_restriction(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        _window(store, "seven_day_opus", reset=(NOW + timedelta(days=4)).isoformat().replace("+00:00", "Z"))
        result = quota.evaluate(store, CLAUDE, NOW, model="not-sonnet")
    assert result["state"] == "blocked"


def test_canonical_claude_ids_and_haiku_scope_are_exact():
    assert quota.model_family("claude-opus-4-6") == "opus"
    assert quota.model_family("claude-sonnet-4-6") == "sonnet"
    assert quota.model_family("claude-3-7-sonnet-20250219") == "sonnet"
    assert quota.window_applies("seven_day_opus", "claude-haiku-4-5") is False
    assert quota.window_applies("seven_day_sonnet", "claude-haiku-4-5") is False


def test_persisted_exact_model_scope_applies_independent_of_window_name(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        store.record_quota_restriction(
            "claude",
            scope="model",
            model="model-red",
            window="custom-period",
            period_key="custom-period",
            reset_at="2026-09-10T00:00:00Z",
            source="test",
            evidence_fingerprint="model-red",
        )
        red = quota.evaluate(store, CLAUDE, NOW, model="model-red")
        blue = quota.evaluate(store, CLAUDE, NOW, model="model-blue")
    assert red["state"] == "blocked"
    assert blue["state"] == "available"


def test_persisted_family_scope_applies_independent_of_window_name(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        store.record_quota_restriction(
            "claude",
            scope="model_family",
            model="opus",
            window="custom-period",
            period_key="custom-period",
            reset_at="2026-09-10T00:00:00Z",
            source="test",
            evidence_fingerprint="opus-family",
        )
        opus = quota.evaluate(store, CLAUDE, NOW, model="claude-opus-4-6")
        sonnet = quota.evaluate(store, CLAUDE, NOW, model="claude-sonnet-4-6")
        unknown = quota.evaluate(store, CLAUDE, NOW, model="custom-model")
    assert opus["state"] == "blocked"
    assert sonnet["state"] == "available"
    assert unknown["state"] == "blocked"


def test_allowed_one_hundred_percent_is_telemetry_not_a_restriction(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        _window(
            store,
            "seven_day",
            reset=(NOW + timedelta(days=4)).isoformat().replace("+00:00", "Z"),
            status="allowed",
            percent=100.0,
        )
        result = quota.evaluate(store, CLAUDE, NOW, model="claude-opus-5")
    assert result["state"] == "available"


def test_passed_reset_is_retry_not_permission_and_unspecified_may_defer(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        _window(
            store,
            "seven_day_opus",
            reset=(NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        )
        retry = quota.evaluate(store, CLAUDE, NOW, model="unrecognised-model")
        _window(store, "seven_day_sonnet", reset=(NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z"))
        deferred = quota.evaluate(store, CLAUDE, NOW, model=None, defer_model=True)
        conservative = quota.evaluate(store, CLAUDE, NOW, model=None)
    assert retry["state"] == "retry"
    assert deferred["quota_restrictions"] == []
    assert conservative["state"] == "blocked"


def test_legacy_ignore_override_is_retired_before_admission(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        try:
            require_provider_available(store, CLAUDE, now=NOW, ignore=True)
        except TaskSpindleError as exc:
            assert exc.code == LEGACY_OVERRIDE_RETIRED
        else:  # pragma: no cover - makes the retirement requirement explicit
            raise AssertionError("legacy override admitted a task")


def test_recent_native_quota_with_passed_reset_is_a_retry(tmp_path, monkeypatch):
    grok = Profile(id="grok", auth="oauth", command=("grok",))
    native = {
        "state": "quota",
        "eligible_hint": None,
        "used_percent": 100,
        "window": "weekly",
        "period_start": "2026-09-01T00:00:00Z",
        "reset_at": "2026-09-06T00:00:00Z",
        "checked_at": "2026-09-07T00:00:00Z",
        "freshness": "fresh",
        "last_success": None,
    }
    monkeypatch.setattr(quota, "cached_native_check", lambda *args, **kwargs: native)
    monkeypatch.setattr(quota, "native_fingerprint", lambda *args, **kwargs: "native")
    with Store.open(tmp_path / "state.db") as store:
        result = quota.evaluate(store, grok, NOW)
    assert result["state"] == "retry"
    assert result["retry_fingerprints"]
