"""A write connection to the dispatch policy, and nothing else in the database.

This deliberately duplicates the save SQL from :meth:`taskspindle.store.Store.save_dispatch_policy`
rather than opening a :class:`~taskspindle.store.Store`, because ``Store`` migrates the schema on
construction. The dashboard never migrates: an old database simply reports :attr:`available` as
``False``. A SQLite authorizer is the second line of defense -- even a bug in this module's own
SQL cannot touch a table other than ``dispatch_policy`` and ``dispatch_policy_history``, and no
statement here can change the schema.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..store import BUSY_TIMEOUT_MS, PolicyRevisionConflict
from ..store import now as _now

__all__ = ["PolicyStore"]

_WRITABLE_TABLES = {"dispatch_policy", "dispatch_policy_history"}
_WRITE_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_STRUCTURAL_ACTIONS = {
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_VTABLE,
    sqlite3.SQLITE_ALTER_TABLE,
}


def _authorize(
    action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None
) -> int:
    del arg2, dbname, source
    if action in _STRUCTURAL_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action in _WRITE_ACTIONS and arg1 not in _WRITABLE_TABLES:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class PolicyStore:
    """A read-write connection restricted to the dispatch policy tables, or to nothing at all.

    Use as a context manager; :attr:`available` is False when the database file does not exist or
    its schema predates the dispatch policy tables (schema 11), in which case every write method
    raises rather than silently doing nothing.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self.available = False
        if not self.path.is_file():
            return
        conn = sqlite3.connect(
            str(self.path),
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        except sqlite3.OperationalError:
            row = None
        version = int(row[0]) if row and row[0] is not None else 0
        if version < 11:
            conn.close()
            return
        conn.set_authorizer(_authorize)
        self._conn = conn
        self.available = True

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> PolicyStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @staticmethod
    def _policy_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        raw = result.get("document")
        result["document"] = json.loads(raw) if isinstance(raw, str) else raw
        return result

    def get(self) -> dict[str, Any] | None:
        """The current policy row with its document decoded, or None when nothing is saved."""
        if self._conn is None:
            return None
        return self._policy_row(self._conn.execute("SELECT * FROM dispatch_policy WHERE id = 1").fetchone())

    def save(
        self,
        document_json: str,
        fingerprint: str,
        *,
        updated_by: str = "web",
        if_revision: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Replace the policy document atomically and record the revision in history.

        ``document_json`` is canonical JSON text produced by :mod:`taskspindle.policy`. When the
        stored revision (0 when no row exists) is not ``if_revision``, nothing is written and
        :class:`~taskspindle.store.PolicyRevisionConflict` is raised.
        """
        if self._conn is None:
            raise sqlite3.OperationalError("policy store is not writable")
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT revision FROM dispatch_policy WHERE id = 1").fetchone()
            current = int(row["revision"]) if row else 0
            if if_revision != current:
                raise PolicyRevisionConflict(if_revision, current)
            revision = current + 1
            stamp = _now()
            conn.execute(
                "INSERT INTO dispatch_policy (id, revision, document, fingerprint, updated_at, updated_by) "
                "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET revision = excluded.revision, "
                "document = excluded.document, fingerprint = excluded.fingerprint, "
                "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (revision, document_json, fingerprint, stamp, updated_by),
            )
            conn.execute(
                "INSERT INTO dispatch_policy_history "
                "(revision, document, fingerprint, updated_at, updated_by, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (revision, document_json, fingerprint, stamp, updated_by, reason),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        saved = self.get()
        assert saved is not None
        return saved
