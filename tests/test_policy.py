"""Dispatch policy: defaults, validation, canonical form, loading and observed status."""

from __future__ import annotations

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
    assert set(default.providers) == {"claude", "grok", "agy", "claude-alias"}
    assert default.providers["claude"].advertised_models == ["haiku", "sonnet", "opus[1m]"]
    assert default.providers["claude"].models_without_effort == ["haiku"]
    assert default.providers["claude-alias"].advertised_models == ["haiku", "sonnet", "opus[1m]"]
    assert default.providers["agy"].advertised_efforts == ["low", "medium", "high"]
    assert list(default.roles) == ["mechanic", "explorer", "implementer", "planner", "debugger", "reviewer"]
    planner = default.roles["planner"]
    assert planner.provider_preference == ["claude", "grok", "agy"]
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
    assert (("roles", "explorer", "selections", "agy"), "AGY_MODEL_INVALID") in codes
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
