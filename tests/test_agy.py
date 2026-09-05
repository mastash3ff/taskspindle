"""Exact advertised-model selection and continuity for the Antigravity ACP provider."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from taskspindle.agy import ModelSelection, ModelSelectionError, resolve_model


def _advertise(*models: str) -> dict[str, Any]:
    return {
        "configOptions": [
            {
                "id": "model",
                "type": "select",
                "category": "model",
                "options": [{"value": model, "name": model} for model in models],
            }
        ],
    }


def test_latest_numeric_release_precedes_family_and_effort() -> None:
    choices = _advertise("gemini-3.9-flash-medium", "gemini-3.10-pro-low", "claude-opus-99")
    assert resolve_model(choices) == ModelSelection("gemini-3.10-pro-low", "low")


def test_new_generation_is_not_pinned_to_a_current_model_list() -> None:
    choices = _advertise("gemini-3.99-flash-medium", "gemini-4-pro-high", "gemini-4.0-pro-low")
    assert resolve_model(choices) == ModelSelection("gemini-4.0-pro-low", "low")


def test_newest_unfamiliar_numeric_variant_is_not_silently_excluded() -> None:
    choices = _advertise("gemini-3.99-flash-medium", "gemini-4-ultra-medium")
    assert resolve_model(choices) == ModelSelection("gemini-4-ultra-medium", "medium")


def test_exact_advertised_preview_id_is_retained() -> None:
    choices = _advertise("gemini-3.1-pro-preview", "gemini-5-flash-medium")
    assert resolve_model(choices, model="gemini-3.1-pro-preview") == ModelSelection("gemini-3.1-pro-preview")


def test_preview_variant_preserves_its_exact_effort_suffix() -> None:
    choices = _advertise("gemini-4-pro-preview-high", "gemini-4-pro-preview-medium")
    assert resolve_model(choices, model="gemini-4-pro-preview-high") == ModelSelection(
        "gemini-4-pro-preview-high", "high"
    )


def test_plain_flash_precedes_flash_variant_then_other_variants() -> None:
    choices = _advertise("gemini-4-flash-high", "gemini-4-flash-preview-medium", "gemini-4-ultra-medium")
    assert resolve_model(choices) == ModelSelection("gemini-4-flash-high", "high")
    assert resolve_model(
        _advertise("gemini-4-flash-preview-high", "gemini-4-ultra-medium")
    ) == ModelSelection("gemini-4-flash-preview-high", "high")


def test_flash_wins_only_on_a_numeric_release_tie() -> None:
    choices = _advertise("gemini-4.0-pro-medium", "gemini-4-flash-high", "gemini-3.99-flash-medium")
    assert resolve_model(choices) == ModelSelection("gemini-4-flash-high", "high")


def test_medium_is_preferred_within_selected_release_and_family() -> None:
    ids = ["gemini-4.2-flash-high", "gemini-4.2-flash-low", "gemini-4.2-flash-medium"]
    assert resolve_model(_advertise(*ids)) == resolve_model(_advertise(*reversed(ids)))
    assert resolve_model(_advertise(*ids)) == ModelSelection("gemini-4.2-flash-medium", "medium")


@pytest.mark.parametrize(
    ("ids", "expected"),
    [
        (["gemini-4-pro", "gemini-4-pro-low", "gemini-4-pro-high"], ModelSelection("gemini-4-pro")),
        (["gemini-4-pro-low", "gemini-4-pro-high"], ModelSelection("gemini-4-pro-low", "low")),
        (["gemini-4-pro-high"], ModelSelection("gemini-4-pro-high", "high")),
    ],
)
def test_deterministic_fallback_when_medium_absent(ids: list[str], expected: ModelSelection) -> None:
    assert resolve_model(_advertise(*ids)) == expected


def test_explicit_suffixed_model_is_preserved() -> None:
    choices = _advertise("gemini-3.1-pro-high", "gemini-3.1-pro-medium", "gemini-5-flash-medium")
    assert resolve_model(choices, model="gemini-3.1-pro-high") == ModelSelection(
        "gemini-3.1-pro-high", "high"
    )
    assert (
        resolve_model(choices, model="gemini-3.1-pro-high", effort="high").model_id == "gemini-3.1-pro-high"
    )


def test_explicit_base_selects_only_its_advertised_variant() -> None:
    choices = _advertise("gemini-3.1-pro-high", "gemini-3.1-pro-medium", "gemini-5-flash-medium")
    assert resolve_model(choices, model="gemini-3.1-pro") == ModelSelection("gemini-3.1-pro-medium", "medium")
    assert resolve_model(choices, model="gemini-3.1-pro", effort="high").model_id == "gemini-3.1-pro-high"


def test_effort_only_override_applies_to_newest_preferred_family() -> None:
    choices = _advertise("gemini-4-flash-high", "gemini-4-flash-low", "gemini-4-pro-high")
    assert resolve_model(choices, effort="high") == ModelSelection("gemini-4-flash-high", "high")


@pytest.mark.parametrize("effort", ["medium", "low"])
def test_missing_effort_does_not_downgrade_generation_or_switch_family(effort: str) -> None:
    choices = _advertise("gemini-3-flash-medium", "gemini-4-pro-low", "gemini-4-flash-high")
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(choices, effort=effort)
    assert error.value.code == "AGY_MODEL_UNAVAILABLE"


@pytest.mark.parametrize("model", ["claude-sonnet-4", "gemini-latest", "gemini-4-pro/", "", 17])
def test_unsupported_explicit_model_is_invalid(model: Any) -> None:
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(_advertise("gemini-4-flash-medium"), model=model)
    assert error.value.code == "AGY_MODEL_INVALID"


@pytest.mark.parametrize("effort", ["xhigh", "auto", "Medium", "", 17])
def test_unsupported_explicit_effort_is_invalid(effort: Any) -> None:
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(_advertise("gemini-4-flash-medium"), effort=effort)
    assert error.value.code == "AGY_MODEL_INVALID"


def test_effort_cannot_silently_replace_explicit_exact_model() -> None:
    with pytest.raises(ModelSelectionError, match="conflicts"):
        resolve_model(
            _advertise("gemini-4-pro-high", "gemini-4-pro-low"), model="gemini-4-pro-high", effort="low"
        )


@pytest.mark.parametrize("model", ["gemini-3-pro", "gemini-3-pro-high", "gemini-4-pro-ultra"])
def test_unavailable_explicit_model_never_falls_back(model: str) -> None:
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(_advertise("gemini-4-flash-medium"), model=model)
    assert error.value.code == "AGY_MODEL_UNAVAILABLE"


def test_non_gemini_and_unrankable_aliases_are_not_default_candidates() -> None:
    choices = _advertise("claude-opus-99", "gpt-oss-120b-medium", "gemini-latest", "gemini-4-pro-high")
    assert resolve_model(choices) == ModelSelection("gemini-4-pro-high", "high")


@pytest.mark.parametrize("choices", [_advertise(), _advertise("claude-opus-99", "gemini-latest"), {}])
def test_missing_gemini_choices_fail_closed(choices: Any) -> None:
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(choices)
    assert error.value.code == "AGY_MODEL_UNAVAILABLE"


def test_modern_picker_is_authoritative_even_when_empty() -> None:
    choices = _advertise()
    choices["models"] = {"availableModels": [{"modelId": "gemini-4-pro-medium"}]}
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(choices)
    assert error.value.code == "AGY_MODEL_UNAVAILABLE"


def test_legacy_models_fallback_when_no_model_picker() -> None:
    choices = {
        "configOptions": [{"id": "mode", "type": "select", "options": []}],
        "models": {"availableModels": [{"modelId": "gemini-4-pro-medium"}]},
    }
    assert resolve_model(choices) == ModelSelection("gemini-4-pro-medium", "medium")


def test_sdk_attribute_and_snake_case_shapes() -> None:
    model = SimpleNamespace(model_id="gemini-4-pro-medium")
    response = SimpleNamespace(config_options=None, models=SimpleNamespace(available_models=[model]))
    assert resolve_model(response) == ModelSelection("gemini-4-pro-medium", "medium")
    response = {"models": {"available_models": [{"model_id": "gemini-4-pro-medium"}]}}
    assert resolve_model(response) == ModelSelection("gemini-4-pro-medium", "medium")


def test_grouped_config_options_and_duplicate_choices() -> None:
    choices = {
        "config_options": [
            SimpleNamespace(
                id="model",
                category="model",
                type="select",
                options=[
                    SimpleNamespace(
                        group="Gemini",
                        options=[
                            SimpleNamespace(value="gemini-4-pro-low"),
                            SimpleNamespace(value="gemini-4-pro-medium"),
                            SimpleNamespace(value="gemini-4-pro-medium"),
                        ],
                    ),
                ],
            )
        ]
    }
    assert resolve_model(choices) == ModelSelection("gemini-4-pro-medium", "medium")


@pytest.mark.parametrize(
    "choices",
    [
        {"configOptions": "not a list"},
        {"configOptions": [{"id": "model", "type": "boolean", "options": []}]},
        {"configOptions": [{"id": "model", "options": "gemini-4-pro-medium"}]},
        {"configOptions": [{"id": "model", "options": [{}]}]},
        {"configOptions": [{"id": "model", "options": [{"value": 17}]}]},
        {"configOptions": [{"id": "model", "options": []}, {"id": "model", "options": []}]},
        {"models": {"availableModels": [{"modelId": " gemini-4-pro-medium"}]}},
        {"models": {"availableModels": {"modelId": "gemini-4-pro-medium"}}},
    ],
)
def test_malformed_advertisement_is_rejected(choices: Any) -> None:
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(choices)
    assert error.value.code == "AGY_MODEL_INVALID"


def test_resume_preserves_exact_selection_when_newer_model_appears() -> None:
    persisted = ModelSelection("gemini-3.1-pro-high", "high")
    choices = _advertise(persisted.model_id, "gemini-5-flash-medium")
    assert resolve_model(choices, persisted=persisted) is persisted
    assert resolve_model(choices, model="gemini-3.1-pro", effort="high", persisted=persisted) is persisted


def test_resume_fails_when_previous_variant_disappears() -> None:
    choices = _advertise("gemini-3.1-pro-medium", "gemini-5-flash-medium")
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(choices, persisted=ModelSelection("gemini-3.1-pro-high", "high"))
    assert error.value.code == "AGY_MODEL_UNAVAILABLE"


@pytest.mark.parametrize(
    ("persisted", "kwargs"),
    [
        (ModelSelection("gemini-4-pro-high", "low"), {}),
        (ModelSelection("gemini-4-pro-high", "high"), {"effort": "low"}),
        (ModelSelection("gemini-4-pro-high", "high"), {"model": "gemini-4-flash"}),
        (ModelSelection("gemini-4-pro-high", "high"), {"model": "gemini-4-pro-low"}),
        (ModelSelection("claude-opus-99"), {}),
    ],
)
def test_resume_rejects_inconsistent_or_changed_selection(persisted: ModelSelection, kwargs: Any) -> None:
    choices = _advertise("gemini-4-pro-high", "gemini-4-pro-low", "gemini-4-flash-medium")
    with pytest.raises(ModelSelectionError) as error:
        resolve_model(choices, persisted=persisted, **kwargs)
    assert error.value.code == "AGY_MODEL_INVALID"
