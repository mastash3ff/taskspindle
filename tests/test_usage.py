"""Token usage: the parsers, the price table, the session-file fallback and the rollup."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskspindle import usage
from taskspindle.acp_client import TurnCapture, TurnResult
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


def test_with_model_prices_a_turn_once_the_model_is_known() -> None:
    unknown = usage.from_prompt_response({"input_tokens": 1_000_000}, model=None, duration_ms=None)
    assert unknown.cost_estimate_usd is None

    priced = usage.with_model(unknown, "claude-sonnet-5")
    assert priced.model == "claude-sonnet-5"
    assert priced.cost_estimate_usd == pytest.approx(2.0)
    assert priced.input_tokens == 1_000_000
    assert usage.with_model(priced, None) is priced
    assert usage.with_model(priced, "claude-sonnet-5") is priced


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
    # Grok's own tick figure stays raw and unconverted. The estimate beside it is ours, computed
    # from the published list price; see the real-turn agreement test below.
    assert turn.raw["costUsdTicks"] == 43475800
    assert turn.cost_estimate_usd == pytest.approx(0.025574)
    assert turn.price_table_version == "2026-09-16"
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


@pytest.mark.parametrize("session_model", ["claude-opus-5", None])
def test_claude_configuration_model_precedes_the_session_file(
    tmp_path: Path, monkeypatch, session_model: str | None
) -> None:
    reads = []

    def file_model(path: Path) -> str:
        reads.append(path)
        return "claude-sonnet-5"

    monkeypatch.setattr(usage, "claude_session_model", file_model)
    collected = usage.collect(
        TurnResult("end_turn", "OK", TurnCapture(), usage={"input_tokens": 100}),
        profile=Profile(id="claude", auth="oauth", command=("claude",), first_class=True),
        cwd=tmp_path, session_id="session-1", home=tmp_path, duration_ms=10,
        session_model=session_model,
    )
    assert collected.model == (session_model or "claude-sonnet-5")
    assert collected.usage.model == collected.model
    assert bool(reads) is (session_model is None)


def _seed(store: Store, provider: str, mode: Mode, *, state: TaskState, ms: int, tokens: int,
          repository_id: str | None = None, role: str | None = None) -> str:
    fields: dict[str, object] = {"provider": provider, "mode": mode, "prompt": "p", "role": role}
    if mode is Mode.IMPLEMENT:
        fields.update(
            repository="/r", acceptance_criteria="a", path_prefixes=["src"],
            verification_commands=[], candidate_message="Add it",
        )
    if mode is Mode.REVIEW:
        fields["review_target"] = {"kind": "candidate", "task_id": "ts_x", "candidate_sha": "abc"}
    record = create_task(
        store, StartTaskRequest(**fields), repository_id=repository_id, auth_mode=AuthMode.OAUTH
    )
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


def _seed_usage_row(
    store: Store, provider: str, *,
    model: str | None = None,
    cost_estimate_usd: float | None = None,
    price_table_version: str | None = None,
    resolved_model: str | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 100,
    source: str = "acp_prompt_response",
    raw: dict | None = None,
    captured_at: str | None = None,
) -> tuple[str, int]:
    """One task with one turn and one fully-controlled ``turn_usage`` row, for reprice tests."""
    record = create_task(
        store, StartTaskRequest(provider=provider, mode=Mode.CONSULT, prompt="p"),
        repository_id=None, auth_mode=AuthMode.OAUTH,
    )
    if resolved_model is not None:
        store.update_task(record.id, None, bump_version=False, resolved_model=resolved_model)
    turn_id = store.insert_turn(
        record.id, 1, "initial",
        started_at="2030-01-02T11:00:00.000000Z", ended_at="2030-01-02T11:00:01.000000Z",
    )
    store.insert_turn_usage(
        turn_id, record.id, provider, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens, cache_read_tokens=0, cache_write_tokens=0,
        reasoning_tokens=0, model_calls=1, duration_ms=1000,
        cost_estimate_usd=cost_estimate_usd, cost_is_estimate=True,
        price_table_version=price_table_version, source=source, raw=raw,
    )
    if captured_at is not None:
        store._conn.execute(
            "UPDATE turn_usage SET captured_at = ? WHERE turn_id = ?", (captured_at, turn_id)
        )
    return record.id, turn_id


def test_reprice_prices_a_known_model_with_no_stored_cost(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    _, turn_id = _seed_usage_row(store, "grok", model="grok-4.6", input_tokens=1_000_000)
    expected_cost, expected_version = usage.estimate_cost(
        "grok-4.6", input_tokens=1_000_000, output_tokens=100,
        cache_read_tokens=0, cache_write_tokens=0,
    )

    result = usage.reprice(store)

    assert result["rows_examined"] == 1
    assert result["rows_repriced"] == 1
    assert result["rows_unpriced"] == 0
    entry = result["repriced"][0]
    assert entry["turn_id"] == turn_id
    assert entry["old_cost_estimate_usd"] is None
    assert entry["new_cost_estimate_usd"] == expected_cost
    assert entry["price_table_version"] == expected_version
    assert result["by_price_table_version"] == {expected_version: 1}

    row = store.list_turn_usage()[0]
    assert row["cost_estimate_usd"] == expected_cost
    assert row["price_table_version"] == expected_version


def test_reprice_leaves_an_unknown_model_unpriced_and_reports_why(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    task_id, turn_id = _seed_usage_row(store, "claude", model="mystery-9")

    result = usage.reprice(store)

    assert result["rows_repriced"] == 0
    assert result["rows_unpriced"] == 1
    assert result["unpriced"] == [
        {
            "turn_id": turn_id, "task_id": task_id,
            "model": "mystery-9", "reason": "model unknown to the price table",
        }
    ]


def test_reprice_reports_a_row_with_no_model_as_no_model_recorded(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    _seed_usage_row(store, "agy", model=None)

    result = usage.reprice(store)

    assert result["rows_unpriced"] == 1
    assert result["unpriced"][0]["reason"] == "no model recorded"
    assert result["unpriced"][0]["model"] is None


def test_reprice_dry_run_reports_but_writes_nothing(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    _seed_usage_row(store, "grok", model="grok-4.6", input_tokens=1_000_000)

    result = usage.reprice(store, dry_run=True)

    assert result["rows_repriced"] == 1
    assert store.list_turn_usage()[0]["cost_estimate_usd"] is None


def test_reprice_filters_by_since_and_provider(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    _seed_usage_row(store, "grok", model="grok-4.6", captured_at="2029-01-01T00:00:00.000000Z")
    _seed_usage_row(store, "claude", model="claude-sonnet-5", captured_at="2030-06-01T00:00:00.000000Z")

    only_recent = usage.reprice(store, since="2030-01-01T00:00:00Z")
    assert only_recent["rows_examined"] == 1

    only_grok = usage.reprice(store, provider="grok")
    assert only_grok["rows_examined"] == 1
    assert only_grok["repriced"][0]["model"] == "grok-4.6"


def test_reprice_preserves_every_other_column(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    _, turn_id = _seed_usage_row(
        store, "grok", model="grok-4.6", input_tokens=1_000_000,
        source="acp_turn_completed", raw={"costUsdTicks": 123}, captured_at="2029-06-01T00:00:00.000000Z",
    )
    before = store.get_turn_usage(turn_id)

    usage.reprice(store)

    after = store.get_turn_usage(turn_id)
    assert after["captured_at"] == before["captured_at"] == "2029-06-01T00:00:00.000000Z"
    assert after["raw"] == before["raw"] == {"costUsdTicks": 123}
    assert after["source"] == before["source"] == "acp_turn_completed"
    assert after["cost_estimate_usd"] != before["cost_estimate_usd"]


def test_reprice_running_twice_reports_the_second_run_as_unchanged(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    _seed_usage_row(store, "grok", model="grok-4.6", input_tokens=1_000_000)

    first = usage.reprice(store)
    second = usage.reprice(store)

    assert first["rows_repriced"] == 1
    assert second["rows_repriced"] == 0
    assert second["rows_unchanged"] == 1


def test_reprice_use_selected_model_prices_from_the_task_and_leaves_attribution_null(
    tmp_path: Path,
) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    task_id, turn_id = _seed_usage_row(
        store, "agy", model=None, input_tokens=1_000_000, resolved_model="gemini-3.1-pro-high",
    )
    expected_cost, expected_version = usage.estimate_cost(
        "gemini-3.1-pro-high", input_tokens=1_000_000, output_tokens=100,
        cache_read_tokens=0, cache_write_tokens=0,
    )

    without_flag = usage.reprice(store)
    assert without_flag["rows_unpriced"] == 1
    assert without_flag["unpriced"][0]["reason"] == "no model recorded"

    result = usage.reprice(store, use_selected_model=True)

    assert result["rows_unpriced"] == 0
    entry = result["repriced"][0]
    assert entry["model"] == "gemini-3.1-pro-high"
    assert entry["model_source"] == "task.resolved_model"
    assert entry["new_cost_estimate_usd"] == expected_cost

    row = store.get_turn_usage(turn_id)
    assert row["cost_estimate_usd"] == expected_cost
    assert row["price_table_version"] == expected_version
    assert row["model"] is None
    assert store.get_task(task_id).resolved_model == "gemini-3.1-pro-high"


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


def test_report_groups_by_the_recorded_role(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    _seed(store, "claude", Mode.CONSULT, state=TaskState.COMPLETED, ms=1000, tokens=100, role="explorer")
    _seed(store, "claude", Mode.CONSULT, state=TaskState.COMPLETED, ms=1000, tokens=200, role="explorer")
    _seed(store, "grok", Mode.CONSULT, state=TaskState.COMPLETED, ms=1000, tokens=300)
    by_role = usage.report(store, group_by="role", now=NOW)["usage"]
    assert [(row["provider"], row["role"], row["turns"], row["input_tokens"]) for row in by_role] == [
        ("claude", "explorer", 2, 300), ("grok", None, 1, 300),
    ]
    assert store.get_task(by_role and _first_task_id(store)).role == "explorer"
    store.close()


def _first_task_id(store: Store) -> str:
    return store.list_tasks(provider="claude", limit=1)[0].id


def test_windows_report_never_exposes_a_legacy_provider_reason(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "s.sqlite3")
    try:
        store.set_provider_status(
            "claude",
            "auth_expired",
            source="acp_error",
            reason="oauth_token=legacy-secret-value",
        )
        result = usage.windows_report(
            store,
            {"claude": Profile(id="claude", auth="oauth", command=("c",), first_class=True)},
            NOW,
        )
    finally:
        store.close()

    assert result[0]["status"]["reason"] == "Provider authentication is required."
    assert "legacy-secret-value" not in json.dumps(result)


def test_repository_rollup_matches_read_only_store_and_preserves_filters(tmp_path: Path) -> None:
    from taskspindle.web.db import ReadOnlyStore

    path = tmp_path / "s.sqlite3"
    with Store.open(path) as store:
        for repo in ("r1", "r2"):
            store.insert_repository(repo, f"/{repo}/.git", repo, f"/{repo}")
        tasks = []
        for repo, provider, tokens in [
            ("r1", "claude", 100), ("r1", "grok", 200), ("r2", "claude", 400),
            (None, "grok", 800),
        ]:
            tasks.append(_seed(store, provider, Mode.CONSULT, state=TaskState.COMPLETED,
                               ms=1000, tokens=tokens, repository_id=repo))
        result = usage.report(store, group_by="repository_id", now=NOW)
        assert {row["repository_id"]: row["input_tokens"] for row in result["usage"]} == {
            "r1": 300, "r2": 400, None: 800,
        }
        assert next(row for row in result["usage"] if row["repository_id"] == "r1")["turns"] == 2
        assert {row["repository_id"]: row["repository_path"] for row in result["usage"]} == {
            "r1": "/r1", "r2": "/r2", None: None,
        }
        with ReadOnlyStore(path) as reader:
            assert usage.report(reader, group_by="repository_id", now=NOW) == result
            for backend in (store, reader):
                filtered = usage.report(backend, provider="claude", group_by="repository_id", now=NOW)
                assert {r["repository_id"]: r["input_tokens"] for r in filtered["usage"]} == {
                    "r1": 100, "r2": 400,
                }
                assert backend.list_turn_usage(task_id=tasks[1])[0]["repository_id"] == "r1"
                assert backend.list_turn_usage(since="2999-01-01T00:00:00Z") == []


@pytest.mark.parametrize(("wire", "backend", "expected"), [
    (["grok-4.6"], {"grok-4.6-build": {}}, "grok-4.6"),
    ([], {"grok-4.6-build": {}}, "grok-4.6-build"),
    ([], {}, "grok-profile"),
])
def test_grok_model_precedence_keeps_attribution_and_usage_together(
    tmp_path: Path, wire, backend, expected,
) -> None:
    result = TurnResult(text="done", stop_reason="end_turn", capture=TurnCapture(
        model_ids=wire, turn_completed={"usage": {
            "inputTokens": 10, "modelCalls": 1, "modelUsage": backend,
        }},
    ))
    collected = usage.collect(
        result, profile=Profile(id="grok", auth="oauth", command=("grok",), model="grok-profile"),
        cwd=tmp_path, session_id="session", home=tmp_path, duration_ms=100,
    )
    assert collected.model == collected.usage.model == expected
    assert collected.usage.raw["modelUsage"] == backend
    assert collected.usage.input_tokens == 10
    # A grok-* attribution prices; the bare profile id in the third case has no price row.
    assert (collected.usage.cost_estimate_usd is None) is (not expected.startswith("grok-4"))
    assert collected.usage.source == usage.SOURCE_TURN_COMPLETED


# -- prices beyond Anthropic -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("input_tokens", "cache_read", "output_tokens", "ticks"),
    [
        (17144, 128, 634, 128_860_000),
        (21030, 6144, 11512, 346_514_400),
        (389004, 231680, 14043, 1_750_136_400),
    ],
)
def test_the_grok_estimate_matches_what_grok_reported_for_the_same_turn(
    input_tokens: int, cache_read: int, output_tokens: int, ticks: int
) -> None:
    # Three real grok-4.6 turns: Grok's own costUsdTicks is list-price USD x 3.4e9, with cached
    # tokens counted inside inputTokens and reasoning inside outputTokens.
    usd, version = usage.estimate_cost(
        "grok-4.6", input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read, cache_write_tokens=0,
    )
    assert round(usd * 3.4e9) == ticks
    assert version == "2026-09-16"


def test_antigravity_picker_ids_price_as_their_gemini_model() -> None:
    pro, _ = usage.estimate_cost(
        "gemini-3.1-pro-high", input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_tokens=1_000_000, cache_write_tokens=0,
    )
    flash, version = usage.estimate_cost(
        "gemini-3.8-flash-medium", input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_tokens=1_000_000, cache_write_tokens=0,
    )
    assert pro == pytest.approx(2.0 + 12.0 + 0.2)
    assert flash == pytest.approx(0.75 + 3.75 + 0.075)
    assert version == "2026-09-16"


def test_a_claude_row_keeps_the_anthropic_table_date_and_separate_cache_accounting() -> None:
    usd, version = usage.estimate_cost(
        "claude-sonnet-5", input_tokens=1_000_000, output_tokens=0,
        cache_read_tokens=1_000_000, cache_write_tokens=0,
    )
    assert usd == pytest.approx(2.0 + 0.2)
    assert version == usage.PRICE_TABLE_VERSION
