"""Dispatch policy: defaults, validation, canonical form, loading and observed status."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskspindle import policy, providers
from taskspindle.models import AuthMode, CleanupState, Mode, TaskRecord, TaskState, TurnKind
from taskspindle.store import PolicyRevisionConflict, Store, now

NOW = datetime(2030, 1, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def profiles(tmp_path: Path) -> dict[str, providers.Profile]:
    return providers.load_profiles(
        {"providers": {"claude-alias": {"base": "claude", "auth": "oauth", "modes": ["consult"]}}},
        runtime_dir=tmp_path / "rt", home=tmp_path, state_dir=tmp_path / "state",
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store.open(tmp_path / "state" / "taskspindle.sqlite3") as opened:
        yield opened


def _task(store: Store, task_id: str, provider: str) -> TaskRecord:
    if store.get_repository("repo1") is None:
        store.insert_repository("repo1", "/repo/.git", "root", "/repo")
    stamp = now()
    return store.insert_task(TaskRecord(
        id=task_id, state=TaskState.RESULT_READY, cleanup_state=CleanupState.RETAINED, repository_id="repo1",
        provider=provider, auth_mode=AuthMode.OAUTH, mode=Mode.CONSULT, prompt="q", created_at=stamp,
        updated_at=stamp,
    ))


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _turn(store: Store, task_id: str, provider: str, *, age: timedelta, tokens: int | None) -> None:
    started = _stamp(NOW - age)
    turn_id = store.insert_turn(task_id, 1, TurnKind.INITIAL, started_at=started, ended_at=started)
    if tokens is not None:
        store.insert_turn_usage(
            turn_id, task_id, provider, input_tokens=tokens, output_tokens=tokens, source="test"
        )


# -- defaults and canonical form -----------------------------------------------------------------


def test_defaults_seed_every_profile_and_the_six_skill_roles(profiles) -> None:
    default = policy.defaults(profiles)
    assert set(default.providers) == {"claude", "grok", "agy", "muse", "claude-alias"}
    assert default.providers["claude"].advertised_models == ["haiku", "sonnet", "opus[1m]"]
    assert default.providers["claude"].models_without_effort == ["haiku"]
    assert default.providers["claude-alias"].advertised_models == ["haiku", "sonnet", "opus[1m]"]
    assert default.providers["agy"].advertised_efforts == ["low", "medium", "high"]
    assert list(default.roles) == ["mechanic", "explorer", "implementer", "planner", "debugger", "reviewer"]
    planner = default.roles["planner"]
    assert planner.provider_preference == ["grok", "agy", "claude"]
    assert default.roles["mechanic"].provider_preference == ["agy", "grok", "claude"]
    assert default.roles["reviewer"].selections["agy"].model == "claude-sonnet-4-6"
    assert default.roles["planner"].selections["agy"].model == "claude-opus-4-6-thinking"
    assert default.providers["agy"].advertised_models[-1] == "gpt-oss-120b-medium"
    assert planner.selections["claude"].model == "opus[1m]"
    assert planner.selections["claude"].effort == "xhigh"
    assert default.roles["mechanic"].selections["claude"].effort is None
    assert policy.validate(default, profiles) == []


def test_fingerprint_is_stable_and_key_order_independent(profiles) -> None:
    a = policy.defaults(profiles)
    b = policy.parse(dict(reversed(list(a.model_dump(mode="json").items()))))
    assert policy.canonical_json(a) == policy.canonical_json(b)
    assert policy.fingerprint(a) == policy.fingerprint(b)
    a.providers["grok"].target_share = 10
    assert policy.fingerprint(a) != policy.fingerprint(b)


# -- validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "loc"),
    [
        ({"providers": {"claude": {"target_share": 101}}}, ["providers", "claude", "target_share"]),
        (
            {"providers": {"claude": {"budgets": {"day": {"turns": 0}}}}},
            ["providers", "claude", "budgets", "day", "turns"],
        ),
        (
            {"providers": {"claude": {"budgets": {"month": {}}}}},
            ["providers", "claude", "budgets", "month", "[key]"],
        ),
        ({"providers": {"claude": {"advertised_models": ["a b"]}}}, ["providers", "claude", "advertised_models"]),  # noqa: E501
        (
            {"providers": {"claude": {"advertised_models": ["x", "x"]}}},
            ["providers", "claude", "advertised_models"],
        ),
        ({"roles": {"Planner": {}}}, ["roles"]),
        ({"roles": {"planner": {"timeout_s": 10}}}, ["roles", "planner", "timeout_s"]),
        (
            {"roles": {"planner": {"provider_preference": ["grok", "grok"]}}},
            ["roles", "planner", "provider_preference"],
        ),
        ({"roles": {"planner": {"unknown": 1}}}, ["roles", "planner", "unknown"]),
        ({"version": 2}, ["version"]),
        ({"share_window": "month"}, ["share_window"]),
    ],
)
def test_shape_errors_are_located(document, loc) -> None:
    with pytest.raises(policy.PolicyError) as excinfo:
        policy.parse(document)
    assert excinfo.value.errors[0]["loc"] == loc


def test_cross_field_rules(profiles) -> None:
    doc = policy.defaults(profiles)
    doc.providers["claude"].target_share = 60
    doc.providers["grok"].target_share = 50
    doc.providers["agy"].enabled = False
    doc.providers["agy"].target_share = 100  # disabled shares do not count
    doc.providers["nope"] = policy.ProviderPolicy()
    doc.providers["claude-alias"].allowed_modes = [Mode.IMPLEMENT]
    doc.providers["claude-alias"].models_without_effort = ["ghost"]
    doc.roles["planner"].provider_preference = ["claude", "missing"]
    doc.roles["planner"].selections["claude"] = policy.Selection(model="opus", effort="xhigh")
    doc.roles["planner"].selections["grok"] = policy.Selection(model="grok-4.6", effort="ultra")
    doc.roles["mechanic"].selections["claude"] = policy.Selection(model="haiku", effort="low")
    doc.roles["mechanic"].selections["agy"] = policy.Selection(model="gemini-3.1-pro-high", effort="low")
    doc.roles["explorer"].selections["agy"] = policy.Selection(model="not-gemini", effort=None)
    doc.roles["explorer"].selections["ghost"] = policy.Selection()
    codes = {(tuple(e["loc"]), e["code"]) for e in policy.validate(doc, profiles)}
    assert (("providers", "nope"), "unknown_provider") in codes
    assert (("providers", "claude-alias", "allowed_modes"), "mode_not_served") in codes
    assert (("providers", "claude-alias", "models_without_effort"), "unadvertised") in codes
    assert (("providers",), "share_sum") in codes
    assert (("roles", "planner", "provider_preference", 1), "unknown_provider") in codes
    assert (("roles", "planner", "selections", "claude", "model"), "unadvertised") in codes
    assert (("roles", "planner", "selections", "grok", "effort"), "unadvertised") in codes
    assert (("roles", "mechanic", "selections", "claude", "effort"), "effort_unsupported") in codes
    assert (("roles", "mechanic", "selections", "agy"), "AGY_MODEL_INVALID") in codes
    assert (("roles", "explorer", "selections", "agy", "model"), "unadvertised") in codes
    assert (("roles", "explorer", "selections", "ghost"), "unknown_provider") in codes


def test_share_sum_of_exactly_one_hundred_is_accepted(profiles) -> None:
    doc = policy.defaults(profiles)
    doc.providers["claude"].target_share = 50
    doc.providers["grok"].target_share = 30
    doc.providers["agy"].target_share = 20
    assert policy.validate(doc, profiles) == []


# -- loading and saving --------------------------------------------------------------------------


def test_load_without_a_row_reports_defaults_at_revision_zero(store, profiles) -> None:
    loaded = policy.load(store, profiles)
    assert loaded.source == "defaults"
    assert loaded.revision == 0
    assert loaded.updated_at is None
    assert loaded.fingerprint == policy.fingerprint(policy.defaults(profiles))
    assert loaded.describe()["document_error"] is None


def test_save_increments_revision_records_history_and_checks_the_expected_revision(store, profiles) -> None:
    doc = policy.defaults(profiles)
    doc.providers["grok"].target_share = 40
    first = policy.save(store, doc, updated_by="cli", if_revision=0, reason="first")
    assert first["revision"] == 1
    assert first["updated_by"] == "cli"
    assert first["document"]["providers"]["grok"]["target_share"] == 40
    with pytest.raises(PolicyRevisionConflict) as excinfo:
        policy.save(store, doc, updated_by="web", if_revision=0)
    assert (excinfo.value.expected, excinfo.value.actual) == (0, 1)
    assert store.get_dispatch_policy()["revision"] == 1
    second = policy.save(store, doc, updated_by="web", if_revision=None)
    assert second["revision"] == 2
    history = store.list_dispatch_policy_history()
    assert [(row["revision"], row["updated_by"], row["reason"]) for row in history] == [
        (2, "web", None), (1, "cli", "first"),
    ]
    assert store.get_dispatch_policy_revision(1)["document"]["providers"]["grok"]["target_share"] == 40
    assert store.get_dispatch_policy_revision(9) is None
    loaded = policy.load(store, profiles)
    assert (loaded.source, loaded.revision, loaded.updated_by) == ("store", 2, "web")
    assert loaded.policy.providers["grok"].target_share == 40


def test_a_stored_document_that_no_longer_parses_falls_back_to_defaults(store, profiles) -> None:
    store.save_dispatch_policy('{"version": 7}', "deadbeef", updated_by="test")
    loaded = policy.load(store, profiles)
    assert loaded.source == "defaults"
    assert loaded.revision == 1
    assert "version" in (loaded.document_error or "")
    assert loaded.policy == policy.defaults(profiles)


# -- status -------------------------------------------------------------------------------------


def test_status_computes_windows_shares_targets_and_budgets(store, profiles) -> None:
    for name in ("claude", "grok", "agy"):
        _task(store, f"ts_{name:0>12}", name)
    # Week: claude 6 turns, grok 2, agy 2 (agy paused). Day: claude 1, grok 2.
    for i in range(5):
        _turn(store, "ts_000000claude", "claude", age=timedelta(days=2, hours=i), tokens=1000)
    _turn(store, "ts_000000claude", "claude", age=timedelta(hours=1), tokens=None)
    _turn(store, "ts_00000000grok", "grok", age=timedelta(hours=2), tokens=500)
    _turn(store, "ts_00000000grok", "grok", age=timedelta(hours=3), tokens=500)
    _turn(store, "ts_000000000agy", "agy", age=timedelta(days=3), tokens=9000)
    _turn(store, "ts_000000000agy", "agy", age=timedelta(days=3), tokens=9000)
    _turn(store, "ts_000000claude", "claude", age=timedelta(days=8), tokens=99999)  # outside both windows

    doc = policy.defaults(profiles)
    doc.providers["claude"].target_share = 50
    doc.providers["grok"].target_share = 30
    doc.providers["agy"].enabled = False
    doc.providers["grok"].budgets["day"] = policy.Budget(turns=2, enforce=True)
    doc.providers["claude"].budgets["week"] = policy.Budget(tokens=100_000)
    policy.save(store, doc, updated_by="cli", if_revision=0)
    loaded = policy.load(store, profiles)
    report = policy.status(store, loaded, profiles, NOW)

    assert report["share_window"] == "week"
    assert report["window_start"]["day"] == _stamp(NOW - timedelta(hours=24))
    claude, grok, agy = (report["providers"][n] for n in ("claude", "grok", "agy"))
    assert claude["observed"]["week"] == {
        "turns": 6, "telemetry_turns": 5, "tokens": 10_000, "share_turns": 0.75,
        "share_tokens": 10_000 / 12_000,
    }
    assert claude["observed"]["day"]["turns"] == 1
    assert grok["observed"]["day"]["share_turns"] == 2 / 3
    # Paused providers are excluded from share denominators and reported as paused.
    assert agy["observed"]["week"]["turns"] == 2
    assert agy["observed"]["week"]["share_turns"] is None
    assert (agy["state"], agy["share_state"]) == ("paused", "paused")
    # Targets normalize over enabled providers with a target: 50/80 and 30/80.
    assert claude["target_share_normalized"] == pytest.approx(0.625)
    assert (claude["share_state"], grok["share_state"]) == ("over_target", "under_target")
    assert report["under_target_order"] == ["grok", "claude"]
    # Budgets: grok's day turns budget is exhausted and enforced; claude's week token budget is not.
    assert grok["budgets"]["day"]["turns"] == {"limit": 2, "used": 2, "remaining": 0, "exhausted": True}
    assert grok["budgets"]["day"]["tokens"]["limit"] is None
    assert (grok["state"], grok["enforced_exhaustion"]) == ("budget_exhausted", True)
    assert claude["budgets"]["week"]["tokens"] == {
        "limit": 100_000, "used": 10_000, "remaining": 90_000, "exhausted": False,
    }
    assert (claude["state"], claude["enforced_exhaustion"]) == ("active", False)
    assert "claude-alias" in report["providers"]
    assert report["providers"]["claude-alias"]["share_state"] == "untracked"

    refusal = policy.admission_refusal(loaded, report, "grok")
    assert refusal == {
        "provider": "grok", "window": "day", "kind": "turns", "limit": 2, "used": 2,
        "window_start": report["window_start"]["day"], "policy_revision": 1,
    }
    assert policy.admission_refusal(loaded, report, "claude") is None
    assert policy.admission_refusal(loaded, report, "unknown") is None


def test_advisory_budget_marks_exhaustion_without_enforced_flag(store, profiles) -> None:
    _task(store, "ts_000000000001", "grok")
    _turn(store, "ts_000000000001", "grok", age=timedelta(hours=1), tokens=None)
    doc = policy.defaults(profiles)
    doc.providers["grok"].budgets["week"] = policy.Budget(turns=1)
    loaded = policy.load(store, profiles)  # defaults
    report = policy.status(store, policy.LoadedPolicy(doc, 0, "x", None, None, "defaults"), profiles, NOW)
    grok = report["providers"]["grok"]
    assert (grok["state"], grok["enforced_exhaustion"]) == ("budget_exhausted", False)
    assert policy.admission_refusal(loaded, report, "grok") is None


def test_status_with_no_turns_marks_targets_under_target(store, profiles) -> None:
    doc = policy.defaults(profiles)
    doc.providers["claude"].target_share = 10
    report = policy.status(store, policy.LoadedPolicy(doc, 0, "x", None, None, "defaults"), profiles, NOW)
    assert report["providers"]["claude"]["share_state"] == "under_target"
    assert report["providers"]["grok"]["share_state"] == "untracked"
    assert report["under_target_order"] == ["claude"]


# -- utilization tuning: additive fields, presets, limits, escalation, fill ------------------------


def test_new_fields_at_their_defaults_leave_the_stored_document_and_fingerprint_alone(profiles) -> None:
    """A document that uses no tuning knob is what the first release wrote, byte for byte."""
    document = policy.defaults(profiles)
    stored = json.loads(policy.canonical_json(document))

    assert "escalation" not in stored and "max_concurrent_total" not in stored
    assert all("max_concurrent" not in spec for spec in stored["providers"].values())
    assert all("ladders" not in spec and "fanout" not in spec for spec in stored["roles"].values())
    # Pinned: what ``main`` computed for these same profiles before the tuning fields existed.
    assert policy.fingerprint(document) == "72b7eb19087028929a55ed1a644cea58259fd4bb96e22f39b85367dc6d9d282d"
    # The API still hands readers every key.
    assert document.model_dump(mode="json")["roles"]["mechanic"]["fanout"] == 1


def test_a_set_knob_is_stored_and_changes_the_fingerprint(profiles) -> None:
    document = policy.defaults(profiles)
    before = policy.fingerprint(document)
    document.providers["claude"].max_concurrent = 6
    document.roles["reviewer"].fanout = 2
    document.escalation.enabled = True

    stored = json.loads(policy.canonical_json(document))
    assert stored["providers"]["claude"]["max_concurrent"] == 6
    assert stored["roles"]["reviewer"]["fanout"] == 2
    assert stored["escalation"]["enabled"] is True
    assert policy.fingerprint(document) != before
    assert policy.parse(stored) == document


@pytest.mark.parametrize(
    ("document", "loc"),
    [
        ({"providers": {"claude": {"max_concurrent": 0}}}, ["providers", "claude", "max_concurrent"]),
        ({"providers": {"claude": {"max_concurrent": 17}}}, ["providers", "claude", "max_concurrent"]),
        ({"max_concurrent_total": 49}, ["max_concurrent_total"]),
        ({"roles": {"r": {"fanout": 4}}}, ["roles", "r", "fanout"]),
        (
            {"roles": {"r": {"ladders": {"claude": {"above": [{}] * 4}}}}},
            ["roles", "r", "ladders", "claude", "above"],
        ),
        ({"escalation": {"step_up_points": [12, 5]}}, ["escalation"]),
        ({"escalation": {"release_points": 5}}, ["escalation"]),
        ({"escalation": {"window_hold_percent": 95}}, ["escalation"]),
    ],
)
def test_tuning_shape_errors_are_located(document, loc) -> None:
    with pytest.raises(policy.PolicyError) as caught:
        policy.parse(document)
    assert loc in [error["loc"] for error in caught.value.errors]


def test_ladder_cross_field_rules(profiles) -> None:
    document = policy.defaults(profiles)
    ladders = document.roles["explorer"].ladders
    ladders["claude"] = policy.Ladder(above=[policy.Selection(model="gpt-9", effort="high")],
                                      below=[policy.Selection(effort="low")])
    ladders["claude-alias"] = policy.Ladder(above=[policy.Selection(model="sonnet")])
    ladders["nope"] = policy.Ladder()
    del document.roles["mechanic"].selections["grok"]
    document.roles["mechanic"].ladders["grok"] = policy.Ladder(above=[policy.Selection(model="grok-4.6")])

    found = {(tuple(error["loc"]), error["code"]) for error in policy.validate(document, profiles)}
    assert (("roles", "explorer", "ladders", "claude", "above", 0, "model"), "unadvertised") in found
    assert (("roles", "explorer", "ladders", "claude", "below", 0, "model"), "ladder_step_empty") in found
    assert (("roles", "explorer", "ladders", "claude-alias"), "ladder_metered") in found
    assert (("roles", "explorer", "ladders", "nope"), "unknown_provider") in found
    assert (("roles", "mechanic", "ladders", "grok"), "ladder_without_selection") in found


def test_presets_are_valid_idempotent_and_recognised(profiles) -> None:
    document = policy.defaults(profiles)
    assert policy.preset_matches(document, profiles, "claude") == "custom"
    assert policy.preset_matches(document, profiles, "muse") is None

    for provider in ("claude", "grok", "agy"):
        for level in policy.PRESET_LEVELS:
            once = policy.apply_preset(document, profiles, provider, level)
            assert policy.validate(once, profiles) == []
            assert policy.apply_preset(once, profiles, provider, level) == once
            assert policy.preset_matches(once, profiles, provider) == level
            # A preset for one provider leaves every other provider's fields alone.
            for other in ("claude", "grok", "agy"):
                if other != provider:
                    assert once.providers[other] == document.providers[other]
                    assert all(
                        once.roles[r].selections.get(other) == document.roles[r].selections.get(other)
                        for r in document.roles
                    )

    balanced = policy.apply_preset(document, profiles, "claude", "balanced")
    assert all(balanced.roles[r].selections["claude"] == document.roles[r].selections["claude"]
               for r in document.roles)
    assert balanced.providers["claude"].max_concurrent == 4
    conserve = policy.apply_preset(document, profiles, "grok", "conserve")
    assert all(not ladder.above for role in conserve.roles.values()
               for name, ladder in role.ladders.items() if name == "grok")
    hand_edited = balanced.model_copy(deep=True)
    hand_edited.providers["claude"].max_concurrent = 5
    assert policy.preset_matches(hand_edited, profiles, "claude") == "custom"


def test_presets_refuse_what_they_cannot_tune(profiles) -> None:
    document = policy.defaults(profiles)
    for provider, level, code in (
        ("claude", "turbo", "unknown_preset"), ("nope", "max", "unknown_provider"),
        ("muse", "max", "no_preset"), ("claude-alias", "max", "no_preset"),
    ):
        with pytest.raises(policy.PolicyError) as caught:
            policy.apply_preset(document, profiles, provider, level)
        assert caught.value.errors[0]["code"] == code
    patches = policy.preset_table(document, profiles)
    assert set(patches["claude"]) == set(policy.PRESET_LEVELS)
    assert patches["claude-alias"] == {} and patches["muse"] == {}
    assert patches["grok"]["max"][0] == {"path": ["providers", "grok", "max_concurrent"], "value": 8}


def test_effective_limits_cap_the_policy_but_not_the_file(profiles) -> None:
    document = policy.defaults(profiles)
    document.providers["claude"].max_concurrent = 12
    document.providers["grok"].max_concurrent = 2
    document.max_concurrent_total = 20

    limits = policy.effective_limits(
        document, {"claude": 4, "grok": 4, "agy": 10}, per_provider_max=8, total_max=12,
    )
    assert limits["providers"]["claude"] == {"limit": 8, "source": "policy", "ceiling": 8}
    assert limits["providers"]["grok"] == {"limit": 2, "source": "policy", "ceiling": 8}
    # The file's own value is the operator's and is used as written.
    assert limits["providers"]["agy"] == {"limit": 10, "source": "config", "ceiling": 8}
    assert limits["total"] == {"limit": 12, "source": "config", "ceiling": 12}
    document.max_concurrent_total = 6
    assert policy.effective_limits(document, {}, per_provider_max=8, total_max=12)["total"]["limit"] == 6
    unset = policy.effective_limits(policy.defaults(profiles), {}, per_provider_max=8)
    assert unset["total"]["limit"] is None


def _shares(store: Store, *, claude: int, grok: int) -> None:
    for index in range(claude):
        task_id = f"ts_c{index:011d}"
        _task(store, task_id, "claude")
        _turn(store, task_id, "claude", age=timedelta(hours=1), tokens=10)
    for index in range(grok):
        task_id = f"ts_g{index:011d}"
        _task(store, task_id, "grok")
        _turn(store, task_id, "grok", age=timedelta(hours=1), tokens=10)


def _escalating(profiles, *, claude: int = 50, grok: int = 50) -> policy.LoadedPolicy:
    document = policy.defaults(profiles)
    for name in document.providers:
        document.providers[name].enabled = name in {"claude", "grok"}
    document.providers["claude"].target_share = claude
    document.providers["grok"].target_share = grok
    document.escalation.enabled = True
    document = policy.apply_preset(document, profiles, "claude", "balanced")
    return policy.LoadedPolicy(document, 7, policy.fingerprint(document), None, None, "store")


def _level(store: Store, loaded: policy.LoadedPolicy, profiles, provider: str) -> dict:
    return policy.status(store, loaded, profiles, NOW)["providers"][provider]["escalation"]


def test_escalation_is_off_by_default_and_needs_a_sample(store, profiles) -> None:
    _shares(store, claude=1, grok=19)
    plain = policy.LoadedPolicy(policy.defaults(profiles), 0, "f", None, None, "defaults")
    assert _level(store, plain, profiles, "claude") | {"signal": None} == {
        "level": 0, "previous_level": 0, "reason": "escalation is off", "signal": None,
    }
    loaded = _escalating(profiles)
    loaded.policy.escalation.min_turns = 50
    assert _level(store, loaded, profiles, "claude")["level"] == 0
    assert "below the 50 needed" in _level(store, loaded, profiles, "claude")["reason"]


def test_escalation_steps_with_the_share_deficit_and_explains_itself(store, profiles) -> None:
    _shares(store, claude=4, grok=16)  # claude at 20% of a 50% target: 30 points under
    loaded = _escalating(profiles)

    claude = _level(store, loaded, profiles, "claude")
    assert claude["level"] == 2 and claude["reason"] == "step +2: 30 points under target"
    assert claude["signal"]["deficit_points"] == 30 and claude["signal"]["sample_turns"] == 20
    grok = _level(store, loaded, profiles, "grok")
    assert grok["level"] == -2 and grok["reason"] == "step -2: 30 points over target"

    report = policy.status(store, loaded, profiles, NOW)
    assert report["under_target"] == [
        {"provider": "claude", "deficit": 0.3}, {"provider": "grok", "deficit": -0.3},
    ]
    assert report["providers"]["claude"]["share_deficit"] == 0.3
    # A ladder shorter than the level stops at its last step.
    assert report["roles"]["explorer"]["effective_selections"]["claude"] == {
        "model": "opus[1m]", "effort": "high", "level": 1,
    }
    assert report["roles"]["planner"]["effective_selections"]["claude"]["level"] == 0


def test_escalation_holds_its_level_inside_the_release_margin(store, profiles) -> None:
    """At 4 points under, the level is 0 coming up and 1 going down: it cannot flap at 5."""
    _shares(store, claude=46, grok=54)
    loaded = _escalating(profiles)
    assert _level(store, loaded, profiles, "claude")["level"] == 0

    holder = _task(store, "ts_h00000000001", "claude")
    store.set_task_dispatch(holder.id, "claude", selection_source="policy", ladder_level=1)
    store._conn.execute("UPDATE task_dispatch SET created_at = ?", (_stamp(NOW - timedelta(hours=2)),))
    held = _level(store, loaded, profiles, "claude")
    assert (held["level"], held["previous_level"]) == (1, 1)

    loaded.policy.escalation.release_points = 0
    assert _level(store, loaded, profiles, "claude")["level"] == 0


def test_escalation_brakes_only_ever_lower_the_level(store, profiles) -> None:
    _shares(store, claude=4, grok=16)
    loaded = _escalating(profiles)
    loaded.policy.providers["claude"].budgets = {"week": policy.Budget(turns=5)}
    held = _level(store, loaded, profiles, "claude")
    assert held["level"] == 0 and held["reason"] == "held at 0: a budget is 80% used"

    loaded.policy.providers["claude"].budgets = {"week": policy.Budget(turns=4)}
    down = _level(store, loaded, profiles, "claude")
    assert down["level"] == -1 and "a budget is 100% used" in down["reason"]

    loaded.policy.providers["claude"].budgets = {}
    store.set_provider_status("claude", state="throttled", code="PROVIDER_THROTTLED", source="test",
                              observed_at=_stamp(NOW))
    throttled = _level(store, loaded, profiles, "claude")
    assert throttled["level"] == 0 and throttled["reason"] == "held at 0: provider is throttled"
    # A brake never raises a level that was already below it.
    assert _level(store, loaded, profiles, "grok")["level"] == -2


def test_escalation_never_steps_a_second_class_profile(store, profiles) -> None:
    _shares(store, claude=4, grok=16)
    loaded = _escalating(profiles)
    loaded.policy.providers["claude-alias"].enabled = True
    loaded.policy.providers["claude-alias"].target_share = 0
    alias = _level(store, loaded, profiles, "claude-alias")
    assert alias["level"] == 0 and "built-in subscription profile" in alias["reason"]


@pytest.mark.parametrize(
    ("role", "model", "effort", "expected"),
    [
        # nothing sent: the role's current step, as a pair
        ("explorer", None, None, ("opus[1m]", "high", "policy", 1)),
        ("planner", None, None, ("opus[1m]", "xhigh", "policy", 0)),
        # both sent: untouched
        ("explorer", "haiku", "low", ("haiku", "low", "caller", None)),
        # model only: the step's effort belongs to the step's model
        ("explorer", "opus[1m]", None, ("opus[1m]", "high", "mixed", 1)),
        ("explorer", "sonnet", None, ("sonnet", None, "caller", None)),
        # effort only: the step's model, unless it takes no effort
        ("explorer", None, "medium", ("opus[1m]", "medium", "mixed", 1)),
        ("explorer", None, "turbo", (None, "turbo", "caller", None)),
        # no role, or one the policy does not have
        (None, None, None, (None, None, "profile", None)),
        ("stranger", None, None, (None, None, "profile", None)),
    ],
)
def test_resolve_selection_fills_only_what_the_caller_left_out(
    store, profiles, role, model, effort, expected
) -> None:
    _shares(store, claude=4, grok=16)
    loaded = _escalating(profiles)
    report = policy.status(store, loaded, profiles, NOW)

    out = policy.resolve_selection(loaded, report, profiles["claude"], role=role, model=model, effort=effort)
    assert (out.model, out.effort, out.source, out.ladder_level) == expected
    assert (out.caller_model, out.caller_effort, out.policy_revision) == (model, effort, 7)


def test_resolve_selection_skips_models_without_effort_and_unsteerable_profiles(store, profiles) -> None:
    loaded = _escalating(profiles)
    report = policy.status(store, loaded, profiles, NOW)

    claude = profiles["claude"]
    mechanic = policy.resolve_selection(loaded, report, claude, role="mechanic", model=None, effort=None)
    assert (mechanic.model, mechanic.effort, mechanic.source) == ("haiku", None, "policy")
    # haiku takes no effort, so an effort sent alone is not paired with it.
    alone = policy.resolve_selection(loaded, report, claude, role="mechanic", model=None, effort="low")
    assert (alone.model, alone.effort, alone.source) == (None, "low", "caller")

    alias = policy.resolve_selection(
        loaded, report, profiles["claude-alias"], role="explorer", model=None, effort=None
    )
    assert alias.source == "profile" and "built-in subscription profile" in alias.reason
    assert alias.describe()["ladder_level"] is None
