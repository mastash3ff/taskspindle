"""Native CLI provisioning and cached-login checks with no real account or model turn."""

import subprocess
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


def native_profile(paths):
    return providers.builtin_profiles(
        paths.runtime_dir, home=Path("/user"), state_dir=paths.state_dir, data_dir=paths.data_dir,
    )["agy"]


def runner_with(catalog="gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n", *, status=0):
    def run(argv, **kwargs):
        assert argv[-1] in ("--version", "models")
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 30
        assert Path(kwargs["cwd"]).is_dir()
        assert not providers.env_violations(kwargs["env"])
        assert "GEMINI_HOME" not in kwargs["env"]
        assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "1.1.26\n", "")
        return subprocess.CompletedProcess(argv, status, catalog, "private diagnostic fixture")
    return run


def test_builtin_uses_native_pin_and_existing_home_with_no_secrets(paths, environment, tmp_path):
    profile = native_profile(paths)
    assert profile.command == (str(paths.runtime_dir / "antigravity-cli/1.1.26/agy"),)
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
    }


def test_setup_copies_only_code_and_reuses_pin_after_daily_cli_changes(paths, environment):
    home = Path(environment["HOME"])
    source = executable(home / ".local/bin/agy", b"qualified build")
    companion = executable(home / ".gemini/antigravity-cli/bin/webm_encoder", b"qualified encoder")
    credential = home / ".gemini/antigravity-cli/antigravity-oauth-token"
    credential.write_bytes(b"untouched fixture")
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text("# existing\n")
    report = setup.install_agy_runtime(paths, runner=runner_with(), parent_env=environment)
    target = Path(report["command"][0])
    assert target.read_bytes() == b"qualified build"
    assert (target.parent / "bin/webm_encoder").read_bytes() == b"qualified encoder"
    assert not (target.parent / credential.name).exists()
    assert credential.read_bytes() == b"untouched fixture"
    assert paths.config_file.read_text() == "# existing\n"
    assert not (paths.data_dir / "agy-home").exists()
    assert target.stat().st_mode & 0o777 == 0o700
    source.write_bytes(b"later daily CLI")
    companion.write_bytes(b"later encoder")
    assert setup.install_agy_runtime(
        paths, runner=runner_with(), parent_env=environment,
    )["already_installed"] is True
    assert target.read_bytes() == b"qualified build"
    assert (target.parent / "bin/webm_encoder").read_bytes() == b"qualified encoder"


def test_setup_wrong_version_fails_before_installing(paths, environment):
    executable(Path(environment["HOME"]) / ".local/bin/agy")
    def wrong(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "1.1.27", "")
    with pytest.raises(setup.SetupError, match=r"exactly version 1\.1\.26"):
        setup.install_agy_runtime(paths, runner=wrong, parent_env=environment)
    assert not paths.runtime_dir.exists()


def test_setup_rejects_companion_symlink_without_partial_pin(paths, environment):
    home = Path(environment["HOME"])
    executable(home / ".local/bin/agy")
    directory = home / ".gemini/antigravity-cli/bin"
    directory.mkdir(parents=True)
    (directory / "unexpected").symlink_to(home / ".local/bin/agy")
    with pytest.raises(setup.SetupError, match="regular owned files"):
        setup.install_agy_runtime(paths, runner=runner_with(), parent_env=environment)
    assert not Path(adapter.adapter_command(paths.runtime_dir)[0]).exists()


def test_setup_rejects_runtime_symlink(paths, environment, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.runtime_dir.parent.mkdir(parents=True)
    paths.runtime_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(setup.SetupError, match="symlink"):
        setup.install_agy_runtime(paths, runner=runner_with(), parent_env=environment)
    assert list(outside.iterdir()) == []


def test_catalog_reuses_home_and_never_returns_identity_or_stderr(paths, environment):
    profile = native_profile(paths)
    executable(Path(profile.command[0]))
    evidence = adapter.agy_oauth_evidence(
        profile, environment, runner=runner_with("gemini-4-pro\tGemini 4 Pro\nclaude-x\tClaude\n"),
    )
    assert evidence == {
        "auth_method_id": "oauth-personal", "cached_credential": True,
        "model_count": 1, "protocol": "agy-cli", "version": "1.1.26",
    }


@pytest.mark.parametrize("text", ["", "not a catalog", "claude-x\tClaude\n", "gemini-4-pro\t\n",
                                  "gemini-4-pro\tPro\ngemini-4-pro\tDuplicate\n"])
def test_catalog_rejects_malformed_or_non_gemini_advertisement(text):
    with pytest.raises(ProfileError):
        adapter.parse_model_catalog(text)


def test_auth_failure_is_actionable_and_redacts_provider_output(paths, environment):
    profile = native_profile(paths)
    executable(Path(profile.command[0]))
    with pytest.raises(ProfileError, match="Run `agy` interactively") as error:
        adapter.agy_oauth_evidence(profile, environment, runner=runner_with(status=1))
    assert "private diagnostic" not in str(error.value)


def test_profile_rejects_credential_override_and_unpinned_commands(paths):
    profile = native_profile(paths)
    for invalid in [replace(profile, command=("agy",)), replace(profile, env={"HOME": "/wrong"}),
                    replace(profile, command=(profile.command[0], "--yolo")),
                    replace(profile, auth="api_key")]:
        with pytest.raises(ProfileError):
            adapter.validate_cli_profile(invalid)


def test_doctor_native_checks_never_use_acp_or_a_model_turn(paths, environment, monkeypatch):
    profile = native_profile(paths)
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
            )


def test_doctor_no_live_does_not_request_catalog(paths, monkeypatch):
    instance = doctor._Doctor(
        profiles={"agy": native_profile(paths)}, paths=paths, parent_env={},
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
    profile = native_profile(paths)
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
