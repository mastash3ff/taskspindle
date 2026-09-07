"""Subscription model and store behavior at clock and ownership boundaries."""

from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from taskspindle.subscriptions.models import (
    ERROR_MESSAGES,
    PROVIDERS,
    SubscriptionObservation,
    validate_result,
)
from taskspindle.subscriptions.store import SubscriptionStore


def at(hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 1, 10, hour, minute, tzinfo=UTC)


def observation(
    provider: str = "chatgpt",
    account_id: str = "a" * 64,
    **changes: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "provider": provider,
        "account_id": account_id,
        "account_label": "b***@e***.com",
        "billing_channel": "provider_web",
        "plan": "Plus",
        "status": "renewing",
        "renews_at": "2026-02-10",
        "access_ends_at": None,
        "date_precision": "date",
        "timezone": "America/Chicago",
        "source_url": {
            "chatgpt": "https://chatgpt.com/",
            "claude": "https://claude.ai/settings/billing",
            "google_ai": "https://one.google.com/settings",
            "grok": "https://grok.com/",
        }[provider],
        "collector_version": "test-1",
    }
    value.update(changes)
    return value


def finish_connect(
    store: SubscriptionStore,
    provider: str = "chatgpt",
    when: datetime | None = None,
    **changes: object,
) -> None:
    when = when or at()
    job = store.enqueue(provider, "connect", when)
    claimed = store.claim_next(when, "worker")
    assert claimed is not None and claimed["id"] == job["id"]
    assert store.finish(
        job["id"],
        "worker",
        {"ok": True, "observation": observation(provider, **changes)},
        when,
    )


def test_provider_metadata_and_observation_validation() -> None:
    assert tuple(PROVIDERS) == ("chatgpt", "claude", "google_ai", "grok")
    assert all(set(metadata) == {"label", "billing_url"} for metadata in PROVIDERS.values())

    parsed = SubscriptionObservation.model_validate(observation())
    assert parsed.account_id == "a" * 64
    with pytest.raises(ValidationError, match="SHA-256"):
        SubscriptionObservation.model_validate(observation(account_id="raw-account@example.com"))
    with pytest.raises(ValidationError, match="must be masked"):
        SubscriptionObservation.model_validate(observation(account_label="raw-account@example.com*"))
    with pytest.raises(ValidationError, match="not recognized"):
        SubscriptionObservation.model_validate(observation(plan="Plus token=raw-secret"))
    with pytest.raises(ValidationError, match="query or fragment"):
        SubscriptionObservation.model_validate(
            observation(source_url="https://chatgpt.com/billing?token=secret")
        )
    with pytest.raises(ValidationError, match="date_precision"):
        SubscriptionObservation.model_validate(
            observation(status="unknown", renews_at=None, date_precision="date")
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        SubscriptionObservation.model_validate(
            observation(renews_at="2026-02-10T00:00:00", date_precision="datetime")
        )


def test_collector_failures_are_reduced_to_safe_messages() -> None:
    assert validate_result(
        {"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "secret raw error"}}
    ) == {
        "ok": False,
        "error": {"code": "AUTH_REQUIRED", "message": ERROR_MESSAGES["AUTH_REQUIRED"]},
    }
    assert validate_result(
        {"ok": False, "error": {"code": "SOMETHING_NEW", "message": "token=secret"}}
    )["error"] == {
        "code": "COLLECTOR_FAILED",
        "message": ERROR_MESSAGES["COLLECTOR_FAILED"],
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"renews_at": None},
        {"status": "cancelled", "access_ends_at": None},
        {"access_ends_at": "2026-03-10"},
        {"status": "free"},
        {"status": "none"},
    ],
)
def test_status_and_billing_dates_cannot_contradict(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SubscriptionObservation.model_validate(observation(**changes))


def test_source_url_is_restricted_to_known_provider_path() -> None:
    with pytest.raises(ValidationError, match="billing source"):
        SubscriptionObservation.model_validate(
            observation(source_url="https://chatgpt.com/backend-api/secret")
        )


def test_read_only_missing_database_returns_empty_rows_without_filesystem_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing" / "subscriptions.sqlite3"
    with SubscriptionStore(database, read_only=True) as store:
        rows = store.list_subscriptions(at())
        assert [row["provider"] for row in rows] == list(PROVIDERS)
        assert all(row["connected"] is False for row in rows)
        assert all(row["freshness"] == "never_verified" for row in rows)
        assert store.collector_heartbeat() is None
    assert not database.exists()
    assert not database.parent.exists()


def test_writable_database_is_private_and_read_only_reopen_is_nonmutating(tmp_path: Path) -> None:
    database = tmp_path / "state" / "subscriptions.sqlite3"
    with SubscriptionStore(database) as store:
        finish_connect(store)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    before = database.stat().st_mtime_ns
    with SubscriptionStore(database, read_only=True) as store:
        assert store.list_subscriptions(at())[0]["connected"] is True
        with pytest.raises(PermissionError, match="read-only"):
            store.enqueue("claude", "connect", at())
    assert database.stat().st_mtime_ns == before


def test_snapshot_schema_is_account_keyed_with_separate_provider_selection(tmp_path: Path) -> None:
    with SubscriptionStore(tmp_path / "subscriptions.sqlite3") as store:
        snapshot_columns = store._conn.execute("PRAGMA table_info(subscription_snapshots)").fetchall()
        primary_key = [row["name"] for row in snapshot_columns if row["pk"]]
        history_columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(subscription_observations)")
        }
        selection_columns = {
            row["name"]
            for row in store._conn.execute("PRAGMA table_info(subscription_provider_state)")
        }
        assert primary_key == ["provider", "account_id"]
        assert "account_id" in history_columns
        assert "selected_account_id" in selection_columns


def test_refresh_requires_connection_but_free_and_none_accounts_are_connected(
    tmp_path: Path,
) -> None:
    with SubscriptionStore(tmp_path / "subscriptions.sqlite3") as store:
        with pytest.raises(ValueError, match="not connected"):
            store.enqueue("chatgpt", "refresh", at())

        finish_connect(
            store,
            status="free",
            plan="Free",
            renews_at=None,
            date_precision=None,
        )
        row = store.list_subscriptions(at())[0]
        assert row["connected"] is True
        assert row["status"] == "free"
        assert store.enqueue("chatgpt", "refresh", at(6))["expected_account_id"] == "a" * 64


def test_failure_retains_last_success_and_keeps_clocks_distinct(tmp_path: Path) -> None:
    with SubscriptionStore(tmp_path / "subscriptions.sqlite3") as store:
        finish_connect(store, when=at())
        refresh = store.enqueue("chatgpt", "refresh", at(6))
        assert store.claim_next(at(6), "worker") is not None
        assert store.finish(
            refresh["id"],
            "worker",
            {"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "cookie detail"}},
            at(6, 1),
        )
        store.set_collector_heartbeat(at(7))

        row = store.list_subscriptions(at(7))[0]
        assert row["connected"] is True
        assert row["last_success_at"] == "2026-01-10T00:00:00.000000Z"
        assert row["last_attempt_at"] == "2026-01-10T06:01:00.000000Z"
        assert row["days_remaining"] is None  # renewal is not an access-end countdown
        assert row["error"] == {
            "code": "AUTH_REQUIRED",
            "message": ERROR_MESSAGES["AUTH_REQUIRED"],
        }
        assert store.collector_heartbeat() == "2026-01-10T07:00:00.000000Z"
        assert len(store.observation_history("chatgpt")) == 1


def test_queue_coalesces_and_only_current_unexpired_owner_can_update(tmp_path: Path) -> None:
    database = tmp_path / "subscriptions.sqlite3"
    with SubscriptionStore(database) as store:
        first = store.enqueue("chatgpt", "connect", at())
        assert store.enqueue("chatgpt", "connect", at(1))["id"] == first["id"]
        claimed = store.claim_next(at(), "worker-a", lease_seconds=60)
        assert claimed is not None
        assert store.claim_next(at(), "worker-b") is None
        thirty_seconds = at() + timedelta(seconds=30)
        assert store.heartbeat(first["id"], "worker-b", thirty_seconds) is False
        assert store.heartbeat(first["id"], "worker-a", thirty_seconds, lease_seconds=60)
        assert store.finish(
            first["id"],
            "worker-b",
            {"ok": True, "observation": observation()},
            at() + timedelta(seconds=31),
        ) is False

    # Queue ownership survives process restart. Once the lease expires it is
    # atomically transferred, and the stale owner cannot publish its result.
    with SubscriptionStore(database) as reopened:
        taken = reopened.claim_next(at(1, 31), "worker-b", lease_seconds=60)
        assert taken is not None and taken["id"] == first["id"]
        before_second_lease_expires = at(1, 31) + timedelta(seconds=30)
        assert reopened.finish(
            first["id"],
            "worker-a",
            {"ok": True, "observation": observation()},
            before_second_lease_expires,
        ) is False
        assert reopened.finish(
            first["id"],
            "worker-b",
            {"ok": True, "observation": observation()},
            before_second_lease_expires,
        )


def test_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "subscriptions.sqlite3"
    with SubscriptionStore(database) as store:
        store.enqueue("chatgpt", "connect", at())

    def claim(owner: str) -> dict[str, object] | None:
        with SubscriptionStore(database) as contender:
            return contender.claim_next(at(), owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-a", "worker-b")))
    assert sum(claimed is not None for claimed in claims) == 1


@pytest.mark.parametrize("action", ["connect", "refresh"])
def test_reconnect_and_refresh_reject_account_substitution_and_retain_snapshot(
    tmp_path: Path, action: str
) -> None:
    with SubscriptionStore(tmp_path / "subscriptions.sqlite3") as store:
        finish_connect(store)
        refresh = store.enqueue("chatgpt", action, at(6))
        assert refresh["expected_account_id"] == "a" * 64
        assert store.claim_next(at(6), "worker") is not None
        assert store.finish(
            refresh["id"],
            "worker",
            {"ok": True, "observation": observation(account_id="b" * 64)},
            at(6, 1),
        )

        row = store.list_subscriptions(at(7))[0]
        assert row["account_id"] == "a" * 64
        assert row["last_success_at"] == "2026-01-10T00:00:00.000000Z"
        assert row["error"]["code"] == "ACCOUNT_MISMATCH"
        assert len(store.observation_history("chatgpt")) == 1


def test_due_queue_uses_six_hour_attempt_clock_and_blocking_failures_wait(
    tmp_path: Path,
) -> None:
    with SubscriptionStore(tmp_path / "subscriptions.sqlite3") as store:
        finish_connect(store, "chatgpt", at())
        assert store.enqueue_due(at(5, 59)) == []
        due = store.enqueue_due(at(6))
        assert [job["provider"] for job in due] == ["chatgpt"]
        assert store.enqueue_due(at(7)) == []  # active work coalesces

        claimed = store.claim_next(at(6), "worker")
        assert claimed is not None
        store.finish(
            claimed["id"],
            "worker",
            {"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "raw"}},
            at(6, 1),
        )
        assert store.enqueue_due(at(23)) == []

        # Explicit action remains available to recover a blocked provider.
        explicit = store.enqueue("chatgpt", "refresh", at(23))
        assert explicit["status"] == "queued"


def test_stale_and_date_only_expiry_use_the_provider_timezone(tmp_path: Path) -> None:
    with SubscriptionStore(tmp_path / "subscriptions.sqlite3") as store:
        finish_connect(
            store,
            when=at(16),
            status="cancelled",
            renews_at=None,
            access_ends_at="2026-01-10",
        )

        # 05:59 UTC is still Jan 10 in Chicago, so the date-only access
        # promise has not passed even though UTC is already Jan 11.
        before_local_midnight = datetime(2026, 1, 11, 5, 59, tzinfo=UTC)
        row = store.list_subscriptions(before_local_midnight)[0]
        assert row["days_remaining"] == 0
        assert row["end_passed_unverified"] is False
        assert row["status"] == "cancelled"

        after_local_midnight = datetime(2026, 1, 11, 6, 1, tzinfo=UTC)
        row = store.list_subscriptions(after_local_midnight)[0]
        assert row["days_remaining"] == -1
        assert row["end_passed_unverified"] is True
        assert row["status"] == "cancelled"  # never inferred to expired

        row = store.list_subscriptions(at(16) + timedelta(hours=24))[0]
        assert row["freshness"] == "stale"


def test_past_access_end_stays_unverified_until_expired_is_directly_observed(
    tmp_path: Path,
) -> None:
    with SubscriptionStore(tmp_path / "subscriptions.sqlite3") as store:
        after_end = datetime(2026, 1, 11, 12, tzinfo=UTC)
        finish_connect(
            store,
            when=after_end,
            status="cancelled",
            renews_at=None,
            access_ends_at="2026-01-10",
        )
        assert store.list_subscriptions(after_end)[0]["end_passed_unverified"] is True
        refresh = store.enqueue("chatgpt", "refresh", after_end + timedelta(minutes=1))
        assert store.claim_next(after_end + timedelta(minutes=1), "worker") is not None
        assert store.finish(
            refresh["id"],
            "worker",
            {
                "ok": True,
                "observation": observation(
                    status="expired",
                    renews_at=None,
                    access_ends_at="2026-01-10",
                ),
            },
            after_end + timedelta(minutes=1),
        )
        assert store.list_subscriptions(after_end + timedelta(minutes=1))[0][
            "end_passed_unverified"
        ] is False
