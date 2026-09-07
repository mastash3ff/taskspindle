"""Local account collection commands; no task dispatch or service activation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
import tempfile
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..config import Paths

_PROVIDERS = ("chatgpt", "claude", "google_ai", "grok")
_EXTENSION_GUIDE = "https://github.com/microsoft/playwright/blob/main/packages/extension/README.md"


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "subscriptions", help="connect accounts and collect subscription billing dates"
    )
    commands = parser.add_subparsers(dest="subscription_command", required=True)
    commands.add_parser("setup-browser", help="provision the pinned browser helper")
    commands.add_parser(
        "setup-extension", help="save the normal Chrome extension connection token using hidden input"
    )
    connect = commands.add_parser("connect", help="queue a visible account sign-in")
    connect.add_argument("provider", choices=_PROVIDERS)
    refresh = commands.add_parser("refresh", help="queue a refresh of connected accounts")
    refresh.add_argument("provider", choices=_PROVIDERS, nargs="?")
    status = commands.add_parser("status", help="read the last verified subscription results")
    status.add_argument("--json", action="store_true", help="print structured status")
    watch = commands.add_parser("watch", help="run the independent subscription collector")
    watch.add_argument("--once", action="store_true", help="process at most one due or queued job")
    commands.add_parser("service-unit", help="print a service definition without installing it")


def _setup_extension(paths: Paths) -> None:
    """Keep the browser bridge token out of argv, shell history, JSON and logs."""
    print(f"Install Playwright in your normal Chrome profile: {_EXTENSION_GUIDE}")
    print("Open its status page and copy PLAYWRIGHT_MCP_EXTENSION_TOKEN.")
    if not sys.stdin.isatty():
        raise ValueError("an interactive terminal is required for hidden token input")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        token = getpass.getpass("Extension token (hidden): ")
    if not 1 <= len(token) <= 512 or not token.isascii() or any(
        not 33 <= ord(char) <= 126 for char in token
    ):
        raise ValueError("invalid extension token")
    directory = paths.data_dir / "subscriptions"
    if directory.is_symlink():
        raise ValueError("invalid token directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    target = directory / "extension-token"
    if target.is_symlink():
        raise ValueError("invalid token file")
    fd, temporary = tempfile.mkstemp(prefix=".extension-token-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print("Extension connection configured. Use Connect to verify your account and billing.")


def _unit_quote(value: str) -> str:
    """Quote one systemd argument, without enabling specifier or environment expansion."""
    if any(char in value for char in "\r\n\x00"):
        raise ValueError("service paths must not contain line breaks or NUL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def service_unit(paths: Paths, *, env: Mapping[str, str] | None = None) -> str:
    """Render the exact interpreter and state/config paths used by this invocation."""
    env = os.environ if env is None else env
    # Preserve the venv path: resolving its Python symlink would select the base interpreter.
    executable = str(Path(sys.executable).absolute()).replace("$", "$$")
    environment = {
        "TASKSPINDLE_CONFIG": str(paths.config_file),
        "XDG_STATE_HOME": str(paths.state_dir.parent),
        "XDG_DATA_HOME": str(paths.data_dir.parent),
        "PYTHONUNBUFFERED": "1",
    }
    if env.get("TZ"):
        environment["TZ"] = env["TZ"]
    lines = [
        "[Unit]",
        "Description=TaskSpindle subscription collector",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={_unit_quote(executable)} -m taskspindle.cli subscriptions watch",
        *[f"Environment={_unit_quote(f'{key}={value}')}" for key, value in environment.items()],
        "Restart=on-failure",
        "RestartSec=5",
        "UMask=0077",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _print_status(payload: dict[str, Any]) -> None:
    running = "running" if payload.get("collector_running") else "not running"
    print(f"Subscription collector: {running}")
    scheduling = "enabled" if payload.get("scheduled_refresh_enabled") else "disabled"
    print(f"Scheduled refresh: {scheduling}")
    for row in payload.get("subscriptions", []):
        label = row.get("label") or row.get("provider") or "Unknown provider"
        state = row.get("status") or "unknown"
        if row.get("end_passed_unverified"):
            state = "recorded access end passed; verification needed"
        elif row.get("upcoming_end_warning") == "within_1_day":
            state += "; access ends within one day"
        elif row.get("upcoming_end_warning") == "within_7_days":
            state += "; access ends within seven days"
        error = row.get("error")
        if error:
            state += f"; {error.get('message', 'verification failed')}"
        boundary = row.get("access_ends_at") or row.get("renews_at") or "date unavailable"
        verified = row.get("last_success_at") or "never"
        plan = row.get("plan") or "plan unavailable"
        print(f"{label}: {plan}; {state}; {boundary}; verified {verified}")


def run(args: argparse.Namespace, paths: Paths) -> int:
    command = args.subscription_command
    try:
        if command == "setup-extension":
            _setup_extension(paths)
            return 0
        if command == "service-unit":
            print(service_unit(paths), end="")
            return 0
        if command == "setup-browser":
            from .runtime import setup_browser

            result = setup_browser(paths)
            print(json.dumps(result, indent=2))
            return 0 if result.get("ok", False) else 1

        from .service import SubscriptionService

        service = SubscriptionService(paths)
        if command == "status":
            payload = service.status()
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                _print_status(payload)
        elif command == "watch":
            if args.once:
                result = service.run_once()
                if result is not None:
                    print(json.dumps(result))
                    return 0 if result.get("ok", False) else 1
            else:
                service.watch()
        elif command == "connect":
            print(json.dumps(service.request(args.provider, "connect")))
        elif command == "refresh":
            providers = [args.provider] if args.provider else [
                row["provider"] for row in service.status()["subscriptions"] if row["connected"]
            ]
            print(json.dumps({"jobs": [service.request(provider, "refresh") for provider in providers]}))
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error, EOFError, getpass.GetPassWarning):
        # Browser sessions and network exceptions can contain tokens or payment details.
        print("taskspindle subscriptions: operation failed; inspect subscription status", file=sys.stderr)
        return 1
