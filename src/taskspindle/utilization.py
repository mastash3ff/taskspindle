"""How much of each provider's slot capacity was actually used.

Everything here is derived on read from ``lease_history`` rows: a lease is deleted when it is
released, so the history table is the only record of what the pool did. Nothing is sampled and
nothing is stored, which keeps the numbers reproducible from the database alone.

These are scheduling figures for this host. They say how busy TaskSpindle's own slots were, not
how much of a provider's subscription quota is left.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["SPANS", "fanout_report", "pool", "report"]

SPANS: dict[str, timedelta] = {"day": timedelta(hours=24), "week": timedelta(days=7)}


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _wait_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean_s": None, "p50_s": None, "max_s": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean_s": round(sum(ordered) / len(ordered), 3),
        "p50_s": round(ordered[(len(ordered) - 1) // 2], 3),
        "max_s": round(ordered[-1], 3),
    }


def report(
    rows: Iterable[Mapping[str, Any]],
    *,
    since: datetime,
    now: datetime,
    limits: Mapping[str, int],
    queued: Mapping[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per-provider slot occupancy and queue wait between ``since`` and ``now``.

    ``slot_utilization`` is slot-seconds held over slot-seconds offered at the provider's
    *current* limit; a limit changed inside the window is not replayed. A hold still open is
    counted up to ``now`` only while its lease is live, so a row orphaned by a crash between the
    two writes cannot inflate the figure.
    """
    queued = queued or {}
    span = max((now - since).total_seconds(), 0.0)
    held: dict[str, float] = {}
    waits: dict[str, list[float]] = {}
    tallies: dict[str, dict[str, int]] = {}
    for row in rows:
        provider = str(row["provider"])
        acquired = _parse(row.get("acquired_at"))
        if acquired is None:
            continue
        released = _parse(row.get("released_at"))
        if released is None:
            if not row.get("live"):
                continue
            released = now
        start, end = max(acquired, since), min(released, now)
        if end > start:
            held[provider] = held.get(provider, 0.0) + (end - start).total_seconds()
        if acquired < since:
            continue
        tally = tallies.setdefault(provider, {"holds": 0, "peak_active": 0, "saturated_acquires": 0})
        tally["holds"] += 1
        active = int(row.get("active_at_acquire") or 0)
        tally["peak_active"] = max(tally["peak_active"], active)
        if active >= int(row.get("limit_at_acquire") or 1):
            tally["saturated_acquires"] += 1
        ready = _parse(row.get("ready_at"))
        if ready is not None and acquired >= ready:
            waits.setdefault(provider, []).append((acquired - ready).total_seconds())
    out: dict[str, dict[str, Any]] = {}
    for provider in sorted(set(limits) | set(held) | set(tallies) | set(queued)):
        limit = limits.get(provider)
        offered = (limit or 0) * span
        used = held.get(provider, 0.0)
        tally = tallies.get(provider, {"holds": 0, "peak_active": 0, "saturated_acquires": 0})
        out[provider] = {
            "limit": limit,
            "slot_seconds_used": round(used, 3),
            "slot_utilization": round(min(used / offered, 1.0), 4) if offered else None,
            **tally,
            "queued": int(queued.get(provider, 0)),
            "queue_wait": _wait_stats(waits.get(provider, [])),
        }
    return out


def pool(
    store: Any, *, now: datetime, limits: Mapping[str, int], queued_states: Sequence[str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """:func:`report` for the rolling day and week, from a store that has the history reads."""
    history = getattr(store, "list_lease_history", None)
    if history is None:
        return {}
    queued = store.queued_counts(queued_states)
    return {
        window: report(
            history(since=_stamp(now - span)), since=now - span, now=now, limits=limits, queued=queued,
        )
        for window, span in SPANS.items()
    }


def fanout_report(
    members: Iterable[Mapping[str, Any]], configured: Mapping[str, int]
) -> dict[str, dict[str, Any]]:
    """Per role: the fan-out asked for, and how wide the groups started under it actually were.

    ``members`` is one row per task in a fan-out group. A group's role is its first member's.
    """
    groups: dict[str, dict[str, Any]] = {}
    for row in members:
        group = groups.setdefault(str(row["fanout_group"]), {"role": row.get("role"), "providers": set()})
        group["providers"].add(row["provider"])
    out: dict[str, dict[str, Any]] = {
        role: {"configured": width, "groups": 0, "mean_width": None, "full_width_groups": 0}
        for role, width in configured.items()
    }
    widths: dict[str, list[int]] = {}
    for group in groups.values():
        widths.setdefault(group["role"] or "", []).append(len(group["providers"]))
    for role, sizes in widths.items():
        entry = out.setdefault(
            role, {"configured": None, "groups": 0, "mean_width": None, "full_width_groups": 0}
        )
        entry["groups"] = len(sizes)
        entry["mean_width"] = round(sum(sizes) / len(sizes), 2)
        wanted = entry["configured"]
        entry["full_width_groups"] = sum(1 for size in sizes if wanted and size >= wanted)
    return out
