"""OAuth context fingerprints identify a credential's location and inode, never its freshness."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from taskspindle import auth_context, providers
from taskspindle.providers import ProfileError


def _profiles(tmp_path: Path):
    return providers.builtin_profiles(
        tmp_path / "runtime", home=tmp_path / "configured-home", state_dir=tmp_path / "state",
    )


def _parent(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return {"HOME": str(home), "PATH": "/usr/bin:/bin"}


def test_aliases_of_one_family_share_the_builtin_auth_context(tmp_path: Path) -> None:
    profiles = _profiles(tmp_path)
    parent = _parent(tmp_path / "home")
    alias = replace(profiles["claude"], id="claude-alias", base="claude", first_class=False)

    assert auth_context.fingerprint(profiles["claude"], parent) == auth_context.fingerprint(alias, parent)
    auth_context.validate_contexts({"claude": profiles["claude"], alias.id: alias}, parent)


@pytest.mark.parametrize(
    ("family", "override"),
    [("claude", "CLAUDE_CONFIG_DIR"), ("grok", "GROK_HOME")],
)
def test_alias_with_a_different_auth_root_is_rejected(tmp_path: Path, family: str, override: str) -> None:
    profiles = _profiles(tmp_path)
    parent = _parent(tmp_path / "home")
    builtin = profiles[family]
    env = dict(builtin.env)
    env[override] = str(tmp_path / "other-login")
    alias = replace(builtin, id=f"{family}-alias", base=family, first_class=False, env=env)

    with pytest.raises(ProfileError) as raised:
        auth_context.validate_contexts({family: builtin, alias.id: alias}, parent)
    assert raised.value.code == "AUTH_CONTEXT_CONFLICT"
    assert str(raised.value) == (
        "OAuth profiles in the same provider family must use the same authentication context."
    )


def test_load_profiles_validates_alias_context_against_its_worker_parent(tmp_path: Path) -> None:
    parent = _parent(tmp_path / "home")
    with pytest.raises(ProfileError, match="same provider family") as raised:
        providers.load_profiles(
            {"providers": {"other": {
                "base": "claude", "auth": "oauth",
                "env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "other-login")},
            }}},
            runtime_dir=tmp_path / "runtime", home=tmp_path / "configured-home",
            state_dir=tmp_path / "state", parent_env=parent,
        )
    assert raised.value.code == "AUTH_CONTEXT_CONFLICT"


def test_different_parent_home_changes_agy_context(tmp_path: Path) -> None:
    profile = _profiles(tmp_path)["agy"]
    first = _parent(tmp_path / "first-home")
    second = _parent(tmp_path / "second-home")

    assert auth_context.fingerprint(profile, first) != auth_context.fingerprint(profile, second)


@pytest.mark.parametrize("family", ["claude", "grok"])
def test_non_allowlisted_parent_auth_roots_do_not_change_worker_context(tmp_path: Path, family: str) -> None:
    profile = _profiles(tmp_path)[family]
    parent = _parent(tmp_path / "home")
    changed = {**parent, "CLAUDE_CONFIG_DIR": str(tmp_path / "wrong-claude"),
               "GROK_HOME": str(tmp_path / "wrong-grok")}

    assert auth_context.fingerprint(profile, parent) == auth_context.fingerprint(profile, changed)


@pytest.mark.parametrize("family", ["claude", "grok", "agy"])
def test_fingerprint_is_stable_across_an_in_place_credential_refresh(tmp_path: Path, family: str) -> None:
    """A token refresh rewrites the same file in place, changing size and both timestamps.

    The fingerprint must not change, since it is still the same login at the same location.
    """
    profile = _profiles(tmp_path)[family]
    parent = _parent(tmp_path / "home")
    if family == "claude":
        location = Path(profile.env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    elif family == "grok":
        location = Path(parent["HOME"]) / ".grok" / "auth.json"
    else:
        location = Path(parent["HOME"]) / ".gemini/antigravity-cli/antigravity-oauth-token"
    location.parent.mkdir(parents=True)
    location.write_bytes(b"first fixture credential")
    before = auth_context.fingerprint(profile, parent)
    location.write_bytes(b"second fixture credential, refreshed in place, different size and mtime")

    assert auth_context.fingerprint(profile, parent) == before


def test_fingerprint_never_reads_credential_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profiles(tmp_path)["grok"]
    parent = _parent(tmp_path / "home")
    credential = Path(parent["HOME"]) / ".grok" / "auth.json"
    credential.parent.mkdir(parents=True)
    credential.write_text("PRIVATE_TOKEN_MUST_NOT_BE_READ", encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("auth-context fingerprint read a credential")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    value = auth_context.fingerprint(profile, parent)

    assert value.startswith("2:") and len(value) == len("2:") + 64
    assert "PRIVATE" not in value


def test_missing_credential_has_a_stable_fingerprint_and_creates_nothing(tmp_path: Path) -> None:
    profile = _profiles(tmp_path)["agy"]
    parent = _parent(tmp_path / "no-login")
    before = auth_context.fingerprint(profile, parent)

    assert auth_context.fingerprint(profile, parent) == before
    assert not (Path(parent["HOME"]) / ".gemini").exists()


def test_same_profile_file_change_is_observable_without_a_context_conflict(tmp_path: Path) -> None:
    """An in-place refresh of the bound credential is not a context conflict.

    It also does not change the fingerprint ``validate_contexts`` would compare against a stored
    value.
    """
    profiles = _profiles(tmp_path)
    parent = _parent(tmp_path / "home")
    credential = Path(profiles["claude"].env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_bytes(b"one")
    before = auth_context.fingerprint(profiles["claude"], parent)
    credential.write_bytes(b"two credential bytes")

    auth_context.validate_contexts({"claude": profiles["claude"]}, parent)
    assert auth_context.fingerprint(profiles["claude"], parent) == before


def test_a_different_resolved_path_changes_the_fingerprint(tmp_path: Path) -> None:
    profile = _profiles(tmp_path)["claude"]
    parent = _parent(tmp_path / "home")
    other = replace(profile, env={**profile.env, "CLAUDE_CONFIG_DIR": str(tmp_path / "other-config")})

    assert auth_context.fingerprint(profile, parent) != auth_context.fingerprint(other, parent)


def test_a_different_inode_at_the_same_path_changes_the_fingerprint(tmp_path: Path) -> None:
    profile = _profiles(tmp_path)["claude"]
    parent = _parent(tmp_path / "home")
    location = Path(profile.env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    location.parent.mkdir(parents=True)
    location.write_bytes(b"first login")
    before_ino = location.stat().st_ino
    before = auth_context.fingerprint(profile, parent)

    replacement = location.with_name(".credentials.json.new")
    replacement.write_bytes(b"second login")
    if replacement.stat().st_ino == before_ino:
        pytest.skip("filesystem reused the same inode across os.replace")
    os.replace(replacement, location)
    assert location.stat().st_ino != before_ino

    assert auth_context.fingerprint(profile, parent) != before


def test_fingerprint_has_a_version_prefix(tmp_path: Path) -> None:
    profile = _profiles(tmp_path)["agy"]
    parent = _parent(tmp_path / "no-login")
    value = auth_context.fingerprint(profile, parent)

    assert value.startswith("2:")
    assert len(value) == len("2:") + 64


def test_matches_skips_comparison_for_a_stored_value_from_another_version(tmp_path: Path) -> None:
    profile = _profiles(tmp_path)["claude"]
    parent = _parent(tmp_path / "home")
    current = auth_context.fingerprint(profile, parent)
    stale = "1:" + "0" * 64

    assert auth_context.matches(stale, current) is True
    assert auth_context.matches(None, current) is True


def test_matches_rejects_a_genuine_version_2_mismatch(tmp_path: Path) -> None:
    profile = _profiles(tmp_path)["claude"]
    parent = _parent(tmp_path / "home")
    current = auth_context.fingerprint(profile, parent)
    version, digest = current.split(":", 1)
    flipped_digest = ("1" if digest[0] != "1" else "2") + digest[1:]
    different = f"{version}:{flipped_digest}"

    assert current != different
    assert auth_context.matches(different, current) is False
    assert auth_context.matches(current, current) is True
