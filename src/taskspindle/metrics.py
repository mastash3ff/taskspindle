"""What the task database records, as Prometheus text exposition (format 0.0.4).

Everything is read from the database on every render; nothing is kept in memory between scrapes,
so the dashboard's ``GET /metrics`` and ``taskspindle metrics`` print the same numbers for the same
database. The accumulating series (``*_total`` and the summary's ``_sum``/``_count``) are sums
over the rows on file: they only grow while rows are only added, and a pruned database lowers them,
which Prometheus reads as a counter reset.

Label values are identifiers and enums only -- provider IDs, modes, states, error codes, token
kinds -- and anything that does not look like one is reported as ``other``. No path, prompt, model
string, message or other free text ever becomes a label.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import taskspindle

from . import utilization
from .models import TaskState
from .providers import Profile
from .store import UsageReader
from .usage import quantile

__all__ = ["CONTENT_TYPE", "Family", "MetricsReader", "collect", "format_families", "render"]

#: The ``Content-Type`` of the text exposition format this module writes.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: The quantiles of turn duration reported per provider.
TURN_QUANTILES = (0.5, 0.95, 0.99)
#: How far back the turn-duration quantiles look; ``_sum`` and ``_count`` cover every turn.
QUANTILE_SPAN = timedelta(days=7)
#: The span slot utilization is measured over.
UTILIZATION_SPAN = utilization.SPANS["day"]

_TOKEN_KINDS = ("input", "output", "cache_read", "cache_write", "reasoning")
_DISPATCHABLE = (TaskState.QUEUED.value, TaskState.REPAIRING.value, TaskState.RESUMING.value)
_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,63}")
_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


class MetricsReader(UsageReader, Protocol):
    """The store reads a metrics render needs; both the store and the dashboard's view have them."""

    def schema_version(self) -> int | None: ...

    def usage_totals(self) -> list[dict[str, Any]]: ...

    def list_leases(self, provider: str | None = None) -> list[dict[str, Any]]: ...

    def queued_counts(self, states: Sequence[str]) -> dict[str, int]: ...

    def list_lease_history(self, *, since: str) -> list[dict[str, Any]]: ...


@dataclass
class Family:
    """One metric family: its name, type, help text and samples.

    A sample is ``(suffix, labels, value)``; ``suffix`` is appended to the family name
    (``"_sum"`` for a summary's sum, ``""`` for an ordinary sample). Two samples whose labels
    collapse to the same values (both ``other``) are added together, so no series is duplicated.
    """

    name: str
    type: str
    help: str
    _values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = field(default_factory=dict)

    def add(self, value: float, suffix: str = "", **labels: Any) -> None:
        key = (suffix, tuple(sorted((name, label(val)) for name, val in labels.items())))
        self._values[key] = self._values.get(key, 0) + value

    @property
    def samples(self) -> list[tuple[str, dict[str, str], float]]:
        return [(suffix, dict(labels), value) for (suffix, labels), value in self._values.items()]


def label(value: Any) -> str:
    """A label value that is an identifier or enum; ``other`` for anything else."""
    text = str(value) if value is not None else ""
    return text if _SAFE_LABEL.fullmatch(text) else "other"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _number(value: float) -> str:
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))


def _sample_order(family: Family) -> list[tuple[str, dict[str, str], float]]:
    """Samples by label set; a summary keeps its quantiles in ascending order, then sum, count."""

    def key(sample: tuple[str, dict[str, str], float]) -> tuple[Any, ...]:
        suffix, labels, _ = sample
        base = tuple(sorted((k, v) for k, v in labels.items() if k != "quantile"))
        rank = {"": 0, "_sum": 1, "_count": 2}.get(suffix, 3)
        return (base, rank, float(labels.get("quantile", "0")), suffix)

    return sorted(family.samples, key=key)


def format_families(families: Iterable[Family]) -> str:
    """The text exposition of ``families``, in the order given, samples in a stable order."""
    lines: list[str] = []
    for family in families:
        if not _METRIC_NAME.fullmatch(family.name):
            raise ValueError(f"invalid metric name: {family.name!r}")
        lines.append(f"# HELP {family.name} {_escape_help(family.help)}")
        lines.append(f"# TYPE {family.name} {family.type}")
        for suffix, labels, value in _sample_order(family):
            for name in labels:
                if not _LABEL_NAME.fullmatch(name) or name.startswith("__"):
                    raise ValueError(f"invalid label name: {name!r}")
            rendered = ",".join(f'{name}="{_escape_label(val)}"' for name, val in sorted(labels.items()))
            lines.append(f"{family.name}{suffix}{{{rendered}}} {_number(value)}" if rendered
                         else f"{family.name}{suffix} {_number(value)}")
    return "\n".join(lines) + "\n"


def collect(
    store: MetricsReader,
    *,
    profiles: Mapping[str, Profile],
    limits: Mapping[str, int],
    now: datetime,
) -> list[Family]:
    """Every family, in the order they are printed."""
    from .service import provider_availability, provider_eligible

    build = Family("taskspindle_build_info", "gauge",
                   "TaskSpindle build: package version and the schema version it migrates to.")
    build.add(1, version=taskspindle.__version__, schema=str(taskspindle.SCHEMA_VERSION))

    schema = Family("taskspindle_database_schema_version", "gauge",
                    "The schema version the task database is at; absent when there is no database.")
    if (version := store.schema_version()) is not None:
        schema.add(version)

    tasks = Family("taskspindle_tasks", "gauge", "Tasks on record, by provider, mode and current state.")
    for row in store.task_counts():
        tasks.add(int(row["count"]), provider=row["provider"], mode=row["mode"], state=row["state"])

    failures = Family("taskspindle_task_failures_total", "counter",
                      "FAILED tasks on record, by provider and error code.")
    by_code: dict[tuple[str, str], int] = {}
    for row in store.failure_counts():
        key = (label(row["provider"]), label(row["code"]))
        by_code[key] = by_code.get(key, 0) + int(row["count"])
    for (provider, code), count in by_code.items():
        failures.add(count, provider=provider, code=code)

    turns = Family("taskspindle_turns_total", "counter", "Turns on record, by provider.")
    unmetered = Family("taskspindle_turns_unmetered_total", "counter",
                       "Turns with no recorded token usage, by provider; token and cost totals omit them.")
    for row in store.metering_counts():
        turns.add(int(row["turns"]), provider=row["provider"])
        unmetered.add(int(row["unmetered_turns"]), provider=row["provider"])

    duration = Family(
        "taskspindle_turn_duration_seconds", "summary",
        "Finished turn wall time by provider; quantiles over the trailing 7 days, sum and count "
        "over every turn on record.",
    )
    recent: dict[str, list[float]] = {}
    cutoff = utilization.stamp(now - QUANTILE_SPAN)
    for provider, _mode, ms in store.turn_durations_ms(since=cutoff):
        recent.setdefault(label(provider), []).append(ms / 1000)
    totals: dict[str, list[float]] = {}
    for provider, _mode, ms in store.turn_durations_ms():
        totals.setdefault(label(provider), []).append(ms / 1000)
    for provider in sorted(set(totals) | set(recent)):
        values = recent.get(provider, [])
        for fraction in TURN_QUANTILES:
            duration.add(round(quantile(values, fraction), 3) if values else math.nan,
                         provider=provider, quantile=repr(fraction))
        duration.add(round(sum(totals.get(provider, [])), 3), "_sum", provider=provider)
        duration.add(len(totals.get(provider, [])), "_count", provider=provider)

    tokens = Family("taskspindle_tokens_total", "counter",
                    "Tokens reported by workers, by provider and kind (input, output, cache_read, "
                    "cache_write, reasoning).")
    cost = Family("taskspindle_estimated_cost_usd_total", "counter",
                  "Estimated cost at published API rates, by provider; an estimate, not a charge.")
    for row in store.usage_totals():
        for kind in _TOKEN_KINDS:
            tokens.add(int(row[f"{kind}_tokens"]), provider=row["provider"], kind=kind)
        cost.add(round(float(row["cost_estimate_usd"]), 6), provider=row["provider"])

    checks = Family("taskspindle_checks_total", "counter",
                    "Verification commands run, by provider and result (passed, failed).")
    by_result: dict[tuple[str, str], int] = {}
    for provider, ok, _ms in store.check_durations_ms():
        key = (label(provider), "passed" if ok else "failed")
        by_result[key] = by_result.get(key, 0) + 1
    for (provider, result), count in by_result.items():
        checks.add(count, provider=provider, result=result)

    leases = store.list_leases()
    queued = store.queued_counts(list(_DISPATCHABLE))
    providers = sorted(
        {label(name) for name in profiles} | {label(name) for name in limits}
        | {label(row["provider"]) for row in leases} | {label(name) for name in queued}
    )
    active: dict[str, int] = {}
    for row in leases:
        active[label(row["provider"])] = active.get(label(row["provider"]), 0) + 1
    waiting: dict[str, int] = {}
    for name, count in queued.items():
        waiting[label(name)] = waiting.get(label(name), 0) + int(count)
    safe_limits = {label(name): int(value) for name, value in limits.items()}

    slot_limit = Family("taskspindle_slot_limit", "gauge", "Concurrent turn slots in force, by provider.")
    active_leases = Family("taskspindle_active_leases", "gauge", "Slots held right now, by provider.")
    queue_depth = Family(
        "taskspindle_queue_depth", "gauge",
        "Tasks waiting for a slot (queued, repairing or resuming without a lease), by provider.",
    )
    for provider in providers:
        if provider in safe_limits:
            slot_limit.add(safe_limits[provider], provider=provider)
        active_leases.add(active.get(provider, 0), provider=provider)
        queue_depth.add(waiting.get(provider, 0), provider=provider)

    slot_use = Family("taskspindle_slot_utilization_ratio", "gauge",
                      "Slot-seconds held over slot-seconds offered at the current limit, trailing 24 hours.")
    start = now - UTILIZATION_SPAN
    busy = utilization.report(
        store.list_lease_history(since=utilization.stamp(start)), since=start, now=now, limits=limits,
    )
    for provider, info in busy.items():
        if info.get("slot_utilization") is not None:
            slot_use.add(float(info["slot_utilization"]), provider=provider)

    available = Family("taskspindle_provider_available", "gauge",
                       "1 when the provider would admit a turn now, else 0.")
    state = Family("taskspindle_provider_state", "gauge",
                   "The provider's availability state: 1 for the current state.")
    for profile in sorted(profiles.values(), key=lambda item: item.id):
        availability = provider_availability(store, profile, now=now, model=profile.model)
        available.add(1 if provider_eligible(availability, now) else 0, provider=profile.id)
        state.add(1, provider=profile.id, state=availability["state"])

    return [build, schema, tasks, failures, turns, unmetered, duration, tokens, cost, checks,
            slot_limit, active_leases, queue_depth, slot_use, available, state]


def render(
    store: MetricsReader,
    *,
    profiles: Mapping[str, Profile],
    limits: Mapping[str, int],
    now: datetime,
) -> str:
    """The whole exposition, ready to serve or print."""
    return format_families(collect(store, profiles=profiles, limits=limits, now=now))
