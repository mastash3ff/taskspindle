"""Slot occupancy and queue wait, derived from lease history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from taskspindle import utilization

NOW = datetime(2030, 1, 8, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=1)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hold(provider, start_min, end_min, *, ready_min=None, limit=2, active=1, live=False):
    return {
        "provider": provider,
        "ready_at": None if ready_min is None else _stamp(SINCE + timedelta(minutes=ready_min)),
        "acquired_at": _stamp(SINCE + timedelta(minutes=start_min)),
        "released_at": None if end_min is None else _stamp(SINCE + timedelta(minutes=end_min)),
        "limit_at_acquire": limit,
        "active_at_acquire": active,
        "live": live,
    }


def test_occupancy_is_held_slot_seconds_over_offered_slot_seconds() -> None:
    rows = [
        _hold("claude", 0, 30, ready_min=0),
        _hold("claude", 10, 40, ready_min=4, active=2),
    ]
    out = utilization.report(rows, since=SINCE, now=NOW, limits={"claude": 2, "grok": 4},
                             queued={"grok": 3})

    claude = out["claude"]
    assert claude["slot_seconds_used"] == 3600.0
    assert claude["slot_utilization"] == 0.5
    assert (claude["holds"], claude["peak_active"], claude["saturated_acquires"]) == (2, 2, 1)
    assert claude["queue_wait"] == {"count": 2, "mean_s": 180.0, "p50_s": 0.0, "max_s": 360.0}
    # A provider with a limit and no history still reports, at zero.
    assert out["grok"]["slot_utilization"] == 0.0
    assert out["grok"]["queued"] == 3


def test_holds_are_clipped_to_the_window_and_open_ones_need_a_live_lease() -> None:
    rows = [
        _hold("claude", -30, 15),              # began before the window: only 15 minutes count
        _hold("claude", 50, None, live=True),  # still running: counted up to now
        _hold("claude", 5, None, live=False),  # orphaned open row: ignored entirely
    ]
    claude = utilization.report(rows, since=SINCE, now=NOW, limits={"claude": 1})["claude"]

    assert claude["slot_seconds_used"] == 25 * 60
    # The hold that began before the window is occupancy, but not an acquisition inside it.
    assert claude["holds"] == 1


def test_a_provider_without_a_limit_reports_no_ratio() -> None:
    out = utilization.report([_hold("agy", 0, 10)], since=SINCE, now=NOW, limits={})
    assert out["agy"]["limit"] is None
    assert out["agy"]["slot_utilization"] is None
