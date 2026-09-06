"""The ``taskspindle`` command line.

The MCP server is what a Codex
session talks to, and ``worker`` and ``accept`` exist so that the two detached entry points the
systemd units run can also be run by hand when something has gone wrong. ``setup``, ``doctor``,
``auth``, ``discover``, ``usage`` and ``web`` are the ones a person actually types.

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
        choices=("provider", "day", "provider_day", "model", "mode", "repository_id"),
        help="how to roll the token counts up (default: provider)",
    )
    usage.add_argument("--json", action="store_true", help="print the report as JSON")

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
    discover.add_argument("--json", action="store_true", help="print the findings as JSON")

    web = sub.add_parser("web", help="serve the read-only dashboard on localhost")
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
    if args.command == "mcp":
        return _mcp()
    if args.command == "worker":
        return _worker(args.task)
    if args.command == "usage":
        return _usage(args.since, args.provider, args.group_by, as_json=args.json)
    if args.command == "discover":
        return _discover(args.registry, refresh=args.refresh, show_all=args.all, as_json=args.json)
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
    from .store import Store, StoreError

    database = paths.state_dir / "taskspindle.sqlite3"
    if not database.exists():
        return []
    try:
        with Store.open(database) as store:
            return store.list_provider_status()
    except (StoreError, OSError):
        return []


def _profiles(paths: Paths) -> dict[str, Any]:
    from . import providers
    from .config import load_config

    settings = load_config(paths.config_file)
    return providers.load_profiles(
        settings,
        runtime_dir=paths.runtime_dir,
        home=Path(os.environ.get("HOME", "")),
        state_dir=paths.state_dir,
        data_dir=paths.data_dir,
    )


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
    columns = [*keys, "turns", "input", "output", "cache_read", "cache_write", "est_usd"]
    rows = [
        [
            *(str(row.get(key) or "-") for key in keys),
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


# -- discover ------------------------------------------------------------------------


def _discover(registry: str | None, *, refresh: bool, show_all: bool, as_json: bool) -> int:
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
                        }
                        for item in found
                    ],
                    "not_installed": [agent.id for agent in agents if agent.id not in found_ids],
                },
                indent=2,
            )
        )
        return 0
    print(f"registry: {source} ({origin}, {len(agents)} agents with a local launch form)")
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
    return 0


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
