"""Queue-to-runner behavior for the subscription collector service."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskspindle.config import Paths
from taskspindle.subscriptions.service import SubscriptionService
from taskspindle.subscriptions.store import SubscriptionStore

ACCOUNT = "a" * 64


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 7, 18, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


def configure_scheduling(configured: Paths, enabled: bool) -> None:
    configured.config_file.write_text(
        f"[subscriptions]\nscheduled_refresh = {str(enabled).lower()}\n",
        encoding="utf-8",
    )


def observation(provider: str = "chatgpt") -> dict[str, object]:
    return {
        "provider": provider,
        "account_id": ACCOUNT,
        "account_label": "b***@e***.com",
        "billing_channel": "provider_web",
        "plan": "Plus",
        "status": "renewing",
        "renews_at": "2026-10-07",
        "access_ends_at": None,
        "date_precision": "date",
        "timezone": "America/Chicago",
        "source_url": "https://chatgpt.com/",
        "collector_version": "1",
    }


def test_status_of_missing_database_is_read_only(tmp_path: Path) -> None:
    service = SubscriptionService(paths(tmp_path), clock=Clock())

    status = service.status()

    assert len(status["subscriptions"]) == 4
    assert status["collector_running"] is False
    assert status["collector_last_seen_at"] is None
    assert status["scheduled_refresh_enabled"] is False
    assert not (tmp_path / "state").exists()


def test_run_once_connects_and_refresh_passes_expected_account(tmp_path: Path) -> None:
    clock = Clock()
    calls: list[tuple[str, str, str | None]] = []

    def runner(provider: str, action: str, expected: str | None) -> dict[str, object]:
        calls.append((provider, action, expected))
        return {"ok": True, "observation": observation(provider)}

    service = SubscriptionService(paths(tmp_path), clock=clock, runner=runner)
    first = service.request("chatgpt", "connect")
    assert service.request("chatgpt", "connect")["id"] == first["id"]

    assert service.run_once()["ok"] is True
    assert calls == [("chatgpt", "connect", None)]
    assert service.status()["subscriptions"][0]["connected"] is True

    service.request("chatgpt", "refresh")
    assert service.run_once()["ok"] is True
    assert calls[-1] == ("chatgpt", "refresh", ACCOUNT)


def test_failure_preserves_last_valid_snapshot_and_waits_for_reconnect(tmp_path: Path) -> None:
    clock = Clock()
    configured = paths(tmp_path)
    configure_scheduling(configured, True)
    results = iter(
        [
            {"ok": True, "observation": observation()},
            {"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "unsafe raw"}},
        ]
    )
    service = SubscriptionService(configured, clock=clock, runner=lambda *_args: next(results))
    service.request("chatgpt", "connect")
    service.run_once()
    clock.now += timedelta(hours=6)
    service.run_once()

    row = service.status()["subscriptions"][0]
    assert row["account_id"] == ACCOUNT
    assert row["error"] == {
        "code": "AUTH_REQUIRED",
        "message": "Sign in is required to check this subscription.",
    }
    clock.now += timedelta(days=1)
    assert service.run_once() is None


def test_runner_exception_becomes_safe_failure(tmp_path: Path) -> None:
    def broken(*_args: object) -> dict[str, object]:
        raise RuntimeError("secret transport detail")

    service = SubscriptionService(paths(tmp_path), clock=Clock(), runner=broken)
    service.request("claude", "connect")

    assert service.run_once() == {
        "ok": False,
        "error": {"code": "COLLECTOR_FAILED", "message": "The subscription check failed."},
    }
    assert "secret" not in str(service.status())


def test_run_once_does_not_advertise_an_idle_collector(tmp_path: Path) -> None:
    service = SubscriptionService(paths(tmp_path), clock=Clock())
    assert service.run_once() is None
    assert not service.paths.state_dir.exists()
    assert service.status()["collector_running"] is False
    assert service.status()["collector_last_seen_at"] is None


def test_scheduled_refresh_is_opt_in_but_manual_jobs_always_run(tmp_path: Path) -> None:
    clock = Clock()
    calls: list[tuple[str, str, str | None]] = []

    def runner(provider: str, action: str, expected: str | None) -> dict[str, object]:
        calls.append((provider, action, expected))
        return {"ok": True, "observation": observation(provider)}

    service = SubscriptionService(paths(tmp_path), clock=clock, runner=runner)
    service.request("chatgpt", "connect")
    assert service.run_once()["ok"] is True
    clock.now += timedelta(hours=6)
    assert service.run_once() is None
    assert len(calls) == 1

    service.request("chatgpt", "refresh")
    assert service.run_once()["ok"] is True
    assert len(calls) == 2


def test_enabled_schedule_enqueues_due_refresh(tmp_path: Path) -> None:
    clock = Clock()
    configured = paths(tmp_path)
    configure_scheduling(configured, True)
    calls: list[tuple[str, str, str | None]] = []

    def runner(provider: str, action: str, expected: str | None) -> dict[str, object]:
        calls.append((provider, action, expected))
        return {"ok": True, "observation": observation(provider)}

    service = SubscriptionService(configured, clock=clock, runner=runner)
    service.request("chatgpt", "connect")
    service.run_once()
    clock.now += timedelta(hours=6)
    assert service.run_once()["ok"] is True
    assert [action for _provider, action, _expected in calls] == ["connect", "refresh"]
    assert service.status()["scheduled_refresh_enabled"] is True


def test_disabling_schedule_skips_existing_scheduled_job_until_manual_request(
    tmp_path: Path,
) -> None:
    clock = Clock()
    calls: list[str] = []

    def runner(provider: str, action: str, _expected: str | None) -> dict[str, object]:
        calls.append(action)
        return {"ok": True, "observation": observation(provider)}

    service = SubscriptionService(paths(tmp_path), clock=clock, runner=runner)
    service.request("chatgpt", "connect")
    service.run_once()
    clock.now += timedelta(hours=6)
    with SubscriptionStore(service.database) as store:
        scheduled = store.enqueue_due(clock.now)[0]

    assert service.run_once() is None
    assert calls == ["connect"]
    with SubscriptionStore(service.database, read_only=True) as store:
        skipped = store._conn.execute(
            "SELECT status FROM subscription_jobs WHERE id = ?", (scheduled["id"],)
        ).fetchone()
        assert skipped["status"] == "skipped"

    manual = service.request("chatgpt", "refresh")
    assert manual["origin"] == "manual"
    assert service.run_once()["ok"] is True
    assert calls == ["connect", "refresh"]


def test_scheduled_refresh_requires_a_boolean(tmp_path: Path) -> None:
    configured = paths(tmp_path)
    configured.config_file.write_text(
        '[subscriptions]\nscheduled_refresh = "true"\n', encoding="utf-8"
    )
    service = SubscriptionService(configured, clock=Clock(), runner=lambda *_args: pytest.fail())
    with pytest.raises(ValueError, match="invalid subscription configuration"):
        service.status()
    assert not configured.state_dir.exists()


def test_expired_lease_does_not_report_unpersisted_success(tmp_path: Path) -> None:
    clock = Clock()

    def suspended_runner(*_args: object) -> dict[str, object]:
        clock.now += timedelta(seconds=301)
        return {"ok": True, "observation": observation()}

    service = SubscriptionService(paths(tmp_path), clock=clock, runner=suspended_runner)
    service.request("chatgpt", "connect")

    assert service.run_once() == {
        "ok": False,
        "error": {"code": "COLLECTOR_FAILED", "message": "The subscription check failed."},
    }
    row = service.status()["subscriptions"][0]
    assert row["connected"] is False
    assert row["operation"]["status"] == "running"


def test_watch_honors_stop_event(tmp_path: Path) -> None:
    service = SubscriptionService(paths(tmp_path), clock=Clock())
    stop = threading.Event()
    calls = 0

    def once() -> None:
        nonlocal calls
        calls += 1
        stop.set()

    service.run_once = once  # type: ignore[method-assign]
    service.watch(stop, poll_interval=0.01)
    assert calls == 1
