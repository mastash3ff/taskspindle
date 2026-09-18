"""The native resume handle ``task_status`` and ``task_result`` hand to a human."""

from __future__ import annotations

import pytest

from taskspindle import service
from taskspindle.models import CleanupState
from taskspindle.providers import Profile, resume_command
from tests.test_orchestrator import build_candidate
from tests.test_orchestrator import (
    harness as harness,
)
from tests.test_orchestrator import (
    paths as paths,
)
from tests.test_orchestrator import (
    script as script,
)
from tests.test_orchestrator import (
    store as store,
)
from tests.test_store import make_task


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        ("claude", ("claude", "--resume", "sess-1")),
        ("grok", ("grok", "-r", "sess-1")),
        ("agy", None),
        ("custom", None),
        (None, None),
    ],
)
def test_resume_command_knows_only_the_shell_resumable_families(family, expected) -> None:
    assert resume_command(family, "sess-1") == expected


def test_resume_command_needs_a_session() -> None:
    assert resume_command("claude", "") is None


def test_no_session_means_no_handle(store) -> None:
    record = make_task(store)
    assert record.session_id is None
    assert service.resume_handle(record) is None
    assert service.task_result(store, record.id).resume is None


def test_claude_handle_reopens_from_the_worktree_with_the_profile_config_dir(store) -> None:
    make_task(store)
    record = store.update_task(
        "ts_000000000001", None, session_id="sess-1", worktree_path="/state/worktrees/ts_1"
    )
    profile = Profile(
        id="claude",
        auth="oauth",
        command=("claude-agent-acp",),
        env={"CLAUDE_CONFIG_DIR": "/home/me/.claude"},
    )
    handle = service.resume_handle(record, profile)
    assert handle == {
        "family": "claude",
        "session_id": "sess-1",
        "cwd": "/state/worktrees/ts_1",
        "argv": ["claude", "--resume", "sess-1"],
        "command": "claude --resume sess-1",
        "env": {"CLAUDE_CONFIG_DIR": "/home/me/.claude"},
        "note": None,
    }
    result = service.task_result(store, record.id, profile=profile)
    assert result.session_id == "sess-1"
    assert result.resume == handle


def test_grok_handle_uses_the_scratch_repo_for_a_consult(store) -> None:
    make_task(store, provider="grok")
    record = store.update_task(
        "ts_000000000001", None, session_id="01a0-grok", scratch_repo="/state/scratch/ts_1"
    )
    handle = service.resume_handle(record)
    assert handle["argv"] == ["grok", "-r", "01a0-grok"]
    assert handle["cwd"] == "/state/scratch/ts_1"
    assert handle["env"] == {}
    assert handle["note"] is None


def test_agy_handle_explains_why_there_is_no_command(store) -> None:
    make_task(store, provider="agy")
    record = store.update_task("ts_000000000001", None, session_id="conv-1")
    record = record.model_copy(update={"provider_family": "agy"})
    handle = service.resume_handle(record)
    assert handle["argv"] is None
    assert handle["command"] is None
    assert "cannot be reopened from a shell" in handle["note"]


def test_a_cleaned_up_workspace_is_called_out(store) -> None:
    make_task(store)
    record = store.update_task(
        "ts_000000000001", None, session_id="sess-1", cleanup_state=CleanupState.COMPLETE
    )
    handle = service.resume_handle(record)
    assert handle["argv"] == ["claude", "--resume", "sess-1"]
    assert "cleaned up" in handle["note"]


def test_handle_follows_the_recorded_family_not_the_live_profile(store) -> None:
    make_task(store, provider="alias")
    record = store.update_task("ts_000000000001", None, session_id="sess-1")
    record = record.model_copy(update={"provider_family": "grok"})
    profile = Profile(id="alias", auth="oauth", command=("claude-agent-acp",), base="claude")
    handle = service.resume_handle(record, profile)
    assert handle["family"] == "grok"
    assert handle["argv"] == ["grok", "-r", "sess-1"]


def test_legacy_record_without_family_is_only_resumable_for_reserved_builtins(store) -> None:
    make_task(store, provider="alias")
    record = store.update_task("ts_000000000001", None, session_id="sess-1")
    assert record.provider_family is None
    handle = service.resume_handle(record)
    assert handle["family"] is None
    assert handle["argv"] is None
    assert "raw ACP id" in handle["note"]


def test_status_and_result_carry_the_handle_end_to_end(harness, make_repo) -> None:
    task_id = build_candidate(harness, make_repo())
    o = harness.orchestrator
    status = o.task_status(task_id)
    result = o.task_result(task_id)
    assert status["session_id"] == result["session_id"]
    assert status["resume"] == result["resume"]
    if status["session_id"]:
        assert status["resume"]["argv"] is None  # the fake family has no native resume
        assert status["resume"]["cwd"] == status["worktree_path"]
    else:
        assert status["resume"] is None
