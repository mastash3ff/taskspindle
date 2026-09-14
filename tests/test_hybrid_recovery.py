"""Real-store recovery budgets, scope, and admission survive clients and restarts."""

from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from taskspindle import config, providers
from taskspindle.service import TaskSpindleError, provider_availability
from taskspindle.store import Store
from tests.test_store import make_task

NOW = datetime(2026, 9, 12, tzinfo=UTC)


def profile(name="claude"):
    return replace(
        providers.Profile(id=name, base="claude", auth="oauth", command=("unused",)),
        provider_recovery="hybrid",
    )


def status(store, now=NOW, selected=None, model=None):
    return provider_availability(
        store, selected or profile(), now=now, model=model, parent_env={"HOME": "/nonexistent"}
    )["automatic_recovery"]


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "recovery.db") as opened:
        opened.set_provider_status("claude", "auth_expired", source="acp_error", observed_at=NOW.isoformat())
        yield opened


def attempt(store, task, now, model=None):
    from taskspindle import hybrid_recovery as hybrid

    with store.transaction():
        make_task(store, task)
        hybrid.admit(store, profile(), task, now=now, model=model, parent_env={"HOME": "/nonexistent"})
    hybrid.admit(
        store, profile(), task, now=now, model=model, parent_env={"HOME": "/nonexistent"}, prompting=True
    )


def test_policy_exact_profile_validation_and_manual_default():
    names = {"claude": providers.Profile(id="claude", auth="oauth", command=("unused",))}
    assert config.provider_recovery_policies({}, names) == {"claude": "manual"}
    assert config.provider_recovery_policies({"provider_recovery": {"claude": "hybrid"}}, names) == {
        "claude": "hybrid"
    }
    for invalid in ({"alias": "hybrid"}, {"claude": True}, {"claude": "automatic"}):
        with pytest.raises(config.ConfigError):
            config.provider_recovery_policies({"provider_recovery": invalid}, names)


def test_three_turn_budget_uses_delays_from_previous_failure_and_survives_restart(store):
    from taskspindle import hybrid_recovery as hybrid

    view = status(store)
    assert view["state"] == "cooldown"
    assert view["next_attempt_at"] == "2026-09-12T00:05:00Z"
    assert view["attempts_used"] == 0
    initial_episode = view["episode_id"]
    now = NOW
    for index, delay in enumerate((5, 15, 60), 1):
        now += timedelta(minutes=delay)
        assert status(store, now - timedelta(seconds=1))["state"] == "cooldown"
        assert status(store, now)["state"] == "trial_ready"
        attempt(store, f"task{index}", now)
        assert status(store, now)["state"] == "trial_running"
        store.set_provider_status("claude", "auth_expired", source="acp_error", observed_at=now.isoformat())
        hybrid.finish(store, f"task{index}", "failed", now=now)
        assert status(store, now)["attempts_used"] == index
    with Store.open(store.path) as reopened:
        held = status(reopened, now + timedelta(days=365))
        assert held["episode_id"] == initial_episode
        assert held["state"] == "held"
        assert held["attempts_remaining"] == 0
        assert held["next_attempt_at"] is None


def observe(store, state, at, **values):
    from taskspindle.access_checks import native_fingerprint

    fingerprint = native_fingerprint(profile(), {"HOME": "/nonexistent"})
    stamp = at.isoformat().replace("+00:00", "Z")
    assert store.claim_native_check("claude", fingerprint, "test", stamp, stamp, stamp)
    assert store.finish_native_check(
        "claude",
        fingerprint,
        "test",
        stamp,
        {
            "state": state,
            "source": "claude_auth_status",
            "checked_at": stamp,
            **values,
        },
    )


def hold(store):
    from taskspindle import hybrid_recovery as hybrid

    now = NOW
    for index, delay in enumerate((5, 15, 60)):
        now += timedelta(minutes=delay)
        attempt(store, f"initial{index}", now)
        hybrid.finish(store, f"initial{index}", "failed", now=now)
    return now


def test_only_new_relevant_semantic_transition_releases_hold_once(store):
    from taskspindle import hybrid_recovery as hybrid

    now = hold(store)
    observe(store, "cached_auth", now, plan="max")
    assert status(store, now)["state"] == "held"  # unknown -> positive is not recovery
    observe(store, "auth_required", now + timedelta(minutes=5))
    observe(store, "check_failed", now + timedelta(minutes=10))
    observe(store, "cached_auth", now + timedelta(minutes=15), plan="max")
    now += timedelta(minutes=15)
    assert status(store, now)["state"] == "trial_ready"
    attempt(store, "positive-trial", now)
    hybrid.finish(store, "positive-trial", "failed", now=now)
    observe(store, "cached_auth", now + timedelta(minutes=5), plan="max")
    assert status(store, now + timedelta(minutes=5))["state"] == "held"


def test_fresh_native_denial_blocks_without_consuming_model_attempt(store):
    from taskspindle import hybrid_recovery as hybrid

    now = NOW + timedelta(minutes=5)
    observe(store, "auth_required", now)
    make_task(store, "blocked")
    with pytest.raises(TaskSpindleError):
        hybrid.admit(store, profile(), "blocked", now=now, parent_env={"HOME": "/nonexistent"})
    assert status(store, now)["attempts_used"] == 0
    assert status(store, now)["active_task_id"] is None


def _compete(path, name):
    from taskspindle import hybrid_recovery as hybrid

    with Store.open(path) as store:
        try:
            with store.transaction():
                make_task(store, name)
                hybrid.admit(
                    store,
                    profile(name),
                    name,
                    now=NOW + timedelta(minutes=5),
                    parent_env={"HOME": "/nonexistent"},
                )
            return "claimed"
        except TaskSpindleError:
            return "blocked"


def test_alias_processes_atomically_claim_with_task_creation(store):
    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_compete, [store.path] * 2, ["first", "second"]))
    assert sorted(outcomes) == ["blocked", "claimed"]
    assert len(store.list_tasks()) == 1
    assert status(store, NOW + timedelta(minutes=5))["attempts_used"] == 0


def test_exact_model_hold_does_not_block_other_models_and_catalog_release_is_scoped(store):
    from taskspindle import hybrid_recovery as hybrid

    store.set_provider_status("claude", "ok", source="turn_ok", observed_at=NOW.isoformat())
    store.set_provider_model_status(
        "claude", "opus", "model_unavailable", source="acp_error", observed_at=NOW.isoformat()
    )
    now = NOW
    for index, delay in enumerate((5, 15, 60)):
        now += timedelta(minutes=delay)
        attempt(store, f"opus{index}", now, model="opus")
        hybrid.finish(store, f"opus{index}", "failed", now=now)
    assert status(store, now, model="opus")["state"] == "held"
    assert status(store, now, model="sonnet")["state"] == "eligible"
    observe(store, "auth_required", now)
    observe(store, "cached_auth", now + timedelta(minutes=5), plan="max")
    assert status(store, now + timedelta(minutes=5), model="opus")["state"] == "held"


def test_readonly_status_never_creates_claims_or_tasks(store):
    from taskspindle.web.db import ReadOnlyStore

    with ReadOnlyStore(store.path) as reader:
        view = status(reader, NOW + timedelta(minutes=5))
        assert view["state"] == "trial_ready"
        assert reader._conn.total_changes == 0
    assert store.list_tasks() == []


def test_quota_window_without_account_status_obeys_budget_and_future_reset(store):
    from taskspindle import hybrid_recovery as hybrid

    store.set_provider_status("claude", "ok", source="turn_ok", observed_at=NOW.isoformat())
    store.insert_provider_window(
        "claude",
        "seven_day_opus",
        source="rate_limit_event",
        status="rejected",
        observed_at=NOW.isoformat(),
        resets_at=(NOW + timedelta(minutes=10)).isoformat(),
    )
    assert status(store, NOW + timedelta(minutes=5), model="opus")["hold_reason"] == "provider_reset_pending"
    assert status(store, NOW + timedelta(minutes=10), model="opus")["state"] == "trial_ready"
    assert status(store, NOW + timedelta(minutes=10), model="sonnet")["state"] == "eligible"
    now = NOW + timedelta(minutes=10)
    for index, delay in enumerate((0, 15, 60)):
        now += timedelta(minutes=delay)
        attempt(store, f"quota{index}", now, model="opus")
        hybrid.finish(store, f"quota{index}", "failed", now=now)
    assert status(store, now + timedelta(days=10), model="opus")["state"] == "held"
    assert status(store, now, model="sonnet")["state"] == "eligible"


@pytest.mark.parametrize(
    "window,selected,want",
    [
        ("seven_day_opus", "sonnet", "eligible"),
        ("seven_day_opus", "opus", "held"),
        ("seven_day_sonnet", "opus", "eligible"),
        ("seven_day_sonnet", "sonnet", "held"),
        ("five_hour", "sonnet", "held"),
        ("seven_day_opus", "unrecognized-model", "held"),
    ],
)
def test_legacy_account_quota_reset_respects_selected_model_scope(store, window, selected, want):
    store.mark_provider_healthy("claude", expected=store.get_provider_status("claude"))
    reset = (NOW + timedelta(hours=1)).isoformat()
    store.set_provider_status(
        "claude", "throttled", source="acp_error", window=window, reset_at=reset, observed_at=NOW.isoformat()
    )
    store.insert_provider_window(
        "claude",
        window,
        source="rate_limit_event",
        status="rejected",
        resets_at=reset,
        observed_at=NOW.isoformat(),
    )
    view = status(store, NOW + timedelta(minutes=5), model=selected)
    assert view["state"] == want
    assert view["hold_reason"] == ("provider_reset_pending" if want == "held" else None)
    if want == "eligible":
        assert view["episode_id"] is None


def test_auth_reset_remains_account_wide_even_with_family_window_metadata(store):
    store.set_provider_status(
        "claude",
        "auth_expired",
        source="acp_error",
        window="seven_day_opus",
        reset_at=(NOW + timedelta(hours=1)).isoformat(),
        observed_at=NOW.isoformat(),
    )
    assert (
        status(store, NOW + timedelta(minutes=5), model="sonnet")["hold_reason"] == "provider_reset_pending"
    )


def test_model_catalog_added_edge_releases_only_that_model_once(store):
    from taskspindle import hybrid_recovery as hybrid

    store.set_provider_status("claude", "ok", source="turn_ok", observed_at=NOW.isoformat())
    store.set_provider_model_status(
        "claude", "opus", "model_unavailable", source="acp_error", observed_at=NOW.isoformat()
    )
    now = NOW
    for index, delay in enumerate((5, 15, 60)):
        now += timedelta(minutes=delay)
        attempt(store, f"catalog{index}", now, model="opus")
        hybrid.finish(store, f"catalog{index}", "failed", now=now)
    observe(store, "catalog_access", now, model_ids=["sonnet"])
    observe(store, "catalog_access", now + timedelta(minutes=5), model_ids=["sonnet", "opus"])
    assert status(store, now + timedelta(minutes=5), model="opus")["state"] == "trial_ready"
    attempt(store, "added-model", now + timedelta(minutes=5), model="opus")
    hybrid.finish(store, "added-model", "failed", now=now + timedelta(minutes=5))
    assert status(store, now + timedelta(days=1), model="opus")["state"] == "held"


def test_existing_manual_permit_prevents_concurrent_automatic_trial(store):
    from taskspindle import hybrid_recovery as hybrid
    from taskspindle import provider_recovery

    now = NOW + timedelta(minutes=5)
    manual = replace(profile(), provider_recovery="manual")
    view = provider_recovery.status(store, manual, now=now, parent_env={"HOME": "/nonexistent"})
    provider_recovery.arm(
        store,
        manual,
        now=now,
        evidence_revision=view["evidence_revision"],
        parent_env={"HOME": "/nonexistent"},
    )
    make_task(store, "automatic")
    with pytest.raises(TaskSpindleError):
        hybrid.admit(store, profile(), "automatic", now=now, parent_env={"HOME": "/nonexistent"})


def test_new_manual_permit_cannot_race_automatic_claim(store):
    from taskspindle import provider_recovery

    now = NOW + timedelta(minutes=5)
    attempt(store, "automatic", now)
    manual = replace(profile(), provider_recovery="manual")
    view = provider_recovery.status(store, manual, now=now, parent_env={"HOME": "/nonexistent"})
    with pytest.raises(TaskSpindleError):
        provider_recovery.arm(
            store,
            manual,
            now=now,
            evidence_revision=view["evidence_revision"],
            parent_env={"HOME": "/nonexistent"},
        )


def test_legacy_database_migrates_restrictions_with_zero_automatic_attempts(tmp_path, monkeypatch):
    from taskspindle import store as store_module

    path = tmp_path / "legacy.db"
    with monkeypatch.context() as before:
        before.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:9])
        with Store.open(path) as legacy:
            legacy.set_provider_status(
                "claude", "auth_expired", source="acp_error", observed_at=NOW.isoformat()
            )
            legacy.insert_provider_window(
                "claude",
                "seven_day_opus",
                source="rate_limit_event",
                status="rejected",
                observed_at=NOW.isoformat(),
            )
    with Store.open(path) as migrated:
        assert migrated.schema_version() == 11
        assert migrated.get_provider_status("claude")["state"] == "auth_expired"
        assert len(migrated.list_quota_restrictions("claude")) == 1
        assert status(migrated)["attempts_used"] == 0
        assert status(migrated, NOW + timedelta(minutes=5))["state"] == "trial_ready"


def test_native_negative_blocks_initial_manual_work_and_cannot_be_permitted(store):
    from taskspindle import provider_recovery
    from taskspindle.service import require_provider_available

    manual = replace(profile(), provider_recovery="manual")
    store.set_provider_status("claude", "ok", source="turn_ok", observed_at=NOW.isoformat())
    observe(store, "auth_required", NOW)
    with pytest.raises(TaskSpindleError):
        require_provider_available(store, manual, now=NOW, parent_env={"HOME": "/nonexistent"})
    store.set_provider_status("claude", "auth_expired", source="acp_error", observed_at=NOW.isoformat())
    assert not provider_recovery.status(store, manual, now=NOW, parent_env={"HOME": "/nonexistent"})[
        "can_arm"
    ]


def test_manual_permits_are_not_an_override_for_hybrid_holds(store):
    from taskspindle import provider_recovery

    now = hold(store)
    hybrid_profile = profile()
    view = provider_recovery.status(store, hybrid_profile, now=now, parent_env={"HOME": "/nonexistent"})
    assert not view["can_arm"]
    with pytest.raises(TaskSpindleError):
        provider_recovery.arm(
            store,
            hybrid_profile,
            now=now,
            evidence_revision=view["evidence_revision"],
            parent_env={"HOME": "/nonexistent"},
        )


def test_quota_positive_transition_releases_credit_hold_once_and_metadata_does_not(store, tmp_path):
    from taskspindle import hybrid_recovery as hybrid
    from taskspindle.access_checks import native_fingerprint

    grok = replace(providers.Profile(id="grok", auth="oauth", command=("grok",)), provider_recovery="hybrid")
    env = {"HOME": str(tmp_path)}
    store.set_provider_status("grok", "access_denied", source="acp_error", observed_at=NOW.isoformat())
    now = NOW
    for index, delay in enumerate((5, 15, 60)):
        now += timedelta(minutes=delay)
        make_task(store, f"grok{index}")
        hybrid.admit(store, grok, f"grok{index}", now=now, parent_env=env, prompting=True)
        hybrid.finish(store, f"grok{index}", "failed", now=now)

    def seed(used, at):
        stamp = at.isoformat().replace("+00:00", "Z")
        fingerprint = native_fingerprint(grok, env)
        assert store.claim_native_check("grok", fingerprint, "test", stamp, stamp, stamp)
        store.finish_native_check(
            "grok",
            fingerprint,
            "test",
            stamp,
            {
                "state": "quota",
                "checked_at": stamp,
                "used_percent": used,
                "window": "monthly",
                "period_start": "2026-09-01T00:00:00Z",
                "reset_at": "2026-10-01T00:00:00Z",
            },
        )

    seed(100, now)
    assert hybrid.status(store, grok, now=now, parent_env=env)["hold_reason"] == "native_quota_exhausted"
    now += timedelta(minutes=5)
    seed(20, now)
    assert hybrid.status(store, grok, now=now, parent_env=env)["state"] == "trial_ready"
    make_task(store, "credit-trial")
    hybrid.admit(store, grok, "credit-trial", now=now, parent_env=env, prompting=True)
    hybrid.finish(store, "credit-trial", "failed", now=now)
    token_dir = tmp_path / ".grok"
    token_dir.mkdir()
    (token_dir / "auth.json").write_text("synthetic metadata only")
    now += timedelta(minutes=5)
    seed(20, now)
    assert hybrid.status(store, grok, now=now, parent_env=env)["state"] == "held"


def test_migration_preserves_model_family_quota_scope(tmp_path, monkeypatch):
    from taskspindle import store as store_module

    path = tmp_path / "family.db"
    with monkeypatch.context() as before:
        before.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:9])
        with Store.open(path) as legacy:
            legacy.set_provider_status(
                "claude",
                "throttled",
                source="acp_error",
                window="seven_day_opus",
                observed_at=NOW.isoformat(),
            )
            legacy.insert_provider_window(
                "claude",
                "seven_day_opus",
                source="rate_limit_event",
                status="rejected",
                observed_at=NOW.isoformat(),
            )
    with Store.open(path) as migrated:
        assert status(migrated, NOW + timedelta(minutes=5), model="sonnet")["state"] == "eligible"
        assert status(migrated, NOW + timedelta(minutes=5), model="opus")["state"] == "trial_ready"


def test_native_only_exhaustion_keeps_one_shared_trial_after_cache_expiry(store, tmp_path):
    from taskspindle import hybrid_recovery as hybrid
    from taskspindle.access_checks import native_fingerprint

    grok = replace(providers.Profile(id="grok", auth="oauth", command=("grok",)), provider_recovery="hybrid")
    alias = replace(grok, id="grok-alias", base="grok")
    env = {"HOME": str(tmp_path)}
    fingerprint = native_fingerprint(grok, env)
    stamp = NOW.isoformat().replace("+00:00", "Z")
    result = {
        "state": "quota",
        "checked_at": stamp,
        "used_percent": 100,
        "window": "monthly",
        "period_start": "2026-09-01T00:00:00Z",
        "reset_at": "2026-10-01T00:00:00Z",
    }
    assert store.claim_native_check("grok", fingerprint, "test", stamp, stamp, stamp)
    store.finish_native_check("grok", fingerprint, "test", stamp, result)
    view = hybrid.status(store, grok, now=NOW, parent_env=env)
    assert view["state"] == "held"
    assert view["attempts_used"] == 0
    assert view["episode_id"] is not None
    episode_id = view["episode_id"]
    now = NOW + timedelta(minutes=5)
    for index, delay in enumerate((0, 15, 60)):
        now += timedelta(minutes=delay)
        make_task(store, f"native-only{index}")
        assert hybrid.admit(store, grok, f"native-only{index}", now=now, parent_env=env, prompting=True)
        with Store.open(store.path) as sibling:
            make_task(sibling, f"competing{index}")
            with pytest.raises(TaskSpindleError):
                hybrid.admit(sibling, alias, f"competing{index}", now=now, parent_env=env, prompting=True)
        hybrid.finish(store, f"native-only{index}", "failed", now=now)
    assert hybrid.status(store, grok, now=now, parent_env=env)["state"] == "held"
    refreshed = (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    assert store.claim_native_check("grok", fingerprint, "refresh", refreshed, refreshed, refreshed)
    store.finish_native_check("grok", fingerprint, "refresh", refreshed, result | {"checked_at": refreshed})
    held = hybrid.status(store, grok, now=now + timedelta(days=30), parent_env=env)
    assert held["state"] == "held"
    assert held["episode_id"] == episode_id
    assert held["attempts_used"] == 3


def test_migration_retains_valid_native_only_exhaustion_with_no_attempts(tmp_path, monkeypatch):
    from taskspindle import hybrid_recovery as hybrid
    from taskspindle import store as store_module
    from taskspindle.access_checks import native_fingerprint

    grok = replace(providers.Profile(id="grok", auth="oauth", command=("grok",)), provider_recovery="hybrid")
    env = {"HOME": str(tmp_path)}
    path = tmp_path / "native-legacy.db"
    stamp = NOW.isoformat().replace("+00:00", "Z")
    fingerprint = native_fingerprint(grok, env)
    with monkeypatch.context() as before:
        before.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:9])
        with Store.open(path) as legacy:
            assert legacy.claim_native_check("grok", fingerprint, "test", stamp, stamp, stamp)
            legacy.finish_native_check(
                "grok",
                fingerprint,
                "test",
                stamp,
                {
                    "state": "quota",
                    "checked_at": stamp,
                    "used_percent": 100,
                    "window": "monthly",
                    "period_start": "2026-09-01T00:00:00Z",
                    "reset_at": "2026-10-01T00:00:00Z",
                },
            )
    with Store.open(path) as migrated:
        view = hybrid.status(migrated, grok, now=NOW + timedelta(days=30), parent_env=env)
        assert view["state"] == "trial_ready"
        assert view["attempts_used"] == 0
        assert view["episode_id"] is not None


def test_migrated_undated_refusal_stays_held_until_relevant_positive_edge(tmp_path, monkeypatch):
    from taskspindle import hybrid_recovery as hybrid
    from taskspindle import store as store_module

    path = tmp_path / "undated.db"
    with monkeypatch.context() as before:
        before.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:9])
        with Store.open(path) as legacy:
            legacy.set_provider_status("claude", "auth_expired", source="acp_error", observed_at="not-a-date")
    with Store.open(path) as migrated:
        view = provider_availability(migrated, profile(), now=NOW, parent_env={"HOME": "/nonexistent"})
        assert view["automatic_recovery"]["state"] == "held"
        assert view["automatic_recovery"]["attempts_used"] == 0
        assert view["automatic_recovery"]["next_attempt_at"] is None
        assert not view["retry_eligible"]
        observe(migrated, "cached_auth", NOW, plan="max")
        assert status(migrated, NOW)["state"] == "held"
        observe(migrated, "auth_required", NOW + timedelta(minutes=5))
        observe(migrated, "cached_auth", NOW + timedelta(minutes=10), plan="max")
        now = NOW + timedelta(minutes=10)
        assert status(migrated, now)["state"] == "trial_ready"
        attempt(migrated, "undated-trial", now)
        hybrid.finish(migrated, "undated-trial", "failed", now=now)
        assert status(migrated, now + timedelta(days=365))["state"] == "held"


@pytest.mark.parametrize("negative", ["quota", "auth_required"])
def test_migrated_native_baseline_releases_exhausted_episode_once(tmp_path, monkeypatch, negative):
    from taskspindle import hybrid_recovery as hybrid
    from taskspindle import store as store_module
    from taskspindle.access_checks import native_fingerprint

    grok = replace(providers.Profile(id="grok", auth="oauth", command=("grok",)), provider_recovery="hybrid")
    env = {"HOME": str(tmp_path)}
    path = tmp_path / "native-baseline.db"
    fingerprint = native_fingerprint(grok, env)
    sample = {
        "state": negative,
        "checked_at": NOW.isoformat().replace("+00:00", "Z"),
        "used_percent": 100,
        "window": "monthly",
        "period_start": "2026-09-01T00:00:00Z",
        "reset_at": "2026-10-01T00:00:00Z",
    }

    def cache(store, at, payload):
        stamp = at.isoformat().replace("+00:00", "Z")
        assert store.claim_native_check("grok", fingerprint, "test", stamp, stamp, stamp)
        store.finish_native_check("grok", fingerprint, "test", stamp, payload | {"checked_at": stamp})

    with monkeypatch.context() as before:
        before.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:9])
        with Store.open(path) as legacy:
            if negative == "auth_required":
                legacy.set_provider_status(
                    "grok", "auth_expired", source="acp_error", observed_at=NOW.isoformat()
                )
            cache(legacy, NOW, sample)
    with Store.open(path) as migrated:
        assert migrated.list_recovery_evidence("grok") == []
        assert hybrid.status(migrated, grok, now=NOW, parent_env=env)["attempts_used"] == 0
        now = NOW
        for index, delay in enumerate((5, 15, 60)):
            now += timedelta(minutes=delay)
            make_task(migrated, f"migrated{index}")
            assert hybrid.admit(migrated, grok, f"migrated{index}", now=now, parent_env=env, prompting=True)
            hybrid.finish(migrated, f"migrated{index}", "failed", now=now)
        assert hybrid.status(migrated, grok, now=now, parent_env=env)["state"] == "held"
        cache(migrated, now, sample | {"state": "quota", "used_percent": 20})
        assert hybrid.status(migrated, grok, now=now, parent_env=env)["state"] == "trial_ready"
        make_task(migrated, "baseline-trial")
        hybrid.admit(migrated, grok, "baseline-trial", now=now, parent_env=env, prompting=True)
        hybrid.finish(migrated, "baseline-trial", "failed", now=now)
        cache(migrated, now + timedelta(minutes=5), sample | {"state": "quota", "used_percent": 20})
        assert (
            hybrid.status(migrated, grok, now=now + timedelta(minutes=5), parent_env=env)["state"] == "held"
        )
