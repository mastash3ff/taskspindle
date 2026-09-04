"""The Starlette application behind ``taskspindle web``.

Every route is read-only: the database is opened ``mode=ro`` (see :mod:`.db`), the diff and log
files are read but never written, and every route is GET-only. There is no session, no cookie and
no form -- the dashboard is a static page plus a handful of JSON endpoints it polls.
"""

from __future__ import annotations

import functools
import importlib.resources
import inspect
import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import taskspindle

from .. import limits, usage
from ..config import Paths
from ..doctor import run_doctor_async
from ..providers import Profile
from ..service import provider_availability, task_view
from .db import ReadOnlyStore

__all__ = ["build_app"]

_DOCTOR_CACHE_SECONDS = 60
_WORKER_LOG_TAIL_LINES = 200

Handler = Callable[[Request], Response | Awaitable[Response]]


def _guard(handler: Handler) -> Handler:
    """Turn a stray sqlite/OS error into a 500 JSON body instead of a traceback."""
    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def wrapped_async(request: Request) -> Response:
            try:
                return await handler(request)
            except (sqlite3.Error, OSError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=500)

        return wrapped_async

    @functools.wraps(handler)
    def wrapped(request: Request) -> Response:
        try:
            return handler(request)  # type: ignore[return-value]
        except (sqlite3.Error, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    return wrapped


def _resolve_within(base: Path, candidate: str | Path | None) -> Path | None:
    """The resolved path of ``candidate``, or None if it is missing or escapes ``base``."""
    if not candidate:
        return None
    try:
        resolved = Path(candidate).resolve()
        base_resolved = base.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(base_resolved):
        return None
    return resolved


def build_app(
    paths: Paths,
    profiles: dict[str, Profile],
    *,
    clock: Callable[[], datetime] | None = None,
) -> Starlette:
    """Build the dashboard app. ``clock`` lets tests fix "now" for provider/usage windows."""
    clock = clock or (lambda: datetime.now(UTC))
    db_path = paths.state_dir / "taskspindle.sqlite3"
    # The dashboard serves assets from an unpacked filesystem installation (including wheels).
    static_dir = importlib.resources.files("taskspindle.web") / "static"

    def _store() -> ReadOnlyStore:
        return ReadOnlyStore(db_path)

    async def _doctor(app_state: Any, *, live: bool) -> dict[str, Any]:
        cache = app_state.doctor_cache
        if not live:
            elapsed = time.monotonic() - cache["at"]
            if cache["result"] is not None and elapsed < _DOCTOR_CACHE_SECONDS:
                return cache["result"]
        with _store() as store:
            status_rows = store.list_provider_status()
        result = await run_doctor_async(
            profiles=profiles,
            paths=paths,
            parent_env=os.environ,
            live_probes=live,
            provider_status=status_rows,
            now=clock(),
        )
        if not live:
            cache["result"] = result
            cache["at"] = time.monotonic()
        return result

    # -- static page --------------------------------------------------------------

    @_guard
    def index(request: Request) -> Response:
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    # -- health ---------------------------------------------------------------------

    @_guard
    def health(request: Request) -> Response:
        with _store() as store:
            return JSONResponse(
                {
                    "schema_version": store.schema_version(),
                    "db_path": str(db_path),
                    "db_exists": store.exists,
                    "read_only": True,
                    "version": taskspindle.__version__,
                }
            )

    # -- tasks ------------------------------------------------------------------------

    @_guard
    def list_tasks_endpoint(request: Request) -> Response:
        params = request.query_params
        try:
            limit = int(params.get("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 1000))
        with _store() as store:
            records = store.list_tasks(
                provider=params.get("provider"),
                mode=params.get("mode"),
                state=params.get("state"),
                limit=limit,
                q=params.get("q"),
            )
        tasks = [task_view(record).model_dump(mode="json") for record in records]
        return JSONResponse({"tasks": tasks})

    def _build_turn(
        record: Any,
        turn: dict[str, Any],
        usage_by_turn: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        entry = dict(turn)
        entry["usage"] = usage_by_turn.get(turn["id"])
        entry["transcript"] = None
        transcript_dir = Path(record.transcript_path).parent if record.transcript_path else None
        if transcript_dir is not None:
            candidate = transcript_dir / f"turn-{turn['revision']}.json"
            resolved = _resolve_within(paths.state_dir, candidate)
            if resolved is not None and resolved.is_file():
                try:
                    entry["transcript"] = json.loads(resolved.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    entry["transcript"] = None
        return entry

    @_guard
    def task_detail_endpoint(request: Request) -> Response:
        task_id = request.path_params["task_id"]
        with _store() as store:
            record = store.get_task(task_id)
            if record is None:
                return JSONResponse({"error": "TASK_NOT_FOUND"}, status_code=404)

            events = store.list_events(task_id)
            usage_rows = store.list_turn_usage(task_id=task_id)
            usage_by_turn = {row["turn_id"]: row for row in usage_rows}
            turns = [
                _build_turn(record, turn, usage_by_turn) for turn in store.list_turns(task_id)
            ]
            checks = [check.model_dump(mode="json") for check in store.list_checks(task_id)]

            review: dict[str, Any] | None = None
            if record.mode.value == "review":
                review = store.get_review_for(task_id)
            elif record.candidate_sha:
                review = store.latest_review_for_subject(task_id, record.candidate_sha)

            repository = None
            if record.repository_id:
                repository = next(
                    (r for r in store.list_repositories() if r["id"] == record.repository_id),
                    None,
                )

            worker_log = None
            log_path = _resolve_within(
                paths.state_dir, paths.state_dir / "tasks" / task_id / "worker.log"
            )
            if log_path is not None and log_path.is_file():
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                worker_log = "\n".join(lines[-_WORKER_LOG_TAIL_LINES:])

        return JSONResponse(
            {
                "task": task_view(record).model_dump(mode="json"),
                "events": events,
                "turns": turns,
                "checks": checks,
                "review": review,
                "reviews_of": [],
                "repository": repository,
                "worker_log": worker_log,
            }
        )

    @_guard
    def task_diff_endpoint(request: Request) -> Response:
        task_id = request.path_params["task_id"]
        params = request.query_params
        with _store() as store:
            record = store.get_task(task_id)
            if record is None:
                return JSONResponse({"error": "TASK_NOT_FOUND"}, status_code=404)
            revision_raw = params.get("revision")
            try:
                revision = int(revision_raw) if revision_raw is not None else record.candidate_revision
            except ValueError:
                return JSONResponse({"error": "INVALID_REVISION"}, status_code=400)
            expected_sha = params.get("candidate_sha")
            if expected_sha is not None and (
                revision != record.candidate_revision or expected_sha != record.candidate_sha
            ):
                return JSONResponse({"error": "CANDIDATE_CHANGED"}, status_code=409)
            artifact = store.get_artifact(task_id, revision, "candidate_diff")
        if artifact is None:
            return JSONResponse({"error": "DIFF_NOT_FOUND"}, status_code=404)
        path = _resolve_within(paths.state_dir, artifact.get("path"))
        if path is None or not path.is_file():
            return JSONResponse({"error": "DIFF_NOT_FOUND"}, status_code=404)
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    # -- providers --------------------------------------------------------------------

    @_guard
    async def providers_endpoint(request: Request) -> Response:
        with _store() as store:
            providers_out = []
            for profile in sorted(profiles.values(), key=lambda item: item.id):
                providers_out.append(
                    {
                        "id": profile.id,
                        "first_class": profile.first_class,
                        "auth": profile.auth,
                        "modes": sorted(profile.modes),
                        "model": profile.model,
                        "gateway_host": profile.gateway_host,
                        "family": profile.family,
                        "command_name": os.path.basename(profile.command[0]) if profile.command else None,
                        "availability": provider_availability(store, profile, now=clock()),
                        "windows": store.latest_provider_windows(limits.status_key(profile)),
                    }
                )
            status = store.list_provider_status()
        doctor_result = await _doctor(request.app.state, live=False)
        return JSONResponse({"providers": providers_out, "status": status, "doctor": doctor_result})

    @_guard
    async def doctor_endpoint(request: Request) -> Response:
        live = request.query_params.get("live") in ("1", "true")
        result = await _doctor(request.app.state, live=live)
        return JSONResponse(result)

    # -- usage ----------------------------------------------------------------------

    @_guard
    def usage_endpoint(request: Request) -> Response:
        params = request.query_params
        group_by = params.get("group_by", "provider")
        try:
            since = usage.parse_since(params.get("since"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        with _store() as store:
            try:
                report = usage.report(
                    store,
                    since=since,
                    provider=params.get("provider"),
                    group_by=group_by,
                    profiles=profiles,
                    now=clock(),
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(report)

    routes = [
        Route("/", index, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=Path(str(static_dir))), name="static"),
        Route("/api/health", health, methods=["GET"]),
        Route("/api/tasks", list_tasks_endpoint, methods=["GET"]),
        Route("/api/tasks/{task_id}", task_detail_endpoint, methods=["GET"]),
        Route("/api/tasks/{task_id}/diff", task_diff_endpoint, methods=["GET"]),
        Route("/api/providers", providers_endpoint, methods=["GET"]),
        Route("/api/doctor", doctor_endpoint, methods=["GET"]),
        Route("/api/usage", usage_endpoint, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.doctor_cache = {"result": None, "at": 0.0}
    return app
