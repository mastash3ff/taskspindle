"""The preflight checks, against a recorded command runner rather than a real machine."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import taskspindle
from taskspindle import doctor
from taskspindle.acp_client import InitInfo
from taskspindle.config import Paths
from taskspindle.doctor import run_doctor
from taskspindle.providers import Profile

#: The node ``install_adapter`` pins by default, standing in for what ``taskspindle setup`` would
#: have resolved and recorded.
FAKE_NODE = "/opt/fakenode/bin/node"

#: What a healthy machine answers.
HEALTHY: dict[str, tuple[int, str]] = {
    "git": (0, "git version 2.43.0\n"),
    "systemctl": (0, "running\n"),
    "systemd-run": (0, ""),
    FAKE_NODE: (0, "v22.11.0\n"),
    "grok": (0, "grok 1.0.13\n"),
    "claude": (
        0,
        json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "max",
                "apiProvider": "firstParty",
            }
        ),
    ),
}


#: What a well-behaved agent answers at ``initialize``.
HANDSHAKE = InitInfo(
    load_session=True, auth_method_ids=("cached_token",), agent_info={"name": "a-harness"}
)


def _fake_probe(answer: InitInfo | Exception):
    """A stand-in for the ACP initialize probe: no process is started."""

    async def probe(self: object, profile: Profile, workspace: Path) -> InitInfo:
        if isinstance(answer, Exception):
            raise answer
        return answer

    return probe


class RecordedRunner:
    """Answers by the command's name and remembers everything it was asked to run."""

    def __init__(self, answers: dict[str, tuple[int, str]] | None = None) -> None:
        self.answers = dict(HEALTHY if answers is None else answers)
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        timeout: float = 30.0,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        code, stdout = self.answers.get(argv[0], (127, ""))
        return subprocess.CompletedProcess(list(argv), code, stdout, "")


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


def install_adapter(paths: Paths, version: str, *, pin_node: bool = True) -> None:
    manifest = paths.runtime_dir / "node_modules" / taskspindle.ADAPTER_PACKAGE / "package.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"version": version}), encoding="utf-8")
    if pin_node:
        pin_node_and_launcher(paths, FAKE_NODE)


def pin_node_and_launcher(paths: Paths, node: str) -> Path:
    """Lay out what a pinned ``taskspindle setup`` leaves: the node-path file and the shim."""
    (paths.runtime_dir / "node-path").write_text(f"{node}\n", encoding="utf-8")
    entry = paths.runtime_dir / "node_modules" / taskspindle.ADAPTER_PACKAGE / "dist" / "index.js"
    launcher = paths.runtime_dir / "node_modules" / ".bin" / "claude-agent-acp"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(f'#!/bin/sh\nexec "{node}" "{entry}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


def install_symlinked_launcher(paths: Paths) -> None:
    """Lay out the pre-fix launcher: ``npm``'s own relative symlink into ``dist/index.js``."""
    modules = paths.runtime_dir / "node_modules"
    dist = modules / taskspindle.ADAPTER_PACKAGE / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    binaries = modules / ".bin"
    binaries.mkdir(parents=True, exist_ok=True)
    link = binaries / "claude-agent-acp"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(Path("..") / taskspindle.ADAPTER_PACKAGE / "dist" / "index.js")


def register_codex(home: Path, *, present: bool = True) -> None:
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    body = "[mcp_servers.taskspindle]\ncommand = \"taskspindle\"\n" if present else "[history]\n"
    config.write_text(body, encoding="utf-8")


def profile(profile_id: str = "shell", **overrides: object) -> Profile:
    fields: dict[str, object] = {
        "id": profile_id,
        "auth": "oauth",
        "command": ("/bin/sh",),
    }
    fields.update(overrides)
    return Profile(**fields)  # type: ignore[arg-type]


def by_name(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {check["name"]: check for check in report["checks"]}  # type: ignore[index,union-attr]


def run(paths: Paths, home: Path, runner: RecordedRunner, **overrides: object) -> dict[str, object]:
    settings: dict[str, object] = {
        "profiles": {"shell": profile()},
        "paths": paths,
        "parent_env": {"HOME": str(home), "PATH": "/usr/bin:/bin"},
        "live_probes": False,
        "runner": runner,
    }
    settings.update(overrides)
    return run_doctor(**settings)  # type: ignore[arg-type]


def test_a_healthy_machine_passes_every_check(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    register_codex(tmp_path)
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner)

    assert report["ok"] is True
    checks = by_name(report)
    assert checks["git"]["detail"] == "git version 2.43.0"
    assert checks["node"]["detail"] == f"pinned v22.11.0 at {FAKE_NODE}"
    assert checks["adapter"]["detail"].startswith(taskspindle.ADAPTER_PACKAGE)
    assert checks["profile_shell_command"]["detail"] == "/bin/sh"
    assert checks["child_env_shell"]["ok"] is True


def test_worker_container_omits_host_systemd_and_unmounted_provider_checks(paths, tmp_path):
    commands = RecordedRunner()
    report = run(
        paths, tmp_path, commands,
        parent_env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TASKSPINDLE_WORKER_CONTAINER": "1"},
    )
    checks = by_name(report)
    assert checks["worker_container"]["ok"] is True
    assert "systemd_user" not in checks and "transient_unit" not in checks
    assert "claude_oauth" not in checks and "grok_cli" not in checks
    assert "git" in checks and "node" in checks and "child_env_shell" in checks
    assert not any(call[0] in ("systemctl", "systemd-run") for call in commands.calls)


def test_a_throttled_provider_is_an_advisory_check(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    register_codex(tmp_path)
    throttled = {
        "provider": "shell",
        "state": "throttled",
        "code": "PROVIDER_THROTTLED",
        "reason": "You've hit your limit",
        "reset_at": "2999-01-01T00:00:00Z",
    }

    report = run(paths, tmp_path, RecordedRunner(), provider_status=[throttled])

    checks = by_name(report)
    assert checks["availability_shell"]["ok"] is False
    assert checks["availability_shell"]["advisory"] is True
    assert "resets 2999-01-01T00:00:00Z" in checks["availability_shell"]["detail"]
    assert report["ok"] is True

    clear = by_name(run(paths, tmp_path, RecordedRunner()))
    assert clear["availability_shell"] == {
        "name": "availability_shell",
        "ok": True,
        "detail": "no limit recorded",
        "advisory": True,
    }


def test_an_old_git_is_reported_with_the_version_it_found(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    runner = RecordedRunner({**HEALTHY, "git": (0, "git version 2.34.1\n")})

    report = run(paths, tmp_path, runner)

    check = by_name(report)["git"]
    assert check["ok"] is False
    assert "2.34.1" in check["detail"]
    assert "2.38" in check["detail"]
    assert report["ok"] is False


def test_a_degraded_user_manager_is_accepted(paths: Paths, tmp_path: Path) -> None:
    runner = RecordedRunner({**HEALTHY, "systemctl": (0, "degraded\n")})

    report = run(paths, tmp_path, runner)

    assert by_name(report)["systemd_user"] == {
        "name": "systemd_user",
        "ok": True,
        "detail": "the user manager is degraded",
        "advisory": False,
    }


def test_an_offline_user_manager_fails(paths: Paths, tmp_path: Path) -> None:
    runner = RecordedRunner({**HEALTHY, "systemctl": (1, "offline\n")})

    report = run(paths, tmp_path, runner)

    assert by_name(report)["systemd_user"]["ok"] is False
    assert report["ok"] is False


def test_an_adapter_at_the_wrong_version_is_flagged(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, "0.1.0")
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner)

    check = by_name(report)["adapter"]
    assert check["ok"] is False
    assert "0.1.0" in check["detail"]
    assert taskspindle.ADAPTER_VERSION in check["detail"]


def test_an_adapter_newer_than_pinned_is_accepted_as_advisory(paths: Paths, tmp_path: Path) -> None:
    """A vendor release bumping the installed adapter past the pin must never veto readiness."""
    install_adapter(paths, "999.0.0")
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner)

    check = by_name(report)["adapter"]
    assert check["ok"] is True
    assert check["advisory"] is False
    assert "999.0.0" in check["detail"]
    assert "newer than tested" in check["detail"]
    assert report["ok"] is True


def test_a_symlinked_launcher_that_resolves_to_an_executable_is_accepted(
    paths: Paths, tmp_path: Path
) -> None:
    """Only an npm-linked symlink that still needs its own ``node`` on PATH is the problem --
    a symlink that resolves to something already pinned and runnable is fine."""
    install_adapter(paths, taskspindle.ADAPTER_VERSION, pin_node=True)
    launcher = paths.runtime_dir / "node_modules" / ".bin" / "claude-agent-acp"
    real = tmp_path / "elsewhere" / "claude-agent-acp-shim"
    real.parent.mkdir(parents=True)
    real.write_text(f'#!/bin/sh\nexec "{FAKE_NODE}" "entry.js" "$@"\n', encoding="utf-8")
    real.chmod(0o755)
    launcher.unlink()
    launcher.symlink_to(real)
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner)

    check = by_name(report)["adapter"]
    assert check["ok"] is True


def test_an_unpinned_launcher_is_flagged_even_at_the_right_adapter_version(
    paths: Paths, tmp_path: Path
) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION, pin_node=False)
    install_symlinked_launcher(paths)
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner)

    check = by_name(report)["adapter"]
    assert check["ok"] is False
    assert "launcher not pinned; run taskspindle setup" in check["detail"]


def test_node_falls_back_to_the_parent_env_path_when_nothing_is_pinned(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION, pin_node=False)
    found = "/usr/local/bin/node"
    monkeypatch.setattr(
        doctor.shutil, "which", lambda name, path=None: found if name == "node" else None
    )
    runner = RecordedRunner({**HEALTHY, found: (0, "v22.11.0\n")})

    check = by_name(run(paths, tmp_path, runner))["node"]

    assert check["ok"] is True
    assert check["detail"] == "v22.11.0"


def test_a_missing_codex_registration_is_only_advisory(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    register_codex(tmp_path, present=False)
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner)

    check = by_name(report)["codex_registration"]
    assert check["ok"] is False
    assert check["advisory"] is True
    assert report["ok"] is True


def test_no_grok_hooks_directory_is_a_clean_advisory(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    register_codex(tmp_path)
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner)

    check = by_name(report)["grok_sandbox_hooks"]
    assert check["ok"] is True
    assert check["advisory"] is True
    assert report["ok"] is True


def test_a_symlinked_grok_hook_source_is_flagged_as_an_advisory(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    register_codex(tmp_path)
    hooks = tmp_path / ".grok" / "hooks"
    hooks.mkdir(parents=True)
    real = tmp_path / "elsewhere-guard-bash.json"
    real.write_text("{}", encoding="utf-8")
    (hooks / "guard-bash.json").symlink_to(real)
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner)

    check = by_name(report)["grok_sandbox_hooks"]
    assert check["ok"] is False
    assert check["advisory"] is True
    assert "symlink" in check["detail"]
    assert "guard-bash.json" in check["detail"]
    # Advisory: it never fails the overall result, even though it explains a real refusal to start.
    assert report["ok"] is True


def test_a_missing_api_key_secret_is_only_advisory(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    register_codex(tmp_path)
    metered = profile("metered", auth="api_key", secret_env=("SOME_API_KEY",))
    runner = RecordedRunner()

    report = run(paths, tmp_path, runner, profiles={"metered": metered})

    checks = by_name(report)
    check = checks["profile_metered_secret_SOME_API_KEY"]
    assert check["ok"] is False
    assert check["advisory"] is True
    assert "SOME_API_KEY" in check["detail"]
    # The child env cannot be built without the secret, and that is reported the same way: a
    # machine that was never given a metered provider's key is not a broken machine.
    assert checks["child_env_metered"] == {
        "name": "child_env_metered",
        "ok": False,
        "detail": "secret not set: SOME_API_KEY",
        "advisory": True,
    }
    assert report["ok"] is True


def test_live_probes_can_be_skipped(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    monkeypatch.setattr(doctor._Doctor, "_init_probe", _fake_probe(HANDSHAKE))
    runner = RecordedRunner()

    skipped = by_name(run(paths, tmp_path, runner))
    assert "transient_unit" not in skipped
    assert "grok_acp" not in skipped
    assert "acp_shell" not in skipped
    assert all(argv[0] != "systemd-run" for argv in runner.calls)

    live = RecordedRunner()
    report = by_name(run(paths, tmp_path, live, live_probes=True))
    assert report["transient_unit"]["ok"] is True
    # No grok profile is configured, so the live handshake is reported, never attempted.
    assert report["grok_acp"]["ok"] is False
    assert any(argv[0] == "systemd-run" for argv in live.calls)
    assert all(argv[0] != "grok" or argv[1] == "--version" for argv in live.calls)


@pytest.mark.parametrize("reported", [
    "grok 1.0.13", "grok 1.0.30", "grok 1.0.30 (04b7ffed98c6)",
    "grok 1.0.31", "grok 2.0.0-beta+build", "Grok CLI v3.0.0 (new format)", "",
])
def test_grok_version_labels_do_not_gate_compatibility(paths: Paths, tmp_path: Path, reported: str) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    runner = RecordedRunner({**HEALTHY, "grok": (0, f"{reported}\n")})

    report = run(paths, tmp_path, runner)
    check = by_name(report)["grok_cli"]

    assert report["ok"] is True
    assert check["ok"] is True
    assert check["advisory"] is True
    assert "informational only" in check["detail"]
    assert "requires a live probe" in check["detail"]


@pytest.mark.parametrize("answer,compatible", [
    (HANDSHAKE, True),
    (InitInfo(load_session=False, auth_method_ids=("cached_token",), agent_info={}), False),
    (InitInfo(load_session=True, auth_method_ids=("api_key",), agent_info={}), False),
    (RuntimeError("sandbox startup failed"), False),
])
def test_updated_grok_is_judged_by_actual_capabilities(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer, compatible: bool,
) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    auth = tmp_path / ".grok/auth.json"
    auth.parent.mkdir()
    auth.touch()
    monkeypatch.setattr(doctor._Doctor, "_init_probe", _fake_probe(answer))
    runner = RecordedRunner({**HEALTHY, "/bin/sh": (0, "grok 9.0.0 preview")})

    report = run(paths, tmp_path, runner, profiles={"grok": profile("grok")}, live_probes=True)

    assert report["ok"] is compatible
    assert by_name(report)["grok_acp"]["ok"] is compatible
    assert by_name(report)["grok_acp"]["advisory"] is False
    assert ("/bin/sh", "--version") in runner.calls
    assert ("grok", "--version") not in runner.calls


def test_failed_version_diagnostic_does_not_override_working_grok(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    auth = tmp_path / ".grok/auth.json"
    auth.parent.mkdir()
    auth.touch()
    monkeypatch.setattr(doctor._Doctor, "_init_probe", _fake_probe(HANDSHAKE))
    report = run(paths, tmp_path, RecordedRunner(), profiles={"grok": profile("grok")}, live_probes=True)
    assert by_name(report)["grok_cli"]["ok"] is False
    assert by_name(report)["grok_cli"]["advisory"] is True
    assert report["ok"] is True


def test_missing_grok_executable_is_still_a_hard_failure(paths: Paths, tmp_path: Path) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    missing = str(tmp_path / "missing-grok")
    report = run(paths, tmp_path, RecordedRunner(),
                 profiles={"grok": profile("grok", command=(missing,))})
    assert by_name(report)["grok_cli"]["advisory"] is True
    assert by_name(report)["profile_grok_command"]["ok"] is False
    assert by_name(report)["profile_grok_command"]["advisory"] is False
    assert report["ok"] is False


@pytest.mark.parametrize("answer,compatible", [
    (InitInfo(load_session=False, auth_method_ids=(), agent_info={"name": "claude-adapter"}), True),
    (InitInfo(load_session=False, auth_method_ids=("api_key",), agent_info={}), True),
    (RuntimeError("adapter protocol changed"), False), (None, False),
])
def test_builtin_claude_adapter_gets_a_live_capability_check(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer, compatible: bool,
) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    auth = tmp_path / ".grok/auth.json"
    auth.parent.mkdir()
    auth.touch()

    async def init(self, selected, workspace):
        response = HANDSHAKE if selected.family == "grok" else answer
        return await _fake_probe(response)(self, selected, workspace)

    monkeypatch.setattr(doctor._Doctor, "_init_probe", init)
    report = run(paths, tmp_path, RecordedRunner(), live_probes=True,
                 profiles={"claude": profile("claude", first_class=True), "grok": profile("grok")})
    check = by_name(report)["acp_claude"]
    assert check["ok"] is compatible
    assert check["advisory"] is False
    assert report["ok"] is compatible
    if answer is None:
        assert check["detail"].startswith("protocol:")


def test_no_live_skips_claude_initialization_and_agy_catalog(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("no-live must not run this probe")

    monkeypatch.setattr(doctor._Doctor, "_init_probe", forbidden)
    monkeypatch.setattr(doctor._Doctor, "agy_oauth", forbidden)
    report = run(paths, tmp_path, RecordedRunner(), profiles={
        "claude": profile("claude", first_class=True), "agy": profile("agy", first_class=True),
    })
    assert "acp_claude" not in by_name(report)
    assert "agy_oauth" not in by_name(report)


@pytest.mark.parametrize("family", ["grok", "claude", "agy", "shell"])
async def test_initialize_uses_grok_consult_launch_and_preserves_other_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family: str,
) -> None:
    selected = profile(family, command=("/bin/sh", "agent", "acp"))
    launch_calls = []
    worker_options = []
    launch_command = doctor.providers.launch_command

    def record_launch(selected_profile, mode):
        launch_calls.append((selected_profile, mode))
        return launch_command(selected_profile, mode)

    class Worker:
        def __init__(self, **options):
            worker_options.append(options)
            self.init = HANDSHAKE

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(doctor.providers, "launch_command", record_launch)
    monkeypatch.setattr(doctor, "AcpWorker", Worker)

    assert await doctor.probe_initialize(selected, {"HOME": str(tmp_path)}, tmp_path) is HANDSHAKE

    assert len(worker_options) == 1
    if family == "grok":
        assert launch_calls == [(selected, "consult")]
        assert worker_options[0]["command"] == (
            "/bin/sh", *doctor.providers.GROK_READ_ONLY_FLAGS, "agent", "acp",
        )
        assert worker_options[0]["command"] != selected.command
    else:
        assert launch_calls == []
        assert worker_options[0]["command"] == selected.command


def test_a_configured_profile_is_probed_for_an_acp_handshake(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    monkeypatch.setattr(doctor._Doctor, "_init_probe", _fake_probe(HANDSHAKE))

    report = by_name(run(paths, tmp_path, RecordedRunner(), live_probes=True))

    assert report["acp_shell"]["ok"] is True
    assert report["acp_shell"]["detail"] == "configured: a-harness answered initialize"


def test_an_unreachable_configured_profile_is_reported_as_such(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    monkeypatch.setattr(doctor._Doctor, "_init_probe", _fake_probe(RuntimeError("no such agent")))

    report = by_name(run(paths, tmp_path, RecordedRunner(), live_probes=True))

    assert report["acp_shell"]["ok"] is False
    assert report["acp_shell"]["detail"] == "unreachable: RuntimeError: no such agent"


def test_a_profile_whose_secret_is_missing_is_not_probed(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metered = profile("metered", auth="api_key", secret_env=("SOME_API_KEY",))
    install_adapter(paths, taskspindle.ADAPTER_VERSION)
    monkeypatch.setattr(
        doctor._Doctor, "_init_probe", _fake_probe(AssertionError("must not be probed"))
    )

    report = by_name(
        run(
            paths,
            tmp_path,
            RecordedRunner(),
            live_probes=True,
            profiles={"metered": metered},
        )
    )

    assert report["acp_metered"] == {
        "name": "acp_metered",
        "ok": False,
        "detail": "secret not set: SOME_API_KEY",
        "advisory": True,
    }
