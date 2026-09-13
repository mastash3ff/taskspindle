"""Human-readable provider status must expose the decisions operators can act on."""

import pytest

from taskspindle import cli


@pytest.mark.parametrize(
    ("state", "remaining", "next_at", "reason", "active"),
    [
        ("cooldown", 2, "2026-09-13T01:15:00Z", None, None),
        ("held", 0, None, "retry_budget_exhausted", None),
        ("trial_running", 1, None, None, "ts_recovery"),
        ("trial_ready", 0, None, "positive_evidence", None),
    ],
)
def test_hybrid_status_shows_retry_decision_without_manual_permit_instructions(
    capsys, state, remaining, next_at, reason, active,
):
    # Omitting the hybrid renderer hides the hold/cooldown and misleadingly offers a manual permit.
    cli._print_provider_recovery({
        "automatic_recovery": {
            "policy": "hybrid", "state": state, "attempts_used": 3 - remaining,
            "attempts_remaining": remaining, "next_attempt_at": next_at,
            "hold_reason": reason, "active_task_id": active,
            "episode_id": "episode_123", "evidence_revision": "evidence_456",
        },
        "recovery": {"state": "none", "next_action": "arm"},
    }, indent="  ")
    output = capsys.readouterr().out
    assert f"Hybrid recovery: {state}" in output
    assert f"Recovery retries remaining: {remaining}" in output
    assert "episode_123" in output and "evidence_456" in output
    for expected in (next_at, reason, active):
        if expected:
            assert expected in output
    assert "Recovery permit:" not in output
    assert "Recovery next action: arm" not in output


def test_manual_status_keeps_existing_permit_visible(capsys):
    cli._print_provider_recovery({
        "automatic_recovery": {"policy": "manual"},
        "recovery": {"state": "armed", "permit_id": "permit_123", "next_action": "use_permit"},
    }, indent="")
    output = capsys.readouterr().out
    assert "Recovery permit: permit_123" in output
    assert "Recovery next action: use_permit" in output
