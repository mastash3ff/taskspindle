"""Staged model promotion uses only the local policy database and fake runtime identities."""

from pathlib import Path

import pytest

from taskspindle import policy, policy_promotion, providers
from taskspindle.store import PolicyRevisionConflict, Store


@pytest.fixture
def context(tmp_path: Path):
    profiles = providers.load_profiles(
        {}, runtime_dir=tmp_path / "rt", home=tmp_path, state_dir=tmp_path / "state",
    )
    with Store.open(tmp_path / "state" / "taskspindle.sqlite3") as store:
        yield tmp_path / "staged.json", store, profiles


def _stage(path, store, profiles, *, model="grok-4.7"):
    loaded = policy.load(store, profiles)
    return policy_promotion.stage(
        path, store, profiles, source_commit="a" * 40,
        image_identity="sha256:" + "b" * 64,
        binary_digests={"grok": "c" * 64},
        expected_revision=loaded.revision, expected_fingerprint=loaded.fingerprint,
        candidates={"planner": {"grok": {"model": model, "effort": "medium"}}},
    )


def _apply(path, store, profiles, **overrides):
    arguments = {
        "admission_closed": True,
        "runtime_image_identity": "sha256:" + "b" * 64,
        "runtime_binary_digests": {"grok": "c" * 64},
    }
    arguments.update(overrides)
    return policy_promotion.apply(path, store, profiles, **arguments)


def test_stage_rejects_stale_policy_revision(context):
    path, store, profiles = context
    old = policy.load(store, profiles)
    policy.save(store, old.policy, updated_by="test", if_revision=0)
    with pytest.raises(PolicyRevisionConflict):
        policy_promotion.stage(
            path, store, profiles, source_commit="a" * 40,
            image_identity="image", binary_digests={"grok": "c" * 64},
            expected_revision=0, expected_fingerprint=old.fingerprint,
            candidates={"planner": {"grok": {"model": "grok-4.7", "effort": "medium"}}},
        )
    assert not path.exists()


def test_unknown_candidate_model_is_refused(context):
    path, store, profiles = context
    with pytest.raises(policy_promotion.PromotionError, match="not advertised"):
        _stage(path, store, profiles, model="grok-4.8")
    assert not path.exists()


def test_apply_requires_closed_admission_and_matching_runtime(context):
    path, store, profiles = context
    _stage(path, store, profiles)
    with pytest.raises(policy_promotion.PromotionError, match="admission is closed"):
        _apply(path, store, profiles, admission_closed=False)
    with pytest.raises(policy_promotion.PromotionError, match="image identity mismatch"):
        _apply(path, store, profiles, runtime_image_identity="other")
    with pytest.raises(policy_promotion.PromotionError, match="binary digest mismatch"):
        _apply(path, store, profiles, runtime_binary_digests={"grok": "d" * 64})
    assert store.get_dispatch_policy() is None


def test_apply_and_cas_rollback_restore_prior_policy(context):
    path, store, profiles = context
    before = policy.load(store, profiles)
    staged = _stage(path, store, profiles)
    assert path.stat().st_mode & 0o222 == 0
    with pytest.raises(FileExistsError):
        _stage(path, store, profiles)
    applied = _apply(path, store, profiles)
    assert applied["revision"] == 1
    assert applied["fingerprint"] == staged["candidate_fingerprint"]
    assert applied["document"]["roles"]["planner"]["selections"]["grok"]["effort"] == "medium"
    with pytest.raises(PolicyRevisionConflict):
        _apply(path, store, profiles)
    restored = policy_promotion.rollback(path, store, profiles, admission_closed=True)
    assert restored["revision"] == 2
    assert restored["fingerprint"] == before.fingerprint
    assert restored["document"]["roles"]["planner"]["selections"]["grok"]["model"] == "grok-4.7"
    assert [row["reason"] for row in store.list_dispatch_policy_history()] == [
        "rollback " + "a" * 40, "promote " + "a" * 40,
    ]
    with pytest.raises(PolicyRevisionConflict):
        policy_promotion.rollback(path, store, profiles, admission_closed=True)


def test_apply_rejects_policy_changed_after_stage(context):
    path, store, profiles = context
    _stage(path, store, profiles)
    loaded = policy.load(store, profiles)
    policy.save(store, loaded.policy, updated_by="test", if_revision=0)
    with pytest.raises(PolicyRevisionConflict):
        _apply(path, store, profiles)


def test_tampered_record_is_rejected(context):
    path, store, profiles = context
    _stage(path, store, profiles)
    path.chmod(0o600)
    path.write_text(path.read_text().replace("grok-4.7", "grok-4.6"))
    with pytest.raises(policy_promotion.PromotionError, match="checksum mismatch"):
        _apply(path, store, profiles)
