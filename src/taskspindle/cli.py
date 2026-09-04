"""The ``taskspindle`` command line.

Five subcommands, and none of them is the interesting part: the MCP server is what a Codex
session talks to, and ``worker`` and ``accept`` exist so that the two detached entry points the
systemd units run can also be run by hand when something has gone wrong. ``setup`` and ``doctor``
are the ones a person actually types.

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
    if args.command == "setup":
        return _setup(args.runtime_dir, args.npm)
    if args.command == "doctor":
        return _doctor(live_probes=not args.no_live, as_json=args.json)
    if args.command == "mcp":
        return _mcp()
    if args.command == "worker":
        return _worker(args.task)
    return _accept(args.task)


# -- setup ---------------------------------------------------------------------------


def _setup(runtime_dir: str | None, npm: str) -> int:
    from .setup import SetupError, install_runtime

    paths = resolve_paths(runtime_dir)
    try:
        report = install_runtime(paths, npm=npm)
    except SetupError as exc:
        print(f"taskspindle setup: {exc}", file=sys.stderr)
        return 1
    print(f"adapter {taskspindle.ADAPTER_PACKAGE} {report['adapter_version']}")
    print(f"runtime {report['runtime_dir']}")
    print(f"node    {report['node']}")
    print(f"config  {report['config_file']} ({'written' if report['created_config'] else 'kept'})")
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
        )
    except (ConfigError, providers.ProfileError, OSError) as exc:
        return _report({"ok": False, "checks": [_check("config", str(exc))]}, as_json=as_json)

    report = doctor_module.run_doctor(
        profiles=profiles,
        paths=paths,
        parent_env=os.environ,
        live_probes=live_probes,
    )
    return _report(report, as_json=as_json)


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
