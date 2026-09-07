"""Private SQLite storage for subscription snapshots and collection work."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from .models import (
    BLOCKING_ERROR_CODES,
    PROVIDERS,
    parse_timestamp,
    presentation_values,
    safe_error,
    timestamp,
    validate_action,
    validate_provider,
    validate_result,
)

BUSY_TIMEOUT_MS = 10_000
REFRESH_INTERVAL = timedelta(hours=6)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_provider_state (
    provider TEXT PRIMARY KEY,
    selected_account_id TEXT,
    last_attempt_at TEXT,
    error TEXT,
    FOREIGN KEY(provider, selected_account_id)
        REFERENCES subscription_snapshots(provider, account_id)
);

CREATE TABLE IF NOT EXISTS subscription_snapshots (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    observation TEXT NOT NULL,
    last_success_at TEXT,
    PRIMARY KEY(provider, account_id)
);

CREATE TABLE IF NOT EXISTS subscription_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    observation TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subscription_observations_provider_idx
    ON subscription_observations(provider, account_id, id);

CREATE TRIGGER IF NOT EXISTS subscription_observations_no_update
BEFORE UPDATE ON subscription_observations
BEGIN
    SELECT RAISE(ABORT, 'subscription observations are append-only');
END;

CREATE TRIGGER IF NOT EXISTS subscription_observations_no_delete
BEFORE DELETE ON subscription_observations
BEGIN
    SELECT RAISE(ABORT, 'subscription observations are append-only');
END;

CREATE TABLE IF NOT EXISTS subscription_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    expected_account_id TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    heartbeat_at TEXT,
    lease_expires_at TEXT,
    owner TEXT,
    finished_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS subscription_jobs_order_idx
    ON subscription_jobs(status, id);
CREATE UNIQUE INDEX IF NOT EXISTS subscription_jobs_one_active_provider_idx
    ON subscription_jobs(provider) WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS subscription_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class SubscriptionStore:
    """Transactional subscription state and a coalescing single-flight queue."""

    def __init__(self, path: str | Path, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None

        if read_only and not self.path.exists():
            return
        if read_only:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
            self._conn.execute("PRAGMA query_only = ON")
        else:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)

        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            self._conn.executescript(_SCHEMA)
            self._conn.executemany(
                "INSERT OR IGNORE INTO subscription_provider_state(provider) VALUES (?)",
                ((provider,) for provider in PROVIDERS),
            )
            self._conn.commit()
            os.chmod(self.path, 0o600)

    def __enter__(self) -> SubscriptionStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _write_connection(self) -> sqlite3.Connection:
        if self.read_only:
            raise PermissionError("subscription store is read-only")
        if self._conn is None:
            raise RuntimeError("subscription store is closed")
        return self._conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._write_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()

    def _active_job(self, conn: sqlite3.Connection, provider: str) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT * FROM subscription_jobs
               WHERE provider = ? AND status IN ('queued', 'running')
               ORDER BY id LIMIT 1""",
            (provider,),
        ).fetchone()

    @staticmethod
    def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "provider": row["provider"],
            "action": row["action"],
            "status": row["status"],
            "expected_account_id": row["expected_account_id"],
            "created_at": row["created_at"],
            "claimed_at": row["claimed_at"],
            "heartbeat_at": row["heartbeat_at"],
            "lease_expires_at": row["lease_expires_at"],
            "finished_at": row["finished_at"],
            "error": _loads(row["error"]),
        }

    def list_subscriptions(self, now: object) -> list[dict[str, Any]]:
        """Return four presentation rows in stable provider order."""

        now_stamp = timestamp(now)  # validates the presentation clock once
        snapshots: dict[str, sqlite3.Row] = {}
        operations: dict[str, sqlite3.Row] = {}
        if self._conn is not None:
            snapshots = {
                row["provider"]: row
                for row in self._conn.execute(
                    """SELECT state.provider, state.selected_account_id,
                              state.last_attempt_at, state.error,
                              snapshot.observation, snapshot.last_success_at
                       FROM subscription_provider_state AS state
                       LEFT JOIN subscription_snapshots AS snapshot
                         ON snapshot.provider = state.provider
                        AND snapshot.account_id = state.selected_account_id"""
                )
            }
            operations = {
                row["provider"]: row
                for row in self._conn.execute(
                    """SELECT * FROM subscription_jobs
                       WHERE status IN ('queued', 'running') ORDER BY id"""
                )
            }

        result: list[dict[str, Any]] = []
        for provider, metadata in PROVIDERS.items():
            snapshot = snapshots.get(provider)
            observation = (
                _loads(snapshot["observation"])
                if snapshot is not None and snapshot["observation"] is not None
                else None
            )
            row: dict[str, Any] = {
                "provider": provider,
                **metadata,
                "account_id": None,
                "account_label": None,
                "billing_channel": None,
                "plan": None,
                "status": None,
                "renews_at": None,
                "access_ends_at": None,
                "date_precision": None,
                "timezone": None,
                "source_url": None,
                "collector_version": None,
                "connected": observation is not None,
                "last_attempt_at": snapshot["last_attempt_at"] if snapshot is not None else None,
                "last_success_at": snapshot["last_success_at"] if snapshot is not None else None,
                "error": _loads(snapshot["error"]) if snapshot is not None else None,
                "operation": (
                    self._job_dict(operations[provider]) if provider in operations else None
                ),
            }
            if observation is not None:
                row.update(observation)
            row.update(
                presentation_values(
                    observation,
                    last_success_at=row["last_success_at"],
                    now=now_stamp,
                )
            )
            result.append(row)
        return result

    def enqueue(self, provider: str, action: str, now: object) -> dict[str, Any]:
        """Queue one provider operation, coalescing with existing active work."""

        provider = validate_provider(provider)
        action = validate_action(action)
        now_stamp = timestamp(now)
        with self._write() as conn:
            existing = self._active_job(conn, provider)
            if existing is not None:
                return self._job_dict(existing)
            snapshot = conn.execute(
                """SELECT state.selected_account_id, snapshot.observation
                   FROM subscription_provider_state AS state
                   LEFT JOIN subscription_snapshots AS snapshot
                     ON snapshot.provider = state.provider
                    AND snapshot.account_id = state.selected_account_id
                   WHERE state.provider = ?""",
                (provider,),
            ).fetchone()
            observation = _loads(snapshot["observation"]) if snapshot is not None else None
            if action == "refresh" and observation is None:
                raise ValueError("provider is not connected")
            expected_account_id = observation["account_id"] if observation is not None else None
            cursor = conn.execute(
                """INSERT INTO subscription_jobs(
                       provider, action, status, expected_account_id, created_at
                   ) VALUES (?, ?, 'queued', ?, ?)""",
                (provider, action, expected_account_id, now_stamp),
            )
            row = conn.execute(
                "SELECT * FROM subscription_jobs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            assert row is not None
            return self._job_dict(row)

    def claim_next(
        self, now: object, owner: str, lease_seconds: int = 300
    ) -> dict[str, Any] | None:
        """Atomically claim the oldest queued or expired job."""

        if not owner.strip() or len(owner) > 256:
            raise ValueError("owner must be a bounded non-empty value")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_stamp = timestamp(now)
        expires = timestamp(parse_timestamp(now_stamp) + timedelta(seconds=lease_seconds))
        with self._write() as conn:
            row = conn.execute(
                """SELECT * FROM subscription_jobs
                   WHERE status = 'queued'
                      OR (status = 'running' AND lease_expires_at <= ?)
                   ORDER BY id LIMIT 1""",
                (now_stamp,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE subscription_jobs
                   SET status = 'running', owner = ?, claimed_at = ?, heartbeat_at = ?,
                       lease_expires_at = ?
                   WHERE id = ?""",
                (owner, now_stamp, now_stamp, expires, row["id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM subscription_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            assert claimed is not None
            return self._job_dict(claimed)

    def heartbeat(
        self, job_id: int, owner: str, now: object, lease_seconds: int = 300
    ) -> bool:
        """Extend an unexpired lease only for its current owner."""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now_stamp = timestamp(now)
        expires = timestamp(parse_timestamp(now_stamp) + timedelta(seconds=lease_seconds))
        with self._write() as conn:
            cursor = conn.execute(
                """UPDATE subscription_jobs
                   SET heartbeat_at = ?, lease_expires_at = ?
                   WHERE id = ? AND status = 'running' AND owner = ?
                     AND lease_expires_at > ?""",
                (now_stamp, expires, job_id, owner, now_stamp),
            )
            return cursor.rowcount == 1

    def finish(
        self, job_id: int, owner: str, result: object, now: object
    ) -> bool:
        """Finish an owned job and atomically update its provider snapshot."""

        now_stamp = timestamp(now)
        normalized = validate_result(result)
        with self._write() as conn:
            job = conn.execute(
                """SELECT * FROM subscription_jobs
                   WHERE id = ? AND status = 'running' AND owner = ?
                     AND lease_expires_at > ?""",
                (job_id, owner, now_stamp),
            ).fetchone()
            if job is None:
                return False

            if normalized["ok"]:
                observation = normalized["observation"]
                if observation["provider"] != job["provider"] or (
                    job["expected_account_id"] is not None
                    and observation["account_id"] != job["expected_account_id"]
                ):
                    normalized = {
                        "ok": False,
                        "error": safe_error({"code": "ACCOUNT_MISMATCH"}),
                    }

            if normalized["ok"]:
                observation = normalized["observation"]
                encoded = _dumps(observation)
                conn.execute(
                    """INSERT INTO subscription_snapshots(
                           provider, account_id, observation, last_success_at
                       ) VALUES (?, ?, ?, ?)
                       ON CONFLICT(provider, account_id) DO UPDATE SET
                           observation = excluded.observation,
                           last_success_at = excluded.last_success_at""",
                    (
                        job["provider"],
                        observation["account_id"],
                        encoded,
                        now_stamp,
                    ),
                )
                conn.execute(
                    """UPDATE subscription_provider_state
                       SET selected_account_id = ?, last_attempt_at = ?, error = NULL
                       WHERE provider = ?""",
                    (observation["account_id"], now_stamp, job["provider"]),
                )
                conn.execute(
                    """INSERT INTO subscription_observations(
                           provider, account_id, observed_at, observation
                       ) VALUES (?, ?, ?, ?)""",
                    (job["provider"], observation["account_id"], now_stamp, encoded),
                )
                error = None
                status = "succeeded"
            else:
                error = normalized["error"]
                conn.execute(
                    """UPDATE subscription_provider_state
                       SET last_attempt_at = ?, error = ? WHERE provider = ?""",
                    (now_stamp, _dumps(error), job["provider"]),
                )
                status = "failed"

            conn.execute(
                """UPDATE subscription_jobs
                   SET status = ?, finished_at = ?, error = ?, owner = NULL,
                       lease_expires_at = NULL
                   WHERE id = ?""",
                (status, now_stamp, _dumps(error) if error else None, job_id),
            )
            return True

    def enqueue_due(self, now: object) -> list[dict[str, Any]]:
        """Schedule connected providers whose last attempt is at least six hours old."""

        now_stamp = timestamp(now)
        cutoff = timestamp(parse_timestamp(now_stamp) - REFRESH_INTERVAL)
        enqueued: list[dict[str, Any]] = []
        with self._write() as conn:
            rows = conn.execute(
                """SELECT state.provider, state.last_attempt_at, state.error,
                          snapshot.observation
                   FROM subscription_provider_state AS state
                   JOIN subscription_snapshots AS snapshot
                     ON snapshot.provider = state.provider
                    AND snapshot.account_id = state.selected_account_id"""
            ).fetchall()
            for snapshot in rows:
                if self._active_job(conn, snapshot["provider"]) is not None:
                    continue
                error = _loads(snapshot["error"])
                if error and error.get("code") in BLOCKING_ERROR_CODES:
                    continue
                last_attempt = snapshot["last_attempt_at"]
                if last_attempt is not None and last_attempt > cutoff:
                    continue
                observation = _loads(snapshot["observation"])
                cursor = conn.execute(
                    """INSERT INTO subscription_jobs(
                           provider, action, status, expected_account_id, created_at
                       ) VALUES (?, 'refresh', 'queued', ?, ?)""",
                    (snapshot["provider"], observation["account_id"], now_stamp),
                )
                row = conn.execute(
                    "SELECT * FROM subscription_jobs WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                assert row is not None
                enqueued.append(self._job_dict(row))
        return enqueued

    def set_collector_heartbeat(self, now: object) -> None:
        """Record liveness independently from billing attempts and successes."""

        stamp = timestamp(now)
        with self._write() as conn:
            conn.execute(
                """INSERT INTO subscription_metadata(key, value)
                   VALUES ('collector_heartbeat', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (stamp,),
            )

    def collector_heartbeat(self) -> str | None:
        """Return the last collector heartbeat, if any."""

        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT value FROM subscription_metadata WHERE key = 'collector_heartbeat'"
        ).fetchone()
        return row["value"] if row is not None else None

    def observation_history(self, provider: str) -> list[dict[str, Any]]:
        """Return append-only successful observations, oldest first."""

        provider = validate_provider(provider)
        if self._conn is None:
            return []
        return [
            {"observed_at": row["observed_at"], "observation": _loads(row["observation"])}
            for row in self._conn.execute(
                """SELECT observed_at, observation FROM subscription_observations
                   WHERE provider = ? ORDER BY id""",
                (provider,),
            )
        ]
