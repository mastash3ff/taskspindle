"""The report's failure, daily, metering and window rollups, from the store and its read-only view."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskspindle import usage
from taskspindle.models import AuthMode, CheckRecord, Mode, StartTaskRequest, TaskState
from taskspindle.providers import Profile
from taskspindle.service import create_task
from taskspindle.store import Store, current_windows, failure_code
from taskspindle.web.db import ReadOnlyStore

NOW = datetime(2030, 1, 10, 12, 0, tzinfo=UTC)
CLAUDE = {"claude": Profile(id="claude", auth="oauth", command=("c",), first_class=True)}


def _task(
    store: Store, provider: str, *, day: str, state: TaskState, error: dict | None = None,
    mode: Mode = Mode.CONSULT,
) -> str:
    fields: dict[str, object] = {"provider": provider, "mode": mode, "prompt": "secret prompt /home/x"}
    if mode is Mode.REVIEW:
        fields["review_target"] = {"kind": "candidate", "task_id": "ts_x", "candidate_sha": "abc"}
    record = create_task(store, StartTaskRequest(**fields), repository_id=None, auth_mode=AuthMode.OAUTH)
    store.update_task(record.id, None, state=state, error=error, created_at=f"{day}T08:00:00.000000Z")
    return record.id


def _turn(
    store: Store, task_id: str, provider: str, *, day: str, stop_reason: str | None = "end_turn",
    tokens: int | None = None, cost: float | None = 0.25, seconds: int = 10,
) -> int:
    turn_id = store.insert_turn(
        task_id, 1, "initial", started_at=f"{day}T09:00:00.000000Z",
        ended_at=f"{day}T09:00:{seconds:02d}.000000Z", stop_reason=stop_reason,
    )
    if tokens is not None:
        store.insert_turn_usage(
            turn_id, task_id, provider, model="m", input_tokens=tokens, output_tokens=tokens // 10,
            cost_estimate_usd=cost, cost_is_estimate=True, source="acp_prompt_response",
        )
        with store.transaction() as conn:
            conn.execute(
                "UPDATE turn_usage SET captured_at = ? WHERE turn_id = ?",
                (f"{day}T09:00:30.000000Z", turn_id),
            )
    return turn_id


def _check(store: Store, task_id: str, *, day: str, ok: bool) -> None:
    check_id = store.insert_check(
        task_id, 1, CheckRecord(command="pytest", exit_code=0 if ok else 1, ok=ok, duration_ms=5)
    )
    with store.transaction() as conn:
        conn.execute("UPDATE checks SET created_at = ? WHERE id = ?", (f"{day}T10:00:00.000000Z", check_id))


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    path = tmp_path / "s.sqlite3"
    with Store.open(path) as store:
        ok = _task(store, "claude", day="2030-01-08", state=TaskState.COMPLETED)
        _turn(store, ok, "claude", day="2030-01-08", tokens=1000)
        _check(store, ok, day="2030-01-08", ok=True)
        _check(store, ok, day="2030-01-08", ok=True)

        timeout = _task(store, "grok", day="2030-01-09", state=TaskState.FAILED,
                        error={"code": "TURN_TIMEOUT", "message": "turn exceeded 300.0s /home/x"})
        _turn(store, timeout, "grok", day="2030-01-09", stop_reason="timeout", seconds=30)
        _check(store, timeout, day="2030-01-09", ok=False)

        malformed = _task(store, "grok", day="2030-01-09", state=TaskState.FAILED, mode=Mode.REVIEW,
                          error={"code": "REVIEW_MALFORMED", "message": "no json"})
        # A successful turn nothing metered, and a metered one no price covers.
        _turn(store, malformed, "grok", day="2030-01-09", stop_reason="end_turn")
        _turn(store, malformed, "grok", day="2030-01-09", tokens=500, cost=None)

        _task(store, "grok", day="2030-01-09", state=TaskState.FAILED)  # no error recorded
        _task(store, "claude", day="2030-01-10", state=TaskState.RUNNING)
        _task(store, "claude", day="2029-12-01", state=TaskState.FAILED,
              error={"code": "TURN_TIMEOUT", "message": "old"})
    return path


def test_failures_are_counted_by_provider_mode_and_code_never_by_message(seeded: Path) -> None:
    with Store.open(seeded) as store:
        result = usage.report(store, since="2030-01-01T00:00:00Z", now=NOW)
    assert result["failures"] == [
        {"provider": "grok", "mode": "consult", "code": "TURN_TIMEOUT", "count": 1},
        {"provider": "grok", "mode": "consult", "code": "UNKNOWN", "count": 1},
        {"provider": "grok", "mode": "review", "code": "REVIEW_MALFORMED", "count": 1},
    ]
    failed = sum(row["count"] for row in result["outcomes"] if row["state"] == "FAILED")
    assert failed == sum(row["count"] for row in result["failures"])
    assert "/home/x" not in json.dumps(result)


def test_failure_code_reports_only_an_identifier() -> None:
    assert failure_code('{"code": "ACP_HANDSHAKE_FAILED", "message": "x"}') == "ACP_HANDSHAKE_FAILED"
    assert failure_code({"code": "TURN_TIMEOUT"}) == "TURN_TIMEOUT"
    assert failure_code(None) == "UNKNOWN"
    assert failure_code("not json") == "UNKNOWN"
    assert failure_code('{"message": "no code"}') == "UNKNOWN"
    assert failure_code('{"code": "turn exceeded /home/x"}') == "OTHER"
    assert failure_code('{"code": 7}') == "OTHER"


def test_timing_stats_carry_a_p95() -> None:
    assert usage._stats([]) == {
        "count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None,
    }
    values = list(range(1, 21))
    stats = usage._stats(values)
    assert stats["p50_ms"] == int(usage.quantile(values, 0.5)) == 10
    assert stats["p95_ms"] == 19  # 1 + 0.95 * 19 = 19.05, interpolated
    assert usage._stats([7])["p95_ms"] == 7


def test_daily_series_is_dense_and_counts_each_kind_on_its_own_day(seeded: Path) -> None:
    with Store.open(seeded) as store:
        result = usage.report(store, since="2030-01-07T00:00:00Z", now=NOW)
    days = {row["day"]: row for row in result["daily"]}
    assert list(days) == ["2030-01-07", "2030-01-08", "2030-01-09", "2030-01-10"]
    assert days["2030-01-07"] == {
        "day": "2030-01-07", "tasks_created": 0, "outcomes": {}, "failures": 0, "turns": 0,
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "cost_estimate_usd": 0.0, "checks_run": 0, "checks_passed": 0,
    }
    assert days["2030-01-08"]["outcomes"] == {"COMPLETED": 1}
    assert days["2030-01-08"]["input_tokens"] == 1000
    assert days["2030-01-08"]["cost_estimate_usd"] == 0.25
    assert (days["2030-01-08"]["checks_run"], days["2030-01-08"]["checks_passed"]) == (2, 2)
    assert days["2030-01-09"]["tasks_created"] == 3
    assert days["2030-01-09"]["failures"] == 3
    assert days["2030-01-09"]["turns"] == 3
    assert (days["2030-01-09"]["checks_run"], days["2030-01-09"]["checks_passed"]) == (1, 0)
    # A running task is created but has no terminal outcome yet.
    assert days["2030-01-10"]["tasks_created"] == 1
    assert days["2030-01-10"]["outcomes"] == {}

    with Store.open(seeded) as store:
        grok = usage.report(store, since="2030-01-07T00:00:00Z", provider="grok", now=NOW)
    assert sum(row["tasks_created"] for row in grok["daily"]) == 3
    assert sum(row["checks_run"] for row in grok["daily"]) == 1


def test_open_ended_daily_series_covers_the_default_days_and_caps_long_windows(seeded: Path) -> None:
    with Store.open(seeded) as store:
        open_ended = usage.report(store, now=NOW)["daily"]
        long = usage.report(store, since="2020-01-01T00:00:00Z", now=NOW)["daily"]
    assert len(open_ended) == usage.DAILY_DEFAULT_DAYS
    assert open_ended[-1]["day"] == "2030-01-10"
    # The December task is outside the default window and does not appear.
    assert sum(row["tasks_created"] for row in open_ended) == 5
    assert len(long) == usage.DAILY_MAX_DAYS


def test_metering_says_how_much_of_the_work_carries_usage_and_a_price(seeded: Path) -> None:
    with Store.open(seeded) as store:
        result = usage.report(store, since="2030-01-01T00:00:00Z", now=NOW)
    assert result["metering"] == [
        {"provider": "claude", "turns": 1, "turns_with_usage": 1, "unmetered_turns": 0,
         "unmetered_successful_turns": 0, "unpriced_turns": 0},
        {"provider": "grok", "turns": 3, "turns_with_usage": 1, "unmetered_turns": 2,
         "unmetered_successful_turns": 1, "unpriced_turns": 1},
    ]


def test_new_sections_match_between_the_store_and_the_read_only_view(seeded: Path) -> None:
    with Store.open(seeded) as store:
        expected = usage.report(store, since="2030-01-01T00:00:00Z", profiles=CLAUDE, now=NOW)
    with ReadOnlyStore(seeded) as reader:
        assert usage.report(reader, since="2030-01-01T00:00:00Z", profiles=CLAUDE, now=NOW) == expected
    with ReadOnlyStore(seeded.parent / "missing.sqlite3") as empty:
        nothing = usage.report(empty, since="2030-01-01T00:00:00Z", now=NOW)
    assert nothing["failures"] == [] and nothing["metering"] == []
    assert all(row["tasks_created"] == 0 for row in nothing["daily"])


# -- windows ----------------------------------------------------------------------------------


def _window(store: Store, window: str, *, status: str, percent: float | None, resets: str, at: str) -> None:
    store.insert_provider_window(
        "claude", window, source="rate_limit_event", status=status, used_percent=percent,
        resets_at=resets, observed_at=at, period_key=f"{window}|{resets}",
    )


def _seed_windows(store: Store) -> None:
    for hour, resets in ((1, "2030-01-10T05:00:00Z"), (6, "2030-01-10T10:00:00Z")):
        _window(store, "five_hour", status="allowed", percent=None, resets=resets,
                at=f"2030-01-10T0{hour}:00:00.000000Z")
    _window(store, "five_hour", status="allowed_warning", percent=81.0, resets="2030-01-10T10:00:00Z",
            at="2030-01-10T07:00:00.000000Z")
    _window(store, "five_hour", status="allowed", percent=None, resets="2030-01-10T10:00:00Z",
            at="2030-01-10T08:00:00.000000Z")
    _window(store, "seven_day", status="allowed_warning", percent=90.0, resets="2030-01-12T00:00:00Z",
            at="2030-01-10T07:00:00.000000Z")
    _window(store, "seven_day_opus", status="allowed", percent=None, resets="2030-01-12T00:00:00Z",
            at="2030-01-10T07:00:00.000000Z")


def test_windows_report_shows_each_window_once_with_the_last_known_percentage(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    with Store.open(path) as store:
        _seed_windows(store)
        entries = usage.windows_report(store, CLAUDE, NOW)
        per_period = store.latest_provider_windows("claude")
    windows = {row["window"]: row for row in entries[0]["windows"]}
    assert list(windows) == ["five_hour", "seven_day", "seven_day_opus"]
    five = windows["five_hour"]
    assert five["status"] == "allowed"
    assert five["observed_at"] == "2030-01-10T08:00:00.000000Z"
    assert five["used_percent"] == 81.0
    assert five["used_percent_observed_at"] == "2030-01-10T07:00:00.000000Z"
    assert windows["seven_day"]["used_percent_observed_at"] == windows["seven_day"]["observed_at"]
    assert windows["seven_day_opus"]["scope"] == "model_family"
    assert windows["seven_day_opus"]["used_percent"] is None
    assert windows["seven_day_opus"]["used_percent_observed_at"] is None
    # The per-period read the dispatch policy uses is unchanged.
    assert len([row for row in per_period if row["window"] == "five_hour"]) == 2
    with ReadOnlyStore(path) as reader:
        assert usage.windows_report(reader, CLAUDE, NOW) == entries


def test_a_percentage_from_an_earlier_period_is_never_carried_forward() -> None:
    rows = [
        {"id": 1, "provider": "claude", "window": "five_hour", "status": "allowed_warning",
         "used_percent": 95.0, "resets_at": "A", "observed_at": "t1"},
        {"id": 2, "provider": "claude", "window": "five_hour", "status": "allowed",
         "used_percent": None, "resets_at": "B", "observed_at": "t2"},
    ]
    (current,) = current_windows(rows)
    assert current["id"] == 2
    assert current["used_percent"] is None
    assert current["used_percent_observed_at"] is None
