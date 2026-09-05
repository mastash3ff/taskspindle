"""Pinned AGY controls and persisted model selections, without a Google seat."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from taskspindle import runner, service
from taskspindle.acp_client import TurnCapture, TurnResult
from taskspindle.agy_policy import AgyPermissionPolicy
from taskspindle.models import TurnKind
from taskspindle.providers import Profile
from tests.test_store import make_store, make_task

OPTIONS = [
    PermissionOption(option_id="yes", name="Allow once", kind="allow_once"),
    PermissionOption(option_id="always", name="Always", kind="allow_always"),
    PermissionOption(option_id="no", name="Deny", kind="reject_once"),
]


@pytest.mark.parametrize("kind,title", [
    (None, "Trust this workspace?"), ("other", "Run start_subagent?"),
    ("other", "MCP edit_file"), ("switch_mode", "Use yolo"),
    ("edit", "Run create_file?"), ("execute", "touch file"),
])
def test_readonly_rejects_every_permission(tmp_path, kind, title):
    policy = AgyPermissionPolicy(allow_writes=False, workspace=tmp_path)
    decision, violation = policy.select(
        ToolCallUpdate(tool_call_id="x", title=title, kind=kind, raw_input={"path": "file"}), OPTIONS,
    )
    assert decision == "no"
    assert violation


def test_implement_only_allows_known_worktree_edits_and_verification(tmp_path):
    policy = AgyPermissionPolicy(allow_writes=True, workspace=tmp_path, commands=["pytest -q"])
    def edit(path, title="Run create_file?"):
        return policy.select(ToolCallUpdate(
            tool_call_id="x", title=title, kind="edit", raw_input={"path": str(path)},
        ), OPTIONS)[0]
    assert edit("source.py") == "yes"
    assert edit("../outside.py") == "no"
    assert edit(".git/config") == "no"
    assert edit("source.py", "unknown_edit") == "no"
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    assert edit("link/outside.py") == "no"
    for command, expected in [("pytest -q", "yes"), ("agy do work", "no")]:
        assert policy.select(ToolCallUpdate(
            tool_call_id="x", title=command, kind="execute", raw_input={"command": command},
        ), OPTIONS)[0] == expected


class Agent:
    def __init__(self, models):
        self.session_config_options = [{
            "id": "model", "type": "select", "options": [{"value": m, "name": m} for m in models],
        }]
        self.selections = []

    async def set_config_option(self, session_id, key, value):
        self.selections.append((session_id, key, value))


async def test_runner_persists_selection_and_reuses_it_when_catalog_grows(tmp_path):
    with make_store(tmp_path) as store:
        task = make_task(store, provider="agy")
        run = SimpleNamespace(task=task, task_id=task.id, store=store, session_id="agy-session",
                              kind=TurnKind.INITIAL, profile=None, session_model=None)
        agent = Agent(["gemini-3.8-flash-medium", "gemini-3.8-pro-high"])
        await runner._configure_agy_session(run, agent)
        assert run.task.requested_model is None
        assert run.task.reported_model is None
        assert run.task.resolved_model == "gemini-3.8-flash-medium"
        assert service.task_view(run.task).resolved_effort == "medium"
        assert agent.selections[0] == ("agy-session", "mode", "default")
        run.kind = TurnKind.CONTINUE
        resumed = Agent(["gemini-3.8-flash-medium", "gemini-4-flash-medium"])
        await runner._configure_agy_session(run, resumed)
        assert resumed.selections[-1] == ("agy-session", "model", "gemini-3.8-flash-medium")
        missing = Agent(["gemini-4-flash-medium"])
        with pytest.raises(runner._Failure):
            await runner._configure_agy_session(run, missing)
        assert missing.selections == []


async def test_partial_prompt_result_reaches_failed_task(tmp_path, monkeypatch):
    from tests.test_runner import BOOT, Mode, Paths, profile_for, seed_task

    paths = Paths(tmp_path / "config.toml", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    with make_store(tmp_path) as store:
        task = seed_task(store, paths, mode=Mode.CONSULT)
        script = tmp_path / "agent.json"
        script.write_text('{"response": "partial evidence", "fail_after_response": true}')
        await runner.run_worker(store, task.id, profiles={task.provider: profile_for(script)},
                                paths=paths, boot=BOOT, signals=False)
        result = store.get_task(task.id)
        assert result.state.value == "FAILED"
        assert result.response == "partial evidence"
        assert Path(result.transcript_path).is_file()


@pytest.mark.parametrize("wire_model", [None, "gemini-3.8-flash-backend"])
async def test_agy_usage_does_not_report_selected_models_as_observed(tmp_path, wire_model):
    profile = Profile(
        id="agy", command=("unused",), auth="oauth",
        model="gemini-3.8-flash", first_class=True,
    )
    run = SimpleNamespace(
        session_id="session", session_model="gemini-3.8-flash-medium",
        prompt_started_at=None, prompt_ended_at=None,
    )
    result = TurnResult(
        stop_reason="end_turn", text="done",
        capture=TurnCapture(model_ids=[wire_model] if wire_model else []),
        usage={"input_tokens": 10, "output_tokens": 2},
    )
    await runner._record_usage(run, profile, tmp_path, result)
    assert run.reported_model == wire_model
    assert run.usage.model == wire_model
    assert run.usage.input_tokens == 10
    assert run.usage.output_tokens == 2
    assert run.usage.cost_estimate_usd is None
