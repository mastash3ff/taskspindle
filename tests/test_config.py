"""XDG resolution and config.toml loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskspindle.config import ConfigError, load_config, paths


def test_xdg_overrides_are_honoured(tmp_path: Path) -> None:
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    resolved = paths(env, version="9.9.9")

    assert resolved.config_file == tmp_path / "cfg" / "taskspindle" / "config.toml"
    assert resolved.state_dir == tmp_path / "state" / "taskspindle"
    assert resolved.data_dir == tmp_path / "data" / "taskspindle"
    assert resolved.runtime_dir == tmp_path / "data" / "taskspindle" / "runtimes" / "9.9.9"


def test_defaults_fall_back_to_home(tmp_path: Path) -> None:
    resolved = paths({"HOME": str(tmp_path)}, version="1.2.3")

    assert resolved.config_file == tmp_path / ".config" / "taskspindle" / "config.toml"
    assert resolved.state_dir == tmp_path / ".local" / "state" / "taskspindle"
    assert resolved.runtime_dir == tmp_path / ".local" / "share" / "taskspindle" / "runtimes" / "1.2.3"


def test_missing_config_is_empty(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nowhere" / "config.toml") == {}


def test_config_is_parsed(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[providers.local]\nauth = "api_key"\n', encoding="utf-8")

    assert load_config(config_file) == {"providers": {"local": {"auth": "api_key"}}}


def test_broken_config_raises(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("this is not = = toml\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(config_file)
