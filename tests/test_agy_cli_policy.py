"""Native launch controls, checked without a model or real credentials."""

from __future__ import annotations

import importlib.util
import json
import os
import select
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest


def test_policy_module_exists() -> None:
    assert importlib.util.find_spec("taskspindle.agy_cli_policy") is not None


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    home, workspace, task = (tmp_path / name for name in ("home", "workspace", "task"))
    for directory in (home, workspace, task):
        directory.mkdir()
    cli = home / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True)
    (cli / "antigravity-oauth-token").write_text("synthetic-test-fixture")
    (home / ".gemini" / "GEMINI.md").write_text("inherited rule")
    (workspace / "src").mkdir()
    (workspace / "src" / "owned.py").write_text("before")
    (workspace / "outside.txt").write_text("before")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("protected")
    (workspace / ".agents").mkdir()
    (workspace / ".agents" / "hooks.json").write_text("inherited hook")
    return home, workspace, task


def launch(layout: tuple[Path, Path, Path], mode: str = "consult", prefixes: tuple[str, ...] = ()):
    from taskspindle.agy_cli_policy import prepare_launch

    home, workspace, task = layout
    return prepare_launch(Path(sys.executable), workspace, task, home, mode, prefixes, ("pytest",))


def test_policy_is_private_and_does_not_copy_auth(layout) -> None:
    home, _, task = layout
    argv = launch(layout)
    assert "--clearenv" in argv
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--setenv") + 1:argv.index("--setenv") + 3] == ("HOME", str(home))
    assert not any(b"synthetic-test-fixture" in p.read_bytes() for p in task.rglob("*") if p.is_file())
    metadata = json.loads((task / "agy-cli-launch.json").read_text())
    assert metadata["credential_refresh"] == "read-only; reauthenticate with native CLI if expired"
    assert metadata["verification_commands_external"] == 1


def test_native_namespace_forces_auto_updates_off_after_environment_reset(layout) -> None:
    argv = launch(layout)
    result = _run_inside(argv, "import json, os; print(json.dumps({'value': "
                         "os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE')}))")
    assert result["value"] == "true"


def test_consult_settings_deny_writes_execution_and_extensions(layout) -> None:
    _, _, task = layout
    launch(layout)
    settings = json.loads((task / "agy-cli-policy" / "settings.json").read_text())
    assert {"write_file(*)", "command(*)", "mcp(*)", "unsandboxed(*)"} <= set(
        settings["permissions"]["deny"]
    )
    agent = (task / "agy-cli-policy" / "config" / "agents" / "spindle-consult.md").read_text()
    assert "run_command" not in agent
    assert "invoke_subagent" not in agent
    assert "subagent: false" in agent


@pytest.mark.parametrize("prefix", ["../outside", "/tmp", ".git", ".agents", "missing.py"])
def test_implement_rejects_unmountable_or_protected_scope(layout, prefix) -> None:
    with pytest.raises(ValueError):
        launch(layout, "implement", (prefix,))


def test_implement_rejects_symlink_scope(layout) -> None:
    _, workspace, _ = layout
    (workspace / "alias").symlink_to(workspace / "src", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        launch(layout, "implement", ("alias",))


def test_implement_rejects_hardlinks_into_protected_files(layout) -> None:
    _, workspace, _ = layout
    (workspace / "src" / "alias").hardlink_to(workspace / ".git" / "config")
    with pytest.raises(ValueError, match="hardlink"):
        launch(layout, "implement", ("src",))


def _run_inside(argv: tuple[str, ...], code: str) -> dict:
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap unavailable")
    boundary = argv.index("--")
    proc = subprocess.run(
        [*argv[:boundary + 1], sys.executable, "-c", code],
        capture_output=True, text=True, timeout=10, check=False,
        env={**os.environ, "GEMINI_API_KEY": "must-not-reach-child"},
    )
    if "Creating new namespace failed" in proc.stderr or "No permissions to create" in proc.stderr:
        pytest.skip("kernel does not permit unprivileged mount namespaces")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("mode,prefixes", [("consult", ()), ("review", ()), ("implement", ("src",))])
def test_real_namespace_blocks_writes_outside_scope_and_hides_customizations(layout, mode, prefixes) -> None:
    home, workspace, task = layout
    argv = launch(layout, mode, prefixes)
    targets = [workspace / "src" / "owned.py", workspace / "outside.txt", workspace / ".git" / "config",
               task / "agy-cli-policy" / "settings.json"]
    code = f"""
import json, os
from pathlib import Path
result = {{}}
for name in {list(map(str, targets))!r}:
    try:
        Path(name).write_text('attempt')
        result[name] = True
    except OSError:
        result[name] = False
result['hook_hidden'] = not Path({str(workspace / '.agents' / 'hooks.json')!r}).exists()
result['global_hidden'] = not Path({str(home / '.gemini' / 'GEMINI.md')!r}).exists()
result['api_env_absent'] = 'GEMINI_API_KEY' not in os.environ
state = Path({str(home / '.gemini' / 'antigravity-cli' / 'state-probe')!r})
state.write_text('private')
result['state_writable'] = state.exists()
print(json.dumps(result))
"""
    result = _run_inside(argv, code)
    assert result[str(targets[0])] is (mode == "implement")
    assert all(result[str(p)] is False for p in targets[1:])
    assert result["hook_hidden"] and result["global_hidden"] and result["api_env_absent"]
    assert result["state_writable"]
    assert not (home / ".gemini" / "antigravity-cli" / "state-probe").exists()


def test_continuation_preserves_private_state_but_rewrites_controls(layout) -> None:
    _, _, task = layout
    launch(layout)
    state = task / "agy-cli-state" / "cli" / "conversation-state"
    state.write_text("preserve")
    settings = task / "agy-cli-policy" / "settings.json"
    settings.write_text("untrusted")
    launch(layout)
    assert state.read_text() == "preserve"
    assert "command(*)" in json.loads(settings.read_text())["permissions"]["deny"]


def test_full_workspace_scope_keeps_git_controls_and_native_token_read_only(layout) -> None:
    home, workspace, task = layout
    token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    inode = token.stat().st_ino
    argv = launch(layout, "implement", (".",))
    protected = [
        str(workspace / ".git" / "config"), str(token), str(task / "agy-cli-policy" / "settings.json"),
    ]
    code = f"""
import json
from pathlib import Path
result = {{}}
for name in {protected!r}:
    try:
        with Path(name).open('a'):
            pass
        result[name] = True
    except OSError:
        result[name] = False
result['same_token_inode'] = Path({str(token)!r}).stat().st_ino == {inode}
Path({str(workspace / 'new-file.txt')!r}).write_text('new implementation file')
print(json.dumps(result))
"""
    result = _run_inside(argv, code)
    assert result.pop("same_token_inode")
    assert not any(result.values())
    assert (workspace / "new-file.txt").exists()


def test_native_child_stays_in_transport_process_group(layout) -> None:
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap unavailable")
    argv = launch(layout)
    command = [*argv[:argv.index("--") + 1], sys.executable, "-u", "-c",
               "import time; print('ready', flush=True); time.sleep(30)"]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 5)[0], "sandbox child did not start"
        ready = process.stdout.readline().strip()
        if not ready:
            _, stderr = process.communicate(timeout=3)
            if "Creating new namespace failed" in stderr or "No permissions to create" in stderr:
                pytest.skip("kernel does not permit unprivileged mount namespaces")
            pytest.fail(stderr)
        assert ready == "ready"
        pending = [process.pid]
        children = []
        while pending:
            parent = pending.pop()
            path = Path(f"/proc/{parent}/task/{parent}/children")
            if path.exists():
                found = [int(value) for value in path.read_text().split()]
                children.extend(found)
                pending.extend(found)
        native = [pid for pid in children
                  if Path(f"/proc/{pid}/exe").resolve() == Path(sys.executable).resolve()]
        assert len(native) == 1
        assert os.getpgid(native[0]) == process.pid
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=3)


def test_native_overage_policy_controls_only_credit_setting(layout) -> None:
    from taskspindle.agy_cli_policy import prepare_launch

    home, workspace, task = layout
    for policy, credits in (("observe_only", False), ("provider_managed", True)):
        argv = prepare_launch(Path(sys.executable), workspace, task, home, "consult", (), (),
                              native_overage=policy)
        settings = json.loads((task / "agy-cli-policy" / "settings.json").read_text())
        assert settings["useG1Credits"] is credits
        assert settings["enableTerminalSandbox"] is True
        assert settings["allowNonWorkspaceAccess"] is False
        assert "--unshare-pid" in argv
        assert "write_file(*)" in settings["permissions"]["deny"]
