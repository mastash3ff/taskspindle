"""Parsing a reviewer's answer, and asking what it would block."""

from __future__ import annotations

import json

import pytest

from taskspindle.models import Severity, Verdict
from taskspindle.review import ReviewParseError, parse_review_output, review_blocks_acceptance

PASS_REVIEW = {
    "verdict": "PASS",
    "summary": "the change is small and covered",
    "findings": [],
    "checks": ["read the diff", "ran the tests"],
}


def finding(finding_id: str, severity: str = "low") -> dict[str, object]:
    return {
        "id": finding_id,
        "severity": severity,
        "path": "src/thing.py",
        "line": 12,
        "evidence": "the lock is taken twice",
        "remedy": "take it once",
    }


def test_a_bare_json_object_is_accepted() -> None:
    review = parse_review_output(json.dumps(PASS_REVIEW))
    assert review.verdict is Verdict.PASS
    assert review.checks == ["read the diff", "ran the tests"]


def test_json_inside_a_fence_is_accepted() -> None:
    text = f"Here is my review:\n\n```json\n{json.dumps(PASS_REVIEW)}\n```\n"
    assert parse_review_output(text).verdict is Verdict.PASS


def test_the_last_json_object_embedded_in_prose_wins() -> None:
    body = dict(PASS_REVIEW, verdict="CONCERN", findings=[finding("F1", "medium")])
    text = (
        'I first considered {"verdict": "PASS"} but that was wrong.\n'
        f"My answer is {json.dumps(body)}\nThank you.\n"
    )
    review = parse_review_output(text)
    assert review.verdict is Verdict.CONCERN
    assert [item.id for item in review.findings] == ["F1"]


def test_output_with_no_json_object_is_malformed() -> None:
    with pytest.raises(ReviewParseError) as excinfo:
        parse_review_output("I could not review this, sorry.")
    assert excinfo.value.code == "REVIEW_MALFORMED"


def test_a_json_object_that_is_not_a_review_is_malformed() -> None:
    with pytest.raises(ReviewParseError) as excinfo:
        parse_review_output(json.dumps({"verdict": "MAYBE", "summary": "hmm"}))
    assert excinfo.value.code == "REVIEW_MALFORMED"
    assert "verdict" in excinfo.value.detail


def test_duplicate_finding_ids_are_rejected() -> None:
    body = dict(PASS_REVIEW, verdict="CONCERN", findings=[finding("F1"), finding("F1", "high")])
    with pytest.raises(ReviewParseError) as excinfo:
        parse_review_output(json.dumps(body))
    assert "duplicate finding ids: F1" in excinfo.value.detail


def test_blank_finding_ids_are_rejected() -> None:
    body = dict(PASS_REVIEW, verdict="CONCERN", findings=[finding("  ")])
    with pytest.raises(ReviewParseError):
        parse_review_output(json.dumps(body))


def test_review_blocks_acceptance_for_block_and_for_critical_findings() -> None:
    clean = parse_review_output(json.dumps(PASS_REVIEW))
    assert review_blocks_acceptance(clean) == []

    critical = parse_review_output(
        json.dumps(dict(PASS_REVIEW, verdict="CONCERN", findings=[finding("F1"), finding("F2", "critical")]))
    )
    assert review_blocks_acceptance(critical) == ["F2"]
    assert critical.findings[1].severity is Severity.CRITICAL

    blocked = parse_review_output(
        json.dumps(dict(PASS_REVIEW, verdict="BLOCK", findings=[finding("F1"), finding("F2")]))
    )
    assert review_blocks_acceptance(blocked) == ["F1", "F2"]

    empty_block = parse_review_output(json.dumps(dict(PASS_REVIEW, verdict="BLOCK")))
    assert review_blocks_acceptance(empty_block) == ["BLOCK"]
