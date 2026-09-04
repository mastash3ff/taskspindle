"""Provider profiles, the child environment allowlist, and OAuth evidence.

A *profile* is everything TaskSpindle needs to launch one ACP agent: the argv, the environment it
is allowed to see, how it authenticates, and which task modes it may serve. Two profiles are
built in and first class -- ``claude`` (the pinned ``claude-agent-acp`` adapter) and ``grok`` (the
native ``grok agent ... stdio`` endpoint), both OAuth-only. Anything else is a configured,
second-class profile from ``[providers.<id>]`` in ``config.toml``.

The child environment is built by *allowlist*, never by filtering the parent: a name reaches the
agent only because this module put it there. :func:`env_violations` is the belt to that braces --
a programming-error guard asserting no credential-shaped name leaked through.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

__all__ = [
    "ALL_MODES",
    "FIRST_CLASS",
    "FORBIDDEN_ENV_PATTERNS",
    "Profile",
    "ProfileError",
    "build_child_env",
    "builtin_profiles",
    "claude_oauth_evidence",
    "default_runner",
    "env_violations",
    "grok_oauth_evidence",
    "grok_overlay_path",
    "load_profiles",
    "opposite_provider",
    "pinned_node",
    "profile_for_task",
    "reviewer_independent",
    "session_options",
    "write_grok_overlay",
]

#: The two providers TaskSpindle ships with. Their ids are reserved.
FIRST_CLASS: tuple[str, ...] = ("claude", "grok")

#: Every task mode. A profile serves all three unless its config narrows the set.
ALL_MODES = frozenset({"consult", "review", "implement"})

_AUTH_MODES = ("oauth", "api_key")

#: Names copied from the parent environment when present. Nothing else is inherited.
ENV_ALLOWLIST: tuple[str, ...] = (
    "HOME",
    "LANG",
    "LC_ALL",
    "USER",
    "LOGNAME",
    "XDG_RUNTIME_DIR",
    "NO_COLOR",
)

_FORBIDDEN_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PROXY", "_BASE_URL")
_FORBIDDEN_PREFIXES = ("ANTHROPIC_", "OPENAI_", "XAI_", "AWS_", "GOOGLE_", "AZURE_", "LITELLM_")
_FORBIDDEN_EXACT = ("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "NPM_TOKEN")

#: Human-readable description of what :func:`env_violations` rejects.
FORBIDDEN_ENV_PATTERNS: tuple[str, ...] = (
    *(f"*{suffix}" for suffix in _FORBIDDEN_SUFFIXES),
    *(f"{prefix}*" for prefix in _FORBIDDEN_PREFIXES),
    *_FORBIDDEN_EXACT,
)

#: Written next to the state dir and pointed at by ``GROK_CONFIG``. Grok 1.0.13 does not apply
#: ``[compat.*]`` keys from a ``GROK_CONFIG`` overlay (verified with ``grok inspect``), so the
#: compatibility sources are switched off through :data:`GROK_COMPAT_ENV` instead; the overlay only
#: backs up the ``--no-subagents`` launch flag.
GROK_OVERLAY_TOML = """# Written by TaskSpindle. Compatibility sources are disabled through the
# GROK_*_ENABLED environment variables; this overlay only backs up --no-subagents.
[subagents]
enabled = false
"""

#: One switch per vendor source Grok would otherwise import (verified with ``grok inspect`` on
#: Grok 1.0.13: every one of these reports ``OFF (env)``).
GROK_COMPAT_ENV: dict[str, str] = {
    f"GROK_{vendor}_{source}_ENABLED": "false"
    for vendor in ("CLAUDE", "CURSOR", "CODEX")
    for source in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS", "SESSIONS")
}

_GROK_DEFAULT_MODEL = "grok-4.6"
_GROK_DEFAULT_EFFORT = "medium"


class ProfileError(Exception):
    """A profile could not be resolved, or refused a task.

    ``code`` is one of ``PROFILE_UNKNOWN``, ``PROFILE_INVALID``, ``OAUTH_REJECTED``,
    ``MODE_NOT_ALLOWED``, ``METERED_NOT_ALLOWED``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Profile:
    """One launchable provider configuration."""

    id: str
    auth: Literal["oauth", "api_key"]
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    secret_env: tuple[str, ...] = ()
    model: str | None = None
    effort: str | None = None
    modes: frozenset[str] = ALL_MODES
    first_class: bool = False
    base: str | None = None
    #: Host of ``ANTHROPIC_BASE_URL`` / ``OPENAI_BASE_URL`` in :attr:`env`, for attribution only.
    gateway_host: str | None = None

    @property
    def family(self) -> str:
        """The built-in this profile behaves as: its own id, or the id it is based on."""
        return self.base or self.id


# --------------------------------------------------------------------------------------------
# forbidden names


def _is_forbidden(name: str) -> bool:
    upper = name.upper()
    if upper in _FORBIDDEN_EXACT:
        return True
    if upper.startswith(_FORBIDDEN_PREFIXES):
        return True
    return upper.endswith(_FORBIDDEN_SUFFIXES)


def env_violations(env: Mapping[str, str], *, allowed: Sequence[str] = ()) -> list[str]:
    """Return the credential-shaped names in ``env`` that are not explicitly ``allowed``.

    Matching is case-insensitive throughout -- the brief only requires it for ``*_PROXY``, but
    denying ``https_proxy`` and ``Anthropic_Api_Key`` alike costs nothing and leaks less.
    """
    permitted = set(allowed)
    return [name for name in env if name not in permitted and _is_forbidden(name)]


def _gateway_host(env: Mapping[str, str]) -> str | None:
    for name in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
        value = env.get(name)
        if not value:
            continue
        host = urlsplit(value).hostname
        if host:
            return host
    return None


# --------------------------------------------------------------------------------------------
# built-in profiles


def grok_overlay_path(state_dir: Path) -> Path:
    """Where the Grok compatibility overlay lives."""
    return state_dir / "grok-overlay.toml"


def write_grok_overlay(state_dir: Path) -> Path:
    """Write (or rewrite) the Grok overlay at mode 0o600 and return its path.

    Idempotent: the content is fixed, so a rewrite is a no-op in effect. The file is created
    0o600 from the start and re-chmodded in case it already existed with looser bits.
    """
    path = grok_overlay_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(GROK_OVERLAY_TOML)
    path.chmod(0o600)
    return path


def _grok_command(model: str, effort: str) -> tuple[str, ...]:
    # ``--no-subagents`` is a top-level ``grok`` flag; ``grok agent`` itself only accepts
    # ``--model``, ``--reasoning-effort`` and ``--no-leader`` (Grok 1.0.13 rejects it after ``agent``).
    return (
        "grok",
        "--no-subagents",
        "agent",
        "--model",
        model,
        "--reasoning-effort",
        effort,
        "--no-leader",
        "stdio",
    )


def builtin_profiles(runtime_dir: Path, *, home: Path, state_dir: Path) -> dict[str, Profile]:
    """The two first-class OAuth profiles.

    ``state_dir`` is required (beyond the brief's signature) because the ``grok`` profile's
    ``GROK_CONFIG`` must point at the overlay that disables hooks, skills, MCPs and subagents;
    the path is derived here, and :func:`write_grok_overlay` puts the file there.
    """
    claude_env = {"CLAUDE_CONFIG_DIR": str(home / ".claude")}
    grok_env = {
        "GROK_DISABLE_API_KEY_AUTH": "true",
        **GROK_COMPAT_ENV,
        "GROK_CONFIG": str(grok_overlay_path(state_dir)),
    }
    return {
        "claude": Profile(
            id="claude",
            auth="oauth",
            command=(str(runtime_dir / "node_modules" / ".bin" / "claude-agent-acp"),),
            env=claude_env,
            model=None,
            effort=None,
            modes=ALL_MODES,
            first_class=True,
        ),
        "grok": Profile(
            id="grok",
            auth="oauth",
            command=_grok_command(_GROK_DEFAULT_MODEL, _GROK_DEFAULT_EFFORT),
            env=grok_env,
            model=_GROK_DEFAULT_MODEL,
            effort=_GROK_DEFAULT_EFFORT,
            modes=ALL_MODES,
            first_class=True,
        ),
    }


# --------------------------------------------------------------------------------------------
# configured profiles


def _invalid(profile_id: str, detail: str) -> ProfileError:
    return ProfileError("PROFILE_INVALID", f"provider {profile_id!r}: {detail}")


def _str_list(profile_id: str, table: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    raw = table.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise _invalid(profile_id, f"{key} must be a list of strings")
    return tuple(raw)


def _str_map(profile_id: str, table: Mapping[str, Any], key: str) -> dict[str, str]:
    raw = table.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in raw.items()
    ):
        raise _invalid(profile_id, f"{key} must be a table of strings")
    return dict(raw)


def _optional_str(profile_id: str, table: Mapping[str, Any], key: str) -> str | None:
    raw = table.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _invalid(profile_id, f"{key} must be a string")
    return raw


def _configured_profile(
    profile_id: str,
    table: Mapping[str, Any],
    builtins: Mapping[str, Profile],
) -> Profile:
    if profile_id in FIRST_CLASS:
        raise _invalid(profile_id, "id is reserved for a built-in provider")
    if not isinstance(table, dict):
        raise _invalid(profile_id, "must be a table")

    base_id = _optional_str(profile_id, table, "base")
    if base_id is not None and base_id not in FIRST_CLASS:
        raise _invalid(profile_id, f"base must be one of {', '.join(FIRST_CLASS)}")
    base = builtins[base_id] if base_id is not None else None

    auth = table.get("auth")
    if auth not in _AUTH_MODES:
        raise _invalid(profile_id, f"auth must be one of {', '.join(_AUTH_MODES)}")

    secret_env = _str_list(profile_id, table, "secret_env") or ()
    if secret_env and auth == "oauth":
        raise _invalid(profile_id, "secret_env is only meaningful for api_key providers")

    model = _optional_str(profile_id, table, "model") or (base.model if base else None)
    effort = _optional_str(profile_id, table, "effort") or (base.effort if base else None)

    mode_names = _str_list(profile_id, table, "modes")
    modes = ALL_MODES if mode_names is None else frozenset(mode_names)
    unknown = modes - ALL_MODES
    if unknown:
        raise _invalid(profile_id, f"unknown modes: {', '.join(sorted(unknown))}")

    command = _str_list(profile_id, table, "command")
    if command is None:
        if base is None:
            raise _invalid(profile_id, "command is required for a provider without a base")
        # A grok-derived profile carries its model and effort on the argv; a claude-derived one
        # passes them through session_options instead, so its argv is the adapter's untouched.
        command = (
            _grok_command(model or _GROK_DEFAULT_MODEL, effort or _GROK_DEFAULT_EFFORT)
            if base.id == "grok"
            else base.command
        )
    if not command:
        raise _invalid(profile_id, "command must not be empty")

    env = dict(base.env) if base else {}
    env.update(_str_map(profile_id, table, "env"))

    return Profile(
        id=profile_id,
        auth=auth,
        command=command,
        env=env,
        secret_env=secret_env,
        model=model,
        effort=effort,
        modes=modes,
        first_class=False,
        base=base_id,
        gateway_host=_gateway_host(env),
    )


def load_profiles(
    config: Mapping[str, Any],
    *,
    runtime_dir: Path,
    home: Path,
    state_dir: Path,
) -> dict[str, Profile]:
    """Built-in profiles plus every ``[providers.<id>]`` table in ``config``."""
    write_grok_overlay(state_dir)
    profiles = builtin_profiles(runtime_dir, home=home, state_dir=state_dir)
    providers = config.get("providers") or {}
    if not isinstance(providers, dict):
        raise ProfileError("PROFILE_INVALID", "providers must be a table")
    for profile_id, table in providers.items():
        profiles[profile_id] = _configured_profile(profile_id, table, profiles)
    return profiles


def profile_for_task(
    profiles: Mapping[str, Profile],
    provider_id: str,
    *,
    mode: str,
    allow_metered: bool,
) -> Profile:
    """Pick the profile for a task, or refuse it."""
    profile = profiles.get(provider_id)
    if profile is None:
        raise ProfileError("PROFILE_UNKNOWN", f"no such provider: {provider_id!r}")
    if mode not in profile.modes:
        raise ProfileError("MODE_NOT_ALLOWED", f"provider {provider_id!r} does not serve mode {mode!r}")
    if profile.auth == "api_key" and not allow_metered:
        raise ProfileError(
            "METERED_NOT_ALLOWED",
            f"provider {provider_id!r} bills per token; allow_metered was not set",
        )
    return profile


def opposite_provider(profile_id: str) -> str | None:
    """The other first-class provider, for a default independent reviewer."""
    if profile_id == "claude":
        return "grok"
    if profile_id == "grok":
        return "claude"
    return None


def reviewer_independent(author: Profile, reviewer: Profile) -> bool:
    """Whether ``reviewer`` is a genuinely different agent from ``author``."""
    if author.id == reviewer.id:
        return False
    return author.command != reviewer.command or author.model != reviewer.model


def pinned_node(runtime_dir: Path) -> Path | None:
    """The absolute node ``taskspindle setup`` pinned into ``runtime_dir``, if it did.

    ``taskspindle setup`` writes the node it resolved to ``runtime_dir / "node-path"`` and bakes
    the same path into the adapter launcher shim, so a worker never needs ``node`` on its own
    PATH. A runtime installed before that fix, or one whose file was removed, has no such record
    -- ``None`` says so rather than guessing.
    """
    try:
        text = (runtime_dir / "node-path").read_text(encoding="utf-8")
    except OSError:
        return None
    text = text.strip()
    return Path(text) if text else None


# --------------------------------------------------------------------------------------------
# child environment


def build_child_env(
    profile: Profile,
    parent: Mapping[str, str],
    *,
    task_tmp: Path,
) -> dict[str, str]:
    """Build the environment the agent process will see.

    Built by allowlist: a name is present only because this function put it there. ``PATH`` is
    scrubbed of every ``node_modules/.bin`` entry so a stray global adapter cannot shadow the
    pinned one, and the profile's own runtime bin dir goes back on the front for the Claude
    family. When that runtime pinned a node (see :func:`pinned_node`), its directory is
    prepended ahead of the runtime bin dir too, so the family works even under a PATH that has
    no ``node`` of its own. Secrets are copied only for ``api_key`` profiles, and only by the
    exact names the profile declared.
    """
    env = {name: parent[name] for name in ENV_ALLOWLIST if name in parent}

    entries = [
        entry
        for entry in parent.get("PATH", "").split(os.pathsep)
        if entry and "node_modules/.bin" not in entry
    ]
    if profile.family == "claude":
        launcher = Path(profile.command[0])
        runtime_bin = str(launcher.parent)
        entries.insert(0, runtime_bin)
        node = pinned_node(launcher.parent.parent.parent)
        if node is not None:
            entries.insert(0, str(node.parent))
    env["PATH"] = os.pathsep.join(entries)

    env["TERM"] = "dumb"
    env["CI"] = "1"
    env["NO_BROWSER"] = "1"
    env["TMPDIR"] = str(task_tmp)

    env.update(profile.env)

    if profile.auth == "api_key":
        for name in profile.secret_env:
            if name not in parent:
                raise ProfileError(
                    "PROFILE_INVALID",
                    f"provider {profile.id!r}: secret {name} is not set in the environment",
                )
            env[name] = parent[name]
        allowed: tuple[str, ...] = (*profile.secret_env, *profile.env)
    else:
        # An oauth profile gets no exemptions at all: its own env keys are checked too, so a
        # config cannot smuggle ANTHROPIC_BASE_URL past an OAuth seat.
        allowed = ()

    leaked = env_violations(env, allowed=allowed)
    if leaked:
        # Not an assertion: this guard is the last thing between a credential in the parent
        # environment and the agent process, and it has to hold with optimisations on.
        raise ProfileError(
            "PROFILE_INVALID",
            f"provider {profile.id!r}: forbidden names would reach the child environment: "
            f"{', '.join(sorted(leaked))}",
        )
    return env


# --------------------------------------------------------------------------------------------
# OAuth evidence


def default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a short local command and capture its output as text."""
    return subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)


def _rejected(field_name: str, detail: str) -> ProfileError:
    return ProfileError("OAUTH_REJECTED", f"claude auth status: {field_name} {detail}")


def claude_oauth_evidence(
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] = default_runner,
) -> dict[str, Any]:
    """Prove the local ``claude`` CLI holds a first-party Max subscription seat.

    Returns exactly the four fields that make that claim. Identity -- email, org id -- is never
    read out of the payload, so it cannot reach a log or a task record.
    """
    try:
        completed = run(["claude", "auth", "status"])
    except OSError as exc:
        raise ProfileError("OAUTH_REJECTED", f"could not run claude auth status: {exc}") from exc
    except subprocess.SubprocessError as exc:
        raise ProfileError("OAUTH_REJECTED", f"claude auth status failed: {exc}") from exc

    if completed.returncode != 0:
        raise ProfileError("OAUTH_REJECTED", f"claude auth status exited {completed.returncode}")
    try:
        payload = json.loads(completed.stdout or "")
    except ValueError as exc:
        raise ProfileError("OAUTH_REJECTED", f"claude auth status did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProfileError("OAUTH_REJECTED", "claude auth status did not return a JSON object")

    if payload.get("loggedIn") is not True:
        raise _rejected("loggedIn", "is not true")
    for name, expected in (
        ("authMethod", "claude.ai"),
        ("subscriptionType", "max"),
        ("apiProvider", "firstParty"),
    ):
        if payload.get(name) != expected:
            raise _rejected(name, f"is not {expected!r}")
    return {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "subscriptionType": "max",
        "apiProvider": "firstParty",
    }


def grok_oauth_evidence(auth_methods: Sequence[Mapping[str, Any] | object], *, home: Path) -> dict[str, Any]:
    """Prove the Grok endpoint is on a cached OAuth token rather than an API key."""
    ids: list[str] = []
    for method in auth_methods:
        raw = method.get("id") if isinstance(method, Mapping) else getattr(method, "id", None)
        if isinstance(raw, str):
            ids.append(raw)
    if not any(method_id in {"cached_token", "grok.com"} for method_id in ids):
        raise ProfileError(
            "OAUTH_REJECTED",
            f"grok advertised no OAuth auth method (got: {', '.join(ids) or 'none'})",
        )
    if not (home / ".grok" / "auth.json").is_file():
        raise ProfileError("OAUTH_REJECTED", "grok auth file is missing")
    return {"auth_method_ids": ids, "auth_file_present": True}


# --------------------------------------------------------------------------------------------
# session options


def session_options(profile: Profile) -> dict[str, Any]:
    """The ``_meta`` kwargs to pass to ``new_session`` / ``load_session`` for this profile.

    ``ClientSideConnection.new_session(**kwargs)`` folds every extra keyword into the request's
    ``field_meta`` (serialized under the ``_meta`` alias), so returning ``{"claudeCode": ...}``
    puts the adapter options exactly where the adapter reads them. The options deny the adapter
    its own delegation tools and every settings source and MCP server: a TaskSpindle worker is a
    leaf, never a parent.
    """
    if profile.family != "claude":
        return {}
    options: dict[str, Any] = {
        "settingSources": [],
        "disallowedTools": ["Agent", "Task", "TeamCreate", "SendMessage"],
        "mcpServers": {},
    }
    if profile.model:
        options["model"] = profile.model
    return {"claudeCode": {"options": options}}
