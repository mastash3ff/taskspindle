"""The operational log: one JSON line per decision or failure, shared by every process.

Every TaskSpindle process that touches a state directory -- the stdio MCP server, the Docker
controller, a worker, a drain pass, the CLI -- appends to ``state_dir/logs/taskspindle.jsonl``.
A record carries ``ts`` (UTC, ISO-8601 with ``Z``), ``level``, ``component``, ``event``, the
writing process's role and pid, and only the optional identifiers that apply: ``task_id``,
``unit``, ``provider``, ``code``, ``exc_type``, a bounded ``message``, a bounded frames-only
``traceback`` and a few scalar fields.

What never goes in: prompts, agent output, environments, provider argv or credentials. The only
free text is a fixed message chosen at the call site or the message of an error TaskSpindle
shaped itself (one that carries a ``code``); an arbitrary exception contributes its class name
and, where asked, its stack frames without the exception's own text.

Unanticipated tracebacks keep their human-readable homes: ``state_dir/server.log`` for the MCP
server and the dispatch that runs inside a tool call, ``state_dir/dispatch-errors.log`` for a
drain pass. Each block now opens with a timestamped header, and both files rotate with the JSON
log.

Everything here is best effort. No function in this module raises: a full disk, a read-only
mount or a corrupt dedup file costs the record, never the operation being logged.

Rotation is size-capped (:data:`MAX_BYTES`, :data:`GENERATIONS` old files) and safe with several
processes appending: the size check, the rename and the append all happen under one ``fcntl``
lock on ``logs/.lock``.

Repeated failures are deduplicated across processes through ``logs/dedup.json``: the first
occurrence of a failure in a *scope* is logged in full, identical repeats inside
:data:`DEDUP_WINDOW_S` are only counted, and the count is logged as ``repeats_suppressed`` when
the failure changes, recurs after the window, or clears.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sys
import time
import traceback as traceback_module
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "COMPONENTS",
    "DEDUP_WINDOW_S",
    "GENERATIONS",
    "LOG_NAME",
    "MAX_BYTES",
    "clear",
    "configure",
    "emit",
    "exception",
    "log_dir",
    "log_path",
    "managed_files",
    "reset",
    "rotate",
]

#: The subsystems a record can name.
COMPONENTS = frozenset(
    {"server", "controller", "worker", "drain", "dispatch", "recovery", "web", "lease", "cli"}
)

LOG_DIR_NAME = "logs"
LOG_NAME = "taskspindle.jsonl"
#: The traceback files the server and the drain write, relative to the state directory.
TRACEBACK_FILES = ("server.log", "dispatch-errors.log")

MAX_BYTES = 5 * 1024 * 1024
GENERATIONS = 3
DEDUP_WINDOW_S = 15 * 60
MESSAGE_MAX = 500
FIELD_MAX = 200
TRACEBACK_MAX = 16 * 1024
_DEDUP_SCOPES_MAX = 256

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_DEFAULT_LEVEL = "info"

_config: dict[str, Any] = {"process": None, "stderr": False}


def configure(*, process: str | None = None, stderr: bool | None = None) -> None:
    """Set this process's role and whether records are mirrored to stderr.

    Only the Docker controller mirrors to stderr, so ``docker logs`` shows its records; the stdio
    MCP server must never write to its own stdio, and stays file-only.
    """
    if process is not None:
        _config["process"] = process
    if stderr is not None:
        _config["stderr"] = bool(stderr)


def reset() -> None:
    """Forget :func:`configure` (for tests)."""
    _config.update(process=None, stderr=False)


def log_dir(state_dir: Path) -> Path:
    return Path(state_dir) / LOG_DIR_NAME


def log_path(state_dir: Path) -> Path:
    return log_dir(state_dir) / LOG_NAME


def managed_files(state_dir: Path) -> list[Path]:
    """Every file this module rotates: the JSON log and the two traceback logs."""
    return [log_path(state_dir), *(Path(state_dir) / name for name in TRACEBACK_FILES)]


def _threshold() -> int:
    name = os.environ.get("TASKSPINDLE_LOG_LEVEL", _DEFAULT_LEVEL).strip().lower()
    return _LEVELS.get(name, _LEVELS[_DEFAULT_LEVEL])


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    return _bounded(str(value), FIELD_MAX)


@contextlib.contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    directory = log_dir(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _generation(path: Path, number: int) -> Path:
    return path.with_name(f"{path.name}.{number}")


def _rotate_locked(path: Path, generations: int = GENERATIONS) -> None:
    """Shift ``path`` to ``path.1`` and older generations down; the oldest falls off."""
    with contextlib.suppress(FileNotFoundError):
        _generation(path, generations).unlink()
    for number in range(generations - 1, 0, -1):
        with contextlib.suppress(FileNotFoundError):
            os.replace(_generation(path, number), _generation(path, number + 1))
    with contextlib.suppress(FileNotFoundError):
        os.replace(path, _generation(path, 1))


def _append(state_dir: Path, path: Path, data: bytes) -> None:
    """Append ``data`` to ``path``, rotating first when it would cross :data:`MAX_BYTES`."""
    with _locked(state_dir):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(data) > MAX_BYTES:
            _rotate_locked(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


def _record(
    component: str,
    event: str,
    level: str,
    *,
    task_id: str | None = None,
    unit: str | None = None,
    provider: str | None = None,
    code: str | None = None,
    exc_type: str | None = None,
    message: str | None = None,
    traceback: str | None = None,
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ts": _timestamp(),
        "level": level,
        "component": component,
        "event": event,
    }
    if _config["process"]:
        record["process"] = _config["process"]
    record["pid"] = os.getpid()
    for key, value in (
        ("task_id", task_id), ("unit", unit), ("provider", provider), ("code", code),
        ("exc_type", exc_type),
    ):
        if value is not None:
            record[key] = _scalar(value)
    if message is not None:
        record["message"] = _bounded(str(message), MESSAGE_MAX)
    for key, value in (fields or {}).items():
        if key not in record:
            record[str(key)] = _scalar(value)
    if traceback is not None:
        record["traceback"] = traceback[-TRACEBACK_MAX:]
    return record


def _write(state_dir: Path | None, record: Mapping[str, Any]) -> None:
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str) + "\n"
    if _config["stderr"]:
        with contextlib.suppress(Exception):
            sys.stderr.write(line)
            sys.stderr.flush()
    if state_dir is not None:
        with contextlib.suppress(Exception):
            _append(Path(state_dir), log_path(Path(state_dir)), line.encode("utf-8", "replace"))


def emit(
    state_dir: Path | None,
    component: str,
    event: str,
    *,
    level: str = "info",
    task_id: str | None = None,
    unit: str | None = None,
    provider: str | None = None,
    code: str | None = None,
    exc_type: str | None = None,
    message: str | None = None,
    traceback: str | None = None,
    **fields: Any,
) -> None:
    """Append one record. Never raises.

    ``state_dir=None`` writes only to stderr, and only in a process configured to mirror there.
    Records below ``TASKSPINDLE_LOG_LEVEL`` (default ``info``) are dropped.
    """
    try:
        if _LEVELS.get(level, _LEVELS["info"]) < _threshold():
            return
        _write(state_dir, _record(
            component, event, level, task_id=task_id, unit=unit, provider=provider, code=code,
            exc_type=exc_type, message=message, traceback=traceback, fields=fields,
        ))
    except Exception:  # pragma: no cover - the log is best effort by design
        pass


def _code_of(exc: BaseException) -> str | None:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None


def fingerprint(exc: BaseException) -> str:
    """What makes two failures "the same": class, code, and where it was raised."""
    frames = traceback_module.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
    raw = f"{type(exc).__name__}|{_code_of(exc) or ''}|{where}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _frames(exc: BaseException) -> str:
    """File, line and function of each frame, then the class name.

    Neither the exception's text nor the source lines are included: either can carry a literal
    argument, and a record is meant to be safe to paste anywhere.
    """
    frames = traceback_module.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    listed = [f"  File \"{frame.filename}\", line {frame.lineno}, in {frame.name}\n" for frame in frames]
    return "".join(listed) + type(exc).__name__ + "\n"


# -- dedup ------------------------------------------------------------------------------------


def _dedup_path(state_dir: Path) -> Path:
    return log_dir(state_dir) / "dedup.json"


def _load_dedup(state_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_dedup_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_dedup(state_dir: Path, data: Mapping[str, Any]) -> None:
    if len(data) > _DEDUP_SCOPES_MAX:
        keep = sorted(
            data.items(), key=lambda item: float(item[1].get("logged_at", 0)), reverse=True,
        )[:_DEDUP_SCOPES_MAX]
        data = dict(keep)
    target = _dedup_path(state_dir)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, sort_keys=True).encode())
    finally:
        os.close(fd)
    os.replace(temporary, target)


def _gate(
    state_dir: Path, scope: str, key: str, *, event: str | None = None,
    window_s: float = DEDUP_WINDOW_S,
) -> tuple[bool, dict[str, Any] | None]:
    """``(log_it, summary)``: whether to log this occurrence, and a count to report first."""
    moment = time.time()
    with _locked(state_dir):
        data = _load_dedup(state_dir)
        entry = data.get(scope)
        if (
            isinstance(entry, dict) and entry.get("key") == key
            and moment - float(entry.get("logged_at", 0)) < window_s
        ):
            entry["suppressed"] = int(entry.get("suppressed", 0)) + 1
            entry["last_at"] = moment
            _save_dedup(state_dir, data)
            return False, None
        summary = None
        if isinstance(entry, dict) and int(entry.get("suppressed", 0)) > 0:
            summary = {
                "scope": scope,
                "suppressed": int(entry["suppressed"]),
                "reason": "recurred" if entry.get("key") == key else "changed",
                "event": entry.get("event"),
            }
        data[scope] = {"key": key, "logged_at": moment, "suppressed": 0, "event": event}
        _save_dedup(state_dir, data)
        return True, summary


def _summarize(state_dir: Path, component: str, summary: Mapping[str, Any]) -> None:
    emit(
        state_dir, component, "repeats_suppressed", level="warning",
        message=f"{summary['suppressed']} identical repeats were not logged",
        scope=summary["scope"], suppressed=summary["suppressed"], reason=summary["reason"],
        repeated_event=summary.get("event"),
    )


def dedup(
    state_dir: Path | None, component: str, scope: str, key: str, *, event: str | None = None,
) -> bool:
    """Whether an occurrence of ``key`` in ``scope`` should be logged now. Never raises.

    With no state directory there is nothing to remember across calls, so everything is logged.
    """
    if state_dir is None:
        return True
    try:
        log_it, summary = _gate(Path(state_dir), scope, key, event=event)
        if summary is not None:
            _summarize(Path(state_dir), component, summary)
        return log_it
    except Exception:
        return True


def clear(state_dir: Path | None, component: str, scope: str) -> None:
    """The failure in ``scope`` stopped: report how many repeats went unlogged. Never raises.

    Cheap when nothing is being suppressed -- one ``stat`` -- because a success path calls this
    every time it succeeds.
    """
    if state_dir is None:
        return
    try:
        state = Path(state_dir)
        if not _dedup_path(state).exists() or scope not in _load_dedup(state):
            return
        with _locked(state):
            data = _load_dedup(state)
            entry = data.pop(scope, None)
            if entry is None:
                return
            _save_dedup(state, data)
        suppressed = int(entry.get("suppressed", 0)) if isinstance(entry, dict) else 0
        emit(
            state, component, "failure_cleared",
            scope=scope, suppressed=suppressed,
            repeated_event=entry.get("event") if isinstance(entry, dict) else None,
        )
    except Exception:  # pragma: no cover - the log is best effort by design
        pass


# -- exceptions -------------------------------------------------------------------------------


def exception(
    state_dir: Path | None,
    component: str,
    event: str,
    exc: BaseException,
    *,
    level: str = "error",
    traceback_file: str | None = None,
    frames: bool = False,
    dedup_scope: str | None = None,
    message: str | None = None,
    task_id: str | None = None,
    unit: str | None = None,
    provider: str | None = None,
    **fields: Any,
) -> bool:
    """Record a failure; return whether it was logged (``False`` when a repeat was suppressed).

    ``traceback_file`` (a name in the state directory, such as ``server.log``) receives the full
    traceback under a timestamped header, as those files always have. ``frames`` puts the stack
    -- without the exception's text -- into the JSON record instead. Never raises.
    """
    try:
        if dedup_scope is not None and not dedup(
            state_dir, component, dedup_scope, fingerprint(exc), event=event,
        ):
            return False
        code = _code_of(exc)
        exc_type = type(exc).__name__
        if message is None and code is not None:
            # An error TaskSpindle shaped itself: its message was written for a caller to read.
            message = str(exc)
        if traceback_file is not None and state_dir is not None:
            header = f"--- {_timestamp()} {component} {event}: {exc_type}\n"
            text = header + "".join(traceback_module.format_exception(exc))
            with contextlib.suppress(Exception):
                _append(Path(state_dir), Path(state_dir) / traceback_file, text.encode("utf-8", "replace"))
        emit(
            state_dir, component, event, level=level, task_id=task_id, unit=unit,
            provider=provider, code=code, exc_type=exc_type, message=message,
            traceback=_frames(exc) if frames else None, **fields,
        )
        return True
    except Exception:  # pragma: no cover - the log is best effort by design
        return False


# -- retention --------------------------------------------------------------------------------


def rotate(state_dir: Path, *, apply: bool) -> list[dict[str, Any]]:
    """Rotate every managed file over the cap and prune generations beyond :data:`GENERATIONS`.

    Returns one entry per action, with the bytes it frees (a rotation frees the oldest
    generation it pushes out). With ``apply=False`` nothing changes. Never raises.
    """
    actions: list[dict[str, Any]] = []
    try:
        state = Path(state_dir)
        for path in managed_files(state):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size > MAX_BYTES:
                oldest = _generation(path, GENERATIONS)
                freed = oldest.stat().st_size if oldest.exists() else 0
                actions.append({"action": "rotate", "path": str(path), "bytes": freed})
            extra = sorted(
                (
                    candidate for candidate in path.parent.glob(f"{path.name}.*")
                    if candidate.name[len(path.name) + 1:].isdigit()
                    and int(candidate.name[len(path.name) + 1:]) > GENERATIONS
                ),
                key=lambda candidate: candidate.name,
            )
            for candidate in extra:
                actions.append({
                    "action": "prune", "path": str(candidate), "bytes": candidate.stat().st_size,
                })
        if apply and actions:
            with _locked(state):
                for action in actions:
                    target = Path(action["path"])
                    if action["action"] == "rotate":
                        with contextlib.suppress(OSError):
                            if target.stat().st_size > MAX_BYTES:
                                _rotate_locked(target)
                    else:
                        with contextlib.suppress(FileNotFoundError):
                            target.unlink()
    except Exception:  # pragma: no cover - the log is best effort by design
        pass
    return actions
