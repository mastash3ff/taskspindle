"""Process bridge for regular-Chrome and dedicated-profile subscription collectors.

Regular mode opens the user's chosen Chrome profile without remote-debugging or
user-data-dir flags and talks through the reviewed Playwright MCP extension.
Dedicated mode remains available for an isolated provider-specific browser profile.
Neither mode inherits unrelated credentials from the calling shell.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from taskspindle.config import Paths

from .models import ACTIONS, ERROR_MESSAGES, PROVIDER_IDS, PROVIDERS, validate_result

__all__ = ["run_browser", "setup_browser"]

WINDOWS_NODE = r"C:\Program Files\nodejs\node.exe"
WINDOWS_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PLAYWRIGHT_VERSION = "1.63.0"
_SETUP_TIMEOUT = 900.0
_PROCESS_GRACE = 10.0
_PROFILE_LOCK_WAIT = 5.0
_MAX_OUTPUT = 1024 * 1024
_CANCEL_REQUESTED = threading.Event()
_ASSET_NAMES = (
    "helper.mjs",
    "normal-helper.mjs",
    "collection.mjs",
    "claude.mjs",
    "google.mjs",
    "extractors.mjs",
    "page.mjs",
    "ownership.mjs",
    "package.json",
    "package-lock.json",
    "NOTICE",
)

_ERROR_MESSAGES = ERROR_MESSAGES


@dataclass(frozen=True)
class _Settings:
    mode: str
    timezone: str
    node_path: str | None
    chrome_path: str | None
    local_app_data: str | None
    connect_timeout_s: int
    refresh_timeout_s: int
    browser_mode: str = "normal"
    chrome_profile: str = "Default"
    scheduled_refresh: bool = False


@dataclass(frozen=True)
class _Layout:
    mode: str
    root: Path
    runtime: Path
    profiles: Path
    node: Path
    chrome: Path
    request_runtime: str
    request_profiles: str
    request_chrome: str


@dataclass(frozen=True)
class _Owner:
    pid: int
    start: str
    nonce: str
    operation_nonce: str


class _RuntimeError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


def _failure(code: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": _ERROR_MESSAGES[code]}}


def _monotonic() -> float:
    """Local clock seam without replacing ``time.monotonic`` process-wide in tests."""
    return time.monotonic()


def _request_browser_stop() -> None:
    """Ask the one service-owned collector process to stop at its next poll."""
    _CANCEL_REQUESTED.set()


def _reset_browser_stop() -> None:
    """Clear cancellation before a watch installs handlers and begins accepting work."""
    _CANCEL_REQUESTED.clear()


def _load_settings(paths: Paths) -> _Settings:
    try:
        raw = tomllib.loads(paths.config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _RuntimeError("CONFIG_INVALID") from exc
    values = raw.get("subscriptions", {})
    if not isinstance(values, dict):
        raise _RuntimeError("CONFIG_INVALID")
    mode = values.get("platform", "auto")
    browser_mode = values.get("browser_mode", "normal")
    chrome_profile = values.get("chrome_profile", "Default")
    timezone = values.get("timezone") or _local_timezone()
    scheduled_refresh = values.get("scheduled_refresh", False)
    if (
        mode not in {"auto", "windows", "native"}
        or browser_mode not in {"normal", "dedicated"}
        or not isinstance(timezone, str)
        or not _valid_chrome_profile(chrome_profile)
        or type(scheduled_refresh) is not bool
    ):
        raise _RuntimeError("CONFIG_INVALID")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise _RuntimeError("CONFIG_INVALID") from exc

    def optional_path(name: str) -> str | None:
        value = values.get(name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _RuntimeError("CONFIG_INVALID")
        return value

    def timeout(name: str, default: int) -> int:
        value = values.get(name, default)
        if type(value) is not int or not 30 <= value <= 900:
            raise _RuntimeError("CONFIG_INVALID")
        return value

    return _Settings(
        mode=mode,
        timezone=timezone,
        node_path=optional_path("node_path"),
        chrome_path=optional_path("chrome_path"),
        local_app_data=optional_path("local_app_data"),
        connect_timeout_s=timeout("connect_timeout_s", 600),
        refresh_timeout_s=timeout("refresh_timeout_s", 180),
        browser_mode=browser_mode,
        chrome_profile=chrome_profile,
        scheduled_refresh=scheduled_refresh,
    )


def scheduled_refresh_enabled(paths: Paths) -> bool:
    """Read the opt-in scheduling flag without exposing configuration details."""

    try:
        return _load_settings(paths).scheduled_refresh
    except _RuntimeError as exc:
        raise ValueError("invalid subscription configuration") from exc


def _valid_chrome_profile(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value == "Default":
        return True
    if not value.startswith("Profile "):
        return False
    suffix = value.removeprefix("Profile ")
    return suffix.isascii() and suffix.isdigit() and not suffix.startswith("0")


def _extension_token_path(paths: Paths) -> Path:
    return paths.data_dir / "subscriptions" / "extension-token"


def _extension_token(paths: Paths) -> str:
    token_path = _extension_token_path(paths)
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(token_path, flags)
    except FileNotFoundError as exc:
        raise _RuntimeError("SETUP_REQUIRED") from exc
    except OSError as exc:
        raise _RuntimeError("CONFIG_INVALID") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise _RuntimeError("CONFIG_INVALID")
        raw = os.read(fd, 513)
    finally:
        os.close(fd)
    if not 1 <= len(raw) <= 512 or any(byte < 0x21 or byte > 0x7E for byte in raw):
        raise _RuntimeError("CONFIG_INVALID")
    return raw.decode("ascii")


def _local_timezone() -> str:
    configured = os.environ.get("TZ")
    if configured:
        with contextlib.suppress(ZoneInfoNotFoundError):
            ZoneInfo(configured)
            return configured
    try:
        candidate = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        candidate = ""
    if candidate:
        with contextlib.suppress(ZoneInfoNotFoundError):
            ZoneInfo(candidate)
            return candidate
    return "UTC"


def _is_wsl() -> bool:
    return "microsoft" in platform.release().lower() or "WSL_DISTRO_NAME" in os.environ


def _windows_to_host(raw: str) -> Path:
    value = raw.strip().strip('"')
    if len(value) >= 3 and value[1] == ":" and value[2] in {"\\", "/"}:
        drive = value[0].lower()
        rest = value[3:].replace("\\", "/")
        return Path("/mnt") / drive / rest
    path = Path(value)
    if path.is_absolute():
        return path
    raise _RuntimeError("CONFIG_INVALID")


def _host_to_windows(path: Path) -> str:
    resolved = path.absolute()
    parts = resolved.parts
    if len(parts) >= 4 and parts[1] == "mnt" and len(parts[2]) == 1:
        return f"{parts[2].upper()}:\\" + "\\".join(parts[3:])
    raise _RuntimeError("CONFIG_INVALID")


def _discovery_env() -> dict[str, str]:
    names = (
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "WSL_DISTRO_NAME",
        "WSLENV",
        "WSL_INTEROP",
    )
    return {name: os.environ[name] for name in names if name in os.environ}


def _windows_local_app_data() -> Path:
    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        return _windows_to_host(configured)
    command = Path("/mnt/c/Windows/System32/cmd.exe")
    try:
        proc = subprocess.run(
            [str(command), "/d", "/s", "/c", "echo", "%LOCALAPPDATA%"],
            env=_discovery_env(),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _RuntimeError("RUNTIME_UNAVAILABLE") from exc
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value or value == "%LOCALAPPDATA%":
        raise _RuntimeError("RUNTIME_UNAVAILABLE")
    return _windows_to_host(value)


def _native_chrome(configured: str | None) -> Path:
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise _RuntimeError("CONFIG_INVALID")
        return path
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    raise _RuntimeError("RUNTIME_UNAVAILABLE")


def _layout(paths: Paths, settings: _Settings) -> _Layout:
    mode = settings.mode
    if mode == "auto":
        mode = "windows" if _is_wsl() else "native"
    if mode == "windows":
        local = (
            _windows_to_host(settings.local_app_data)
            if settings.local_app_data
            else _windows_local_app_data()
        )
        root = local / "TaskSpindle" / "subscriptions"
        node = _windows_to_host(settings.node_path or WINDOWS_NODE)
        chrome = _windows_to_host(settings.chrome_path or WINDOWS_CHROME)
        return _Layout(
            mode=mode,
            root=root,
            runtime=root / "runtime",
            profiles=root / "profiles",
            node=node,
            chrome=chrome,
            request_runtime=_host_to_windows(root / "runtime"),
            request_profiles=_host_to_windows(root / "profiles"),
            request_chrome=_host_to_windows(chrome),
        )
    if settings.local_app_data is not None:
        root = Path(settings.local_app_data).expanduser()
        if not root.is_absolute():
            raise _RuntimeError("CONFIG_INVALID")
        root = root / "TaskSpindle" / "subscriptions"
    else:
        root = paths.data_dir / "subscriptions"
    node_value = settings.node_path or shutil.which("node")
    if not node_value:
        raise _RuntimeError("RUNTIME_UNAVAILABLE")
    node = Path(node_value).expanduser()
    if not node.is_absolute():
        node = node.resolve()
    chrome = _native_chrome(settings.chrome_path)
    return _Layout(
        mode=mode,
        root=root,
        runtime=root / "runtime",
        profiles=root / "profiles",
        node=node,
        chrome=chrome,
        request_runtime=str(root / "runtime"),
        request_profiles=str(root / "profiles"),
        request_chrome=str(chrome),
    )


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


@contextlib.contextmanager
def _lock(path: Path, *, wait: float) -> Any:
    _private_directory(path.parent)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = _monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if _monotonic() >= deadline:
                    raise _RuntimeError("PROFILE_BUSY") from None
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _copy_assets(destination: Path) -> None:
    package = resources.files("taskspindle").joinpath("_subscription_browser")
    for name in _ASSET_NAMES:
        source = package.joinpath(name)
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise _RuntimeError("SETUP_REQUIRED") from exc
        target = destination / name
        target.write_bytes(content)
        target.chmod(0o600)


def _npm_command(layout: _Layout) -> list[str]:
    if layout.mode == "windows":
        npm_cli = layout.node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        return [
            str(layout.node),
            _host_to_windows(npm_cli),
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ]
    npm = shutil.which("npm")
    if not npm:
        raise _RuntimeError("RUNTIME_UNAVAILABLE")
    return [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"]


def _node_env(settings: _Settings) -> dict[str, str]:
    names = (
        "LANG",
        "LC_ALL",
        "PATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
        "WSL_DISTRO_NAME",
        "WSLENV",
        "WSL_INTEROP",
    )
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["TZ"] = settings.timezone
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    return env


def _normal_helper_env(settings: _Settings, token: str) -> dict[str, str]:
    env = _node_env(settings)
    token_name = "PLAYWRIGHT_MCP_EXTENSION_TOKEN"
    env[token_name] = token
    inherited = env.get("WSLENV", "")
    allowed = {name.casefold() for name in env if name != "WSLENV"}
    entries: list[str] = []
    for entry in inherited.split(":"):
        name = entry.split("/", 1)[0]
        if (
            name
            and name.casefold() in allowed
            and name.casefold() != token_name.casefold()
        ):
            entries.append(entry)
    entries.append(f"{token_name}/w")
    env["WSLENV"] = ":".join(entries)
    return env


def _launch_normal_chrome(
    layout: _Layout,
    settings: _Settings,
    provider: str,
    *,
    billing_url: bool,
) -> None:
    """Open one provider URL in regular Chrome without creating an automation profile."""
    if layout.mode != "windows":
        raise _RuntimeError("RUNTIME_UNAVAILABLE")

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    chrome = layout.request_chrome
    arguments = [f"--profile-directory={settings.chrome_profile}"]
    if billing_url:
        arguments.append(PROVIDERS[provider]["billing_url"])
    argument_line = subprocess.list2cmdline(arguments)
    script = (
        "$ErrorActionPreference='Stop';"
        f"Start-Process -FilePath {literal(chrome)} "
        f"-ArgumentList {literal(argument_line)} | Out-Null"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    try:
        proc = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=_discovery_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _RuntimeError("BROWSER_UNAVAILABLE") from exc
    if proc.returncode != 0:
        raise _RuntimeError("BROWSER_UNAVAILABLE")


def _verify_tools(layout: _Layout, settings: _Settings) -> str:
    if not layout.node.is_file() or not layout.chrome.is_file():
        raise _RuntimeError("RUNTIME_UNAVAILABLE")
    try:
        proc = subprocess.run(
            [str(layout.node), "--version"],
            env=_node_env(settings),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _RuntimeError("RUNTIME_UNAVAILABLE") from exc
    version = proc.stdout.strip()
    try:
        major = int(version.removeprefix("v").split(".", 1)[0])
    except ValueError as exc:
        raise _RuntimeError("RUNTIME_UNAVAILABLE") from exc
    if proc.returncode != 0 or major < 20:
        raise _RuntimeError("RUNTIME_UNAVAILABLE")
    return version


def setup_browser(paths: Paths) -> dict[str, Any]:
    """Install the packaged helper and pinned ``playwright-core`` in its private runtime."""
    try:
        settings = _load_settings(paths)
        layout = _layout(paths, settings)
        if settings.browser_mode == "normal" and layout.mode != "windows":
            raise _RuntimeError("RUNTIME_UNAVAILABLE")
        node_version = _verify_tools(layout, settings)
        _private_directory(layout.root)
        _private_directory(layout.profiles)
        with _lock(layout.root / ".setup.lock", wait=_PROFILE_LOCK_WAIT):
            staging = Path(tempfile.mkdtemp(prefix=".runtime.setup-", dir=layout.root))
            backup = layout.root / f".runtime.previous-{os.getpid()}-{uuid.uuid4().hex}"
            try:
                _copy_assets(staging)
                proc = subprocess.run(
                    _npm_command(layout),
                    cwd=staging,
                    env=_node_env(settings),
                    capture_output=True,
                    text=True,
                    timeout=_SETUP_TIMEOUT,
                    check=False,
                )
                if proc.returncode != 0:
                    raise _RuntimeError("RUNTIME_UNAVAILABLE")
                manifest = staging / "node_modules" / "playwright-core" / "package.json"
                installed = json.loads(manifest.read_text(encoding="utf-8"))
                if installed.get("version") != PLAYWRIGHT_VERSION:
                    raise _RuntimeError("RUNTIME_UNAVAILABLE")
                if layout.runtime.exists():
                    layout.runtime.rename(backup)
                try:
                    staging.rename(layout.runtime)
                except BaseException:
                    if backup.exists() and not layout.runtime.exists():
                        backup.rename(layout.runtime)
                    raise
                if backup.exists():
                    shutil.rmtree(backup)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return {
            "ok": True,
            "browser_mode": settings.browser_mode,
            "platform": layout.mode,
            "runtime_dir": str(layout.runtime),
            "profiles_dir": (
                str(layout.profiles) if settings.browser_mode == "dedicated" else None
            ),
            "node": str(layout.node),
            "node_version": node_version,
            "chrome": str(layout.chrome),
            "playwright_core": PLAYWRIGHT_VERSION,
            "extension_required": settings.browser_mode == "normal",
            "extension_token_path": str(_extension_token_path(paths)),
            "extension_setup_command": "taskspindle subscriptions setup-extension",
        }
    except _RuntimeError as exc:
        return _failure(exc.code)
    except (OSError, ValueError, json.JSONDecodeError):
        return _failure("RUNTIME_UNAVAILABLE")


def _profile_owner(profile: Path, operation_nonce: str) -> _Owner | None:
    """Read only the helper's PID/nonce ownership receipt, never profile data."""
    try:
        value = json.loads((profile / ".taskspindle-collector.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    start = value.get("start") if isinstance(value, dict) else None
    nonce = value.get("nonce") if isinstance(value, dict) else None
    recorded_operation = value.get("operation_nonce") if isinstance(value, dict) else None
    if (
        type(pid) is not int
        or pid < 1
        or not isinstance(start, str)
        or not start
        or not isinstance(nonce, str)
        or not nonce
        or recorded_operation != operation_nonce
    ):
        return None
    return _Owner(pid, start, nonce, operation_nonce)


def _remove_owned_profile_lock(profile: Path, owner: _Owner) -> None:
    lock = profile / ".taskspindle-collector.lock"
    if _profile_owner(profile, owner.operation_nonce) == owner:
        with contextlib.suppress(OSError):
            lock.unlink()


def _terminate_windows_tree(owner: _Owner) -> bool:
    """Kill a Windows tree only while its PID still has the recorded start identity."""
    if not owner.start.startswith("windows:") or not owner.start[8:].isdigit():
        return False
    ticks = owner.start[8:]
    script = (
        "$ErrorActionPreference='Stop';"
        f"$p=Get-Process -Id {owner.pid} -ErrorAction SilentlyContinue;"
        "if ($null -eq $p) { exit 4 };"
        f"if ($p.StartTime.ToUniversalTime().Ticks.ToString() -ne '{ticks}') {{ exit 3 }};"
        f"& taskkill.exe /PID {owner.pid} /T /F | Out-Null;"
        "exit $LASTEXITCODE"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=_discovery_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _terminate_windows_helper(owner: _Owner) -> bool:
    """Stop one exact helper PID without traversing into regular Chrome."""
    if not owner.start.startswith("windows:") or not owner.start[8:].isdigit():
        return False
    ticks = owner.start[8:]
    script = (
        "$ErrorActionPreference='Stop';"
        f"$p=Get-Process -Id {owner.pid} -ErrorAction SilentlyContinue;"
        "if ($null -eq $p) { exit 4 };"
        f"if ($p.StartTime.ToUniversalTime().Ticks.ToString() -ne '{ticks}') {{ exit 3 }};"
        "Stop-Process -InputObject $p -Force;"
        "exit 0"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    powershell = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=_discovery_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _cancel_file(profile: Path, operation_nonce: str) -> Path:
    return profile / f".taskspindle-cancel-{operation_nonce}"


def _terminate_normal_helper(
    proc: subprocess.Popen[str],
    *,
    layout: _Layout,
    profile: Path,
    operation_nonce: str,
) -> None:
    """Cooperatively stop the helper, then target only its exact PID as fallback."""
    if proc.poll() is not None:
        return
    sentinel = _cancel_file(profile, operation_nonce)
    created = False
    try:
        _private_directory(profile)
        fd = os.open(sentinel, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        os.close(fd)
        created = True
    except FileExistsError:
        pass
    except OSError:
        pass
    # The helper may need two seconds to receive a late page and another two
    # seconds to close it; allow that bounded cleanup before exact-PID fallback.
    deadline = _monotonic() + 5.0
    while proc.poll() is None and _monotonic() < deadline:
        try:
            proc.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            continue
    owner = _profile_owner(profile, operation_nonce)
    helper_stopped = False
    if proc.poll() is None and layout.mode == "windows" and owner is not None:
        helper_stopped = _terminate_windows_helper(owner)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(proc.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1)
    if helper_stopped and owner is not None:
        _remove_owned_profile_lock(profile, owner)
    if created:
        with contextlib.suppress(OSError):
            sentinel.unlink()


def _terminate_owned(
    proc: subprocess.Popen[str],
    *,
    layout: _Layout | None = None,
    profile: Path | None = None,
    operation_nonce: str | None = None,
) -> None:
    """Terminate this invocation's process tree and no name-matched browser processes."""
    windows_owner = (
        _profile_owner(profile, operation_nonce)
        if layout and layout.mode == "windows" and profile and operation_nonce
        else None
    )
    windows_tree_stopped = bool(windows_owner and _terminate_windows_tree(windows_owner))
    if proc.poll() is not None:
        if windows_tree_stopped and profile is not None and windows_owner is not None:
            _remove_owned_profile_lock(profile, windows_owner)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)
    if windows_tree_stopped and profile is not None and windows_owner is not None:
        _remove_owned_profile_lock(profile, windows_owner)


def _normalize_result(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > _MAX_OUTPUT:
        return _failure("COLLECTOR_PROTOCOL_ERROR")
    try:
        decoded = json.loads(raw)
        validated = validate_result(decoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _failure("COLLECTOR_PROTOCOL_ERROR")
    if validated.get("ok") is False:
        code = validated["error"]["code"]
        if code not in _ERROR_MESSAGES:
            return _failure("COLLECTOR_PROTOCOL_ERROR")
        return _failure(code)
    return validated


def run_browser(
    paths: Paths,
    provider: str,
    action: str,
    expected_account_id: str | None = None,
) -> dict[str, Any]:
    """Run one collector request with JSON stdin and return one validated, safe result."""
    if provider not in PROVIDER_IDS or action not in ACTIONS:
        return _failure("INVALID_REQUEST")
    if _CANCEL_REQUESTED.is_set():
        return _failure("COLLECTOR_FAILED")
    try:
        settings = _load_settings(paths)
        layout = _layout(paths, settings)
        _verify_tools(layout, settings)
        token: str | None = None
        if settings.browser_mode == "normal":
            try:
                token = _extension_token(paths)
            except _RuntimeError as exc:
                if action == "connect" and exc.code == "SETUP_REQUIRED":
                    _launch_normal_chrome(
                        layout, settings, provider, billing_url=True
                    )
                raise
            if action == "connect":
                _launch_normal_chrome(layout, settings, provider, billing_url=False)
            helper = layout.runtime / "normal-helper.mjs"
        else:
            helper = layout.runtime / "helper.mjs"
        if not helper.is_file() or not (layout.runtime / "node_modules" / "playwright-core").is_dir():
            raise _RuntimeError("SETUP_REQUIRED")
        _private_directory(layout.profiles)
        timeout_s = settings.connect_timeout_s if action == "connect" else settings.refresh_timeout_s
        request_profile = (
            f"{layout.request_profiles}\\{provider}"
            if layout.mode == "windows"
            else str(layout.profiles / provider)
        )
        request = {
            "provider": provider,
            "action": action,
            "profile_dir": request_profile,
            "chrome_path": layout.request_chrome,
            "expected_account_id": expected_account_id,
            "timeout_s": timeout_s,
            "timezone": settings.timezone,
        }
        if settings.browser_mode == "normal":
            request["chrome_profile"] = settings.chrome_profile
        operation_nonce = str(uuid.uuid4())
        request["operation_nonce"] = operation_nonce
        lock_name = (
            ".normal.runtime.lock"
            if settings.browser_mode == "normal"
            else f".{provider}.runtime.lock"
        )
        lock_path = layout.profiles / lock_name
        with _lock(lock_path, wait=_PROFILE_LOCK_WAIT):
            profile_path = layout.profiles / provider
            helper_name = helper.name
            script = (
                f"{layout.request_runtime}\\{helper_name}"
                if layout.mode == "windows"
                else str(helper)
            )
            child_env = (
                _normal_helper_env(settings, token)
                if token is not None
                else _node_env(settings)
            )
            try:
                proc = subprocess.Popen(
                    [str(layout.node), script],
                    cwd=layout.runtime,
                    env=child_env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    start_new_session=True,
                )
            except OSError:
                return _failure("BROWSER_UNAVAILABLE")
            try:
                deadline = _monotonic() + timeout_s + _PROCESS_GRACE
                input_data: str | None = json.dumps(request, separators=(",", ":")) + "\n"
                while True:
                    if _CANCEL_REQUESTED.is_set():
                        if settings.browser_mode == "normal":
                            _terminate_normal_helper(
                                proc,
                                layout=layout,
                                profile=profile_path,
                                operation_nonce=operation_nonce,
                            )
                        else:
                            _terminate_owned(
                                proc,
                                layout=layout,
                                profile=profile_path,
                                operation_nonce=operation_nonce,
                            )
                        return _failure("COLLECTOR_FAILED")
                    remaining = deadline - _monotonic()
                    if remaining <= 0:
                        if settings.browser_mode == "normal":
                            _terminate_normal_helper(
                                proc,
                                layout=layout,
                                profile=profile_path,
                                operation_nonce=operation_nonce,
                            )
                        else:
                            _terminate_owned(
                                proc,
                                layout=layout,
                                profile=profile_path,
                                operation_nonce=operation_nonce,
                            )
                        return _failure("TIMEOUT")
                    try:
                        stdout, _ = proc.communicate(input_data, timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        input_data = None
            finally:
                if settings.browser_mode == "normal":
                    _terminate_normal_helper(
                        proc,
                        layout=layout,
                        profile=profile_path,
                        operation_nonce=operation_nonce,
                    )
                else:
                    _terminate_owned(
                        proc,
                        layout=layout,
                        profile=profile_path,
                        operation_nonce=operation_nonce,
                    )
                _CANCEL_REQUESTED.clear()
        if proc.returncode != 0:
            return _failure("COLLECTOR_FAILED")
        return _normalize_result(stdout)
    except _RuntimeError as exc:
        return _failure(exc.code)
    except OSError:
        return _failure("RUNTIME_UNAVAILABLE")
