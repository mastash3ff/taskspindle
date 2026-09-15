"""Native CLI resolution and cached-login checks with no real account or model turn."""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from taskspindle import agy_cli_adapter as adapter
from taskspindle import cli, discover, doctor, providers, setup
from taskspindle.config import Paths
from taskspindle.providers import ProfileError


@pytest.fixture
def paths(tmp_path):
    return Paths(
        config_file=tmp_path / "config/config.toml", state_dir=tmp_path / "state",
        data_dir=tmp_path / "data", runtime_dir=tmp_path / "data/runtimes/test",
    )


@pytest.fixture
def environment(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    token = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_bytes(b"synthetic native login fixture")
    return {
        "HOME": str(home), "USER": "test-user", "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "GOOGLE_API_KEY": "forbidden fixture", "GEMINI_API_KEY": "forbidden fixture",
        "OPENAI_API_KEY": "forbidden fixture", "HTTPS_PROXY": "forbidden fixture",
        "GEMINI_HOME": "/wrong/home",
    }


def executable(path, payload=b"fixture executable"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o700)
    return path


def native_profile(paths, environment):
    """The built-in ``agy`` profile, resolved against the fixture's ``PATH``/``HOME``.

    ``environment["PATH"]`` never has an ``agy`` on it, so this always resolves to the
    conventional ``~/.local/bin/agy`` fallback -- deterministic regardless of what is actually
    installed on the machine running the test.
    """
    home = Path(environment["HOME"])
    return providers.builtin_profiles(
        paths.runtime_dir, home=home, state_dir=paths.state_dir, data_dir=paths.data_dir,
        parent_env=environment,
    )["agy"]


def runner_with(
    catalog="gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n", *, status=0, version="1.1.26",
):
    def run(argv, **kwargs):
        assert argv[-1] in ("--version", "models")
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 30
        assert Path(kwargs["cwd"]).is_dir()
        assert not providers.env_violations(kwargs["env"])
        assert "GEMINI_HOME" not in kwargs["env"]
        assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, f"{version}\n", "")
        return subprocess.CompletedProcess(argv, status, catalog, "private diagnostic fixture")
    return run


def test_builtin_resolves_the_installed_cli_and_existing_home_with_no_secrets(paths, environment, tmp_path):
    profile = native_profile(paths, environment)
    assert profile.command == (str(Path(environment["HOME"]) / ".local/bin/agy"),)
    assert profile.env == {}
    assert profile.model is None
    env = providers.build_child_env(profile, environment, task_tmp=tmp_path)
    assert env["HOME"] == environment["HOME"]
    assert env["DBUS_SESSION_BUS_ADDRESS"] == environment["DBUS_SESSION_BUS_ADDRESS"]
    assert env["XDG_RUNTIME_DIR"] == environment["XDG_RUNTIME_DIR"]
    assert "GEMINI_API_KEY" not in env
    assert "GEMINI_HOME" not in env
    assert providers.adapter_metadata(profile) == {
        "protocol": "agy-cli", "package": "antigravity-cli", "version": "1.1.26",
        "min_version": "1.1.26", "tested_up_to": "1.2.3",
    }


def test_builtin_prefers_an_explicit_source_override_and_then_path(paths, environment, tmp_path):
    on_path = executable(tmp_path / "on-path/agy")
    environment["TASKSPINDLE_AGY_SOURCE"] = str(tmp_path / "explicit/agy")
    environment["PATH"] = f"{on_path.parent}:/usr/bin:/bin"
    profile = native_profile(paths, environment)
    assert profile.command == (str(tmp_path / "explicit/agy"),)

    del environment["TASKSPINDLE_AGY_SOURCE"]
    profile = native_profile(paths, environment)
    assert profile.command == (str(on_path),)


@pytest.mark.parametrize("provider", ["agy", "agy-alias"])
def test_native_child_environment_forces_the_qualified_build_to_skip_updates(
    paths, environment, tmp_path, provider,
):
    profile = native_profile(paths, environment)
    if provider != "agy":
        profile = replace(profile, id=provider, base="agy", first_class=False)
    environment["AGY_CLI_DISABLE_AUTO_UPDATE"] = "false"
    env = providers.build_child_env(profile, environment, task_tmp=tmp_path)
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


def test_version_probe_leaves_the_resolved_binary_unmodified_by_a_running_updater(paths, environment):
    binary = Path(native_profile(paths, environment).command[0])
    body = (f"#!{sys.executable}\n"
            "import os\nfrom pathlib import Path\n"
            "if os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE') != 'true':\n"
            "    Path(__file__).write_text('self-updated binary')\n"
            "print('1.1.26')\n").encode()
    executable(binary, body)
    assert adapter.verify_cli_version(binary, parent_env=environment) == "1.1.26"
    assert binary.read_bytes() == body


@pytest.mark.parametrize("reported", ["1.1.26", "1.1.30", "1.2.3"])
def test_verify_cli_version_accepts_the_minimum_and_the_tested_band(paths, environment, reported):
    binary = executable(Path(environment["HOME"]) / ".local/bin/agy")

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, f"{reported}\n", "")

    assert adapter.verify_cli_version(binary, runner=run, parent_env=environment) == reported
    assert adapter.newer_than_tested(reported) is False


def test_verify_cli_version_accepts_a_build_newer_than_tested_as_advisory(paths, environment):
    binary = executable(Path(environment["HOME"]) / ".local/bin/agy")

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "1.4.0\n", "")

    assert adapter.verify_cli_version(binary, runner=run, parent_env=environment) == "1.4.0"
    assert adapter.newer_than_tested("1.4.0") is True


def test_verify_cli_version_refuses_a_build_below_the_minimum(paths, environment):
    binary = executable(Path(environment["HOME"]) / ".local/bin/agy")

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "1.1.25\n", "")

    with pytest.raises(ProfileError, match=r"older than the required minimum 1\.1\.26"):
        adapter.verify_cli_version(binary, runner=run, parent_env=environment)


def test_verify_cli_version_refuses_an_unparsable_report(paths, environment):
    binary = executable(Path(environment["HOME"]) / ".local/bin/agy")

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "agy version one-point-one\n", "")

    with pytest.raises(ProfileError, match="unparsable version"):
        adapter.verify_cli_version(binary, runner=run, parent_env=environment)


def test_setup_confirms_the_path_binary_without_copying_it(paths, environment):
    home = Path(environment["HOME"])
    source = executable(home / ".local/bin/agy", b"qualified build")
    credential = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    credential.write_bytes(b"untouched fixture")
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text("# existing\n")

    report = setup.install_agy_runtime(paths, runner=runner_with(), parent_env=environment)

    assert report["command"] == [str(source)]
    assert report["adapter_version"] == "1.1.26"
    assert report["advisory"] is None
    assert source.read_bytes() == b"qualified build"
    assert credential.read_bytes() == b"untouched fixture"
    assert paths.config_file.read_text() == "# existing\n"
    assert not (paths.runtime_dir / "antigravity-cli").exists()

    # A later daily-CLI update is picked up on the very next call: there is nothing pinned to
    # reuse or bypass.
    source.write_bytes(b"later daily CLI")
    calls = []

    def updated_daily_runner(argv, **kwargs):
        assert Path(argv[0]) == source
        calls.append(tuple(argv))
        return runner_with()(argv, **kwargs)

    report_again = setup.install_agy_runtime(paths, runner=updated_daily_runner, parent_env=environment)
    assert report_again["command"] == [str(source)]
    assert calls == [(str(source), "--version")]
    assert source.read_bytes() == b"later daily CLI"


def test_setup_accepts_a_build_newer_than_tested_as_advisory(paths, environment):
    executable(Path(environment["HOME"]) / ".local/bin/agy")
    report = setup.install_agy_runtime(paths, runner=runner_with(version="1.4.0"), parent_env=environment)
    assert report["adapter_version"] == "1.4.0"
    assert report["tested_up_to"] == "1.2.3"
    assert report["advisory"] is not None
    assert "1.4.0" in report["advisory"]
    assert "1.2.3" in report["advisory"]


def test_setup_below_minimum_version_fails_before_touching_anything(paths, environment):
    executable(Path(environment["HOME"]) / ".local/bin/agy")

    def wrong(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "1.1.25", "")

    with pytest.raises(setup.SetupError, match=r"older than the required minimum 1\.1\.26"):
        setup.install_agy_runtime(paths, runner=wrong, parent_env=environment)
    assert not paths.runtime_dir.exists()


def test_setup_rejects_runtime_symlink(paths, environment, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.runtime_dir.parent.mkdir(parents=True)
    paths.runtime_dir.symlink_to(outside, target_is_directory=True)
    executable(Path(environment["HOME"]) / ".local/bin/agy")
    with pytest.raises(setup.SetupError, match="symlink"):
        setup.install_agy_runtime(paths, runner=runner_with(), parent_env=environment)
    assert list(outside.iterdir()) == []


def test_catalog_reuses_home_and_never_returns_identity_or_stderr(paths, environment):
    profile = native_profile(paths, environment)
    executable(Path(profile.command[0]))
    evidence = adapter.agy_oauth_evidence(
        profile, environment, runner=runner_with("gemini-4-pro\tGemini 4 Pro\nclaude-x\tClaude\n"),
    )
    assert evidence == {
        "auth_method_id": "oauth-personal", "cached_credential": True,
        "model_count": 1, "protocol": "agy-cli", "version": "1.1.26",
        "model_ids": ["gemini-4-pro", "claude-x"],
    }


def test_catalog_flags_a_build_newer_than_tested_as_advisory(paths, environment):
    profile = native_profile(paths, environment)
    executable(Path(profile.command[0]))
    evidence = adapter.agy_oauth_evidence(profile, environment, runner=runner_with(version="1.4.0"))
    assert evidence["version"] == "1.4.0"
    assert "1.4.0" in evidence["version_advisory"]
    assert "1.2.3" in evidence["version_advisory"]


@pytest.mark.parametrize("text", ["", "not a catalog", "claude-x\tClaude\n", "gemini-4-pro\t\n",
                                  "gemini-4-pro\tPro\ngemini-4-pro\tDuplicate\n"])
def test_catalog_rejects_malformed_or_non_gemini_advertisement(text):
    with pytest.raises(ProfileError):
        adapter.parse_model_catalog(text)


def test_auth_failure_is_actionable_and_redacts_provider_output(paths, environment):
    profile = native_profile(paths, environment)
    executable(Path(profile.command[0]))
    with pytest.raises(ProfileError, match="Run `agy` interactively") as error:
        adapter.agy_oauth_evidence(profile, environment, runner=runner_with(status=1))
    assert "private diagnostic" not in str(error.value)


def test_profile_rejects_credential_override_and_unpinned_commands(paths, environment):
    profile = native_profile(paths, environment)
    for invalid in [replace(profile, command=("agy",)), replace(profile, env={"HOME": "/wrong"}),
                    replace(profile, command=(profile.command[0], "--yolo")),
                    replace(profile, auth="api_key")]:
        with pytest.raises(ProfileError):
            adapter.validate_cli_profile(invalid)


def test_doctor_native_checks_never_use_acp_or_a_model_turn(paths, environment, monkeypatch):
    profile = native_profile(paths, environment)
    executable(Path(profile.command[0]))
    instance = doctor._Doctor(
        profiles={"agy": profile}, paths=paths, parent_env=environment,
        live_probes=True, runner=runner_with(),
    )
    monkeypatch.setattr(instance, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "bwrap 0.11", ""))
    monkeypatch.setattr(doctor.shutil, "which", lambda *a, **k: "/usr/bin/bwrap")
    instance.agy_cli()
    instance.agy_oauth()
    assert [(check.name, check.ok) for check in instance.checks] == [
        ("agy_cli", True), ("agy_sandbox", True), ("agy_oauth", True),
    ]


def test_doctor_agy_cli_reports_a_build_newer_than_tested_as_advisory_not_failure(
    paths, environment, monkeypatch,
):
    profile = native_profile(paths, environment)
    executable(Path(profile.command[0]))
    instance = doctor._Doctor(
        profiles={"agy": profile}, paths=paths, parent_env=environment,
        live_probes=True, runner=runner_with(version="1.4.0"),
    )
    instance.agy_cli()
    check = next(c for c in instance.checks if c.name == "agy_cli")
    assert check.ok is True
    assert check.advisory is False
    assert "1.4.0" in check.detail
    assert "newer than tested" in check.detail


def test_cli_auth_missing_login_never_opens_browser_or_store(paths, monkeypatch, capsys):
    from taskspindle.store import Store

    monkeypatch.setattr(cli, "resolve_paths", lambda *args: paths)
    monkeypatch.setattr(Store, "open", lambda *a: pytest.fail("auth touched the store"))
    def missing(*args, **kwargs):
        raise ProfileError("OAUTH_REJECTED", "Run `agy` interactively to sign in")
    monkeypatch.setattr(adapter, "agy_oauth_evidence", missing)
    assert cli.main(["auth", "agy"]) == 1
    assert "Run `agy` interactively" in capsys.readouterr().err


def test_discovery_recognizes_native_cli_without_proposing_an_acp_alias(tmp_path):
    binary = executable(tmp_path / "bin/agy")
    executable(tmp_path / "bin/agy_acp_server.par")
    legacy = discover.RegistryAgent(
        id="antigravity-acp", name="ACP", description="", candidates=(("agy_acp_server.par", ()),),
    )
    found = discover.detect([legacy], env={"HOME": str(tmp_path), "PATH": str(binary.parent)})
    assert len(found) == 1
    assert found[0].agent.id == "antigravity-cli"
    assert discover.first_class_match(found[0].agent) == "agy"
    assert "[providers." not in discover.proposal(found[0])
    assert "setup --provider agy" in discover.proposal(found[0])


def test_native_alias_cannot_replace_qualified_command_or_home(paths, environment):
    for override in [{"command": ["agy"]}, {"env": {"HOME": "/other"}}]:
        with pytest.raises(ProfileError, match="retain the pinned native"):
            providers.load_profiles(
                {"providers": {"other-agy": {"base": "agy", "auth": "oauth", **override}}},
                runtime_dir=paths.runtime_dir, home=Path(environment["HOME"]), state_dir=paths.state_dir,
                parent_env=environment,
            )


def test_doctor_no_live_does_not_request_catalog(paths, environment, monkeypatch):
    instance = doctor._Doctor(
        profiles={"agy": native_profile(paths, environment)}, paths=paths, parent_env={},
        live_probes=False, runner=lambda *a, **k: pytest.fail("unexpected command"),
    )
    for method in ("git", "systemd_user", "node", "adapter", "grok_cli", "claude_oauth",
                   "agy_cli", "child_envs", "codex_registration", "profile_commands",
                   "provider_availability"):
        monkeypatch.setattr(instance, method, lambda: None)
    monkeypatch.setattr(instance, "agy_oauth", lambda: pytest.fail("no-live requested a catalog"))
    instance._collect_blocking([])


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory"])
@pytest.mark.parametrize("surface", ["auth", "doctor"])
def test_preflight_requires_worker_token_file_before_any_command(
    paths, environment, tmp_path, kind, surface,
):
    profile = native_profile(paths, environment)
    executable(Path(profile.command[0]))
    token = Path(environment["HOME"]) / ".gemini/antigravity-cli/antigravity-oauth-token"
    token.unlink()
    if kind == "symlink":
        other = tmp_path / "other-token-fixture"
        other.write_bytes(b"synthetic")
        token.symlink_to(other)
    elif kind == "directory":
        token.mkdir()

    def forbidden(*args, **kwargs):
        pytest.fail("missing native token must fail before a CLI or model command")

    if surface == "auth":
        with pytest.raises(ProfileError, match="Run `agy` interactively") as error:
            adapter.agy_oauth_evidence(profile, environment, runner=forbidden)
        assert error.value.code == "OAUTH_REJECTED"
    else:
        instance = doctor._Doctor(
            profiles={"agy": profile}, paths=paths, parent_env=environment,
            live_probes=True, runner=forbidden,
        )
        instance.agy_oauth()
        assert instance.checks[-1].ok is False
        assert "Run `agy` interactively" in instance.checks[-1].detail


def test_native_token_predicate_never_reads_credentials(environment, monkeypatch):
    home = Path(environment["HOME"])
    token = home / ".gemini/antigravity-cli/antigravity-oauth-token"

    def forbidden(*args, **kwargs):
        pytest.fail("credential contents were read")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert adapter.require_cached_token(home) == token
