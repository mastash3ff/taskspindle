"""OAuth context fingerprints are location and metadata based, never credential based."""

from __future__ import annotations

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
def test_fingerprint_changes_when_credential_metadata_changes(tmp_path: Path, family: str) -> None:
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
    location.write_bytes(b"second fixture credential with changed metadata")

    assert auth_context.fingerprint(profile, parent) != before


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

    assert len(value) == 64
    assert "PRIVATE" not in value


def test_missing_credential_has_a_stable_fingerprint_and_creates_nothing(tmp_path: Path) -> None:
    profile = _profiles(tmp_path)["agy"]
    parent = _parent(tmp_path / "no-login")
    before = auth_context.fingerprint(profile, parent)

    assert auth_context.fingerprint(profile, parent) == before
    assert not (Path(parent["HOME"]) / ".gemini").exists()


def test_same_profile_file_change_is_observable_without_a_context_conflict(tmp_path: Path) -> None:
    profiles = _profiles(tmp_path)
    parent = _parent(tmp_path / "home")
    credential = Path(profiles["claude"].env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_bytes(b"one")
    before = auth_context.fingerprint(profiles["claude"], parent)
    credential.write_bytes(b"two credential bytes")

    auth_context.validate_contexts({"claude": profiles["claude"]}, parent)
    assert auth_context.fingerprint(profiles["claude"], parent) != before
