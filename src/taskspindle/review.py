"""Turning a reviewer's answer into a structured verdict.

An agent asked for JSON will usually return JSON, and sometimes return JSON wrapped in a fence or
buried in a sentence. All three are accepted here; anything else is ``REVIEW_MALFORMED`` and the
review task fails rather than recording a verdict nobody wrote.

The disposition rules that decide whether a recorded review lets an acceptance through live in
:func:`taskspindle.service.validate_acceptance`. :func:`review_blocks_acceptance` is the same
question asked ahead of time, for a caller that wants to know before it composes a request.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from pydantic import ValidationError

from .models import ReviewOutput, Severity, Verdict

__all__ = [
    "REVIEW_MALFORMED",
    "ReviewParseError",
    "parse_review_output",
    "review_blocks_acceptance",
]

REVIEW_MALFORMED = "REVIEW_MALFORMED"

_FENCE = "```"


class ReviewParseError(Exception):
    """The reviewer's output was not a review."""

    def __init__(self, detail: str, *, code: str = REVIEW_MALFORMED) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _fenced_blocks(text: str) -> list[str]:
    """Return the contents of every ``` fence, innermost content only, in source order."""
    blocks: list[str] = []
    parts = text.split(_FENCE)
    # Odd-indexed parts sit between an opening and a closing fence.
    for part in parts[1::2]:
        body = part.split("\n", 1)[1] if "\n" in part else part
        blocks.append(body)
    return blocks


def _balanced_objects(text: str) -> list[str]:
    """Return every top-level ``{...}`` span in ``text``, honouring JSON string quoting."""
    spans: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                spans.append(text[start : index + 1])
    return spans


def _candidates(text: str) -> Iterator[str]:
    """Yield the substrings that might be the review object, best guess first."""
    stripped = text.strip()
    if stripped:
        yield stripped
    for block in reversed(_fenced_blocks(text)):
        candidate = block.strip()
        if candidate:
            yield candidate
    yield from reversed(_balanced_objects(text))


def parse_review_output(text: str) -> ReviewOutput:
    """Extract and validate the review object from whatever the reviewer said."""
    payload: dict[str, Any] | None = None
    for candidate in _candidates(text):
        try:
            decoded = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break
    if payload is None:
        raise ReviewParseError("no JSON object found in the review output")

    try:
        review = ReviewOutput(**payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "<root>"
        raise ReviewParseError(
            f"review output did not validate ({exc.error_count()} errors); {where}: {first['msg']}"
        ) from exc

    ids = [finding.id for finding in review.findings]
    if any(not finding_id.strip() for finding_id in ids):
        raise ReviewParseError("every finding needs a non-empty id")
    duplicates = sorted({finding_id for finding_id in ids if ids.count(finding_id) > 1})
    if duplicates:
        raise ReviewParseError(f"duplicate finding ids: {', '.join(duplicates)}")
    return review


def review_blocks_acceptance(review: ReviewOutput) -> list[str]:
    """Return what an acceptance would have to override, empty when nothing blocks.

    Mirrors the rule :func:`taskspindle.service.validate_acceptance` enforces: a BLOCK verdict
    makes every finding blocking, a critical finding blocks on its own, and a BLOCK verdict with
    no findings at all blocks under the verdict's own name because there is nothing to override.
    """
    blocking = sorted(
        finding.id
        for finding in review.findings
        if review.verdict is Verdict.BLOCK or finding.severity is Severity.CRITICAL
    )
    if not blocking and review.verdict is Verdict.BLOCK:
        return [review.verdict.value]
    return blocking
