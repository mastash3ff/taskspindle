"""Stage an explicitly supplied Muse binary without authenticating or enabling it."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import Paths
from .muse import MUSE_BUILD, MUSE_SCHEMA_FINGERPRINT, MUSE_VERSION, qualification
from .setup import SetupError

_TIMEOUT = 30.0


def _raw_binary(binary: Path) -> None:
    if not binary.is_absolute():
        raise SetupError("Muse requires an explicit absolute native binary path; PATH lookup is disabled")
    try:
        info = binary.lstat()
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise SetupError(f"Muse binary must be a regular executable, not a symlink: {binary}")
        with binary.open("rb") as handle:
            if handle.read(4) != b"\x7fELF":
                raise SetupError(
                    f"Muse requires the raw Linux ELF binary, not a launcher or wrapper: {binary}"
                )
    except OSError as exc:
        raise SetupError(f"could not inspect Muse binary {binary}: {exc}") from exc


def _sha256(binary: Path) -> str:
    with binary.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _offline_command(binary: Path, args: list[str], home: Path) -> str:
    # No inherited credentials, provider endpoints, dynamic-loader overrides, or foreign roots.
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
    }
    try:
        result = subprocess.run(
            [str(binary), *args], cwd=home, env=env,
            capture_output=True, text=True, timeout=_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise SetupError(f"Muse offline {args[0]} check failed: {exc}") from exc
    if result.returncode != 0:
        raise SetupError(f"Muse offline {args[0]} check exited {result.returncode}")
    return result.stdout.strip()


def verify_muse_binary(binary: Path) -> dict[str, str]:
    """Verify raw format, exact build and embedded stable schema using offline commands only.

    The checksum identifies the supplied file; it is not publisher-signature verification.
    This establishes binary compatibility, never subscription or tool-policy qualification.
    """
    binary = Path(binary)
    _raw_binary(binary)
    try:
        digest = _sha256(binary)
        with tempfile.TemporaryDirectory(prefix="taskspindle-muse-verify-") as directory:
            home = Path(directory)
            found = _offline_command(binary, ["--version"], home)
            expected = f"Muse Code {MUSE_VERSION} ({MUSE_BUILD})"
            if found != expected:
                raise SetupError(f"Muse binary must report the exact pinned build: {expected}")
            schema_dir = home / "schema"
            _offline_command(binary, ["schema", "generate-json-schema", "--out", str(schema_dir)], home)
            manifest = json.loads((schema_dir / "manifest.json").read_text(encoding="utf-8"))
            schema = json.loads((schema_dir / "msp.schema.json").read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("experimental") is not False
                or type(manifest.get("schemaVersion")) is not int
                or manifest["schemaVersion"] != 1
                or manifest.get("fingerprint") != MUSE_SCHEMA_FINGERPRINT
                or not isinstance(schema, dict)
                or not isinstance(schema.get("methods"), dict)
                or "initialize" not in schema["methods"]
            ):
                raise SetupError("Muse embedded stable schema does not match the pinned MSP surface")
        if _sha256(binary) != digest:
            raise SetupError("Muse binary changed during offline verification")
    except (OSError, ValueError) as exc:
        raise SetupError(f"could not verify Muse binary {binary}: {exc}") from exc
    return {
        "version": MUSE_VERSION, "build": MUSE_BUILD,
        "schema_fingerprint": MUSE_SCHEMA_FINGERPRINT, "sha256": digest,
    }


def install_muse_runtime(paths: Paths, binary: Path) -> dict[str, Any]:
    """Atomically install verified bytes at ``runtime_dir/muse``; preserve user config."""
    binary = Path(binary)
    _raw_binary(binary)
    runtime = paths.runtime_dir
    staged: Path | None = None
    try:
        if runtime.is_symlink() or (runtime.exists() and not runtime.is_dir()):
            raise SetupError(f"Muse runtime directory must be a real directory: {runtime}")
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = runtime.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise SetupError(
                f"Muse runtime directory must be user-owned and not writable by others: {runtime}"
            )
        destination = runtime / "muse"
        if destination.is_symlink():
            raise SetupError(f"Muse runtime executable must not be a symlink: {destination}")
        descriptor, name = tempfile.mkstemp(prefix=".muse-stage-", dir=runtime)
        staged = Path(name)
        with os.fdopen(descriptor, "wb") as output, binary.open("rb") as source:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        staged.chmod(0o700)
        verified = verify_muse_binary(staged)
        os.replace(staged, destination)
        staged = None
    except OSError as exc:
        raise SetupError(f"could not stage Muse runtime: {exc}") from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return {
        "provider": "muse", "adapter_package": "muse", "adapter_version": verified["version"],
        "runtime_dir": str(runtime), "command": [str(destination)],
        "config_file": str(paths.config_file), "created_config": False,
        **verified, "qualification": qualification(),
    }
