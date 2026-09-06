"""SQLite persistence for TaskSpindle.

The database is shared between the MCP server process and detached worker processes, so every
write goes through a short ``BEGIN IMMEDIATE`` transaction and no transaction is ever held across
a Python-level wait.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .models import (
    ACTIVE_STATES,
    TASK_BOOL_COLUMNS,
    TASK_COLUMNS,
    TASK_JSON_COLUMNS,
    CheckRecord,
    EventKind,
    ReviewOutput,
    TaskRecord,
)


class ProviderStatusReader(Protocol):
    """The read interface needed to describe provider availability."""

    def get_provider_status(self, provider: str) -> dict[str, Any] | None: ...


class UsageReader(ProviderStatusReader, Protocol):
    """The store reads needed by usage and window reports."""

    def get_task(self, task_id: str) -> TaskRecord | None: ...

    def list_turn_usage(
        self, *, since: str | None = None, provider: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_provider_status(self) -> list[dict[str, Any]]: ...

    def latest_provider_windows(self, provider: str | None = None) -> list[dict[str, Any]]: ...

    def task_counts(self, *, since: str | None = None) -> list[dict[str, Any]]: ...

    def turn_durations_ms(
        self, *, since: str | None = None, provider: str | None = None,
    ) -> list[tuple[str, str, int]]: ...

    def check_durations_ms(self, *, since: str | None = None) -> list[tuple[str, bool, int]]: ...

    def violation_counts(self, *, since: str | None = None) -> list[dict[str, Any]]: ...


BUSY_TIMEOUT_MS = 10_000


def now() -> str:
    """Return the current UTC time as an ISO-8601 string ending in ``Z``."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class StoreError(Exception):
    """Base class for every error the store raises; sqlite errors never escape."""


class NotFoundError(StoreError):
    """A row that must exist does not."""


class ConstraintError(StoreError):
    """A write violated a schema constraint."""


class StaleStateVersionError(StoreError):
    """An optimistic-concurrency check failed."""

    def __init__(self, task_id: str, expected: int, actual: int) -> None:
        super().__init__(f"task {task_id} is at state_version {actual}, not {expected}")
        self.task_id = task_id
        self.expected = expected
        self.actual = actual


_MIGRATION_1 = """
CREATE TABLE repositories (
    id TEXT PRIMARY KEY,
    common_dir TEXT NOT NULL,
    root_commit TEXT NOT NULL,
    display_path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (common_dir, root_commit)
);

CREATE TABLE repository_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    mode TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    UNIQUE (repository_id, provider, mode)
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 1,
    cleanup_state TEXT NOT NULL,
    repository_id TEXT REFERENCES repositories(id),
    provider TEXT NOT NULL,
    auth_mode TEXT NOT NULL,
    mode TEXT NOT NULL,
    prompt TEXT NOT NULL,
    requested_model TEXT,
    requested_effort TEXT,
    timeout_s INTEGER NOT NULL,
    allow_metered INTEGER NOT NULL DEFAULT 0,
    acceptance_criteria TEXT,
    path_prefixes TEXT,
    verification_commands TEXT,
    candidate_message TEXT,
    review_target TEXT,
    base_head TEXT,
    target_head TEXT,
    branch TEXT,
    worktree_path TEXT,
    scratch_repo TEXT,
    session_id TEXT,
    reported_model TEXT,
    oauth_evidence TEXT,
    candidate_sha TEXT,
    candidate_revision INTEGER NOT NULL DEFAULT 0,
    changed_paths TEXT,
    diff_digest TEXT,
    diff_size INTEGER,
    check_summary TEXT,
    warnings TEXT,
    error TEXT,
    unit_name TEXT,
    worker_pid INTEGER,
    boot_id TEXT,
    heartbeat_at TEXT,
    response TEXT,
    transcript_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX tasks_state_idx ON tasks(state);
CREATE INDEX tasks_repository_idx ON tasks(repository_id);

CREATE TABLE turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    prompt TEXT,
    session_id TEXT,
    started_at TEXT,
    ended_at TEXT,
    stop_reason TEXT,
    response TEXT,
    attribution TEXT
);

CREATE INDEX turns_task_idx ON turns(task_id);

CREATE TABLE checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    command TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    stdout_tail TEXT,
    stderr_tail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX checks_task_idx ON checks(task_id, revision);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id),
    kind TEXT NOT NULL,
    at TEXT NOT NULL,
    payload TEXT
);

CREATE INDEX events_task_idx ON events(task_id);

CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    path TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, revision, kind)
);

CREATE TABLE artifact_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    digest TEXT NOT NULL,
    "offset" INTEGER NOT NULL,
    length INTEGER NOT NULL,
    at TEXT NOT NULL
);

CREATE INDEX artifact_receipts_idx ON artifact_receipts(task_id, digest);

CREATE TABLE leases (
    provider TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    unit_name TEXT,
    pid INTEGER,
    boot_id TEXT,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT
);

CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    subject_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    candidate_sha TEXT,
    provider TEXT NOT NULL,
    verdict TEXT NOT NULL,
    summary TEXT,
    findings TEXT,
    checks TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (review_task_id)
);

CREATE INDEX reviews_subject_idx ON reviews(subject_task_id, candidate_sha);

CREATE TABLE integration_journal (
    task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    target_head TEXT,
    candidate_sha TEXT,
    changed_paths TEXT NOT NULL DEFAULT '[]',
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATION_2 = """
CREATE TABLE provider_status (
    provider TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    code TEXT,
    window TEXT,
    reason TEXT,
    reset_at TEXT,
    observed_at TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(id),
    source TEXT NOT NULL
);

CREATE TABLE turn_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL UNIQUE REFERENCES turns(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reasoning_tokens INTEGER,
    model_calls INTEGER,
    duration_ms INTEGER,
    cost_estimate_usd REAL,
    cost_is_estimate INTEGER NOT NULL DEFAULT 1,
    price_table_version TEXT,
    source TEXT NOT NULL,
    raw TEXT,
    captured_at TEXT NOT NULL
);

CREATE INDEX turn_usage_task_idx ON turn_usage(task_id);
CREATE INDEX turn_usage_provider_idx ON turn_usage(provider, captured_at);

CREATE TABLE provider_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    window TEXT NOT NULL,
    status TEXT,
    used_percent REAL,
    resets_at TEXT,
    observed_at TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(id),
    source TEXT NOT NULL
);

CREATE INDEX provider_windows_idx ON provider_windows(provider, window, observed_at);
"""

_MIGRATION_3 = """
ALTER TABLE tasks ADD COLUMN resolved_model TEXT;
ALTER TABLE tasks ADD COLUMN resolved_effort TEXT;
ALTER TABLE tasks ADD COLUMN provider_family TEXT;
CREATE TRIGGER tasks_family_immutable BEFORE UPDATE OF provider_family ON tasks
WHEN NEW.provider_family IS NOT OLD.provider_family
BEGIN
    SELECT RAISE(ABORT, 'task provider family is immutable');
END;
"""

_MIGRATION_4 = """
ALTER TABLE leases RENAME TO leases_single_flight;
CREATE TABLE leases (
    provider TEXT NOT NULL,
    task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    unit_name TEXT,
    pid INTEGER,
    boot_id TEXT,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT
);
INSERT INTO leases SELECT * FROM leases_single_flight;
DROP TABLE leases_single_flight;
CREATE INDEX leases_provider_idx ON leases(provider);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _MIGRATION_1), (2, _MIGRATION_2), (3, _MIGRATION_3), (4, _MIGRATION_4),
]

#: The ``turn_usage`` columns a caller may set; everything else is bookkeeping.
TURN_USAGE_FIELDS: tuple[str, ...] = (
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "model_calls",
    "duration_ms",
    "cost_estimate_usd",
    "cost_is_estimate",
    "price_table_version",
    "source",
    "raw",
)

#: The event kinds that count as violations in a usage report.
VIOLATION_EVENT_KINDS: tuple[str, ...] = (
    "SCOPE_VIOLATION",
    "ROOT_MUTATION",
    "READ_ONLY_VIOLATION",
    "DELEGATION_ATTEMPT",
    "MODE_SWITCH_ATTEMPT",
)

_UPDATABLE_TASK_COLUMNS = frozenset(TASK_COLUMNS) - {"id", "state_version", "updated_at", "provider_family"}


def _split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements, honouring ``CREATE TRIGGER`` bodies."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise StoreError("migration script ends with an incomplete statement")
    return statements


def _encode(column: str, value: Any) -> Any:
    """Convert a Python value into what the column stores."""
    if isinstance(value, Enum):
        value = value.value
    if column in TASK_JSON_COLUMNS:
        return None if value is None else json.dumps(value)
    if column in TASK_BOOL_COLUMNS:
        return None if value is None else int(bool(value))
    return value


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class Store:
    """Typed access to the TaskSpindle SQLite database."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(str(self.path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._depth = 0
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

    @classmethod
    def open(cls, path: Path | str) -> Store:
        """Create the database file (0600) and its parent directory (0700), then migrate."""
        path = Path(path)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_RDWR | os.O_EXCL, 0o600))
        else:
            os.chmod(path, 0o600)
        store = cls(path)
        store.migrate()
        return store

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transactions ---------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run the block inside one ``BEGIN IMMEDIATE`` transaction; nesting joins the outer one."""
        if self._depth:
            self._depth += 1
            try:
                yield self._conn
            finally:
                self._depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self._conn
        except BaseException:
            self._depth = 0
            self._conn.execute("ROLLBACK")
            raise
        self._depth = 0
        self._conn.execute("COMMIT")

    @contextmanager
    def _guard(self) -> Iterator[None]:
        """Translate sqlite errors into :class:`StoreError`."""
        try:
            yield
        except sqlite3.IntegrityError as exc:
            raise ConstraintError(str(exc)) from exc
        except sqlite3.DatabaseError as exc:
            raise StoreError(str(exc)) from exc

    # -- migrations -----------------------------------------------------------------

    def migrate(self) -> list[int]:
        """Apply every unapplied migration; return the versions applied by this call."""
        with self._guard(), self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            done: list[int] = []
            for version, script in MIGRATIONS:
                if version in applied:
                    continue
                for statement in _split_statements(script):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, now()),
                )
                done.append(version)
        return done

    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def rollback_concurrency_schema(self) -> None:
        """Reverse only schema 4 after operators stop MCP servers and drain worker jobs.

        History and grants are untouched. Use ``Store(path)`` rather than ``Store.open`` for
        this maintenance operation, since open intentionally migrates forward.
        """
        with self._guard(), self.transaction() as conn:
            if self.schema_version() != 4:
                raise StoreError("concurrency rollback requires schema 4")
            states = (*(state.value for state in ACTIVE_STATES), "RECOVERY_AMBIGUOUS")
            placeholders = ",".join("?" for _ in states)
            if conn.execute("SELECT 1 FROM leases LIMIT 1").fetchone() or conn.execute(
                f"SELECT 1 FROM tasks WHERE state IN ({placeholders}) LIMIT 1", states
            ).fetchone() or conn.execute("SELECT 1 FROM integration_journal LIMIT 1").fetchone():
                raise StoreError("drain all active/queued jobs, leases and accepts before rollback")
            conn.execute("DROP TABLE leases")
            conn.execute("""CREATE TABLE leases (
                provider TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                unit_name TEXT, pid INTEGER, boot_id TEXT,
                acquired_at TEXT NOT NULL, heartbeat_at TEXT
            )""")
            conn.execute("DELETE FROM schema_migrations WHERE version = 4")

    # -- repositories ---------------------------------------------------------------

    def insert_repository(
        self,
        repository_id: str,
        common_dir: str,
        root_commit: str,
        display_path: str | None = None,
    ) -> str:
        with self._guard(), self.transaction() as conn:
            conn.execute(
                "INSERT INTO repositories(id, common_dir, root_commit, display_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (repository_id, common_dir, root_commit, display_path, now()),
            )
        return repository_id

    def get_repository(self, repository_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM repositories WHERE id = ?", (repository_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_repositories(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM repositories ORDER BY created_at, id").fetchall()
        return [dict(row) for row in rows]

    def find_repository(self, common_dir: str, root_commit: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM repositories WHERE common_dir = ? AND root_commit = ?",
            (common_dir, root_commit),
        ).fetchone()
        return dict(row) if row else None

    # -- grants ---------------------------------------------------------------------

    def upsert_grant(self, repository_id: str, provider: str, mode: str) -> None:
        mode = mode.value if isinstance(mode, Enum) else mode
        with self._guard(), self.transaction() as conn:
            conn.execute(
                "INSERT INTO repository_grants(repository_id, provider, mode, active, created_at) "
                "VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(repository_id, provider, mode) "
                "DO UPDATE SET active = 1, revoked_at = NULL",
                (repository_id, provider, mode, now()),
            )

    def revoke_grant(self, repository_id: str, provider: str, mode: str) -> bool:
        mode = mode.value if isinstance(mode, Enum) else mode
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "UPDATE repository_grants SET active = 0, revoked_at = ? "
                "WHERE repository_id = ? AND provider = ? AND mode = ? AND active = 1",
                (now(), repository_id, provider, mode),
            )
            return cur.rowcount == 1

    def list_grants(self, repository_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM repository_grants WHERE repository_id = ? ORDER BY id",
            (repository_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def grant_active(self, repository_id: str, provider: str, mode: str) -> bool:
        mode = mode.value if isinstance(mode, Enum) else mode
        row = self._conn.execute(
            "SELECT active FROM repository_grants "
            "WHERE repository_id = ? AND provider = ? AND mode = ?",
            (repository_id, provider, mode),
        ).fetchone()
        return bool(row and row[0])

    # -- tasks ----------------------------------------------------------------------

    def insert_task(self, record: TaskRecord) -> TaskRecord:
        data = record.model_dump()
        values = [_encode(column, data[column]) for column in TASK_COLUMNS]
        placeholders = ", ".join("?" for _ in TASK_COLUMNS)
        with self._guard(), self.transaction() as conn:
            conn.execute(
                f"INSERT INTO tasks({', '.join(TASK_COLUMNS)}) VALUES ({placeholders})",
                values,
            )
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return TaskRecord.from_row(row) if row else None

    def list_tasks(
        self,
        repository_id: str | None = None,
        provider: str | None = None,
        mode: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> list[TaskRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("repository_id", repository_id),
            ("provider", provider),
            ("mode", mode),
            ("state", state),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value.value if isinstance(value, Enum) else value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self._conn.execute(
            f"SELECT * FROM tasks{where} ORDER BY created_at DESC, id DESC LIMIT ?", params
        ).fetchall()
        return [TaskRecord.from_row(row) for row in rows]

    def update_task(
        self,
        task_id: str,
        expected_state_version: int | None = None,
        **fields: Any,
    ) -> TaskRecord:
        """Update columns, bump ``state_version`` and set ``updated_at`` in one transaction."""
        unknown = set(fields) - _UPDATABLE_TASK_COLUMNS
        if unknown:
            raise StoreError(f"unknown task columns: {', '.join(sorted(unknown))}")
        with self._guard(), self.transaction() as conn:
            row = conn.execute(
                "SELECT state_version FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"no such task: {task_id}")
            current = int(row[0])
            if expected_state_version is not None and current != expected_state_version:
                raise StaleStateVersionError(task_id, expected_state_version, current)
            assignments = [f"{column} = ?" for column in fields]
            params = [_encode(column, value) for column, value in fields.items()]
            assignments.extend(["state_version = state_version + 1", "updated_at = ?"])
            params.extend([now(), task_id])
            conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", params)
            updated = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return TaskRecord.from_row(updated)

    def heartbeat(self, task_id: str, at: str | None = None) -> None:
        """Record worker liveness on the task without bumping ``state_version``."""
        with self._guard(), self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET heartbeat_at = ? WHERE id = ?", (at or now(), task_id)
            )

    # -- events ---------------------------------------------------------------------

    def append_event(
        self,
        task_id: str | None,
        kind: EventKind | str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        kind_value = kind.value if isinstance(kind, EventKind) else kind
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO events(task_id, kind, at, payload) VALUES (?, ?, ?, ?)",
                (task_id, kind_value, now(), _json_or_none(payload)),
            )
            return int(cur.lastrowid or 0)

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["payload"] = _loads(event["payload"])
            events.append(event)
        return events

    # -- turns ----------------------------------------------------------------------

    def insert_turn(
        self,
        task_id: str,
        revision: int,
        kind: str,
        prompt: str | None = None,
        session_id: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        stop_reason: str | None = None,
        response: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> int:
        kind_value = kind.value if isinstance(kind, Enum) else kind
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO turns(task_id, revision, kind, prompt, session_id, started_at, "
                "ended_at, stop_reason, response, attribution) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    revision,
                    kind_value,
                    prompt,
                    session_id,
                    started_at or now(),
                    ended_at,
                    stop_reason,
                    response,
                    _json_or_none(attribution),
                ),
            )
            return int(cur.lastrowid or 0)

    def complete_turn(
        self,
        turn_id: int,
        ended_at: str | None = None,
        stop_reason: str | None = None,
        response: str | None = None,
        session_id: str | None = None,
        attribution: dict[str, Any] | None = None,
    ) -> None:
        """Close the open turn row a worker was given, recording what the turn produced."""
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "UPDATE turns SET ended_at = ?, stop_reason = ?, response = ?, "
                "session_id = COALESCE(?, session_id), attribution = ? WHERE id = ?",
                (
                    ended_at or now(),
                    stop_reason,
                    response,
                    session_id,
                    _json_or_none(attribution),
                    turn_id,
                ),
            )
            if cur.rowcount != 1:
                raise NotFoundError(f"no such turn: {turn_id}")

    def list_turns(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM turns WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        turns = []
        for row in rows:
            turn = dict(row)
            turn["attribution"] = _loads(turn["attribution"])
            turns.append(turn)
        return turns

    # -- checks ---------------------------------------------------------------------

    def insert_check(self, task_id: str, revision: int, check: CheckRecord) -> int:
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO checks(task_id, revision, command, exit_code, ok, duration_ms, "
                "stdout_tail, stderr_tail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    revision,
                    check.command,
                    check.exit_code,
                    int(check.ok),
                    check.duration_ms,
                    check.stdout_tail,
                    check.stderr_tail,
                    now(),
                ),
            )
            return int(cur.lastrowid or 0)

    def list_checks(self, task_id: str, revision: int | None = None) -> list[CheckRecord]:
        """Return the checks of one revision, or of every revision when ``revision`` is None."""
        if revision is None:
            rows = self._conn.execute(
                "SELECT * FROM checks WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM checks WHERE task_id = ? AND revision = ? ORDER BY id",
                (task_id, revision),
            ).fetchall()
        return [
            CheckRecord(
                command=row["command"],
                exit_code=row["exit_code"],
                ok=bool(row["ok"]),
                duration_ms=row["duration_ms"],
                stdout_tail=row["stdout_tail"] or "",
                stderr_tail=row["stderr_tail"] or "",
            )
            for row in rows
        ]

    # -- artifacts and receipts -----------------------------------------------------

    def insert_artifact(
        self,
        task_id: str,
        revision: int,
        kind: str,
        digest: str,
        size: int,
        path: str | None = None,
    ) -> int:
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO artifacts(task_id, revision, kind, digest, size, path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, revision, kind, digest, size, path, now()),
            )
            return int(cur.lastrowid or 0)

    def get_artifact(self, task_id: str, revision: int, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND revision = ? AND kind = ?",
            (task_id, revision, kind),
        ).fetchone()
        return dict(row) if row else None

    def add_receipt(self, task_id: str, digest: str, offset: int, length: int) -> int:
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                'INSERT INTO artifact_receipts(task_id, digest, "offset", length, at) '
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, digest, offset, length, now()),
            )
            return int(cur.lastrowid or 0)

    def receipt_coverage(self, task_id: str, digest: str) -> list[tuple[int, int]]:
        """Return the merged, sorted half-open ``[start, end)`` byte ranges already delivered."""
        rows = self._conn.execute(
            'SELECT "offset", length FROM artifact_receipts WHERE task_id = ? AND digest = ? '
            'ORDER BY "offset", length',
            (task_id, digest),
        ).fetchall()
        merged: list[list[int]] = []
        for offset, length in rows:
            if length <= 0:
                continue
            start, end = int(offset), int(offset) + int(length)
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    # -- leases ---------------------------------------------------------------------

    def acquire_lease(
        self, provider: str, task_id: str, unit_name: str, pid: int | None, boot_id: str,
        *, limit: int = 1,
    ) -> bool:
        """Atomically claim one provider slot, with at most one lease per task.

        The capacity read and insert share a SQLite write transaction across all processes.
        Lowering a limit does not evict existing workers; it prevents further acquisitions.
        """
        if type(limit) is not int or limit < 1:
            raise StoreError("lease limit must be a positive integer")
        stamp = now()
        with self._guard(), self.transaction() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM leases WHERE provider = ?", (provider,)
            ).fetchone()[0]
            if count >= limit or conn.execute(
                "SELECT 1 FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone():
                return False
            cur = conn.execute(
                "INSERT OR IGNORE INTO leases"
                "(provider, task_id, unit_name, pid, boot_id, acquired_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (provider, task_id, unit_name, pid, boot_id, stamp, stamp),
            )
            return cur.rowcount == 1

    def release_lease(self, provider: str, task_id: str) -> bool:
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM leases WHERE provider = ? AND task_id = ?", (provider, task_id)
            )
            return cur.rowcount == 1

    def get_lease(self, provider: str, task_id: str | None = None) -> dict[str, Any] | None:
        """Read the exact task lease, or the oldest lease for legacy single-flight callers."""
        if task_id is not None:
            row = self._conn.execute(
                "SELECT * FROM leases WHERE provider = ? AND task_id = ?", (provider, task_id)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM leases WHERE provider = ? ORDER BY acquired_at, task_id LIMIT 1",
                (provider,),
            ).fetchone()
        return dict(row) if row else None

    def list_leases(self, provider: str | None = None) -> list[dict[str, Any]]:
        where, args = (" WHERE provider = ?", (provider,)) if provider else ("", ())
        return [dict(row) for row in self._conn.execute(
            "SELECT * FROM leases" + where + " ORDER BY acquired_at, task_id", args
        )]

    def bind_lease(
        self,
        provider: str,
        task_id: str,
        *,
        unit_name: str | None = None,
        pid: int | None = None,
        boot_id: str | None = None,
    ) -> bool:
        """Record the running worker's identity on a lease the task already holds.

        The server acquires the lease before it starts the unit, so it cannot know the worker's
        pid or boot; the worker fills those in once it is running.
        """
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "UPDATE leases SET unit_name = COALESCE(?, unit_name), pid = COALESCE(?, pid), "
                "boot_id = COALESCE(?, boot_id), heartbeat_at = ? "
                "WHERE provider = ? AND task_id = ?",
                (unit_name, pid, boot_id, now(), provider, task_id),
            )
            return cur.rowcount == 1

    def touch_lease(self, task_id: str, at: str | None = None) -> bool:
        """Refresh only this task's lease heartbeat."""
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "UPDATE leases SET heartbeat_at = ? WHERE task_id = ?", (at or now(), task_id)
            )
            return cur.rowcount >= 1

    # -- reviews --------------------------------------------------------------------

    def insert_review(
        self,
        review_task_id: str,
        subject_task_id: str,
        candidate_sha: str | None,
        provider: str,
        output: ReviewOutput,
    ) -> int:
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO reviews(review_task_id, subject_task_id, candidate_sha, provider, "
                "verdict, summary, findings, checks, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_task_id,
                    subject_task_id,
                    candidate_sha,
                    provider,
                    output.verdict.value,
                    output.summary,
                    json.dumps([finding.model_dump(mode="json") for finding in output.findings]),
                    json.dumps(output.checks),
                    now(),
                ),
            )
            return int(cur.lastrowid or 0)

    def _review_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        review = dict(row)
        review["findings"] = _loads(review["findings"]) or []
        review["checks"] = _loads(review["checks"]) or []
        return review

    def get_review_for(self, review_task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM reviews WHERE review_task_id = ?", (review_task_id,)
        ).fetchone()
        return self._review_row(row)

    def latest_review_for_subject(
        self, subject_task_id: str, candidate_sha: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM reviews WHERE subject_task_id = ? AND candidate_sha = ? "
            "ORDER BY id DESC LIMIT 1",
            (subject_task_id, candidate_sha),
        ).fetchone()
        return self._review_row(row)

    # -- integration journal --------------------------------------------------------

    def write_journal(
        self,
        task_id: str,
        phase: str,
        target_head: str | None = None,
        candidate_sha: str | None = None,
        changed_paths: Sequence[str] = (),
    ) -> None:
        stamp = now()
        paths = json.dumps(list(changed_paths))
        with self._guard(), self.transaction() as conn:
            conn.execute(
                "INSERT INTO integration_journal"
                "(task_id, phase, target_head, candidate_sha, changed_paths, "
                "started_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET phase = excluded.phase, "
                "target_head = excluded.target_head, candidate_sha = excluded.candidate_sha, "
                "changed_paths = excluded.changed_paths, updated_at = excluded.updated_at",
                (task_id, phase, target_head, candidate_sha, paths, stamp, stamp),
            )

    def read_journal(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM integration_journal WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        journal = dict(row)
        journal["changed_paths"] = json.loads(journal["changed_paths"] or "[]")
        return journal

    def clear_journal(self, task_id: str) -> None:
        with self._guard(), self.transaction() as conn:
            conn.execute("DELETE FROM integration_journal WHERE task_id = ?", (task_id,))

    # -- provider status --------------------------------------------------------------

    def set_provider_status(
        self,
        provider: str,
        state: str,
        *,
        source: str,
        code: str | None = None,
        window: str | None = None,
        reason: str | None = None,
        reset_at: str | None = None,
        task_id: str | None = None,
        observed_at: str | None = None,
    ) -> None:
        """Record what TaskSpindle last learned about a provider's willingness to take a turn."""
        with self._guard(), self.transaction() as conn:
            conn.execute(
                "INSERT INTO provider_status(provider, state, code, window, reason, reset_at, "
                "observed_at, task_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET state = excluded.state, "
                "code = excluded.code, window = excluded.window, reason = excluded.reason, "
                "reset_at = excluded.reset_at, observed_at = excluded.observed_at, "
                "task_id = excluded.task_id, source = excluded.source",
                (
                    provider,
                    state,
                    code,
                    window,
                    reason,
                    reset_at,
                    observed_at or now(),
                    task_id,
                    source,
                ),
            )

    def get_provider_status(self, provider: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM provider_status WHERE provider = ?", (provider,)
        ).fetchone()
        return dict(row) if row else None

    def mark_provider_healthy(
        self,
        provider: str,
        *,
        expected: Mapping[str, Any] | None,
        task_id: str | None = None,
    ) -> bool:
        """Clear only the status observed before this successful provider turn began.

        Comparing and writing inside one immediate transaction protects against other worker
        processes. A sibling failure after that observation must survive this completion; the
        full observation also avoids treating matching timestamps as matching status records.
        """
        with self._guard(), self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM provider_status WHERE provider = ?", (provider,)
            ).fetchone()
            current = dict(row) if row else None
            if current != expected or (current is not None and current["state"] == "ok"):
                return False
            conn.execute(
                "INSERT INTO provider_status(provider, state, observed_at, task_id, source) "
                "VALUES (?, 'ok', ?, ?, 'turn_ok') ON CONFLICT(provider) DO UPDATE SET "
                "state = 'ok', code = NULL, window = NULL, reason = NULL, reset_at = NULL, "
                "observed_at = excluded.observed_at, task_id = excluded.task_id, source = 'turn_ok'",
                (provider, now(), task_id),
            )
            return True

    def list_provider_status(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM provider_status ORDER BY provider").fetchall()
        return [dict(row) for row in rows]

    # -- turn usage -------------------------------------------------------------------

    def insert_turn_usage(self, turn_id: int, task_id: str, provider: str, **fields: Any) -> int:
        """Record what one turn cost; a second record for the same turn replaces the first."""
        unknown = set(fields) - set(TURN_USAGE_FIELDS)
        if unknown:
            raise StoreError(f"unknown turn_usage columns: {', '.join(sorted(unknown))}")
        columns = ["turn_id", "task_id", "provider", *fields, "captured_at"]
        values: list[Any] = [turn_id, task_id, provider]
        for column, value in fields.items():
            if column == "raw":
                values.append(_json_or_none(value))
            elif column == "cost_is_estimate":
                values.append(int(bool(value)))
            else:
                values.append(value)
        values.append(now())
        updates = ", ".join(f"{column} = excluded.{column}" for column in columns[1:])
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                f"INSERT INTO turn_usage({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(turn_id) DO UPDATE SET {updates}",
                values,
            )
            row = conn.execute(
                "SELECT id FROM turn_usage WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            return int(row[0]) if row else int(cur.lastrowid or 0)

    @staticmethod
    def _usage_row(row: sqlite3.Row) -> dict[str, Any]:
        usage = dict(row)
        usage["raw"] = _loads(usage.get("raw"))
        usage["cost_is_estimate"] = bool(usage.get("cost_is_estimate"))
        return usage

    def get_turn_usage(self, turn_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM turn_usage WHERE turn_id = ?", (turn_id,)
        ).fetchone()
        return self._usage_row(row) if row else None

    def list_turn_usage(
        self,
        *,
        since: str | None = None,
        provider: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("u.captured_at >= ?")
            params.append(since)
        if provider is not None:
            clauses.append("u.provider = ?")
            params.append(provider)
        if task_id is not None:
            clauses.append("u.task_id = ?")
            params.append(task_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            "SELECT u.*, t.repository_id FROM turn_usage u JOIN tasks t ON t.id = u.task_id "
            f"{where} ORDER BY u.id", params
        ).fetchall()
        return [self._usage_row(row) for row in rows]

    # -- provider windows -------------------------------------------------------------

    def insert_provider_window(
        self,
        provider: str,
        window: str,
        *,
        source: str,
        status: str | None = None,
        used_percent: float | None = None,
        resets_at: str | None = None,
        task_id: str | None = None,
        observed_at: str | None = None,
    ) -> int:
        """Record one observation of a provider's usage window."""
        with self._guard(), self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO provider_windows(provider, window, status, used_percent, resets_at, "
                "observed_at, task_id, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    window,
                    status,
                    used_percent,
                    resets_at,
                    observed_at or now(),
                    task_id,
                    source,
                ),
            )
            return int(cur.lastrowid or 0)

    def latest_provider_windows(self, provider: str | None = None) -> list[dict[str, Any]]:
        """The newest observation of every ``(provider, window)`` pair."""
        params: list[Any] = []
        where = ""
        if provider is not None:
            where = " WHERE provider = ?"
            params.append(provider)
        rows = self._conn.execute(
            "SELECT * FROM provider_windows WHERE id IN ("
            f"SELECT MAX(id) FROM provider_windows{where} GROUP BY provider, window"
            ") ORDER BY provider, window",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    # -- rollup reads -----------------------------------------------------------------

    def task_counts(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """How many tasks ended up in each state, per provider and mode."""
        where = " WHERE created_at >= ?" if since is not None else ""
        params = [since] if since is not None else []
        rows = self._conn.execute(
            "SELECT provider, mode, state, COUNT(*) AS count FROM tasks"
            f"{where} GROUP BY provider, mode, state ORDER BY provider, mode, state",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def turn_durations_ms(
        self, *, since: str | None = None, provider: str | None = None
    ) -> list[tuple[str, str, int]]:
        """``(provider, mode, milliseconds)`` for every finished turn."""
        clauses = ["t.started_at IS NOT NULL", "t.ended_at IS NOT NULL"]
        params: list[Any] = []
        if since is not None:
            clauses.append("t.started_at >= ?")
            params.append(since)
        if provider is not None:
            clauses.append("k.provider = ?")
            params.append(provider)
        rows = self._conn.execute(
            "SELECT k.provider, k.mode, "
            "CAST(ROUND((julianday(t.ended_at) - julianday(t.started_at)) * 86400000) AS INTEGER) "
            "AS ms FROM turns t JOIN tasks k ON k.id = t.task_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY t.id",
            params,
        ).fetchall()
        return [(str(row["provider"]), str(row["mode"]), int(row["ms"])) for row in rows]

    def check_durations_ms(self, *, since: str | None = None) -> list[tuple[str, bool, int]]:
        """``(provider, ok, milliseconds)`` for every verification command that ran."""
        where = " WHERE c.created_at >= ?" if since is not None else ""
        params = [since] if since is not None else []
        rows = self._conn.execute(
            "SELECT k.provider, c.ok, c.duration_ms FROM checks c JOIN tasks k ON k.id = c.task_id"
            f"{where} ORDER BY c.id",
            params,
        ).fetchall()
        return [(str(row["provider"]), bool(row["ok"]), int(row["duration_ms"])) for row in rows]

    def violation_counts(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """How often each violation kind was recorded, per provider."""
        placeholders = ", ".join("?" for _ in VIOLATION_EVENT_KINDS)
        clauses = [f"e.kind IN ({placeholders})"]
        params: list[Any] = list(VIOLATION_EVENT_KINDS)
        if since is not None:
            clauses.append("e.at >= ?")
            params.append(since)
        rows = self._conn.execute(
            "SELECT k.provider, e.kind, COUNT(*) AS count FROM events e "
            "JOIN tasks k ON k.id = e.task_id "
            f"WHERE {' AND '.join(clauses)} GROUP BY k.provider, e.kind ORDER BY k.provider, e.kind",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


__all__: Sequence[str] = (
    "MIGRATIONS",
    "TURN_USAGE_FIELDS",
    "VIOLATION_EVENT_KINDS",
    "ConstraintError",
    "NotFoundError",
    "ProviderStatusReader",
    "StaleStateVersionError",
    "Store",
    "StoreError",
    "UsageReader",
    "now",
)
