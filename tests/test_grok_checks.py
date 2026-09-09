"""Native billing diagnostics: fake CLI protocol, bounded cleanup, persisted coalescing."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from taskspindle import access_checks, grok_checks, providers
from taskspindle.service import provider_availability
from taskspindle.store import Store


@pytest.fixture
def profile(tmp_path):
    return providers.builtin_profiles(tmp_path / "runtime", home=tmp_path, state_dir=tmp_path / "state")[
        "grok"
    ]


def billing(percent=40):
    return {
        "config": {
            "creditUsagePercent": percent,
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-09-10T00:00:00Z",
            },
            "PRIVATE": "SECRET",
        },
        "principal": "SECRET",
    }


def observation(percent=40):
    return {
        "state": "quota",
        "source": "grok_billing",
        "version": "1.0.13",
        "checked_at": "2026-09-07T10:00:00Z",
        "error_code": None,
        **grok_checks.parse_billing(billing(percent)),
    }


@pytest.mark.parametrize("percent", [0, 42.5, 100])
def test_parser_allowlists_only_quota(percent):
    parsed = grok_checks.parse_billing(billing(percent))
    assert parsed["used_percent"] == percent
    assert set(parsed) == {"used_percent", "window", "period_start", "reset_at"}
    assert "SECRET" not in json.dumps(parsed)


@pytest.mark.parametrize("percent", [True, "50", -1, 101, float("nan"), float("inf"), None])
def test_invalid_quota_percent(percent):
    assert grok_checks.parse_billing(billing(percent)) is None


def test_missing_period_and_bad_dates_fail_closed():
    for end in (None, "2026-02-30T00:00:00Z", "2026-09-10", "2026-08-01T00:00:00Z"):
        value = billing()
        value["config"]["currentPeriod"]["end"] = end
        assert grok_checks.parse_billing(value) is None


def test_extra_usage_parser_preserves_native_cent_units_and_known_zero():
    value = billing()
    value["on_demand_enabled"] = True
    value["config"].update(
        prepaidBalance={"val": 1234}, onDemandCap={}, onDemandUsed={"val": 500},
    )
    parsed = grok_checks.parse_extra_usage(value)
    assert parsed == {
        "on_demand_enabled": True,
        "prepaid_balance": 1234,
        "on_demand_cap": 0,
        "on_demand_used": 500,
        "unit": "usd_cents",
        "currency": "USD",
        "auto_topup": None,
    }
    assert "SECRET" not in json.dumps(parsed)


@pytest.mark.parametrize("amount", [True, -1, 2**63, "500", float("nan"), 1.5, None])
def test_extra_usage_rejects_unknown_shapes_and_invalid_money(amount):
    value = {"config": {"prepaidBalance": {"val": amount}}, "on_demand_enabled": "true"}
    parsed = grok_checks.parse_extra_usage(value)
    assert parsed["prepaid_balance"] is None
    assert parsed["on_demand_cap"] is None
    assert parsed["on_demand_enabled"] is None
    assert grok_checks.parse_extra_usage({"config": {"prepaidBalance": 500}})["prepaid_balance"] is None
    invalid_unit = grok_checks.parse_extra_usage({"config": {"prepaidBalance": {"usd": 5}}})
    assert invalid_unit["prepaid_balance"] is None


def test_auto_topup_is_read_only_observation_with_absent_distinct_from_zero():
    assert grok_checks.parse_auto_topup({"rule": None}) is None
    assert grok_checks.parse_auto_topup({"rule": {}}) == {
        "enabled": False, "topup_amount": None, "max_amount_per_month": None,
    }
    assert grok_checks.parse_auto_topup({"rule": {
        "enabled": True, "topupAmount": {"val": 500}, "maxAmountPerMonth": {},
        "token": "SECRET",
    }}) == {"enabled": True, "topup_amount": 500, "max_amount_per_month": 0}


def test_native_cache_keeps_safe_billing_and_discards_arbitrary_fields():
    value = observation()
    value["billing"] = {
        "on_demand_enabled": True, "prepaid_balance": 1200, "on_demand_cap": 0,
        "on_demand_used": 20, "unit": "usd_cents", "currency": "USD",
        "auto_topup": {"enabled": False, "topup_amount": 500, "max_amount_per_month": None},
        "token": "SECRET", "source": "SECRET", "observed_at": "SECRET",
    }
    result = access_checks._safe_grok_result(value)
    assert result["billing"]["prepaid_balance"] == 1200
    assert result["billing"]["auto_topup"]["enabled"] is False
    assert result["billing"]["source"] == "grok_billing"
    assert result["billing"]["observed_at"] == value["checked_at"]
    assert "SECRET" not in json.dumps(result)


def test_native_cache_does_not_relabel_unknown_units_as_dollars():
    value = observation()
    value["billing"] = {"unit": "ticks", "currency": "USD", "prepaid_balance": 500}
    assert access_checks._safe_grok_result(value)["billing"] is None


def test_optional_topup_extension_failure_preserves_quota(profile, tmp_path):
    cli = fake_cli(tmp_path)
    from pathlib import Path

    path = Path(cli)
    path.write_text(path.read_text().replace(
        "result={'rule':{'enabled':True,'topupAmount':{'val':500},'maxAmountPerMonth':{'val':2000}}}",
        "print(json.dumps({'jsonrpc':'2.0','id':req['id'],'error':{'code':-32601}}),flush=True);continue",
    ))
    result = grok_checks.check_grok(
        replace(profile, command=(cli,)), {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert result["state"] == "quota"
    assert result["error_code"] is None
    assert result["billing"]["auto_topup"] is None


def fake_cli(tmp_path, mode="quota"):
    path = tmp_path / "grok"
    path.write_text(
        """#!/usr/bin/python3
import json,sys,os,time
if '--version' in sys.argv:
 print('grok 1.0.13 (abcdef) [stable]');sys.exit()
assert sys.argv[1:] == ['--no-subagents','agent','--no-leader','stdio']
assert os.environ['NO_BROWSER']=='1' and os.environ['CI']=='1'
assert os.environ['GROK_DISABLE_API_KEY_AUTH']=='true'
assert 'XAI_API_KEY' not in os.environ and 'OPENAI_API_KEY' not in os.environ
with open(os.path.join(os.environ['HOME'], 'native-process.pid'), 'w') as proof:
 proof.write(str(os.getpid()))
for line in sys.stdin:
 req=json.loads(line)
 assert req['method'] in ('initialize','_x.ai/billing','_x.ai/auto-topup-rule')
 if req['method']=='initialize':
  result={'authMethods':[{'id':'cached_token'}]}
 elif req['method']=='_x.ai/auto-topup-rule':
  result={'rule':{'enabled':True,'topupAmount':{'val':500},'maxAmountPerMonth':{'val':2000}}}
 else:
  MODE = """
        + repr(mode)
        + """
  if MODE=='hang': time.sleep(60)
  if MODE=='unavailable':
   print(json.dumps({'jsonrpc':'2.0','id':req['id'],'error':{'code':-32601,'message':'SECRET'}}),flush=True);continue
  result="""
        + repr(billing())
        + """
 print(json.dumps({'jsonrpc':'2.0','id':req['id'],'result':result}),flush=True)
"""
    )
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize("mode", ["quota", "unavailable"])
def test_exact_protocol_no_session_auth_prompt(profile, tmp_path, mode):
    cli = fake_cli(tmp_path, mode)
    result = grok_checks.check_grok(
        replace(profile, command=(cli,)),
        {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "XAI_API_KEY": "SECRET", "OPENAI_API_KEY": "SECRET"},
    )
    assert result["state"] == ("quota" if mode == "quota" else "unsupported")
    assert result["version"] == "1.0.13"
    if mode == "quota":
        assert result["billing"]["auto_topup"] == {
            "enabled": True, "topup_amount": 500, "max_amount_per_month": 2000,
        }
    assert "SECRET" not in json.dumps(result)
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "native-process.pid").read_text()), 0)


def test_owned_process_timeout(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(grok_checks, "TIMEOUT_S", 1.3)
    cli = fake_cli(tmp_path, "hang")
    before = time.monotonic()
    result = grok_checks.check_grok(
        replace(profile, command=(cli,)), {"PATH": "/usr/bin", "HOME": str(tmp_path)}
    )
    assert result["error_code"] == "TIMEOUT"
    assert time.monotonic() - before < 2
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "native-process.pid").read_text()), 0)


@pytest.mark.parametrize(
    "change",
    [{"auth": "api_key"}, {"secret_env": ("XAI_API_KEY",)}, {"base": "custom"}, {"command": ("arbitrary",)}],
)
def test_paid_or_custom_routes_never_execute(profile, monkeypatch, change):
    monkeypatch.setattr(grok_checks.subprocess, "Popen", lambda *a, **kw: pytest.fail("executed"))
    assert grok_checks.check_grok(replace(profile, **change), {})["error_code"] == "UNSUPPORTED_PROFILE"


def test_cross_process_cache_coalescing(profile, tmp_path, monkeypatch):
    database = tmp_path / "native.sqlite"
    with Store.open(database):
        pass
    calls = []

    def check(*args):
        calls.append(1)
        time.sleep(0.15)
        return observation()

    monkeypatch.setattr(grok_checks, "check_grok", check)
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}

    def refresh():
        with Store(database) as store:
            return access_checks.refresh_native_check(store, profile, env)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: refresh(), range(3)))
    assert len(calls) == 1
    assert all(result["state"] == "quota" for result in results)
    assert refresh()["state"] == "quota" and len(calls) == 1


def seed(store, profile, env, result, at="2026-09-07T10:00:00Z"):
    fp = access_checks.native_fingerprint(profile, env)
    assert store.claim_native_check(profile.id, fp, "owner", at, "2026-09-07T10:00:40Z", at)
    assert store.finish_native_check(profile.id, fp, "owner", at, result)
    return fp


def test_cache_invalidation_failure_retention_and_owner(profile, tmp_path):
    env = {"HOME": str(tmp_path)}
    with Store.open(tmp_path / "db") as store:
        fp = seed(store, profile, env, observation())
        assert not store.finish_native_check(profile.id, fp, "other", "2026-09-07T10:01:00Z", {})
        seed(store, profile, env, {"state": "check_failed", "error_code": "TIMEOUT"}, "2026-09-07T10:06:00Z")
        view = access_checks.cached_native_check(
            store, profile, env, now=datetime(2026, 9, 7, 10, 6, tzinfo=UTC)
        )
        assert view["last_success_at"] == "2026-09-07T10:00:00Z"
        assert view["last_success"]["used_percent"] == 40
        assert view["eligible_hint"] is None
        auth = tmp_path / ".grok" / "auth.json"
        auth.parent.mkdir()
        auth.write_text("SECRET")
        assert access_checks.cached_native_check(store, profile, env)["last_success"] is None
        assert (
            access_checks.cached_native_check(store, replace(profile, env={"OTHER": "changed"}), env)["state"]
            == "not_checked"
        )


def test_safe_projection_of_corrupt_rows(profile, tmp_path):
    env = {"HOME": str(tmp_path)}
    with Store.open(tmp_path / "db") as store:
        result = dict(
            observation(),
            detail="SECRET",
            version="SECRET",
            account_binding="SECRET",
            error_code="SECRET",
            arbitrary="SECRET",
        )
        seed(store, profile, env, result)
        view = access_checks.cached_native_check(store, profile, env)
        assert "SECRET" not in json.dumps(view)


def test_fresh_quota_gates_without_clearing_task_refusal(profile, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with Store.open(tmp_path / "db") as store:
        seed(store, profile, os.environ, observation(100))
        current = datetime(2026, 9, 7, 10, 1, tzinfo=UTC)
        assert provider_availability(store, profile, now=current)["code"] == "NATIVE_QUOTA_EXHAUSTED"
        assert provider_availability(store, profile, now=current + timedelta(minutes=5))["state"] == "unknown"
        store.set_provider_status("grok", "auth_expired", source="acp_error", code="AUTH_EXPIRED")
        before = store.get_provider_status("grok")
        assert provider_availability(store, profile, now=current)["state"] == "auth_expired"
        assert store.get_provider_status("grok") == before


def test_version_output_is_bounded_and_process_cleaned(profile, tmp_path):
    path = tmp_path / "grok"
    path.write_text("#!/usr/bin/python3\nimport sys\nprint('SECRET'*10000)\n")
    path.chmod(0o700)
    result = grok_checks.check_grok(replace(profile, command=(str(path),)), {"HOME": str(tmp_path)})
    assert result["error_code"] == "VERSION_UNAVAILABLE"
    assert "SECRET" not in json.dumps(result)


def test_missing_cached_auth_never_requests_billing(profile, tmp_path):
    path = tmp_path / "grok"
    path.write_text("""#!/usr/bin/python3
import sys,json
if '--version' in sys.argv: print('grok 1.0.13'); sys.exit()
req=json.loads(sys.stdin.readline())
assert req['method']=='initialize'
print(json.dumps({'jsonrpc':'2.0','id':1,'result':{'authMethods':[{'id':'grok.com'}]}}),flush=True)
assert not sys.stdin.readline()
""")
    path.chmod(0o700)
    result = grok_checks.check_grok(replace(profile, command=(str(path),)), {"HOME": str(tmp_path)})
    assert result["state"] == "auth_required"
    assert result["used_percent"] is None


def test_executable_and_auth_mode_invalidate_cache(profile, tmp_path):
    path = tmp_path / "grok"
    path.write_text("first")
    selected = replace(profile, command=(str(path),))
    env = {"HOME": str(tmp_path)}
    with Store.open(tmp_path / "db") as store:
        seed(store, selected, env, observation())
        assert access_checks.cached_native_check(store, selected, env)["last_success"]
        path.write_text("second version")
        assert access_checks.cached_native_check(store, selected, env)["last_success"] is None
        assert (
            access_checks.cached_native_check(store, replace(selected, auth="api_key"), env)["state"]
            == "unsupported"
        )


def test_passed_reset_missing_start_and_future_observations_allow_unknown(profile, tmp_path):
    current = datetime(2026, 9, 7, 10, 1, tzinfo=UTC)
    env = {"HOME": str(tmp_path)}
    for index, change in enumerate(
        [
            {"period_start": None},
            {"reset_at": "2026-09-07T10:00:00Z"},
            {"checked_at": "2026-09-07T11:00:00Z"},
        ]
    ):
        with Store.open(tmp_path / f"db{index}") as store:
            seed(store, profile, env, dict(observation(100), **change))
            assert (
                access_checks.cached_native_check(store, profile, env, now=current)["eligible_hint"] is None
            )


def test_lease_expiry_recovery_and_late_owner_cannot_publish(profile, tmp_path):
    with Store.open(tmp_path / "db") as store:
        assert store.claim_native_check(
            profile.id, "fp", "a", "2026-09-07T10:00:00Z", "2026-09-07T10:00:40Z", "2026-09-07T09:55:00Z"
        )
        assert not store.claim_native_check(
            profile.id, "fp", "b", "2026-09-07T10:00:10Z", "2026-09-07T10:00:50Z", "2026-09-07T09:55:10Z"
        )
        assert store.claim_native_check(
            profile.id, "fp", "b", "2026-09-07T10:00:41Z", "2026-09-07T10:01:21Z", "2026-09-07T09:55:41Z"
        )
        assert not store.finish_native_check(profile.id, "fp", "a", "2026-09-07T10:00:42Z", observation())
        assert store.finish_native_check(profile.id, "fp", "b", "2026-09-07T10:00:42Z", observation())


def test_actual_multiprocess_claim_coalescing(tmp_path):
    import multiprocessing

    context = multiprocessing.get_context("fork")
    database = tmp_path / "multiprocess.sqlite"
    with Store.open(database):
        pass
    queue = context.Queue()

    def claim(owner):
        with Store(database) as store:
            queue.put(
                store.claim_native_check(
                    "grok",
                    "fp",
                    owner,
                    "2026-09-07T10:00:00Z",
                    "2026-09-07T10:00:40Z",
                    "2026-09-07T09:55:00Z",
                )
            )

    children = [context.Process(target=claim, args=(str(i),)) for i in range(3)]
    for child in children:
        child.start()
    values = [queue.get(timeout=3) for _ in children]
    for child in children:
        child.join(timeout=3)
        assert child.exitcode == 0
    assert values.count(True) == 1
    queue.close()


def test_oauth_model_alias_shares_quota_fingerprint_cache_and_gate(profile, tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin"}
    alias = replace(
        profile,
        id="grok-fast",
        base="grok",
        first_class=False,
        command=("grok", "agent", "--model", "another-model", "stdio"),
        model="another-model",
        effort="low",
    )
    assert access_checks.native_fingerprint(profile, env) == access_checks.native_fingerprint(alias, env)
    with Store.open(tmp_path / "db") as store:
        seed(store, profile, env, observation(100))
        current = datetime(2026, 9, 7, 10, 1, tzinfo=UTC)
        assert access_checks.cached_native_check(store, alias, env, now=current)["used_percent"] == 100
        assert (
            provider_availability(store, alias, now=current, parent_env=env)["code"]
            == "NATIVE_QUOTA_EXHAUSTED"
        )


def test_start_and_dispatch_use_orchestrator_environment(profile, tmp_path, monkeypatch):
    from taskspindle.config import Paths
    from taskspindle.models import TaskState
    from taskspindle.orchestrator import Orchestrator
    from taskspindle.service import TaskSpindleError
    from tests.fakes.units import FakeUnitBackend
    from tests.test_orchestrator import implement_request
    from tests.test_store import make_task

    env = {"HOME": str(tmp_path / "selected-home"), "PATH": "/usr/bin"}
    monkeypatch.setenv("HOME", str(tmp_path / "wrong-ambient-home"))
    with Store.open(tmp_path / "db") as store:
        seed(store, profile, env, observation(100))
        paths = Paths(
            config_file=tmp_path / "config",
            state_dir=tmp_path / "state",
            data_dir=tmp_path / "data",
            runtime_dir=tmp_path / "runtime",
        )
        orchestrator = Orchestrator(
            store=store,
            paths=paths,
            profiles={"grok": profile},
            units=FakeUnitBackend(),
            boot="test",
            parent_env=env,
            clock=lambda: datetime(2026, 9, 7, 10, 1, tzinfo=UTC),
        )
        with pytest.raises(TaskSpindleError) as caught:
            orchestrator.start_task(implement_request(tmp_path, provider="grok"))
        assert caught.value.code == "PROVIDER_UNAVAILABLE"
        assert store.list_tasks() == []
        task = make_task(store, provider="grok")
        queued = store.update_task(task.id, None, state=TaskState.QUEUED)
        assert not orchestrator._start_worker(queued)
        assert store.list_leases() == []
        assert store.get_task(task.id).state == TaskState.QUEUED


@pytest.mark.parametrize("raw", ["{broken", "[]", '"SECRET"', "42"])
def test_malformed_native_cache_is_unknown_safe(profile, tmp_path, raw):
    with Store.open(tmp_path / "db") as store:
        seed(store, profile, {}, observation())
        store._conn.execute("UPDATE native_checks SET result_json=?", (raw,))
        assert store.get_native_check("grok") is None
        assert access_checks.cached_native_check(store, profile, {})["state"] == "not_checked"
        assert provider_availability(store, profile, now=datetime.now(UTC))["state"] == "unknown"


@pytest.mark.parametrize("raw", [{}, [], ["weekly"]])
def test_invalid_period_type_never_raises(raw):
    value = billing()
    value["config"]["currentPeriod"]["type"] = raw
    assert grok_checks.parse_billing(value) is None


def test_cleanup_never_signals_reaped_process_and_contains_errors(monkeypatch):
    import subprocess
    from io import BytesIO
    from types import SimpleNamespace

    def forbidden(*args):
        pytest.fail("signaled a reaped PID")

    monkeypatch.setattr(grok_checks.os, "killpg", forbidden)
    reaped = SimpleNamespace(pid=123, poll=lambda: 0, wait=lambda **kw: 0, stdin=BytesIO(), stdout=BytesIO())
    assert grok_checks._stop_owned(reaped)
    assert reaped.stdin.closed and reaped.stdout.closed
    monkeypatch.setattr(grok_checks.os, "killpg", lambda *a: None)

    def timeout(**kwargs):
        raise subprocess.TimeoutExpired("SECRET", 1)

    active = SimpleNamespace(pid=123, poll=lambda: None, wait=timeout, stdin=BytesIO(), stdout=BytesIO())
    assert grok_checks._stop_owned(active) is False
