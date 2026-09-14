"""The ``taskspindle`` command line.

The MCP server is what a Codex
session talks to, and ``worker`` and ``accept`` exist so that the two detached entry points the
systemd units run can also be run by hand when something has gone wrong. ``setup``, ``doctor``,
``auth``, ``discover``, ``usage``, ``providers`` and ``web`` are the ones a person types.

Nothing here decides anything. Each subcommand resolves the paths, hands off to the module that
owns the work, and turns whatever comes back into an exit code and a line of output. Failures are
printed as a reason, never as a traceback: a stack trace on a terminal tells the operator less
than the sentence that caused it, and can carry paths they did not ask to see.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import taskspindle

from .config import ConfigError, Paths
from .config import paths as default_paths

__all__ = ["build_parser", "main", "resolve_paths"]

_OK = "[ok]"
_FAIL = "[FAIL]"
_WARN = "[warn]"


def resolve_paths(runtime_dir: str | None = None) -> Paths:
    """The locations this invocation works in: XDG, with ``TASKSPINDLE_CONFIG`` on top.

    The same override the MCP server and the units apply, so a command run by hand looks at the
    configuration the server would have read.
    """
    resolved = default_paths()
    raw_config = os.environ.get("TASKSPINDLE_CONFIG")
    return Paths(
        config_file=Path(raw_config) if raw_config else resolved.config_file,
        state_dir=resolved.state_dir,
        data_dir=resolved.data_dir,
        runtime_dir=Path(runtime_dir) if runtime_dir else resolved.runtime_dir,
    )


def build_parser() -> argparse.ArgumentParser:
    """The full argument parser, built separately so the tests can inspect it."""
    parser = argparse.ArgumentParser(
        prog="taskspindle",
        description="Delegate bounded work to OAuth-backed coding agents in isolated worktrees.",
    )
    parser.add_argument(
        "--version", action="store_true", help="print the TaskSpindle version and exit"
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    setup = sub.add_parser("setup", help="install the pinned adapter runtime and lay out the dirs")
    setup.add_argument("--runtime-dir", help="install into this directory instead of the default")
    setup.add_argument("--npm", default="npm", help="the npm executable to use (default: npm)")
    setup.add_argument("--provider", choices=("claude", "agy"), default="claude",
                       help="adapter to install (default: claude)")

    auth = sub.add_parser("auth", help="check the existing native provider login")
    auth.add_argument("provider", choices=("agy",))
    auth.add_argument("--runtime-dir", help="use an explicitly installed adapter runtime directory")

    doctor = sub.add_parser("doctor", help="check everything TaskSpindle needs before it runs")
    doctor.add_argument(
        "--no-live", action="store_true", help="skip the checks that start a process"
    )
    doctor.add_argument("--json", action="store_true", help="print the report as JSON")

    provider_status = sub.add_parser(
        "providers", help="show cached worker availability without starting work",
    )
    provider_status.add_argument("--provider", help="only this configured provider ID")
    provider_status.add_argument(
        "--model", help="model for the one controlled retry (requires --retry-next)"
    )
    provider_status.add_argument("--json", action="store_true", help="print the report as JSON")
    provider_action = provider_status.add_mutually_exclusive_group()
    provider_action.add_argument(
        "--check", action="store_true",
        help="also check supported native cached logins/catalogs without browser login or inference",
    )
    provider_action.add_argument(
        "--retry-next", action="store_true",
        help="arm one controlled retry using the current cached evidence revision",
    )
    provider_action.add_argument(
        "--revoke-retry", metavar="PERMIT_ID",
        help="revoke an armed controlled-retry permit",
    )

    sub.add_parser("mcp", help="run the stdio MCP server (what Codex launches)")

    worker = sub.add_parser("worker", help="run one turn of a task by hand")
    worker.add_argument("--task", required=True, help="the task id to run one turn of")

    accept = sub.add_parser("accept", help="run a pending acceptance by hand")
    accept.add_argument("--task", required=True, help="the task id to accept")

    usage = sub.add_parser("usage", help="tokens, estimated cost, outcomes and timings")
    usage.add_argument("--since", help="ISO-8601, or shorthand like 7d, 24h, 30m")
    usage.add_argument("--provider", help="only this provider")
    usage.add_argument(
        "--group-by",
        default="provider",
        choices=("provider", "day", "provider_day", "model", "mode", "role", "repository_id"),
        help="how to roll the token counts up (default: provider)",
    )
    usage.add_argument("--json", action="store_true", help="print the report as JSON")

    policy = sub.add_parser("policy", help="inspect and edit the dispatch policy")
    policy_sub = policy.add_subparsers(dest="policy_command", metavar="POLICY_COMMAND")

    policy_show = policy_sub.add_parser("show", help="show the current dispatch policy")
    policy_show.add_argument(
        "--status", action="store_true", help="also show observed usage against targets and budgets"
    )
    policy_show.add_argument("--json", action="store_true", help="print the report as JSON")

    policy_sub.add_parser("export", help="print the canonical policy document JSON to stdout")

    policy_import = policy_sub.add_parser(
        "import", help="replace the policy document from a JSON file or stdin"
    )
    policy_import.add_argument("file", help="a JSON file, or - to read stdin")
    policy_import.add_argument(
        "--if-revision", type=int, help="only write if this is still the current revision"
    )

    policy_set = policy_sub.add_parser(
        "set", help="set one dotted path in the policy document to a JSON value"
    )
    policy_set.add_argument("path", help="dotted path, e.g. providers.claude.target_share")
    policy_set.add_argument("value", help="a JSON value; a raw string when it does not parse as JSON")

    policy_reset = policy_sub.add_parser("reset", help="reset the policy document to its defaults")
    policy_reset.add_argument(
        "--if-revision", type=int, help="only write if this is still the current revision"
    )

    discover = sub.add_parser(
        "discover", help="list installed ACP agents from the community registry as config proposals"
    )
    discover.add_argument(
        "--registry", help="a registry URL or local file instead of the published one"
    )
    discover.add_argument("--refresh", action="store_true", help="ignore the cached registry")
    discover.add_argument(
        "--all", action="store_true", help="also list registry agents that are not installed"
    )
    discover.add_argument(
        "--probe", action="store_true", help="initialize installed agents without sending prompts"
    )
    discover.add_argument("--json", action="store_true", help="print the findings as JSON")

    web = sub.add_parser("web", help="serve the local task dashboard")
    web.add_argument("--host", default="127.0.0.1", help="address to bind (default: 127.0.0.1)")
    web.add_argument("--port", type=int, default=8765, help="port to bind (default: 8765)")
    web.add_argument("--open", action="store_true", help="open the page in a browser")

    rollback = sub.add_parser(
        "rollback-concurrency", help="after draining jobs and stopping MCP servers, reverse schema 4 only"
    )
    rollback.add_argument("--database", required=True, help="explicit existing SQLite database path")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and run one subcommand; the return value is the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"taskspindle {taskspindle.__version__}")
        return 0
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "rollback-concurrency":
        return _rollback_concurrency(args.database)
    if args.command == "setup":
        return _setup(args.runtime_dir, args.npm, args.provider)
    if args.command == "auth":
        return _auth_agy(args.runtime_dir)
    if args.command == "doctor":
        return _doctor(live_probes=not args.no_live, as_json=args.json)
    if args.command == "providers":
        if args.retry_next and not args.provider:
            parser.error("providers --retry-next requires --provider")
        if args.retry_next and args.model is not None and not args.model.strip():
            parser.error("providers --model must not be empty")
        if args.model and not args.retry_next:
            parser.error("providers --model requires --retry-next")
        if args.revoke_retry and args.provider:
            parser.error("providers --revoke-retry does not accept --provider")
        if args.retry_next or args.revoke_retry:
            return _provider_recovery(
                provider=args.provider,
                model=args.model,
                permit_id=args.revoke_retry,
                as_json=args.json,
            )
        return _providers_status(check=args.check, as_json=args.json, provider=args.provider)
    if args.command == "mcp":
        return _mcp()
    if args.command == "worker":
        return _worker(args.task)
    if args.command == "usage":
        return _usage(args.since, args.provider, args.group_by, as_json=args.json)
    if args.command == "policy":
        return _policy(parser, args)
    if args.command == "discover":
        return _discover(args.registry, refresh=args.refresh, show_all=args.all, as_json=args.json,
                         probe=args.probe)
    if args.command == "web":
        return _web(args.host, args.port, open_browser=args.open)
    return _accept(args.task)


def _rollback_concurrency(database: str) -> int:
    """Maintenance deliberately avoids Store.open, which would auto-upgrade the schema."""
    from .store import Store, StoreError

    path = Path(database)
    if not path.is_file():
        print("taskspindle rollback-concurrency: database must be an existing file", file=sys.stderr)
        return 1
    try:
        with Store(path) as store:
            store.rollback_concurrency_schema()
    except (StoreError, OSError) as exc:
        print(f"taskspindle rollback-concurrency: {exc}", file=sys.stderr)
        return 1
    print("Restored schema 3 lease layout; task history and grants preserved. "
          "Start only the previous runtime.")
    return 0


# -- setup ---------------------------------------------------------------------------


def _setup(runtime_dir: str | None, npm: str, provider: str = "claude") -> int:
    from .setup import SetupError, install_agy_runtime, install_runtime

    paths = resolve_paths(runtime_dir)
    try:
        report = install_agy_runtime(paths) if provider == "agy" else install_runtime(paths, npm=npm)
    except SetupError as exc:
        print(f"taskspindle setup: {exc}", file=sys.stderr)
        return 1
    package = report.get("adapter_package", taskspindle.ADAPTER_PACKAGE)
    print(f"adapter {package} {report['adapter_version']}")
    print(f"runtime {report['runtime_dir']}")
    if "node" in report:
        print(f"node    {report['node']}")
    if provider == "agy":
        print("check the existing CLI login with taskspindle auth agy")
    print(f"config  {report['config_file']} ({'written' if report['created_config'] else 'kept'})")
    return 0


def _auth_agy(runtime_dir: str | None = None) -> int:
    """Check native CLI login without opening the task store or starting browser OAuth."""
    from . import providers
    from .agy_cli_adapter import agy_oauth_evidence
    from .config import load_config

    paths = resolve_paths(runtime_dir)
    try:
        profiles = providers.load_profiles(
            load_config(paths.config_file), runtime_dir=paths.runtime_dir,
            home=Path(os.environ.get("HOME", "")), state_dir=paths.state_dir, data_dir=paths.data_dir,
        )
        profile = profiles["agy"]
        evidence = agy_oauth_evidence(profile, os.environ)
    except (ConfigError, providers.ProfileError, OSError) as exc:
        print(f"taskspindle auth agy: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("taskspindle auth agy: sign-in cancelled", file=sys.stderr)
        return 130
    print(f"Antigravity CLI cached login works; {evidence['model_count']} Gemini models advertised.")
    return 0


# -- doctor --------------------------------------------------------------------------


def _doctor(*, live_probes: bool, as_json: bool) -> int:
    from . import doctor as doctor_module
    from . import providers
    from .config import load_config

    paths = resolve_paths()
    try:
        settings = load_config(paths.config_file)
        profiles = providers.load_profiles(
            settings,
            runtime_dir=paths.runtime_dir,
            home=Path(os.environ.get("HOME", "")),
            state_dir=paths.state_dir,
            data_dir=paths.data_dir,
        )
    except (ConfigError, providers.ProfileError, OSError) as exc:
        return _report({"ok": False, "checks": [_check("config", str(exc))]}, as_json=as_json)

    report = doctor_module.run_doctor(
        profiles=profiles,
        paths=paths,
        parent_env=os.environ,
        live_probes=live_probes,
        provider_status=_provider_status(paths),
    )
    return _report(report, as_json=as_json)


def _provider_status(paths: Paths) -> list[dict[str, Any]]:
    """The provider rows, when a store exists; a machine that never ran a task has none."""
    import sqlite3

    from .web.db import ReadOnlyStore

    database = paths.state_dir / "taskspindle.sqlite3"
    if not database.exists():
        return []
    try:
        with ReadOnlyStore(database) as store:
            return store.list_provider_status()
    except (sqlite3.Error, OSError):
        return []


def _providers_status(*, check: bool, as_json: bool, provider: str | None = None) -> int:
    """Project the same availability as MCP/web; native checks never clear task evidence."""
    import sqlite3
    from datetime import UTC, datetime

    from .providers import ProfileError
    from .service import model_availability, provider_availability
    from .web.db import ReadOnlyStore

    paths = resolve_paths()
    try:
        profiles = _profiles(paths)
        if provider is not None:
            if provider not in profiles:
                raise ProfileError("PROFILE_UNKNOWN", "Unknown provider")
            profiles = {provider: profiles[provider]}
        rows = []
        from .access_checks import cached_native_check, refresh_native_check
        from .store import Store
        factory = Store.open if check else ReadOnlyStore
        with factory(paths.state_dir / "taskspindle.sqlite3") as store:
            for profile in sorted(profiles.values(), key=lambda item: item.id):
                native = (refresh_native_check(store, profile, os.environ) if check
                          else cached_native_check(store, profile))
                checked_at = datetime.now(UTC)
                row = {
                    "native_check": native,
                    "id": profile.id, "family": profile.family,
                    "auth": profile.auth, "model": profile.model,
                    "availability": provider_availability(
                        store, profile, now=checked_at, model=profile.model,
                    ),
                    "model_availability": model_availability(store, profile, now=checked_at),
                }
                rows.append(row)
    except (ConfigError, ProfileError, OSError, sqlite3.Error):
        if as_json:
            print(json.dumps({"error": "PROVIDER_STATUS_UNAVAILABLE"}))
        else:
            print(
                "taskspindle providers: configuration or provider status could not be read", file=sys.stderr,
            )
        return 1
    if as_json:
        print(json.dumps({"providers": rows}, indent=2, sort_keys=True))
    else:
        for row in rows:
            availability = row["availability"]
            print(f"{row['id']}: {availability['state']} — {availability.get('next_action', 'verify')}")
            print(f"  Last successful use: {availability.get('last_success_at') or 'not observed'}")
            if availability.get("reset_at"):
                print(f"  Reported quota reset: {availability['reset_at']}")
            _print_quota_context(availability, indent="  ")
            _print_provider_recovery(availability, indent="  ")
            for model in row["model_availability"]:
                if model["affected_model"] != availability.get("affected_model"):
                    print(f"  Model {model['affected_model']}: {model['state']} — {model['next_action']}")
                    _print_quota_context(model, indent="    ")
                    _print_provider_recovery(model, indent="    ")
            if check:
                native = row["native_check"]
                print(f"  Native check: {native['state']} — {native['detail']}")
    return 0


def _print_quota_context(availability: dict[str, Any], *, indent: str) -> None:
    """Print quota/auth/retry evidence without implying a provider identity."""
    for restriction in availability.get("quota_restrictions") or []:
        model_family = restriction.get("model_family") or restriction.get("model") or "-"
        reset = restriction.get("reset") or restriction.get("reset_at") or "-"
        observed = restriction.get("observed") or restriction.get("observed_at") or "-"
        print(
            f"{indent}Quota restriction: scope={restriction.get('scope') or '-'} "
            f"model_family={model_family} window={restriction.get('window') or '-'} "
            f"reset={reset} source={restriction.get('source') or '-'} observed={observed}"
        )
    context = availability.get("auth_context") or {}
    if context.get("changed"):
        print(f"{indent}Authentication context changed (metadata only; not an account identity).")
        if context.get("fingerprint"):
            print(f"{indent}Authentication context fingerprint: {context['fingerprint']}")
    retry = availability.get("quota_retry") or {}
    if retry.get("state") == "pending":
        print(f"{indent}Quota retry pending: {retry.get('task_id') or '-'}")


def _print_provider_recovery(availability: dict[str, Any], *, indent: str) -> None:
    """Print every decision-bearing recovery field from a cached availability projection."""
    automatic = availability.get("automatic_recovery") or {}
    if automatic.get("policy") == "hybrid":
        print(f"{indent}Hybrid recovery: {automatic.get('state') or 'unknown'}")
        print(f"{indent}Recovery retries remaining: {automatic.get('attempts_remaining', '-')}")
        for key, label in (
            ("next_attempt_at", "Next recovery attempt"),
            ("hold_reason", "Recovery hold reason"),
            ("active_task_id", "Recovery task"),
            ("episode_id", "Recovery episode"),
            ("evidence_revision", "Recovery evidence revision"),
        ):
            if automatic.get(key) is not None:
                print(f"{indent}{label}: {automatic[key]}")
        return
    recovery = availability.get("recovery") or {}

    def value(name: str) -> Any:
        return recovery.get(name) or "-"

    print(f"{indent}Evidence revision: {availability.get('evidence_revision') or '-'}")
    print(f"{indent}Recovery: {value('state')}")
    print(f"{indent}Recovery scope: {value('provider')} / {recovery.get('model') or 'provider default'}")
    print(f"{indent}Shared availability key: {value('status_key')}")
    print(f"{indent}Recovery permit: {value('permit_id')}")
    print(f"{indent}Recovery expires: {value('expires_at')}")
    print(f"{indent}Recovery task: {value('task_id')}")
    print(f"{indent}Recovery outcome: {value('outcome')}")
    if recovery.get("outcome_code"):
        print(f"{indent}Recovery outcome code: {recovery['outcome_code']}")
    print(f"{indent}Recovery next action: {value('next_action')}")


def _provider_recovery(
    *, provider: str | None, model: str | None, permit_id: str | None, as_json: bool,
) -> int:
    """Arm or revoke one permit without checking providers or starting inference."""
    import sqlite3
    from datetime import UTC, datetime

    from . import provider_recovery
    from .providers import ProfileError
    from .service import TaskSpindleError, provider_availability
    from .store import Store

    paths = resolve_paths()
    try:
        with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
            now = datetime.now(UTC)
            if permit_id is not None:
                result = provider_recovery.revoke(store, permit_id, now=now)
            else:
                profiles = _profiles(paths)
                if provider not in profiles:
                    raise ProfileError("PROFILE_UNKNOWN", "Unknown provider")
                profile = profiles[provider]
                selected_model = model or profile.model
                availability = provider_availability(
                    store,
                    profile,
                    now=now,
                    model=selected_model,
                    parent_env=os.environ,
                )
                revision = availability.get("evidence_revision")
                if not isinstance(revision, str) or not revision:
                    raise TaskSpindleError(
                        "RECOVERY_EVIDENCE_UNAVAILABLE",
                        "current cached provider evidence has no recovery revision",
                    )
                result = provider_recovery.arm(
                    store,
                    profile,
                    evidence_revision=revision,
                    now=now,
                    model=selected_model,
                    parent_env=os.environ,
                )
    except TaskSpindleError as exc:
        if as_json:
            print(json.dumps({"error": exc.to_error_body().model_dump(mode="json")}, sort_keys=True))
        else:
            print(f"taskspindle providers: {exc.code}: {exc}", file=sys.stderr)
        return 1
    except (ConfigError, ProfileError, OSError, sqlite3.Error):
        if as_json:
            print(json.dumps({"error": "PROVIDER_RECOVERY_UNAVAILABLE"}))
        else:
            print("taskspindle providers: controlled recovery could not be updated", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        action = {
            "armed": "Armed", "claimed": "Recovery attempt pending for",
            "revoked": "Revoked", "expired": "Expired",
        }.get(result.get("state"), "Recorded")
        print(f"{action} controlled retry permit {result.get('permit_id')}.")
    return 0


def _profiles(paths: Paths) -> dict[str, Any]:
    from . import auth_context, providers
    from .config import load_config

    settings = load_config(paths.config_file)
    loaded = providers.load_profiles(
        settings,
        runtime_dir=paths.runtime_dir,
        home=Path(os.environ.get("HOME", "")),
        state_dir=paths.state_dir,
        data_dir=paths.data_dir,
    )
    auth_context.validate_contexts(loaded, os.environ)
    return loaded


def _check(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": False, "detail": detail, "advisory": False}


def _mark(check: dict[str, Any]) -> str:
    """``[ok]``, ``[warn]`` for an advisory failure, ``[FAIL]`` for one that counts."""
    if check["ok"]:
        return _OK
    return _WARN if check.get("advisory") else _FAIL


def _report(report: dict[str, Any], *, as_json: bool) -> int:
    """Print a doctor report and return the exit code its non-advisory checks imply."""
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for check in report.get("checks", []):
            print(f"{_mark(check)} {check['name']}: {check['detail']}")
    return 0 if report.get("ok") else 1


# -- usage ---------------------------------------------------------------------------


def _usage(since: str | None, provider: str | None, group_by: str, *, as_json: bool) -> int:
    from . import providers, usage
    from .store import Store

    paths = resolve_paths()
    try:
        profiles = _profiles(paths)
        with Store.open(paths.state_dir / "taskspindle.sqlite3") as store:
            report = usage.report(
                store,
                since=usage.parse_since(since),
                provider=provider,
                group_by=group_by,
                profiles=profiles,
            )
    except (ConfigError, providers.ProfileError, OSError, ValueError) as exc:
        print(f"taskspindle usage: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    _print_usage(report)
    return 0


def _print_usage(report: dict[str, Any]) -> None:
    """A few fixed-width tables; the JSON form carries everything."""
    keys = [
        key for key in ("provider", "day", "model", "mode", "repository_id")
        if any(key in row for row in report["usage"])
    ]
    columns = [*("repository" if key == "repository_id" else key for key in keys),
               "turns", "input", "output", "cache_read", "cache_write", "est_usd"]
    rows = [
        [
            *(str(row.get("repository_path") or row.get(key) or "No repository")
              if key == "repository_id" else str(row.get(key) or "-") for key in keys),
            str(row["turns"]),
            str(row["input_tokens"]),
            str(row["output_tokens"]),
            str(row["cache_read_tokens"]),
            str(row["cache_write_tokens"]),
            f"{row['cost_estimate_usd']:.4f}" if row["priced_turns"] else "-",
        ]
        for row in report["usage"]
    ]
    print("usage" + (f" since {report['since']}" if report["since"] else ""))
    _table(columns, rows)
    print()
    print("outcomes")
    _table(
        ["provider", "mode", "state", "count"],
        [[r["provider"], r["mode"], r["state"], str(r["count"])] for r in report["outcomes"]],
    )
    print()
    turns = report["turns"]
    print(
        f"turns: {turns['count']} (mean {turns['mean_ms']} ms, p50 {turns['p50_ms']} ms); "
        f"checks: {report['checks']['passed']}/{report['checks']['count']} passed"
    )
    for entry in report["windows"]:
        marks = ", ".join(
            f"{w['window']} {w['status'] or '?'} {w['used_percent'] or '?'}%" for w in entry["windows"]
        )
        print(f"{entry['provider']}: {entry['state']}" + (f"; windows: {marks}" if marks else ""))
    print(report["cost_note"])


def _table(columns: list[str], rows: list[list[str]]) -> None:
    widths = [
        max([len(column), *(len(row[index]) for row in rows)]) for index, column in enumerate(columns)
    ]
    print("  ".join(column.ljust(widths[index]) for index, column in enumerate(columns)))
    for row in rows:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))
    if not rows:
        print("(nothing recorded)")


# -- dispatch policy -------------------------------------------------------------------


def _policy(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    if args.policy_command is None:
        parser.error("policy requires a subcommand (show, export, import, set, reset)")
    if args.policy_command == "show":
        return _policy_show(status=args.status, as_json=args.json)
    if args.policy_command == "export":
        return _policy_export()
    if args.policy_command == "import":
        return _policy_import(args.file, args.if_revision)
    if args.policy_command == "set":
        return _policy_set(args.path, args.value)
    return _policy_reset(args.if_revision)


def _policy_store_and_profiles() -> tuple[Any, dict[str, Any]]:
    """The loaded profiles and an open store at the resolved paths.

    Raises the same exceptions ``_usage`` does; the caller turns them into a one-line error.
    """
    from .store import Store

    paths = resolve_paths()
    profiles = _profiles(paths)
    store = Store.open(paths.state_dir / "taskspindle.sqlite3")
    return store, profiles


def _print_policy_errors(errors: list[dict[str, Any]]) -> None:
    for error in errors:
        loc = ".".join(str(part) for part in error["loc"])
        print(f"{loc}: {error['msg']}", file=sys.stderr)


def _policy_show(*, status: bool, as_json: bool) -> int:
    from datetime import UTC, datetime

    from . import policy as policy_module
    from . import providers

    try:
        store, profiles = _policy_store_and_profiles()
        with store:
            loaded = policy_module.load(store, profiles)
            result: dict[str, Any] = {
                "policy": loaded.policy.model_dump(mode="json"),
                **loaded.describe(),
            }
            if status:
                result["status"] = policy_module.status(store, loaded, profiles, datetime.now(UTC))
    except (ConfigError, providers.ProfileError, OSError, ValueError) as exc:
        print(f"taskspindle policy: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    _print_policy(result)
    return 0


def _print_policy(result: dict[str, Any]) -> None:
    """A short human summary; ``--json`` carries the whole document and status."""
    print(
        f"policy revision {result['revision']} ({result['source']}), "
        f"updated {result['updated_at'] or 'never'} by {result['updated_by'] or '-'}"
    )
    if result.get("document_error"):
        print(f"stored document did not parse, showing defaults: {result['document_error']}")
    document = result["policy"]
    status = result.get("status")
    provider_status = (status or {}).get("providers", {})
    share_window = document.get("share_window", "week")
    rows = []
    for name, spec in sorted(document.get("providers", {}).items()):
        info = provider_status.get(name, {})
        share = info.get("observed", {}).get(share_window, {}).get("share_turns")
        budgets = ", ".join(
            f"{window}:{'enforced' if budget.get('enforce') else 'advisory'}"
            for window, budget in (spec.get("budgets") or {}).items()
        )
        default_state = "active" if spec.get("enabled") else "paused"
        rows.append([
            name,
            str(info.get("state", "-")) if status is not None else default_state,
            str(spec.get("target_share")) if spec.get("target_share") is not None else "-",
            f"{share:.2f}" if isinstance(share, int | float) else "-",
            budgets or "-",
        ])
    print()
    print("providers")
    _table(["provider", "state", "target_share", f"observed_share ({share_window})", "budgets"], rows)

    role_rows = []
    for role, spec in sorted(document.get("roles", {}).items()):
        selections = ", ".join(
            f"{provider}:{sel.get('model') or '-'}/{sel.get('effort') or '-'}"
            for provider, sel in (spec.get("selections") or {}).items()
        )
        role_rows.append([
            role,
            ",".join(spec.get("provider_preference") or []) or "-",
            selections or "-",
        ])
    print()
    print("roles")
    _table(["role", "provider_preference", "selections"], role_rows)


def _policy_export() -> int:
    from . import policy as policy_module
    from . import providers

    try:
        store, profiles = _policy_store_and_profiles()
        with store:
            loaded = policy_module.load(store, profiles)
    except (ConfigError, providers.ProfileError, OSError, ValueError) as exc:
        print(f"taskspindle policy: {exc}", file=sys.stderr)
        return 1
    print(policy_module.canonical_json(loaded.policy))
    return 0


def _policy_import(file: str, if_revision: int | None) -> int:
    from . import policy as policy_module
    from . import providers
    from .store import PolicyRevisionConflict

    try:
        raw = sys.stdin.read() if file == "-" else Path(file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"taskspindle policy: {exc}", file=sys.stderr)
        return 1
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"taskspindle policy: {exc}", file=sys.stderr)
        return 1
    try:
        store, profiles = _policy_store_and_profiles()
        with store:
            try:
                parsed = policy_module.parse(document)
            except policy_module.PolicyError as exc:
                _print_policy_errors(exc.errors)
                return 1
            errors = policy_module.validate(parsed, profiles)
            if errors:
                _print_policy_errors(errors)
                return 1
            policy_module.save(
                store, parsed, updated_by="cli", if_revision=if_revision, reason="import"
            )
    except PolicyRevisionConflict as exc:
        print(f"taskspindle policy: revision conflict, current revision is {exc.actual}", file=sys.stderr)
        return 3
    except (ConfigError, providers.ProfileError, OSError, ValueError) as exc:
        print(f"taskspindle policy: {exc}", file=sys.stderr)
        return 1
    return 0


def _policy_set(path: str, raw_value: str) -> int:
    from . import policy as policy_module
    from . import providers
    from .store import PolicyRevisionConflict

    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        value = raw_value
    keys = path.split(".")
    try:
        store, profiles = _policy_store_and_profiles()
        with store:
            loaded = policy_module.load(store, profiles)
            document = loaded.policy.model_dump(mode="json")
            target: Any = document
            for key in keys[:-1]:
                if not isinstance(target, dict) or key not in target:
                    print(f"taskspindle policy: no such path: {path}", file=sys.stderr)
                    return 1
                target = target[key]
            if not isinstance(target, dict):
                print(f"taskspindle policy: no such path: {path}", file=sys.stderr)
                return 1
            target[keys[-1]] = value
            try:
                parsed = policy_module.parse(document)
            except policy_module.PolicyError as exc:
                _print_policy_errors(exc.errors)
                return 1
            errors = policy_module.validate(parsed, profiles)
            if errors:
                _print_policy_errors(errors)
                return 1
            policy_module.save(store, parsed, updated_by="cli", if_revision=None, reason="cli set")
    except PolicyRevisionConflict as exc:
        print(f"taskspindle policy: revision conflict, current revision is {exc.actual}", file=sys.stderr)
        return 3
    except (ConfigError, providers.ProfileError, OSError, ValueError) as exc:
        print(f"taskspindle policy: {exc}", file=sys.stderr)
        return 1
    return 0


def _policy_reset(if_revision: int | None) -> int:
    from . import policy as policy_module
    from . import providers
    from .store import PolicyRevisionConflict

    try:
        store, profiles = _policy_store_and_profiles()
        with store:
            defaults = policy_module.defaults(profiles)
            policy_module.save(
                store, defaults, updated_by="cli", if_revision=if_revision, reason="reset"
            )
    except PolicyRevisionConflict as exc:
        print(f"taskspindle policy: revision conflict, current revision is {exc.actual}", file=sys.stderr)
        return 3
    except (ConfigError, providers.ProfileError, OSError, ValueError) as exc:
        print(f"taskspindle policy: {exc}", file=sys.stderr)
        return 1
    return 0


# -- discover ------------------------------------------------------------------------


def _discover(
    registry: str | None, *, refresh: bool, show_all: bool, as_json: bool, probe: bool = False,
) -> int:
    from . import discover as discover_module

    paths = resolve_paths()
    source = registry or os.environ.get("TASKSPINDLE_ACP_REGISTRY") or discover_module.DEFAULT_REGISTRY_URL
    try:
        agents, origin = discover_module.fetch_registry(
            source=source, cache_path=paths.data_dir / "acp-registry.json", refresh=refresh
        )
    except discover_module.RegistryError as exc:
        print(f"taskspindle discover: {exc}", file=sys.stderr)
        return 1
    found = discover_module.detect(agents)
    found_ids = {item.agent.id for item in found}
    probes: dict[str, dict[str, Any]] = {}
    if probe:
        import asyncio

        probes = asyncio.run(discover_module.probe(found))
    exit_code = int(any(not result["ok"] for result in probes.values()))
    if as_json:
        print(
            json.dumps(
                {
                    "registry": source,
                    "origin": origin,
                    "installed": [
                        {
                            "id": item.agent.id,
                            "name": item.agent.name,
                            "path": item.path,
                            "command": list(item.command),
                            "first_class": discover_module.first_class_match(item.agent),
                            "profile_id": discover_module.profile_id(item.agent),
                            **({"probe": probes[item.agent.id]} if probe else {}),
                        }
                        for item in found
                    ],
                    "not_installed": [agent.id for agent in agents if agent.id not in found_ids],
                },
                indent=2,
            )
        )
        return exit_code
    print(f"registry: {source} ({origin}, {len(agents)} agents with a local launch form)")
    for agent_id, result in probes.items():
        if result["ok"]:
            print(f"{agent_id}: ACP initialize passed ({json.dumps(result['agent_info'])})")
        else:
            print(f"{agent_id}: ACP initialize failed: {result['error']['message']}")
    proposals = [item for item in found if discover_module.first_class_match(item.agent) is None]
    for item in found:
        first = discover_module.first_class_match(item.agent)
        if first is not None:
            print(f"{item.agent.id}: {item.path} -- TaskSpindle's first-class {first!r} provider")
    if not proposals:
        print("no other registry agent is installed on this machine")
    else:
        print(f"{len(proposals)} installed; proposed blocks for {paths.config_file} (nothing written):")
        for item in proposals:
            print()
            print(discover_module.proposal(item), end="")
    if show_all:
        missing = [agent.id for agent in agents if agent.id not in found_ids]
        print()
        print(f"not installed: {', '.join(missing) or 'none'}")
    return exit_code


# -- web -----------------------------------------------------------------------------


def _web(host: str, port: int, *, open_browser: bool) -> int:
    from . import providers
    from .web import serve

    paths = resolve_paths()
    try:
        profiles = _profiles(paths)
    except (ConfigError, providers.ProfileError, OSError) as exc:
        print(f"taskspindle web: {exc}", file=sys.stderr)
        return 1
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"taskspindle web: binding {host}; the dashboard has no authentication",
            file=sys.stderr,
        )
    return serve(paths, profiles, host=host, port=port, open_browser=open_browser)


# -- the three entry points ----------------------------------------------------------


def _mcp() -> int:
    from . import server

    server.main()
    return 0


def _worker(task_id: str) -> int:
    from . import runner

    return runner.main(["--task", task_id])


def _accept(task_id: str) -> int:
    from . import accept

    return accept.main(["--task", task_id])


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
