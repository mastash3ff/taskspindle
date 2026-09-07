"""Persistent subscription collection service.

Queue state lives in :class:`SubscriptionStore`; this module claims one job at a
time, keeps its ownership alive during collection, and records only normalized
results.  Browser login remains an explicit ``connect`` job.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from taskspindle.config import Paths

from .models import parse_timestamp, safe_error, validate_action, validate_provider, validate_result
from .runtime import _request_browser_stop, _reset_browser_stop, run_browser
from .store import SubscriptionStore

__all__ = ["SubscriptionService"]

POLL_INTERVAL_SECONDS = 2.0
LEASE_SECONDS = 300
LEASE_HEARTBEAT_SECONDS = 15.0
COLLECTOR_ALIVE_SECONDS = 30

Clock = Callable[[], datetime]
Runner = Callable[[str, str, str | None], dict[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SubscriptionService:
    """Queue controller for subscription collection."""

    def __init__(
        self,
        paths: Paths,
        clock: Clock | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.paths = paths
        self.clock = clock or _utc_now
        self.runner = runner or (
            lambda provider, action, expected: run_browser(
                paths, provider, action, expected_account_id=expected
            )
        )
        self.database = paths.state_dir / "subscriptions.sqlite3"
        self.owner = f"subscriptions-{os.getpid()}-{uuid.uuid4().hex}"
        self._watching = False

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("subscription service clock must return a timezone-aware datetime")
        return now

    def status(self) -> dict[str, Any]:
        """Return subscription rows and read-only collector liveness."""
        now = self._now()
        with SubscriptionStore(self.database, read_only=True) as store:
            subscriptions = store.list_subscriptions(now)
            heartbeat = store.collector_heartbeat()
        running = False
        if heartbeat is not None:
            with contextlib.suppress(ValueError):
                age = now.astimezone(UTC) - parse_timestamp(heartbeat)
                running = timedelta(0) <= age < timedelta(seconds=COLLECTOR_ALIVE_SECONDS)
        return {
            "subscriptions": subscriptions,
            "collector_running": running,
            "collector_last_seen_at": heartbeat,
        }

    def request(self, provider: str, action: str) -> dict[str, Any]:
        """Coalesce an explicit connect or refresh request into the persistent queue."""
        provider = validate_provider(provider)
        action = validate_action(action)
        with SubscriptionStore(self.database) as store:
            return store.enqueue(provider, action, self._now())

    def _heartbeat_job(self, job_id: int, stop: threading.Event) -> None:
        """Keep one claimed job owned while its bounded browser runner is active."""
        while not stop.wait(LEASE_HEARTBEAT_SECONDS):
            try:
                with SubscriptionStore(self.database) as store:
                    if not store.heartbeat(job_id, self.owner, self._now(), LEASE_SECONDS):
                        return
                    if self._watching:
                        store.set_collector_heartbeat(self._now())
            except (OSError, ValueError, sqlite3.Error):
                # A transient heartbeat failure does not invent a collection result.
                # The lease still bounds ownership and finish() rechecks it transactionally.
                continue

    def run_once(self) -> dict[str, Any] | None:
        """Claim and perform at most one job, returning its normalized result."""
        now = self._now()
        with SubscriptionStore(self.database) as store:
            if self._watching:
                store.set_collector_heartbeat(now)
            store.enqueue_due(now)
            job = store.claim_next(now, self.owner, LEASE_SECONDS)
        if job is None:
            return None

        stopped = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_job,
            args=(job["id"], stopped),
            name=f"subscription-heartbeat-{job['id']}",
            daemon=True,
        )
        heartbeat.start()
        try:
            try:
                result = self.runner(
                    job["provider"], job["action"], job["expected_account_id"]
                )
                result = validate_result(result)
            except Exception:
                result = {"ok": False, "error": safe_error({"code": "COLLECTOR_FAILED"})}
        finally:
            stopped.set()
            heartbeat.join(timeout=1)

        finished_at = self._now()
        with SubscriptionStore(self.database) as store:
            finished = store.finish(job["id"], self.owner, result, finished_at)
            if self._watching:
                store.set_collector_heartbeat(finished_at)
        if not finished:
            return {"ok": False, "error": safe_error({"code": "COLLECTOR_FAILED"})}
        return result

    def watch(
        self,
        stop_event: threading.Event | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        """Collect until signalled, checking persistent work every ``poll_interval`` seconds."""
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        stop = stop_event or threading.Event()
        previous: dict[int, Any] = {}
        can_install_signals = threading.current_thread() is threading.main_thread()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()
            _request_browser_stop()

        _reset_browser_stop()
        if can_install_signals:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
        try:
            self._watching = True
            while not stop.is_set():
                self.run_once()
                stop.wait(poll_interval)
        finally:
            self._watching = False
            if can_install_signals:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
