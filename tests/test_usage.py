"""Token usage: the parsers, the price table, the session-file fallback and the rollup."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskspindle import usage
from taskspindle.models import AuthMode, CheckRecord, Mode, StartTaskRequest, TaskState
from taskspindle.providers import Profile
from taskspindle.service import create_task
from taskspindle.store import Store

NOW = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)


def test_prompt_response_usage_is_priced_for_a_known_model() -> None:
    turn = usage.from_prompt_response(
        {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cached_read_tokens": 10_000,
            "cached_write_tokens": 2000,
        },
        model="claude-opus-5",
        duration_ms=1234,
    )
    assert turn.source == "acp_prompt_response"
    # 1000 * 5 + 500 * 25 + 10000 * 0.5 + 2000 * 6.25, per million.
    assert turn.cost_estimate_usd == pytest.approx((5000 + 12500 + 5000 + 12500) / 1_000_000)
    assert turn.cost_is_estimate is True
    assert turn.price_table_version == usage.PRICE_TABLE_VERSION

    unknown = usage.from_prompt_response({"input_tokens": 1}, model="mystery-9", duration_ms=None)
    assert unknown.cost_estimate_usd is None
    assert unknown.price_table_version is None


def test_turn_completed_keeps_grok_cost_ticks_raw() -> None:
    turn = usage.from_turn_completed(
        {
            "usage": {
                "inputTokens": 16708,
                "outputTokens": 165,
                "cachedReadTokens": 5888,
                "reasoningTokens": 160,
                "modelCalls": 1,
                "apiDurationMs": 5197,
                "costUsdTicks": 43475800,
                "modelUsage": {"grok-4.6-build": {"inputTokens": 16708}},
            }
        },
        model=None,
        duration_ms=None,
    )
    assert turn is not None
    assert turn.model == "grok-4.6-build"
    assert turn.duration_ms == 5197
    assert turn.cost_estimate_usd is None
    assert turn.raw["costUsdTicks"] == 43475800
    assert usage.from_turn_completed({"elapsed_ms": 3}, model=None, duration_ms=None) is None


def test_the_claude_session_file_is_found_and_summed_once_per_request(tmp_path: Path) -> None:
    cwd = Path("/home/x/.local/state/taskspindle/worktrees/ts_abc")
    path = usage.claude_session_file(tmp_path, cwd, "sess-1")
    encoded = "-home-x--local-state-taskspindle-worktrees-ts-abc"
    assert path == tmp_path / "projects" / encoded / "sess-1.jsonl"

    path.parent.mkdir(parents=True)
    record = {
        "type": "assistant",
        "timestamp": "2030-01-02T12:00:01Z",
        "requestId": "req-1",
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 50,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 40,
            },
        },
    }
    older = {**record, "timestamp": "2030-01-01T00:00:00Z", "requestId": "req-0"}
    path.write_text("\n".join(json.dumps(item) for item in (older, record, record)) + "\n")

    assert usage.claude_session_model(path) == "claude-opus-5"
    summed = usage.from_claude_session_file(path, since="2030-01-02T00:00:00Z", duration_ms=9)
    assert summed is not None
    assert summed.model_calls == 1
    counts = (summed.input_tokens, summed.output_tokens, summed.cache_read_tokens, summed.cache_write_tokens)
    assert counts == (2, 50, 300, 40)
    assert summed.source == "session_file"
    assert usage.from_claude_session_file(tmp_path / "missing.jsonl", since=None, duration_ms=None) is None


def test_parse_since_accepts_shorthand_and_iso() -> None:
    assert usage.parse_since(None) is None
    assert usage.parse_since("7d", NOW) == "2029-12-26T12:00:00.000000Z"
    assert usage.parse_since("90m", NOW) == "2030-01-02T10:30:00.000000Z"
    assert usage.parse_since("2030-01-01T00:00:00Z", NOW) == "2030-01-01T00:00:00.000000Z"
    with pytest.raises(ValueError, match="since"):
        usage.parse_since("yesterday", NOW)


def _seed(store: Store, provider: str, mode: Mode, *, state: TaskState, ms: int, tokens: int) -> str:
    fields: dict[str, object] = {"provider": provider, "mode": mode, "prompt": "p"}
    if mode is Mode.IMPLEMENT:
        fields.update(
            repository="/r", acceptance_criteria="a", path_prefixes=["src"],
            verification_commands=[], candidate_message="Add it",
        )
    if mode is Mode.REVIEW:
        fields["review_target"] = {"kind": "candidate", "task_id": "ts_x", "candidate_sha": "abc"}
    record = create_task(store, StartTaskRequest(**fields), repository_id=None, auth_mode=AuthMode.OAUTH)
    store.update_task(record.id, None, state=state)
    started = "2030-01-02T11:00:00.000000Z"
    ended = f"2030-01-02T11:00:{ms // 1000:02d}.{ms % 1000:03d}000Z"
    turn_id = store.insert_turn(record.id, 1, "initial", started_at=started, ended_at=ended)
    store.insert_turn_usage(
        turn_id, record.id, provider, model="claude-opus-5", input_tokens=tokens, output_tokens=tokens // 10,
        cost_estimate_usd=0.5, cost_is_estimate=True, source="acp_prompt_response",
    )
    store.insert_check(record.id, 1, CheckRecord(command="true", exit_code=0, ok=True, duration_ms=ms // 2))
    return record.id


def test_report_rolls_up_tokens_outcomes_and_timings(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    _seed(store, "claude", Mode.CONSULT, state=TaskState.COMPLETED, ms=2000, tokens=1000)
    _seed(store, "claude", Mode.IMPLEMENT, state=TaskState.ACCEPTED, ms=4000, tokens=3000)
    _seed(store, "grok", Mode.REVIEW, state=TaskState.COMPLETED, ms=6000, tokens=500)
    store.set_provider_status(
        "grok", "throttled", code="PROVIDER_THROTTLED", source="acp_error", reset_at="2030-01-03T00:00:00Z"
    )
    profiles = {"claude": Profile(id="claude", auth="oauth", command=("c",), first_class=True),
                "grok": Profile(id="grok", auth="oauth", command=("g",), first_class=True)}

    result = usage.report(store, group_by="provider", profiles=profiles, now=NOW)

    by_provider = {row["provider"]: row for row in result["usage"]}
    assert by_provider["claude"]["turns"] == 2
    assert by_provider["claude"]["input_tokens"] == 4000
    assert by_provider["claude"]["cost_estimate_usd"] == pytest.approx(1.0)
    assert by_provider["grok"]["input_tokens"] == 500
    assert {(row["provider"], row["mode"], row["state"], row["count"]) for row in result["outcomes"]} == {
        ("claude", "consult", "COMPLETED", 1),
        ("claude", "implement", "ACCEPTED", 1),
        ("grok", "review", "COMPLETED", 1),
    }
    assert result["turns"]["count"] == 3
    assert result["turns"]["p50_ms"] == 4000
    assert result["turns"]["by_provider"]["grok"]["mean_ms"] == 6000
    assert result["checks"]["passed"] == 3
    assert result["violations"] == []
    windows = {row["provider"]: row for row in result["windows"]}
    assert windows["grok"]["state"] == "throttled"
    assert windows["grok"]["observable"] is False
    assert windows["claude"]["state"] == "unknown"
    assert windows["claude"]["observable"] is True

    only_grok = usage.report(store, provider="grok", group_by="mode", now=NOW)
    assert only_grok["usage"] == [
        {"provider": "grok", "mode": "review", "turns": 1, "input_tokens": 500, "output_tokens": 50,
         "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
         "cost_estimate_usd": 0.5, "priced_turns": 1}
    ]
    assert only_grok["turns"]["count"] == 1
    with pytest.raises(ValueError, match="group_by"):
        usage.report(store, group_by="colour", now=NOW)
    store.close()
