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
