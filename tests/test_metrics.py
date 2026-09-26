"""The Prometheus text exposition: format, escaping, ordering, and what may become a label."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

import taskspindle
from taskspindle import metrics
from taskspindle.models import AuthMode, CheckRecord, Mode, StartTaskRequest, TaskState
from taskspindle.providers import Profile
from taskspindle.service import create_task
from taskspindle.store import Store
from taskspindle.web.db import ReadOnlyStore

NOW = datetime(2030, 1, 10, 12, 0, tzinfo=UTC)
PROFILES = {
    "claude": Profile(id="claude", auth="oauth", command=("c",), first_class=True),
    "grok": Profile(id="grok", auth="oauth", command=("g",), first_class=True),
}
LIMITS = {"claude": 2, "grok": 1}
SECRET = "do not leak /home/someone/private.key"

_SAMPLE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(?:\{(?P<labels>[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\["\\n])*"'
    r'(?:,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\["\\n])*")*)\})?'
    r' (?P<value>NaN|[+-]Inf|-?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?)$'
)


def _seed(path: Path) -> None:
    with Store.open(path) as store:
        def task(provider: str, state: TaskState, error: dict | None = None) -> str:
            record = create_task(
                store, StartTaskRequest(provider=provider, mode=Mode.CONSULT, prompt=SECRET),
                repository_id=None, auth_mode=AuthMode.OAUTH,
            )
            store.update_task(record.id, None, state=state, error=error)
            return record.id

        done = task("claude", TaskState.COMPLETED)
        for seconds in (2, 4, 10):
            turn = store.insert_turn(done, 1, "initial", started_at="2030-01-10T09:00:00.000000Z",
                                     ended_at=f"2030-01-10T09:00:{seconds:02d}.000000Z",
                                     stop_reason="end_turn", prompt=SECRET)
            store.insert_turn_usage(
                turn, done, "claude", model="claude-opus-5", input_tokens=100, output_tokens=10,
                cache_read_tokens=1000, cache_write_tokens=5, reasoning_tokens=0,
                cost_estimate_usd=0.125, cost_is_estimate=True, source="acp_prompt_response",
            )
        store.insert_check(done, 1, CheckRecord(command=SECRET, exit_code=0, ok=True, duration_ms=5))
        store.insert_check(done, 1, CheckRecord(command="false", exit_code=1, ok=False, duration_ms=5))

        failed = task("grok", TaskState.FAILED, {"code": "TURN_TIMEOUT", "message": SECRET})
        store.insert_turn(failed, 1, "initial", started_at="2029-01-01T00:00:00.000000Z",
                          ended_at="2029-01-01T00:01:00.000000Z", stop_reason="timeout")
        task("grok", TaskState.FAILED, {"code": SECRET, "message": "prose"})
        task("grok", TaskState.QUEUED)
        store.set_provider_status("grok", "throttled", code="PROVIDER_THROTTLED", source="acp_error",
                                  reason=SECRET, reset_at="2030-01-11T00:00:00Z")


def _render(path: Path) -> str:
    with ReadOnlyStore(path) as store:
        return metrics.render(store, profiles=PROFILES, limits=LIMITS, now=NOW)


def _samples(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        assert match, f"not a valid sample line: {line!r}"
        name = line.rsplit(" ", 1)[0]
        assert name not in found, f"duplicate series: {name}"
        found[name] = float(match["value"])
    return found


def test_every_line_is_valid_exposition_and_every_family_is_declared_once(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    _seed(path)
    text = _render(path)

    assert text.endswith("\n") and not text.endswith("\n\n")
    declared: list[str] = []
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("# HELP "):
            current = line.split(" ")[2]
            assert current not in declared
            declared.append(current)
        elif line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert name == current
            assert kind in {"counter", "gauge", "summary"}
        else:
            name = _SAMPLE.match(line)["name"]
            assert current is not None and name.startswith(current)
            assert name in {current, f"{current}_sum", f"{current}_count"}
    assert declared == [
        "taskspindle_build_info", "taskspindle_database_schema_version", "taskspindle_tasks",
        "taskspindle_task_failures_total", "taskspindle_turns_total",
        "taskspindle_turns_unmetered_total", "taskspindle_turn_duration_seconds",
        "taskspindle_tokens_total", "taskspindle_estimated_cost_usd_total", "taskspindle_checks_total",
        "taskspindle_slot_limit", "taskspindle_active_leases", "taskspindle_queue_depth",
        "taskspindle_slot_utilization_ratio", "taskspindle_provider_available",
        "taskspindle_provider_state",
    ]
    _samples(text)


def test_values_come_from_the_database(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    _seed(path)
    samples = _samples(_render(path))

    version, schema = taskspindle.__version__, taskspindle.SCHEMA_VERSION
    assert samples[f'taskspindle_build_info{{schema="{schema}",version="{version}"}}'] == 1
    assert samples["taskspindle_database_schema_version"] == taskspindle.SCHEMA_VERSION
    assert samples['taskspindle_tasks{mode="consult",provider="claude",state="COMPLETED"}'] == 1
    assert samples['taskspindle_tasks{mode="consult",provider="grok",state="FAILED"}'] == 2
    assert samples['taskspindle_task_failures_total{code="TURN_TIMEOUT",provider="grok"}'] == 1
    assert samples['taskspindle_task_failures_total{code="OTHER",provider="grok"}'] == 1
    assert samples['taskspindle_turns_total{provider="claude"}'] == 3
    assert samples['taskspindle_turns_unmetered_total{provider="grok"}'] == 1
    assert samples['taskspindle_tokens_total{kind="input",provider="claude"}'] == 300
    assert samples['taskspindle_tokens_total{kind="cache_read",provider="claude"}'] == 3000
    assert samples['taskspindle_estimated_cost_usd_total{provider="claude"}'] == 0.375
    assert samples['taskspindle_checks_total{provider="claude",result="passed"}'] == 1
    assert samples['taskspindle_checks_total{provider="claude",result="failed"}'] == 1
    assert samples['taskspindle_slot_limit{provider="claude"}'] == 2
    assert samples['taskspindle_active_leases{provider="claude"}'] == 0
    assert samples['taskspindle_queue_depth{provider="grok"}'] == 1
    assert samples['taskspindle_provider_available{provider="grok"}'] == 0
    assert samples['taskspindle_provider_state{provider="grok",state="throttled"}'] == 1
    assert samples['taskspindle_provider_available{provider="claude"}'] == 1


def test_turn_durations_are_a_summary_with_recent_quantiles_and_lifetime_sum(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    _seed(path)
    text = _render(path)
    samples = _samples(text)

    assert samples['taskspindle_turn_duration_seconds{provider="claude",quantile="0.5"}'] == 4.0
    assert samples['taskspindle_turn_duration_seconds{provider="claude",quantile="0.95"}'] == 9.4
    assert samples['taskspindle_turn_duration_seconds_sum{provider="claude"}'] == 16.0
    assert samples['taskspindle_turn_duration_seconds_count{provider="claude"}'] == 3
    # grok's only turn is a year old: counted in sum and count, absent from the recent quantiles.
    assert math.isnan(samples['taskspindle_turn_duration_seconds{provider="grok",quantile="0.5"}'])
    assert samples['taskspindle_turn_duration_seconds_count{provider="grok"}'] == 1
    claude = [line.split(" ")[0] for line in text.splitlines()
              if line.startswith("taskspindle_turn_duration_seconds") and 'provider="claude"' in line]
    assert claude == [
        'taskspindle_turn_duration_seconds{provider="claude",quantile="0.5"}',
        'taskspindle_turn_duration_seconds{provider="claude",quantile="0.95"}',
        'taskspindle_turn_duration_seconds{provider="claude",quantile="0.99"}',
        'taskspindle_turn_duration_seconds_sum{provider="claude"}',
        'taskspindle_turn_duration_seconds_count{provider="claude"}',
    ]


def test_no_prompt_path_command_or_message_ever_reaches_the_output(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    _seed(path)
    text = _render(path)
    assert "leak" not in text
    assert "/home" not in text
    assert "prose" not in text


def test_output_is_stable_and_samples_are_sorted_within_a_family(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    _seed(path)
    first, second = _render(path), _render(path)
    assert first == second
    tasks = [line for line in first.splitlines() if line.startswith("taskspindle_tasks{")]
    assert tasks == sorted(tasks)


def test_a_missing_database_still_renders_build_info_and_per_provider_gauges(tmp_path: Path) -> None:
    with ReadOnlyStore(tmp_path / "missing.sqlite3") as store:
        text = metrics.render(store, profiles=PROFILES, limits=LIMITS, now=NOW)
    samples = _samples(text)
    assert "# TYPE taskspindle_database_schema_version gauge" in text
    assert "taskspindle_database_schema_version" not in samples
    assert samples['taskspindle_active_leases{provider="grok"}'] == 0
    assert samples['taskspindle_provider_state{provider="claude",state="ok"}'] == 1
    assert not (tmp_path / "missing.sqlite3").exists()


@pytest.mark.parametrize(("value", "expected"), [
    ("claude", "claude"), ("opencode-go", "opencode-go"), ("TURN_TIMEOUT", "TURN_TIMEOUT"),
    ("0.95", "0.95"), ("/home/x", "other"), ("two words", "other"), ("", "other"), (None, "other"),
    ('a"b', "other"), ("x" * 65, "other"),
])
def test_only_identifiers_become_label_values(value: object, expected: str) -> None:
    assert metrics.label(value) == expected


def test_label_and_help_escaping_follow_the_text_format() -> None:
    assert metrics._escape_label('a\\b"c\nd') == 'a\\\\b\\"c\\nd'
    family = metrics.Family("x_total", "counter", "line one\nback\\slash")
    family.add(1, provider="p")
    family.add(2, provider="p")  # the same series adds up rather than repeating
    assert metrics.format_families([family]) == (
        "# HELP x_total line one\\nback\\\\slash\n# TYPE x_total counter\nx_total{provider=\"p\"} 3\n"
    )


def test_numbers_render_as_the_format_expects() -> None:
    family = metrics.Family("n", "gauge", "numbers")
    family.add(math.nan, which="nan")
    family.add(math.inf, which="pinf")
    family.add(-math.inf, which="ninf")
    family.add(0.5, which="half")
    family.add(3, which="int")
    lines = metrics.format_families([family]).splitlines()[2:]
    assert lines == [
        'n{which="half"} 0.5', 'n{which="int"} 3', 'n{which="nan"} NaN',
        'n{which="ninf"} -Inf', 'n{which="pinf"} +Inf',
    ]


def test_invalid_names_are_refused() -> None:
    with pytest.raises(ValueError, match="metric name"):
        metrics.format_families([metrics.Family("bad-name", "gauge", "x")])
    family = metrics.Family("ok", "gauge", "x")
    family.add(1, __reserved="v")
    with pytest.raises(ValueError, match="label name"):
        metrics.format_families([family])
