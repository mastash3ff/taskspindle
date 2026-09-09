"""Billing classification must not turn token estimates or shared balances into charges."""

import pytest

from taskspindle import usage


def row(provider="claude", classification="unknown", policy="observe_only", **extra):
    return {
        "task_id": "task-1", "revision": 1, "provider": provider, "mode": "consult",
        "model": "model-1", "repository_id": "repo-1", "captured_at": "2030-01-02T00:00:00Z",
        "native_overage": {
            "policy": policy, "billing_classification": classification,
            "observed": {"prepaid_balance": 10000, "on_demand_used": 5000},
        },
        **extra,
    }


def test_report_counts_unknown_and_mixed_without_inventing_task_charges():
    rows = [row(), row(classification="included"), row(classification="native_overage"),
            row(classification="mixed"), row(native_overage=None)]
    result = usage.native_overage_report(rows, group_by="provider")
    assert result["total_turns"] == 5
    assert result["unknown_turns"] == 2
    assert result["observed_turns"] == 3
    assert sum(item["turns"] for item in result["groups"]) == 5
    assert {item["billing_classification"] for item in result["groups"]} == {
        "included", "native_overage", "mixed", "unknown",
    }
    assert all("cost" not in key and "balance" not in key for item in result["groups"] for key in item)


@pytest.mark.parametrize("group_by", ["provider", "day", "provider_day", "model", "mode", "repository_id"])
def test_native_overage_rollup_keeps_the_requested_dimensions(group_by):
    result = usage.native_overage_report([row()], group_by=group_by, repositories={"repo-1": "/repo"})
    group = result["groups"][0]
    expected = {"provider": "claude", "day": "2030-01-02", "model": "model-1",
                "mode": "consult", "repository_id": "repo-1"}
    for key in group_by.split("_") if group_by == "provider_day" else [group_by]:
        assert group[key] == expected[key]
    if group_by == "repository_id":
        assert group["repository_path"] == "/repo"


def test_unknown_policy_and_classification_are_not_trusted():
    result = usage.native_overage_report([row(classification="free", policy="SECRET")], group_by="provider")
    assert result["groups"][0]["billing_classification"] == "unknown"
    assert result["groups"][0]["policy"] == "unknown"


def test_readonly_report_matches_store_including_tokenless_history_and_filters(tmp_path):
    from taskspindle.store import Store
    from taskspindle.web.db import ReadOnlyStore
    from tests.test_store import make_task

    path = tmp_path / "db"
    with Store.open(path) as store:
        first = make_task(store)
        second = make_task(store, task_id="ts_000000000002", provider="grok")
        store.insert_turn(first.id, 1, "initial", started_at="2030-01-01T00:00:00Z")
        store.insert_turn(second.id, 1, "initial", started_at="2030-01-02T00:00:00Z",
                          native_overage={"policy": "provider_managed",
                                          "billing_classification": "native_overage"})
        with ReadOnlyStore(path) as reader:
            assert reader.list_native_overage_turns() == store.list_native_overage_turns()
            for kwargs in ({}, {"provider": "grok"}, {"since": "2030-01-02T00:00:00Z"}):
                result = usage.report(reader, **kwargs)
                assert result["native_overage"] == usage.report(store, **kwargs)["native_overage"]
                assert result["native_overage"]["total_turns"] == (2 if not kwargs else 1)
                assert result["usage"] == []
            assert reader.list_turns(second.id) == store.list_turns(second.id)


def test_readonly_schema8_and_missing_database_remain_unknown_without_migration(tmp_path):
    import sqlite3

    from taskspindle.store import Store
    from taskspindle.web.db import ReadOnlyStore
    from tests.test_store import make_task

    missing = tmp_path / "missing"
    with ReadOnlyStore(missing) as reader:
        assert reader.list_native_overage_turns() == []
        assert reader.get_native_overage_attempt("key") is None
        assert reader.list_native_overage_attempts() == []
        assert reader.latest_native_overage_observation("claude") is None
    assert not missing.exists()
    path = tmp_path / "db"
    with Store.open(path) as store:
        task = make_task(store)
        store.insert_turn(task.id, 1, "initial")
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE native_overage_attempts")
        connection.execute("DROP TABLE native_overage_observations")
        connection.execute("ALTER TABLE turns DROP COLUMN native_overage")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
    before = path.read_bytes()
    with ReadOnlyStore(path) as reader:
        assert reader.schema_version() == 8
        assert reader.list_native_overage_turns()[0]["native_overage"]["billing_classification"] == "unknown"
        assert reader.get_native_overage_attempt("key") is None
        assert reader.list_native_overage_attempts() == []
        assert reader.latest_native_overage_observation("claude") is None
        assert usage.report(reader)["native_overage"]["unknown_turns"] == 1
    assert path.read_bytes() == before


def test_dashboard_policy_refreshes_after_optin_and_revocation(tmp_path):
    from tests.test_web import _client, _paths

    paths = _paths(tmp_path)
    client = _client(paths)

    def policy():
        response = client.get("/api/providers")
        assert response.status_code == 200
        provider = next(row for row in response.json()["providers"] if row["id"] == "claude")
        return provider["availability"]["native_overage"]["policy"]

    assert policy() == "observe_only"
    paths.config_file.write_text('[native_overage]\nclaude = "provider_managed"\n')
    assert policy() == "provider_managed"
    paths.config_file.unlink()
    assert policy() == "observe_only"
    assert not (paths.state_dir / "taskspindle.sqlite3").exists()
