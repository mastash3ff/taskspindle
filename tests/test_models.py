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
    assert request.role is None


@pytest.mark.parametrize("role", ["Planner", "a" * 33, "-x", "has space", ""])
def test_role_must_be_a_short_lowercase_key(role: str) -> None:
    with pytest.raises(ValidationError):
        StartTaskRequest(provider="grok", mode=Mode.CONSULT, prompt="q", role=role)


def test_review_kind_defaults_to_standard() -> None:
    request = StartTaskRequest(
        provider="grok",
        mode=Mode.REVIEW,
        prompt="q",
        review_target=ReviewTarget(kind="candidate", task_id="ts_1", candidate_sha="a" * 40),
    )
    assert request.review_kind == "standard"
    assert request.model_copy(update={"review_kind": "adversarial"}).review_kind == "adversarial"


CONSULT_FIELDS = {"provider": "grok", "mode": Mode.CONSULT, "prompt": "q"}


@pytest.mark.parametrize("fields", [CONSULT_FIELDS, IMPLEMENT_FIELDS])
def test_a_non_standard_review_kind_is_review_only(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="review only"):
        StartTaskRequest(**fields, review_kind="adversarial")


def test_an_unknown_review_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StartTaskRequest(
            provider="grok",
            mode=Mode.REVIEW,
            prompt="q",
            review_target=ReviewTarget(kind="candidate", task_id="ts_1", candidate_sha="a" * 40),
            review_kind="hostile",
        )


def test_context_files_default_to_none_and_deduplicate() -> None:
    assert StartTaskRequest(**CONSULT_FIELDS).context_files is None
    request = StartTaskRequest(**CONSULT_FIELDS, context_files=["/a/b.md", "/c.md", "/a/b.md"])
    assert request.context_files == ["/a/b.md", "/c.md"]
    assert StartTaskRequest(**IMPLEMENT_FIELDS, context_files=["/a.md"]).context_files == ["/a.md"]


@pytest.mark.parametrize(
    "paths",
    [["relative.md"], ["/a/../etc/passwd"], [""], ["/has\x00nul"], [f"/f{i}" for i in range(33)]],
)
def test_bad_context_file_paths_are_rejected(paths: list[str]) -> None:
    with pytest.raises(ValidationError):
        StartTaskRequest(**CONSULT_FIELDS, context_files=paths)


def test_role_is_recorded_as_given() -> None:
    request = StartTaskRequest(provider="grok", mode=Mode.CONSULT, prompt="q", role="code-reviewer_2")
    assert request.role == "code-reviewer_2"


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
