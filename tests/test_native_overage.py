from dataclasses import replace
from datetime import UTC, datetime

import pytest

from taskspindle.config import ConfigError, native_overage_policies
from taskspindle.native_overage import normalize_observation, project
from taskspindle.providers import Profile
from taskspindle.store import Store


@pytest.fixture
def profile():
    return Profile(id="claude", auth="oauth", command=("claude",), model="sonnet")


def test_exact_profile_opt_in_and_validation(profile):
    alias = replace(profile, id="alias", base="claude")
    profiles = {"claude": profile, "alias": alias}
    assert native_overage_policies({}, profiles) == {"alias": "observe_only", "claude": "observe_only"}
    assert (
        native_overage_policies({"native_overage": {"claude": "provider_managed"}}, profiles)["alias"]
        == "observe_only"
    )
    for value in (True, {}, "yes"):
        with pytest.raises(ConfigError):
            native_overage_policies({"native_overage": {"claude": value}}, profiles)
    with pytest.raises(ConfigError):
        native_overage_policies({"native_overage": {"missing": "provider_managed"}}, profiles)
    with pytest.raises(ConfigError):
        native_overage_policies(
            {"native_overage": {"claude": "provider_managed"}}, {"claude": replace(profile, auth="api_key")}
        )


def test_observations_are_allowlisted_and_missing_is_unknown():
    assert normalize_observation({"isUsingOverage": True, "secret": "never"}) == {"in_use": True}
    assert normalize_observation(
        {"overageStatus": "allowed", "overageDisabledReason": "secret arbitrary prose"}
    ) == {"status": "allowed"}
    assert normalize_observation({}) == {}


def test_only_included_exhaustion_qualifies(profile, tmp_path):
    store = Store.open(tmp_path / "db")
    managed = replace(profile, native_overage="provider_managed")
    for window, eligible in [("unknown", False), ("overage", False), ("five_hour", True)]:
        store.insert_provider_window(
            "claude", window, status="rejected", used_percent=100, source="rate_limit_event"
        )
        view = project(store, managed, datetime.now(UTC))
        assert (view["eligibility"] == "overage") is eligible
        store._conn.execute("DELETE FROM provider_quota_restrictions")
    store.close()


def test_claims_are_atomic_and_refusal_survives_sibling_success(profile, tmp_path):
    from taskspindle import native_overage
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    path = tmp_path / "claims.db"
    with Store.open(path) as store, Store.open(path) as sibling:
        store.insert_provider_window("claude", "five_hour", status="rejected", source="rate_limit_event")
        turns = []
        for name in ("first", "second", "third"):
            task = make_task(store, name)
            store.insert_turn(
                task.id,
                1,
                "initial",
                native_overage=native_overage.snapshot(store, profile, datetime.now(UTC), parent_env={}),
            )
            turns.append(store.list_turns(task.id)[-1])
        native_overage.admit(store, profile, turns[0], datetime.now(UTC), model="sonnet", parent_env={})
        assert (
            native_overage.project(sibling, profile, datetime.now(UTC), model="sonnet", parent_env={})[
                "admission_reason"
            ]
            == "native_attempt_pending"
        )
        native_overage.admit(
            store,
            profile,
            store.list_turns("first")[-1],
            datetime.now(UTC),
            model="sonnet",
            parent_env={},
            prompting=True,
        )
        with pytest.raises(Exception, match="native_attempt_pending"):
            native_overage.admit(sibling, profile, turns[1], datetime.now(UTC), model="sonnet", parent_env={})
        store.finish_native_overage(turns[0]["id"], True)
        for turn in turns[1:]:
            native_overage.admit(
                store, profile, turn, datetime.now(UTC), model="sonnet", parent_env={}, prompting=True
            )
        store.finish_native_overage(turns[1]["id"], False, "PROVIDER_THROTTLED")
        store.finish_native_overage(turns[2]["id"], True)
        assert (
            native_overage.project(store, profile, datetime.now(UTC), model="sonnet", parent_env={})[
                "admission_reason"
            ]
            == "native_attempt_refused"
        )
        assert store.list_quota_restrictions("claude")


def test_schema_nine_preserves_historical_unknown(tmp_path, monkeypatch):
    from taskspindle import store as store_module
    from tests.test_store import make_task

    path = tmp_path / "old.db"
    with monkeypatch.context() as old:
        old.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS[:8])
        with Store.open(path) as store:
            task = make_task(store, "history")
            store._conn.execute(
                "INSERT INTO turns(task_id,revision,kind,started_at,response) "
                "VALUES (?,1,'initial','2026-09-09T00:00:00Z','retained')",
                (task.id,),
            )
    with Store.open(path) as migrated:
        assert migrated.schema_version() == 9
        turn = migrated.list_turns("history")[0]
        assert turn["response"] == "retained"
        assert turn["native_overage"] is None
        view = migrated.list_native_overage_turns()[0]["native_overage"]
        assert view["policy"] is None
        assert view["billing_classification"] == "unknown"


def test_known_paid_exhaustion_and_hard_status_override_included(profile, tmp_path):
    from taskspindle import native_overage, quota
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    with Store.open(tmp_path / "db") as store:
        store.insert_provider_window("claude", "five_hour", status="rejected", source="rate_limit_event")
        store.set_provider_status(
            "claude", "access_denied", code="PROVIDER_ACCESS_DENIED", source="acp_error"
        )
        assert project(store, profile, datetime.now(UTC))["admission_reason"] == "hard_provider_block"
        store.set_provider_status("claude", "unknown", source="test")
        task = make_task(store, "paid")
        turn = store.insert_turn(task.id, 1, "initial")
        store.record_native_overage_observation(
            "claude",
            task.id,
            turn,
            native_overage.normalize_observation(
                {"overageStatus": "rejected", "overageDisabledReason": "out_of_credits"}
            ),
            "2026-09-09T00:00:00Z",
            quota._auth_context(store, profile, None)["fingerprint"],
        )
        assert (
            project(store, profile, datetime.now(UTC))["admission_reason"]
            == "native_paid_allowance_unavailable"
        )


def test_conflicting_flags_and_recognized_sdk_reason():
    assert normalize_observation({"isUsingOverage": False, "overageInUse": True}) == {}
    assert normalize_observation({"overageDisabledReason": "out_of_credits"}) == {
        "disabled_reason": "out_of_credits"
    }


def test_service_success_never_proves_included_funds(profile, tmp_path):
    with Store.open(tmp_path / "db") as store:
        store.set_provider_status("claude", "ok", source="turn_ok")
        assert project(store, profile, datetime.now(UTC))["eligibility"] == "unknown"
        store.set_provider_auth_context("claude", "old-auth-context")
        assert project(store, profile, datetime.now(UTC))["eligibility"] == "unknown"


def test_paid_reset_allows_one_new_attempt_for_same_included_period(profile, tmp_path):
    from datetime import timedelta

    from taskspindle import native_overage, quota
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    stamp = datetime(2030, 1, 1, tzinfo=UTC)
    with Store.open(tmp_path / "db") as store:
        store.insert_provider_window("claude", "seven_day", status="rejected", source="rate_limit_event")
        context = quota._auth_context(store, profile, {})["fingerprint"]
        first = make_task(store, "first-reset")
        first_id = store.insert_turn(
            first.id,
            1,
            "initial",
            native_overage=native_overage.snapshot(store, profile, stamp, parent_env={}),
        )
        native_overage.admit(
            store,
            profile,
            store.list_turns(first.id)[0],
            stamp,
            model="sonnet",
            parent_env={},
            prompting=True,
        )
        store.finish_native_overage(first_id, False)
        reset = stamp + timedelta(hours=1)
        paid = {"status": "rejected", "disabled_reason": "out_of_credits", "resets_at": reset.isoformat()}
        store.record_native_overage_observation(
            "claude", first.id, first_id, paid, stamp.isoformat(), context
        )
        assert project(store, profile, stamp, parent_env={})["eligibility"] == "blocked"
        after = reset + timedelta(seconds=1)
        assert project(store, profile, after, parent_env={})["eligibility"] == "overage"
        second = make_task(store, "second-reset")
        second_id = store.insert_turn(
            second.id,
            1,
            "initial",
            native_overage=native_overage.snapshot(store, profile, after, parent_env={}),
        )
        native_overage.admit(
            store,
            profile,
            store.list_turns(second.id)[0],
            after,
            model="sonnet",
            parent_env={},
            prompting=True,
        )
        store.finish_native_overage(second_id, False)
        # Re-reading cached evidence retains the original source turn. A new
        # source turn reporting the same refusal is a new denial (tested below).
        store.record_native_overage_observation(
            "claude", first.id, first_id, paid, after.isoformat(), context
        )
        assert project(store, profile, after, parent_env={})["admission_reason"] == "native_attempt_refused"
        # A flag-only observation is not a new grant or a new reset generation.
        store.record_native_overage_observation(
            "claude", second.id, second_id, {"in_use": False}, after.isoformat(), context
        )
        assert project(store, profile, after, parent_env={})["admission_reason"] == "native_attempt_refused"


def test_paid_administrative_disable_does_not_expire_like_credit_window(profile, tmp_path):
    from taskspindle import quota
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    with Store.open(tmp_path / "db") as store:
        store.insert_provider_window("claude", "seven_day", status="rejected", source="rate_limit_event")
        task = make_task(store, "disabled")
        turn = store.insert_turn(task.id, 1, "initial")
        store.record_native_overage_observation(
            "claude",
            task.id,
            turn,
            {
                "status": "rejected",
                "disabled_reason": "org_level_disabled",
                "resets_at": "2020-01-01T00:00:00Z",
            },
            "2020-01-01T00:00:00Z",
            quota._auth_context(store, profile, {})["fingerprint"],
        )
        assert project(store, profile, datetime.now(UTC), parent_env={})["eligibility"] == "blocked"


def test_aliases_share_one_account_attempt_without_inheriting_authorization(profile, tmp_path):
    from taskspindle import native_overage
    from tests.test_store import make_task

    aliases = [
        replace(profile, id=name, base="claude", native_overage="provider_managed")
        for name in ("first", "second")
    ]
    with Store.open(tmp_path / "db") as store:
        store.insert_provider_window("claude", "five_hour", status="rejected", source="rate_limit_event")
        turns = []
        for alias in aliases:
            task = make_task(store, alias.id)
            store.insert_turn(
                task.id,
                1,
                "initial",
                native_overage=native_overage.snapshot(store, alias, datetime.now(UTC), parent_env={}),
            )
            turns.append(store.list_turns(task.id)[0])
        native_overage.admit(
            store, aliases[0], turns[0], datetime.now(UTC), model="sonnet", parent_env={}, prompting=True
        )
        with pytest.raises(Exception, match="native_attempt_pending"):
            native_overage.admit(
                store, aliases[1], turns[1], datetime.now(UTC), model="sonnet", parent_env={}, prompting=True
            )
        assert len(store.list_native_overage_attempts()) == 1
        assert native_overage.fingerprint(aliases[0]) != native_overage.fingerprint(aliases[1])
        assert (
            project(
                store, replace(aliases[1], native_overage="observe_only"), datetime.now(UTC), parent_env={}
            )["eligibility"]
            == "blocked"
        )


def test_sparse_or_older_success_cannot_clear_newer_paid_denial(profile, tmp_path):
    from taskspindle import quota
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    with Store.open(tmp_path / "db") as store:
        store.insert_provider_window("claude", "seven_day", status="rejected", source="rate_limit_event")
        context = quota._auth_context(store, profile, {})["fingerprint"]
        old, new = make_task(store, "old"), make_task(store, "new")
        old_id = store.insert_turn(old.id, 1, "initial", started_at="2030-01-01T00:00:00Z")
        new_id = store.insert_turn(new.id, 1, "initial", started_at="2030-01-01T01:00:00Z")
        stamp = datetime(2030, 1, 1, 2, tzinfo=UTC)
        store.record_native_overage_observation(
            "claude", new.id, new_id, {"disabled_reason": "out_of_credits"}, stamp.isoformat(), context
        )
        for sparse in (
            {"in_use": False},
            {"disabled_reason": "unknown"},
            {"disabled_reason": "fetch_error"},
            {"status": "allowed"},
        ):
            store.record_native_overage_observation(
                "claude", old.id, old_id, sparse, stamp.isoformat(), context
            )
            assert (
                project(store, profile, stamp, parent_env={})["admission_reason"]
                == "native_paid_allowance_unavailable"
            )


def test_paid_recovery_generations_advance_only_on_new_denied_to_allowed_transition(profile, tmp_path):
    from datetime import timedelta

    from taskspindle import native_overage, quota
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    stamp = datetime(2030, 1, 1, tzinfo=UTC)
    with Store.open(tmp_path / "db") as store:
        store.insert_provider_window("claude", "seven_day", status="rejected", source="rate_limit_event")
        context = quota._auth_context(store, profile, {})["fingerprint"]
        keys = []
        for cycle in range(3):
            started = stamp + timedelta(hours=cycle)
            task = make_task(store, f"paid-cycle-{cycle}")
            turn_id = store.insert_turn(task.id, 1, "initial", started_at=started.isoformat())
            store.record_native_overage_observation(
                "claude", task.id, turn_id, {"status": "allowed"}, started.isoformat(), context
            )
            store.update_turn_native_overage(
                turn_id, native_overage.snapshot(store, profile, started, parent_env={})
            )
            native_overage.admit(
                store,
                profile,
                store.list_turns(task.id)[0],
                started,
                model="sonnet",
                parent_env={},
                prompting=True,
            )
            keys.append(store.list_turns(task.id)[0]["native_overage"]["claim_key"])
            store.finish_native_overage(turn_id, False)
            store.record_native_overage_observation(
                "claude", task.id, turn_id, {"status": "allowed_warning"}, started.isoformat(), context
            )
            assert (
                project(store, profile, started, parent_env={})["admission_reason"]
                == "native_attempt_refused"
            )
            store.record_native_overage_observation(
                "claude", task.id, turn_id, {"status": "rejected"}, started.isoformat(), context
            )
            assert (
                project(store, profile, started, parent_env={})["admission_reason"]
                == "native_paid_allowance_unavailable"
            )
        assert len(set(keys)) == 3


def test_reason_only_denial_does_not_inherit_an_allowed_period_reset(profile, tmp_path):
    from taskspindle import quota
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    with Store.open(tmp_path / "db") as store:
        store.insert_provider_window("claude", "seven_day", status="rejected", source="rate_limit_event")
        task = make_task(store, "new-credit-denial")
        turn = store.insert_turn(task.id, 1, "initial")
        context = quota._auth_context(store, profile, {})["fingerprint"]
        stamp = datetime.now(UTC)
        store.record_native_overage_observation(
            "claude",
            task.id,
            turn,
            {"status": "allowed", "resets_at": "2020-01-01T00:00:00Z"},
            stamp.isoformat(),
            context,
        )
        store.record_native_overage_observation(
            "claude", task.id, turn, {"disabled_reason": "out_of_credits"}, stamp.isoformat(), context
        )
        assert "resets_at" not in store.latest_native_overage_observation("claude")["observed"]
        assert (
            project(store, profile, stamp, parent_env={})["admission_reason"]
            == "native_paid_allowance_unavailable"
        )


def test_only_fresh_affirmative_native_allowance_projects_included(profile, tmp_path):
    from taskspindle import quota

    with Store.open(tmp_path / "db") as store:
        evidence = quota.evaluate(store, profile, datetime.now(UTC), parent_env={})
        evidence["native"] = {"state": "quota", "freshness": "fresh", "eligible_hint": True}
        assert project(store, profile, datetime.now(UTC), evidence=evidence)["eligibility"] == "included"
        evidence["native"]["freshness"] = "stale"
        assert project(store, profile, datetime.now(UTC), evidence=evidence)["eligibility"] == "unknown"
        evidence["native"]["freshness"] = "fresh"
        evidence["auth_context"]["changed"] = True
        assert project(store, profile, datetime.now(UTC), evidence=evidence)["eligibility"] == "unknown"


@pytest.mark.parametrize("success", [False, True])
def test_initial_paid_metadata_does_not_rotate_first_attempt_before_or_after_settlement(
    profile, tmp_path, success
):
    from taskspindle import native_overage, quota
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    with Store.open(tmp_path / "db") as store:
        stamp = datetime.now(UTC)
        store.insert_provider_window("claude", "seven_day", status="rejected", source="rate_limit_event")
        task = make_task(store, "initial-paid-metadata")
        turn = store.insert_turn(
            task.id,
            1,
            "initial",
            native_overage=native_overage.snapshot(store, profile, stamp, parent_env={}),
        )
        native_overage.admit(
            store, profile, store.list_turns(task.id)[0], stamp, model="sonnet", parent_env={}, prompting=True
        )
        store.record_native_overage_observation(
            "claude",
            task.id,
            turn,
            {"status": "allowed", "resets_at": "2020-01-01T00:00:00Z"},
            stamp.isoformat(),
            quota._auth_context(store, profile, {})["fingerprint"],
        )
        assert project(store, profile, stamp, parent_env={})["admission_reason"] == "native_attempt_pending"
        native_overage.finish(store, profile, turn, success, now=stamp, parent_env={})
        view = project(store, profile, stamp, parent_env={})
        assert view["eligibility"] == ("overage" if success else "blocked")
        assert len(store.list_native_overage_attempts()) == 1


def test_new_turn_reason_only_denial_cannot_reuse_old_turn_elapsed_reset(profile, tmp_path):
    from taskspindle import quota
    from tests.test_store import make_task

    profile = replace(profile, native_overage="provider_managed")
    with Store.open(tmp_path / "db") as store:
        store.insert_provider_window("claude", "seven_day", status="rejected", source="rate_limit_event")
        context = quota._auth_context(store, profile, {})["fingerprint"]
        old = make_task(store, "old-denial-source")
        new = make_task(store, "new-denial-source")
        old_id = store.insert_turn(old.id, 1, "initial", started_at="2030-01-01T00:00:00Z")
        new_id = store.insert_turn(new.id, 1, "initial", started_at="2030-01-01T02:00:00Z")
        paid = {
            "status": "rejected",
            "disabled_reason": "out_of_credits",
            "resets_at": "2030-01-01T01:00:00Z",
        }
        store.record_native_overage_observation(
            "claude", old.id, old_id, paid, "2030-01-01T00:00:00Z", context
        )
        # Repeated same-source telemetry preserves the original timed evidence.
        store.record_native_overage_observation(
            "claude", old.id, old_id, {"disabled_reason": "out_of_credits"}, "2030-01-01T02:00:00Z", context
        )
        before = store.latest_native_overage_observation("claude")["observed"]
        assert before["paid_observed_at"] == "2030-01-01T00:00:00Z"
        assert (
            project(store, profile, datetime(2030, 1, 1, 2, tzinfo=UTC), parent_env={})["eligibility"]
            == "overage"
        )
        store.record_native_overage_observation(
            "claude", new.id, new_id, {"disabled_reason": "out_of_credits"}, "2030-01-01T02:00:00Z", context
        )
        after = store.latest_native_overage_observation("claude")["observed"]
        assert after["paid_turn_id"] == new_id
        assert after["paid_observed_at"] == "2030-01-01T02:00:00Z"
        assert "resets_at" not in after
        assert (
            project(store, profile, datetime(2030, 1, 1, 2, tzinfo=UTC), parent_env={})["admission_reason"]
            == "native_paid_allowance_unavailable"
        )
