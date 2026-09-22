"""Mount planning and standalone, pre-exec namespace verification for AGY."""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from bisect import bisect_left
from pathlib import Path


class MountTable(dict):
    """Index mount destinations once, without repeated full-host scans."""

    paths: list[str]
    filesystems: dict[Path, str]


def mount_table() -> dict[Path, set[str]]:
    result = MountTable()
    result.filesystems = {}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        path = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
        result[Path(path)] = set(fields[5].split(','))
        result.filesystems[Path(path)] = fields[fields.index('-') + 1]
    result.paths = sorted(str(path) for path in result)
    return result


def runtime_sources(root: Path, mounts: dict[Path, set[str]]) -> list[Path]:
    """Split around, and exclude, mounted descendants; the count grants no access."""
    prefix = str(root).rstrip("/") + "/"
    descendants = {p for p in mounts if str(p).startswith(prefix)}
    if len(descendants) > 64:
        raise ValueError(f'runtime mount inventory exceeds 64 descendants: {root}')
    def visit(path: Path) -> list[Path]:
        if path in descendants:
            return []
        if not any(p.is_relative_to(path) for p in descendants):
            return [path]
        result = []
        for child in sorted(path.iterdir()):
            if child.is_symlink():
                # Symlinks are reconstructed separately by the planner.
                result.append(child)
            elif child.is_dir():
                result.extend(visit(child))
            else:
                result.append(child)
        return result
    return visit(root)


def validate_source(source: Path, mounts: dict[Path, set[str]]) -> None:
    prefix = str(source).rstrip('/') + '/'
    paths = mounts.paths if isinstance(mounts, MountTable) else sorted(map(str, mounts))
    index = bisect_left(paths, prefix)
    if source.is_dir() and index < len(paths) and paths[index].startswith(prefix):
        raise ValueError(f'launch source contains nested mounts: {source}')


def verify(manifest: dict) -> None:
    """Check the actual private namespace, including binds raced after preflight."""
    mounts = mount_table()
    expected = {Path(row['destination']): row for row in manifest['mounts']}
    private = {Path(p): fs for p, fs in {
        '/': 'tmpfs', '/proc': 'proc', '/dev': 'tmpfs', '/dev/pts': 'devpts',
        '/tmp': 'tmpfs', '/var/tmp': 'tmpfs',
    }.items()}
    devices = {Path('/dev') / name: os.makedev(major, minor) for name, major, minor in (
        ('null', 1, 3), ('zero', 1, 5), ('full', 1, 7), ('random', 1, 8),
        ('urandom', 1, 9), ('tty', 5, 0),
    )}
    unexpected = set(mounts) - set(expected) - set(private) - set(devices)
    if unexpected:
        raise ValueError(f'launch gained an unapproved mount: {sorted(unexpected)[0]}')
    for path, filesystem in private.items():
        if mounts.filesystems.get(path) != filesystem:
            raise ValueError(f'launch private filesystem changed: {path}')
    for path, device in devices.items():
        info = path.stat()
        if not stat.S_ISCHR(info.st_mode) or info.st_rdev != device:
            raise ValueError(f'launch private device changed: {path}')
    for destination, row in expected.items():
        info = destination.stat()
        if [info.st_dev, info.st_ino] != row['identity']:
            raise ValueError(f'launch mount identity changed: {destination}')
        if destination not in mounts:
            raise ValueError(f'launch mount missing: {destination}')
        if row['readonly'] and 'ro' not in mounts[destination]:
            raise ValueError(f'launch mount is writable: {destination}')
        if row['directory']:
            unexpected = [p for p in mounts if p != destination and p.is_relative_to(destination)
                          and p not in expected]
            if unexpected:
                raise ValueError(f'launch source gained an unapproved mount: {destination}')
    # Host preflight alone cannot catch links inserted before the namespace is
    # assembled. Recheck the visible writable scopes immediately before exec.
    for value in manifest['writable_scopes']:
        scope = Path(value)
        files = [scope] if scope.is_file() else (
            Path(root) / name for root, _, names in os.walk(scope, followlinks=False) for name in names
        )
        for path in files:
            if not path.is_symlink() and path.is_file() and path.stat().st_nlink > 1:
                raise ValueError(f'writable scope gained a hardlink: {path}')
    if 'ro' not in mounts.get(Path('/'), set()):
        raise ValueError('launch root is writable')


def main() -> None:
    try:
        manifest = json.loads(Path(sys.argv[1]).read_text())
        verify(manifest)
        command = sys.argv[2:]
        if not command:
            raise ValueError('missing launch command')
        os.execv(command[0], command)
    except (OSError, ValueError, KeyError) as exc:
        print(f'AGY namespace validation failed: {exc}', file=sys.stderr)
        raise SystemExit(125) from exc


if __name__ == '__main__':
    main()
