"""The native CLI participates in the same durable task/turn machinery."""

from types import SimpleNamespace

import pytest

from taskspindle import repos, runner
from taskspindle.acp_client import TurnCapture, TurnResult
from taskspindle.models import Mode, TurnKind
from taskspindle.providers import Profile
from tests.test_runner import BOOT, seed_task
from tests.test_store import make_store, make_task


def test_native_selection_is_bound_across_catalog_and_profile_changes(tmp_path):
    with make_store(tmp_path) as store:
        task = make_task(store, provider="agy")
        run = SimpleNamespace(task=task, task_id=task.id, store=store, kind=TurnKind.INITIAL)
        original = Profile(id="agy", auth="oauth", command=("agy",))
        catalog = [("gemini-3.8-flash-medium", "Flash"), ("gemini-3.8-pro-high", "Pro")]
        runner._configure_native_agy(run, original, catalog)
        assert run.task.resolved_model == "gemini-3.8-flash-medium"
        assert run.task.resolved_effort == "medium"
        assert run.task.requested_model is None
        run.kind = TurnKind.CONTINUE
        changed = Profile(id="agy", auth="oauth", command=("agy",), model="gemini-4-flash-high")
        runner._configure_native_agy(run, changed, [*catalog, (changed.model, "New")])
        assert run.task.resolved_model == "gemini-3.8-flash-medium"


async def test_native_runner_records_real_session_model_selection_and_usage(tmp_path, monkeypatch):
    from taskspindle import agy_cli_policy
    from taskspindle.config import Paths

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    calls = []

    async def catalog(*args):
        return [("gemini-3.8-flash-medium", "Flash")]

    class Worker:
        last_result = None

        def __init__(self, **kwargs):
            self.remember = kwargs["on_session"]
            self.command = kwargs["command"]
            self.cwd = kwargs["cwd"]
            calls.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def prompt(self, session_id, prompt, **kwargs):
            assert session_id is None
            assert kwargs["model"] == "gemini-3.8-flash-medium"
            assert kwargs["effort"] == "medium"
            self.remember("native-session")
            self.last_result = TurnResult(
                stop_reason="end_turn", text="native response", capture=TurnCapture(),
                usage={"input_tokens": 12, "output_tokens": 3,
                       "_agy_cli_cumulative": {"input_tokens": 12, "output_tokens": 3}},
            )
            return self.last_result

    monkeypatch.setattr(runner, "cli_model_catalog", catalog)
    monkeypatch.setattr(runner, "AgyCliWorker", Worker)
    monkeypatch.setattr(agy_cli_policy, "prepare_launch", lambda **kwargs: ("native-worker",))
    with make_store(tmp_path) as store:
        task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="agy")
        profile = Profile(id=task.provider, base="agy", auth="oauth", command=("/pinned/agy",))
        await runner.run_worker(store, task.id, profiles={task.provider: profile},
                                paths=paths, boot=BOOT, signals=False)
        final = store.get_task(task.id)
        assert final.state.value == "COMPLETED", final.error
        assert final.session_id == "native-session"
        assert final.resolved_model == "gemini-3.8-flash-medium"
        assert final.reported_model is None
        assert final.oauth_evidence["source"] == "authenticated_model_catalog"
        turn = store.list_turns(task.id)[0]
        usage = store.get_turn_usage(turn["id"])
        assert usage["input_tokens"] == 12
        assert usage["model"] is None
        assert usage["source"] == "agy_cli_result"
        assert usage["raw"]["_agy_cli_cumulative"]["output_tokens"] == 3
        assert calls[0]["prior_usage"] is None


@pytest.mark.parametrize("relative", ["src/new/AGENTS.md", "src/new/.agents/hooks.json"])
async def test_native_controls_in_new_directories_cannot_become_candidates(make_repo, monkeypatch, relative):
    repo = make_repo()
    before = repos.current_head(repo)
    target = repo / relative
    target.parent.mkdir(parents=True)
    target.write_text("protected control")
    warnings = []
    run = SimpleNamespace(
        task=SimpleNamespace(provider_family="agy", base_head=before, candidate_revision=0),
        warn=lambda *args: warnings.append(args),
    )

    def collapse(*args, **kwargs):
        pytest.fail("Protected control must be rejected before creating a candidate")

    monkeypatch.setattr(runner.worktrees, "collapse_candidate", collapse)
    with pytest.raises(runner._Failure) as caught:
        await runner._finalize_implement(run, repo, TurnResult("end_turn", "done", TurnCapture()))
    assert caught.value.code == "AGY_CONTROL_PATH_VIOLATION"
    assert warnings[0][2]["paths"] == [relative]
    assert repos.current_head(repo) == before
