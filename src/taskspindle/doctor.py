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
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import taskspindle

from . import providers
from .acp_client import AcpWorker, InitInfo, PermissionPolicy
from .config import Paths
from .providers import Profile
from .setup import ADAPTER_BIN

__all__ = [
    "GROK_VERSION_PREFIX",
    "MIN_GIT",
    "MIN_NODE",
    "Check",
    "run_doctor",
    "run_doctor_async",
]

#: The oldest git whose ``merge-tree --write-tree`` behaves the way integration relies on.
MIN_GIT = (2, 38)

#: The oldest Node the pinned adapter is supported on.
MIN_NODE = (22,)

#: The Grok CLI build whose ACP endpoint and config keys TaskSpindle was written against.
GROK_VERSION_PREFIX = "grok 1.0.13"

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
    ) -> None:
        self.profiles = dict(profiles)
        self.paths = paths
        self.parent_env = dict(parent_env)
        self.live_probes = live_probes
        self.runner = runner
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
            live.append(await self._grok_acp())
            live.extend(await self._configured_acp())
        await asyncio.to_thread(self._collect_blocking, live)

    def _collect_blocking(self, live: list[Check]) -> None:
        self.git()
        self.systemd_user()
        if self.live_probes:
            self.transient_unit()
        self.node()
        self.adapter()
        self.grok_cli()
        self.claude_oauth()
        self.checks.extend(live)
        self.child_envs()
        self.codex_registration()
        self.profile_commands()

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
            if found != taskspindle.ADAPTER_VERSION:
                raise RuntimeError(
                    f"{taskspindle.ADAPTER_PACKAGE} is at {found or 'no version'}, "
                    f"not the pinned {taskspindle.ADAPTER_VERSION}"
                )
            pinned = providers.pinned_node(self.paths.runtime_dir)
            if launcher.is_symlink() or pinned is None:
                raise RuntimeError("launcher not pinned; run taskspindle setup")
            if not launcher.is_file():
                raise RuntimeError(f"{launcher} is missing")
            if str(pinned) not in launcher.read_text(encoding="utf-8"):
                raise RuntimeError("launcher not pinned; run taskspindle setup")
            return f"{taskspindle.ADAPTER_PACKAGE} {found} at {manifest.parent}"

    def grok_cli(self) -> None:
        @self.check("grok_cli")
        def probe() -> str:
            proc = self.run(["grok", "--version"])
            if proc.returncode != 0:
                raise RuntimeError(f"grok --version exited {proc.returncode}")
            reported = (proc.stdout or "").strip()
            if not reported.startswith(GROK_VERSION_PREFIX):
                raise RuntimeError(f"{reported or 'nothing'} is not {GROK_VERSION_PREFIX}")
            return reported

    def claude_oauth(self) -> None:
        @self.check("claude_oauth")
        def probe() -> str:
            evidence = providers.claude_oauth_evidence(
                lambda command: self.run(command)
            )
            return (
                f"{evidence['authMethod']} / {evidence['subscriptionType']} / "
                f"{evidence['apiProvider']}"
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

        A first-class provider already has a check of its own -- ``claude_oauth`` and
        ``grok_acp`` -- so this is the answer for the profiles that come from ``config.toml``,
        which until now were only ever checked for a command on PATH.
        """
        checks: list[Check] = []
        for profile_id, profile in sorted(self.profiles.items()):
            if profile.first_class:
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
            agent = (init.agent_info.get("name") if init else None) or profile.command[0]
            checks.append(Check(name, True, f"configured: {agent} answered initialize"))
        return checks

    def _missing_secrets(self, profile: Profile) -> list[str]:
        """The secrets an ``api_key`` profile declares that this environment does not have."""
        if profile.auth != "api_key":
            return []
        return [name for name in profile.secret_env if name not in self.parent_env]

    async def _init_probe(self, profile: Profile, workspace: Path) -> InitInfo | None:
        """Start the agent, keep what it said about itself at ``initialize``, and stop it."""
        env = providers.build_child_env(profile, self.parent_env, task_tmp=workspace / "tmp")
        (workspace / "tmp").mkdir(parents=True, exist_ok=True)
        worker = AcpWorker(
            command=profile.command,
            env=env,
            cwd=workspace,
            stderr_path=workspace / "agent.stderr",
            policy=PermissionPolicy(allow_writes=False),
            handshake_timeout=_TIMEOUT,
        )
        async with worker as agent:
            return agent.init

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
) -> dict[str, Any]:
    """Ask every question and report the answers; advisory failures never fail the run."""
    doctor = _Doctor(
        profiles=profiles,
        paths=paths,
        parent_env=parent_env,
        live_probes=live_probes,
        runner=runner,
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
) -> dict[str, Any]:
    """:func:`run_doctor_async` for a caller that owns no event loop, such as the CLI."""
    return asyncio.run(
        run_doctor_async(
            profiles=profiles,
            paths=paths,
            parent_env=parent_env,
            live_probes=live_probes,
            runner=runner,
        )
    )
