"""Durable, explicitly requested one-shot Docker jobs. Only the controller imports this module."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, NotFound
from docker.types import LogConfig, Mount

from .config import ConfigError, Paths
from .units import UnitError, UnitState

_UNIT = re.compile(r"taskspindle-(worker|accept)-([A-Za-z0-9_-]{1,160})\Z")
_IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[^\s]+@sha256:[0-9a-f]{64})\Z")
_TERMINAL = {"exited", "dead"}


def _atomic(path: Path, value: Any) -> None:
    fd, raw = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(raw)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 65536:
            raise ValueError
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError
        return result
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise UnitError("UNIT_RECORD_INVALID", "Execution record cannot be safely read") from exc


class DockerBackend:
    """Persist intent before Engine calls; never interpret transport failure as absence."""
    requires_inactive_previous_turn = True

    def __init__(self, paths: Paths, settings: Mapping[str, Any], *, client: Any = None) -> None:
        self.paths, self.settings = paths, settings
        self.config = settings.get("execution", {})
        self.image = self.config.get("worker_image", "")
        if not isinstance(self.image, str) or not _IMAGE.fullmatch(self.image):
            raise ConfigError("execution.worker_image must be an immutable sha256 image ID or digest")
        self.user = self.config.get("user", "1000:1000")
        if not isinstance(self.user, str) or not re.fullmatch(r"[1-9][0-9]*:[0-9]+", self.user):
            raise ConfigError("execution.user must be a non-root numeric UID:GID")
        self.directory = paths.state_dir / "execution"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.owner = hashlib.sha256(str(paths.state_dir).encode()).hexdigest()[:20]
        self.client = client if client is not None else docker.from_env(timeout=30)
        self._thread_lock = threading.RLock()

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock, (self.directory / "operations.lock").open("a") as stream:
            os.chmod(stream.name, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _record_path(self, unit: str) -> Path:
        if not isinstance(unit, str) or not _UNIT.fullmatch(unit):
            raise UnitError("UNIT_INVALID", "Invalid logical job name")
        return self.directory / f"{unit}.json"

    def _record(self, unit: str) -> dict[str, Any] | None:
        record = _read(self._record_path(unit))
        if record is not None:
            required = {"unit", "generation", "identity", "name", "phase", "kind", "task_id"}
            if not required <= record.keys() or record["unit"] != unit:
                raise UnitError("UNIT_RECORD_INVALID", "Execution record has invalid identity")
            match = _UNIT.fullmatch(unit)
            assert match is not None
            if (record["kind"], record["task_id"]) != match.groups():
                raise UnitError("UNIT_RECORD_INVALID", "Execution record has invalid job kind")
            if not re.fullmatch(r"[0-9a-f]{32}", str(record["generation"])):
                raise UnitError("UNIT_RECORD_INVALID", "Execution record has invalid generation")
            expected = f"taskspindle-{self.owner}-{record['generation']}"
            if record["name"] != expected or record["phase"] not in {
                "reserved", "creating", "created", "starting", "started", "finished", "failed", "retired",
            }:
                raise UnitError("UNIT_RECORD_INVALID", "Execution record has invalid launch state")
            if not isinstance(record["identity"], str):
                raise UnitError("UNIT_RECORD_INVALID", "Execution record has invalid turn identity")
            if "container_id" in record and not isinstance(record["container_id"], str):
                raise UnitError("UNIT_RECORD_INVALID", "Execution record has invalid container ID")
            evidence = record.get("evidence")
            if evidence is not None:
                template = asdict(UnitState("", "", "", ""))
                if (not isinstance(evidence, dict) or set(evidence) != set(template)
                        or any(not isinstance(evidence[key], str) for key in (
                            "load_state", "active_state", "sub_state", "result",
                        )) or any(evidence[key] is not None and type(evidence[key]) is not int
                                  for key in ("exec_main_status", "main_pid"))):
                    raise UnitError("UNIT_RECORD_INVALID", "Execution record has invalid exit evidence")
            if record["phase"] in {"finished", "retired"} and (
                evidence is None or UnitState(**evidence).kind not in {"success", "exit", "oom", "signal"}
            ):
                raise UnitError("UNIT_RECORD_INVALID", "Terminal execution record has no terminal evidence")
        return record

    def _save(self, record: dict[str, Any]) -> None:
        _atomic(self._record_path(record["unit"]), record)

    def admission_open(self) -> bool:
        with self._locked():
            value = _read(self.directory / "admission.json")
            if value is None:
                return True
            if type(value.get("open")) is not bool:
                raise UnitError("UNIT_RECORD_INVALID", "Admission record is invalid")
            return value["open"]

    def set_admission(self, value: bool) -> None:
        if type(value) is not bool:
            raise UnitError("UNIT_INVALID", "Admission value must be boolean")
        with self._locked():
            _atomic(self.directory / "admission.json", {"open": value})

    @contextlib.contextmanager
    def _database(self):
        try:
            connection = sqlite3.connect(
                (self.paths.state_dir / "taskspindle.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=5,
            )
            connection.row_factory = sqlite3.Row
            try:
                yield connection
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise UnitError("UNIT_QUERY_FAILED", "Task database cannot be read safely") from exc

    def _identity(self, unit: str) -> tuple[str, str, str, str | None]:
        match = _UNIT.fullmatch(unit)
        if match is None:
            raise UnitError("UNIT_INVALID", "Invalid logical job name")
        kind, task_id = match.groups()
        with self._database() as connection:
            task = connection.execute(
                "SELECT provider, provider_family FROM tasks WHERE id=?", (task_id,),
            ).fetchone()
            if task is None:
                raise UnitError("UNIT_START_FAILED", "Launch task does not exist")
            if kind == "accept":
                row = connection.execute(
                    "SELECT started_at, candidate_sha FROM integration_journal WHERE task_id=?", (task_id,),
                ).fetchone()
                if row is None:
                    raise UnitError("UNIT_START_FAILED", "Accept launch has no integration journal")
                identity = json.dumps(list(row))
                family = None
            else:
                row = connection.execute("SELECT MAX(id) FROM turns WHERE task_id=?", (task_id,)).fetchone()
                identity = str(row[0])
                family = task["provider_family"] or task["provider"]
                if family not in {"claude", "grok", "agy"}:
                    profile = self._profiles().get(task["provider"])
                    family = profile.family if profile else None
                if family not in {"claude", "grok", "agy"}:
                    raise UnitError("UNIT_START_FAILED", "Task provider family is not configured")
        return kind, task_id, identity, family

    def _profiles(self):
        from .providers import load_profiles
        return load_profiles(self.settings, runtime_dir=self.paths.runtime_dir,
                             home=Path(os.environ.get("HOME", "/home/bsheffield")),
                             state_dir=self.paths.state_dir, data_dir=self.paths.data_dir,
                             parent_env=os.environ)

    def _mounts(self, provider: str | None, *, probe: bool = False) -> list[Mount]:
        configured = self.config.get("mounts", [])
        providers = self.config.get("provider_mounts", {})
        if not isinstance(configured, list) or not isinstance(providers, Mapping):
            raise ConfigError("execution mounts must be arrays and provider_mounts a table")
        selected = providers.get(provider, []) if provider else []
        if not isinstance(selected, list):
            raise ConfigError("execution provider mount entries must be arrays")
        mounts: list[Mount] = []
        targets: set[str] = set()
        protected = [Path("/var/run/docker.sock"), Path("/run/docker.sock")]
        for key in ("jobs_socket", "diagnostics_socket"):
            if self.config.get(key):
                protected.append(Path(self.config[key]))
        for item in ([] if probe else configured) + selected:
            if not isinstance(item, Mapping) or set(item) != {"source", "target", "read_only"}:
                raise ConfigError("execution mount requires source, target, and read_only")
            source, target = item["source"], item["target"]
            if (not isinstance(source, str) or not Path(source).is_absolute()
                    or source != target or type(item["read_only"]) is not bool):
                raise ConfigError("execution mounts must preserve absolute paths and specify read_only")
            resolved = Path(source).resolve()
            if not resolved.exists():
                raise UnitError("UNIT_START_FAILED", "Required worker mount source does not exist")
            if resolved.is_socket() or any(p == resolved or resolved in p.parents for p in protected):
                raise ConfigError("Worker mounts must not expose control or Docker sockets")
            if target in targets:
                raise ConfigError("Worker mount targets must be unique")
            targets.add(target)
            mounts.append(Mount(target=target, source=source, type="bind", read_only=item["read_only"]))
        return mounts

    def _options(self, provider: str | None, *, probe: bool = False) -> dict[str, Any]:
        security = ["no-new-privileges:true"]
        if provider in {"agy", "grok"} and self.config.get("seccomp_profile"):
            profile = Path(self.config["seccomp_profile"])
            if not profile.is_absolute():
                raise ConfigError("execution.seccomp_profile must be absolute")
            try:
                value = json.loads(profile.read_text())
                if not isinstance(value, dict) or value.get("defaultAction") != "SCMP_ACT_ERRNO":
                    raise ValueError
            except (OSError, ValueError) as exc:
                raise ConfigError(
                    "Worker seccomp profile must be a readable restrictive JSON profile",
                ) from exc
            security.append("seccomp=" + json.dumps(value, separators=(",", ":")))
        return dict(user=self.user, mounts=self._mounts(provider, probe=probe), init=True,
                    use_config_proxy=False,
                    restart_policy={"Name": "no"}, auto_remove=False, cap_drop=["ALL"],
                    security_opt=security, privileged=False, read_only=True,
                    tmpfs={"/tmp": "rw,nosuid,nodev,size=512m,mode=1777"},
                    mem_limit=3 * 1024**3, mem_reservation=2 * 1024**3,
                    memswap_limit=3 * 1024**3 + 512 * 1024**2, pids_limit=512,
                    stop_signal="SIGTERM", log_config=LogConfig(type="json-file", config={
                        "max-size": "1m", "max-file": "2",
                    }))

    def _container(self, record: dict[str, Any]):
        try:
            container = self.client.containers.get(record.get("container_id") or record["name"])
        except NotFound:
            return None
        except Exception as exc:
            raise UnitError("UNIT_QUERY_FAILED", "Docker Engine query is unavailable") from exc
        labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
        if (labels.get("taskspindle.owner") != self.owner
                or labels.get("taskspindle.generation") != record["generation"]):
            raise UnitError("UNIT_RECORD_INVALID", "Container ownership does not match launch record")
        if record.get("container_id") and record["container_id"] != container.id:
            raise UnitError("UNIT_RECORD_INVALID", "Container ID does not match launch record")
        return container

    def _observe(self, record: dict[str, Any], container: Any) -> UnitState:
        state = container.attrs["State"]
        status = state.get("Status", "unknown")
        if status in {"running", "restarting", "paused"}:
            result = UnitState("loaded", "active", status, "success", None, state.get("Pid"))
        elif status in _TERMINAL:
            code = state.get("ExitCode")
            outcome = "oom-kill" if state.get("OOMKilled") else (
                "success" if code == 0 else "signal" if isinstance(code, int) and code >= 128 else "exit-code"
            )
            result = UnitState("loaded", "inactive" if code == 0 else "failed", status, outcome, code)
            record["phase"] = "finished"
        else:
            result = UnitState("loaded", "unknown", status, "unknown")
        record["container_id"] = container.id
        record["evidence"] = asdict(result)
        self._save(record)
        return result

    def start(self, unit: str, argv: Sequence[str], *, working_dir: Path,
              env: Mapping[str, str], properties: Mapping[str, str]) -> None:
        self._record_path(unit)
        with self._locked():
            # Read fence within the same cross-process lock as the launch reservation.
            admission = _read(self.directory / "admission.json")
            if admission is not None and admission.get("open") is not True:
                raise UnitError("UNIT_ADMISSION_CLOSED", "Worker admission is closed")
            kind, task_id, identity, provider = self._identity(unit)
            record = self._record(unit)
            if record is not None:
                try:
                    container = self._container(record)
                except UnitError as exc:
                    raise UnitError("UNIT_START_UNCERTAIN", "Earlier launch cannot be inspected") from exc
                if container is not None:
                    observed = self._observe(record, container)
                    if observed.kind == "active" and record["identity"] != identity:
                        raise UnitError("UNIT_PREVIOUS_TURN_ACTIVE", "An earlier turn still owns the job")
                    if record["identity"] == identity and observed.kind != "unknown":
                        return
                if record["phase"] not in {"retired", "failed", "finished"}:
                    raise UnitError("UNIT_START_UNCERTAIN", "An earlier launch has an unsettled outcome")
                if record["identity"] == identity and record["phase"] != "failed":
                    raise UnitError("UNIT_START_UNCERTAIN", "This launch identity has already been consumed")
                _atomic(self.directory / f"history-{record['generation']}.json", record)
            # Validate every input before reserving a launch. No host Python path enters the image.
            expected = ["-m", f"taskspindle.{'runner' if kind == 'worker' else 'accept'}", "--task", task_id]
            if list(argv)[1:] != expected or Path(working_dir) != self.paths.state_dir:
                raise UnitError("UNIT_START_FAILED", "Invalid task launch command or working directory")
            options = self._options(provider)
            try:
                self.client.images.get(self.image)  # local only; never pull or authenticate
            except Exception as exc:
                raise UnitError("UNIT_START_FAILED", "Worker image is not locally available") from exc
            generation = uuid.uuid4().hex
            record = {"unit": unit, "kind": kind, "task_id": task_id, "identity": identity,
                      "generation": generation, "name": f"taskspindle-{self.owner}-{generation}",
                      "phase": "creating"}
            self._save(record)
            labels = {"taskspindle.owner": self.owner, "taskspindle.generation": generation,
                      "taskspindle.unit": unit, "taskspindle.kind": kind, "taskspindle.task": task_id}
            try:
                container = self.client.containers.create(
                    self.image, ["python", *expected], name=record["name"], labels=labels,
                    working_dir=str(working_dir),
                    environment={**env, "TASKSPINDLE_WORKER_CONTAINER": "1",
                                 "PATH": "/opt/taskspindle/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                                 "XDG_DATA_HOME": "/opt/taskspindle/data"}, **options,
                )
            except Exception as exc:
                try:
                    container = self._container(record)
                except UnitError:
                    container = None
                if container is None:
                    if isinstance(exc, APIError) and exc.status_code in {400, 401, 403, 404, 422}:
                        record["phase"] = "failed"
                        self._save(record)
                        raise UnitError("UNIT_START_FAILED", "Docker rejected the job before launch") from exc
                    raise UnitError("UNIT_START_UNCERTAIN", "Docker create outcome is unsettled") from exc
            record["container_id"] = container.id
            record["phase"] = "starting"
            self._save(record)
            try:
                container.start()
            except Exception as exc:
                try:
                    current = self._container(record)
                except UnitError as query_error:
                    raise UnitError("UNIT_START_UNCERTAIN", "Docker start is unsettled") from query_error
                if current is not None and self._observe(record, current).kind in {
                    "active", "success", "exit", "signal", "oom",
                }:
                    return
                raise UnitError("UNIT_START_UNCERTAIN", "Docker start outcome is unsettled") from exc
            record["phase"] = "started"
            self._save(record)

    def show(self, unit: str) -> UnitState:
        with self._locked():
            record = self._record(unit)
            if record is None:
                return UnitState("not-found", "inactive", "dead", "unknown")
            container = self._container(record)
            if container is not None:
                observed = self._observe(record, container)
                if observed.kind == "unknown":
                    raise UnitError("UNIT_START_UNCERTAIN", "Docker has not confirmed launch execution")
                return observed
            if record.get("evidence") and record["phase"] in {"finished", "retired"}:
                return UnitState(**record["evidence"])
            if record["phase"] == "failed":
                return UnitState("not-found", "inactive", "dead", "unknown")
            raise UnitError("UNIT_START_UNCERTAIN", "Recorded launch is missing without terminal evidence")

    def _signal(self, unit: str, signal: str | None) -> None:
        with self._locked():
            record = self._record(unit)
            if record is None:
                return
            container = self._container(record)
            if container is None:
                if record["phase"] in {"finished", "retired", "failed"}:
                    return
                raise UnitError("UNIT_STOP_FAILED", "Cannot settle an unobserved launch")
            if container.attrs["State"].get("Status") in _TERMINAL:
                self._observe(record, container)
                return
            try:
                if signal and signal not in {"SIGTERM", "TERM"}:
                    container.kill(signal=signal)
                # Stop waits then kills all remaining processes in the container namespace.
                container.stop(timeout=30)
                container.reload()
                observed = self._observe(record, container)
                if observed.kind in {"active", "unknown"}:
                    raise UnitError("UNIT_STOP_FAILED", "Docker has not confirmed container termination")
            except Exception as exc:
                raise UnitError("UNIT_STOP_FAILED", "Docker could not confirm container termination") from exc

    def kill(self, unit: str, signal: str) -> None:
        if signal not in {"SIGTERM", "TERM", "SIGKILL", "KILL", "SIGINT", "INT"}:
            raise UnitError("UNIT_KILL_FAILED", "Unsupported container signal")
        self._signal(unit, signal)

    def stop(self, unit: str) -> None:
        self._signal(unit, None)

    def reset_failed(self, unit: str) -> None:
        with self._locked():
            record = self._record(unit)
            if record is None:
                return
            container = self._container(record)
            if container is not None:
                state = self._observe(record, container)
                if state.kind in {"active", "unknown"}:
                    return
                # Persist evidence before deleting the Docker object.
                try:
                    container.remove()
                except Exception as exc:
                    raise UnitError("UNIT_QUERY_FAILED", "Docker job cleanup is unavailable") from exc
            elif record["phase"] not in {"finished", "retired", "failed"}:
                raise UnitError("UNIT_START_UNCERTAIN", "Cannot clean up an unsettled launch")
            if record["phase"] != "failed":
                record["phase"] = "retired"
            self._save(record)

    def status(self) -> dict[str, Any]:
        with self._locked():
            try:
                self.client.ping()
                reachable = True
            except Exception:
                reachable = False
            jobs = []
            for path in sorted(self.directory.glob("taskspindle-*.json")):
                record = self._record(path.stem)
                assert record is not None
                try:
                    container = self._container(record)
                    state = self._observe(record, container).kind if container else (
                        "finished" if record["phase"] in {"finished", "retired", "failed"} else "unknown"
                    )
                except UnitError:
                    state = "unknown"
                if state in {"active", "unknown"}:
                    jobs.append({"unit": record["unit"], "kind": record["kind"],
                                 "task_id": record["task_id"], "state": state})
            admission = _read(self.directory / "admission.json")
            try:
                with self._database() as connection:
                    unsettled = connection.execute(
                        "SELECT COUNT(*) FROM (SELECT task_id FROM integration_journal "
                        "UNION SELECT id FROM tasks WHERE state='ACCEPTING')",
                    ).fetchone()[0]
                    reservations = connection.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
                    nonterminal = connection.execute(
                        "SELECT COUNT(*) FROM tasks WHERE state IN ('RUNNING', 'PREPARING', 'CANCELLING')",
                    ).fetchone()[0]
            except UnitError:
                unsettled = reservations = nonterminal = None
            return {"backend": "docker", "admission_open": admission is None or admission.get("open") is True,
                    "engine_reachable": reachable, "jobs": jobs, "unsettled_integrations": unsettled,
                    "active_reservations": reservations, "nonterminal_worker_tasks": nonterminal}

    def interrupt_workers(self) -> dict[str, Any]:
        # Fence first; never reopen automatically even if the guard or stop fails.
        self.set_admission(False)
        with self._database() as connection:
            journal = connection.execute(
                "SELECT task_id FROM integration_journal UNION SELECT id FROM tasks WHERE state='ACCEPTING' "
                "LIMIT 1",
            ).fetchone()
        snapshot = self.status()
        if journal or any(job["kind"] == "accept" for job in snapshot["jobs"]):
            raise UnitError("ACCEPT_IN_FLIGHT", "An integration is active or unsettled")
        for job in snapshot["jobs"]:
            self.stop(job["unit"])
        return self.status()

    def run_probe(self, provider: str, argv: Sequence[str], *, env: Mapping[str, str] | None = None,
                  timeout: float = 90) -> dict[str, Any]:
        family = provider
        if family not in {"claude", "grok", "agy"}:
            profile = self._profiles().get(provider)
            family = profile.family if profile else None
        if family not in {"claude", "grok", "agy"} or not 0 < timeout <= 90:
            raise UnitError("UNIT_INVALID", "Invalid diagnostic provider or timeout")
        options = self._options(family, probe=True)
        # Config is needed to reconstruct provider profiles. Runtime tools are image-baked.
        if not self.paths.config_file.is_file():
            raise UnitError("DIAGNOSTIC_FAILED", "Diagnostic configuration is unavailable")
        options["mounts"].append(Mount(target=str(self.paths.config_file), source=str(self.paths.config_file),
                                       type="bind", read_only=True))
        # Scratch is a tmpfs, never the live state bind; provider mounts are selected explicitly.
        uid = self.user.split(":")[0]
        options["tmpfs"][str(self.paths.state_dir)] = f"rw,nosuid,nodev,uid={uid},mode=700"
        container = None
        name = f"taskspindle-probe-{uuid.uuid4().hex}"
        try:
            self.client.images.get(self.image)
            try:
                container = self.client.containers.create(
                    self.image, list(argv), name=name,
                    labels={"taskspindle.owner": self.owner, "taskspindle.kind": "diagnostic"},
                    environment={"HOME": os.environ.get("HOME", "/home/bsheffield"),
                                 "XDG_STATE_HOME": str(self.paths.state_dir.parent), **(env or {}),
                                 "TASKSPINDLE_CONFIG": str(self.paths.config_file),
                                 "TASKSPINDLE_WORKER_CONTAINER": "1",
                                 "PATH": "/opt/taskspindle/.venv/bin:/usr/local/bin:/usr/bin:/bin",
                                 "XDG_DATA_HOME": "/opt/taskspindle/data"}, **options,
                )
            except Exception:
                # Recover an accepted create with a lost reply so its owned container is cleaned.
                recovered = self.client.containers.get(name)
                labels = recovered.attrs.get("Config", {}).get("Labels", {})
                if (labels.get("taskspindle.owner") == self.owner
                        and labels.get("taskspindle.kind") == "diagnostic"):
                    container = recovered
                raise
            container.start()
            result = container.wait(timeout=timeout)
            stdout = container.logs(stdout=True, stderr=False, tail=1000)[-65536:].decode("utf-8", "replace")
            stderr = container.logs(stdout=False, stderr=True, tail=1000)[-65536:].decode("utf-8", "replace")
            return {"exit_code": result["StatusCode"], "stdout": stdout, "stderr": stderr}
        except Exception as exc:
            raise UnitError("DIAGNOSTIC_FAILED", "Isolated worker diagnostic failed") from exc
        finally:
            if container is not None:
                with contextlib.suppress(Exception):
                    container.stop(timeout=1)
                    container.remove()
