"""Isolation and protocol tests for the subscription browser process bridge."""

from __future__ import annotations

import base64
import itertools
import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskspindle.config import Paths
from taskspindle.subscriptions import runtime

ACCOUNT = "a" * 64


def paths(tmp_path: Path) -> Paths:
    node = tmp_path / "node"
    chrome = tmp_path / "chrome"
    node.touch(mode=0o700)
    chrome.touch(mode=0o700)
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[subscriptions]",
                'platform = "native"',
                'browser_mode = "dedicated"',
                'timezone = "America/Chicago"',
                f'node_path = "{node}"',
                f'chrome_path = "{chrome}"',
                f'local_app_data = "{tmp_path / "local"}"',
            ]
        ),
        encoding="utf-8",
    )
    return Paths(config, tmp_path / "state", tmp_path / "data", tmp_path / "adapter")


def prepare_runtime(paths: Paths) -> Path:
    layout = runtime._layout(paths, runtime._load_settings(paths))
    layout.runtime.mkdir(parents=True)
    (layout.runtime / "helper.mjs").write_text("// helper", encoding="utf-8")
    (layout.runtime / "node_modules" / "playwright-core").mkdir(parents=True)
    return layout.runtime


def normal_paths(tmp_path: Path, *, token: str | None = None, profile: str = "Default") -> Paths:
    configured = paths(tmp_path)
    configured.config_file.write_text(
        "\n".join(
            [
                "[subscriptions]",
                'platform = "windows"',
                'browser_mode = "normal"',
                f'chrome_profile = "{profile}"',
                'timezone = "America/Chicago"',
                f'node_path = "{tmp_path / "node"}"',
                f'chrome_path = "{tmp_path / "chrome"}"',
                f'local_app_data = "{tmp_path / "local"}"',
            ]
        ),
        encoding="utf-8",
    )
    if token is not None:
        token_path = configured.data_dir / "subscriptions/extension-token"
        token_path.parent.mkdir(parents=True)
        token_path.write_text(token, encoding="ascii")
        token_path.chmod(0o600)
    return configured


def stub_normal_runtime(
    configured: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> runtime._Layout:
    root = tmp_path / "normal-root"
    layout = runtime._Layout(
        mode="windows",
        root=root,
        runtime=root / "runtime",
        profiles=root / "profiles",
        node=tmp_path / "node",
        chrome=tmp_path / "chrome",
        request_runtime=r"C:\TaskSpindleTest\runtime",
        request_profiles=r"C:\TaskSpindleTest\profiles",
        request_chrome=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    layout.runtime.mkdir(parents=True)
    (layout.runtime / "normal-helper.mjs").write_text("// normal helper", encoding="utf-8")
    (layout.runtime / "node_modules/playwright-core").mkdir(parents=True)
    monkeypatch.setattr(runtime, "_layout", lambda _paths, _settings: layout)
    return layout


def valid_observation() -> dict[str, object]:
    return {
        "provider": "chatgpt",
        "account_id": ACCOUNT,
        "account_label": "b***@e***.com",
        "billing_channel": "provider_web",
        "plan": "Plus",
        "status": "renewing",
        "renews_at": "2026-10-07",
        "access_ends_at": None,
        "date_precision": "date",
        "timezone": "America/Chicago",
        "source_url": "https://chatgpt.com/",
        "collector_version": "1",
    }


class Process:
    def __init__(self, stdout: str, *, timeout: bool = False) -> None:
        self.stdout = stdout
        self.timeout = timeout
        self.returncode = 0
        self.pid = 4321
        self.input: str | None = None

    def communicate(self, value: str, timeout: float) -> tuple[str, None]:
        self.input = value
        if self.timeout:
            runtime._request_browser_stop()
            raise subprocess.TimeoutExpired("node", timeout)
        return self.stdout, None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        return self.returncode


class HangingProcess(Process):
    def __init__(self) -> None:
        super().__init__("")
        self.returncode = None

    def wait(self, timeout: float) -> int:
        raise subprocess.TimeoutExpired("node", timeout)


def stub_node_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "v24.14.0\n", ""),
    )


def test_run_browser_sends_only_normalized_request_and_safe_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = paths(tmp_path)
    prepare_runtime(configured)
    stub_node_check(monkeypatch)
    process = Process(json.dumps({"ok": True, "observation": valid_observation()}))
    captured: dict[str, object] = {}

    def popen(command: list[str], **kwargs: object) -> Process:
        captured.update(command=command, **kwargs)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-forward")

    result = runtime.run_browser(configured, "chatgpt", "connect")

    assert result["ok"] is True
    request = json.loads(process.input or "")
    operation_nonce = request.pop("operation_nonce")
    uuid.UUID(operation_nonce)
    assert request == {
        "provider": "chatgpt",
        "action": "connect",
        "profile_dir": str(tmp_path / "local/TaskSpindle/subscriptions/profiles/chatgpt"),
        "chrome_path": str(tmp_path / "chrome"),
        "expected_account_id": None,
        "timeout_s": 600,
        "timezone": "America/Chicago",
    }
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert captured["stderr"] is subprocess.DEVNULL
    assert captured["start_new_session"] is True


def test_invalid_stdout_is_replaced_with_safe_protocol_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = paths(tmp_path)
    prepare_runtime(configured)
    stub_node_check(monkeypatch)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_args, **_kwargs: Process("raw failure"))

    assert runtime.run_browser(configured, "chatgpt", "connect") == {
        "ok": False,
        "error": {
            "code": "COLLECTOR_PROTOCOL_ERROR",
            "message": "The subscription collector returned an invalid response.",
        },
    }


def test_normal_connect_opens_regular_chrome_then_reports_missing_extension_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = normal_paths(tmp_path)
    stub_normal_runtime(configured, tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stdout = "v24.14.0\n" if command[-1] == "--version" else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(
        runtime.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("missing token launched helper"),
    )

    result = runtime.run_browser(configured, "chatgpt", "connect")

    assert result["error"]["code"] == "SETUP_REQUIRED"
    assert len(calls) == 2
    launch_script = base64.b64decode(calls[1][-1]).decode("utf-16-le")
    assert "Start-Process" in launch_script
    assert "--profile-directory=Default" in launch_script
    assert "https://chatgpt.com/#settings/Billing" in launch_script
    assert "user-data-dir" not in launch_script
    assert "remote-debug" not in launch_script


def test_normal_refresh_without_token_fails_before_any_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = normal_paths(tmp_path)
    stub_normal_runtime(configured, tmp_path, monkeypatch)
    stub_node_check(monkeypatch)
    monkeypatch.setattr(
        runtime.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("refresh launched a process without a token"),
    )

    result = runtime.run_browser(configured, "chatgpt", "refresh", ACCOUNT)

    assert result["error"]["code"] == "SETUP_REQUIRED"


def test_normal_chrome_profile_with_space_is_one_windows_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = normal_paths(tmp_path, profile="Profile 1")
    layout = stub_normal_runtime(configured, tmp_path, monkeypatch)
    settings = runtime._load_settings(configured)
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    runtime._launch_normal_chrome(
        layout, settings, "chatgpt", billing_url=True
    )
    script = base64.b64decode(calls[0][-1]).decode("utf-16-le")
    assert '\"--profile-directory=Profile 1\"' in script
    assert "-ArgumentList @(" not in script


def test_normal_helper_receives_only_private_extension_token_as_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = normal_paths(tmp_path, token="extension-token-123")
    stub_normal_runtime(configured, tmp_path, monkeypatch)
    stub_node_check(monkeypatch)
    helper = Process(json.dumps({"ok": True, "observation": valid_observation()}))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(command: list[str], **kwargs: object) -> Process:
        calls.append((command, kwargs))
        return helper

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")

    assert runtime.run_browser(configured, "chatgpt", "connect")["ok"] is True
    assert len(calls) == 1
    helper_command, helper_options = calls[0]
    assert helper_command[-1].endswith("normal-helper.mjs")
    assert helper_options["env"]["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] == "extension-token-123"
    assert "OPENAI_API_KEY" not in helper_options["env"]
    assert "ANTHROPIC_API_KEY" not in helper_options["env"]
    request = json.loads(helper.input or "")
    assert request["chrome_profile"] == "Default"
    assert "extension-token-123" not in helper.input


@pytest.mark.parametrize(
    "profile",
    ["", "Profile 0", "Profile 01", "Profile -1", "../Default", r"Default\\Other", "$(id)"],
)
def test_normal_chrome_profile_rejects_paths_and_shell_syntax(
    tmp_path: Path, profile: str
) -> None:
    configured = normal_paths(tmp_path, profile=profile)
    assert runtime.run_browser(configured, "chatgpt", "connect")["error"]["code"] == (
        "CONFIG_INVALID"
    )


def test_extension_token_requires_exact_private_regular_file(tmp_path: Path) -> None:
    configured = normal_paths(tmp_path, token="token")
    token = configured.data_dir / "subscriptions/extension-token"
    token.chmod(0o644)
    with pytest.raises(runtime._RuntimeError) as unsafe:
        runtime._extension_token(configured)
    assert unsafe.value.code == "CONFIG_INVALID"
    token.unlink()
    token.symlink_to(configured.config_file)
    with pytest.raises(runtime._RuntimeError) as symlink:
        runtime._extension_token(configured)
    assert symlink.value.code == "CONFIG_INVALID"


def test_normal_helper_fallback_never_tree_kills_regular_chrome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = normal_paths(tmp_path, token="token")
    layout = stub_normal_runtime(configured, tmp_path, monkeypatch)
    profile = layout.profiles / "chatgpt"
    profile.mkdir(parents=True)
    operation = str(uuid.uuid4())
    lock = profile / ".taskspindle-collector.lock"
    lock.write_text(
        json.dumps(
            {
                "pid": 777,
                "start": "windows:638000000000000000",
                "nonce": "helper-release",
                "operation_nonce": operation,
            }
        ),
        encoding="utf-8",
    )
    helper = HangingProcess()
    helper.pid = 888
    selected: list[runtime._Owner] = []

    def stop_exact(owner: runtime._Owner) -> bool:
        selected.append(owner)
        helper.returncode = 0
        return True

    monkeypatch.setattr(runtime, "_terminate_windows_helper", stop_exact)
    monkeypatch.setattr(
        runtime,
        "_terminate_windows_tree",
        lambda _owner: pytest.fail("normal mode attempted a process-tree kill"),
    )
    ticks = iter([0.0, 6.0])
    monkeypatch.setattr(runtime, "_monotonic", lambda: next(ticks))

    runtime._terminate_normal_helper(
        helper, layout=layout, profile=profile, operation_nonce=operation
    )

    assert selected == [
        runtime._Owner(777, "windows:638000000000000000", "helper-release", operation)
    ]
    assert not lock.exists()
    assert not runtime._cancel_file(profile, operation).exists()


def test_normal_helper_cooperatively_observes_scoped_cancel_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = normal_paths(tmp_path, token="token")
    layout = stub_normal_runtime(configured, tmp_path, monkeypatch)
    profile = layout.profiles / "chatgpt"
    operation = str(uuid.uuid4())

    class Cooperative(HangingProcess):
        def wait(self, timeout: float) -> int:
            assert runtime._cancel_file(profile, operation).is_file()
            self.returncode = 0
            return 0

    monkeypatch.setattr(
        runtime,
        "_terminate_windows_helper",
        lambda _owner: pytest.fail("cooperative helper needed forced termination"),
    )
    helper = Cooperative()
    runtime._terminate_normal_helper(
        helper, layout=layout, profile=profile, operation_nonce=operation
    )
    assert not runtime._cancel_file(profile, operation).exists()


def test_cancellation_terminates_only_the_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = paths(tmp_path)
    prepare_runtime(configured)
    stub_node_check(monkeypatch)
    process = Process("", timeout=True)
    killed: list[Process] = []
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        runtime, "_terminate_owned", lambda candidate, **_kwargs: killed.append(candidate)
    )

    result = runtime.run_browser(configured, "chatgpt", "connect")

    assert result["error"]["code"] == "COLLECTOR_FAILED"
    assert killed and set(killed) == {process}


def test_preexisting_cancellation_does_not_launch_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = paths(tmp_path)
    prepare_runtime(configured)
    stub_node_check(monkeypatch)
    monkeypatch.setattr(
        runtime.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("cancelled operation launched Node"),
    )
    runtime._request_browser_stop()
    try:
        assert runtime.run_browser(configured, "chatgpt", "connect") == {
            "ok": False,
            "error": {
                "code": "COLLECTOR_FAILED",
                "message": "The subscription check failed.",
            },
        }
    finally:
        runtime._reset_browser_stop()


def test_process_deadline_returns_timeout_and_terminates_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = paths(tmp_path)
    prepare_runtime(configured)
    stub_node_check(monkeypatch)
    process = Process("")
    killed: list[Process] = []
    ticks = itertools.count(0.0, 1000.0)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(runtime, "_monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        runtime, "_terminate_owned", lambda candidate, **_kwargs: killed.append(candidate)
    )

    result = runtime.run_browser(configured, "chatgpt", "connect")

    assert result["error"]["code"] == "TIMEOUT"
    assert killed and set(killed) == {process}


def test_foreign_profile_owner_is_never_selected_for_windows_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = paths(tmp_path)
    settings = runtime._Settings(
        mode="windows",
        timezone="America/Chicago",
        node_path=None,
        chrome_path=None,
        local_app_data=r"C:\Users\person\AppData\Local",
        connect_timeout_s=600,
        refresh_timeout_s=180,
    )
    layout = runtime._layout(configured, settings)
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".taskspindle-collector.lock").write_text(
        json.dumps(
            {
                "pid": 999,
                "start": "windows:12345",
                "nonce": "lock-owner",
                "operation_nonce": "another-operation",
            }
        ),
        encoding="utf-8",
    )
    selected: list[runtime._Owner] = []
    monkeypatch.setattr(runtime, "_terminate_windows_tree", lambda owner: selected.append(owner))

    runtime._terminate_owned(
        Process(""),
        layout=layout,
        profile=profile,
        operation_nonce="this-operation",
    )

    assert selected == []
    assert (profile / ".taskspindle-collector.lock").exists()


def test_pid_start_mismatch_denies_windows_tree_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def deny(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 3, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", deny)
    owner = runtime._Owner(123, "windows:638000000000000000", "nonce", "operation")

    assert runtime._terminate_windows_tree(owner) is False
    encoded = calls[0][-1]
    script = base64.b64decode(encoded).decode("utf-16-le")
    assert "StartTime.ToUniversalTime().Ticks" in script
    assert "638000000000000000" in script
    assert "taskkill.exe /PID 123 /T /F" in script


def test_matching_operation_removes_only_its_owned_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = paths(tmp_path)
    settings = runtime._Settings(
        mode="windows",
        timezone="America/Chicago",
        node_path=None,
        chrome_path=None,
        local_app_data=r"C:\Users\person\AppData\Local",
        connect_timeout_s=600,
        refresh_timeout_s=180,
    )
    layout = runtime._layout(configured, settings)
    profile = tmp_path / "profile"
    profile.mkdir()
    operation = str(uuid.uuid4())
    lock = profile / ".taskspindle-collector.lock"
    lock.write_text(
        json.dumps(
            {
                "pid": 999,
                "start": "windows:12345",
                "nonce": "release-token",
                "operation_nonce": operation,
            }
        ),
        encoding="utf-8",
    )
    selected: list[runtime._Owner] = []
    monkeypatch.setattr(
        runtime,
        "_terminate_windows_tree",
        lambda owner: selected.append(owner) or True,
    )

    runtime._terminate_owned(
        Process(""), layout=layout, profile=profile, operation_nonce=operation
    )

    assert selected == [runtime._Owner(999, "windows:12345", "release-token", operation)]
    assert not lock.exists()


def test_setup_copies_assets_and_pins_playwright_without_browser_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = paths(tmp_path)
    calls: list[SimpleNamespace] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(SimpleNamespace(command=command, kwargs=kwargs))
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "v24.14.0\n", "")
        cwd = Path(kwargs["cwd"])
        manifest = cwd / "node_modules/playwright-core/package.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"version":"1.63.0"}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", run)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    report = runtime.setup_browser(configured)

    assert report["ok"] is True
    installed = Path(report["runtime_dir"])
    assert (installed / "helper.mjs").is_file()
    npm = calls[-1]
    assert npm.command[1:] == ["ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    assert npm.kwargs["env"]["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"


def test_windows_layout_stays_below_local_app_data(tmp_path: Path) -> None:
    configured = paths(tmp_path)
    settings = runtime._Settings(
        mode="windows",
        timezone="America/Chicago",
        node_path=None,
        chrome_path=None,
        local_app_data=r"C:\Users\person\AppData\Local",
        connect_timeout_s=600,
        refresh_timeout_s=180,
    )
    layout = runtime._layout(configured, settings)
    assert layout.runtime == Path("/mnt/c/Users/person/AppData/Local/TaskSpindle/subscriptions/runtime")
    assert layout.request_profiles == r"C:\Users\person\AppData\Local\TaskSpindle\subscriptions\profiles"
