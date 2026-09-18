"""What a turn cost, and the report that adds it up.

Usage is taken from the wire first: the Claude adapter settles every ``session/prompt`` with the
turn's own token counts, and Grok sends a ``turn_completed`` update carrying the same. Only when an
agent reported nothing does TaskSpindle read the Claude session file the adapter's own Claude Code
wrote -- and only the one file for this turn's session, never the rest of ``~/.claude/projects``.

Costs are estimates: the number is what the same tokens would have cost at published API rates.
OAuth sessions can consume provider-managed extra usage; an estimate is not proof of a charge. Grok
reports its own ``costUsdTicks`` whose unit is not documented; it is kept raw and never converted.
(Observed on grok 1.0.30: ticks == list-price USD x 3.4e9 with cached tokens counted inside
``inputTokens`` and reasoning inside ``outputTokens``, which is how the Grok row below is applied.)
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import limits
from .acp_client import TurnResult
from .providers import Profile
from .store import Store, UsageReader

__all__ = [
    "PRICES_USD_PER_MTOK",
    "PRICE_TABLE_VERSION",
    "Collected",
    "TurnUsage",
    "claude_session_file",
    "claude_session_model",
    "claude_session_path",
    "collect",
    "estimate_cost",
    "from_claude_session_file",
    "from_prompt_response",
    "from_turn_completed",
    "parse_since",
    "priced_as",
    "report",
    "reprice",
    "windows_report",
    "with_model",
]

#: The date of the published Anthropic price list the Claude rates were copied from. Rows from
#: another vendor carry their own date in ``_PRICE_AS_OF``.
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
    # xAI, https://docs.x.ai/docs/models read 2026-09-16: "grok-4.6 (< 200k prompt tokens) $2.00 /
    # $0.50 cached / $6.00". A request whose prompt reaches 200k tokens is billed at double these
    # rates; a turn's totals cannot tell, so the base tier is used and the estimate is a floor.
    "grok-4.6": {"input": 2.0, "output": 6.0, "cache_read": 0.5, "cache_write": 0.0},
    # Google, https://ai.google.dev/gemini-api/docs/pricing read 2026-09-16, paid Standard tier,
    # output "including thinking tokens". Antigravity picker ids add an effort suffix
    # (gemini-3.1-pro-high), which the prefix match absorbs. Pro is the <= 200k-token prompt tier
    # ($4.00 / $18.00 / $0.40 above it); Flash is the rate "through December 31, 2026".
    "gemini-3.1-pro": {"input": 2.0, "output": 12.0, "cache_read": 0.2, "cache_write": 0.0},
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75, "cache_read": 0.075, "cache_write": 0.0},
}

#: When a row that is not Anthropic's was copied, by model-id prefix.
_PRICE_AS_OF: dict[str, str] = {
    "grok-4.6": "2026-09-16",
    "gemini-3.1-pro": "2026-09-16",
    "gemini-3.8-flash": "2026-09-16",
}

#: Model-id prefixes whose reported input count already contains the cached tokens, so the cached
#: part is priced at the cache rate and only the remainder at the input rate.
_INPUT_INCLUDES_CACHE_READ = ("grok-",)

#: Where a usage record came from.
SOURCE_PROMPT_RESPONSE = "acp_prompt_response"
SOURCE_TURN_COMPLETED = "acp_turn_completed"
SOURCE_SESSION_FILE = "session_file"

_SINCE_SHORTHAND = re.compile(r"^(\d+)([smhd])$")
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


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
    prefix, prices = priced
    if prefix.startswith(_INPUT_INCLUDES_CACHE_READ):
        input_tokens = max((input_tokens or 0) - (cache_read_tokens or 0), 0)
    total = (
        (input_tokens or 0) * prices["input"]
        + (output_tokens or 0) * prices["output"]
        + (cache_read_tokens or 0) * prices["cache_read"]
        + (cache_write_tokens or 0) * prices["cache_write"]
    ) / 1_000_000
    return round(total, 6), _PRICE_AS_OF.get(prefix, PRICE_TABLE_VERSION)


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
        source="agy_cli_result" if "_agy_cli_cumulative" in usage else SOURCE_PROMPT_RESPONSE,
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
        price=True,
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


def priced_as(turn: TurnUsage, model: str | None) -> TurnUsage:
    """The same record, costed as ``model``, with its attribution left alone.

    For Antigravity the picker ID names the model TaskSpindle asked for, not the backend that
    answered: it is good enough to price a turn, and not good enough to claim the turn ran on it.
    A record that already has a cost keeps it.
    """
    if model is None or turn.cost_estimate_usd is not None:
        return turn
    cost, version = estimate_cost(
        model,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cache_read_tokens=turn.cache_read_tokens,
        cache_write_tokens=turn.cache_write_tokens,
    )
    if cost is None:
        return turn
    return TurnUsage(**{**asdict(turn), "cost_estimate_usd": cost, "price_table_version": version})


def with_model(turn: TurnUsage, model: str | None) -> TurnUsage:
    """The same counts attributed to ``model``, priced again now that the model is known."""
    if model is None or model == turn.model:
        return turn
    cost, version = estimate_cost(
        model,
        input_tokens=turn.input_tokens,
        output_tokens=turn.output_tokens,
        cache_read_tokens=turn.cache_read_tokens,
        cache_write_tokens=turn.cache_write_tokens,
    )
    fields = {**asdict(turn), "model": model, "cost_estimate_usd": cost, "price_table_version": version}
    return TurnUsage(**fields)


def claude_session_path(profile: Profile, cwd: Path, session_id: str | None, home: Path) -> Path | None:
    """Where this turn's Claude session record is, for a Claude-family profile; else None."""
    if profile.family != "claude" or not session_id:
        return None
    config_dir = Path(profile.env.get("CLAUDE_CONFIG_DIR") or (home / ".claude"))
    return claude_session_file(config_dir, cwd, session_id)


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
    session_model: str | None = None,
) -> Collected:
    """What this turn cost and which model answered, from the wire first and a file last."""
    capture = result.capture
    # Session configuration is a snapshot; per-turn wire IDs override it if the model changes.
    model: str | None = capture.model_ids[0] if capture.model_ids else session_model
    session_path = claude_session_path(profile, cwd, session_id, home)
    if session_path is not None and model is None:
        model = claude_session_model(session_path)
    usage: TurnUsage | None = None
    if capture.turn_completed is not None:
        usage = from_turn_completed(capture.turn_completed, model=model, duration_ms=duration_ms)
        if usage is not None and model is None:
            model = usage.model
    if model is None and profile.family != "agy":
        model = profile.model
        if usage is not None:
            usage = with_model(usage, model)
    if usage is None and result.usage:
        usage = from_prompt_response(
            result.usage, model=model, duration_ms=duration_ms, price=True
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
    if group_by == "repository_id":
        return {"repository_id": row.get("repository_id")}
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
    if group_by == "role":
        return {"provider": row["provider"], "role": row.get("role")}
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
    store: UsageReader,
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

    if group_by == "repository_id":
        repositories = {row["id"]: row for row in store.list_repositories()}
        for bucket in groups.values():
            repository = repositories.get(bucket["repository_id"], {})
            bucket["repository_path"] = repository.get("display_path") or None

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
            f"(price table {PRICE_TABLE_VERSION}); it is not a reported charge. OAuth sessions can "
            "consume provider-managed extra usage. Grok's cost ticks remain raw and unconverted."
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


def reprice(
    store: Store,
    *,
    since: str | None = None,
    provider: str | None = None,
    use_selected_model: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-run :func:`estimate_cost` over stored rows at the current price table.

    Every row's token counts are taken exactly as stored -- nothing is re-collected from a
    provider. This is both the one-off backfill for rows captured before their vendor was priced,
    and the standing answer to a price table that has since changed: run it again and any row
    whose stored cost no longer matches what the current table would charge is brought back into
    line, priced or not.

    A row with no ``model`` recorded (typically an Antigravity turn, whose picker ID names the
    model TaskSpindle asked for rather than the backend that answered, so it is not stored as
    attribution) is reported as unpriced with reason ``"no model recorded"`` -- never silently
    skipped, and never priced from a guess. ``use_selected_model`` is an explicit opt-in that
    prices such a row from its task's ``resolved_model`` instead, the same trust
    :func:`priced_as` already gives an Antigravity turn's own row: good enough to cost the turn,
    not good enough to claim the turn ran on it. The row's stored ``model`` column is left NULL
    either way; only the price columns are written.
    """
    rows = store.list_turn_usage(since=since, provider=provider)
    repriced: list[dict[str, Any]] = []
    unpriced: list[dict[str, Any]] = []
    by_version: dict[str, int] = {}
    unchanged = 0
    tasks: dict[str, Any] = {}

    for row in rows:
        model = row.get("model")
        model_source = "recorded" if model else None
        if model is None and use_selected_model:
            task_id = str(row["task_id"])
            if task_id not in tasks:
                tasks[task_id] = store.get_task(task_id)
            task = tasks[task_id]
            if task is not None and task.resolved_model:
                model = task.resolved_model
                model_source = "task.resolved_model"
        if model is None:
            unpriced.append(
                {
                    "turn_id": row["turn_id"], "task_id": row["task_id"],
                    "model": None, "reason": "no model recorded",
                }
            )
            continue
        cost, version = estimate_cost(
            model,
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            cache_read_tokens=row.get("cache_read_tokens"),
            cache_write_tokens=row.get("cache_write_tokens"),
        )
        if cost is None:
            unpriced.append(
                {
                    "turn_id": row["turn_id"], "task_id": row["task_id"],
                    "model": model, "reason": "model unknown to the price table",
                }
            )
            continue
        old_cost = row.get("cost_estimate_usd")
        old_cost = float(old_cost) if old_cost is not None else None
        if old_cost is not None and old_cost == cost and row.get("price_table_version") == version:
            unchanged += 1
            continue
        repriced.append(
            {
                "turn_id": row["turn_id"], "task_id": row["task_id"],
                "model": model, "model_source": model_source,
                "old_cost_estimate_usd": old_cost, "new_cost_estimate_usd": cost,
                "price_table_version": version,
            }
        )
        by_version[version] = by_version.get(version, 0) + 1
        if not dry_run:
            store.set_turn_price(int(row["turn_id"]), cost, version)

    return {
        "since": since,
        "provider": provider,
        "dry_run": dry_run,
        "use_selected_model": use_selected_model,
        "rows_examined": len(rows),
        "rows_repriced": len(repriced),
        "rows_unpriced": len(unpriced),
        "rows_unchanged": unchanged,
        "repriced": repriced,
        "unpriced": unpriced,
        "by_price_table_version": by_version,
        "cost_note": (
            "cost_estimate_usd is what the tokens would cost at published API rates from the "
            f"current price table (price_table_version records which; Claude's is {PRICE_TABLE_VERSION}); "
            "it is not a reported charge. OAuth sessions can consume provider-managed extra usage."
        ),
    }


_WINDOW_NOTES = {
    "claude": (
        "refreshed only while a Claude turn runs: the adapter forwards the SDK's rate_limit_event, "
        "and a usage-limit refusal records the window as rejected"
    ),
    "grok": (
        "Task refusals supply window evidence; optional native Grok billing checks are shown under Workers"
    ),
}


def windows_report(
    store: UsageReader, profiles: Mapping[str, Profile], now: datetime
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
                "status": limits.safe_status_row(status),
                "windows": store.latest_provider_windows(key),
            }
        )
    return entries
