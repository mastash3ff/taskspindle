"""Request validation rules that can be decided without any IO."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from taskspindle.models import (
    Disposition,
    FindingDisposition,
    Mode,
    ReviewTarget,
    StartTaskRequest,
)

IMPLEMENT_FIELDS = {
    "provider": "claude",
    "mode": Mode.IMPLEMENT,
    "prompt": "add the widget",
    "repository": "/repo",
    "acceptance_criteria": "the widget exists",
    "path_prefixes": ["src/"],
    "verification_commands": ["pytest -q"],
    "candidate_message": "Add the widget",
}


def test_consult_without_repository_is_valid() -> None:
    request = StartTaskRequest(provider="grok", mode=Mode.CONSULT, prompt="what shape?")
    assert request.repository is None
    assert request.timeout_s == 1800


@pytest.mark.parametrize(
    "missing", ["repository", "acceptance_criteria", "path_prefixes", "verification_commands"]
)
def test_implement_requires_its_fields(missing: str) -> None:
    fields = dict(IMPLEMENT_FIELDS)
    fields[missing] = None
    with pytest.raises(ValidationError, match="implement mode requires"):
        StartTaskRequest(**fields)


def test_implement_rejects_empty_path_prefixes() -> None:
    with pytest.raises(ValidationError, match="implement mode requires"):
        StartTaskRequest(**{**IMPLEMENT_FIELDS, "path_prefixes": []})


def test_implement_accepts_empty_verification_commands() -> None:
    request = StartTaskRequest(**{**IMPLEMENT_FIELDS, "verification_commands": []})
    assert request.verification_commands == []


def test_candidate_message_must_be_one_short_line() -> None:
    with pytest.raises(ValidationError, match="at most 72 characters"):
        StartTaskRequest(**{**IMPLEMENT_FIELDS, "candidate_message": "x" * 73})
    with pytest.raises(ValidationError, match="single line"):
        StartTaskRequest(**{**IMPLEMENT_FIELDS, "candidate_message": "subject\nbody"})


@pytest.mark.parametrize("prefix", ["/etc", "../secrets", "src/../../etc", ""])
def test_bad_path_prefixes_are_rejected(prefix: str) -> None:
    with pytest.raises(ValidationError):
        StartTaskRequest(**{**IMPLEMENT_FIELDS, "path_prefixes": [prefix]})


def test_dot_path_prefix_is_allowed() -> None:
    request = StartTaskRequest(**{**IMPLEMENT_FIELDS, "path_prefixes": ["."]})
    assert request.path_prefixes == ["."]


def test_review_requires_a_target() -> None:
    with pytest.raises(ValidationError, match="review mode requires review_target"):
        StartTaskRequest(provider="grok", mode=Mode.REVIEW, prompt="look")
    request = StartTaskRequest(
        provider="grok",
        mode=Mode.REVIEW,
        prompt="look",
        review_target=ReviewTarget(kind="candidate", task_id="ts_abc", candidate_sha="deadbeef"),
    )
    assert request.review_target is not None


def test_candidate_review_target_needs_its_fields() -> None:
    with pytest.raises(ValidationError, match="candidate review target"):
        ReviewTarget(kind="candidate", task_id="ts_abc")


def test_timeout_bounds() -> None:
    with pytest.raises(ValidationError):
        StartTaskRequest(provider="claude", mode=Mode.CONSULT, prompt="hi", timeout_s=59)
    with pytest.raises(ValidationError):
        StartTaskRequest(provider="claude", mode=Mode.CONSULT, prompt="hi", timeout_s=14401)


def test_disposition_reason_required_unless_fixed() -> None:
    assert FindingDisposition(finding_id="f1", disposition=Disposition.FIXED).reason is None
    with pytest.raises(ValidationError, match="requires a reason"):
        FindingDisposition(finding_id="f1", disposition=Disposition.OVERRIDDEN)
    assert FindingDisposition(
        finding_id="f1", disposition=Disposition.ACCEPTED_RISK, reason="tracked in #12"
    ).reason
