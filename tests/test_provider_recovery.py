"""Recovery authority persists, binds exact evidence, and cannot evade account limits."""

import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from taskspindle import provider_recovery as recovery
from taskspindle.providers import Profile
from taskspindle.service import TaskSpindleError, model_availability, provider_availability
from taskspindle.store import Store
from taskspindle.web.db import ReadOnlyStore
from tests.test_store import make_task

NOW = datetime(2026, 9, 7, tzinfo=UTC)
OLD = "2026-08-01T00:00:00Z"
PROFILE = Profile(id="claude", auth="oauth", command=("unused",))


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "state.db") as value:
        value.set_provider_status("claude", "auth_expired", source="turn_error", observed_at=OLD)
        yield value


def arm(store, profile=PROFILE, model=None, now=NOW):
    return recovery.arm(
        store,
        profile,
        now=now,
        model=model,
        evidence_revision=recovery.status(store, profile, now=now, model=model)["evidence_revision"],
    )


def test_restart_idempotence_expiry_and_revocation(store):
    first = arm(store)
    assert arm(store)["permit_id"] == first["permit_id"]
    with Store.open(store.path) as reopened:
        assert recovery.status(reopened, PROFILE, now=NOW)["permit_id"] == first["permit_id"]
        before = reopened._conn.total_changes
        assert recovery.status(reopened, PROFILE, now=NOW + timedelta(hours=24))["state"] == "expired"
        assert reopened._conn.total_changes == before
        newer = arm(reopened, now=NOW + timedelta(hours=24))
        assert newer["permit_id"] != first["permit_id"]
        assert reopened.get_recovery_permit(first["permit_id"])["state"] == "expired"
        assert recovery.revoke(reopened, newer["permit_id"], now=NOW)["state"] == "revoked"
        assert recovery.revoke(reopened, newer["permit_id"], now=NOW)["state"] == "revoked"


def test_claim_is_permanent_finish_idempotent_and_refusal_retained(store):
    permit = arm(store)
    make_task(store, "task1")
    make_task(store, "task2")
    recovery.claim(store, PROFILE, permit["permit_id"], "task1", now=NOW)
    with pytest.raises(TaskSpindleError, match="cannot be revoked"):
        recovery.revoke(store, permit["permit_id"], now=NOW)
    with pytest.raises(TaskSpindleError):
        recovery.claim(store, PROFILE, permit["permit_id"], "task2", now=NOW)
    assert arm(store)["task_id"] == "task1"
    recovery.finish(store, "task1", "failed", now=NOW, code="AUTH_EXPIRED")
    recovery.finish(store, "task1", "succeeded", now=NOW)
    assert store.get_task_recovery_permit("task1")["state"] == "failed"
    assert store.get_provider_status("claude")["state"] == "auth_expired"
    assert len(store.list_events("task1")) == 1
    assert arm(store)["permit_id"] != permit["permit_id"]


def test_alias_visibility_is_not_authority(store):
    permit = arm(store)
    alias = Profile(id="other-claude", base="claude", auth="oauth", command=("unused",))
    assert recovery.status(store, alias, now=NOW)["permit_id"] == permit["permit_id"]
    with pytest.raises(TaskSpindleError):
        arm(store, alias)
    with pytest.raises(TaskSpindleError):
        recovery.validate(store, alias, permit["permit_id"], now=NOW)


def test_changed_account_and_model_fingerprints(store):
    permit = arm(store, model="sonnet")
    store.set_provider_model_status(
        "claude", "sonnet", "model_unavailable", source="new_observation", observed_at=OLD
    )
    with pytest.raises(TaskSpindleError) as error:
        recovery.validate(store, PROFILE, permit["permit_id"], now=NOW, model="sonnet")
    assert error.value.code == "RECOVERY_EVIDENCE_CHANGED"
    with pytest.raises(TaskSpindleError):
        recovery.arm(store, PROFILE, evidence_revision=permit["evidence_revision"], now=NOW, model="sonnet")


def test_unspecified_model_cannot_follow_resolved_refusal(store):
    permit = arm(store)
    make_task(store, "task1")
    recovery.claim(store, PROFILE, permit["permit_id"], "task1", now=NOW)
    recovery.validate(store, PROFILE, permit["permit_id"], now=NOW, task_id="task1", model="sonnet")
    store.set_provider_model_status(
        "claude", "sonnet", "model_unavailable", source="turn_error", observed_at=OLD
    )
    with pytest.raises(TaskSpindleError):
        recovery.validate(store, PROFILE, permit["permit_id"], now=NOW, task_id="task1", model="sonnet")


@pytest.mark.parametrize("reset", ["2026-09-08T00:00:00Z", "malformed"])
def test_account_reset_restriction_even_when_auth_masks_it(store, reset):
    store.set_provider_status("claude", "auth_expired", source="turn_error", reset_at=reset, observed_at=OLD)
    assert not recovery.status(store, PROFILE, now=NOW)["can_arm"]
    with pytest.raises(TaskSpindleError):
        arm(store)


def test_new_native_exhaustion_cannot_be_masked_by_auth(store, monkeypatch):
    permit = arm(store)
    monkeypatch.setattr(
        recovery,
        "cached_native_check",
        lambda *a, **kw: {
            "eligible_hint": False,
            "checked_at": OLD,
            "reset_at": "2026-09-08T00:00:00Z",
            "window": "daily",
            "used_percent": 100,
        },
    )
    assert not recovery.status(store, PROFILE, now=NOW)["can_arm"]
    with pytest.raises(TaskSpindleError):
        recovery.validate(store, PROFILE, permit["permit_id"], now=NOW)


@pytest.mark.parametrize("observed", [None, "broken", "2026-09-08T00:00:00Z"])
def test_malformed_observation_is_not_recovery_authority(store, observed):
    # Schema enforces non-null; an injected reader also exercises legacy/malformed adapters.
    class Reader:
        def get_provider_status(self, key):
            return {"state": "auth_expired", "observed_at": observed}

        def get_provider_model_status(self, *args):
            return None

    assert not recovery.status(Reader(), PROFILE, now=NOW)["can_arm"]


def test_passed_reset_does_not_need_recovery_missing_status_cannot_arm(store):
    store.set_provider_status("claude", "throttled", source="turn_error", reset_at=OLD, observed_at=OLD)
    assert not recovery.status(store, PROFILE, now=NOW)["can_arm"]
    store._conn.execute("DELETE FROM provider_status")
    assert not recovery.status(store, PROFILE, now=NOW)["can_arm"]


def test_readonly_legacy_and_missing_database_projections(tmp_path, store):
    for path in (tmp_path / "missing.db", store.path):
        with ReadOnlyStore(path) as reader:
            assert recovery.status(reader, PROFILE, now=NOW)["state"] == "none"
    store._conn.execute("DROP TABLE provider_recovery_permits")
    store._conn.execute("DELETE FROM schema_migrations WHERE version = 7")
    with ReadOnlyStore(store.path) as reader:
        before = reader._conn.total_changes
        assert reader.get_recovery_permit("missing") is None
        assert reader.get_task_recovery_permit("missing") is None
        assert provider_availability(reader, PROFILE, now=NOW)["recovery"]["can_arm"]
        assert reader._conn.total_changes == before


def test_project_exact_models_and_profile_default(store):
    store.set_provider_model_status(
        "claude", "sonnet", "model_unavailable", source="turn_error", observed_at=OLD
    )
    profile = Profile(id="claude", auth="oauth", command=("unused",), model="sonnet")
    result = provider_availability(store, profile, now=NOW)
    exact = model_availability(store, profile, now=NOW)[0]
    assert result["evidence_revision"] == exact["evidence_revision"]
    assert result["recovery"]["model"] == "sonnet"


def _compete(path, operation, task_id):
    with Store.open(path) as store:
        try:
            if operation == "arm":
                return arm(store)["permit_id"]
            permit = store.latest_recovery_permit("claude")
            return recovery.claim(store, PROFILE, permit["permit_id"], task_id, now=NOW)["task_id"]
        except TaskSpindleError as error:
            return error.code


def test_cross_process_arm_and_claim_uniqueness(store):
    make_task(store, "task1")
    make_task(store, "task2")
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_compete, [store.path] * 2, ["arm"] * 2, [None] * 2))
        assert results[0] == results[1]
        results = list(pool.map(_compete, [store.path] * 2, ["claim"] * 2, ["task1", "task2"]))
    assert sum(value.startswith("RECOVERY_") for value in results) == 1
    row = store.latest_recovery_permit("claude")
    assert row["task_id"] in ("task1", "task2")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO provider_recovery_permits "
            "(permit_id, provider, status_key, state, evidence_revision, created_at, expires_at) "
            "VALUES ('illegal', 'alias', 'claude', 'armed', 'revision', 'now', 'later')"
        )


def test_outer_transaction_rolls_back_claim_with_task_creation(store):
    permit = arm(store)
    with pytest.raises(RuntimeError), store.transaction():
        make_task(store, "rolled_back")
        recovery.claim(store, PROFILE, permit["permit_id"], "rolled_back", now=NOW)
        raise RuntimeError("simulated create failure")
    assert store.get_task("rolled_back") is None
    assert store.get_recovery_permit(permit["permit_id"])["state"] == "armed"


def test_invalid_legacy_scope_keeps_availability_readable(store):
    profile = Profile(id="legacy provider; $shell", auth="oauth", command=("unused",))
    projected = provider_availability(store, profile, now=NOW)
    assert projected["recovery"]["can_arm"] is False
    assert projected["recovery"]["provider"] is None
    with pytest.raises(TaskSpindleError):
        arm(store, profile)


def test_explicit_model_scope_and_deadline_remain_binding_after_claim(store):
    permit = arm(store, model="sonnet")
    make_task(store, "task1")
    with pytest.raises(TaskSpindleError):
        recovery.claim(store, PROFILE, permit["permit_id"], "task1", now=NOW, model="opus")
    recovery.claim(store, PROFILE, permit["permit_id"], "task1", now=NOW, model="sonnet")
    with pytest.raises(TaskSpindleError) as error:
        recovery.validate(
            store,
            PROFILE,
            permit["permit_id"],
            now=NOW + timedelta(hours=24),
            task_id="task1",
            model="sonnet",
        )
    assert error.value.code == "RECOVERY_EXPIRED"
    # Deadline does not mutate/settle an already claimed attempt when projecting running work.
    assert recovery.status(store, PROFILE, now=NOW + timedelta(days=2))["state"] == "claimed"


def test_active_mismatched_projection_never_suggests_consumption(store):
    arm(store, model="sonnet")
    assert recovery.status(store, PROFILE, now=NOW, model="opus")["next_action"] == "inspect_permit"
    alias = Profile(id="other", base="claude", auth="oauth", command=("unused",))
    assert recovery.status(store, alias, now=NOW, model="sonnet")["next_action"] == "inspect_permit"


def test_success_metadata_does_not_invalidate_exact_observation(store):
    permit = arm(store)
    store._conn.execute("UPDATE provider_status SET last_success_at = ?", (OLD,))
    assert recovery.validate(store, PROFILE, permit["permit_id"], now=NOW)
    store.set_provider_status("claude", "auth_expired", source="later_sibling", observed_at=OLD)
    with pytest.raises(TaskSpindleError):
        recovery.validate(store, PROFILE, permit["permit_id"], now=NOW)


def test_profile_default_arm_matches_availability(store):
    profile = Profile(id="claude", auth="oauth", command=("unused",), model="sonnet")
    current = provider_availability(store, profile, now=NOW)
    permit = recovery.arm(store, profile, evidence_revision=current["evidence_revision"], now=NOW)
    assert permit["model"] == "sonnet"
    assert recovery.validate(store, profile, permit["permit_id"], now=NOW)["model"] == "sonnet"


def test_actual_cache_restriction_new_after_arm_and_stale_cache_release(store, tmp_path):
    from tests.test_grok_checks import observation, seed

    profile = Profile(id="grok", auth="oauth", command=("grok",))
    current = NOW + timedelta(hours=10)
    env = {"HOME": str(tmp_path)}
    store.set_provider_status("grok", "auth_expired", source="turn_error", observed_at=OLD)
    revision = recovery.status(store, profile, now=current, parent_env=env)["evidence_revision"]
    permit = recovery.arm(store, profile, now=current, parent_env=env, evidence_revision=revision)
    seed(store, profile, env, observation(100))
    with pytest.raises(TaskSpindleError):
        recovery.validate(store, profile, permit["permit_id"], now=current, parent_env=env)
    assert not recovery.status(store, profile, now=current, parent_env=env)["can_arm"]
    recovery.revoke(store, permit["permit_id"], now=current)
    # Stale quota is not current exhaustion; retained auth still needs explicit authority.
    stale = recovery.status(store, profile, now=current + timedelta(hours=1), parent_env=env)
    assert stale["can_arm"]
    assert store.get_provider_status("grok")["state"] == "auth_expired"


def test_unseen_explicit_model_uses_account_revision_and_stays_discoverable(store):
    profile = Profile(id="claude", auth="oauth", command=("unused",), model="default-model")
    store.set_provider_model_status(
        "claude", "default-model", "model_unavailable", source="acp_error", observed_at=OLD,
    )
    projection = provider_availability(store, profile, now=NOW)
    revision = projection["recovery"]["account_evidence_revision"]
    permit = recovery.arm(store, profile, model="unseen-model", evidence_revision=revision, now=NOW)
    models = {row["affected_model"]: row for row in model_availability(store, profile, now=NOW)}
    assert models["unseen-model"]["state"] == "unknown"
    assert models["unseen-model"]["recovery"]["next_action"] == "use_permit"
    assert models["unseen-model"]["evidence_revision"] == revision
    assert models["unseen-model"]["recovery"]["permit_id"] == permit["permit_id"]
    with pytest.raises(TaskSpindleError):
        recovery.validate(store, profile, permit["permit_id"], model="different-model", now=NOW)
    store.set_provider_model_status(
        "claude", "unrelated", "model_unavailable", observed_at=OLD, source="acp_error",
    )
    recovery.validate(store, profile, permit["permit_id"], model="unseen-model", now=NOW)


def test_new_model_evidence_prevents_arming_with_account_only_revision(store):
    revision = recovery.status(store, PROFILE, now=NOW)["account_evidence_revision"]
    store.set_provider_model_status(
        "claude", "unseen-model", "model_unavailable", observed_at=OLD, source="acp_error",
    )
    with pytest.raises(TaskSpindleError) as error:
        recovery.arm(store, PROFILE, model="unseen-model", evidence_revision=revision, now=NOW)
    assert error.value.code == "RECOVERY_EVIDENCE_CHANGED"
