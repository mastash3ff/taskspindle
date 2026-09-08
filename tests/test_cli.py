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
    assert sorted(seen["profiles"]) == ["agy", "claude", "grok"]


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


def test_providers_reads_cached_status_without_creating_task_database(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import subprocess

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("cached provider status must not start a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert cli.main(["providers", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert {row["id"] for row in report["providers"]} == {"claude", "grok", "agy"}
    assert all(row["availability"]["state"] == "unknown" for row in report["providers"])
    assert all("native_check" in row for row in report["providers"])
    assert all(row["native_check"]["eligible_hint"] is None for row in report["providers"])
    assert not (home / "state/taskspindle/taskspindle.sqlite3").exists()


def test_provider_check_does_not_clear_recorded_refusal(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from taskspindle import access_checks, grok_checks
    from taskspindle.store import Store

    database = home / "state/taskspindle/taskspindle.sqlite3"
    database.parent.mkdir(parents=True)
    with Store.open(database) as store:
        store.set_provider_status("claude", "auth_expired", source="acp_error", reason="private-value")
        before = store.get_provider_status("claude")
    monkeypatch.setattr(grok_checks, "check_grok", lambda *_: {
        "state": "unsupported", "error_code": "METHOD_UNAVAILABLE", "source": "grok_billing",
    })
    monkeypatch.setattr(access_checks, "check_native_access", lambda *_: {
        "state": "cached_auth", "detail": "cached claim only", "account_binding": "unverified",
    })
    assert cli.main(["providers", "--check", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    claude = next(row for row in report["providers"] if row["id"] == "claude")
    assert claude["availability"]["state"] == "auth_expired"
    assert claude["native_check"]["state"] == "cached_auth"
    assert "private-value" not in output
    with Store.open(database) as store:
        assert store.get_provider_status("claude") == before


def test_usage_prints_the_report_as_json_or_as_tables(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["usage", "--json", "--since", "7d", "--group-by", "day"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["group_by"] == "day"
    assert printed["usage"] == []
    assert printed["turns"]["count"] == 0
    assert {entry["provider"] for entry in printed["windows"]} == {"agy", "claude", "grok"}

    assert cli.main(["usage"]) == 0
    text = capsys.readouterr().out
    assert "(nothing recorded)" in text
    assert "claude: unknown" in text

    assert cli.main(["usage", "--since", "yesterday"]) == 1
    assert "since must be" in capsys.readouterr().err


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
            "node": "/opt/fakenode/bin/node",
            "config_file": str(paths.config_file),
            "created_config": True,
        },
    )

    assert cli.main(["setup", "--runtime-dir", str(home / "runtime")]) == 0

    out = capsys.readouterr().out
    assert f"{taskspindle.ADAPTER_PACKAGE} {taskspindle.ADAPTER_VERSION}" in out
    assert str(home / "runtime") in out
    assert "/opt/fakenode/bin/node" in out
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


def test_web_parses_defaults_and_calls_serve(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from taskspindle import web

    seen: dict[str, Any] = {}

    def fake_serve(paths: Any, profiles: Any, *, host: str, port: int, open_browser: bool) -> int:
        seen["host"] = host
        seen["port"] = port
        seen["open_browser"] = open_browser
        seen["profiles"] = sorted(profiles)
        return 0

    monkeypatch.setattr(web, "serve", fake_serve)

    assert cli.main(["web"]) == 0

    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8765
    assert seen["open_browser"] is False
    assert seen["profiles"] == ["agy", "claude", "grok"]



@pytest.mark.parametrize(("display_path", "label"), [("/repo", "/repo"), ("", "repo-example")])
def test_repository_usage_prints_the_repository_label(home: Path, capsys, display_path, label) -> None:
    from taskspindle.models import Mode, TaskState
    from taskspindle.store import Store
    from tests.test_usage import _seed

    with Store.open(cli.resolve_paths().state_dir / "taskspindle.sqlite3") as store:
        store.insert_repository("repo-example", "/repo/.git", "root", display_path)
        _seed(store, "claude", Mode.CONSULT, state=TaskState.COMPLETED,
              ms=1000, tokens=123, repository_id="repo-example")
    assert cli.main(["usage", "--group-by", "repository_id"]) == 0
    output = capsys.readouterr().out
    assert "repository" in output
    assert label in output
    assert "123" in output


def test_concurrency_rollback_cli_is_explicit_and_preserves_records(home, capsys, monkeypatch):
    from taskspindle import store as store_module
    from taskspindle.models import TaskState
    from taskspindle.store import Store
    from tests.test_store import make_task

    database = home / "fixture.sqlite3"
    # This maintenance command is deliberately specific to the old v4 lease layout.
    monkeypatch.setattr(store_module, "MIGRATIONS", [m for m in store_module.MIGRATIONS if m[0] <= 4])
    with Store.open(database) as store:
        task = make_task(store)
        assert cli.main(["rollback-concurrency", "--database", str(database)]) == 1
        assert store.schema_version() == 4
        store.update_task(task.id, None, state=TaskState.CANCELLED)
    assert cli.main(["rollback-concurrency", "--database", str(database)]) == 0
    with Store(database) as store:
        assert store.schema_version() == 3
        assert store.get_task(task.id).state == TaskState.CANCELLED
    assert "history and grants preserved" in capsys.readouterr().out


def test_concurrency_rollback_refuses_newer_schema_without_mutation(home):
    from taskspindle.store import Store

    database = home / "fixture.sqlite3"
    with Store.open(database) as store:
        version = store.schema_version()
        assert version > 4
        assert cli.main(["rollback-concurrency", "--database", str(database)]) == 1
        assert store.schema_version() == version


def test_concurrency_rollback_does_not_create_missing_database(home):
    path = home / "absent.sqlite3"
    assert cli.main(["rollback-concurrency", "--database", str(path)]) == 1
    assert not path.exists()


def test_provider_filter_check_and_unknown_filter(home, monkeypatch, capsys):
    from taskspindle import grok_checks
    calls = []
    def fake(profile, env):
        calls.append(profile.id)
        return {"state": "unsupported", "source": "grok_billing", "error_code": "METHOD_UNAVAILABLE"}
    monkeypatch.setattr(grok_checks, "check_grok", fake)
    assert cli.main(["providers", "--check", "--provider", "grok", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in report["providers"]] == ["grok"]
    assert calls == ["grok"]
    assert cli.main(["providers", "--check", "--provider", "grok", "--json"]) == 0
    capsys.readouterr()
    assert calls == ["grok"]
    assert cli.main(["providers", "--check", "--provider", "does-not-exist", "--json"]) == 1
    assert calls == ["grok"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["providers", "--retry-next", "--check", "--provider", "grok"],
        ["providers", "--retry-next", "--revoke-retry", "rp_example", "--provider", "grok"],
        ["providers", "--check", "--revoke-retry", "rp_example"],
        ["providers", "--retry-next"],
        ["providers", "--retry-next", "--provider", "grok", "--model", ""],
        ["providers", "--model", "grok-4"],
        ["providers", "--revoke-retry", "rp_example", "--provider", "grok"],
    ],
)
def test_provider_recovery_flags_reject_conflicting_or_incomplete_actions(
    arguments: list[str],
) -> None:
    """Removing an action conflict or prerequisite check would admit an ambiguous mutation."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(arguments)
    assert excinfo.value.code == 2


def test_retry_next_uses_the_current_cached_revision_without_running_a_check(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Using a caller-supplied/stale revision, or refreshing evidence, breaks bounded consent."""
    import subprocess

    from taskspindle import provider_recovery
    from taskspindle.store import Store

    database = home / "state/taskspindle/taskspindle.sqlite3"
    database.parent.mkdir(parents=True)
    with Store.open(database) as store:
        store.set_provider_status(
            "grok", "auth_expired", code="AUTHENTICATION_REQUIRED", source="acp_error"
        )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("arming from cached evidence must not start a process")

    seen: dict[str, Any] = {}

    def arm(store, profile, *, evidence_revision, now, model=None, parent_env=None):
        seen.update(
            profile=profile.id,
            evidence_revision=evidence_revision,
            model=model,
            parent_env=parent_env,
        )
        return {"state": "armed", "permit_id": "rp_example", "evidence_revision": evidence_revision}

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(provider_recovery, "arm", arm)

    assert cli.main(["providers", "--retry-next", "--provider", "grok", "--model", "grok-4", "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "armed"
    assert result["permit_id"] == "rp_example"
    assert seen["profile"] == "grok"
    assert seen["model"] == "grok-4"
    assert seen["evidence_revision"]


def test_provider_text_status_shows_controlled_recovery_fields(
    home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting the permit projection would leave non-JSON operators unable to use recovery."""
    from taskspindle.store import Store

    database = home / "state/taskspindle/taskspindle.sqlite3"
    database.parent.mkdir(parents=True)
    with Store.open(database) as store:
        store.set_provider_status(
            "grok", "auth_expired", code="AUTHENTICATION_REQUIRED", source="acp_error"
        )

    assert cli.main(["providers", "--provider", "grok"]) == 0
    output = capsys.readouterr().out
    assert "Evidence revision: " in output
    assert "Recovery: none" in output
    assert "Recovery permit: -" in output
    assert "Recovery expires: -" in output
    assert "Recovery task: -" in output
    assert "Recovery outcome: -" in output
    assert "Recovery next action: arm" in output


def test_revoke_retry_revokes_by_permit_id_without_loading_profiles(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A revoke must not depend on provider configuration or perform provider work."""
    from taskspindle import provider_recovery

    seen: dict[str, Any] = {}

    def revoke(store, permit_id, *, now):
        seen.update(permit_id=permit_id, database=store.path)
        return {"state": "revoked", "permit_id": permit_id}

    monkeypatch.setattr(cli, "_profiles", lambda _: pytest.fail("revoke must not load profiles"))
    monkeypatch.setattr(provider_recovery, "revoke", revoke)

    assert cli.main(["providers", "--revoke-retry", "rp_example", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"permit_id": "rp_example", "state": "revoked"}
    assert seen["permit_id"] == "rp_example"


def test_revoke_retry_reports_a_safe_error_without_a_traceback(
    home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing permit is an operator-safe recovery code, never an implementation traceback."""
    assert cli.main(["providers", "--revoke-retry", "rp_absent", "--json"]) == 1
    output = capsys.readouterr()
    error = json.loads(output.out)["error"]
    assert error["code"] == "RECOVERY_NOT_FOUND"
    assert output.err == ""
    assert "traceback" not in output.out.lower()
