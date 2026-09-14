"""Dispatch policy: operator preferences for how Codex spreads work across providers.

The policy is advisory data. It never selects a provider, moves a task, or retries on another
provider: Codex still names the provider on every ``start_task``. The only server-side effect is
an *enforced* budget, which refuses admission of a new task on that provider for the rest of
the window. Nothing here calls a provider or reads provider quota; observed usage is what this
host recorded from its own turns.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import agy
from .models import Mode

__all__ = [
    "DEFAULT_ROLES",
    "WINDOWS",
    "Budget",
    "DispatchPolicy",
    "LoadedPolicy",
    "PolicyError",
    "ProviderPolicy",
    "RolePolicy",
    "Selection",
    "canonical_json",
    "defaults",
    "fingerprint",
    "load",
    "status",
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


class RolePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: str = Field(default="", max_length=MAX_NOTE)
    provider_preference: list[str] = Field(default_factory=list)
    selections: dict[str, Selection] = Field(default_factory=dict)
    timeout_s: int | None = Field(default=None, ge=TIMEOUT_BOUNDS[0], le=TIMEOUT_BOUNDS[1])

    @field_validator("provider_preference")
    @classmethod
    def _unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("provider_preference repeats a provider")
        return values


class DispatchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    share_window: Window = "week"
    providers: dict[str, ProviderPolicy] = Field(default_factory=dict)
    roles: dict[str, RolePolicy] = Field(default_factory=dict, max_length=MAX_ROLES)

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
        "advertised_models": ["gemini-3.8-flash-medium", "gemini-3.1-pro-high"],
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
        "selections": {
            "claude": {"model": "opus[1m]", "effort": "xhigh"},
            "grok": {"model": "grok-4.6", "effort": "high"},
            "agy": {"model": "gemini-3.1-pro-high", "effort": "high"},
        },
    },
    "debugger": {
        "brief": (
            "Reproduce and localize unexpected behavior, test hypotheses against evidence, and "
            "identify the smallest justified repair and verification."
        ),
        "selections": {
            "claude": {"model": "opus[1m]", "effort": "xhigh"},
            "grok": {"model": "grok-4.6", "effort": "high"},
            "agy": {"model": "gemini-3.1-pro-high", "effort": "high"},
        },
    },
    "reviewer": {
        "brief": (
            "Independently inspect the exact target against its requirements and report "
            "prioritized, actionable findings with evidence."
        ),
        "selections": {
            "claude": {"model": "opus[1m]", "effort": "xhigh"},
            "grok": {"model": "grok-4.6", "effort": "high"},
            "agy": {"model": "gemini-3.1-pro-high", "effort": "high"},
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
    first_class = [name for name in _PREFERENCE_ORDER if name in profiles]
    roles: dict[str, RolePolicy] = {}
    for role, spec in DEFAULT_ROLES.items():
        selections = {
            name: Selection(**spec["selections"][name]) for name in first_class if name in spec["selections"]
        }
        roles[role] = RolePolicy(
            brief=spec["brief"], provider_preference=list(first_class), selections=selections
        )
    return DispatchPolicy(providers=providers, roles=roles)


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
    return errors


def _check_selection(
    loc: tuple[Any, ...], selection: Selection, profile: Any, provider: ProviderPolicy | None
) -> list[dict[str, Any]]:
    family = _family(profile)
    if family == "agy":
        try:
            agy.validate_selection(selection.model, selection.effort)
        except agy.ModelSelectionError as exc:
            return [_error(loc, str(exc), exc.code)]
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


def canonical_json(policy: DispatchPolicy) -> str:
    return json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


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
        providers[name] = {
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
    return {
        "share_window": policy.share_window,
        "window_start": window_start,
        "observed_at": _stamp(now),
        "providers": providers,
        "under_target_order": under_target_order,
        "note": (
            "Observed usage is what this host recorded from its own turns; it is not provider quota. "
            "Budgets are operator limits and advisory unless enforced."
        ),
    }


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
