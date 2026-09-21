"""Offline enrollment checks. No input to this module can enable a dormant adapter.

Evidence references are operator-supplied, untrusted claims until a later, separately
implemented enrollment flow verifies account binding, runtime containment and billing.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .providers import Profile, ProfileError

DORMANT = frozenset({"muse", "opencode-go"})
_REQUIRED = (
    "binary_identity", "protocol", "tool_containment", "nonrecursive",
    "account_binding", "subscription_billing", "model_catalog", "quota_scope",
    "cancellation", "continuation",
)


def check(profile: Profile, evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Report missing evidence and consistency errors without login, I/O, or activation."""
    evidence = evidence or {}
    required = list(_REQUIRED)
    errors = []
    if profile.family == "opencode-go":
        required += ["go_credential_binding", "zen_use_balance_disabled", "isolated_configuration"]
        balance = evidence.get("zen_use_balance_disabled")
        if balance is not None and (not isinstance(balance, Mapping) or balance.get("value") is not True):
            errors.append("Zen Use balance must be disabled; metered overflow is forbidden")
        if evidence.get("credential_provider") not in {None, "opencode-go"}:
            errors.append("Credential must be bound to OpenCode Go, not the Zen pay-as-you-go provider")
    elif profile.family == "muse":
        required += ["api_key_precedence_excluded", "native_cli_identity"]
    else:
        raise ProfileError("PROFILE_INVALID", "Enrollment checking is only available for dormant adapters")
    # A true flag is only a claim, not a verifiable evidence reference. No credentials belong here.
    missing = [name for name in required
               if not isinstance(evidence.get(name), Mapping)
               or not evidence[name].get("source") or not evidence[name].get("sha256")]
    code = "MUSE_NOT_QUALIFIED" if profile.family == "muse" else "OPENCODE_GO_NOT_QUALIFIED"
    return {
        "enabled": False, "code": code,
        "reason": f"{profile.family} is disabled pending runtime, billing, and account qualification.",
        "live_enrollment_supported": False, "required_evidence": required,
        "missing_evidence": missing, "errors": errors, "qualified_modes": [],
        "evidence_status": "unverified operator claims",
        "subscription_route": "unverified", "tool_containment": "unverified",
    }


def require_enabled(profile: Profile) -> None:
    if profile.family in DORMANT:
        result = check(profile)
        raise ProfileError(result["code"], result["reason"])


def adapter_capabilities(profile: Profile) -> frozenset[str]:
    """Capabilities of recognized, already supported adapters, never a config boolean.

    New routes require code and contract tests here. ``first_class`` and OAuth alone
    do not qualify a command. Registered builtin identity and account/quota binding
    are required; aliases retain their existing admission and steering behavior.
    """
    if (profile.family in DORMANT or not profile.command
            or profile.id != profile.family or profile.auth_method != "oauth"
            or profile.secret_env or profile.gateway_host
            or profile.account_scope != f"{profile.family}:personal"
            or profile.quota_scope != f"{profile.family}:personal"):
        return frozenset()
    from pathlib import Path

    argv = profile.command
    name = Path(argv[0]).name
    recognized = (
        (profile.family == "claude" and name == "claude-agent-acp" and len(argv) == 1)
        or (profile.family == "grok" and name == "grok" and len(argv) == 9
            and argv[1:4] == ("--no-subagents", "agent", "--model")
            and argv[5] == "--reasoning-effort" and argv[7:] == ("--no-leader", "stdio"))
    )
    if profile.family == "agy":
        recognized = name == "agy" and len(argv) == 1
    return frozenset({"model_selection", "effort_selection", "tool_policy"}) if recognized else frozenset()


def require_promotable(profile: Profile) -> None:
    require_enabled(profile)
    if profile.billing_type != "subscription":
        raise ProfileError("METERED_NOT_ALLOWED", "Promotion requires verified subscription billing metadata")
    if "model_selection" not in adapter_capabilities(profile):
        raise ProfileError("ADAPTER_NOT_QUALIFIED", "Adapter is not qualified for model selection")
