"""What a turn cost, and the report that adds it up.

Usage is taken from the wire first: the Claude adapter settles every ``session/prompt`` with the
turn's own token counts, and Grok sends a ``turn_completed`` update carrying the same. Only when an
agent reported nothing does TaskSpindle read the Claude session file the adapter's own Claude Code
wrote -- and only the one file for this turn's session, never the rest of ``~/.claude/projects``.

Costs are estimates. A subscription seat has no per-token bill; the number is what the same tokens
would have cost at the published API rates, so that seat usage can be compared and budgeted. Grok
reports its own ``costUsdTicks`` whose unit is not documented; it is kept raw and never converted.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from . import limits
from .acp_client import TurnResult
from .providers import Profile
from .store import Store

__all__ = [
    "PRICES_USD_PER_MTOK",
    "PRICE_TABLE_VERSION",
    "Collected",
    "TurnUsage",
    "claude_session_file",
    "claude_session_model",
    "collect",
    "estimate_cost",
    "from_claude_session_file",
    "from_prompt_response",
    "from_turn_completed",
    "parse_since",
    "report",
    "windows_report",
]

#: The date of the published Anthropic price list these rates were copied from.
PRICE_TABLE_VERSION = "2026-06-24"

#: USD per million tokens, by model-id prefix. Cache reads are 0.1x the input rate (0.025x on
#: Claude Fable 5.1); cache writes are the 5-minute-TTL rate of 1.25x the input rate.
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
}

#: Where a usage record came from.
SOURCE_PROMPT_RESPONSE = "acp_prompt_response"
SOURCE_TURN_COMPLETED = "acp_turn_completed"
SOURCE_SESSION_FILE = "session_file"

_SINCE_SHORTHAND = re.compile(r"^(\d+)([smhd])$")
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

GroupBy = Literal["provider", "day", "provider_day", "model", "mode"]


@dataclass(frozen=True)
class TurnUsage:
    """One turn's token counts, ready to be written to ``turn_usage``."""

    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    model_calls: int | None
    duration_ms: int | None
    cost_estimate_usd: float | None
    cost_is_estimate: bool
    price_table_version: str | None
    source: str
    raw: dict[str, Any] | None

    def as_fields(self) -> dict[str, Any]:
        """The keyword arguments :meth:`Store.insert_turn_usage` takes."""
        return asdict(self)


@dataclass(frozen=True)
class Collected:
    """What :func:`collect` learned about a turn."""

    usage: TurnUsage | None
    model: str | None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _price_for(model: str | None) -> tuple[str, dict[str, float]] | None:
    """The price row whose model-id prefix matches ``model``; the longest prefix wins."""
    if not model:
        return None
    best: tuple[str, dict[str, float]] | None = None
    for prefix, prices in PRICES_USD_PER_MTOK.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, prices)
    return best


def estimate_cost(
    model: str | None,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
) -> tuple[float | None, str | None]:
    """``(usd, price_table_version)`` at the published rates, or ``(None, None)`` for an unknown model."""
    priced = _price_for(model)
    if priced is None:
        return None, None
    _, prices = priced
    total = (
        (input_tokens or 0) * prices["input"]
        + (output_tokens or 0) * prices["output"]
        + (cache_read_tokens or 0) * prices["cache_read"]
        + (cache_write_tokens or 0) * prices["cache_write"]
    ) / 1_000_000
    return round(total, 6), PRICE_TABLE_VERSION


def _build(
    *,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    reasoning_tokens: int | None,
    model_calls: int | None,
    duration_ms: int | None,
    source: str,
    raw: Mapping[str, Any] | None,
    price: bool,
) -> TurnUsage:
    cost, version = (
        estimate_cost(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        if price
        else (None, None)
    )
    return TurnUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        model_calls=model_calls,
        duration_ms=duration_ms,
        cost_estimate_usd=cost,
        cost_is_estimate=True,
        price_table_version=version,
        source=source,
        raw=dict(raw) if raw is not None else None,
    )


def from_prompt_response(
    usage: Mapping[str, Any], *, model: str | None, duration_ms: int | None, price: bool = True
) -> TurnUsage:
    """The Claude adapter's per-turn ``usage`` on the prompt response (snake_case, ACP 0.12)."""
    return _build(
        model=model,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cached_read_tokens")),
        cache_write_tokens=_int(usage.get("cached_write_tokens")),
        reasoning_tokens=_int(usage.get("thought_tokens")),
        model_calls=None,
        duration_ms=duration_ms,
        source=SOURCE_PROMPT_RESPONSE,
        raw=usage,
        price=price,
    )


def from_turn_completed(
    update: Mapping[str, Any], *, model: str | None, duration_ms: int | None
) -> TurnUsage | None:
    """Grok's ``turn_completed`` update. Its ``costUsdTicks`` stays in ``raw``, unconverted."""
    usage = update.get("usage")
    if not isinstance(usage, Mapping):
        return None
    model_usage = usage.get("modelUsage")
    if model is None and isinstance(model_usage, Mapping) and model_usage:
        model = str(next(iter(model_usage)))
    api_ms = _int(usage.get("apiDurationMs"))
    return _build(
        model=model,
        input_tokens=_int(usage.get("inputTokens")),
        output_tokens=_int(usage.get("outputTokens")),
        cache_read_tokens=_int(usage.get("cachedReadTokens")),
        cache_write_tokens=_int(usage.get("cacheCreationTokens")),
        reasoning_tokens=_int(usage.get("reasoningTokens")),
        model_calls=_int(usage.get("modelCalls")),
        duration_ms=duration_ms if duration_ms is not None else api_ms,
        source=SOURCE_TURN_COMPLETED,
        raw=usage,
        price=False,
    )


# -- the Claude session file ------------------------------------------------------------------


def claude_session_file(config_dir: Path, cwd: Path, session_id: str) -> Path:
    """Where the adapter's Claude Code wrote this session: ``projects/<encoded cwd>/<id>.jsonl``.

    Claude Code encodes the working directory by replacing every ``/``, ``.`` and ``_`` with
    ``-`` (verified against ``~/.claude/projects`` on 2.1.x).
    """
    encoded = re.sub(r"[/._]", "-", str(cwd))
    return config_dir / "projects" / encoded / f"{session_id}.jsonl"


def _assistant_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("type") == "assistant":
                    records.append(record)
    except OSError:
        return []
    return records


def claude_session_model(path: Path) -> str | None:
    """The model of the last assistant message in a Claude session file."""
    for record in reversed(_assistant_records(path)):
        message = record.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model:
            return model
    return None


def from_claude_session_file(
    path: Path, *, since: str | None, duration_ms: int | None
) -> TurnUsage | None:
    """Sum the assistant messages of one session file written at or after ``since``.

    Claude Code repeats a request's record when it streams, with the same ``requestId`` and the
    same usage, so records are counted once per request id.
    """
    seen: set[str] = set()
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    model: str | None = None
    counted = 0
    for record in _assistant_records(path):
        stamp = record.get("timestamp")
        if since and isinstance(stamp, str) and stamp < since:
            continue
        key = str(record.get("requestId") or record.get("uuid") or id(record))
        if key in seen:
            continue
        seen.add(key)
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        counted += 1
        totals["input"] += _int(usage.get("input_tokens")) or 0
        totals["output"] += _int(usage.get("output_tokens")) or 0
        totals["cache_read"] += _int(usage.get("cache_read_input_tokens")) or 0
        totals["cache_write"] += _int(usage.get("cache_creation_input_tokens")) or 0
        found = message.get("model")
        if isinstance(found, str) and found:
            model = found
    if counted == 0:
        return None
    return _build(
        model=model,
        input_tokens=totals["input"],
        output_tokens=totals["output"],
        cache_read_tokens=totals["cache_read"],
        cache_write_tokens=totals["cache_write"],
        reasoning_tokens=None,
        model_calls=counted,
        duration_ms=duration_ms,
        source=SOURCE_SESSION_FILE,
        raw={"records": counted, "path": str(path)},
        price=True,
    )


# -- the collector the runner calls -----------------------------------------------------------


def collect(
    result: TurnResult,
    *,
    profile: Profile,
    cwd: Path,
    session_id: str | None,
    home: Path,
    duration_ms: int | None,
    started_at: str | None = None,
) -> Collected:
    """What this turn cost and which model answered, from the wire first and a file last."""
    capture = result.capture
    model: str | None = capture.model_ids[0] if capture.model_ids else None
    session_path: Path | None = None
    if profile.family == "claude" and session_id:
        config_dir = Path(profile.env.get("CLAUDE_CONFIG_DIR") or (home / ".claude"))
        session_path = claude_session_file(config_dir, cwd, session_id)
        if model is None:
            model = claude_session_model(session_path)
    if model is None:
        model = profile.model

    usage: TurnUsage | None = None
    if capture.turn_completed is not None:
        usage = from_turn_completed(capture.turn_completed, model=model, duration_ms=duration_ms)
        if usage is not None and model is None:
            model = usage.model
    if usage is None and result.usage:
        priced = profile.family == "claude" or profile.auth == "api_key"
        usage = from_prompt_response(
            result.usage, model=model, duration_ms=duration_ms, price=priced
        )
    if usage is None and session_path is not None:
        usage = from_claude_session_file(session_path, since=started_at, duration_ms=duration_ms)
        if usage is not None and model is None:
            model = usage.model
    return Collected(usage=usage, model=model)


# -- the report ---------------------------------------------------------------------------------


def parse_since(text: str | None, now: datetime | None = None) -> str | None:
    """``7d`` / ``24h`` / ``30m`` / ``90s`` relative to now, an ISO-8601 stamp as given, or None."""
    if not text:
        return None
    match = _SINCE_SHORTHAND.match(text.strip())
    if match:
        amount, unit = int(match.group(1)), _SINCE_UNITS[match.group(2)]
        moment = (now or datetime.now(UTC)) - timedelta(**{unit: amount})
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"since must be ISO-8601 or like 7d, 24h, 30m: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _group_key(row: Mapping[str, Any], mode_by_task: Mapping[str, str], group_by: str) -> dict[str, Any]:
    day = str(row.get("captured_at") or "")[:10]
    if group_by == "provider":
        return {"provider": row["provider"]}
    if group_by == "day":
        return {"day": day}
    if group_by == "provider_day":
        return {"provider": row["provider"], "day": day}
    if group_by == "model":
        return {"provider": row["provider"], "model": row.get("model")}
    if group_by == "mode":
        return {"provider": row["provider"], "mode": mode_by_task.get(str(row["task_id"]))}
    raise ValueError(f"unknown group_by: {group_by!r}")


def _stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean_ms": None, "p50_ms": None, "max_ms": None}
    return {
        "count": len(values),
        "mean_ms": int(statistics.fmean(values)),
        "p50_ms": int(statistics.median(values)),
        "max_ms": max(values),
    }


def report(
    store: Store,
    *,
    since: str | None = None,
    provider: str | None = None,
    group_by: str = "provider",
    profiles: Mapping[str, Profile] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Tokens, estimated cost, outcomes, timings, violations and windows, rolled up on read."""
    moment = now or datetime.now(UTC)
    rows = store.list_turn_usage(since=since, provider=provider)
    mode_by_task: dict[str, str] = {}
    if group_by == "mode":
        for row in rows:
            task = store.get_task(str(row["task_id"]))
            if task is not None:
                mode_by_task[task.id] = task.mode.value

    groups: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
    for row in rows:
        key = _group_key(row, mode_by_task, group_by)
        bucket = groups.setdefault(
            tuple(sorted(key.items())),
            {
                **key,
                "turns": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "cost_estimate_usd": 0.0,
                "priced_turns": 0,
            },
        )
        bucket["turns"] += 1
        for column in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            bucket[column] += int(row.get(column) or 0)
        cost = row.get("cost_estimate_usd")
        if cost is not None:
            bucket["cost_estimate_usd"] = round(bucket["cost_estimate_usd"] + float(cost), 6)
            bucket["priced_turns"] += 1

    outcomes = [
        row
        for row in store.task_counts(since=since)
        if provider is None or row["provider"] == provider
    ]
    durations = store.turn_durations_ms(since=since, provider=provider)
    by_provider: dict[str, list[int]] = {}
    for prov, _mode, ms in durations:
        by_provider.setdefault(prov, []).append(ms)
    checks = [
        (prov, ok, ms)
        for prov, ok, ms in store.check_durations_ms(since=since)
        if provider is None or prov == provider
    ]
    violations = [
        row
        for row in store.violation_counts(since=since)
        if provider is None or row["provider"] == provider
    ]
    return {
        "since": since,
        "provider": provider,
        "group_by": group_by,
        "generated_at": moment.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "cost_note": (
            "cost_estimate_usd is what the tokens would cost at the published API rates "
            f"(price table {PRICE_TABLE_VERSION}); subscription seats are not billed per token, "
            "and Grok's own cost figure is kept raw and not converted."
        ),
        "usage": list(groups.values()),
        "outcomes": outcomes,
        "turns": {**_stats([ms for _, _, ms in durations]), "by_provider": {
            prov: _stats(values) for prov, values in sorted(by_provider.items())
        }},
        "checks": {
            **_stats([ms for _, _, ms in checks]),
            "passed": sum(1 for _, ok, _ in checks if ok),
        },
        "violations": violations,
        "windows": windows_report(store, profiles or {}, moment),
    }


_WINDOW_NOTES = {
    "claude": (
        "refreshed only while a Claude turn runs: the adapter forwards the SDK's rate_limit_event, "
        "and a usage-limit refusal records the window as rejected"
    ),
    "grok": "Grok 1.0.13 emits no window telemetry; only a refusal is observed",
}


def windows_report(
    store: Store, profiles: Mapping[str, Profile], now: datetime
) -> list[dict[str, Any]]:
    """Per provider: what is known about its usage windows, and how much can be known at all."""
    keys: dict[str, str] = {}
    for profile in profiles.values():
        keys.setdefault(limits.status_key(profile), profile.family)
    for row in store.list_provider_status():
        keys.setdefault(str(row["provider"]), str(row["provider"]))
    entries: list[dict[str, Any]] = []
    for key in sorted(keys):
        family = keys[key]
        status = store.get_provider_status(key)
        entries.append(
            {
                "provider": key,
                "state": limits.effective_state(status, now),
                "observable": family == "claude",
                "note": _WINDOW_NOTES.get(family, "no window telemetry is documented for this agent"),
                "status": status,
                "windows": store.latest_provider_windows(key),
            }
        )
    return entries
