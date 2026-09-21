"""Resolve a model from an Antigravity ACP session's advertised choices.

Default ranking uses only numeric Gemini IDs. An explicit request may be a Gemini ID
or any other exact advertised ID (Claude/GPT third-party models). This module
performs no discovery, authentication or I/O. Persisted choices are validated rather
than upgraded on resume.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ModelSelection",
    "ModelSelectionError",
    "is_gemini_id",
    "resolve_model",
    "validate_selection",
]

_MODEL_ID = re.compile(
    r"gemini-(?P<version>[0-9]+(?:\.[0-9]+)*)-(?P<variant>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)\Z"
)
_EFFORTS = frozenset({"low", "medium", "high"})
_EFFORT_PREFERENCE = {"medium": 3, None: 2, "low": 1, "high": 0}


class ModelSelectionError(ValueError):
    """Malformed selection/advertisement, or a requested Gemini model is unavailable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModelSelection:
    """The exact advertised model ID and its encoded effort, if any."""

    model_id: str
    effort: str | None = None


@dataclass(frozen=True)
class _Model:
    selection: ModelSelection
    base: str
    version: tuple[int, ...]
    family: str


def _invalid(message: str) -> ModelSelectionError:
    return ModelSelectionError("AGY_MODEL_INVALID", message)


def _unavailable(message: str) -> ModelSelectionError:
    return ModelSelectionError("AGY_MODEL_UNAVAILABLE", message)


def _field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping):
            if name in value:
                return value[name]
        elif hasattr(value, name):
            return getattr(value, name)
    return None


def _items(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _invalid(f"ACP {label} must be a list")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid("ACP model choices must contain nonempty exact string IDs")
    return value


def _option_ids(options: Any) -> list[str]:
    result: list[str] = []
    for option in _items(options, "model options"):
        nested = _field(option, "options")
        value = _field(option, "value")
        if nested is not None and value is None:
            # ACP select options may be grouped; groups contain ordinary select choices.
            result.extend(
                _identifier(_field(item, "value")) for item in _items(nested, "grouped model options")
            )
        else:
            result.append(_identifier(value))
    return result


def _advertised_ids(response: Any) -> list[str]:
    config_options = _field(response, "config_options", "configOptions")
    if config_options is not None:
        model_options = [
            option
            for option in _items(config_options, "configOptions")
            if _field(option, "id") == "model" or _field(option, "category") == "model"
        ]
        if len(model_options) > 1:
            raise _invalid("ACP advertised multiple model config options")
        if model_options:
            option = model_options[0]
            if _field(option, "type") not in (None, "select"):
                raise _invalid("ACP model config option must be a select option")
            # An empty or malformed modern picker must not revive stale legacy choices.
            return _option_ids(_field(option, "options"))

    models = _field(response, "models")
    available = _field(models, "available_models", "availableModels")
    if available is None:
        raise _unavailable("ACP did not advertise model choices")
    return [
        _identifier(_field(model, "model_id", "modelId")) for model in _items(available, "availableModels")
    ]


def is_gemini_id(model_id: str) -> bool:
    """True when ``model_id`` matches the numeric Gemini grammar."""
    return isinstance(model_id, str) and _parse(model_id) is not None


def _parse(model_id: str) -> _Model | None:
    match = _MODEL_ID.fullmatch(model_id)
    if match is None:
        return None
    version = tuple(int(part) for part in match["version"].split("."))
    # 4, 4.0 and 4.0.0 are the same numeric release for ranking, but keep their exact IDs.
    while len(version) > 1 and version[-1] == 0:
        version = version[:-1]
    variant = match["variant"]
    family, _, suffix = variant.rpartition("-")
    effort = suffix if family and suffix in _EFFORTS else None
    base = model_id.removesuffix(f"-{effort}") if effort else model_id
    return _Model(ModelSelection(model_id, effort), base, version, family if effort else variant)


def _release_rank(model: _Model) -> tuple[tuple[int, ...], int]:
    # Prefer plain Flash, then Flash variants, before other variants on a generation tie.
    flash = 2 if model.family == "flash" else int(model.family.startswith("flash-"))
    return model.version, flash


def _requested_model(model: str | None) -> _Model | None:
    if model is None:
        return None
    if not isinstance(model, str) or (parsed := _parse(model)) is None:
        raise _invalid(
            "Model must be a numeric Gemini ID with a variant slug and optional low/medium/high effort"
        )
    return parsed


def _third_party_id(model: str) -> str:
    """Return a non-Gemini advertised-form ID, or raise AGY_MODEL_INVALID."""
    if not isinstance(model, str) or not model or model != model.strip():
        raise _invalid("ACP model choices must contain nonempty exact string IDs")
    if model.startswith("gemini-"):
        raise _invalid(
            "Model must be a numeric Gemini ID with a variant slug and optional low/medium/high effort"
        )
    return model


def validate_selection(model: str | None, effort: str | None) -> ModelSelection | None:
    """Check a model/effort pair without an advertised catalog.

    Gemini IDs use the numeric grammar. Other IDs are exact third-party names; whether
    they are offered is only known once a session advertises them.
    """
    if effort is not None and (not isinstance(effort, str) or effort not in _EFFORTS):
        raise _invalid("Effort must be low, medium or high")
    if model is None:
        return None
    parsed = _parse(model) if isinstance(model, str) else None
    if parsed is None:
        return ModelSelection(_third_party_id(model), effort)
    if parsed.selection.effort and effort and parsed.selection.effort != effort:
        raise _invalid("Effort conflicts with the effort suffix of the model ID")
    return ModelSelection(parsed.selection.model_id, effort or parsed.selection.effort)


def resolve_model(
    response: Any,
    *,
    model: str | None = None,
    effort: str | None = None,
    persisted: ModelSelection | None = None,
) -> ModelSelection:
    """Select or validate an exact advertised model ID from an ACP session response.

    ``response`` may be an ACP SDK object or a camelCase/snake_case mapping. A modern model
    picker takes precedence over legacy ``models``. Numeric Gemini IDs may contain any
    advertised variant slug. Defaults prefer the newest numeric Gemini release, then plain
    Flash, then Flash variants on a release tie, then medium effort. Without medium, prefer
    an unsuffixed server default, then low, then high. Explicit effort must exist in the
    chosen release/variant tier; it never downgrades the release to find a match.

    An exact suffixed Gemini ``model`` is retained; conflicting ``effort`` is an error. A
    base Gemini model may select one of its offered effort variants. An explicit non-Gemini
    ``model`` must match an advertised ID exactly. ``persisted`` must remain available with
    consistent effort and overrides; it is never re-ranked when newer models appear.
    """
    if effort is not None and (not isinstance(effort, str) or effort not in _EFFORTS):
        raise _invalid("Effort must be low, medium or high")

    advertised = _advertised_ids(response)
    candidates = {model_id: parsed for model_id in advertised if (parsed := _parse(model_id))}

    requested = None
    third_party = None
    if model is not None:
        parsed = _parse(model) if isinstance(model, str) else None
        if parsed is None:
            third_party = _third_party_id(model)
        else:
            requested = parsed
            if requested.selection.effort and effort not in (None, requested.selection.effort):
                raise _invalid("Requested effort conflicts with the exact model ID")

    if persisted is not None:
        if not isinstance(persisted, ModelSelection):
            raise _invalid("Persisted model must be a ModelSelection")
        previous = _parse(persisted.model_id)
        if previous is None:
            prior = _third_party_id(persisted.model_id)
            if prior not in advertised:
                raise _unavailable(f"Persisted model {persisted.model_id!r} is no longer advertised")
            if model is not None and model != persisted.model_id:
                raise _invalid("Requested model conflicts with the persisted selection")
            if effort is not None and effort != persisted.effort:
                raise _invalid("Requested effort conflicts with the persisted selection")
            return persisted
        if previous.selection.effort != persisted.effort:
            raise _invalid("Persisted effort does not match the resolved model ID")
        if persisted.model_id not in candidates:
            raise _unavailable(f"Persisted model {persisted.model_id!r} is no longer advertised")
        if requested and (
            requested.base != previous.base
            or (requested.selection.effort is not None and requested.selection != persisted)
        ):
            raise _invalid("Requested model conflicts with the persisted selection")
        if third_party is not None:
            raise _invalid("Requested model conflicts with the persisted selection")
        if effort is not None and effort != persisted.effort:
            raise _invalid("Requested effort conflicts with the persisted selection")
        return persisted

    if third_party is not None:
        if third_party not in advertised:
            raise _unavailable(f"Requested model {model!r} is not advertised")
        return ModelSelection(third_party, effort)

    if not candidates:
        raise _unavailable("ACP did not advertise a supported numeric Gemini model")

    if requested:
        if requested.selection.effort is not None:
            if model not in candidates:
                raise _unavailable(f"Requested model {model!r} is not advertised")
            return requested.selection
        choices = [candidate for candidate in candidates.values() if candidate.base == requested.base]
        if not choices:
            raise _unavailable(f"Requested model {model!r} has no advertised variants")
    else:
        latest = max(_release_rank(candidate) for candidate in candidates.values())
        choices = [candidate for candidate in candidates.values() if _release_rank(candidate) == latest]

    if effort is not None:
        choices = [candidate for candidate in choices if candidate.selection.effort == effort]
        if not choices:
            raise _unavailable(f"Effort {effort!r} is not advertised for the selected Gemini model")
    return max(
        choices,
        key=lambda candidate: (_EFFORT_PREFERENCE[candidate.selection.effort], candidate.selection.model_id),
    ).selection
