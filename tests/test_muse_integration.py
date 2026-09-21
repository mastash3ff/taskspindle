"""Muse admission and the existing task lifecycle, without subscription credentials."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskspindle import access_checks, muse, policy, providers, runner, service, usage
from taskspindle.acp_client import AcpError, InitInfo, TurnCapture, TurnResult
from taskspindle.config import Paths, concurrency_limits
from taskspindle.models import Mode, TaskState
from taskspindle.providers import Profile, ProfileError
from tests.test_runner import BOOT, seed_task
from tests.test_store import make_store


def profiles(tmp_path):
    return providers.builtin_profiles(
        tmp_path / "runtime", home=tmp_path / "home", state_dir=tmp_path / "state",
    )


@pytest.mark.parametrize("mode", ["consult", "review", "implement"])
@pytest.mark.parametrize("metered", [False, True])
def test_muse_admission_cannot_override_qualification(tmp_path, mode, metered):
    with pytest.raises(ProfileError, match="subscription-only") as error:
        providers.profile_for_task(profiles(tmp_path), "muse", mode=mode, allow_metered=metered)
    assert error.value.code == "MUSE_NOT_QUALIFIED"


def test_registration_is_pinned_disabled_and_single_flight(tmp_path):
    all_profiles = profiles(tmp_path)
    profile = all_profiles["muse"]
    assert profile.auth == "subscription"
    assert profile.first_class
    assert profile.command == (str(tmp_path / "runtime" / "muse"),)
    assert providers.adapter_metadata(profile)["protocol"] == "msp"
    assert concurrency_limits({}, all_profiles)["muse"] == 1
    defaults = policy.defaults(all_profiles)
    assert defaults.providers["muse"].enabled is False
    assert all("muse" not in role.provider_preference for role in defaults.roles.values())
    assert muse.qualification()["qualified_modes"] == []


def test_muse_no_secret_or_foreign_configuration_environment(tmp_path):
    parent = {
        "HOME": str(tmp_path / "operator"), "PATH": "/usr/bin",
        "META_API_KEY": "secret", "MODEL_API_KEY": "secret", "MUSE_API_KEY": "secret",
        "CODEX_HOME": "/private", "CLAUDE_CONFIG_DIR": "/private",
        "XDG_DATA_HOME": "/private", "META_BASE_URL": "https://example.invalid",
    }
    profile = profiles(tmp_path)["muse"]
    child = providers.build_child_env(profile, parent, task_tmp=tmp_path / "tmp")
    child = muse.isolated_environment(tmp_path / "task", child)
    assert not any(key in child for key in ("META_API_KEY", "MODEL_API_KEY", "MUSE_API_KEY",
                                          "CODEX_HOME", "CLAUDE_CONFIG_DIR", "META_BASE_URL"))
    assert child["HOME"] != parent["HOME"]
    assert Path(child["XDG_DATA_HOME"]).is_relative_to(tmp_path / "task")
    assert "--disable-write" in providers.launch_command(profile, "review")
    assert "--disable-shell" in providers.launch_command(profile, "consult")


@pytest.mark.parametrize("auth", ["oauth", "api_key", "subscription"])
def test_muse_alias_cannot_replace_auth_or_command(tmp_path, auth):
    with pytest.raises(ProfileError, match="Muse aliases"):
        providers.load_profiles(
            {"providers": {"other-muse": {"base": "muse", "auth": auth}}},
            runtime_dir=tmp_path / "runtime", home=tmp_path, state_dir=tmp_path / "state",
        )


def test_muse_cached_state_does_not_imply_eligibility(tmp_path):
    profile = profiles(tmp_path)["muse"]
    with make_store(tmp_path) as store:
        available = service.provider_availability(store, profile, now=datetime.now(UTC))
        assert available["state"] == "unqualified"
        assert service.provider_eligible(available, datetime.now(UTC)) is False
        check = access_checks.cached_native_check(store, profile, {})
        assert check["account_binding"] == "unverified"
        assert check["source"] == "muse_qualification"
        assert "disabled" in check["detail"]


def test_capabilities_explain_disabled_subscription_provider(tmp_path):
    from taskspindle.orchestrator import Orchestrator
    from tests.fakes.units import FakeUnitBackend

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    with make_store(tmp_path) as store:
        orch = Orchestrator(store=store, paths=paths, profiles=profiles(tmp_path),
                            units=FakeUnitBackend(), boot=BOOT, parent_env={})
        entry = next(item for item in orch.capabilities()["providers"] if item["id"] == "muse")
        assert entry["auth"] == "subscription"
        assert entry["qualification"]["enabled"] is False
        assert entry["qualification"]["subscription_route"] == "unverified"
        assert entry["availability"]["state"] == "unqualified"
        assert entry["policy"]["enabled"] is False


def test_auth_muse_reports_gate_without_login(monkeypatch, capsys):
    import subprocess

    from taskspindle import cli

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("must not authenticate"))
    assert cli.main(["auth", "muse"]) == 1
    assert "disabled" in capsys.readouterr().err


def test_muse_setup_requires_explicit_native_binary():
    from taskspindle import cli

    with pytest.raises(SystemExit) as error:
        cli.main(["setup", "--provider", "muse"])
    assert error.value.code == 2


def test_muse_auth_failure_retains_structured_provenance_in_public_status():
    from taskspindle import limits

    verdict = limits.classify_acp_error(AcpError(
        "PROVIDER_AUTH_EXPIRED", "Muse authentication is required.",
        cause={"msp_error_kind": "authRequired"},
    ), family="muse")
    public = limits.safe_status_row({"state": verdict.provider_state, "source": verdict.source})
    assert public["state"] == "auth_expired"
    assert public["source"] == "muse_msp_terminal"


async def test_direct_worker_is_gated_before_transport(tmp_path, monkeypatch):
    from taskspindle import muse_msp

    monkeypatch.setattr(muse_msp, "MuseWorker", lambda **kw: pytest.fail("must not spawn"))
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    profile = Profile(id="fake", base="muse", auth="subscription", billing_type="subscription",
                      command=("muse",))
    with make_store(tmp_path) as store:
        task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="muse")
        state = await runner.run_worker(
            store, task.id, profiles={"fake": profile}, paths=paths, boot=BOOT, signals=False,
        )
        assert state is TaskState.FAILED
        assert store.get_task(task.id).error["code"] == "MUSE_NOT_QUALIFIED"


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_muse_runner_journals_before_prompt_and_preserves_uncertainty(tmp_path, monkeypatch, ambiguous):
    from taskspindle import muse_msp

    # Synthetic transport qualification only. Production has no enabling config switch.
    monkeypatch.setattr(muse, "qualification", lambda: {"enabled": True})
    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    profile = Profile(id="fake", base="muse", auth="subscription", billing_type="subscription",
                      command=("/pinned/muse",))
    calls = []
    session_id = "01990000-0000-7000-8000-000000000001"
    command_id = "01990000-0000-7000-8000-000000000002"

    class Worker:
        init = InitInfo(True, (), {"name": "muse", "version": "1.3.0"})
        session_model = "muse-test"
        session_effort = None
        last_result = None
        command_settled = False

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.cwd = kwargs["cwd"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def new_session(self):
            self.kwargs["on_session"](session_id)
            return session_id

        async def prompt(self, sid, text, *, timeout):
            calls.append(sid)
            assert sid == session_id
            assert store.get_task(task.id).session_id == session_id
            self.kwargs["on_command"](command_id)
            self.command_id = command_id
            events = store.list_events(task.id)
            assert any(event["payload"].get("command_id") == command_id for event in events)
            self.last_result = TurnResult("end_turn", "answer", TurnCapture(), {
                "input_tokens": 12, "output_tokens": 3, "_muse_msp": True,
            })
            if ambiguous:
                raise AcpError("RECOVERY_AMBIGUOUS", "Turn start acknowledgment was lost")
            self.command_settled = True
            return self.last_result

    monkeypatch.setattr(muse_msp, "MuseWorker", Worker)
    with make_store(tmp_path) as store:
        task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="muse")
        state = await runner.run_worker(
            store, task.id, profiles={"fake": profile}, paths=paths, boot=BOOT, signals=False,
        )
        assert state is (TaskState.RECOVERY_AMBIGUOUS if ambiguous else TaskState.COMPLETED)
        assert calls == [session_id]
        saved = store.get_task(task.id)
        assert saved.resolved_model == "muse-test"
        assert saved.reported_model is None
        assert saved.session_id == session_id
        assert store.list_turn_usage(task_id=task.id)[0]["source"] == "muse_msp"
        assert muse.unresolved_commands(store, saved) is ambiguous


def test_muse_selected_model_is_not_reported_as_observed(tmp_path):
    result = TurnResult("end_turn", "answer", TurnCapture(), {
        "input_tokens": 10, "output_tokens": 4, "_muse_msp": True,
    })
    profile = Profile(id="muse", auth="subscription", billing_type="subscription",
                      command=("muse",), model="requested")
    collected = usage.collect(result, profile=profile, cwd=tmp_path, home=tmp_path,
                              session_id=None, duration_ms=1)
    assert collected.model is None
    assert collected.usage.source == "muse_msp"
    assert collected.usage.cost_estimate_usd is None


@pytest.mark.parametrize("failure", ["signal", "oom", "exit", "missing", "reboot"])
@pytest.mark.parametrize("outcome_saved", [False, True])
def test_crashed_muse_command_requires_authoritative_outcome(tmp_path, failure, outcome_saved):
    from taskspindle import recovery
    from taskspindle.units import worker_unit_name
    from tests.fakes.units import EXITED, NOT_FOUND, OOM, SIGNALLED, FakeUnitBackend

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    with make_store(tmp_path) as store:
        task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="muse")
        task = store.update_task(
            task.id, None, state=TaskState.RUNNING, unit_name=worker_unit_name(task.id),
            boot_id="previous-boot" if failure == "reboot" else BOOT,
            heartbeat_at="2020-01-01T00:00:00Z",
        )
        store.append_event(task.id, "WARNING", {"code": "MUSE_COMMAND_INTENT", "command_id": "one"})
        if outcome_saved:
            store.append_event(task.id, "WARNING", {"code": "MUSE_COMMAND_OUTCOME", "command_id": "one"})
        state = {"signal": SIGNALLED, "oom": OOM, "exit": EXITED, "missing": NOT_FOUND,
                 "reboot": NOT_FOUND}[failure]
        backend = FakeUnitBackend({worker_unit_name(task.id): state})
        recovery.reconcile(store, backend, boot=BOOT, now=datetime.now(UTC))
        saved = store.get_task(task.id)
        if outcome_saved:
            assert saved.state in {TaskState.INTERRUPTED, TaskState.FAILED}
        else:
            assert saved.state is TaskState.RECOVERY_AMBIGUOUS
            assert muse.unresolved_commands(store, saved)


def test_unresolved_muse_cannot_resume_into_a_fresh_command(tmp_path):
    from taskspindle.orchestrator import Orchestrator
    from tests.fakes.units import FakeUnitBackend

    paths = Paths(tmp_path / "config", tmp_path / "state", tmp_path / "data", tmp_path / "runtime")
    with make_store(tmp_path) as store:
        task = seed_task(store, paths, mode=Mode.CONSULT, provider_family="muse")
        task = store.update_task(task.id, None, state=TaskState.RECOVERY_AMBIGUOUS)
        store.release_lease(task.provider, task.id)
        store.append_event(task.id, "WARNING", {"code": "MUSE_COMMAND_INTENT", "command_id": "one"})
        profile = Profile(id="fake", base="muse", auth="subscription", billing_type="subscription",
                      command=("/pinned/muse",))
        orch = Orchestrator(store=store, paths=paths, profiles={"fake": profile},
                            units=FakeUnitBackend(), boot=BOOT, parent_env={})
        before = store.list_turns(task.id)
        with pytest.raises(service.TaskSpindleError) as error:
            orch.continue_task(task_id=task.id, expected_state_version=task.state_version, prompt="resume")
        assert error.value.code == "MANUAL_RECOVERY_REQUIRED"
        assert store.list_turns(task.id) == before
