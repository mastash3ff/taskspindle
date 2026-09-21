"""The promotion CLI accepts strict local requests and reports policy CAS outcomes."""

import json
from pathlib import Path

import pytest

from taskspindle import cli, policy, providers
from taskspindle.store import Store


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    profiles = providers.load_profiles(
        {}, runtime_dir=tmp_path / "rt", home=tmp_path, state_dir=tmp_path / "state",
    )
    database = tmp_path / "state" / "taskspindle.sqlite3"
    with Store.open(database):
        pass
    monkeypatch.setattr(cli, "_policy_store_and_profiles", lambda: (Store.open(database), profiles))
    return tmp_path, database, profiles


def _request(tmp_path: Path, action: str, payload: dict) -> list[str]:
    path = tmp_path / f"{action}.json"
    path.write_text(json.dumps(payload))
    return ["policy", "promote", action, str(path)]


def _stage_request(tmp_path: Path, database: Path, profiles: dict) -> dict:
    with Store.open(database) as store:
        loaded = policy.load(store, profiles)
    return {
        "record_path": str(tmp_path / "record.json"),
        "source_commit": "a" * 40,
        "image_identity": "sha256:" + "b" * 64,
        "binary_digests": {"grok": "c" * 64},
        "expected_revision": loaded.revision,
        "expected_fingerprint": loaded.fingerprint,
        "candidates": {"planner": {"grok": {"model": "grok-4.7", "effort": "medium"}}},
    }


def test_cli_stage_apply_and_rollback(setup, capsys: pytest.CaptureFixture[str]):
    tmp_path, database, profiles = setup
    stage = _stage_request(tmp_path, database, profiles)
    assert cli.main(_request(tmp_path, "stage", stage)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["action"] == "stage"
    assert report["expected_revision"] == 0
    assert Path(stage["record_path"]).exists()

    apply = {
        "record_path": stage["record_path"],
        "admission_closed": True,
        "admission_evidence": "dispatcher paused and queue drained by pipeline run 1",
        "runtime_image_identity": stage["image_identity"],
        "runtime_binary_digests": stage["binary_digests"],
    }
    assert cli.main(_request(tmp_path, "apply", apply)) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 1

    rollback = {
        "record_path": stage["record_path"],
        "admission_closed": True,
        "admission_evidence": "dispatcher still paused by pipeline run 1",
    }
    assert cli.main(_request(tmp_path, "rollback", rollback)) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 2
    with Store.open(database) as store:
        assert policy.load(store, profiles).fingerprint == stage["expected_fingerprint"]


def test_cli_requires_explicit_admission_and_runtime_evidence(setup, capsys):
    tmp_path, database, profiles = setup
    stage = _stage_request(tmp_path, database, profiles)
    assert cli.main(_request(tmp_path, "stage", stage)) == 0
    capsys.readouterr()
    request = {
        "record_path": stage["record_path"],
        "admission_closed": False,
        "admission_evidence": "not drained",
        "runtime_image_identity": stage["image_identity"],
        "runtime_binary_digests": stage["binary_digests"],
    }
    assert cli.main(_request(tmp_path, "apply", request)) == 1
    assert "admission_closed must be true" in capsys.readouterr().err
    request["admission_closed"] = True
    request["runtime_image_identity"] = "wrong"
    assert cli.main(_request(tmp_path, "apply", request)) == 1
    assert "runtime image identity mismatch" in capsys.readouterr().err
    with Store.open(database) as store:
        assert store.get_dispatch_policy() is None


def test_cli_rejects_stale_revision_and_extra_keys(setup, capsys):
    tmp_path, database, profiles = setup
    stage = _stage_request(tmp_path, database, profiles)
    stage["unexpected"] = "value"
    assert cli.main(_request(tmp_path, "stage", stage)) == 1
    assert "exactly" in capsys.readouterr().err
    del stage["unexpected"]
    with Store.open(database) as store:
        policy.save(store, policy.load(store, profiles).policy, updated_by="test", if_revision=0)
    assert cli.main(_request(tmp_path, "stage", stage)) == 3
    assert "revision conflict" in capsys.readouterr().err
