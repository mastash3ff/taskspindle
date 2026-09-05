"""Protected ACP configuration must fail closed without losing partial evidence."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.connection import StreamDirection

from taskspindle import runner
from taskspindle.acp_client import MODE_SWITCH_ATTEMPT, AcpError, AcpWorker, PermissionPolicy
from taskspindle.models import Mode, TurnKind
from tests.test_acp_client import running_agent
from tests.test_agy_runtime import Agent
from tests.test_store import make_store, make_task

MODEL = "gemini-3.8-flash-medium"
CONFIG = [
    {
        "id": "mode",
        "name": "Mode",
        "type": "select",
        "currentValue": "yolo",
        "options": [{"value": "default", "name": "Default"}, {"value": "yolo", "name": "Yolo"}],
    },
    {
        "id": "model",
        "name": "Model",
        "type": "select",
        "currentValue": MODEL,
        "options": [{"value": MODEL, "name": "Gemini"}],
    },
]
CHANGES = [
    {"sessionUpdate": "current_mode_update", "currentModeId": "yolo"},
    {
        "sessionUpdate": "config_option_update",
        "configOptions": [
            {"id": "mode", "currentValue": "yolo"},
        ],
    },
    {
        "sessionUpdate": "config_option_update",
        "configOptions": [
            {"id": "model", "currentValue": "gemini-4-flash-medium"},
        ],
    },
    {"sessionUpdate": "config_option_update", "configOptions": 123},
]


def _notify(worker, update):
    worker._observe(
        SimpleNamespace(
            direction=StreamDirection.INCOMING,
            message={
                "method": "session/update",
                "params": {"sessionId": "session", "update": update},
            },
        )
    )


def _worker(tmp_path):
    worker = AcpWorker(
        command=["unused"],
        env={},
        cwd=tmp_path,
        stderr_path=tmp_path / "unused.stderr",
        policy=PermissionPolicy(allow_writes=False),
    )
    worker._expected_config = {"mode": "default", "model": MODEL}
    return worker


async def test_model_selection_cannot_reset_previously_verified_mode(tmp_path):
    async with running_agent(
        tmp_path,
        {
            "config_options": CONFIG,
            "reset_config_on_set": {"model": {"mode": "yolo"}},
        },
    ) as worker:
        session = await worker.new_session()
        await worker.set_config_option(session, "mode", "default")
        with pytest.raises(AcpError) as caught:
            await worker.set_config_option(session, "model", MODEL)
        assert caught.value.code == "CONFIG_UNAVAILABLE"
        assert worker.last_result.capture.violations == [MODE_SWITCH_ATTEMPT]


@pytest.mark.parametrize("update", CHANGES)
async def test_forbidden_update_aborts_even_when_prompt_response_is_already_available(tmp_path, update):
    worker = _worker(tmp_path)
    terminated = []
    worker._process = SimpleNamespace(terminate=lambda: terminated.append(True))

    async def prompt(**kwargs):
        _notify(
            worker,
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "partial evidence"}},
        )
        _notify(worker, update)
        return SimpleNamespace(stop_reason="end_turn", usage=None)

    worker._conn = SimpleNamespace(prompt=prompt)
    with pytest.raises(AcpError) as caught:
        await worker.prompt("session", "continue", timeout=1)
    assert caught.value.code == MODE_SWITCH_ATTEMPT
    assert terminated == [True]
    assert worker.last_result.text == "partial evidence"
    assert worker.last_result.stop_reason == "error"
    assert worker.last_result.capture.violations == [MODE_SWITCH_ATTEMPT]


async def test_change_between_configuration_and_prompt_prevents_prompt_dispatch(tmp_path):
    worker = _worker(tmp_path)
    _notify(worker, CHANGES[0])
    with pytest.raises(AcpError) as caught:
        await worker.prompt("session", "continue", timeout=1)
    assert caught.value.code == MODE_SWITCH_ATTEMPT
    assert worker.last_result.capture.violations == [MODE_SWITCH_ATTEMPT]


async def test_authorized_configuration_notification_is_not_a_violation(tmp_path):
    worker = _worker(tmp_path)

    async def configure(**kwargs):
        _notify(
            worker,
            {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {"id": "model", "currentValue": "gemini-4-flash-medium"},
                ],
            },
        )
        return SimpleNamespace(
            config_options=[
                {"id": "mode", "currentValue": "default"},
                {"id": "model", "currentValue": "gemini-4-flash-medium"},
            ]
        )

    worker._conn = SimpleNamespace(set_config_option=configure)
    await worker.set_config_option("session", "model", "gemini-4-flash-medium")
    assert worker.session_config_options[-1]["currentValue"] == "gemini-4-flash-medium"


@pytest.mark.parametrize("update", CHANGES[:3])
async def test_forbidden_update_terminates_real_stdio_agent_and_preserves_partial_result(tmp_path, update):
    async with running_agent(
        tmp_path,
        {
            "config_options": CONFIG,
            "early_text": "saved before abort",
            "prompt_updates": [update],
            "block_seconds": 30,
        },
    ) as worker:
        session = await worker.new_session()
        await worker.set_config_option(session, "mode", "default")
        await worker.set_config_option(session, "model", MODEL)
        with pytest.raises(AcpError) as caught:
            await worker.prompt(session, "go", timeout=2)
        assert caught.value.code == MODE_SWITCH_ATTEMPT
        assert worker.last_result.text == "saved before abort"
        assert worker.last_result.capture.violations == [MODE_SWITCH_ATTEMPT]
        assert session == "fake-session-1"


@pytest.mark.parametrize("update", CHANGES[:3])
async def test_runner_records_configuration_abort_with_partial_response_and_session(
    tmp_path, monkeypatch, update
):
    from tests.test_runner import BOOT, Paths, profile_for, seed_task

    async def configure(run, agent):
        await agent.set_config_option(run.session_id, "mode", "default")
        await agent.set_config_option(run.session_id, "model", MODEL)

    monkeypatch.setattr(runner, "_apply_session_mode", configure)
    paths = Paths(tmp_path / "config.toml", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    with make_store(tmp_path) as store:
        task = seed_task(store, paths, mode=Mode.CONSULT)
        script = tmp_path / "agent.json"
        script.write_text(
            json.dumps(
                {
                    "config_options": CONFIG,
                    "early_text": "saved before abort",
                    "prompt_updates": [update],
                    "block_seconds": 30,
                }
            )
        )
        await runner.run_worker(
            store,
            task.id,
            profiles={task.provider: profile_for(script)},
            paths=paths,
            boot=BOOT,
            signals=False,
        )
        result = store.get_task(task.id)
        assert result.state.value == "FAILED"
        assert result.error["code"] == MODE_SWITCH_ATTEMPT
        assert result.response == "saved before abort"
        assert result.session_id == "fake-session-1"
        assert MODE_SWITCH_ATTEMPT in result.warnings
        assert "saved before abort" in Path(result.transcript_path).read_text()
        assert store.list_turns(task.id)[0]["stop_reason"] == "error"
        assert store.get_lease(task.provider) is None


@pytest.mark.parametrize("kind", [TurnKind.CONTINUE, TurnKind.REPAIR])
async def test_changed_profile_defaults_do_not_replace_persisted_task_choice(tmp_path, kind):
    with make_store(tmp_path) as store:
        task = make_task(store, provider="agy")
        profile = SimpleNamespace(model=MODEL, effort="medium")
        run = SimpleNamespace(
            task=task,
            task_id=task.id,
            store=store,
            session_id="session",
            kind=TurnKind.INITIAL,
            profile=profile,
            session_model=None,
        )
        await runner._configure_agy_session(run, Agent([MODEL]))
        profile.model = "gemini-4-flash-high"
        profile.effort = "high"
        run.kind = kind
        resumed = Agent([MODEL, profile.model])
        await runner._configure_agy_session(run, resumed)
        assert resumed.selections[-1] == ("session", "model", MODEL)
        assert run.task.resolved_effort == "medium"
