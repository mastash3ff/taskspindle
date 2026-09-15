"""The permission gate: default-deny for read-only turns, and delegation by word not by path."""

from __future__ import annotations

import pytest
from acp.schema import PermissionOption, ToolCallUpdate

from taskspindle.acp_client import DELEGATION_ATTEMPT, READ_ONLY_VIOLATION, PermissionPolicy


def _options() -> list[PermissionOption]:
    return [
        PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
        PermissionOption(option_id="reject-once", name="Reject once", kind="reject_once"),
    ]


def _select(policy: PermissionPolicy, title: str, kind: str) -> tuple[str | None, str | None]:
    return policy.select(ToolCallUpdate(tool_call_id="tc-1", title=title, kind=kind), _options())


@pytest.mark.parametrize("kind", ["read", "fetch", "search", "think"])
def test_a_read_only_turn_may_use_the_read_kinds(kind: str) -> None:
    option_id, violation = _select(PermissionPolicy(allow_writes=False), "Look at things", kind)
    assert (option_id, violation) == ("allow-once", None)


@pytest.mark.parametrize("kind", ["edit", "delete", "move", "execute", "other", "", "unknown_kind"])
def test_a_read_only_turn_refuses_every_other_kind(kind: str) -> None:
    option_id, violation = _select(PermissionPolicy(allow_writes=False), "Do something", kind)
    assert (option_id, violation) == ("reject-once", READ_ONLY_VIOLATION)


@pytest.mark.parametrize("kind", ["edit", "execute", "other", "search"])
def test_an_implement_turn_keeps_every_non_delegating_kind(kind: str) -> None:
    option_id, violation = _select(PermissionPolicy(allow_writes=True), "Do something", kind)
    assert (option_id, violation) == ("allow-once", None)


@pytest.mark.parametrize("title", [
    "Edit src/agent.py",
    "Write team_config.yaml",
    "Run pytest tests/test_agent.py",
    "Read docs/team/README.md",
    "Edit ./agent",
    "Grep agent.* in src/",
])
def test_file_names_containing_the_delegation_words_are_ordinary_work(title: str) -> None:
    option_id, violation = _select(PermissionPolicy(allow_writes=True), title, "edit")
    assert (option_id, violation) == ("allow-once", None)


@pytest.mark.parametrize("title", [
    "Delegate to a subagent",
    "Spawn an agent for the tests",
    "Create a team: reviewers",
    "Agent(prompt=...)",
    "Task: review the diff",
])
def test_prose_and_tool_names_that_mean_delegation_are_still_refused(title: str) -> None:
    option_id, violation = _select(PermissionPolicy(allow_writes=True), title, "other")
    assert (option_id, violation) == ("reject-once", DELEGATION_ATTEMPT)
