"""Dispatch policy: operator preferences for how Codex spreads work across providers.

The policy never selects a provider, moves a task, or retries on another provider: Codex still
names the provider on every ``start_task``, and a model or effort the caller sends is never
changed. Three things here have a server-side effect, and each is mechanical:

- an *enforced* budget refuses admission of a new task on that provider for the rest of the window;
- ``max_concurrent`` is the number of slots a provider may hold, read at every dispatch;
- a task that names a ``role`` and leaves ``model`` or ``effort`` out has them filled from that
  role's selection, stepped along its ladder by :func:`status`.

Everything else is advisory. Nothing here calls a provider or reads provider quota; observed
usage is what this host recorded from its own turns.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import agy
from .models import Mode

__all__ = [
    "DEFAULT_ROLES",
    "PRESET_LEVELS",
    "WINDOWS",
    "Budget",
    "DispatchPolicy",
    "Escalation",
    "Ladder",
    "LoadedPolicy",
    "PolicyError",
    "ProviderPolicy",
    "Resolution",
    "RolePolicy",
    "Selection",
    "apply_preset",
    "canonical_json",
    "defaults",
    "effective_limits",
    "file_managed",
    "fingerprint",
    "full_document",
    "load",
    "preset_matches",
    "preset_table",
    "resolve_selection",
    "slot_limits_for",
    "status",
    "step_selection",
    "validate",
]

Window = Literal["day", "week"]
WINDOWS: tuple[Window, ...] = ("day", "week")
_WINDOW_SPANS: dict[str, timedelta] = {"day": timedelta(hours=24), "week": timedelta(days=7)}

#: Ids and advertised values are short, printable tokens; ``opus[1m]`` is a legitimate Claude id.
_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$")
_ROLE_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SHARE_BAND = 0.02
TIMEOUT_BOUNDS = (60, 14400)
MAX_ROLES = 32
MAX_ADVERTISED = 64
MAX_NOTE = 512
#: Slots one provider, and the whole pool, may be given here. ``[capacity]`` in ``config.toml``
#: is the ceiling the dashboard cannot raise; these only bound what a document may say.
MAX_CONCURRENT = 16
MAX_CONCURRENT_TOTAL = 48
MAX_LADDER_STEPS = 3
MAX_FANOUT = 3


class PolicyError(Exception):
    """A document that cannot be accepted; ``errors`` lists ``{loc, msg, code}`` entries."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors))
        self.errors = errors


# -- document ------------------------------------------------------------------------------------


class Budget(BaseModel):
    """A locally observed ceiling for one rolling window; advisory unless ``enforce``."""

    model_config = ConfigDict(extra="forbid")

    turns: int | None = Field(default=None, ge=1)
    tokens: int | None = Field(default=None, ge=1)
    enforce: bool = False


class ProviderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    target_share: int | None = Field(default=None, ge=0, le=100)
    budgets: dict[Window, Budget] = Field(default_factory=dict)
    allowed_modes: list[Mode] | None = None
    note: str = Field(default="", max_length=MAX_NOTE)
    advertised_models: list[str] = Field(default_factory=list, max_length=MAX_ADVERTISED)
    advertised_efforts: list[str] = Field(default_factory=list, max_length=MAX_ADVERTISED)
    models_without_effort: list[str] = Field(default_factory=list, max_length=MAX_ADVERTISED)
    #: Slots this provider may hold at once; unset falls back to ``[concurrency]`` in config.toml.
    max_concurrent: int | None = Field(default=None, ge=1, le=MAX_CONCURRENT)

    @field_validator("advertised_models", "advertised_efforts", "models_without_effort")
    @classmethod
    def _values(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        for value in values:
            if not _VALUE.match(value):
                raise ValueError(f"{value!r} is not a valid identifier")
            if value in seen:
                raise ValueError(f"{value!r} is listed twice")
            seen.add(value)
        return values

    @field_validator("allowed_modes")
    @classmethod
    def _modes(cls, values: list[Mode] | None) -> list[Mode] | None:
        if values is not None and len(set(values)) != len(values):
            raise ValueError("allowed_modes repeats a mode")
        return values


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, pattern=_VALUE.pattern)
    effort: str | None = Field(default=None, pattern=_VALUE.pattern)


class Ladder(BaseModel):
    """Steps either side of a role's selection for one provider, nearest first.

    Step 0 is always ``selections[provider]``, so a reader that knows nothing about ladders
    still sees the base choice. ``above`` spends more (a larger model, more effort); ``below``
    spends less.
    """

    model_config = ConfigDict(extra="forbid")

    below: list[Selection] = Field(default_factory=list, max_length=MAX_LADDER_STEPS)
    above: list[Selection] = Field(default_factory=list, max_length=MAX_LADDER_STEPS)


class RolePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(default="", max_length=MAX_NOTE)
    provider_preference: list[str] = Field(default_factory=list)
    selections: dict[str, Selection] = Field(default_factory=dict)
    timeout_s: int | None = Field(default=None, ge=TIMEOUT_BOUNDS[0], le=TIMEOUT_BOUNDS[1])
    ladders: dict[str, Ladder] = Field(default_factory=dict)
    #: How many independent provider families a consult or review in this role may go to.
    fanout: int = Field(default=1, ge=1, le=MAX_FANOUT)

    @field_validator("provider_preference")
    @classmethod
    def _unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("provider_preference repeats a provider")
        return values


class Escalation(BaseModel):
    """When a provider's ladder level moves. Thresholds are percentage points of turn share."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    #: Points under target at which the level rises to 1, 2, ...; mirrored for over target.
    step_up_points: list[int] = Field(
        default_factory=lambda: [5, 12], min_length=1, max_length=MAX_LADDER_STEPS
    )
    #: A level is left only once the deficit is this far back inside its threshold.
    release_points: int = Field(default=3, ge=0, le=50)
    #: Below this many turns in the share window a share is noise, and the level stays 0.
    min_turns: int = Field(default=10, ge=0, le=100_000)
    window_hold_percent: int = Field(default=80, ge=1, le=100)
    window_down_percent: int = Field(default=90, ge=1, le=100)
    budget_hold_ratio: float = Field(default=0.8, gt=0, le=1)
    budget_down_ratio: float = Field(default=0.95, gt=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> Escalation:
        steps = self.step_up_points
        if any(step < 1 or step > 100 for step in steps):
            raise ValueError("step_up_points must each be between 1 and 100")
        if any(later <= earlier for earlier, later in pairwise(steps)):
            raise ValueError("step_up_points must be strictly ascending")
        if self.release_points >= steps[0]:
            raise ValueError("release_points must be below the first step_up_points value")
        if self.window_down_percent < self.window_hold_percent:
            raise ValueError("window_down_percent must not be below window_hold_percent")
        if self.budget_down_ratio < self.budget_hold_ratio:
            raise ValueError("budget_down_ratio must not be below budget_hold_ratio")
        return self


class DispatchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    share_window: Window = "week"
    providers: dict[str, ProviderPolicy] = Field(default_factory=dict)
    roles: dict[str, RolePolicy] = Field(default_factory=dict, max_length=MAX_ROLES)
    #: Slots the whole pool may hold at once, across providers; unset means no total limit here.
    max_concurrent_total: int | None = Field(default=None, ge=1, le=MAX_CONCURRENT_TOTAL)
    escalation: Escalation = Field(default_factory=Escalation)

    @field_validator("roles")
    @classmethod
    def _role_keys(cls, values: dict[str, RolePolicy]) -> dict[str, RolePolicy]:
        for key in values:
            if not _ROLE_KEY.match(key):
                raise ValueError(f"role {key!r} must match {_ROLE_KEY.pattern}")
        return values


# -- defaults ----------------------------------------------------------------------------------

_SEEDS: dict[str, dict[str, list[str]]] = {
    "claude": {
        "advertised_models": ["haiku", "sonnet", "opus[1m]"],
        "advertised_efforts": ["low", "medium", "high", "xhigh"],
        "models_without_effort": ["haiku"],
    },
    "grok": {
        "advertised_models": ["grok-4.6"],
        "advertised_efforts": ["low", "medium", "high"],
        "models_without_effort": [],
    },
    "agy": {
        "advertised_models": [
            "gemini-3.8-flash-medium",
            "gemini-3.1-pro-high",
            "claude-sonnet-4-6",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
        ],
        "advertised_efforts": ["low", "medium", "high"],
        "models_without_effort": [],
    },
}

_PREFERENCE_ORDER = ("claude", "grok", "agy")

#: The role table the Codex work-pool skill carried before the policy existed, verbatim.
DEFAULT_ROLES: dict[str, dict[str, Any]] = {
    "mechanic": {
        "brief": (
            "Perform exact extraction, lookup, prescribed formatting, or a fully specified "
            "mechanical edit with objective checks. Do not introduce design choices."
        ),
        "provider_preference": ["agy", "grok", "claude"],
        "selections": {
            "claude": {"model": "haiku", "effort": None},
            "grok": {"model": "grok-4.6", "effort": "low"},
            "agy": {"model": "gemini-3.8-flash-medium", "effort": "medium"},
        },
    },
    "explorer": {
        "brief": (
            "Investigate a bounded question, gather relevant evidence, and separate verified "
            "facts, inferences, and unknowns."
        ),
        "provider_preference": ["agy", "grok", "claude"],
        "selections": {
            "claude": {"model": "sonnet", "effort": "high"},
            "grok": {"model": "grok-4.6", "effort": "medium"},
            "agy": {"model": "gemini-3.1-pro-high", "effort": "high"},
        },
    },
    "implementer": {
        "brief": (
            "Implement a settled design within owned paths and satisfy the stated acceptance "
            "criteria and checks."
        ),
        "provider_preference": ["agy", "grok", "claude"],
        "selections": {
            "claude": {"model": "sonnet", "effort": "high"},
            "grok": {"model": "grok-4.6", "effort": "high"},
            "agy": {"model": "gemini-3.1-pro-high", "effort": "high"},
        },
    },
    "planner": {
        "brief": (
            "Produce a decision-ready plan with interfaces, dependencies, risks, verification, "
            "and authority gates."
        ),
        "provider_preference": ["grok", "agy", "claude"],
        "selections": {
            "claude": {"model": "opus[1m]", "effort": "xhigh"},
            "grok": {"model": "grok-4.6", "effort": "high"},
            "agy": {"model": "claude-opus-4-6-thinking", "effort": "high"},
        },
    },
    "debugger": {
        "brief": (
            "Reproduce and localize unexpected behavior, test hypotheses against evidence, and "
            "identify the smallest justified repair and verification."
        ),
        "provider_preference": ["grok", "agy", "claude"],
        "selections": {
            "claude": {"model": "opus[1m]", "effort": "xhigh"},
            "grok": {"model": "grok-4.6", "effort": "high"},
            "agy": {"model": "claude-opus-4-6-thinking", "effort": "high"},
        },
    },
    "reviewer": {
        "brief": (
            "Independently inspect the exact target against its requirements and report "
            "prioritized, actionable findings with evidence."
        ),
        "provider_preference": ["grok", "agy", "claude"],
        "selections": {
            "claude": {"model": "opus[1m]", "effort": "xhigh"},
            "grok": {"model": "grok-4.6", "effort": "high"},
            "agy": {"model": "claude-sonnet-4-6", "effort": "high"},
        },
    },
}


def _family(profile: Any) -> str:
    return getattr(profile, "family", None) or getattr(profile, "base", None) or profile.id


def defaults(profiles: Mapping[str, Any]) -> DispatchPolicy:
    """The policy in force when nothing has been saved: every loaded profile, the six skill roles."""
    providers: dict[str, ProviderPolicy] = {}
    for name in sorted(profiles):
        seed = _SEEDS.get(_family(profiles[name]), {})
        providers[name] = ProviderPolicy(**{key: list(values) for key, values in seed.items()})
        if _family(profiles[name]) == "muse":
            providers[name].enabled = False
            providers[name].note = "Disabled pending subscription-route and tool-containment qualification."
    first_class = [name for name in _PREFERENCE_ORDER if name in profiles]
    roles: dict[str, RolePolicy] = {}
    for role, spec in DEFAULT_ROLES.items():
        selections = {
            name: Selection(**spec["selections"][name]) for name in first_class if name in spec["selections"]
        }
        preferred = [
            name for name in spec.get("provider_preference", first_class) if name in profiles
        ]
        roles[role] = RolePolicy(
            brief=spec["brief"],
            provider_preference=preferred or list(first_class),
            selections=selections,
        )
    return DispatchPolicy(providers=providers, roles=roles)


# -- intensity presets --------------------------------------------------------------------------

PRESET_LEVELS: tuple[str, ...] = ("conserve", "balanced", "max")
_PRESET_CONCURRENCY = {"conserve": 1, "balanced": 4, "max": 8}
_HEAVY_ROLES = ("planner", "debugger", "reviewer")


def _pick(model: str | None, effort: str | None = None) -> dict[str, Any]:
    return {"model": model, "effort": effort}


def _rungs(light: Any, middle: Any, heavy: Any) -> dict[str, Any]:
    """One ``(selection, below, above)`` triple per role group: mechanic, explorer/implementer, heavy."""
    table = {"mechanic": light, "explorer": middle, "implementer": middle}
    table.update(dict.fromkeys(_HEAVY_ROLES, heavy))
    return table


_FLASH = _pick("gemini-3.8-flash-medium", "medium")
_PRO = _pick("gemini-3.1-pro-high", "high")
_GROK = "grok-4.6"

#: What each level writes per provider family. ``balanced`` is the shipped role table with a
#: step either side; ``conserve`` has nothing above it, so escalation can only ease it off; ``max``
#: starts at the top and keeps one step down for when a brake applies. AGY uses only the two
#: seeded Gemini ids, because third-party ids depend on the account's live catalog.
_PRESETS: dict[str, dict[str, dict[str, Any]]] = {
    "claude": {
        "conserve": _rungs(
            (_pick("haiku"), [], []),
            (_pick("sonnet", "medium"), [_pick("haiku")], []),
            (_pick("sonnet", "high"), [_pick("sonnet", "medium")], []),
        ),
        "balanced": _rungs(
            (_pick("haiku"), [], [_pick("sonnet", "medium")]),
            (_pick("sonnet", "high"), [_pick("sonnet", "medium")], [_pick("opus[1m]", "high")]),
            (_pick("opus[1m]", "xhigh"), [_pick("sonnet", "high")], []),
        ),
        "max": _rungs(
            (_pick("sonnet", "medium"), [_pick("haiku")], []),
            (_pick("opus[1m]", "high"), [_pick("sonnet", "high")], []),
            (_pick("opus[1m]", "xhigh"), [_pick("opus[1m]", "high")], []),
        ),
    },
    "grok": {
        "conserve": _rungs(
            (_pick(_GROK, "low"), [], []),
            (_pick(_GROK, "medium"), [_pick(_GROK, "low")], []),
            (_pick(_GROK, "medium"), [_pick(_GROK, "low")], []),
        ),
        "balanced": {
            "mechanic": (_pick(_GROK, "low"), [], [_pick(_GROK, "medium")]),
            "explorer": (_pick(_GROK, "medium"), [_pick(_GROK, "low")], [_pick(_GROK, "high")]),
            "implementer": (_pick(_GROK, "high"), [_pick(_GROK, "medium")], []),
            **dict.fromkeys(_HEAVY_ROLES, (_pick(_GROK, "high"), [_pick(_GROK, "medium")], [])),
        },
        "max": _rungs(
            (_pick(_GROK, "medium"), [_pick(_GROK, "low")], []),
            (_pick(_GROK, "high"), [_pick(_GROK, "medium")], []),
            (_pick(_GROK, "high"), [_pick(_GROK, "medium")], []),
        ),
    },
    "agy": {
        "conserve": _rungs((_FLASH, [], []), (_FLASH, [], []), (_PRO, [_FLASH], [])),
        "balanced": _rungs((_FLASH, [], [_PRO]), (_PRO, [_FLASH], []), (_PRO, [_FLASH], [])),
        "max": _rungs((_PRO, [_FLASH], []), (_PRO, [_FLASH], []), (_PRO, [_FLASH], [])),
    },
}


def _preset_patches(
    policy: DispatchPolicy, profiles: Mapping[str, Any], provider: str, level: str
) -> list[dict[str, Any]]:
    profile = profiles.get(provider)
    table = _PRESETS.get(_family(profile), {}).get(level) if profile is not None else None
    if table is None or not _steerable(profile):
        return []
    patches = [{"path": ["providers", provider, "max_concurrent"], "value": _PRESET_CONCURRENCY[level]}]
    for role, (selection, below, above) in table.items():
        spec = policy.roles.get(role)
        if spec is None or provider not in spec.selections:
            continue
        patches.append({"path": ["roles", role, "selections", provider], "value": dict(selection)})
        patches.append(
            {
                "path": ["roles", role, "ladders", provider],
                "value": {"below": [dict(s) for s in below], "above": [dict(s) for s in above]},
            }
        )
    return patches


def preset_table(
    policy: DispatchPolicy, profiles: Mapping[str, Any]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Per provider and level, the path/value patches a preset writes into this document.

    A preset is a macro, not stored state: it fills ``max_concurrent`` and, for each shipped role
    that has a selection on that provider, the selection and its ladder. Shares, budgets, fan-out
    and custom roles are left alone, and every field stays hand-editable afterwards.
    """
    return {
        name: {
            level: patches
            for level in PRESET_LEVELS
            if (patches := _preset_patches(policy, profiles, name, level))
        }
        for name in sorted(policy.providers)
    }


def apply_preset(
    policy: DispatchPolicy, profiles: Mapping[str, Any], provider: str, level: str
) -> DispatchPolicy:
    """``policy`` with one provider set to ``level``; raises :class:`PolicyError` when it has no preset."""
    if level not in PRESET_LEVELS:
        raise PolicyError([_error(("preset",), f"unknown level {level!r}", "unknown_preset")])
    if provider not in policy.providers:
        raise PolicyError(
            [_error(("providers", provider), f"unknown provider {provider!r}", "unknown_provider")]
        )
    patches = _preset_patches(policy, profiles, provider, level)
    if not patches:
        raise PolicyError(
            [_error(("providers", provider), "this provider has no intensity presets", "no_preset")]
        )
    document = policy.model_dump(mode="json")
    for patch in patches:
        node = document
        for key in patch["path"][:-1]:
            node = node.setdefault(key, {})
        node[patch["path"][-1]] = patch["value"]
    return parse(document)


def preset_matches(policy: DispatchPolicy, profiles: Mapping[str, Any], provider: str) -> str | None:
    """The level this provider's fields currently equal, ``"custom"``, or None when it has no presets."""
    current = canonical_json(policy)
    found = False
    for level in PRESET_LEVELS:
        if not _preset_patches(policy, profiles, provider, level):
            continue
        found = True
        if canonical_json(apply_preset(policy, profiles, provider, level)) == current:
            return level
    return "custom" if found else None


# -- validation ---------------------------------------------------------------------------------


def _error(loc: tuple[Any, ...], msg: str, code: str = "invalid") -> dict[str, Any]:
    return {"loc": list(loc), "msg": msg, "code": code}


def _pydantic_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [_error(tuple(err["loc"]), err["msg"], err["type"]) for err in exc.errors()]


def parse(document: Any) -> DispatchPolicy:
    """Validate the document shape; raises :class:`PolicyError` with located messages."""
    try:
        return DispatchPolicy.model_validate(document)
    except ValidationError as exc:
        raise PolicyError(_pydantic_errors(exc)) from exc


def validate(policy: DispatchPolicy, profiles: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Cross-field checks against the loaded profiles; an empty list means acceptable."""
    errors: list[dict[str, Any]] = []
    known = set(profiles)
    for name, provider in policy.providers.items():
        loc = ("providers", name)
        if name not in known:
            errors.append(_error(loc, f"unknown provider {name!r}", "unknown_provider"))
            continue
        profile_modes = set(getattr(profiles[name], "modes", ()))
        if provider.allowed_modes is not None and profile_modes:
            extra = [mode.value for mode in provider.allowed_modes if mode.value not in profile_modes]
            if extra:
                errors.append(
                    _error(
                        (*loc, "allowed_modes"),
                        f"profile does not serve {', '.join(extra)}",
                        "mode_not_served",
                    )
                )
        for model in provider.models_without_effort:
            if provider.advertised_models and model not in provider.advertised_models:
                errors.append(
                    _error(
                        (*loc, "models_without_effort"),
                        f"{model!r} is not an advertised model",
                        "unadvertised",
                    )
                )
    total = sum(p.target_share or 0 for p in policy.providers.values() if p.enabled)
    if total > 100:
        errors.append(_error(("providers",), f"enabled target shares sum to {total}, above 100", "share_sum"))

    for role, spec in policy.roles.items():
        loc = ("roles", role)
        for index, name in enumerate(spec.provider_preference):
            if name not in known:
                errors.append(
                    _error(
                        (*loc, "provider_preference", index), f"unknown provider {name!r}", "unknown_provider"
                    )
                )
        for name, selection in spec.selections.items():
            sloc = (*loc, "selections", name)
            if name not in known:
                errors.append(_error(sloc, f"unknown provider {name!r}", "unknown_provider"))
                continue
            errors.extend(_check_selection(sloc, selection, profiles[name], policy.providers.get(name)))
        for name, ladder in spec.ladders.items():
            lloc = (*loc, "ladders", name)
            if name not in known:
                errors.append(_error(lloc, f"unknown provider {name!r}", "unknown_provider"))
                continue
            if not _steerable(profiles[name]):
                errors.append(
                    _error(lloc, "a ladder needs a built-in subscription profile", "ladder_metered")
                )
                continue
            if name not in spec.selections:
                errors.append(
                    _error(
                        lloc, "a ladder needs a selection for the same provider", "ladder_without_selection"
                    )
                )
            for side in ("below", "above"):
                for index, step in enumerate(getattr(ladder, side)):
                    sloc = (*lloc, side, index)
                    if step.model is None:
                        errors.append(
                            _error((*sloc, "model"), "a ladder step names a model", "ladder_step_empty")
                        )
                        continue
                    errors.extend(_check_selection(sloc, step, profiles[name], policy.providers.get(name)))
    return errors


def _steerable(profile: Any) -> bool:
    """Whether the server may choose a model or effort for this profile.

    Only a built-in subscription profile: ``with_task_selection`` refuses a per-task override on
    anything else, and a metered profile must never be stepped up by a share target.
    """
    return bool(getattr(profile, "first_class", True)) and getattr(profile, "auth", "oauth") != "api_key"


def _check_selection(
    loc: tuple[Any, ...], selection: Selection, profile: Any, provider: ProviderPolicy | None
) -> list[dict[str, Any]]:
    family = _family(profile)
    if family == "agy":
        try:
            agy.validate_selection(selection.model, selection.effort)
        except agy.ModelSelectionError as exc:
            return [_error(loc, str(exc), exc.code)]
        if (
            selection.model is not None
            and not agy.is_gemini_id(selection.model)
            and provider is not None
            and provider.advertised_models
            and selection.model not in provider.advertised_models
        ):
            return [_error((*loc, "model"), f"{selection.model!r} is not an advertised model", "unadvertised")]
        return []
    if family not in {"claude", "grok"} or provider is None:
        return []
    errors: list[dict[str, Any]] = []
    model, effort = selection.model, selection.effort
    if model is not None and provider.advertised_models and model not in provider.advertised_models:
        errors.append(_error((*loc, "model"), f"{model!r} is not an advertised model", "unadvertised"))
    if effort is not None and provider.advertised_efforts and effort not in provider.advertised_efforts:
        errors.append(_error((*loc, "effort"), f"{effort!r} is not an advertised effort", "unadvertised"))
    if effort is not None and model is not None and model in provider.models_without_effort:
        errors.append(_error((*loc, "effort"), f"{model!r} does not accept an effort", "effort_unsupported"))
    return errors


# -- canonical form ----------------------------------------------------------------------------


def _stored_document(policy: DispatchPolicy) -> dict[str, Any]:
    """The document as stored: fields added after the first release are omitted at their defaults.

    A document that uses none of them is then byte-identical to what the first release wrote,
    so its fingerprint does not move and an older image, whose models forbid unknown keys,
    still parses it. Setting any of them is what makes a document newer.
    """
    document = policy.model_dump(mode="json")
    for provider in document["providers"].values():
        if provider["max_concurrent"] is None:
            del provider["max_concurrent"]
    for role in document["roles"].values():
        ladders = {
            name: ladder for name, ladder in role["ladders"].items() if ladder["below"] or ladder["above"]
        }
        if ladders:
            role["ladders"] = ladders
        else:
            del role["ladders"]
        if role["fanout"] == 1:
            del role["fanout"]
    if document["max_concurrent_total"] is None:
        del document["max_concurrent_total"]
    if document["escalation"] == Escalation().model_dump(mode="json"):
        del document["escalation"]
    return document


def canonical_json(policy: DispatchPolicy) -> str:
    return json.dumps(_stored_document(policy), sort_keys=True, separators=(",", ":"))


def full_document(document: Any) -> Any:
    """A stored document with every field present, as readers expect; unparseable ones pass through."""
    try:
        return parse(document).model_dump(mode="json")
    except PolicyError:
        return document


def fingerprint(policy: DispatchPolicy) -> str:
    return hashlib.sha256(canonical_json(policy).encode()).hexdigest()


# -- loading ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedPolicy:
    policy: DispatchPolicy
    revision: int
    fingerprint: str
    updated_at: str | None
    updated_by: str | None
    source: Literal["store", "defaults"]
    document_error: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "fingerprint": self.fingerprint,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "source": self.source,
            "document_error": self.document_error,
        }


def load(store: Any, profiles: Mapping[str, Any]) -> LoadedPolicy:
    """The current policy: the stored document when one parses, otherwise the defaults.

    A stored document that no longer parses (for example after a downgrade) is reported as
    ``document_error`` rather than raised, so ``capabilities`` and the dashboard keep working.
    """
    row = None
    getter = getattr(store, "get_dispatch_policy", None)
    if getter is not None:
        row = getter()
    fallback = defaults(profiles)
    if row is None:
        return LoadedPolicy(fallback, 0, fingerprint(fallback), None, None, "defaults")
    try:
        policy = parse(row["document"])
    except PolicyError as exc:
        return LoadedPolicy(
            fallback,
            int(row["revision"]),
            fingerprint(fallback),
            row.get("updated_at"),
            row.get("updated_by"),
            "defaults",
            document_error=str(exc),
        )
    return LoadedPolicy(
        policy,
        int(row["revision"]),
        str(row["fingerprint"]),
        row.get("updated_at"),
        row.get("updated_by"),
        "store",
    )


def save(
    store: Any, policy: DispatchPolicy, *, updated_by: str, if_revision: int | None, reason: str | None = None
) -> dict[str, Any]:
    """Persist a validated policy; the caller has already run :func:`validate`."""
    return store.save_dispatch_policy(
        canonical_json(policy),
        fingerprint(policy),
        updated_by=updated_by,
        if_revision=if_revision,
        reason=reason,
    )


# -- file-managed tables: shown beside the policy, never written by it ---------------------------


def file_managed(
    config_file: Any, profiles: Mapping[str, Any], policy: DispatchPolicy | None = None
) -> dict[str, Any]:
    """The ``config.toml`` tables the dashboard displays read-only, re-read on every call.

    With ``policy``, also the slot ``limits`` in force: what the document asks for under this
    file's fallback and ceiling, which is what a process reading the same file will dispatch by.
    """
    from .config import ConfigError, capacity_limits, concurrency_limits, load_config

    try:
        settings = load_config(config_file)
        capacity = capacity_limits(settings)
        concurrency = concurrency_limits(settings, profiles)
    except ConfigError as exc:
        return {"config_file": str(config_file), "error": str(exc)}
    managed: dict[str, Any] = {
        "config_file": str(config_file),
        "concurrency": concurrency,
        "capacity": {"per_provider_max": capacity.per_provider_max, "total_max": capacity.total_max},
    }
    if policy is not None:
        managed["limits"] = effective_limits(
            policy, concurrency, per_provider_max=capacity.per_provider_max, total_max=capacity.total_max
        )
    return managed


def slot_limits_for(store: Any, config_file: Any, profiles: Mapping[str, Any]) -> dict[str, int]:
    """Each provider's slot limit as a reader outside the server sees it; empty when the file is broken."""
    limits = file_managed(config_file, profiles, load(store, profiles).policy).get("limits")
    return {name: info["limit"] for name, info in (limits or {}).get("providers", {}).items()}


# -- concurrency: what the policy asks for, under the file's ceiling -----------------------------


def effective_limits(
    policy: DispatchPolicy,
    configured: Mapping[str, int],
    *,
    per_provider_max: int,
    total_max: int | None = None,
) -> dict[str, Any]:
    """The slot limits in force: the policy's where set, ``[concurrency]`` otherwise.

    ``configured`` is ``config.concurrency_limits``, which already defaults an omitted provider
    to one, and is used as written: it is the operator's own file. A value from the policy is
    capped by ``[capacity]``, because the policy can be edited from the dashboard and the file
    cannot, so nothing here can raise that ceiling.
    """
    providers: dict[str, dict[str, Any]] = {}
    for name in sorted(set(configured) | set(policy.providers)):
        spec = policy.providers.get(name)
        asked = spec.max_concurrent if spec is not None else None
        providers[name] = {
            "limit": min(asked, per_provider_max) if asked is not None else int(configured.get(name, 1)),
            "source": "policy" if asked is not None else "config",
            "ceiling": per_provider_max,
        }
    totals = [value for value in (policy.max_concurrent_total, total_max) if value is not None]
    return {
        "providers": providers,
        "total": {
            "limit": min(totals) if totals else None,
            "source": (
                None if not totals
                else "policy" if policy.max_concurrent_total == min(totals) else "config"
            ),
            "ceiling": total_max,
        },
    }


# -- status: observed usage against the policy ---------------------------------------------------


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _budget_status(limit: int | None, used: int) -> dict[str, Any]:
    if limit is None:
        return {"limit": None, "used": used, "remaining": None, "exhausted": False}
    return {"limit": limit, "used": used, "remaining": max(0, limit - used), "exhausted": used >= limit}


def status(store: Any, loaded: LoadedPolicy, profiles: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Observed turns/tokens per provider per window, shares against targets, and budget state.

    Tokens are prompt plus completion tokens from recorded telemetry; turns without telemetry
    still count as turns. Shares are computed across enabled providers only.
    """
    policy = loaded.policy
    totals: dict[str, dict[str, dict[str, int]]] = {}
    window_start: dict[str, str] = {}
    for window in WINDOWS:
        since = _stamp(now - _WINDOW_SPANS[window])
        window_start[window] = since
        totals[window] = {
            row["provider"]: {
                "turns": int(row["turns"]),
                "telemetry_turns": int(row["telemetry_turns"]),
                "tokens": int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0),
            }
            for row in store.provider_turn_totals(since=since)
        }

    levels = getattr(store, "latest_ladder_levels", None)
    previous_levels: dict[str, int] = levels(since=window_start[policy.share_window]) if levels else {}

    names = sorted(set(profiles) | set(policy.providers))
    enabled = {name for name in names if policy.providers.get(name, ProviderPolicy()).enabled}
    target_total = sum(policy.providers[n].target_share or 0 for n in enabled if n in policy.providers)

    providers: dict[str, dict[str, Any]] = {}
    for name in names:
        spec = policy.providers.get(name, ProviderPolicy())
        observed: dict[str, dict[str, Any]] = {}
        budgets: dict[str, dict[str, Any]] = {}
        exhausted = False
        enforced = False
        for window in WINDOWS:
            counts = totals[window].get(name, {"turns": 0, "telemetry_turns": 0, "tokens": 0})
            sum_turns = sum(totals[window].get(n, {}).get("turns", 0) for n in enabled)
            sum_tokens = sum(totals[window].get(n, {}).get("tokens", 0) for n in enabled)
            observed[window] = {
                **counts,
                "share_turns": (counts["turns"] / sum_turns) if name in enabled and sum_turns else None,
                "share_tokens": (counts["tokens"] / sum_tokens) if name in enabled and sum_tokens else None,
            }
            budget = spec.budgets.get(window)
            if budget is not None and (budget.turns is not None or budget.tokens is not None):
                turns_status = _budget_status(budget.turns, counts["turns"])
                tokens_status = _budget_status(budget.tokens, counts["tokens"])
                window_exhausted = turns_status["exhausted"] or tokens_status["exhausted"]
                budgets[window] = {
                    "turns": turns_status,
                    "tokens": tokens_status,
                    "enforce": budget.enforce,
                    "exhausted": window_exhausted,
                    "window_start": window_start[window],
                }
                exhausted = exhausted or window_exhausted
                enforced = enforced or (window_exhausted and budget.enforce)
        target_norm = None
        if spec.target_share is not None and name in enabled and target_total:
            target_norm = spec.target_share / target_total
        share = observed[policy.share_window]["share_turns"]
        if not spec.enabled:
            share_state = "paused"
        elif target_norm is None:
            share_state = "untracked"
        elif share is None:
            share_state = "under_target" if target_norm > 0 else "on_target"
        elif share < target_norm - _SHARE_BAND:
            share_state = "under_target"
        elif share > target_norm + _SHARE_BAND:
            share_state = "over_target"
        else:
            share_state = "on_target"
        budget_ratio = max(
            (
                part["used"] / part["limit"]
                for budget in budgets.values()
                for part in (budget["turns"], budget["tokens"])
                if part["limit"]
            ),
            default=None,
        )
        deficit_points = None
        if target_norm is not None:
            deficit_points = round((target_norm - (share or 0.0)) * 100, 2)
        providers[name] = {
            "share_deficit": None if deficit_points is None else round(deficit_points / 100, 4),
            "max_concurrent": spec.max_concurrent,
            "escalation": _escalation(
                policy.escalation,
                profile=profiles.get(name),
                share_state=share_state,
                deficit_points=deficit_points,
                sample_turns=sum(totals[policy.share_window].get(n, {}).get("turns", 0) for n in enabled),
                previous=previous_levels.get(name, 0),
                availability=_availability_state(store, profiles.get(name), now),
                window_percent=_window_percent(store, profiles.get(name), now),
                budget_ratio=budget_ratio,
            ),
            "enabled": spec.enabled,
            "state": "paused" if not spec.enabled else ("budget_exhausted" if exhausted else "active"),
            "enforced_exhaustion": enforced,
            "target_share": spec.target_share,
            "target_share_normalized": target_norm,
            "share_state": share_state,
            "observed": observed,
            "budgets": budgets,
            "allowed_modes": [m.value for m in spec.allowed_modes]
            if spec.allowed_modes is not None
            else None,
            "note": spec.note,
            "advertised_models": list(spec.advertised_models),
            "advertised_efforts": list(spec.advertised_efforts),
            "models_without_effort": list(spec.models_without_effort),
        }

    def deficit(name: str) -> float:
        info = providers[name]
        return (info["target_share_normalized"] or 0.0) - (
            info["observed"][policy.share_window]["share_turns"] or 0.0
        )

    under_target_order = sorted(
        (n for n in enabled if providers[n]["target_share_normalized"] is not None),
        key=lambda n: (-deficit(n), n),
    )
    roles: dict[str, dict[str, Any]] = {}
    for role, role_spec in policy.roles.items():
        effective: dict[str, dict[str, Any]] = {}
        for name in role_spec.selections:
            level = providers.get(name, {}).get("escalation", {}).get("level", 0)
            chosen, step = step_selection(role_spec, name, level)
            effective[name] = {"model": chosen.model, "effort": chosen.effort, "level": step}
        roles[role] = {"fanout": role_spec.fanout, "effective_selections": effective}
    return {
        "share_window": policy.share_window,
        "window_start": window_start,
        "observed_at": _stamp(now),
        "providers": providers,
        "roles": roles,
        "under_target_order": under_target_order,
        "under_target": [{"provider": n, "deficit": round(deficit(n), 4)} for n in under_target_order],
        "note": (
            "Observed usage is what this host recorded from its own turns; it is not provider quota. "
            "Budgets are operator limits and advisory unless enforced."
        ),
    }


# -- escalation: which ladder step a provider is on ----------------------------------------------


def _availability_state(store: Any, profile: Any, now: datetime) -> str:
    from .providers import Profile
    from .service import provider_availability, provider_eligible

    if not isinstance(profile, Profile) or not hasattr(store, "get_provider_status"):
        return "ok"
    availability = provider_availability(store, profile, now=now)
    return "ok" if provider_eligible(availability, now) else str(availability["state"])


def _window_percent(store: Any, profile: Any, now: datetime) -> float | None:
    """The fullest unexpired provider-reported usage window; only Claude reports one."""
    from .limits import status_key
    from .providers import Profile

    reader = getattr(store, "latest_provider_windows", None)
    if reader is None or not isinstance(profile, Profile):
        return None
    fullest: float | None = None
    for row in reader(status_key(profile)):
        if row.get("used_percent") is None or row.get("resolved_at"):
            continue
        resets = row.get("resets_at")
        if resets:
            try:
                if datetime.fromisoformat(str(resets).replace("Z", "+00:00")) <= now:
                    continue
            except ValueError:
                continue
        fullest = max(fullest or 0.0, float(row["used_percent"]))
    return fullest


def _magnitude(distance: float, held: int, steps: list[int], release: int) -> int:
    """How many thresholds ``distance`` has crossed, holding a level until it is ``release`` back inside.

    A Schmitt trigger: rising needs the threshold itself, falling needs ``release`` points less,
    so a share sitting on a threshold cannot flip the level from one read to the next.
    """
    rising = sum(1 for step in steps if distance >= step)
    holding = max(
        (k for k in range(1, min(held, len(steps)) + 1) if distance >= steps[k - 1] - release), default=0
    )
    return max(rising, holding)


def _escalation(
    rules: Escalation,
    *,
    profile: Any,
    share_state: str,
    deficit_points: float | None,
    sample_turns: int,
    previous: int,
    availability: str,
    window_percent: float | None,
    budget_ratio: float | None,
) -> dict[str, Any]:
    signal = {
        "deficit_points": deficit_points,
        "sample_turns": sample_turns,
        "availability": availability,
        "window_used_percent": window_percent,
        "budget_used_ratio": None if budget_ratio is None else round(budget_ratio, 4),
    }

    def result(level: int, reason: str) -> dict[str, Any]:
        return {"level": level, "previous_level": previous, "reason": reason, "signal": signal}

    if not rules.enabled:
        return result(0, "escalation is off")
    if profile is None or not _steerable(profile):
        return result(0, "only a built-in subscription profile is stepped")
    if share_state in {"paused", "untracked"} or deficit_points is None:
        return result(0, f"provider is {share_state}; there is no target to measure against")

    if sample_turns < rules.min_turns:
        level = 0
        reason = f"{sample_turns} turns in the window, below the {rules.min_turns} needed to judge a share"
    elif deficit_points > 0:
        level = _magnitude(deficit_points, max(previous, 0), rules.step_up_points, rules.release_points)
        reason = f"{deficit_points:g} points under target"
    elif deficit_points < 0:
        level = -_magnitude(-deficit_points, max(-previous, 0), rules.step_up_points, rules.release_points)
        reason = f"{-deficit_points:g} points over target"
    else:
        level, reason = 0, "on target"

    # Brakes only ever lower the level. Each is evidence the pool is close to a limit, which a
    # share deficit knows nothing about.
    brakes: list[tuple[int, str]] = []
    if availability != "ok":
        brakes.append((0, f"provider is {availability}"))
    if window_percent is not None:
        if window_percent >= rules.window_down_percent:
            brakes.append((-1, f"usage window at {window_percent:g}%, over {rules.window_down_percent}%"))
        elif window_percent >= rules.window_hold_percent:
            brakes.append((0, f"usage window at {window_percent:g}%, over {rules.window_hold_percent}%"))
    if budget_ratio is not None:
        if budget_ratio >= rules.budget_down_ratio:
            brakes.append((-1, f"a budget is {budget_ratio:.0%} used"))
        elif budget_ratio >= rules.budget_hold_ratio:
            brakes.append((0, f"a budget is {budget_ratio:.0%} used"))
    for cap, why in sorted(brakes):
        if level > cap:
            return result(cap, f"held at {cap:+d}: {why}" if cap else f"held at 0: {why}")
    return result(level, f"step {level:+d}: {reason}" if level else reason)


def step_selection(role: RolePolicy, provider: str, level: int) -> tuple[Selection, int]:
    """The selection ``level`` steps from the role's base, and the step actually reached.

    A level beyond the end of a ladder stops at its last step, so a short ladder is simply a
    smaller range, not an error.
    """
    base = role.selections.get(provider) or Selection()
    ladder = role.ladders.get(provider)
    if ladder is None or level == 0:
        return base, 0
    if level > 0:
        reached = min(level, len(ladder.above))
        return (ladder.above[reached - 1], reached) if reached else (base, 0)
    reached = min(-level, len(ladder.below))
    return (ladder.below[reached - 1], -reached) if reached else (base, 0)


# -- filling a task's model and effort from its role ----------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """The model and effort a task will run with, and where each came from."""

    model: str | None
    effort: str | None
    #: ``caller`` (sent both), ``policy`` (filled both), ``mixed``, or ``profile`` (nothing to fill).
    source: str
    role: str | None
    ladder_level: int | None
    reason: str
    policy_revision: int
    caller_model: str | None = None
    caller_effort: str | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "effort": self.effort,
            "source": self.source,
            "role": self.role,
            "ladder_level": self.ladder_level,
            "policy_revision": self.policy_revision,
            "reason": self.reason,
        }


def resolve_selection(
    loaded: LoadedPolicy,
    status_report: Mapping[str, Any],
    profile: Any,
    *,
    role: str | None,
    model: str | None,
    effort: str | None,
) -> Resolution:
    """Fill what the caller left out from the role's current ladder step. Never refuses.

    Whatever the caller sent is kept exactly. The fill is skipped, with the reason recorded,
    whenever it cannot be done cleanly: the task would run on the profile's own defaults, as it
    did before the server filled anything.
    """

    def kept(reason: str) -> Resolution:
        source = "caller" if model is not None or effort is not None else "profile"
        return Resolution(model, effort, source, role, None, reason, loaded.revision, model, effort)

    if model is not None and effort is not None:
        return kept("the caller sent both model and effort")
    if role is None:
        return kept("no role was named")
    spec = loaded.policy.roles.get(role)
    if spec is None:
        return kept(f"role {role!r} is not in the policy")
    if not _steerable(profile):
        return kept("only a built-in subscription profile is filled")
    if profile.id not in spec.selections:
        return kept(f"role {role!r} has no selection for {profile.id}")

    info = status_report["providers"].get(profile.id, {})
    level = int(info.get("escalation", {}).get("level", 0))
    why = str(info.get("escalation", {}).get("reason", ""))
    step, reached = step_selection(spec, profile.id, level)
    provider_policy = loaded.policy.providers.get(profile.id)
    no_effort = set(provider_policy.models_without_effort) if provider_policy else set()

    if model is None and effort is None:
        chosen = Selection(model=step.model, effort=step.effort)
    elif model is not None:
        # The step's effort belongs to the step's model; it is not borrowed for another one.
        if step.model != model or model in no_effort or step.effort is None:
            return kept("the caller named a model the role's step does not pair an effort with")
        chosen = Selection(model=model, effort=step.effort)
    else:
        if step.model is None or step.model in no_effort:
            return kept("the role's step has no model that takes the caller's effort")
        chosen = Selection(model=step.model, effort=effort)
    if chosen.model is None and chosen.effort is None:
        return kept(f"role {role!r} selects nothing for {profile.id}")
    if _check_selection(("selection",), chosen, profile, provider_policy):
        return kept("the role's step is not valid for this provider")
    source = "policy" if model is None and effort is None else "mixed"
    return Resolution(
        chosen.model, chosen.effort, source, role, reached, why, loaded.revision, model, effort
    )


def admission_refusal(
    loaded: LoadedPolicy, status_report: Mapping[str, Any], provider: str
) -> dict[str, Any] | None:
    """The details of an enforced, exhausted budget for ``provider``, or None when admission is fine."""
    info = status_report["providers"].get(provider)
    if info is None or not info["enforced_exhaustion"]:
        return None
    for window, budget in info["budgets"].items():
        if not (budget["exhausted"] and budget["enforce"]):
            continue
        kind = "turns" if budget["turns"]["exhausted"] else "tokens"
        return {
            "provider": provider,
            "window": window,
            "kind": kind,
            "limit": budget[kind]["limit"],
            "used": budget[kind]["used"],
            "window_start": budget["window_start"],
            "policy_revision": loaded.revision,
        }
    return None
