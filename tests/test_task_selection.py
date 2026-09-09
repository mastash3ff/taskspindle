"""Saved task choices reach provider launch and resumed ACP sessions."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from taskspindle import providers, runner
from taskspindle.acp_client import AcpError, AcpWorker, PermissionPolicy
from taskspindle.models import Mode, TurnKind
from tests.test_runner import AGENT_ARGV, profile_for
from tests.test_store import make_store, make_task


@pytest.mark.parametrize("provider", ["claude", "grok"])
@pytest.mark.parametrize(
    "model,effort", [(None, None), ("selected-model", None), (None, "high"), ("selected-model", "high")]
)
def test_saved_builtin_choices_reach_launch_without_changing_defaults(tmp_path, provider, model, effort):
    profiles = providers.builtin_profiles(tmp_path, home=tmp_path, state_dir=tmp_path)
    original = profiles[provider]
    with make_store(tmp_path) as store:
        task = make_task(store, provider=provider)
        task = store.update_task(task.id, None, requested_model=model, requested_effort=effort)
        run = SimpleNamespace(task=task)
        selected = runner._resolve_profile(run, profiles)
        expected_model = model or ("grok-4.6" if provider == "grok" else None)
        expected_effort = effort or ("medium" if provider == "grok" else None)
        assert (selected.model, selected.effort) == (expected_model, expected_effort)
        assert profiles[provider] == original
        assert selected.env == original.env
        assert selected.auth == "oauth"
        if provider == "grok":
            for mode in ("consult", "review", "implement"):
                sandbox = () if mode == "implement" else ("--sandbox", "read-only")
                assert providers.launch_command(selected, mode) == (
                    "grok",
                    "--no-subagents",
                    *sandbox,
                    "agent",
                    "--model",
                    expected_model,
                    "--reasoning-effort",
                    expected_effort,
                    "--no-leader",
                    "stdio",
                )
        else:
            options = providers.session_options(selected)["claudeCode"]["options"]
            assert options.get("model") == expected_model
            assert options.get("effort") == expected_effort
            assert options["settingSources"] == []
            assert options["mcpServers"] == {}
            assert options["disallowedTools"] == ["Agent", "Task", "TeamCreate", "SendMessage"]
            assert selected.command == original.command


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_continuation_uses_saved_explicit_choices_after_profile_defaults_change(tmp_path, provider):
    profiles = providers.builtin_profiles(tmp_path, home=tmp_path, state_dir=tmp_path)
    with make_store(tmp_path) as store:
        task = make_task(store, provider=provider)
        store.update_task(task.id, None, requested_model="original-model", requested_effort="high")
        profiles[provider] = replace(profiles[provider], model="new-default", effort="low")
        run = SimpleNamespace(task=store.get_task(task.id), kind=TurnKind.CONTINUE)
        selected = runner._resolve_profile(run, profiles)
        assert (selected.model, selected.effort) == ("original-model", "high")


@pytest.mark.parametrize("family", ["claude", "grok", "agy", "custom"])
def test_configured_profiles_keep_their_command_and_selection(tmp_path, family):
    profile = providers.Profile(
        id="alias", base=family, auth="oauth", command=("custom",), model="configured", effort="high"
    )
    with make_store(tmp_path) as store:
        task = make_task(store, provider="alias")
        task = task.model_copy(update={"provider_family": family})
        assert runner._resolve_profile(SimpleNamespace(task=task), {"alias": profile}) is profile
        assert providers.launch_command(profile, "implement") == ("custom",)


@pytest.mark.parametrize("family", ["claude", "grok"])
def test_custom_profile_task_overrides_fail_instead_of_being_ignored(tmp_path, family):
    profile = providers.Profile(id="alias", base=family, auth="oauth", command=("custom",))
    with make_store(tmp_path) as store:
        task = make_task(store, provider="alias")
        task = task.model_copy(update={"provider_family": family, "requested_effort": "high"})
        with pytest.raises(runner._Failure) as caught:
            runner._resolve_profile(SimpleNamespace(task=task), {"alias": profile})
        assert caught.value.code == "PROFILE_INVALID"


def test_agy_task_selection_stays_with_its_existing_resolver(tmp_path):
    profile = providers.builtin_profiles(tmp_path, home=tmp_path, state_dir=tmp_path)["agy"]
    with make_store(tmp_path) as store:
        task = make_task(store, provider="agy")
        task = task.model_copy(
            update={"provider_family": "agy", "requested_model": "gemini-3.8-pro", "requested_effort": "high"}
        )
        assert runner._resolve_profile(SimpleNamespace(task=task), {"agy": profile}) is profile


@pytest.mark.parametrize("field", ["model", "effort"])
@pytest.mark.parametrize("value", ["", "   "])
def test_empty_explicit_choices_cannot_silently_fall_back(tmp_path, field, value):
    profile = providers.builtin_profiles(tmp_path, home=tmp_path, state_dir=tmp_path)["claude"]
    with pytest.raises(providers.ProfileError) as caught:
        providers.with_task_selection(profile, **{**{"model": None, "effort": None}, field: value})
    assert caught.value.code == "PROFILE_INVALID"


def test_builtin_without_overrides_retains_its_existing_launch_command(tmp_path):
    profile = providers.builtin_profiles(tmp_path, home=tmp_path, state_dir=tmp_path)["grok"]
    profile = replace(profile, command=("isolated-agent",))
    selected = providers.with_task_selection(profile, model=None, effort=None)
    assert selected is profile
    assert providers.launch_command(selected, "implement") == ("isolated-agent",)


@pytest.mark.parametrize(
    "restriction,code",
    [
        ({"modes": frozenset({"consult"})}, "MODE_NOT_ALLOWED"),
        ({"auth": "api_key"}, "METERED_NOT_ALLOWED"),
        ({"base": "grok"}, "PROVIDER_FAMILY_CHANGED"),
    ],
)
def test_task_selection_does_not_bypass_profile_restrictions(tmp_path, restriction, code):
    profile = providers.builtin_profiles(tmp_path, home=tmp_path, state_dir=tmp_path)["claude"]
    with make_store(tmp_path) as store:
        task = make_task(store)
        task = store.update_task(task.id, None, requested_model="selected", requested_effort="high")
        with pytest.raises(runner._Failure) as caught:
            runner._resolve_profile(SimpleNamespace(task=task), {"claude": replace(profile, **restriction)})
        assert caught.value.code == code


@pytest.mark.parametrize("kind", [TurnKind.INITIAL, TurnKind.CONTINUE, TurnKind.RESUME])
@pytest.mark.parametrize("confirmation", ["confirmed", "refused", "unsupported"])
@pytest.mark.parametrize("selection", ["model", "effort", "both"])
async def test_claude_selection_is_confirmed_after_new_or_load(tmp_path, kind, confirmation, selection):
    """Conflicting adapter settings must not replace the task's explicit model or effort."""
    script = tmp_path / "agent.json"
    metadata = tmp_path / "metadata.json"
    script.write_text(
        json.dumps(
            {
                "load_session": True,
                "capture_meta_to": str(metadata),
                "unconfirmed_config": confirmation == "refused",
                "reset_config_on_set": {"model": {"effort": "low"}},
                "config_options": []
                if confirmation == "unsupported"
                else [
                    {
                        "id": "effort",
                        "name": "Effort",
                        "type": "select",
                        "currentValue": "low",
                        "options": [{"value": "low", "name": "Low"}, {"value": "high", "name": "High"}],
                    },
                    {
                        "id": "model",
                        "name": "Model",
                        "type": "select",
                        "currentValue": "claude-saved-model",
                        "options": [
                            {"value": "claude-saved-model", "name": "Saved"},
                            {"value": "claude-selected-model", "name": "Selected"},
                        ],
                    },
                ],
            }
        )
    )
    profile = replace(profile_for(script), id="claude", base="claude", first_class=True)
    with make_store(tmp_path) as store:
        task = make_task(store)
        task = store.update_task(
            task.id,
            None,
            requested_model="claude-selected-model" if selection in {"model", "both"} else None,
            requested_effort="high" if selection in {"effort", "both"} else None,
            mode=Mode.CONSULT,
            session_id=None if kind is TurnKind.INITIAL else "saved-session",
        )
        run = SimpleNamespace(
            task=task,
            task_id=task.id,
            store=store,
            kind=kind,
            session_id=None,
            log=SimpleNamespace(write=lambda text: None),
        )
        run.profile = runner._resolve_profile(run, {"claude": profile})
        async with AcpWorker(
            command=AGENT_ARGV,
            env=dict(profile.env),
            cwd=tmp_path,
            stderr_path=tmp_path / "stderr",
            policy=PermissionPolicy(allow_writes=False),
        ) as agent:
            if confirmation == "confirmed":
                await runner._open_session(run, agent)
                expected_effort = "low" if selection == "model" else "high"
                expected_model = "claude-saved-model" if selection == "effort" else "claude-selected-model"
                assert agent.session_config_options[0]["currentValue"] == expected_effort
                assert agent.session_config_options[1]["currentValue"] == expected_model
                assert agent.session_model == expected_model
                assert run.session_model == expected_model
                assert agent.mode == "plan"
                assert store.get_task(task.id).requested_effort == task.requested_effort
                if kind is TurnKind.INITIAL:
                    options = json.loads(metadata.read_text())["claudeCode"]["options"]
                    assert options.get("model") == task.requested_model
                    assert options.get("effort") == task.requested_effort
                    assert options["disallowedTools"] == ["Agent", "Task", "TeamCreate", "SendMessage"]
                else:
                    assert run.session_id == "saved-session"
            else:
                with pytest.raises(AcpError) as caught:
                    await runner._open_session(run, agent)
                assert caught.value.code == "CONFIG_UNAVAILABLE"
