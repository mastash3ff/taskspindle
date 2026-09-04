"""The command line: what it prints, and what it exits with."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import taskspindle
from taskspindle import cli, doctor, setup


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every location TaskSpindle resolves at a directory the test owns."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("TASKSPINDLE_CONFIG", raising=False)
    return tmp_path


def test_version_prints_the_name_and_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"taskspindle {taskspindle.__version__}\n"


def test_no_command_prints_the_help_and_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "COMMAND" in capsys.readouterr().out


def test_taskspindle_config_overrides_the_xdg_config_file(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TASKSPINDLE_CONFIG", str(home / "elsewhere.toml"))

    resolved = cli.resolve_paths()

    assert resolved.config_file == home / "elsewhere.toml"
    assert resolved.state_dir == home / "state" / "taskspindle"


def _report(*checks: dict[str, Any]) -> dict[str, Any]:
    return {"ok": all(check["ok"] for check in checks if not check["advisory"]), "checks": list(checks)}


def _check(name: str, ok: bool, *, advisory: bool = False) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": f"{name} detail", "advisory": advisory}


def test_doctor_json_prints_the_report_and_an_advisory_failure_still_passes(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_doctor(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return _report(_check("git", True), _check("codex_registration", False, advisory=True))

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)

    assert cli.main(["doctor", "--no-live", "--json"]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert [check["name"] for check in printed["checks"]] == ["git", "codex_registration"]
    assert printed["ok"] is True
    assert seen["live_probes"] is False
    assert sorted(seen["profiles"]) == ["claude", "grok"]


def test_doctor_marks_each_check_and_fails_on_a_real_failure(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        doctor,
        "run_doctor",
        lambda **_: _report(
            _check("git", True),
            _check("node", False),
            _check("codex_registration", False, advisory=True),
        ),
    )

    assert cli.main(["doctor"]) == 1

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "[ok] git: git detail",
        "[FAIL] node: node detail",
        "[warn] codex_registration: codex_registration detail",
    ]


def test_a_broken_config_is_one_failed_check_rather_than_a_traceback(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = home / "config" / "taskspindle" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("this is not toml", encoding="utf-8")

    assert cli.main(["doctor", "--no-live", "--json"]) == 1

    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False
    assert [check["name"] for check in printed["checks"]] == ["config"]


def test_setup_prints_the_adapter_it_installed(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        setup,
        "install_runtime",
        lambda paths, **_: {
            "runtime_dir": str(paths.runtime_dir),
            "adapter_version": taskspindle.ADAPTER_VERSION,
            "config_file": str(paths.config_file),
            "created_config": True,
        },
    )

    assert cli.main(["setup", "--runtime-dir", str(home / "runtime")]) == 0

    out = capsys.readouterr().out
    assert f"{taskspindle.ADAPTER_PACKAGE} {taskspindle.ADAPTER_VERSION}" in out
    assert str(home / "runtime") in out
    assert "(written)" in out


def test_setup_failure_is_one_line_and_exit_one(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(paths: Any, **_: Any) -> dict[str, Any]:
        raise setup.SetupError("npm is not installed or not on PATH")

    monkeypatch.setattr(setup, "install_runtime", refuse)

    assert cli.main(["setup"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["taskspindle setup: npm is not installed or not on PATH"]


def test_worker_and_accept_delegate_to_the_unit_entry_points(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskspindle import accept, runner

    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(runner, "main", lambda argv: seen.append(("worker", argv)) or 0)
    monkeypatch.setattr(accept, "main", lambda argv: seen.append(("accept", argv)) or 3)

    assert cli.main(["worker", "--task", "ts_abc"]) == 0
    assert cli.main(["accept", "--task", "ts_abc"]) == 3
    assert seen == [("worker", ["--task", "ts_abc"]), ("accept", ["--task", "ts_abc"])]
