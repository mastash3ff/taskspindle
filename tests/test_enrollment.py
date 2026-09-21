"""Dormant routes cannot be enabled by policy, credential method, or evidence claims."""
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from taskspindle import access_checks, enrollment, opencode_go, policy, providers, service
from taskspindle.providers import Profile, ProfileError


@pytest.fixture
def profiles(tmp_path):
    return providers.load_profiles({}, runtime_dir=tmp_path / "runtime", home=tmp_path,
                                   state_dir=tmp_path / "state")


def test_authentication_does_not_determine_billing(profiles):
    for name in ("claude", "grok", "agy"):
        assert profiles[name].auth_method == "oauth"
        assert profiles[name].billing_type == "subscription"
        enrollment.require_promotable(profiles[name])
    unknown = Profile(id="new", auth="oauth", command=("new-cli",))
    with pytest.raises(ProfileError, match="billing is unknown"):
        providers.profile_for_task({"new": unknown}, "new", mode="consult", allow_metered=False)
    assert providers.profile_for_task({"new": unknown}, "new", mode="consult", allow_metered=True) == unknown
    declared = replace(unknown, billing_type="subscription", first_class=True)
    with pytest.raises(ProfileError, match="not qualified"):
        enrollment.require_promotable(declared)


@pytest.mark.parametrize("name", ["opencode-go", "muse"])
@pytest.mark.parametrize("allow_metered", [False, True])
def test_dormant_dispatch_cannot_be_enabled(profiles, name, allow_metered):
    claimed = replace(profiles[name], billing_type="subscription", first_class=True)
    with pytest.raises(ProfileError) as error:
        providers.profile_for_task({name: claimed}, name, mode="consult", allow_metered=allow_metered)
    assert error.value.code.endswith("NOT_QUALIFIED")
    with pytest.raises(ProfileError):
        enrollment.require_promotable(claimed)
    assert policy.defaults(profiles).providers[name].enabled is False


@pytest.mark.parametrize("name", ["opencode-go", "muse"])
def test_disabled_catalog_and_availability_do_not_probe(profiles, name, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No process or network operation is allowed")
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    result = access_checks.check_native_access(profiles[name], {})
    assert result["state"] == "unsupported"
    availability = service.provider_availability(None, profiles[name], now=datetime.now(UTC))
    assert availability["state"] == "unqualified"
    status = enrollment.check(profiles[name], {"enabled": True})
    assert not status["enabled"] and not status["live_enrollment_supported"]
    assert status["missing_evidence"]


def test_go_billing_boundary_is_explicit_and_stays_disabled(profiles):
    result = enrollment.check(profiles["opencode-go"], {
        "credential_provider": "opencode", "zen_use_balance_disabled": False,
    })
    assert len(result["errors"]) == 2
    evidence = {name: {"source": "fixture", "sha256": "a" * 64}
                for name in result["required_evidence"]}
    evidence["zen_use_balance_disabled"]["value"] = True
    result = enrollment.check(profiles["opencode-go"], evidence)
    assert result["missing_evidence"] == []
    assert result["enabled"] is False
    assert result["evidence_status"] == "unverified operator claims"
    opencode_go.validate_selection("opencode-go/grok-4.7", "opencode-go")
    with pytest.raises(ValueError):
        opencode_go.validate_selection("opencode/grok-4.7", "opencode-go")


def test_go_environment_is_credential_free_and_denies_tools(tmp_path):
    import json

    env = opencode_go.isolated_environment(tmp_path)
    assert not tmp_path.joinpath("opencode-home").exists()
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["enabled_providers"] == ["opencode-go"]
    assert config["mcp"] == {} and config["plugin"] == []
    assert config["permission"]["task"] == "deny"
    assert providers.env_violations(env) == []


def test_same_underlying_model_is_not_an_independent_review(profiles):
    via_go = replace(profiles["opencode-go"], model="opencode-go/grok-4.7")
    assert via_go.family != profiles["grok"].family
    assert via_go.underlying_family == profiles["grok"].underlying_family
    assert not providers.reviewer_independent(via_go, profiles["grok"])
    via_agy = replace(profiles["agy"], model="claude-opus-4-6-thinking")
    assert not providers.reviewer_independent(via_agy, profiles["claude"])


def test_enrollment_cli_does_not_load_auth_or_runtime(monkeypatch, capsys):
    import json

    from taskspindle import cli

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: pytest.fail("No binary may run"))
    assert cli.main(["enrollment-check", "opencode-go"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["enabled"] is False
    assert "zen_use_balance_disabled" in result["required_evidence"]


@pytest.mark.parametrize("name", ["muse", "opencode-go"])
async def test_direct_doctor_probe_is_blocked_even_without_first_class(profiles, name, tmp_path, monkeypatch):
    from taskspindle import doctor

    monkeypatch.setattr(doctor, "AcpWorker", lambda *a, **kw: pytest.fail("No binary may start"))
    profile = replace(profiles[name], first_class=False)
    with pytest.raises(ProfileError):
        await doctor.probe_initialize(profile, {}, tmp_path)


def test_handwritten_subscription_cannot_admit_api_key_route(tmp_path):
    profiles = providers.load_profiles({"providers": {"paid": {
        "auth": "api_key", "command": ["arbitrary-cli"], "billing_type": "subscription",
        "secret_env": ["PAID_API_KEY"],
    }}}, runtime_dir=tmp_path / "runtime", home=tmp_path, state_dir=tmp_path / "state")
    assert profiles["paid"].billing_type == "unknown"
    with pytest.raises(ProfileError) as error:
        providers.profile_for_task(profiles, "paid", mode="consult", allow_metered=False)
    assert error.value.code == "METERED_NOT_ALLOWED"
    declared = replace(profiles["paid"], billing_type="subscription")
    with pytest.raises(ProfileError):
        providers.profile_for_task({"paid": declared}, "paid", mode="consult", allow_metered=False)
