"""The Starlette application behind ``taskspindle web``.

Task, provider and usage data stays read-only: the task database is opened ``mode=ro`` (see
:mod:`.db`) and task artifacts are only read. Every route - read or write - is gated by
:class:`.security.SecurityMiddleware` on the HTTP ``Host`` header, which blocks DNS rebinding
without breaking a deliberate ``--host 0.0.0.0`` bind; see that class's docstring for the
reasoning. Mutating routes are additionally loopback-gated: ``/api/policy*`` may write the
``dispatch_policy`` and ``dispatch_policy_history`` tables through :mod:`.policy_store`,
whose SQLite authorizer refuses every other table and every schema change; ``/api/ai-policy`` invokes
the optional fixed-argv adapter from ``config.toml`` and never writes the database or that file.
"""

from __future__ import annotations

import asyncio
import functools
import importlib.resources
import inspect
import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import taskspindle

from .. import access_checks, limits, policy, usage
from ..config import Paths
from ..doctor import run_doctor_async
from ..providers import Profile
from ..service import provider_availability, task_view
from ..store import PolicyRevisionConflict
from . import ai_policy
from .ai_policy import AdapterFactory
from .db import ReadOnlyStore
from .policy_store import PolicyStore
from .security import (
    SecurityMiddleware,
    csrf_valid,
    no_store,
    read_json_object,
    same_origin,
    trusted_loopback,
)

__all__ = ["build_app"]

_DOCTOR_CACHE_SECONDS = 60
_WORKER_LOG_TAIL_LINES = 200
_MAX_POLICY_BODY_BYTES = 65536
_MAX_AI_POLICY_BODY_BYTES = 65536

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


_TASK_SUMMARY_CHARS = 160
_TASK_SUMMARY_SCAN_CHARS = 8192
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    re.IGNORECASE | re.DOTALL,
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?<![\w-])([\"']?(?:[\w-]+[_-])?"
    r"(?:api[_ -]?key|access[_ -]?key(?:[_ -]?id)?|access[_ -]?token|refresh[_ -]?token|"
    r"client[_ -]?secret|token|secret|password|passwd|pwd)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"\n]*(?:\"|$)|'[^'\n]*(?:'|$)|[^\s,;]+)",
    re.IGNORECASE | re.MULTILINE,
)
_AUTHORIZATION_VALUE = re.compile(r"\b(Bearer|Basic)[ \t]+[^\s,;\"'<>]+", re.IGNORECASE)


def _task_summary(prompt: str) -> str:
    """A bounded first meaningful line; redact common credential syntax before truncation."""
    text = _PRIVATE_KEY_BLOCK.sub("[private key redacted]", prompt[:_TASK_SUMMARY_SCAN_CHARS])
    text = _AUTHORIZATION_VALUE.sub(r"\1 [redacted]", text)
    text = _CREDENTIAL_ASSIGNMENT.sub(r"\1[redacted]", text)
    for line in text.splitlines():
        line = re.sub(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]*|[-*+][ \t]+|\d+[.)][ \t]+)", "", line)
        line = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", line).split()).strip()
        if not line or line == "[private key redacted]" or re.fullmatch(r"[`~#*_=-]+", line):
            continue
        if line.startswith(("```", "~~~")):
            continue
        return line if len(line) <= _TASK_SUMMARY_CHARS else line[:_TASK_SUMMARY_CHARS - 1].rstrip() + "…"
    return "Untitled task"


def _task_display(record: Any, repository: dict[str, Any] | None) -> dict[str, Any]:
    result = task_view(record).model_dump(mode="json")
    result["summary"] = _task_summary(record.prompt)
    result["repository"] = {
        "id": record.repository_id,
        "path": repository.get("display_path", repository.get("path")) if repository else None,
    }
    return result


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
    policy_store_factory: Callable[[], PolicyStore] | None = None,
    ai_policy_adapter_factory: AdapterFactory | None = None,
) -> Starlette:
    """Build the dashboard application; task tables stay read-only."""
    clock = clock or (lambda: datetime.now(UTC))
    db_path = paths.state_dir / "taskspindle.sqlite3"
    policy_store_factory = policy_store_factory or (lambda: PolicyStore(db_path))
    adapter_factory = ai_policy_adapter_factory or ai_policy.default_adapter_factory
    csrf_token = secrets.token_urlsafe(32)
    ai_policy_lock = asyncio.Lock()
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
            cache["checked_at"] = clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        return result

    # -- static page --------------------------------------------------------------

    @_guard
    def index(request: Request) -> Response:
        # The only inline script is the theme bootstrap in <head>; everything else loads from
        # /static. A per-response nonce lets the CSP below allow just that one inline script
        # instead of falling back to 'unsafe-inline', which would allow any inline script.
        nonce = secrets.token_urlsafe(16)
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        html = html.replace("<script>", f'<script nonce="{nonce}">', 1)
        return HTMLResponse(
            html,
            headers={
                "Content-Security-Policy": (
                    "default-src 'self'; "
                    f"script-src 'self' 'nonce-{nonce}'; "
                    "style-src 'self'; "
                    "img-src 'self' data:; "
                    "connect-src 'self'; "
                    "object-src 'none'; "
                    "base-uri 'none'; "
                    "frame-ancestors 'none'"
                )
            },
        )

    # -- health ---------------------------------------------------------------------

    @_guard
    def health(request: Request) -> Response:
        with _store() as store:
            schema_version = store.schema_version()
            db_exists = store.exists
        with policy_store_factory() as pstore:
            policy_writable = pstore.available
        return JSONResponse(
            {
                "schema_version": schema_version,
                "db_path": str(db_path),
                "db_exists": db_exists,
                "read_only": False,
                "task_database_read_only": True,
                "policy_writable": policy_writable,
                "policy_schema_ready": (schema_version or 0) >= 11,
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
            repositories = {row["id"]: row for row in store.list_repositories()}
        tasks = [_task_display(record, repositories.get(record.repository_id)) for record in records]
        return JSONResponse({"tasks": tasks})

    @_guard
    def overview_endpoint(request: Request) -> Response:
        raw_limit = request.query_params.get("limit", "20")
        try:
            limit = int(raw_limit)
        except ValueError:
            return JSONResponse({"error": "INVALID_LIMIT"}, status_code=400)
        if not 1 <= limit <= 100:
            return JSONResponse({"error": "INVALID_LIMIT"}, status_code=400)
        with _store() as store:
            overview = store.task_overview(limit=limit)

        def project(entry: dict[str, Any]) -> dict[str, Any]:
            return _task_display(entry["task"], entry["repository"])

        active_tasks = [project(entry) for entry in overview["active_tasks"]]
        attention_tasks = [project(entry) for entry in overview["attention_tasks"]]
        counts = overview["counts"]
        generated_at = clock().astimezone(UTC).isoformat().replace("+00:00", "Z")
        return JSONResponse(
            {
                "generated_at": generated_at,
                "counts": counts,
                "active_tasks": active_tasks,
                "attention_tasks": attention_tasks,
                "limit": limit,
                "truncated": {
                    "active": counts["active"] > len(active_tasks),
                    "attention": counts["attention"] > len(attention_tasks),
                },
            }
        )

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
                "task": _task_display(record, repository),
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
            observed_now = clock()
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
                        "availability": provider_availability(
                            store, profile, now=observed_now, model=profile.model
                        ),
                        "native_check": access_checks.cached_native_check(
                            store, profile, now=observed_now
                        ),
                        "windows": store.latest_provider_windows(limits.status_key(profile)),
                    }
                )
            status = [
                safe
                for row in store.list_provider_status()
                if (safe := limits.safe_status_row(row)) is not None
            ]
        cache = request.app.state.doctor_cache
        if cache["result"] is None:
            doctor_result = {
                "ok": None,
                "checks": [],
                "status": "not_run",
                "cached": False,
                "fresh": False,
                "checked_at": None,
            }
        else:
            doctor_result = dict(cache["result"])
            doctor_result.update(
                status="cached",
                cached=True,
                fresh=(time.monotonic() - cache["at"] < _DOCTOR_CACHE_SECONDS),
                checked_at=cache["checked_at"],
            )
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

    # -- dispatch policy --------------------------------------------------------------

    def _policy_payload(now: datetime) -> dict[str, Any]:
        with _store() as ro_store:
            loaded = policy.load(ro_store, profiles)
            status_report = policy.status(ro_store, loaded, profiles, now)
        default_policy = policy.defaults(profiles)
        file_managed = policy.file_managed(paths.config_file, profiles)
        with policy_store_factory() as pstore:
            writable = pstore.available
        profiles_out = [
            {
                "id": profile.id,
                "family": profile.family,
                "first_class": profile.first_class,
                "auth": profile.auth,
                "modes": sorted(profile.modes),
            }
            for profile in sorted(profiles.values(), key=lambda item: item.id)
        ]
        return {
            "policy": loaded.policy.model_dump(mode="json"),
            **loaded.describe(),
            "status": status_report,
            "defaults": default_policy.model_dump(mode="json"),
            "profiles": profiles_out,
            "file_managed": file_managed,
            "writable": writable,
            "csrf_token": csrf_token,
        }

    @_guard
    def policy_get_endpoint(request: Request) -> Response:
        if not trusted_loopback(request):
            return no_store({"error": "LOOPBACK_REQUIRED"}, status=403)
        return no_store(_policy_payload(clock()))

    @_guard
    async def policy_put_endpoint(request: Request) -> Response:
        if not trusted_loopback(request):
            return no_store({"error": "LOOPBACK_REQUIRED"}, status=403)
        if not same_origin(request):
            return no_store({"error": "ORIGIN_REQUIRED"}, status=403)
        if not csrf_valid(request, csrf_token):
            return no_store({"error": "CSRF_INVALID"}, status=403)
        body, error = await read_json_object(request, _MAX_POLICY_BODY_BYTES)
        if error is not None:
            return error
        if_revision = body.get("if_revision")
        if not isinstance(if_revision, int) or isinstance(if_revision, bool) or if_revision < 0:
            return no_store({"error": "INVALID_REQUEST"}, status=400)
        document = body.get("policy")
        with policy_store_factory() as pstore:
            if not pstore.available:
                return no_store({"error": "POLICY_UNAVAILABLE"}, status=503)
            try:
                parsed = policy.parse(document)
            except policy.PolicyError as exc:
                return no_store({"error": "POLICY_INVALID", "details": {"errors": exc.errors}}, status=400)
            errors = policy.validate(parsed, profiles)
            if errors:
                return no_store({"error": "POLICY_INVALID", "details": {"errors": errors}}, status=400)
            try:
                pstore.save(
                    policy.canonical_json(parsed),
                    policy.fingerprint(parsed),
                    updated_by="web",
                    if_revision=if_revision,
                )
            except PolicyRevisionConflict as exc:
                return no_store(
                    {"error": "POLICY_REVISION_CONFLICT", "current_revision": exc.actual}, status=409
                )
        return no_store(_policy_payload(clock()))

    @_guard
    async def policy_reset_endpoint(request: Request) -> Response:
        if not trusted_loopback(request):
            return no_store({"error": "LOOPBACK_REQUIRED"}, status=403)
        if not same_origin(request):
            return no_store({"error": "ORIGIN_REQUIRED"}, status=403)
        if not csrf_valid(request, csrf_token):
            return no_store({"error": "CSRF_INVALID"}, status=403)
        body, error = await read_json_object(request, _MAX_POLICY_BODY_BYTES)
        if error is not None:
            return error
        if_revision = body.get("if_revision")
        if not isinstance(if_revision, int) or isinstance(if_revision, bool) or if_revision < 0:
            return no_store({"error": "INVALID_REQUEST"}, status=400)
        with policy_store_factory() as pstore:
            if not pstore.available:
                return no_store({"error": "POLICY_UNAVAILABLE"}, status=503)
            default_policy = policy.defaults(profiles)
            try:
                pstore.save(
                    policy.canonical_json(default_policy),
                    policy.fingerprint(default_policy),
                    updated_by="web",
                    if_revision=if_revision,
                    reason="reset",
                )
            except PolicyRevisionConflict as exc:
                return no_store(
                    {"error": "POLICY_REVISION_CONFLICT", "current_revision": exc.actual}, status=409
                )
        return no_store(_policy_payload(clock()))

    @_guard
    def policy_history_endpoint(request: Request) -> Response:
        raw_limit = request.query_params.get("limit", "50")
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 200))
        with _store() as store:
            history = store.list_dispatch_policy_history(limit=limit)
        return JSONResponse({"history": history})

    @_guard
    def policy_history_revision_endpoint(request: Request) -> Response:
        try:
            revision = int(request.path_params["revision"])
        except ValueError:
            return JSONResponse({"error": "POLICY_REVISION_NOT_FOUND"}, status_code=404)
        with _store() as store:
            row = store.get_dispatch_policy_revision(revision)
        if row is None:
            return JSONResponse({"error": "POLICY_REVISION_NOT_FOUND"}, status_code=404)
        return JSONResponse(
            {
                "revision": row["revision"],
                "policy": row["document"],
                "updated_at": row["updated_at"],
                "updated_by": row["updated_by"],
                "reason": row["reason"],
                "fingerprint": row["fingerprint"],
            }
        )

    # -- Codex AI policy (fixed adapter; no database write) --------------------------------

    def _ai_policy_setup() -> tuple[tuple[str, ...], Any, str]:
        config, missing_error = ai_policy.load_ai_policy_config(paths.config_file)
        hosts = config.hosts if config is not None else ai_policy.HOSTS
        command = config.command if config is not None else None
        return hosts, adapter_factory(command), missing_error

    async def _ai_policy_invoke(
        adapter: Any,
        payload: dict[str, Any],
        hosts: Sequence[str],
        *,
        require_results: bool,
        missing_error: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        requested = list(payload.get("hosts", hosts))
        if adapter is None:
            results = (
                [{"host": host, "ok": False, "error": missing_error} for host in requested]
                if require_results
                else None
            )
            return ai_policy.unavailable_hosts(requested, missing_error), results
        try:
            raw = await adapter(payload)
            return ai_policy.parse_adapter_payload(
                raw,
                requested,
                require_results=require_results,
                requested_mode=payload.get("mode") if require_results else None,
            )
        except ai_policy.AdapterError as exc:
            results = (
                [{"host": host, "ok": False, "error": exc.code} for host in requested]
                if require_results
                else None
            )
            return ai_policy.unavailable_hosts(requested, exc.code), results

    @_guard
    async def ai_policy_get_endpoint(request: Request) -> Response:
        if not trusted_loopback(request):
            return no_store({"error": "LOOPBACK_REQUIRED"}, status=403)
        hosts, adapter, missing_error = _ai_policy_setup()
        async with ai_policy_lock:
            host_rows, _results = await _ai_policy_invoke(
                adapter,
                {"action": "status", "hosts": list(hosts)},
                hosts,
                require_results=False,
                missing_error=missing_error,
            )
        return no_store(ai_policy.http_payload(host_rows, csrf_token))

    @_guard
    async def ai_policy_put_endpoint(request: Request) -> Response:
        if not trusted_loopback(request):
            return no_store({"error": "LOOPBACK_REQUIRED"}, status=403)
        if not same_origin(request):
            return no_store({"error": "ORIGIN_REQUIRED"}, status=403)
        if not csrf_valid(request, csrf_token):
            return no_store({"error": "CSRF_INVALID"}, status=403)
        body, error = await read_json_object(request, _MAX_AI_POLICY_BODY_BYTES)
        if error is not None:
            return error
        hosts, adapter, missing_error = _ai_policy_setup()
        parsed, request_error = ai_policy.parse_use_request(body, hosts)
        if parsed is None:
            return no_store({"error": request_error}, status=400)
        stdin = {
            "action": "use",
            "hosts": parsed["hosts"],
            "mode": parsed["mode"],
            "expected_revisions": parsed["expected_revisions"],
        }
        async with ai_policy_lock:
            host_rows, results = await _ai_policy_invoke(
                adapter, stdin, parsed["hosts"], require_results=True, missing_error=missing_error
            )
        payload = ai_policy.http_payload(host_rows, csrf_token, results)
        if not ai_policy.apply_succeeded(results, host_rows, parsed["mode"]):
            payload["error"] = "AI_POLICY_APPLY_FAILED"
            return no_store(payload, status=409)
        return no_store(payload)

    routes = [
        Route("/", index, methods=["GET"]),
        Mount("/static", app=StaticFiles(directory=Path(str(static_dir))), name="static"),
        Route("/api/health", health, methods=["GET"]),
        Route("/api/overview", overview_endpoint, methods=["GET"]),
        Route("/api/tasks", list_tasks_endpoint, methods=["GET"]),
        Route("/api/tasks/{task_id}", task_detail_endpoint, methods=["GET"]),
        Route("/api/tasks/{task_id}/diff", task_diff_endpoint, methods=["GET"]),
        Route("/api/providers", providers_endpoint, methods=["GET"]),
        Route("/api/doctor", doctor_endpoint, methods=["GET"]),
        Route("/api/usage", usage_endpoint, methods=["GET"]),
        Route("/api/policy", policy_get_endpoint, methods=["GET"]),
        Route("/api/policy", policy_put_endpoint, methods=["PUT"]),
        Route("/api/policy/reset", policy_reset_endpoint, methods=["POST"]),
        Route("/api/policy/history", policy_history_endpoint, methods=["GET"]),
        Route("/api/policy/history/{revision}", policy_history_revision_endpoint, methods=["GET"]),
        Route("/api/ai-policy", ai_policy_get_endpoint, methods=["GET"]),
        Route("/api/ai-policy", ai_policy_put_endpoint, methods=["PUT"]),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(SecurityMiddleware)])
    app.state.doctor_cache = {"result": None, "at": 0.0, "checked_at": None}
    return app
