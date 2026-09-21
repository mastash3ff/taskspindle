"""Dormant native OpenCode ACP preparation, with no launch or credential enrollment.

This is a candidate containment configuration, not a qualified runtime boundary.
Enrollment must verify project-config/plugin discovery against the pinned binary;
TaskSpindle refuses every dispatch until that separate work is implemented.
"""
from __future__ import annotations

import json
from pathlib import Path


def isolated_environment(task_dir: Path) -> dict[str, str]:
    """Return a fresh, credential-free environment proposal; do not create files.

    Callers must provide a private empty HOME and XDG tree in a sandbox that hides
    project .opencode directories, user hooks, plugins and MCP configuration.
    No inherited configuration or API keys are accepted by this interface.
    """
    home = task_dir / "opencode-home"
    config = {
        "enabled_providers": ["opencode-go"], "plugin": [], "mcp": {},
        "permission": {"*": "deny", "task": "deny", "skill": "deny"},
        "share": "disabled", "autoupdate": False,
    }
    return {
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "OPENCODE_CONFIG_DIR": str(home / ".config/opencode"),
        "OPENCODE_CONFIG_CONTENT": json.dumps(config, sort_keys=True),
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
        "OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS": "false",
    }


def validate_selection(model: str, credential_provider: str) -> None:
    """Refuse cross-provider IDs without looking up an account or a credential."""
    if credential_provider != "opencode-go" or not model.startswith("opencode-go/"):
        raise ValueError("OpenCode Go requires a Go-bound credential and opencode-go/<model-id>")
    if not model.removeprefix("opencode-go/") or model.count("/") != 1:
        raise ValueError("A concrete Go model ID is required")
