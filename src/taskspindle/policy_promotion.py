"""Stage and apply a dispatch-policy model refresh with explicit runtime guards.

This module does no provider probing or image inspection. The operator supplies independently
verified identities; the caller must close admission before either policy write.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import enrollment, policy
from .providers import ProfileError
from .store import PolicyRevisionConflict


class PromotionError(ValueError):
    """A promotion precondition failed before a policy write."""


def _digest(value: str, name: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise PromotionError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _candidate_policy(
    previous: policy.DispatchPolicy, candidates: Mapping[str, Mapping[str, Any]],
    profiles: Mapping[str, Any],
    verified_catalogs: Mapping[str, Mapping[str, Any]] | None = None,
) -> policy.DispatchPolicy:
    if not candidates:
        raise PromotionError("at least one candidate selection is required")
    updated = previous.model_copy(deep=True)
    for provider, catalog in (verified_catalogs or {}).items():
        if provider not in profiles or provider not in updated.providers:
            raise PromotionError(f"unknown catalog provider: {provider!r}")
        try:
            enrollment.require_promotable(profiles[provider])
        except ProfileError as exc:
            raise PromotionError(str(exc)) from exc
        if not isinstance(catalog, Mapping) or set(catalog) != {
            "advertised_models", "advertised_efforts", "source", "evidence_sha256",
        }:
            raise PromotionError("catalog requires models, efforts, source, and evidence_sha256")
        if not isinstance(catalog["source"], str) or not catalog["source"].strip():
            raise PromotionError("catalog source is required")
        _digest(catalog["evidence_sha256"], "catalog evidence_sha256")
        payload = updated.providers[provider].model_dump(mode="json")
        payload.update({key: catalog[key] for key in ("advertised_models", "advertised_efforts")})
        try:
            updated.providers[provider] = policy.ProviderPolicy.model_validate(payload)
        except Exception as exc:
            raise PromotionError(f"invalid catalog: {exc}") from exc
    for role, selections in candidates.items():
        if role not in updated.roles or not selections:
            raise PromotionError(f"unknown role or empty selections: {role!r}")
        for provider, raw in selections.items():
            profile = profiles.get(provider)
            advertised = updated.providers.get(provider)
            if profile is None or advertised is None:
                raise PromotionError(f"unknown provider: {provider!r}")
            try:
                enrollment.require_promotable(profile)
            except ProfileError as exc:
                raise PromotionError(str(exc)) from exc
            try:
                selection = policy.Selection.model_validate(raw)
            except Exception as exc:
                raise PromotionError(f"invalid selection for {role}.{provider}: {exc}") from exc
            if selection.model is None or selection.model not in advertised.advertised_models:
                raise PromotionError(f"{role}.{provider} model is not advertised")
            if selection.effort is not None and selection.effort not in advertised.advertised_efforts:
                raise PromotionError(f"{role}.{provider} effort is not advertised")
            updated.roles[role].selections[provider] = selection
    errors = policy.validate(updated, profiles)
    if errors:
        raise PromotionError(f"candidate policy is invalid: {errors}")
    return updated


def stage(
    path: Path | str, store: Any, profiles: Mapping[str, Any], *, source_commit: str,
    image_identity: str, binary_digests: Mapping[str, str],
    expected_revision: int, expected_fingerprint: str,
    candidates: Mapping[str, Mapping[str, Any]],
    verified_catalogs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create an exclusive, read-only staged record; never modify the policy here."""
    if len(source_commit) != 40 or any(c not in "0123456789abcdef" for c in source_commit):
        raise PromotionError("source_commit must be a lowercase Git SHA-1")
    if not image_identity or not image_identity.strip():
        raise PromotionError("image_identity is required")
    _digest(expected_fingerprint, "expected_fingerprint")
    if not binary_digests:
        raise PromotionError("binary_digests is required")
    for name, digest in binary_digests.items():
        if not name or not isinstance(digest, str):
            raise PromotionError("invalid binary digest entry")
        _digest(digest, f"binary_digests.{name}")
    loaded = policy.load(store, profiles)
    if loaded.document_error:
        raise PromotionError(f"stored policy cannot be parsed: {loaded.document_error}")
    if loaded.revision != expected_revision:
        raise PolicyRevisionConflict(expected_revision, loaded.revision)
    if loaded.fingerprint != expected_fingerprint:
        raise PromotionError("stored policy fingerprint differs from expected fingerprint")
    if policy.fingerprint(loaded.policy) != expected_fingerprint:
        raise PromotionError("stored document does not match its fingerprint")
    candidate = _candidate_policy(loaded.policy, candidates, profiles, verified_catalogs)
    selections = {
        role: {
            provider: policy.Selection.model_validate(raw).model_dump(mode="json")
            for provider, raw in choices.items()
        }
        for role, choices in candidates.items()
    }
    record = {
        "version": 1,
        "source_commit": source_commit,
        "image_identity": image_identity,
        "binary_digests": dict(binary_digests),
        "expected_revision": expected_revision,
        "expected_fingerprint": expected_fingerprint,
        "previous_document": json.loads(policy.canonical_json(loaded.policy)),
        "candidate_selections": selections,
        "verified_catalogs": dict(verified_catalogs or {}),
        "candidate_fingerprint": policy.fingerprint(candidate),
    }
    content = _canonical(record).encode()
    envelope = {"record": record, "sha256": hashlib.sha256(content).hexdigest()}
    target = Path(path)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_canonical(envelope) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return record


def read(path: Path | str) -> dict[str, Any]:
    """Verify a staged record's checksum before use."""
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        record = envelope["record"]
        if hashlib.sha256(_canonical(record).encode()).hexdigest() != envelope["sha256"]:
            raise PromotionError("staged record checksum mismatch")
        if record["version"] != 1:
            raise PromotionError("unsupported staged record version")
        return record
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PromotionError("malformed staged record") from exc


def _closed(admission_closed: bool) -> None:
    if admission_closed is not True:
        raise PromotionError("caller must confirm admission is closed")


def apply(
    path: Path | str, store: Any, profiles: Mapping[str, Any], *, admission_closed: bool,
    runtime_image_identity: str, runtime_binary_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Apply the staged selections with an expected-revision CAS."""
    _closed(admission_closed)
    record = read(path)
    if runtime_image_identity != record["image_identity"]:
        raise PromotionError("runtime image identity mismatch")
    if dict(runtime_binary_digests) != record["binary_digests"]:
        raise PromotionError("runtime binary digest mismatch")
    loaded = policy.load(store, profiles)
    if loaded.document_error:
        raise PromotionError("stored policy cannot be parsed")
    if loaded.revision != record["expected_revision"]:
        raise PolicyRevisionConflict(record["expected_revision"], loaded.revision)
    if loaded.fingerprint != record["expected_fingerprint"]:
        raise PromotionError("stored policy fingerprint changed")
    previous = policy.parse(record["previous_document"])
    if policy.fingerprint(previous) != record["expected_fingerprint"]:
        raise PromotionError("staged prior policy fingerprint mismatch")
    candidate = _candidate_policy(
        previous, record["candidate_selections"], profiles, record.get("verified_catalogs"),
    )
    if policy.fingerprint(candidate) != record["candidate_fingerprint"]:
        raise PromotionError("staged candidate fingerprint mismatch")
    return policy.save(
        store, candidate, updated_by="promotion", if_revision=record["expected_revision"],
        reason=f"promote {record['source_commit']}",
    )


def rollback(
    path: Path | str, store: Any, profiles: Mapping[str, Any], *, admission_closed: bool,
) -> dict[str, Any]:
    """Restore the captured policy only when the staged write is still current."""
    _closed(admission_closed)
    record = read(path)
    expected = record["expected_revision"] + 1
    loaded = policy.load(store, profiles)
    if loaded.document_error:
        raise PromotionError("stored policy cannot be parsed")
    if loaded.revision != expected:
        raise PolicyRevisionConflict(expected, loaded.revision)
    if loaded.fingerprint != record["candidate_fingerprint"]:
        raise PromotionError("current policy is not the staged candidate")
    previous = policy.parse(record["previous_document"])
    if policy.fingerprint(previous) != record["expected_fingerprint"]:
        raise PromotionError("staged prior policy fingerprint mismatch")
    errors = policy.validate(previous, profiles)
    if errors:
        raise PromotionError(f"prior policy is invalid with current profiles: {errors}")
    return policy.save(
        store, previous, updated_by="promotion", if_revision=expected,
        reason=f"rollback {record['source_commit']}",
    )
