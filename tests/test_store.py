"""Store behaviour: schema, append-only events, leases and receipts."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from taskspindle.models import (
    AuthMode,
    CheckRecord,
    CleanupState,
    Mode,
    ReviewFinding,
    ReviewOutput,
    Severity,
    TaskRecord,
    TaskState,
    TurnKind,
    Verdict,
)
from taskspindle.store import NotFoundError, StaleStateVersionError, Store, StoreError, now


def make_store(tmp_path: Path) -> Store:
    return Store.open(tmp_path / "state" / "taskspindle.sqlite3")


def make_task(store: Store, task_id: str = "ts_000000000001", provider: str = "claude") -> TaskRecord:
    if store.get_repository("repo1") is None:
        store.insert_repository("repo1", "/repo/.git", "root", "/repo")
    stamp = now()
    record = TaskRecord(
        id=task_id,
        state=TaskState.PREPARING,
        cleanup_state=CleanupState.RETAINED,
        repository_id="repo1",
        provider=provider,
        auth_mode=AuthMode.OAUTH,
        mode=Mode.IMPLEMENT,
        prompt="do the thing",
        path_prefixes=["src/"],
        verification_commands=["pytest -q"],
        candidate_message="Do the thing",
        acceptance_criteria="it is done",
        created_at=stamp,
        updated_at=stamp,
    )
    return store.insert_task(record)


def test_open_creates_a_private_file_and_applies_every_migration(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.schema_version() == 3
    store.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.migrate() == []
    store.close()
    reopened = Store.open(tmp_path / "state" / "taskspindle.sqlite3")
    assert reopened.migrate() == []
    assert reopened.schema_version() == 3
    reopened.close()


def test_events_are_append_only(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = make_task(store)
    store.append_event(record.id, "WARNING", {"note": "careful"})
    assert [event["kind"] for event in store.list_events(record.id)] == ["WARNING"]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("UPDATE events SET kind = 'CLEANUP'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute("DELETE FROM events")
    store.close()


def test_task_roundtrip_and_optimistic_concurrency(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = make_task(store)
    loaded = store.get_task(record.id)
    assert loaded is not None
    assert loaded.path_prefixes == ["src/"]
    assert loaded.allow_metered is False
    assert loaded.mode is Mode.IMPLEMENT

    updated = store.update_task(record.id, None, changed_paths=["src/a.py"], diff_size=12)
    assert updated.state_version == 2
    assert updated.changed_paths == ["src/a.py"]
    assert updated.updated_at >= record.updated_at

    with pytest.raises(StaleStateVersionError):
        store.update_task(record.id, 1, diff_size=13)
    with pytest.raises(StoreError, match="unknown task columns"):
        store.update_task(record.id, None, nonsense=1)
    store.close()


def test_acquire_lease_is_single_flight(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = make_task(store, "ts_000000000001")
    second = make_task(store, "ts_000000000002")

    assert store.acquire_lease("claude", first.id, "unit-a", 111, "boot") is True
    assert store.acquire_lease("claude", second.id, "unit-b", 222, "boot") is False
    assert store.get_lease("claude")["task_id"] == first.id
    assert store.release_lease("claude", first.id) is True
    assert store.get_lease("claude") is None
    assert store.acquire_lease("claude", second.id, "unit-b", 222, "boot") is True
    store.close()


def test_receipt_coverage_merges_intervals(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = make_task(store)
    for offset, length in ((0, 10), (5, 10), (30, 5), (20, 10)):
        store.add_receipt(record.id, "sha256:abc", offset, length)
    store.add_receipt(record.id, "sha256:other", 0, 100)
    assert store.receipt_coverage(record.id, "sha256:abc") == [(0, 15), (20, 35)]
    store.close()


def test_grants_and_journal(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    make_task(store)
    assert store.grant_active("repo1", "claude", "implement") is False
    store.upsert_grant("repo1", "claude", "implement")
    store.upsert_grant("repo1", "claude", "implement")
    assert store.grant_active("repo1", "claude", "implement") is True
    assert len(store.list_grants("repo1")) == 1
    assert store.revoke_grant("repo1", "claude", "implement") is True
    assert store.grant_active("repo1", "claude", "implement") is False

    store.write_journal("ts_000000000001", "staging", "head1", "cand1")
    store.write_journal("ts_000000000001", "committing", "head2", "cand1")
    journal = store.read_journal("ts_000000000001")
    assert journal is not None
    assert journal["phase"] == "committing"
    assert journal["target_head"] == "head2"
    store.clear_journal("ts_000000000001")
    assert store.read_journal("ts_000000000001") is None
    store.close()


def test_turns_checks_artifacts_and_reviews_round_trip(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = make_task(store)
    reviewer = make_task(store, "ts_000000000002", provider="grok")

    store.insert_turn(record.id, 1, TurnKind.INITIAL, prompt="go", attribution={"model": "x"})
    turns = store.list_turns(record.id)
    assert len(turns) == 1
    assert turns[0]["kind"] == "initial"
    assert turns[0]["attribution"] == {"model": "x"}

    store.insert_check(
        record.id,
        1,
        CheckRecord(command="pytest -q", exit_code=0, ok=True, duration_ms=12),
    )
    store.insert_check(
        record.id,
        2,
        CheckRecord(command="ruff check .", exit_code=1, ok=False, duration_ms=3),
    )
    assert [check.command for check in store.list_checks(record.id, 1)] == ["pytest -q"]
    assert len(store.list_checks(record.id)) == 2

    store.insert_artifact(record.id, 1, "diff", "sha256:abc", 100, "/tmp/diff")
    artifact = store.get_artifact(record.id, 1, "diff")
    assert artifact is not None
    assert artifact["digest"] == "sha256:abc"
    assert store.get_artifact(record.id, 2, "diff") is None

    finding = ReviewFinding(
        id="f1", severity=Severity.LOW, path="src/a.py", line=1, evidence="e", remedy="r"
    )
    store.insert_review(
        reviewer.id,
        record.id,
        "cand1",
        "grok",
        ReviewOutput(verdict=Verdict.CONCERN, summary="ok", findings=[finding], checks=["c"]),
    )
    review = store.latest_review_for_subject(record.id, "cand1")
    assert review is not None
    assert review["verdict"] == "CONCERN"
    assert review["findings"][0]["id"] == "f1"
    assert store.get_review_for(reviewer.id) == review
    assert store.latest_review_for_subject(record.id, "other") is None
    store.close()


def test_complete_turn_closes_the_row_a_worker_was_given(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    record = make_task(store)
    turn_id = store.insert_turn(record.id, 1, TurnKind.INITIAL, prompt="go", session_id="s1")

    store.complete_turn(
        turn_id,
        stop_reason="end_turn",
        response="done",
        attribution={"provider": "claude", "model": "sonnet"},
    )
    turn = store.list_turns(record.id)[0]
    assert turn["ended_at"] is not None
    assert turn["stop_reason"] == "end_turn"
    assert turn["response"] == "done"
    assert turn["attribution"] == {"provider": "claude", "model": "sonnet"}
    # The session the row already had is kept when the worker has none to add.
    assert turn["session_id"] == "s1"

    # A turn that produced nothing at all -- a worker that died before it prompted -- still closes.
    empty_id = store.insert_turn(record.id, 2, TurnKind.REPAIR, prompt="again")
    store.complete_turn(empty_id)
    empty = store.list_turns(record.id)[1]
    assert empty["ended_at"] is not None
    assert (empty["stop_reason"], empty["response"], empty["attribution"]) == (None, None, None)

    with pytest.raises(NotFoundError):
        store.complete_turn(empty_id + 1000)
    store.close()


def test_bind_lease_stamps_the_worker_onto_a_lease_it_already_holds(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    holder = make_task(store, "ts_000000000001")
    other = make_task(store, "ts_000000000002")
    store.acquire_lease("claude", holder.id, "", 0, "boot-old")

    assert store.bind_lease("claude", holder.id, unit_name="unit-a", pid=99, boot_id="boot-new")
    lease = store.get_lease("claude")
    assert (lease["unit_name"], lease["pid"], lease["boot_id"]) == ("unit-a", 99, "boot-new")

    # What is not supplied is left alone, and a task that does not hold the lease cannot sign it.
    assert store.bind_lease("claude", holder.id)
    unchanged = store.get_lease("claude")
    assert (unchanged["unit_name"], unchanged["pid"]) == ("unit-a", 99)
    assert unchanged["heartbeat_at"] >= lease["heartbeat_at"]
    assert store.bind_lease("claude", other.id, pid=1) is False
    assert store.get_lease("claude")["pid"] == 99
    store.close()


def test_now_is_utc_iso8601(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert now().endswith("Z")
    store = make_store(tmp_path)
    monkeypatch.setattr("taskspindle.store.now", lambda: "2026-01-01T00:00:00.000000Z")
    record = make_task(store)
    assert store.get_task(record.id).updated_at == record.updated_at
    updated = store.update_task(record.id, None, branch="ts/x")
    assert updated.updated_at == "2026-01-01T00:00:00.000000Z"
    store.close()
