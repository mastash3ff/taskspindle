"""The MCP server: sixteen tools, one envelope, no surprises.

Every tool returns the same shape whether it succeeded or not, so a caller never has to tell an
exception from a result. Errors carry a stable code; an error TaskSpindle did not anticipate is
reported as ``INTERNAL`` with the exception's class name only, and its traceback goes to
``state_dir/server.log`` rather than to the transport, where it would leak paths and arguments
into the conversation.

The read-only annotations are honest: ``task_diff`` is *not* read-only, because handing a page of
a diff over appends the receipt that later proves the whole candidate was inspected.
"""

from __future__ import annotations

import json
import os
import traceback
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from pydantic import BaseModel, ValidationError

from . import doctor as doctor_module
from . import providers, units
from .config import Paths, load_config
from .config import paths as default_paths
from .models import (
    AcceptTaskRequest,
    AuthorizeRepositoryRequest,
    Envelope,
    ErrorBody,
    RecordIntegrationRequest,
    StartTaskRequest,
)
from .orchestrator import Orchestrator
from .service import INVALID_REQUEST, TaskSpindleError
from .store import Store
from .units import SystemdUserBackend

__all__ = ["INTERNAL", "READ_ONLY_TOOLS", "TOOL_NAMES", "build_server", "main"]

INTERNAL = "INTERNAL"

#: The tools that only read. ``task_diff`` is deliberately absent.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "capabilities",
        "doctor",
        "list_repository_policies",
        "list_tasks",
        "task_status",
        "task_result",
    }
)

#: Every tool this server exposes, in the order it registers them.
TOOL_NAMES: tuple[str, ...] = (
    "capabilities",
    "doctor",
    "authorize_repository",
    "revoke_repository",
    "list_repository_policies",
    "start_task",
    "list_tasks",
    "task_status",
    "task_result",
    "task_diff",
    "continue_task",
    "accept_task",
    "record_integration",
    "reject_task",
    "cancel_task",
    "cleanup_task",
)


def _envelope(result: dict[str, Any]) -> dict[str, Any]:
    return Envelope(ok=True, result=result).model_dump(mode="json")


def _failure(body: ErrorBody) -> dict[str, Any]:
    return Envelope(ok=False, error=body).model_dump(mode="json")


def _log_traceback(log_path: Path, name: str, exc: BaseException) -> None:
    """Write what went wrong where the operator can read it, and nowhere else."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"--- {name}: {type(exc).__name__}\n")
            handle.write("".join(traceback.format_exception(exc)))
    except OSError:  # pragma: no cover - the log is best effort by design
        pass


def _type_name(spec: Mapping[str, Any], defs: Mapping[str, Any]) -> str:
    """A one-token description of a JSON-schema field, for a tool description."""
    if "$ref" in spec:
        return _type_name(defs.get(str(spec["$ref"]).rsplit("/", 1)[-1], {}), defs)
    if "anyOf" in spec:
        return " | ".join(_type_name(item, defs) for item in spec["anyOf"])
    if "enum" in spec:
        return " | ".join(json.dumps(value) for value in spec["enum"])
    if "const" in spec:
        return json.dumps(spec["const"])
    kind = str(spec.get("type", "any"))
    if kind == "array":
        return f"array of {_type_name(spec.get('items', {}), defs)}"
    return kind


def request_fields(model: type[BaseModel]) -> str:
    """The request object's own fields, so a caller never has to guess at the schema."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    required = set(schema.get("required", ()))
    lines = [f"{model.__name__} fields (pass them inside the request object):"]
    for name, spec in schema.get("properties", {}).items():
        mark = "required" if name in required else "optional"
        lines.append(f"- {name}: {_type_name(spec, defs)} ({mark})")
    return "\n".join(lines)


def _describe(summary: str, model: type[BaseModel]) -> str:
    return f"{summary}\n\n{request_fields(model)}"


def _failure_for(name: str, log_path: Path, exc: Exception) -> dict[str, Any]:
    """Turn whatever a tool body raised into the envelope's error body."""
    if isinstance(exc, TaskSpindleError):
        return _failure(exc.to_error_body())
    if isinstance(exc, ValidationError):
        return _failure(
            ErrorBody(
                code=INVALID_REQUEST,
                message=f"the request did not validate ({exc.error_count()} errors)",
                details={
                    "errors": json.loads(exc.json(include_url=False, include_context=False))
                },
            )
        )
    _log_traceback(log_path, name, exc)
    return _failure(
        ErrorBody(
            code=INTERNAL,
            message="the server hit an unexpected error; see the server log",
            details={"exception": type(exc).__name__},
        )
    )


def _guard(name: str, log_path: Path, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one tool body and turn whatever happens into an envelope."""
    try:
        return _envelope(call())
    except Exception as exc:
        return _failure_for(name, log_path, exc)


async def _guard_async(
    name: str, log_path: Path, call: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """The same, for a tool body that has to be awaited on the server's own loop."""
    try:
        return _envelope(await call())
    except Exception as exc:
        return _failure_for(name, log_path, exc)


def build_server(orchestrator: Orchestrator) -> FastMCP:
    """Register every tool against ``orchestrator`` and return the server."""
    mcp: FastMCP = FastMCP(
        name="taskspindle",
        instructions=(
            "Delegate bounded work to a coding agent in its own detached git worktree, then "
            "inspect, cross-review and explicitly accept what it produced. Start with "
            "capabilities; authorize a repository before starting a task in it; retrieve the "
            "whole diff and record an independent review before accept_task will run."
        ),
    )
    log_path = orchestrator.paths.state_dir / "server.log"

    def tool(name: str, description: str | None = None) -> Callable[[Callable[..., Any]], Any]:
        # ``run_in_thread=False`` is load-bearing: the store is one sqlite connection bound to
        # the thread that opened it, and tool calls arrive one at a time from a single session.
        # Serialising them on the loop thread is both correct and what the protocol already does.
        return mcp.tool(
            name=name,
            description=description,
            annotations={"readOnlyHint": name in READ_ONLY_TOOLS},
            run_in_thread=False,
        )

    def call(name: str, body: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        return _guard(name, log_path, body)

    @tool("capabilities")
    def capabilities() -> dict[str, Any]:
        """Providers, modes, versions, limits, states and what isolation does and does not mean."""
        return call("capabilities", orchestrator.capabilities)

    @tool("doctor")
    async def doctor(live_probes: bool = True) -> dict[str, Any]:
        """Check git, systemd, node, the pinned adapter, credentials and every profile."""
        return await _guard_async(
            "doctor",
            log_path,
            lambda: doctor_module.run_doctor_async(
                profiles=orchestrator.profiles,
                paths=orchestrator.paths,
                parent_env=orchestrator.parent_env,
                live_probes=live_probes,
            ),
        )

    @tool(
        "authorize_repository",
        _describe(
            "Grant providers the modes they may use on the repository containing `path`.",
            AuthorizeRepositoryRequest,
        ),
    )
    def authorize_repository(request: dict[str, Any]) -> dict[str, Any]:
        def body() -> dict[str, Any]:
            parsed = AuthorizeRepositoryRequest.model_validate(request)
            return orchestrator.authorize_repository(
                parsed.path, parsed.providers, [mode.value for mode in parsed.modes]
            )

        return call("authorize_repository", body)

    @tool("revoke_repository")
    def revoke_repository(
        path: str,
        providers: list[str] | None = None,
        modes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Withdraw grants. Omit providers or modes to revoke every one of them."""
        return call(
            "revoke_repository",
            lambda: orchestrator.revoke_repository(path, providers, modes),
        )

    @tool("list_repository_policies")
    def list_repository_policies() -> dict[str, Any]:
        """Every repository TaskSpindle knows and the grants each one holds."""
        return call("list_repository_policies", orchestrator.list_repository_policies)

    @tool(
        "start_task",
        _describe(
            "Create one task and queue it. Implement mode needs a repository, acceptance "
            "criteria, path prefixes, verification commands and a one-line candidate commit "
            "message; review mode needs a review_target.",
            StartTaskRequest,
        ),
    )
    def start_task(request: dict[str, Any]) -> dict[str, Any]:
        return call(
            "start_task",
            lambda: orchestrator.start_task(StartTaskRequest.model_validate(request)),
        )

    @tool("list_tasks")
    def list_tasks(
        repository_id: str | None = None,
        provider: str | None = None,
        mode: str | None = None,
        state: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """The public view of every matching task, newest first."""
        return call(
            "list_tasks",
            lambda: orchestrator.list_tasks(repository_id, provider, mode, state, limit),
        )

    @tool("task_status")
    def task_status(task_id: str) -> dict[str, Any]:
        """One task's state, candidate, warnings and -- when recovery is stuck -- what to do."""
        return call("task_status", lambda: orchestrator.task_status(task_id))

    @tool("task_result")
    def task_result(task_id: str) -> dict[str, Any]:
        """The answer a turn produced, its verification checks and its attribution."""
        return call("task_result", lambda: orchestrator.task_result(task_id))

    @tool("task_diff")
    def task_diff(
        task_id: str, offset: int = 0, length: int = 262144
    ) -> dict[str, Any]:
        """One base64 page of the candidate diff. Every page is receipted: acceptance requires
        that the whole diff has been retrieved."""
        return call("task_diff", lambda: orchestrator.task_diff(task_id, offset, length))

    @tool("continue_task")
    def continue_task(
        task_id: str, expected_state_version: int, prompt: str = ""
    ) -> dict[str, Any]:
        """Send one more turn: a repair on a candidate, a follow-up question, or a resume."""
        return call(
            "continue_task",
            lambda: orchestrator.continue_task(task_id, expected_state_version, prompt),
        )

    @tool(
        "accept_task",
        _describe(
            "Accept a candidate into the repository. Requires the full diff to have been "
            "retrieved, an independent review, and a disposition for every blocking or critical "
            "finding.",
            AcceptTaskRequest,
        ),
    )
    def accept_task(request: dict[str, Any]) -> dict[str, Any]:
        return call(
            "accept_task",
            lambda: orchestrator.accept_task(AcceptTaskRequest.model_validate(request)),
        )

    @tool(
        "record_integration",
        _describe(
            "Record what you did by hand: conflict_resolved, manual_integration (both still "
            "require the whole diff and an independent review, and a resulting_head that exists "
            "in the repository), or root_mutation_acknowledged, which clears the warning "
            "blocking an acceptance of this candidate revision.",
            RecordIntegrationRequest,
        ),
    )
    def record_integration(request: dict[str, Any]) -> dict[str, Any]:
        return call(
            "record_integration",
            lambda: orchestrator.record_integration(
                RecordIntegrationRequest.model_validate(request)
            ),
        )

    @tool("reject_task")
    def reject_task(
        task_id: str, expected_state_version: int, reason: str
    ) -> dict[str, Any]:
        """Discard a candidate. The worktree is retained until cleanup_task."""
        return call(
            "reject_task",
            lambda: orchestrator.reject_task(task_id, expected_state_version, reason),
        )

    @tool("cancel_task")
    def cancel_task(task_id: str, expected_state_version: int) -> dict[str, Any]:
        """Stop a task. A live worker is signalled; one that never started is cancelled here."""
        return call(
            "cancel_task",
            lambda: orchestrator.cancel_task(task_id, expected_state_version),
        )

    @tool("cleanup_task")
    def cleanup_task(task_id: str, force: bool = False) -> dict[str, Any]:
        """Give back a finished task's worktree, refs and scratch space. A dirty worktree is
        retained unless force is set."""
        return call("cleanup_task", lambda: orchestrator.cleanup_task(task_id, force))

    return mcp


def build_orchestrator(
    *,
    paths: Paths | None = None,
    parent_env: Mapping[str, str] | None = None,
) -> tuple[Orchestrator, Store]:
    """Assemble the orchestrator the stdio server runs on, and the store it owns."""
    env = dict(parent_env if parent_env is not None else os.environ)
    resolved = paths or default_paths(env)
    raw_config = env.get("TASKSPINDLE_CONFIG")
    config_file = Path(raw_config) if raw_config else resolved.config_file
    resolved = Paths(
        config_file=config_file,
        state_dir=resolved.state_dir,
        data_dir=resolved.data_dir,
        runtime_dir=resolved.runtime_dir,
    )
    settings = load_config(config_file)
    store = Store.open(resolved.state_dir / "taskspindle.sqlite3")
    profiles = providers.load_profiles(
        settings,
        runtime_dir=resolved.runtime_dir,
        home=Path(env.get("HOME", "")),
        state_dir=resolved.state_dir,
    )
    orchestrator = Orchestrator(
        store=store,
        paths=resolved,
        profiles=profiles,
        units=SystemdUserBackend(),
        boot=units.boot_id(),
        parent_env=env,
    )
    return orchestrator, store


def main() -> None:
    """``python -m taskspindle.server``: the stdio MCP server."""
    orchestrator, store = build_orchestrator()
    try:
        build_server(orchestrator).run(transport="stdio", show_banner=False)
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
