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
