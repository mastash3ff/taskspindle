"""Pinned Antigravity ACP paths, isolated cached authentication and interactive login."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from .config import Paths
    from .providers import Profile

ADAPTER_ID = "antigravity-acp"
ADAPTER_VERSION = "1.1.1"
ADAPTER_FILES = ("agy_acp_server.par", "localharness_external")
DOWNLOAD_URL = (
    "https://dl.google.com/agy-extensions/releases/linux/agy-acp-server-agy_acp_server_1.1.1-linux-x86_64.zip"
)
AUTH_METHOD = "oauth-personal"
AUTH_TIMEOUT = 330.0
_LOGIN_PREFIX = "Open the following link to authenticate the ACP server: "
_LOGIN_QUERY = frozenset(
    {
        "response_type",
        "client_id",
        "redirect_uri",
        "scope",
        "state",
        "code_challenge",
        "code_challenge_method",
        "access_type",
        "prompt",
        "include_granted_scopes",
        "login_hint",
        "enable_granular_consent",
    }
)


def adapter_command(runtime_dir: Path) -> tuple[str, str]:
    """The registry's exact Linux launch form, including its empty uid argument."""
    return (str(runtime_dir.resolve() / ADAPTER_ID / ADAPTER_VERSION / ADAPTER_FILES[0]), "--uid=")


def agy_home(data_dir: Path) -> Path:
    return data_dir / "agy-home"


def prepare_agy_home(data_dir: Path) -> Path:
    """Create only TaskSpindle's private home; never copy a CLI credential or setting."""
    home = agy_home(data_dir)
    for path in (home, home / ADAPTER_ID):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private_path(path, directory=True)
    return home


def _private_path(path: Path, *, directory: bool = False) -> None:
    from .providers import ProfileError

    try:
        info = path.lstat()
    except OSError as exc:
        raise ProfileError(
            "AGY_AUTH_REQUIRED", "Run taskspindle setup --provider agy, then taskspindle auth agy"
        ) from exc
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProfileError(
            "AGY_AUTH_UNSAFE", f"Antigravity private path must be owned by this user and private: {path}"
        )


def _control_file(path: Path) -> Any:
    from .providers import ProfileError

    if not path.exists() and not path.is_symlink():
        return None
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("unsafe control file")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(
            "AGY_CONFIG_UNSAFE", f"Antigravity isolated control file is invalid: {path}"
        ) from exc


def validate_agy_home(profile: Profile) -> Path:
    """Refuse inherited MCP, hooks, global skills or workspace-trust bypasses."""
    from .providers import ProfileError

    raw = profile.env.get("GEMINI_HOME", "")
    home = Path(raw)
    if profile.auth != "oauth" or not raw or not home.is_absolute():
        raise ProfileError(
            "AGY_CONFIG_UNSAFE", "Antigravity needs OAuth and an absolute dedicated GEMINI_HOME"
        )
    _private_path(home, directory=True)
    _private_path(home / ADAPTER_ID, directory=True)
    if any((home / name).is_symlink() for name in ("config", "antigravity-cli")):
        raise ProfileError(
            "AGY_CONFIG_UNSAFE", "Antigravity isolated configuration must not inherit symlinked directories"
        )
    if profile.env.get("AGY_ACP_DISABLE_WORKSPACE_TRUST", "false").lower() not in ("", "0", "false", "no"):
        raise ProfileError("AGY_CONFIG_UNSAFE", "Antigravity workspace trust bypass must be disabled")
    for relative, key in (("config/mcp_config.json", "mcpServers"), ("config/hooks.json", "hooks")):
        body = _control_file(home / relative)
        if body not in (None, {}, {key: {}}):
            raise ProfileError("AGY_CONFIG_UNSAFE", f"Antigravity isolated home must have no global {key}")
    trust = _control_file(home / ADAPTER_ID / "trusted_workspaces.json")
    if trust is not None and (
        not isinstance(trust, dict)
        or trust.get("trusted")
        or set(trust) - {"trusted", "untrusted"}
        or not isinstance(trust.get("trusted", []), list)
        or not isinstance(trust.get("untrusted", []), list)
    ):
        raise ProfileError("AGY_CONFIG_UNSAFE", "Antigravity isolated home must have no trusted workspaces")
    settings = _control_file(home / ADAPTER_ID / "settings.json")
    if settings is not None and settings not in ({}, {"auth": {"type": AUTH_METHOD}}):
        raise ProfileError(
            "AGY_CONFIG_UNSAFE", "Antigravity isolated settings must select only oauth-personal"
        )
    for relative in ("config/skills", "antigravity-cli/skills"):
        path = home / relative
        if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
            raise ProfileError("AGY_CONFIG_UNSAFE", "Antigravity isolated home must have no global skills")
    return home


def agy_oauth_evidence(profile: Profile) -> dict[str, Any]:
    """Check private cache presence without reading tokens; session creation validates them."""
    from .providers import ProfileError

    home = validate_agy_home(profile)
    token = home / ADAPTER_ID / "acp_token.json"
    try:
        _private_path(token)
    except ProfileError as exc:
        if exc.code == "AGY_AUTH_REQUIRED":
            raise ProfileError(
                "AGY_AUTH_REQUIRED",
                "Antigravity ACP needs its own personal sign-in; run taskspindle auth agy",
            ) from exc
        raise
    if token.stat().st_size == 0:
        raise ProfileError(
            "AGY_AUTH_REQUIRED", "Antigravity ACP cached credential is empty; run taskspindle auth agy"
        )
    return {
        "auth": "oauth",
        "auth_method_id": AUTH_METHOD,
        "cached_credential": True,
        "source": "private_file_presence",
        "credential_validity": "unverified",
    }


def login_urls(stderr: str) -> list[str]:
    """Extract only Google's authorization endpoint, never callbacks or credential fields."""
    urls: list[str] = []
    for line in stderr.splitlines():
        if _LOGIN_PREFIX not in line:
            continue
        url = line.split(_LOGIN_PREFIX, 1)[1].strip()
        try:
            parsed = urlsplit(url)
            query = parse_qs(parsed.query, keep_blank_values=True)
        except ValueError:
            continue
        if (
            parsed.scheme == "https"
            and parsed.netloc == "accounts.google.com"
            and parsed.path == "/o/oauth2/v2/auth"
            and not parsed.fragment
            and set(query) <= _LOGIN_QUERY
            and query.get("response_type") == ["code"]
        ):
            urls.append(url)
    return urls


async def authenticate_personal(
    profile: Profile,
    paths: Paths,
    parent_env: Mapping[str, str],
    *,
    emit: Callable[[str], None] = print,
    timeout: float = AUTH_TIMEOUT,
) -> dict[str, Any]:
    """Explicit personal login only: no session, model prompt, credential copying or disk stderr."""
    from .acp_client import AcpError, AcpWorker
    from .agy_policy import AgyPermissionPolicy
    from .providers import ProfileError, build_child_env

    prepare_agy_home(paths.data_dir)
    if validate_agy_home(profile) != agy_home(paths.data_dir):
        raise ProfileError("AGY_CONFIG_UNSAFE", "Antigravity sign-in must use TaskSpindle's dedicated home")
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.memfd_create("taskspindle-agy-auth", os.MFD_CLOEXEC)
    try:
        with tempfile.TemporaryDirectory(prefix="agy-auth-", dir=paths.state_dir) as raw:
            workspace = Path(raw)
            temporary = workspace / "tmp"
            temporary.mkdir(mode=0o700)
            env = build_child_env(profile, parent_env, task_tmp=temporary)
            worker = AcpWorker(
                command=profile.command,
                env=env,
                cwd=workspace,
                stderr_path=Path(f"/proc/self/fd/{fd}"),
                policy=AgyPermissionPolicy(allow_writes=False, workspace=workspace),
            )
            async with worker:
                task = asyncio.create_task(worker.authenticate(AUTH_METHOD, timeout=timeout))
                shown: set[str] = set()
                try:
                    while not task.done():
                        size = os.fstat(fd).st_size
                        text = os.pread(fd, min(size, 65536), max(0, size - 65536)).decode("utf-8", "replace")
                        for url in login_urls(text):
                            if url not in shown:
                                shown.add(url)
                                emit(url)
                        await asyncio.sleep(0.1)
                    await task
                finally:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
    except AcpError as exc:
        if exc.code == "ACP_SPAWN_FAILED":
            raise ProfileError(
                "AGY_ADAPTER_MISSING", "Antigravity ACP could not start; run taskspindle setup --provider agy"
            ) from exc
        raise ProfileError(
            "AGY_AUTH_FAILED", "Personal Google sign-in failed or timed out; run taskspindle auth agy again"
        ) from exc
    finally:
        os.close(fd)
    return agy_oauth_evidence(profile)
