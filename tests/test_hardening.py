"""The read-only hardening: Grok's sandbox, Claude's session modes, and the mode-switch refusal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from taskspindle import providers, runner
from taskspindle.acp_client import PermissionPolicy
from taskspindle.config import Paths
from taskspindle.models import EventKind, Mode, TaskState, TurnKind
from taskspindle.providers import Profile
from taskspindle.store import Store
from tests.test_runner import AGENT_ARGV, REPO_ROOT, seed_subject, seed_task


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


@pytest.fixture
def store(paths: Paths):
    store = Store.open(paths.state_dir / "taskspindle.sqlite3")
    yield store
    store.close()


@pytest.fixture
def script(tmp_path: Path):
    written: list[Path] = []

    def _write(body: dict[str, object]) -> Path:
        path = tmp_path / f"script-{len(written)}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        written.append(path)
        return path

    return _write


def test_grok_is_sandboxed_only_for_turns_that_must_not_write(tmp_path: Path) -> None:
    grok = providers.builtin_profiles(tmp_path, home=tmp_path, state_dir=tmp_path)["grok"]

    review = providers.launch_command(grok, "review")
    assert review[:4] == ("grok", "--no-subagents", "--sandbox", "read-only")
    assert review[4:] == grok.command[2:]
    assert providers.launch_command(grok, "consult") == review
    assert providers.launch_command(grok, "implement") == grok.command

    derived = Profile(id="grok-fast", auth="oauth", command=grok.command, base="grok")
    assert providers.launch_command(derived, "review")[2:4] == ("--sandbox", "read-only")
    other = Profile(id="opencode", auth="api_key", command=("opencode", "acp"))
    assert providers.launch_command(other, "review") == ("opencode", "acp")


def test_claude_sessions_get_a_mode_per_task_mode(tmp_path: Path) -> None:
    claude = providers.builtin_profiles(tmp_path, home=tmp_path, state_dir=tmp_path)["claude"]
    assert providers.session_mode(claude, "review") == "plan"
    assert providers.session_mode(claude, "consult") == "plan"
    assert providers.session_mode(claude, "implement") == "default"
    grok = Profile(id="grok", auth="oauth", command=("grok",))
    assert providers.session_mode(grok, "review") is None


def test_a_request_to_switch_mode_is_refused_whatever_the_task_may_do() -> None:
    options = [
        PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
        PermissionOption(option_id="reject-once", name="Reject once", kind="reject_once"),
    ]
    call = ToolCallUpdate(tool_call_id="tc-1", title="Ready to code?", kind="switch_mode")
    for allow_writes in (False, True):
        option_id, violation = PermissionPolicy(allow_writes=allow_writes).select(call, options)
        assert option_id == "reject-once"
        assert violation == "MODE_SWITCH_ATTEMPT"


def claude_like(script_path: Path) -> Profile:
    """The fake agent presented as a Claude-family profile, so the session mode applies to it."""
    return Profile(
        id="fake",
        auth="oauth",
        command=AGENT_ARGV,
        env={"PYTHONPATH": str(REPO_ROOT), "TASKSPINDLE_FAKE_SCRIPT": str(script_path)},
        base="claude",
    )


async def run_as_claude(store, paths, task, script_path: Path) -> TaskState:
    return await runner.run_worker(
        store,
        task.id,
        profiles={task.provider: claude_like(script_path)},
        paths=paths,
        boot="boot-under-test",
        signals=False,
    )


async def test_a_claude_review_runs_in_plan_mode_and_an_implement_in_default(
    store, paths, make_repo, script, tmp_path: Path
) -> None:
    subject = seed_subject(store)
    review = seed_task(
        store,
        paths,
        mode=Mode.REVIEW,
        provider_family="claude",
        repo=make_repo(),
        review_target={"kind": "candidate", "task_id": subject.id, "candidate_sha": subject.candidate_sha},
    )
    mode_file = tmp_path / "mode-review.txt"
    verdict = {"verdict": "PASS", "summary": "fine", "findings": [], "checks": []}
    state = await run_as_claude(
        store, paths, review, script({"response": json.dumps(verdict), "capture_mode_to": str(mode_file)})
    )
    assert state is TaskState.COMPLETED
    assert mode_file.read_text() == "plan"

    implement = seed_task(
        store, paths, mode=Mode.IMPLEMENT, repo=make_repo("other"), provider_family="claude",
    )
    mode_file = tmp_path / "mode-implement.txt"
    state = await run_as_claude(
        store,
        paths,
        implement,
        script(
            {
                "response": "did it",
                "write": {"path": "src/new.txt", "content": "hi\n"},
                "capture_mode_to": str(mode_file),
            }
        ),
    )
    assert state is TaskState.RESULT_READY
    assert mode_file.read_text() == "default"


async def test_an_agent_that_refuses_the_mode_fails_the_turn_rather_than_running_unguarded(
    store, paths, script
) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="claude")

    state = await run_as_claude(store, paths, task, script({"response": "never", "refuse_mode": True}))

    assert state is TaskState.FAILED
    assert store.get_task(task.id).error["code"] == "MODE_UNAVAILABLE"


async def test_asking_to_leave_plan_mode_is_a_recorded_violation(store, paths, script) -> None:
    task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="claude")

    state = await run_as_claude(store, paths, task, script({"response": "ok", "ask_switch_mode": True}))

    assert state is TaskState.COMPLETED
    final = store.get_task(task.id)
    assert final.warnings == ["MODE_SWITCH_ATTEMPT"]
    assert EventKind.MODE_SWITCH_ATTEMPT.value in [e["kind"] for e in store.list_events(task.id)]


def test_the_review_prompt_carries_the_diff_and_caps_it() -> None:
    small = runner.review_diff_section(b"--- a\n+++ b\n+hello\n")
    assert "```diff" in small
    assert "+hello" in small
    assert "truncated" not in small

    big = runner.review_diff_section(b"x" * (runner.MAX_REVIEW_DIFF_BYTES + 10))
    assert "diff truncated at" in big
    assert len(big) < runner.MAX_REVIEW_DIFF_BYTES + 400
    assert runner.review_diff_section(None) == ""
    assert runner.review_diff_section(b"") == ""


@pytest.mark.parametrize("mode", [Mode.CONSULT, Mode.IMPLEMENT])
def test_only_reviews_get_a_diff_section(store, paths, make_repo, mode: Mode) -> None:
    task = seed_task(store, paths, mode=mode, repo=make_repo() if mode is Mode.IMPLEMENT else None)

    text = runner.compose_prompt(task, TurnKind.INITIAL, review_diff=b"+never shown\n")
    assert "never shown" not in text
