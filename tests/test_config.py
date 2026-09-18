"""XDG resolution and config.toml loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskspindle.config import ConfigError, ContextFilesConfig, context_files_config, load_config, paths


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


def test_context_files_are_off_without_the_table() -> None:
    assert context_files_config({}) is None


def test_context_files_table_resolves_roots_and_keeps_defaults(tmp_path: Path) -> None:
    root = tmp_path / "ctx"
    root.mkdir()
    cfg = context_files_config({"context_files": {"roots": [str(root)]}})
    assert cfg == ContextFilesConfig(roots=(root.resolve(),))
    assert (cfg.max_file_bytes, cfg.max_total_bytes, cfg.max_files) == (262144, 1048576, 16)


def test_context_files_limits_are_taken_when_within_ceilings(tmp_path: Path) -> None:
    root = tmp_path / "ctx"
    root.mkdir()
    cfg = context_files_config(
        {"context_files": {"roots": [str(root)], "max_file_bytes": 10, "max_total_bytes": 20, "max_files": 2}}
    )
    assert (cfg.max_file_bytes, cfg.max_total_bytes, cfg.max_files) == (10, 20, 2)


@pytest.mark.parametrize(
    "table",
    [
        "not a table",
        {},
        {"roots": []},
        {"roots": "relative"},
        {"roots": ["relative/dir"]},
        {"roots": [42]},
        {"roots": ["/definitely/not/a/directory/anywhere"]},
        {"roots": ["ROOT"], "max_file_bytes": 0},
        {"roots": ["ROOT"], "max_file_bytes": 2 * 1024 * 1024},
        {"roots": ["ROOT"], "max_total_bytes": True},
        {"roots": ["ROOT"], "max_files": 33},
        {"roots": ["ROOT"], "surprise": 1},
    ],
)
def test_malformed_context_files_tables_are_refused(tmp_path: Path, table: object) -> None:
    root = tmp_path / "ctx"
    root.mkdir()
    if isinstance(table, dict) and table.get("roots") == ["ROOT"]:
        table = {**table, "roots": [str(root)]}
    with pytest.raises(ConfigError, match=r"\[context_files\]"):
        context_files_config({"context_files": table})


def test_a_root_that_is_a_file_names_the_docker_mount_rule(tmp_path: Path) -> None:
    target = tmp_path / "file.md"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="execution"):
        context_files_config({"context_files": {"roots": [str(target)]}})
