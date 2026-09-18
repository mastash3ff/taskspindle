"""Preflight checks: everything TaskSpindle needs, asked one question at a time.

No model is involved and no task is touched. Each check is isolated -- one failure never stops the
others -- so a first run on a fresh machine reports the whole list of what is missing rather than
the first thing that broke. Checks marked *advisory* describe a setup that is merely convenient;
they never make the overall result fail.

``live_probes=False`` drops the checks that actually start something: the transient unit, the Grok
ACP handshake, and the initialize probe each configured profile gets. That is the mode a packaging
smoke test runs in.

:func:`run_doctor_async` is the form the MCP server calls: the handshakes are awaited on the
running loop and every blocking subprocess check happens in a worker thread, so nothing here
starts a second event loop inside one that is already running. :func:`run_doctor` is the same
thing for a command line, with the loop supplied.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import taskspindle

from . import limits, providers
from .acp_client import AcpWorker, InitInfo, PermissionPolicy
from .agy_policy import AgyPermissionPolicy
from .config import Paths, load_config
from .providers import Profile
from .setup import ADAPTER_BIN

__all__ = [
    "GROK_TESTED_VERSIONS",
    "GROK_VERSION_PREFIX",
    "MIN_GIT",
    "MIN_NODE",
    "Check",
    "probe_initialize",
    "run_doctor",
    "run_doctor_async",
]

#: The oldest git whose ``merge-tree --write-tree`` behaves the way integration relies on.
MIN_GIT = (2, 38)

#: The oldest Node the pinned adapter is supported on.
MIN_NODE = (22,)

#: Historical smoke-tested versions, retained as metadata rather than an admission allowlist.
GROK_TESTED_VERSIONS = ("1.0.13", "1.0.30")

#: Legacy exported name; readiness is established by protocol capabilities, not this label.
GROK_VERSION_PREFIX = f"grok {GROK_TESTED_VERSIONS[0]}"

#: ``systemctl --user is-system-running`` answers TaskSpindle can work with.
_HEALTHY_SYSTEMD = frozenset({"running", "degraded"})

#: How long any probe may take.
_TIMEOUT = 30.0
_UNIT_TIMEOUT = 20.0

_CODEX_MARKER = "[mcp_servers.taskspindle]"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Check:
    """One question and its answer."""

    name: str
    ok: bool
    detail: str
    advisory: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "advisory": self.advisory}


def _newer_than_tested(name: str, reported: str, tested_up_to: str) -> str:
    """One consistent wording for a build past the newest one TaskSpindle has been tested with.

    Never blocking: a version above a floor is accepted everywhere in this module, and this
    is only ever appended to an otherwise-successful check's detail.
    """
    return f"{name} {reported} is newer than tested ({tested_up_to}); accepted, compatibility unverified"


def _version(text: str) -> tuple[int, ...]:
    """Pull the first dotted number out of a ``--version`` line."""
    for token in text.replace("v", " ").split():
        parts = token.split(".")
        if parts and parts[0].isdigit():
            numbers: list[int] = []
            for part in parts:
                head = "".join(char for char in part if char.isdigit())
                if not head:
                    break
                numbers.append(int(head))
            if numbers:
                return tuple(numbers)
    raise ValueError(f"no version number in {text.strip()!r}")


async def probe_initialize(
    profile: Profile, parent_env: Mapping[str, str], workspace: Path, *, timeout: float = 30.0,
) -> InitInfo | None:
    """Initialize an installed agent, without authentication, sessions or prompts."""
    env = providers.build_child_env(profile, parent_env, task_tmp=workspace / "tmp")
    (workspace / "tmp").mkdir(parents=True, exist_ok=True)
    worker = AcpWorker(
        command=(
            providers.launch_command(profile, "consult")
            if profile.family == "grok" else profile.command
        ),
        env=env,
        cwd=workspace,
        stderr_path=workspace / "agent.stderr",
        policy=(
            AgyPermissionPolicy(allow_writes=False, workspace=workspace)
            if profile.family == "agy" else PermissionPolicy(allow_writes=False)
        ),
        handshake_timeout=timeout,
    )
    async with worker as agent:
        return agent.init


class _Doctor:
    """Runs the checks and collects them, so one failure can never stop another."""

    def __init__(
        self,
        *,
        profiles: Mapping[str, Profile],
        paths: Paths,
        parent_env: Mapping[str, str],
        live_probes: bool,
        runner: Runner,
        provider_status: Sequence[Mapping[str, Any]] = (),
        now: datetime | None = None,
    ) -> None:
        self.profiles = dict(profiles)
        self.paths = paths
        self.parent_env = dict(parent_env)
        self.worker_container = self.parent_env.get("TASKSPINDLE_WORKER_CONTAINER") == "1"
        self.live_probes = live_probes
        self.runner = runner
        #: Rows of ``provider_status``, read by the caller on its own thread: the store's sqlite
        #: connection must never be used from the worker thread the blocking checks run on.
        self.provider_status = {str(row["provider"]): dict(row) for row in provider_status}
        self.now = now or datetime.now(UTC)
        self.checks: list[Check] = []

    # -- plumbing -------------------------------------------------------------------

    def run(self, argv: Sequence[str], *, timeout: float = _TIMEOUT) -> subprocess.CompletedProcess[str]:
        return self.runner(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )

    def check(self, name: str, *, advisory: bool = False) -> Callable[[Callable[[], str]], None]:
        """Register a check; whatever it raises becomes its failure detail."""

        def register(probe: Callable[[], str]) -> None:
            try:
                detail = probe()
            except Exception as exc:
                self.checks.append(
                    Check(name, False, f"{type(exc).__name__}: {exc}", advisory=advisory)
                )
            else:
                self.checks.append(Check(name, True, detail, advisory=advisory))

        return register

    def fail(self, name: str, detail: str, *, advisory: bool = False) -> None:
        self.checks.append(Check(name, False, detail, advisory=advisory))

    # -- the checks -----------------------------------------------------------------

    async def collect(self) -> None:
        """Ask every question: the ACP handshakes on this loop, everything else in a thread."""
        live: list[Check] = []
        if self.live_probes:
            if not self.worker_container or "grok" in self.profiles:
                live.append(await self._grok_acp())
            live.extend(await self._configured_acp())
        await asyncio.to_thread(self._collect_blocking, live)

    def _collect_blocking(self, live: list[Check]) -> None:
        self.git()
        if self.worker_container:
            self.checks.append(Check("worker_container", True, "checks execute inside the worker image"))
        else:
            self.systemd_user()
            if self.live_probes:
                self.transient_unit()
        self.node()
        if not self.worker_container or any(profile.family == "claude" for profile in self.profiles.values()):
            self.adapter()
            self.claude_oauth()
        if not self.worker_container or "grok" in self.profiles:
            self.grok_cli()
            self.grok_sandbox_hooks()
        if "agy" in self.profiles:
            self.agy_cli()
            if self.live_probes:
                self.agy_oauth()
        self.checks.extend(live)
        self.child_envs()
        if not self.worker_container:
            self.codex_registration()
        self.profile_commands()
        self.provider_availability()

    def provider_availability(self) -> None:
        """What the last turn on each provider learned about its seat. Advisory: a limit lifts."""
        for profile_id, profile in sorted(self.profiles.items()):
            row = self.provider_status.get(limits.status_key(profile))

            @self.check(f"availability_{profile_id}", advisory=True)
            def probe(row: Mapping[str, Any] | None = row) -> str:
                state = limits.effective_state(row, self.now)
                if state in ("ok", "unknown"):
                    if row and row.get("state") == "throttled":
                        return "reported reset passed; a new attempt is allowed, access is unverified"
                    return "no limit recorded" if state == "unknown" else "last task succeeded"
                detail = {
                    "throttled": "throttled: wait for the reported reset or choose another eligible worker",
                    "auth_expired": "auth_expired: sign in again with the native provider CLI",
                    "access_denied": "access_denied: verify access to the required model",
                    "model_unavailable": "model_unavailable: choose an eligible model",
                }.get(state, "provider access is unverified")
                if row and row.get("reset_at"):
                    detail += f" (resets {row['reset_at']})"
                raise RuntimeError(detail)

    def git(self) -> None:
        @self.check("git")
        def probe() -> str:
            proc = self.run(["git", "--version"])
            if proc.returncode != 0:
                raise RuntimeError(f"git --version exited {proc.returncode}")
            found = _version(proc.stdout or "")
            if found < MIN_GIT:
                raise RuntimeError(
                    f"git {'.'.join(map(str, found))} is older than the required "
                    f"{'.'.join(map(str, MIN_GIT))}"
                )
            return (proc.stdout or "").strip()

    def systemd_user(self) -> None:
        @self.check("systemd_user")
        def probe() -> str:
            proc = self.run(["systemctl", "--user", "is-system-running"])
            state = (proc.stdout or "").strip()
            if state not in _HEALTHY_SYSTEMD:
                raise RuntimeError(f"the user manager reports {state or 'nothing'}")
            return f"the user manager is {state}"

    def transient_unit(self) -> None:
        @self.check("transient_unit")
        def probe() -> str:
            proc = self.run(
                ["systemd-run", "--user", "--wait", "--collect", "--quiet", "/bin/true"],
                timeout=_UNIT_TIMEOUT,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"systemd-run exited {proc.returncode}: {(proc.stderr or '').strip()}"
                )
            return "a transient user unit ran to completion"

    def node(self) -> None:
        @self.check("node")
        def probe() -> str:
            pinned = providers.pinned_node(self.paths.runtime_dir)
            if pinned is not None:
                proc = self.run([str(pinned), "--version"])
                if proc.returncode != 0:
                    raise RuntimeError(f"{pinned} --version exited {proc.returncode}")
                found = _version(proc.stdout or "")
                if found < MIN_NODE:
                    raise RuntimeError(
                        f"pinned node {'.'.join(map(str, found))} is older than the required "
                        f"{'.'.join(map(str, MIN_NODE))}"
                    )
                return f"pinned {(proc.stdout or '').strip()} at {pinned}"

            found_path = shutil.which("node", path=self.parent_env.get("PATH"))
            if not found_path:
                raise RuntimeError("node is not on PATH and no pinned node was found")
            proc = self.run([found_path, "--version"])
            if proc.returncode != 0:
                raise RuntimeError(f"node --version exited {proc.returncode}")
            found = _version(proc.stdout or "")
            if found < MIN_NODE:
                raise RuntimeError(
                    f"node {'.'.join(map(str, found))} is older than the required "
                    f"{'.'.join(map(str, MIN_NODE))}"
                )
            return (proc.stdout or "").strip()

    def adapter(self) -> None:
        manifest = (
            self.paths.runtime_dir
            / "node_modules"
            / taskspindle.ADAPTER_PACKAGE
            / "package.json"
        )
        launcher = self.paths.runtime_dir / "node_modules" / ".bin" / ADAPTER_BIN

        @self.check("adapter")
        def probe() -> str:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            found = str(payload.get("version", ""))
            try:
                found_version = _version(found) if found else None
            except ValueError:
                found_version = None
            pinned_version = _version(taskspindle.ADAPTER_VERSION)
            if found_version is None or found_version < pinned_version:
                raise RuntimeError(
                    f"{taskspindle.ADAPTER_PACKAGE} is at {found or 'no version'}, "
                    f"not at least the pinned {taskspindle.ADAPTER_VERSION}"
                )
            pinned = providers.pinned_node(self.paths.runtime_dir)
            if pinned is None:
                raise RuntimeError("launcher not pinned; run taskspindle setup")
            # A symlinked launcher is fine as long as it resolves to something runnable; only an
            # npm-linked symlink that still needs its own ``node`` on PATH -- unreadable, missing,
            # or lacking the pinned node's path in its contents -- is the actual problem.
            if not launcher.is_file() or not os.access(launcher, os.X_OK):
                raise RuntimeError(f"{launcher} is missing or not executable")
            if str(pinned) not in launcher.read_text(encoding="utf-8"):
                raise RuntimeError("launcher not pinned; run taskspindle setup")
            detail = f"{taskspindle.ADAPTER_PACKAGE} {found} at {manifest.parent}"
            if found_version > pinned_version:
                note = _newer_than_tested(taskspindle.ADAPTER_PACKAGE, found, taskspindle.ADAPTER_VERSION)
                detail += f"; {note}"
            return detail

    def grok_cli(self) -> None:
        # A daily CLI update may change the version label or its formatting without
        # changing ACP. Executable presence and the real sandboxed handshake are
        # checked separately; version output must never veto a working provider.
        @self.check("grok_cli", advisory=True)
        def probe() -> str:
            profile = self.profiles.get("grok")
            command = profile.command[0] if profile else "grok"
            proc = self.run([command, "--version"])
            if proc.returncode != 0:
                raise RuntimeError(f"grok --version exited {proc.returncode}")
            reported = (proc.stdout or "").strip()
            label = " ".join(reported.split())[:200] or "version unavailable"
            return (f"{command} --version: {label}; informational only; "
                    "ACP compatibility requires a live probe")

    def grok_sandbox_hooks(self) -> None:
        """Advisory: a symlinked hook source makes grok refuse to start its sandbox at all.

        Grok has no cheap, non-interactive command that exercises sandbox profile resolution --
        ``grok doctor`` only checks terminal, clipboard and input support, never the sandbox --
        so this looks directly at the one filesystem shape known to make it fail: a hook source
        path under ``~/.grok/hooks`` that is a symlink rather than a real file. When that is
        true, every task on this profile fails at the handshake with the same opaque
        ``ACP_HANDSHAKE_FAILED``, and only the agent's own stderr says why.
        """
        home = Path(self.parent_env["HOME"]) if self.parent_env.get("HOME") else Path.home()
        hooks_dir = home / ".grok" / "hooks"

        @self.check("grok_sandbox_hooks", advisory=True)
        def probe() -> str:
            if not hooks_dir.is_dir():
                return f"{hooks_dir} does not exist; nothing to check"
            symlinked = sorted(entry.name for entry in hooks_dir.iterdir() if entry.is_symlink())
            if symlinked:
                raise RuntimeError(
                    "grok refuses to start its sandbox when a hook source path is a symlink: "
                    f"{', '.join(symlinked)} under {hooks_dir}"
                )
            return f"{hooks_dir} has no symlinked hook sources"

    def claude_oauth(self) -> None:
        @self.check("claude_oauth")
        def probe() -> str:
            evidence = providers.claude_oauth_evidence(
                lambda command: self.run(command)
            )
            return (
                f"{evidence['authMethod']} / {evidence['subscriptionType']} / "
                f"{evidence['apiProvider']} (cached authentication claim; entitlement unverified)"
            )

    async def _grok_acp(self) -> Check:
        """The one check that talks to an agent, awaited rather than run in its own loop."""
        profile = self.profiles.get("grok")
        if profile is None:
            return Check("grok_acp", False, "no grok profile is configured")
        try:
            detail = await self._grok_handshake(profile)
        except Exception as exc:
            return Check("grok_acp", False, f"{type(exc).__name__}: {exc}")
        return Check("grok_acp", True, detail)

    async def _grok_handshake(self, profile: Profile) -> str:
        with tempfile.TemporaryDirectory(prefix="taskspindle-doctor-") as raw:
            workspace = Path(raw)
            init = await self._init_probe(profile, workspace)
            if init is None or init.load_session is not True:
                raise RuntimeError("grok did not advertise load_session")
            evidence = providers.grok_oauth_evidence(
                [{"id": method_id} for method_id in init.auth_method_ids],
                home=Path(self.parent_env.get("HOME", "")),
            )
            return f"load_session and {', '.join(evidence['auth_method_ids'])}"

    async def _configured_acp(self) -> list[Check]:
        """Ask every configured profile's agent to initialize, in a directory of its own.

        Claude's cached OAuth claim is separate from its managed adapter's ability
        to initialize. Probe that adapter too, independent of the daily CLI version.
        Grok and native AGY have their own protocol-specific checks.
        """
        checks: list[Check] = []
        for profile_id, profile in sorted(self.profiles.items()):
            if profile.first_class and profile.family != "claude":
                continue
            if profile.family == "agy":
                from .agy_cli_adapter import agy_oauth_evidence

                try:
                    await asyncio.to_thread(
                        agy_oauth_evidence, profile, self.parent_env, runner=self.runner,
                    )
                except Exception as exc:
                    checks.append(Check(f"cli_{profile_id}", False, f"{type(exc).__name__}: {exc}"))
                else:
                    checks.append(Check(f"cli_{profile_id}", True, "native CLI cached catalog available"))
                continue
            name = f"acp_{profile_id}"
            missing = self._missing_secrets(profile)
            if missing:
                checks.append(
                    Check(name, False, f"secret not set: {', '.join(missing)}", advisory=True)
                )
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="taskspindle-doctor-") as raw:
                    init = await self._init_probe(profile, Path(raw))
            except Exception as exc:
                checks.append(Check(name, False, f"unreachable: {type(exc).__name__}: {exc}"))
                continue
            if init is None:
                checks.append(Check(name, False, "protocol: agent did not return initialize capabilities"))
                continue
            # Initialization only: Claude does not inherit Grok's load-session or
            # auth-method requirements here. OAuth is checked separately, and
            # session mode/model/permissions are verified at actual task startup.
            agent = (init.agent_info.get("name") if init else None) or profile.command[0]
            checks.append(Check(name, True, f"configured: {agent} answered initialize"))
        return checks

    def agy_cli(self) -> None:
        from .agy_cli_adapter import (
            AGY_TESTED_MAX,
            newer_than_tested,
            validate_cli_profile,
            verify_cli_version,
        )

        @self.check("agy_cli")
        def binary_probe() -> str:
            binary = validate_cli_profile(self.profiles["agy"])
            version = verify_cli_version(binary, runner=self.runner, parent_env=self.parent_env)
            detail = f"native Antigravity CLI {version} at {binary}"
            if newer_than_tested(version):
                detail += f"; {_newer_than_tested('Antigravity CLI', version, AGY_TESTED_MAX)}"
            return detail

        @self.check("agy_sandbox")
        def sandbox_probe() -> str:
            binary = shutil.which("bwrap", path=self.parent_env.get("PATH"))
            if binary is None:
                raise RuntimeError("bubblewrap is required for native Antigravity workers")
            result = self.run([binary, "--version"])
            if result.returncode != 0:
                raise RuntimeError("bubblewrap could not be started")
            if self.worker_container:
                result = self.run([
                    binary, "--die-with-parent", "--unshare-all", "--ro-bind", "/", "/",
                    "--proc", "/proc", "--dev", "/dev", "--", "/bin/true",
                ])
                if result.returncode != 0:
                    raise RuntimeError("bubblewrap cannot create an isolated worker sandbox")
                return "bubblewrap created an isolated mount and process namespace"
            return "bubblewrap is installed; task startup validates its isolated launch"

    def agy_oauth(self) -> None:
        from .agy_cli_adapter import agy_oauth_evidence

        @self.check("agy_oauth")
        def probe() -> str:
            evidence = agy_oauth_evidence(self.profiles["agy"], self.parent_env, runner=self.runner)
            return (f"cached catalog available; {evidence['model_count']} Gemini models advertised; "
                    "entitlement unverified")

    def agy_acp_oauth(self) -> None:
        """Legacy ACP cache evidence; not used for the native built-in provider."""
        from .agy_adapter import agy_oauth_evidence

        @self.check("agy_acp_oauth")
        def probe() -> str:
            agy_oauth_evidence(self.profiles["agy"])
            return "private oauth-personal cache exists; validity is checked by session creation"

    async def _agy_acp(self) -> Check:
        """Initialize only: this probe never authenticates or creates a model session."""
        from .agy_adapter import ADAPTER_VERSION, AUTH_METHOD, validate_agy_home

        profile = self.profiles["agy"]
        try:
            validate_agy_home(profile)
            with tempfile.TemporaryDirectory(prefix="taskspindle-doctor-") as raw:
                init = await self._init_probe(profile, Path(raw))
            if init is None or not init.load_session or AUTH_METHOD not in init.auth_method_ids:
                raise RuntimeError("Antigravity must advertise load_session and oauth-personal")
            if init.agent_info.get("version") not in (ADAPTER_VERSION, f"agy_acp_server_{ADAPTER_VERSION}"):
                raise RuntimeError(f"Antigravity did not report pinned ACP version {ADAPTER_VERSION}")
        except Exception as exc:
            return Check("agy_acp", False, f"{type(exc).__name__}: {exc}")
        return Check("agy_acp", True, f"Antigravity ACP {ADAPTER_VERSION}: load_session and oauth-personal")

    def _missing_secrets(self, profile: Profile) -> list[str]:
        """The secrets an ``api_key`` profile declares that this environment does not have."""
        if profile.auth != "api_key":
            return []
        return [name for name in profile.secret_env if name not in self.parent_env]

    async def _init_probe(self, profile: Profile, workspace: Path) -> InitInfo | None:
        """Start the agent, keep what it said about itself at ``initialize``, and stop it."""
        return await probe_initialize(profile, self.parent_env, workspace, timeout=_TIMEOUT)

    def child_envs(self) -> None:
        for profile_id, profile in sorted(self.profiles.items()):
            missing = self._missing_secrets(profile)
            if missing:
                # The environment cannot be built without the secret, but a machine that has not
                # been given a metered provider's key is not a broken machine.
                self.fail(
                    f"child_env_{profile_id}",
                    f"secret not set: {', '.join(missing)}",
                    advisory=True,
                )
                continue

            @self.check(f"child_env_{profile_id}")
            def probe(profile: Profile = profile) -> str:
                env = providers.build_child_env(
                    profile, self.parent_env, task_tmp=self.paths.state_dir / "tmp"
                )
                leaked = providers.env_violations(
                    env, allowed=(*profile.secret_env, *profile.env)
                )
                if leaked:
                    raise RuntimeError(f"forbidden names present: {', '.join(sorted(leaked))}")
                return f"{len(env)} names, none credential-shaped"

    def codex_registration(self) -> None:
        home = Path(self.parent_env.get("HOME", "")) if self.parent_env.get("HOME") else Path.home()
        config = home / ".codex" / "config.toml"

        @self.check("codex_registration", advisory=True)
        def probe() -> str:
            text = config.read_text(encoding="utf-8")
            if _CODEX_MARKER not in text:
                raise RuntimeError(f"{config} has no {_CODEX_MARKER} section")
            return f"{config} registers taskspindle"

    def profile_commands(self) -> None:
        for profile_id, profile in sorted(self.profiles.items()):

            @self.check(f"profile_{profile_id}_command")
            def probe(profile: Profile = profile) -> str:
                argv0 = profile.command[0]
                found = shutil.which(argv0)
                if found:
                    return found
                candidate = Path(argv0)
                if candidate.exists():
                    return str(candidate)
                raise RuntimeError(f"{argv0} is not on PATH and does not exist")

            if profile.auth != "api_key":
                continue
            for name in profile.secret_env:

                @self.check(f"profile_{profile_id}_secret_{name}", advisory=True)
                def secret(name: str = name) -> str:
                    if name not in self.parent_env:
                        raise RuntimeError(f"{name} is not set in the environment")
                    return f"{name} is set"


async def run_doctor_async(
    *,
    profiles: Mapping[str, Profile],
    paths: Paths,
    parent_env: Mapping[str, str],
    live_probes: bool = True,
    runner: Runner = subprocess.run,
    provider_status: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask every question and report the answers; advisory failures never fail the run."""
    if parent_env.get("TASKSPINDLE_WORKER_CONTAINER") != "1":
        configured = settings if settings is not None else load_config(paths.config_file)
        execution = configured.get("execution", {})
        if execution.get("backend", "systemd") == "docker":
            from .rpc import RemoteError, request

            socket = execution.get("diagnostics_socket")
            if not socket or not Path(socket).is_absolute():
                return {"ok": False, "checks": [Check(
                    "worker_diagnostics", False, "execution.diagnostics_socket must be an absolute path",
                ).as_dict()]}
            try:
                return await asyncio.to_thread(
                    request, Path(socket), "doctor", {"live": live_probes}, timeout=300,
                )
            except (RemoteError, OSError) as exc:
                return {"ok": False, "checks": [Check(
                    "worker_diagnostics", False, str(exc),
                ).as_dict()]}
    else:
        selected = parent_env.get("TASKSPINDLE_PROBE_PROVIDER")
        if selected:
            if selected not in profiles:
                return {"ok": False, "checks": [Check(
                    "worker_provider", False, f"Unknown worker profile: {selected}",
                ).as_dict()]}
            profiles = {selected: profiles[selected]}
    doctor = _Doctor(
        profiles=profiles,
        paths=paths,
        parent_env=parent_env,
        live_probes=live_probes,
        runner=runner,
        provider_status=provider_status,
        now=now,
    )
    await doctor.collect()
    return {
        "ok": all(check.ok for check in doctor.checks if not check.advisory),
        "checks": [check.as_dict() for check in doctor.checks],
    }


def run_doctor(
    *,
    profiles: Mapping[str, Profile],
    paths: Paths,
    parent_env: Mapping[str, str],
    live_probes: bool = True,
    runner: Runner = subprocess.run,
    provider_status: Sequence[Mapping[str, Any]] = (),
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """:func:`run_doctor_async` for a caller that owns no event loop, such as the CLI."""
    return asyncio.run(
        run_doctor_async(
            profiles=profiles,
            paths=paths,
            parent_env=parent_env,
            live_probes=live_probes,
            runner=runner,
            provider_status=provider_status,
            settings=settings,
        )
    )
