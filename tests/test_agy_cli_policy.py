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
    assert ("--ro-bind", str(Path(sys.executable).resolve()), str(Path(sys.executable).resolve())) in (
        list(zip(argv, argv[1:], argv[2:], strict=False))
    )
    assert "--clearenv" in argv
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--setenv") + 1:argv.index("--setenv") + 3] == ("HOME", str(home))
    assert not any(b"synthetic-test-fixture" in p.read_bytes() for p in task.rglob("*") if p.is_file())
    metadata = json.loads((task / "agy-cli-launch.json").read_text())
    assert metadata["credential_refresh"] == "read-only; reauthenticate with native CLI if expired"
    assert metadata["verification_commands_external"] == 1


def test_launch_binds_the_real_companion_directory_read_only(layout) -> None:
    """The resolved binary is no longer bundled with a copy of its companions: TaskSpindle binds
    whatever is really at ``~/.gemini/antigravity-cli/bin``, the same place an interactive login
    already put it, not something derived from the binary's own (now PATH-resolved) location."""
    home, _, task = layout
    companions = home / ".gemini" / "antigravity-cli" / "bin"
    companions.mkdir(parents=True)
    (companions / "webm_encoder").write_bytes(b"companion fixture")
    argv = launch(layout)
    index = argv.index(str(companions))
    assert argv[index - 1] == "--ro-bind"
    assert argv[index + 1] == str(home / ".gemini" / "antigravity-cli" / "bin")
    metadata = json.loads((task / "agy-cli-launch.json").read_text())
    assert metadata["companion_bin"] == str(companions)


def test_launch_rejects_a_symlinked_companion_directory(layout) -> None:
    home, _, _ = layout
    real = home.parent / "elsewhere-bin"
    real.mkdir()
    (home / ".gemini" / "antigravity-cli" / "bin").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="companion executable directory must not be a symlink"):
        launch(layout)


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
    assert settings["useG1Credits"] is False
    assert settings["enableTerminalSandbox"] is True
    assert settings["allowNonWorkspaceAccess"] is False
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
    boundary = argv.index("/taskspindle-mounts.json", argv.index("--"))
    proc = subprocess.run(
        [*argv[:boundary + 1], str(Path(sys.executable).resolve()), "-c", code],
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
result['git_hidden'] = not Path({str(workspace / '.git' / 'config')!r}).exists()
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
    assert result["git_hidden"]
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


def test_full_workspace_scope_masks_git_and_keeps_native_token_read_only(layout) -> None:
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


@pytest.mark.parametrize("mode,prefixes", [("consult", ()), ("implement", ("src",))])
def test_git_directory_is_masked_empty(layout, mode, prefixes) -> None:
    _, workspace, task = layout
    argv = launch(layout, mode, prefixes)
    triples = list(zip(argv, argv[1:], argv[2:], strict=False))
    assert ("--ro-bind", str(task / "agy-cli-policy" / "empty"), str(workspace / ".git")) in triples
    assert not any(t[0] == "--ro-bind" and t[1] == str(workspace / ".git") for t in triples)
    assert json.loads((task / "agy-cli-launch.json").read_text())["git_masked"] is True


@pytest.mark.parametrize("mode,prefixes", [("consult", ()), ("implement", ("src",)), ("implement", (".",))])
def test_git_pointer_file_is_masked_blank(layout, mode, prefixes) -> None:
    """A linked worktree's ``.git`` names the main repository's gitdir; the model never sees it."""
    _, workspace, task = layout
    shutil.rmtree(workspace / ".git")
    (workspace / ".git").write_text("gitdir: /elsewhere/.git/worktrees/ts_x\n")
    argv = launch(layout, mode, prefixes)
    triples = list(zip(argv, argv[1:], argv[2:], strict=False))
    assert ("--ro-bind", str(task / "agy-cli-policy" / "empty-file"), str(workspace / ".git")) in triples
    code = f"""
import json
from pathlib import Path
git = Path({str(workspace / '.git')!r})
print(json.dumps({{"text": git.read_text(), "is_file": git.is_file()}}))
"""
    assert _run_inside(argv, code) == {"text": "", "is_file": True}
    assert (workspace / ".git").read_text().startswith("gitdir:")


def test_native_child_stays_in_transport_process_group(layout) -> None:
    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap unavailable")
    argv = launch(layout)
    command = [*argv[:argv.index("/taskspindle-mounts.json", argv.index("--")) + 1],
               str(Path(sys.executable).resolve()), "-u", "-c",
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


def test_smoke_uses_only_explicit_runtime_and_synthetic_home(layout, monkeypatch):
    from taskspindle.agy_cli_policy import smoke_launch

    home, _, task = layout
    (home / 'private-secret').write_text('never expose')
    (task / 'private-secret').write_text('never expose')
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/run/user/private')
    monkeypatch.setenv('XDG_RUNTIME_DIR', '/run/user/private')
    argv = launch(layout)
    triples = list(zip(argv, argv[1:], argv[2:], strict=False))
    for source in ('/', str(home), str(task)):
        assert ('--ro-bind', source, source) not in triples
    assert 'DBUS_SESSION_BUS_ADDRESS' not in argv
    assert 'XDG_RUNTIME_DIR' not in argv
    result = smoke_launch(argv)
    assert result['status'] == 'ok'
    assert result['elapsed_seconds'] < 10
    assert result['ca_certificates'] > 0 and result['localhost']
    assert result['mount_count'] < 100
    result = _run_inside(argv, f"import json; from pathlib import Path; print(json.dumps("
                         f"{{'hidden':not Path({str(home / 'private-secret')!r}).exists() "
                         f"and not Path({str(task / 'private-secret')!r}).exists() "
                         "and not Path('/mnt/wsl').exists() and not Path('/run/user').exists()}))")
    assert result['hidden']


@pytest.mark.parametrize('location', ['workspace', 'runtime', 'state', 'companions'])
def test_rejects_nested_mounts_in_every_retained_directory(layout, monkeypatch, location):
    from taskspindle import agy_namespace

    home, workspace, task = layout
    roots = {'workspace': workspace, 'runtime': Path('/usr/bin'),
             'state': task / 'agy-cli-state' / 'cli',
             'companions': home / '.gemini' / 'antigravity-cli' / 'bin'}
    roots['companions'].mkdir()
    original = agy_namespace.mount_table()
    original[roots[location] / 'injected'] = {'rw'}
    # A plain map deliberately exercises a fresh inventory, not the cached index.
    monkeypatch.setattr(agy_namespace, 'mount_table', lambda: dict(original))
    match = '64 explicit mounts' if location == 'runtime' else 'nested mounts'
    with pytest.raises(ValueError, match=match):
        launch(layout)


def test_runtime_descendant_count_is_not_authorization(tmp_path):
    from taskspindle.agy_namespace import runtime_sources

    root = tmp_path / 'runtime'
    root.mkdir()
    (root / 'ordinary').mkdir()
    (root / 'unapproved').mkdir()
    assert runtime_sources(root, {root / 'unapproved': {'ro'}}) == [root / 'ordinary']
    with pytest.raises(ValueError, match='64 descendants'):
        runtime_sources(root, {root / str(i): {'ro'} for i in range(65)})


def test_final_namespace_validation_rejects_a_mount_added_after_planning(layout):
    from taskspindle.agy_cli_policy import smoke_launch

    _, workspace, task = layout
    argv = list(launch(layout))
    index = argv.index('--clearenv')
    # Emulate a source gaining a nested mount after host preflight, before exec.
    argv[index:index] = ['--ro-bind', str(task / 'agy-cli-policy' / 'empty'), str(workspace / 'src')]
    with pytest.raises(ValueError, match='unapproved mount'):
        smoke_launch(argv)


def test_final_namespace_validation_rejects_replaced_source(layout):
    from taskspindle.agy_cli_policy import smoke_launch

    _, workspace, _ = layout
    argv = launch(layout)
    workspace.rename(workspace.with_name('old-workspace'))
    workspace.mkdir()
    (workspace / '.git').mkdir()
    (workspace / '.agents').mkdir()
    with pytest.raises(ValueError, match='identity changed'):
        smoke_launch(argv)


def test_final_namespace_validation_rejects_writable_controls(layout):
    from taskspindle.agy_cli_policy import smoke_launch

    _, _, task = layout
    argv = list(launch(layout))
    index = argv.index(str(task / 'agy-cli-policy' / 'settings.json'))
    assert argv[index - 1] == '--ro-bind'
    argv[index - 1] = '--bind'
    with pytest.raises(ValueError, match='mount is writable'):
        smoke_launch(argv)


@pytest.mark.parametrize('destination,message', [
    ('/unapproved', 'unapproved mount'), ('/dev/null', 'private device changed'),
])
def test_final_namespace_validation_rejects_unapproved_root_mount(layout, destination, message):
    from taskspindle.agy_cli_policy import smoke_launch

    _, _, task = layout
    secret = task / 'synthetic-secret'
    secret.write_text('must stay hidden')
    argv = list(launch(layout))
    index = argv.index('--clearenv')
    argv[index:index] = ['--ro-bind', str(secret), destination]
    with pytest.raises(ValueError, match=message):
        smoke_launch(argv)


def test_final_namespace_validation_rejects_late_hardlink(layout):
    from taskspindle.agy_cli_policy import smoke_launch

    _, workspace, _ = layout
    argv = launch(layout, 'implement', ('src',))
    (workspace / 'src' / 'late-link').hardlink_to(workspace / 'outside.txt')
    with pytest.raises(ValueError, match='gained a hardlink'):
        smoke_launch(argv)
    assert (workspace / 'outside.txt').read_text() == 'before'
