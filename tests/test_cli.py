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


def test_retired_subscriptions_command_is_rejected_without_creating_state(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["subscriptions", "status"])

    assert raised.value.code == 2
    assert "invalid choice: 'subscriptions'" in capsys.readouterr().err
    assert not (home / "state").exists()


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


def test_worker_doctor_does_not_open_live_state(home, monkeypatch, capsys):
    monkeypatch.setenv("TASKSPINDLE_WORKER_CONTAINER", "1")
    monkeypatch.setattr(cli, "_provider_status", lambda _: pytest.fail("live state was read"))
    seen = {}

    def probe(**kwargs):
        seen.update(kwargs)
        return _report(_check("worker_container", True))

    monkeypatch.setattr(doctor, "run_doctor", probe)
    assert cli.main(["doctor", "--json"]) == 0
    assert seen["provider_status"] == []
    assert json.loads(capsys.readouterr().out)["ok"] is True


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
    assert all(row["availability"]["state"] == "ok" for row in report["providers"])
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


def test_reprice_with_no_database_yet_examines_nothing(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["reprice", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["rows_examined"] == 0
    assert printed["rows_repriced"] == 0


def test_reprice_since_must_be_parseable(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["reprice", "--since", "yesterday"]) == 1
    assert "since must be" in capsys.readouterr().err


def test_reprice_prices_a_known_model_and_writes_it_back(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from taskspindle.store import Store
    from tests.test_usage import _seed_usage_row

    database = cli.resolve_paths().state_dir / "taskspindle.sqlite3"
    with Store.open(database) as store:
        _, turn_id = _seed_usage_row(store, "grok", model="grok-4.6", input_tokens=1_000_000)

    assert cli.main(["reprice", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["rows_examined"] == 1
    assert printed["rows_repriced"] == 1
    assert printed["repriced"][0]["turn_id"] == turn_id
    assert printed["repriced"][0]["old_cost_estimate_usd"] is None

    with Store.open(database) as store:
        row = store.get_turn_usage(turn_id)
    assert row["cost_estimate_usd"] is not None
    assert row["price_table_version"] is not None

    assert cli.main(["reprice"]) == 0
    text = capsys.readouterr().out
    assert "1 examined" in text
    assert "0 repriced" in text
    assert "estimate" in text.lower()


def test_reprice_dry_run_writes_nothing(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from taskspindle.store import Store
    from tests.test_usage import _seed_usage_row

    database = cli.resolve_paths().state_dir / "taskspindle.sqlite3"
    with Store.open(database) as store:
        _, turn_id = _seed_usage_row(store, "grok", model="grok-4.6", input_tokens=1_000_000)

    assert cli.main(["reprice", "--dry-run", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["dry_run"] is True
    assert printed["rows_repriced"] == 1

    with Store.open(database) as store:
        row = store.get_turn_usage(turn_id)
    assert row["cost_estimate_usd"] is None
    assert row["price_table_version"] is None


def test_reprice_since_and_provider_filter_the_rows(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from taskspindle.store import Store
    from tests.test_usage import _seed_usage_row

    database = cli.resolve_paths().state_dir / "taskspindle.sqlite3"
    with Store.open(database) as store:
        _seed_usage_row(store, "grok", model="grok-4.6", captured_at="2029-01-01T00:00:00.000000Z")
        _seed_usage_row(
            store, "claude", model="claude-sonnet-5", captured_at="2030-06-01T00:00:00.000000Z"
        )

    assert cli.main(["reprice", "--json", "--since", "2030-01-01T00:00:00Z"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["rows_examined"] == 1
    assert printed["repriced"][0]["model"] == "claude-sonnet-5"

    assert cli.main(["reprice", "--json", "--provider", "grok"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["rows_examined"] == 1
    assert printed["repriced"][0]["model"] == "grok-4.6"


def test_reprice_leaves_an_unknown_model_unpriced_in_human_output(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from taskspindle.store import Store
    from tests.test_usage import _seed_usage_row

    database = cli.resolve_paths().state_dir / "taskspindle.sqlite3"
    with Store.open(database) as store:
        _seed_usage_row(store, "claude", model="mystery-9")

    assert cli.main(["reprice"]) == 0
    out = capsys.readouterr().out
    assert "model unknown to the price table" in out
    assert "mystery-9" in out


def test_reprice_use_selected_model_prices_from_the_resolved_model_only_when_asked(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from taskspindle.store import Store
    from tests.test_usage import _seed_usage_row

    database = cli.resolve_paths().state_dir / "taskspindle.sqlite3"
    with Store.open(database) as store:
        _, turn_id = _seed_usage_row(
            store, "agy", model=None, input_tokens=1_000_000, resolved_model="gemini-3.1-pro-high",
        )

    assert cli.main(["reprice", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["rows_unpriced"] == 1
    assert printed["unpriced"][0]["reason"] == "no model recorded"

    assert cli.main(["reprice", "--json", "--use-selected-model"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["rows_unpriced"] == 0
    assert printed["repriced"][0]["model"] == "gemini-3.1-pro-high"
    assert printed["repriced"][0]["model_source"] == "task.resolved_model"

    with Store.open(database) as store:
        row = store.get_turn_usage(turn_id)
    assert row["model"] is None
    assert row["cost_estimate_usd"] is not None


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


def test_usage_with_group_by_role_includes_the_role_column(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from taskspindle.models import Mode, TaskState
    from taskspindle.store import Store
    from tests.test_usage import _seed

    with Store.open(cli.resolve_paths().state_dir / "taskspindle.sqlite3") as store:
        _seed(store, "claude", Mode.CONSULT, state=TaskState.COMPLETED,
              ms=1000, tokens=123, role="explorer")
    assert cli.main(["usage", "--group-by", "role"]) == 0
    output = capsys.readouterr().out
    assert "role" in output
    assert "explorer" in output
    assert "123" in output


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


def test_provider_text_status_shows_state_reset_and_reason(
    home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from taskspindle.store import Store

    database = home / "state/taskspindle/taskspindle.sqlite3"
    database.parent.mkdir(parents=True)
    with Store.open(database) as store:
        store.set_provider_status(
            "grok", "auth_expired", code="AUTHENTICATION_REQUIRED", source="acp_error",
            reason="Provider authentication is required.",
        )

    assert cli.main(["providers", "--provider", "grok"]) == 0
    output = capsys.readouterr().out
    assert "grok: auth_expired" in output
    assert "Eligible again:" in output
    assert "Reason: Provider authentication is required." in output


# -- policy ----------------------------------------------------------------------------


def test_policy_show_json_reports_the_defaults(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["policy", "show", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["source"] == "defaults"
    assert printed["revision"] == 0
    assert "claude" in printed["policy"]["providers"]
    assert "status" not in printed

    assert cli.main(["policy", "show", "--status", "--json"]) == 0
    with_status = json.loads(capsys.readouterr().out)
    assert "status" in with_status
    assert with_status["status"]["share_window"] == "week"


def test_policy_show_without_json_prints_provider_and_role_tables(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["policy", "show"]) == 0
    text = capsys.readouterr().out
    assert "policy revision 0 (defaults)" in text
    assert "providers" in text
    assert "claude" in text
    assert "roles" in text
    assert "mechanic" in text


def test_policy_set_round_trips_through_show(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["policy", "set", "providers.claude.target_share", "50"]) == 0
    capsys.readouterr()

    assert cli.main(["policy", "show", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["policy"]["providers"]["claude"]["target_share"] == 50
    assert printed["revision"] == 1
    assert printed["updated_by"] == "cli"

    # A value that is not valid JSON falls back to the raw string.
    assert cli.main(["policy", "set", "providers.claude.note", "a plain note"]) == 0
    capsys.readouterr()
    assert cli.main(["policy", "show", "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["policy"]["providers"]["claude"]["note"] == "a plain note"
    assert again["revision"] == 2


def test_policy_set_an_out_of_range_value_is_invalid_and_exits_one(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["policy", "set", "providers.claude.target_share", "150"]) == 1
    err = capsys.readouterr().err
    assert "providers.claude.target_share" in err


def test_policy_set_an_unresolvable_path_is_invalid_and_exits_one(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["policy", "set", "providers.nobody.enabled", "true"]) == 1
    err = capsys.readouterr().err
    assert "no such path" in err


def test_policy_export_and_import_round_trip(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["policy", "export"]) == 0
    exported = capsys.readouterr().out
    document = json.loads(exported)
    assert document["version"] == 1

    document["providers"]["claude"]["target_share"] = 25
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(document), encoding="utf-8")

    assert cli.main(["policy", "import", str(policy_file), "--if-revision", "0"]) == 0
    capsys.readouterr()

    assert cli.main(["policy", "show", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["policy"]["providers"]["claude"]["target_share"] == 25
    assert printed["revision"] == 1
    assert printed["source"] == "store"


def test_policy_import_a_revision_conflict_exits_three(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["policy", "set", "providers.claude.target_share", "10"]) == 0
    capsys.readouterr()

    assert cli.main(["policy", "export"]) == 0
    document = json.loads(capsys.readouterr().out)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(document), encoding="utf-8")

    assert cli.main(["policy", "import", str(policy_file), "--if-revision", "0"]) == 3
    err = capsys.readouterr().err
    assert "revision conflict" in err
    assert "1" in err


def test_policy_import_invalid_json_document_exits_one(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "providers": {"claude": {"target_share": 999}}}))

    assert cli.main(["policy", "import", str(bad)]) == 1
    err = capsys.readouterr().err
    assert err.strip() != ""


def test_policy_reset_restores_the_defaults_as_a_new_revision(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["policy", "set", "providers.claude.target_share", "50"]) == 0
    capsys.readouterr()

    assert cli.main(["policy", "reset"]) == 0
    capsys.readouterr()

    assert cli.main(["policy", "show", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["policy"]["providers"]["claude"]["target_share"] is None
    assert printed["revision"] == 2
    assert printed["updated_by"] == "cli"


def test_policy_reset_a_revision_conflict_exits_three(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["policy", "reset", "--if-revision", "5"]) == 3
    err = capsys.readouterr().err
    assert "revision conflict" in err


def test_policy_without_a_subcommand_prints_usage_and_exits_two(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["policy"])
    assert raised.value.code == 2
    assert "policy requires a subcommand" in capsys.readouterr().err
