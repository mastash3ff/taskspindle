"""Bounded worker readiness probes and explicit post-interrupt reconciliation.

Doctor's worker probes use only initialize/auth metadata, without tasks, prompts, or the live
database. The controller then adds its own execution-state checks (the admission fence, stuck
launch records, orphan leases) from its records and a read-only view of the task database.
The separate post-interrupt callback settles live worker records without dispatching work.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import providers
from .config import Paths
from .doctor import Check, execution_state_checks
from .units import UnitError


def reconcile_interrupted_workers(backend: Any, paths: Paths, settings: Mapping[str, Any]) -> None:
    """Settle workers after admission closes and interruption confirms they stopped.

    Startup grace is unnecessary after confirmed interruption. An unavailable or uncertain
    backend still retains its task and lease under the ordinary recovery rules.
    """
    from . import recovery, units
    from .store import Store

    database = paths.state_dir / "taskspindle.sqlite3"
    if not database.exists():
        return
    with Store.open(database) as store:
        recovery.reconcile(
            store, backend, boot=units.boot_id(), now=datetime.now(UTC),
            stale_after_s=0, workers_only=True,
        )


def doctor_report(
    backend: Any, paths: Paths, settings: Mapping[str, Any], *, live: bool = True,
) -> dict[str, Any]:
    """Ask each profile's worker image, retaining the standard cached doctor report shape."""
    profiles = providers.load_profiles(
        settings, runtime_dir=paths.runtime_dir, home=Path(os.environ.get("HOME", "")),
        state_dir=paths.state_dir, data_dir=paths.data_dir,
    )
    checks: list[dict[str, Any]] = []
    for provider in sorted(profiles):
        argv = ["taskspindle", "doctor", "--json"]
        if not live:
            argv.append("--no-live")
        try:
            result = backend.run_probe(
                provider, argv,
                env={"TASKSPINDLE_WORKER_CONTAINER": "1", "TASKSPINDLE_PROBE_PROVIDER": provider},
                timeout=90,
            )
            report = json.loads(result["stdout"])
            rows = report["checks"]
            if not isinstance(rows, list) or not rows or result["exit_code"] not in (0, 1):
                raise ValueError("worker did not return a complete doctor report")
            for row in rows:
                if not isinstance(row, dict) or not {"name", "ok", "detail", "advisory"} <= row.keys():
                    raise ValueError("worker returned an invalid doctor check")
            checks.extend({**row, "name": f"{provider}:{row['name']}"} for row in rows)
        except Exception as exc:
            # Provider stderr can contain authentication details. Do not return it over RPC.
            # A UnitError carries a stable code and a fixed, already-sanitized message; any
            # other exception contributes only its class name.
            checks.append(Check(
                f"{provider}:worker_probe", False, f"Worker diagnostics failed ({_failure(exc)})",
            ).as_dict())
    collect = getattr(backend, "execution_diagnostics", None)
    if callable(collect):
        try:
            facts = collect()
        except Exception as exc:
            checks.append(Check(
                "execution_state", False, f"Execution state is unavailable ({_failure(exc)})",
            ).as_dict())
        else:
            checks.extend(check.as_dict() for check in execution_state_checks(facts, datetime.now(UTC)))
    return {"ok": all(row["ok"] for row in checks if not row["advisory"]), "checks": checks}


def _failure(exc: Exception) -> str:
    if isinstance(exc, UnitError):
        return f"{exc.code}: {exc}"
    return type(exc).__name__
