"""Per-provider capacity, independent processes, and operational schema rollback."""
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest

from taskspindle import store as store_module
from taskspindle.config import ConfigError, concurrency_limits
from taskspindle.models import TaskState
from taskspindle.store import Store, StoreError
from tests.test_store import make_task


def _take(args):
    path, task_id = args
    with Store.open(path) as store:
        return store.acquire_lease("claude", task_id, task_id, None, "boot", limit=4)


def test_concurrency_defaults_and_explicit_limits():
    assert concurrency_limits({}, ["claude", "grok", "agy"]) == {"claude": 1, "grok": 1, "agy": 1}
    assert concurrency_limits({"concurrency": {"claude": 4}}, ["claude", "grok"]) == {"claude": 4, "grok": 1}


@pytest.mark.parametrize("raw", [0, -1, True, "4", 1.5, [], None])
def test_bad_capacity_rejected(raw):
    with pytest.raises(ConfigError, match="positive integer"):
        concurrency_limits({"concurrency": {"claude": raw}}, ["claude"])


def test_unknown_provider_and_non_table_rejected():
    with pytest.raises(ConfigError, match="unknown provider"):
        concurrency_limits({"concurrency": {"cluade": 4}}, ["claude"])
    with pytest.raises(ConfigError, match="table"):
        concurrency_limits({"concurrency": 4}, ["claude"])


def test_separate_processes_share_capacity_and_unique_tasks(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store.open(path) as store:
        ids = [make_task(store, f"ts_{i:012d}").id for i in range(12)]
    with ProcessPoolExecutor(max_workers=6, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_take, [(path, task) for task in ids] * 2))
    assert sum(results) == 4
    with Store.open(path) as store:
        leases = store.list_leases("claude")
        assert len(leases) == len({row["task_id"] for row in leases}) == 4
        first, sibling = leases[:2]
        assert store.release_lease("grok", first["task_id"]) is False
        assert store.release_lease("claude", first["task_id"]) is True
        assert store.get_lease("claude", sibling["task_id"]) == sibling
        waiting = next(task for task in ids if task not in {row["task_id"] for row in leases})
        assert store.acquire_lease("claude", waiting, waiting, None, "boot", limit=4)
        assert len(store.list_leases("claude")) == 4


def test_schema_three_upgrade_preserves_leases_and_history(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:3])
        with Store.open(path) as store:
            task = make_task(store)
            store.acquire_lease("claude", task.id, "old-unit", 123, "boot")
            store.upsert_grant("repo1", "claude", "implement")
            store.append_event(task.id, "WARNING", {"note": "preserved"})
            before = {
                table: [dict(row) for row in store._conn.execute(f"SELECT * FROM {table}")]
                for table in ("tasks", "leases", "events", "repository_grants")
            }
    with Store.open(path) as store:
        assert store.schema_version() == 8
        for table, rows in before.items():
            assert [dict(row) for row in store._conn.execute(f"SELECT * FROM {table}")] == rows
        other = make_task(store, "ts_000000000002")
        assert store.acquire_lease("claude", other.id, "new-unit", 456, "boot", limit=4)


def test_rollback_requires_drained_state_and_preserves_history(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:4])
        with Store.open(path) as store:
            task = make_task(store)
            store.append_event(task.id, "WARNING", {"note": "preserved"})
            store.upsert_grant("repo1", "claude", "implement")
            with pytest.raises(StoreError, match="drain"):
                store.rollback_concurrency_schema()
            store.update_task(task.id, None, state=TaskState.COMPLETED)
            store.acquire_lease("claude", task.id, "unit", None, "boot")
            with pytest.raises(StoreError, match="drain"):
                store.rollback_concurrency_schema()
            store.release_lease("claude", task.id)
            store.rollback_concurrency_schema()
            assert store.schema_version() == 3
            assert store.get_task(task.id).state == TaskState.COMPLETED
            assert store.list_events(task.id)[0]["payload"] == {"note": "preserved"}
            assert store.grant_active("repo1", "claude", "implement")
            columns = {row["name"]: row for row in store._conn.execute("PRAGMA table_info(leases)")}
            assert columns["provider"]["pk"] == 1
    # A later intentional re-upgrade remains safe.
    with Store.open(path) as store:
        assert store.schema_version() == 8


def _dispatch(args):
    from pathlib import Path

    from taskspindle.config import Paths
    from taskspindle.orchestrator import Orchestrator
    from taskspindle.providers import Profile
    from tests.fakes.units import FakeUnitBackend

    path, root = args
    root = Path(root)
    with Store.open(path) as store:
        orchestrator = Orchestrator(
            store=store,
            paths=Paths(root / "config.toml", root, root / "data", root / "runtime"),
            profiles={"claude": Profile(id="claude", auth="oauth", command=("true",))},
            units=FakeUnitBackend(), boot="boot", parent_env={}, concurrency={"claude": 4},
        )
        return orchestrator.dispatch_queued()


def test_competing_mcp_dispatch_passes_start_four_unique_tasks(tmp_path):
    path = tmp_path / "state.sqlite3"
    with Store.open(path) as store:
        for index in range(12):
            task = make_task(store, f"ts_{index:012d}")
            store.update_task(task.id, None, state=TaskState.QUEUED)
            store.insert_turn(task.id, 1, "INITIAL")
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        starts = [task for group in pool.map(_dispatch, [(path, tmp_path)] * 8) for task in group]
    assert len(starts) == len(set(starts)) == 4
    with Store.open(path) as store:
        assert len(store.list_leases("claude")) == 4
        assert {lease["task_id"] for lease in store.list_leases()} == set(starts)
