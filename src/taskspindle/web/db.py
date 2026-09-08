"""A read-only view of the TaskSpindle database for the web dashboard.

This mirrors the read methods of :class:`taskspindle.store.Store` -- same names, same SQL, same
shapes -- but never touches the schema and never opens a write transaction: the connection is
``mode=ro`` and ``PRAGMA query_only=1``, so a write attempt raises ``sqlite3.OperationalError``
instead of mutating the database the MCP server and its workers own.

The database file may not exist yet (nothing has run). In that case every list method returns an
empty list and every lookup returns ``None``, so the dashboard renders an empty state rather than
failing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..models import ACTIVE_STATES, CheckRecord, CleanupState, TaskRecord, TaskState

__all__ = ["ReadOnlyStore"]

#: The event kinds that count as violations in a usage report (mirrors ``store.VIOLATION_EVENT_KINDS``).
_VIOLATION_EVENT_KINDS: tuple[str, ...] = (
    "SCOPE_VIOLATION",
    "ROOT_MUTATION",
    "READ_ONLY_VIOLATION",
    "DELEGATION_ATTEMPT",
)


def _loads(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class ReadOnlyStore:
    """A read-only connection to the TaskSpindle database, or to nothing at all.

    Use as a context manager; one connection per request keeps this safe across the threadpool
    Starlette runs sync handlers in.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.exists = self.path.is_file()
        self._conn: sqlite3.Connection | None = None
        if self.exists:
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.create_function("casefold", 1, lambda value: (value or "").casefold(),
                                       deterministic=True)
            self._conn.execute("PRAGMA query_only=1")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> ReadOnlyStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- schema -----------------------------------------------------------------------

    def schema_version(self) -> int | None:
        if self._conn is None:
            return None
        row = self._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # -- repositories -------------------------------------------------------------------

    def list_repositories(self) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        rows = self._conn.execute("SELECT * FROM repositories ORDER BY created_at, id").fetchall()
        return [dict(row) for row in rows]

    def list_grants(self, repository_id: str) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM repository_grants WHERE repository_id = ? ORDER BY id",
            (repository_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- tasks ----------------------------------------------------------------------

    def get_task(self, task_id: str) -> TaskRecord | None:
        if self._conn is None:
            return None
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return TaskRecord.from_row(row) if row else None

    def list_tasks(
        self,
        repository_id: str | None = None,
        provider: str | None = None,
        mode: str | None = None,
        state: str | None = None,
        limit: int = 50,
        q: str | None = None,
    ) -> list[TaskRecord]:
        if self._conn is None:
            return []
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
                params.append(value)
        if q and q.strip():
            clauses.append(
                "(instr(casefold(id), casefold(?)) > 0 OR instr(casefold(prompt), casefold(?)) > 0)"
            )
            params.extend([q.strip(), q.strip()])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self._conn.execute(
            f"SELECT * FROM tasks{where} ORDER BY created_at DESC, id DESC LIMIT ?", params
        ).fetchall()
        return [TaskRecord.from_row(row) for row in rows]

    def task_overview(self, *, limit: int) -> dict[str, Any]:
        """Return global lifecycle counts and bounded dashboard task lists.

        The aggregate queries deliberately do not share the list limit.  Repository data is
        limited to the public display identity the overview needs; git directories and root
        commits never enter the projection.
        """
        empty = {
            "counts": {"total": 0, "active": 0, "attention": 0, "awaiting_review": 0},
            "active_tasks": [],
            "attention_tasks": [],
        }
        if self._conn is None:
            return empty

        active_states = tuple(state.value for state in ACTIVE_STATES)
        attention_states = (
            TaskState.RESULT_READY.value,
            TaskState.INTERRUPTED.value,
            TaskState.RECOVERY_AMBIGUOUS.value,
        )
        active_marks = ", ".join("?" for _ in active_states)
        attention_marks = ", ".join("?" for _ in attention_states)
        attention_where = (
            f"(t.state IN ({attention_marks}) OR "
            "(t.state = ? AND t.cleanup_state IS NOT ?))"
        )

        total = int(self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        active = int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE state IN ({active_marks})", active_states
            ).fetchone()[0]
        )
        attention_params = (*attention_states, TaskState.FAILED.value, CleanupState.COMPLETE.value)
        attention = int(
            self._conn.execute(
                f"SELECT COUNT(*) FROM tasks t WHERE {attention_where}", attention_params
            ).fetchone()[0]
        )
        awaiting_review = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE state = ?", (TaskState.RESULT_READY.value,)
            ).fetchone()[0]
        )

        def task_rows(where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
            rows = self._conn.execute(
                "SELECT t.*, r.display_path AS repository_display_path "
                "FROM tasks t LEFT JOIN repositories r ON r.id = t.repository_id "
                f"WHERE {where} ORDER BY t.updated_at DESC, t.id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            result = []
            for row in rows:
                task_data = dict(row)
                repository_display_path = task_data.pop("repository_display_path")
                repository = None
                if task_data.get("repository_id") is not None:
                    repository = {
                        "id": task_data["repository_id"],
                        "path": repository_display_path,
                    }
                result.append({"task": TaskRecord.from_row(task_data), "repository": repository})
            return result

        return {
            "counts": {
                "total": total,
                "active": active,
                "attention": attention,
                "awaiting_review": awaiting_review,
            },
            "active_tasks": task_rows(f"t.state IN ({active_marks})", active_states),
            "attention_tasks": task_rows(attention_where, attention_params),
        }

    # -- events ---------------------------------------------------------------------

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
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

    def list_turns(self, task_id: str) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
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

    def list_checks(self, task_id: str, revision: int | None = None) -> list[CheckRecord]:
        if self._conn is None:
            return []
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

    # -- artifacts --------------------------------------------------------------------

    def get_artifact(self, task_id: str, revision: int, kind: str) -> dict[str, Any] | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? AND revision = ? AND kind = ?",
            (task_id, revision, kind),
        ).fetchone()
        return dict(row) if row else None

    # -- reviews --------------------------------------------------------------------

    @staticmethod
    def _review_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        review = dict(row)
        review["findings"] = _loads(review["findings"]) or []
        review["checks"] = _loads(review["checks"]) or []
        return review

    def get_review_for(self, review_task_id: str) -> dict[str, Any] | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM reviews WHERE review_task_id = ?", (review_task_id,)
        ).fetchone()
        return self._review_row(row)

    def latest_review_for_subject(
        self, subject_task_id: str, candidate_sha: str
    ) -> dict[str, Any] | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM reviews WHERE subject_task_id = ? AND candidate_sha = ? "
            "ORDER BY id DESC LIMIT 1",
            (subject_task_id, candidate_sha),
        ).fetchone()
        return self._review_row(row)

    # -- provider status --------------------------------------------------------------

    def get_provider_status(self, provider: str) -> dict[str, Any] | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM provider_status WHERE provider = ?", (provider,)
        ).fetchone()
        return dict(row) if row else None

    def get_provider_model_status(self, provider: str, model: str) -> dict[str, Any] | None:
        """Read v5 model evidence; a schema-4 database has none."""
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM provider_model_status WHERE provider = ? AND model = ?",
                (provider, model),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        return dict(row) if row else None

    def list_provider_model_status(self, provider: str) -> list[dict[str, Any]]:
        """List v5 model evidence; a schema-4 database has none."""
        if self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                "SELECT * FROM provider_model_status WHERE provider = ? ORDER BY model", (provider,)
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        return [dict(row) for row in rows]

    def list_provider_status(self) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        rows = self._conn.execute("SELECT * FROM provider_status ORDER BY provider").fetchall()
        return [dict(row) for row in rows]

    def get_native_check(self, provider: str) -> dict[str, Any] | None:
        """Read a schema-6 native-check cache row; older databases have no cache."""
        if self._conn is None:
            return None
        try:
            row = self._conn.execute(
                "SELECT * FROM native_checks WHERE provider = ?", (provider,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None
            raise
        if row is None:
            return None
        result = dict(row)
        for column in ("result_json", "success_json"):
            try:
                result[column] = _loads(result.get(column))
            except (TypeError, ValueError):
                result[column] = None
        return result

    # -- turn usage -------------------------------------------------------------------

    @staticmethod
    def _usage_row(row: sqlite3.Row) -> dict[str, Any]:
        usage = dict(row)
        usage["raw"] = _loads(usage.get("raw"))
        usage["cost_is_estimate"] = bool(usage.get("cost_is_estimate"))
        return usage

    def list_turn_usage(
        self,
        *,
        since: str | None = None,
        provider: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
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

    def latest_provider_windows(self, provider: str | None = None) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
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
        if self._conn is None:
            return []
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
        if self._conn is None:
            return []
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
        if self._conn is None:
            return []
        where = " WHERE c.created_at >= ?" if since is not None else ""
        params = [since] if since is not None else []
        rows = self._conn.execute(
            "SELECT k.provider, c.ok, c.duration_ms FROM checks c JOIN tasks k ON k.id = c.task_id"
            f"{where} ORDER BY c.id",
            params,
        ).fetchall()
        return [(str(row["provider"]), bool(row["ok"]), int(row["duration_ms"])) for row in rows]

    def violation_counts(self, *, since: str | None = None) -> list[dict[str, Any]]:
        if self._conn is None:
            return []
        placeholders = ", ".join("?" for _ in _VIOLATION_EVENT_KINDS)
        clauses = [f"e.kind IN ({placeholders})"]
        params: list[Any] = list(_VIOLATION_EVENT_KINDS)
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
