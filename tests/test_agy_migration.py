"""The local schema-2 upgrade preserves existing operational and review records."""

from taskspindle import store as store_module
from taskspindle.models import ReviewOutput, Verdict
from taskspindle.store import Store
from tests.test_store import make_task


def _rows(store, table):
    return [dict(row) for row in store._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_schema_two_upgrade_preserves_records_and_adds_nullable_selection(tmp_path, monkeypatch):
    path = tmp_path / "isolated-state" / "taskspindle.sqlite3"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:2])
        old.setattr(
            store_module,
            "TASK_COLUMNS",
            tuple(
                column
                for column in store_module.TASK_COLUMNS
                if column not in {"resolved_model", "resolved_effort", "provider_family"}
            ),
        )
        with Store.open(path) as store:
            assert store.schema_version() == 2
            task = make_task(store)
            store.update_task(
                task.id,
                None,
                session_id="retained-session",
                requested_model="claude-opus-4",
                reported_model="claude-opus-4",
                response="retained partial response",
            )
            store.upsert_grant("repo1", "claude", "implement")
            store.acquire_lease("claude", task.id, "unit-a", 123, "boot")
            store.append_event(task.id, "WARNING", {"reason": "retained event"})
            turn = store.insert_turn(task.id, 1, "INITIAL", session_id="retained-session")
            store.insert_turn_usage(
                turn, task.id, "claude", source="acp_prompt_usage", input_tokens=12, output_tokens=3
            )
            store.add_receipt(task.id, "sha256:retained", 0, 123)
            review = make_task(store, "ts_000000000002", provider="grok")
            store.insert_review(
                review.id,
                task.id,
                "retained-candidate",
                "grok",
                ReviewOutput(verdict=Verdict.PASS, summary="retained independent review"),
            )
            store.write_journal(
                task.id,
                "PREPARED",
                target_head="retained-target",
                candidate_sha="retained-candidate",
                changed_paths=["src/a.py"],
            )
            tables = [
                "tasks",
                "repositories",
                "repository_grants",
                "events",
                "turns",
                "turn_usage",
                "leases",
                "artifact_receipts",
                "reviews",
                "integration_journal",
            ]
            before = {table: _rows(store, table) for table in tables}

    with Store.open(path) as store:
        assert store.schema_version() == 6
        assert store.migrate() == []
        for table in tables:
            after = _rows(store, table)
            if table == "tasks":
                for row in after:
                    assert row.pop("resolved_model") is None
                    assert row.pop("resolved_effort") is None
                    assert row.pop("provider_family") is None
            assert after == before[table]
        migrated = store.get_task(task.id)
        assert migrated.session_id == "retained-session"
        assert migrated.resolved_model is None
        version = migrated.state_version
        store.update_task(
            task.id, version, resolved_model="gemini-3.8-flash-medium", resolved_effort="medium"
        )

    with Store.open(path) as reopened:
        selected = reopened.get_task(task.id)
        assert selected.resolved_model == "gemini-3.8-flash-medium"
        assert selected.resolved_effort == "medium"
        assert selected.state_version == version + 1
        assert reopened.grant_active("repo1", "claude", "implement")
        assert reopened.get_lease("claude")["task_id"] == task.id
