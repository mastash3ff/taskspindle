"""Durable quota evidence is separate from compatibility provider status rows."""

from taskspindle import store as store_module
from taskspindle.store import Store
from taskspindle.web.db import ReadOnlyStore
from tests.test_store import make_task


def test_restrictions_supersede_one_period_and_exact_resolution(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        task = make_task(store, "task-a")
        first = store.record_quota_restriction(
            "claude", scope="account", period_key="five_hour", period_start="2026-09-07T00:00:00Z",
            reset_at="2026-09-07T05:00:00Z", observed_at="2026-09-07T01:00:00Z",
            source="turn_error", task_id=task.id, evidence_fingerprint="first",
        )
        second = store.record_quota_restriction(
            "claude", scope="account", period_key="five_hour", period_start="2026-09-07T00:00:00Z",
            reset_at="2026-09-07T05:00:00Z", observed_at="2026-09-07T01:01:00Z",
            source="turn_error", task_id=task.id, evidence_fingerprint="second",
        )
        weekly = store.record_quota_restriction(
            "claude", scope="model_family", model="opus", period_key="seven_day_opus",
            reset_at="2026-09-10T00:00:00Z", source="turn_error", task_id=task.id,
            evidence_fingerprint="weekly",
        )
        assert first["resolved_at"] is None
        active = store.list_quota_restrictions("claude")
        assert {row["evidence_fingerprint"] for row in active} == {"second", "weekly"}
        resolved = store.resolve_quota_restrictions("claude", ["first", "weekly"], task_id=task.id)
        assert {row["evidence_fingerprint"] for row in resolved} == {"first", "weekly"}
        assert store.list_quota_restrictions("claude")[0]["evidence_fingerprint"] == "second"
        assert second["id"] != weekly["id"]


def test_distinct_periods_for_one_window_are_independent(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        store.record_quota_restriction(
            "claude", scope="account", window="five_hour", period_key="five_hour|first",
            reset_at="2026-09-07T05:00:00Z", source="test", evidence_fingerprint="first",
        )
        store.record_quota_restriction(
            "claude", scope="account", window="five_hour", period_key="five_hour|second",
            reset_at="2026-09-07T10:00:00Z", source="test", evidence_fingerprint="second",
        )
        assert {row["evidence_fingerprint"] for row in store.list_quota_restrictions("claude")} == {
            "first", "second"
        }


def test_contexts_and_retry_history_are_durable_and_read_only(tmp_path):
    path = tmp_path / "state.db"
    with Store.open(path) as store:
        first = make_task(store, "task-first")
        second = make_task(store, "task-second")
        store.set_task_auth_context(first.id, "a" * 64)
        store.set_task_auth_context(first.id, "a" * 64)
        store.set_provider_auth_context("claude", "b" * 64)
        assert store.get_task_auth_context(first.id) == "a" * 64
        assert store.get_provider_auth_context("claude") == "b" * 64
        claim = store.claim_quota_retry("claude", first.id, ["f-1", "f-2", "f-1"])
        assert claim and claim["state"] == "claimed"
        assert store.claim_quota_retry("claude", second.id, ["f-1"]) is None
        assert store.refine_quota_retry(first.id, ["f-1"])["restriction_fingerprints"] == '["f-1"]'
        assert store.mark_quota_retry_prompting(first.id)["state"] == "prompting"
        assert store.finish_quota_retry(first.id, "succeeded", code="TURN_OK")["state"] == "succeeded"
        assert store.successful_quota_retry_fingerprints("claude") == {"f-1"}
        # A spent task's row is history, never a reusable claim.
        assert store.claim_quota_retry("claude", first.id, ["new"]) ["state"] == "succeeded"
        later = store.claim_quota_retry("claude", second.id, ["f-3"])
        assert later and later["state"] == "claimed"
    with ReadOnlyStore(path) as reader:
        assert reader.get_task_auth_context("task-first") == "a" * 64
        assert reader.get_provider_auth_context("claude") == "b" * 64
        assert reader.get_active_quota_retry_claim("claude")["task_id"] == "task-second"
        assert reader.successful_quota_retry_fingerprints("claude") == {"f-1"}


def test_schema_eight_backfills_rejected_evidence_without_resurrecting_allowed_window(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:7])
        with Store.open(path) as store:
            task = make_task(store, "legacy-task")
            store._conn.execute(
                "INSERT INTO provider_windows(provider, window, status, resets_at, observed_at, "
                "task_id, source) "
                "VALUES ('claude', 'five_hour', 'rejected', '2030-01-01T00:00:00Z', "
                "'2026-09-07T00:00:00Z', ?, 'legacy')", (task.id,)
            )
            store._conn.execute(
                "INSERT INTO provider_windows(provider, window, status, observed_at, source) "
                "VALUES ('claude', 'five_hour', 'allowed', '2026-09-07T01:00:00Z', 'legacy')"
            )
            store._conn.execute(
                "INSERT INTO provider_windows(provider, window, status, resets_at, observed_at, source) "
                "VALUES ('claude', 'seven_day_opus', 'rejected', '2030-01-02T00:00:00Z', "
                "'2026-09-07T02:00:00Z', 'legacy')"
            )
            store.set_provider_status("grok", "throttled", window="daily", source="legacy",
                                      observed_at="2026-09-07T03:00:00Z")
            store.set_provider_status("gemini", "throttled", window="daily", source="legacy",
                                      observed_at="2026-09-07T03:00:00Z")
            store._conn.execute(
                "UPDATE provider_status SET last_success_at = '2026-09-07T04:00:00Z' "
                "WHERE provider = 'gemini'"
            )
    with Store.open(path) as migrated:
        rows = migrated.list_quota_restrictions(unresolved_only=False)
        assert {(row["status_key"], row["period_key"], row["scope"], row["model"]) for row in rows} == {
            ("claude", "five_hour|2030-01-01T00:00:00Z", "account", None),
            ("claude", "seven_day_opus|2030-01-02T00:00:00Z", "model_family", "opus"),
            ("grok", "daily|unknown", "account", None),
        }


def test_schema_six_and_seven_keep_independent_status_and_window_periods(tmp_path, monkeypatch):
    for legacy_version in (6, 7):
        path = tmp_path / f"legacy-{legacy_version}.db"
        with monkeypatch.context() as old:
            old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:legacy_version])
            with Store.open(path) as store:
                store.set_provider_status(
                    "claude", "throttled", window="seven_day_opus", reset_at="2030-01-14T00:00:00Z",
                    source="legacy", observed_at="2026-09-07T00:00:00Z",
                )
                for reset in ("2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z"):
                    store._conn.execute(
                        "INSERT INTO provider_windows(provider, window, status, resets_at, observed_at, "
                        "source) "
                        "VALUES ('claude', 'five_hour', 'rejected', ?, '2026-09-07T01:00:00Z', 'legacy')",
                        (reset,),
                    )
        with Store.open(path) as migrated:
            rows = migrated.list_quota_restrictions("claude")
            assert {(row["window"], row["period_key"], row["scope"], row["model"]) for row in rows} == {
                ("seven_day_opus", "seven_day_opus|2030-01-14T00:00:00Z", "model_family", "opus"),
                ("five_hour", "five_hour|2030-01-01T00:00:00Z", "account", None),
                ("five_hour", "five_hour|2030-01-02T00:00:00Z", "account", None),
            }


def test_schema_six_and_seven_do_not_resurrect_success_cleared_window(tmp_path, monkeypatch):
    for legacy_version in (6, 7):
        path = tmp_path / f"legacy-success-{legacy_version}.db"
        with monkeypatch.context() as old:
            old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:legacy_version])
            with Store.open(path) as store:
                store._conn.execute(
                    "INSERT INTO tasks(id, state, cleanup_state, provider, auth_mode, mode, prompt, "
                    "timeout_s, created_at, updated_at) VALUES "
                    "('successful-task', 'COMPLETED', 'COMPLETE', 'claude', 'oauth', 'implement', "
                    "'done', 60, '2026-09-07T00:00:00Z', '2026-09-07T00:00:00Z')"
                )
                for reset, observed in (
                    ("2030-01-01T00:00:00Z", "2026-09-07T01:00:00Z"),
                    ("2030-01-02T00:00:00Z", "2026-09-07T03:00:00Z"),
                ):
                    store._conn.execute(
                        "INSERT INTO provider_windows(provider, window, status, resets_at, observed_at, "
                        "source) VALUES ('claude', 'five_hour', 'rejected', ?, ?, 'legacy')",
                        (reset, observed),
                    )
                store.set_provider_status(
                    "claude", "ok", source="turn_ok", task_id="successful-task",
                    observed_at="2026-09-07T02:00:00Z",
                )
                store._conn.execute(
                    "UPDATE provider_status SET last_success_at = '2026-09-07T02:00:00Z' "
                    "WHERE provider = 'claude'"
                )
        with Store.open(path) as migrated:
            rows = migrated.list_quota_restrictions("claude")
            assert [(row["window"], row["period_key"]) for row in rows] == [
                ("five_hour", "five_hour|2030-01-02T00:00:00Z")
            ]


def test_schema_eight_success_backfill_keeps_other_model_family_restriction(tmp_path, monkeypatch):
    path = tmp_path / "legacy-family.db"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:7])
        with Store.open(path) as store:
            store._conn.execute(
                "INSERT INTO tasks(id, state, cleanup_state, provider, auth_mode, mode, prompt, "
                "timeout_s, created_at, updated_at, resolved_model, provider_family) VALUES "
                "('opus-success', 'COMPLETED', 'COMPLETE', 'claude', 'oauth', 'implement', "
                "'done', 60, '2026-09-07T00:00:00Z', '2026-09-07T00:00:00Z', "
                "'claude-opus-5', 'claude')"
            )
            for window in ("seven_day_opus", "seven_day_sonnet"):
                store._conn.execute(
                    "INSERT INTO provider_windows(provider, window, status, resets_at, observed_at, "
                    "source) VALUES ('claude', ?, 'rejected', '2030-01-01T00:00:00Z', "
                    "'2026-09-07T01:00:00Z', 'legacy')",
                    (window,),
                )
            store.set_provider_status(
                "claude", "ok", source="turn_ok", task_id="opus-success",
                observed_at="2026-09-07T02:00:00Z",
            )
            store._conn.execute(
                "UPDATE provider_status SET last_success_at = '2026-09-07T02:00:00Z' "
                "WHERE provider = 'claude'"
            )
    with Store.open(path) as migrated:
        assert [(row["scope"], row["model"]) for row in migrated.list_quota_restrictions("claude")] == [
            ("model_family", "sonnet")
        ]


def test_window_telemetry_is_normalized_like_its_restriction(tmp_path):
    with Store.open(tmp_path / "state.db") as store:
        store.insert_provider_window(
            "claude", "seven_day_opus", status="allowed", source="test",
            resets_at="2030-01-01T00:00:00Z", observed_at="2026-09-07T00:00:00Z",
        )
        row = store.latest_provider_windows("claude")[0]
        assert row["scope"] == "model_family"
        assert row["model"] == "opus"
        assert row["period_key"] == "seven_day_opus|2030-01-01T00:00:00Z"
        assert row["evidence_fingerprint"]


def test_schema_eight_revokes_unbound_armed_permits_but_keeps_claimed_spent(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:7])
        with Store.open(path) as store:
            task = make_task(store, "spent-task")
            values = ("claude", "claude", "armed", "evidence", "2026-09-07T00:00:00Z", "2030-01-01T00:00:00Z")
            store._conn.execute(
                "INSERT INTO provider_recovery_permits(permit_id, provider, status_key, state, "
                "evidence_revision, created_at, expires_at) VALUES ('armed', ?, ?, ?, ?, ?, ?)", values
            )
            store._conn.execute(
                "INSERT INTO provider_recovery_permits(permit_id, provider, status_key, state, "
                "evidence_revision, created_at, expires_at, task_id) VALUES "
                "('claimed', ?, ?, 'claimed', ?, ?, ?, ?)",
                ("grok", "grok", "evidence", "2026-09-07T00:00:00Z", "2030-01-01T00:00:00Z", task.id),
            )
    with Store.open(path) as migrated:
        assert migrated.get_recovery_permit("armed")["state"] == "revoked"
        assert migrated.get_recovery_permit("armed")["outcome_code"] == "AUTH_CONTEXT_UNBOUND"
        assert migrated.get_recovery_permit("claimed")["state"] == "claimed"


def test_readonly_schema_seven_falls_back_for_new_tables(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:7])
        with Store.open(path):
            pass
    with ReadOnlyStore(path) as reader:
        assert reader.list_quota_restrictions() == []
        assert reader.get_provider_auth_context("claude") is None
        assert reader.get_active_quota_retry_claim("claude") is None
