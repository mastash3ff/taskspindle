"""Grok CLI launch flags alone do not establish a session's selected effort."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from taskspindle import runner
from taskspindle.acp_client import AcpError, AcpWorker, PermissionPolicy
from taskspindle.models import Mode, TurnKind
from tests.test_runner import AGENT_ARGV, profile_for
from tests.test_store import make_store, make_task


@pytest.mark.parametrize("kind", [TurnKind.INITIAL, TurnKind.CONTINUE, TurnKind.RESUME])
@pytest.mark.parametrize("selection", ["model", "effort", "both", "omitted"])
@pytest.mark.parametrize("confirmation", ["confirmed", "refused", "unsupported"])
async def test_grok_explicit_selection_is_confirmed_after_open(tmp_path, kind, selection, confirmation):
    requested_model = "grok-4.6" if selection in {"model", "both"} else None
    requested_effort = "low" if selection in {"effort", "both"} else None
    script = tmp_path / "agent.json"
    script.write_text(
        json.dumps(
            {
                "load_session": True,
                # An omitted task selection must not configure even a profile with defaults.
                "config_fail": selection == "omitted",
                "unconfirmed_config": confirmation == "refused",
                "config_options": []
                if confirmation == "unsupported"
                else [
                    {
                        "id": "model",
                        "name": "Model",
                        "type": "select",
                        "currentValue": "grok-4.5",
                        "options": [
                            {"value": "grok-4.5", "name": "Grok 4.5"},
                            {"value": "grok-4.6", "name": "Grok 4.6"},
                        ],
                    },
                    {
                        "id": "reasoning_effort",
                        "name": "Reasoning Effort",
                        "type": "select",
                        "currentValue": "high",
                        "options": [{"value": "high", "name": "High"}, {"value": "low", "name": "Low"}],
                    },
                ],
            }
        )
    )
    profile = replace(profile_for(script), id="grok", first_class=True, model="grok-4.6", effort="medium")
    with make_store(tmp_path) as store:
        task = make_task(store, provider="grok")
        task = store.update_task(
            task.id,
            None,
            requested_model=requested_model,
            requested_effort=requested_effort,
            mode=Mode.CONSULT,
            session_id=None if kind is TurnKind.INITIAL else "original-session",
        )
        logs = []
        run = SimpleNamespace(
            task=task,
            task_id=task.id,
            store=store,
            kind=kind,
            session_id=None,
            log=SimpleNamespace(write=logs.append),
        )
        run.profile = runner._resolve_profile(run, {"grok": profile})
        async with AcpWorker(
            command=AGENT_ARGV,
            env=dict(profile.env),
            cwd=tmp_path,
            stderr_path=tmp_path / "stderr",
            policy=PermissionPolicy(allow_writes=False),
        ) as agent:
            if selection != "omitted" and confirmation != "confirmed":
                with pytest.raises(AcpError) as caught:
                    await runner._open_session(run, agent)
                assert caught.value.code == "CONFIG_UNAVAILABLE"
                return
            await runner._open_session(run, agent)
            if confirmation != "unsupported":
                assert agent.session_config_options[0]["currentValue"] == (requested_model or "grok-4.5")
                assert agent.session_config_options[1]["currentValue"] == (requested_effort or "high")
            assert store.get_task(task.id).requested_model == requested_model
            assert store.get_task(task.id).requested_effort == requested_effort
            if requested_model:
                assert f"session model option {requested_model}" in logs
            if requested_effort:
                assert f"session reasoning_effort {requested_effort}" in logs
            if selection == "omitted":
                assert logs == []
            if kind is not TurnKind.INITIAL:
                assert run.session_id == "original-session"
