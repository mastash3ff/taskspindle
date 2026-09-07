"""Provider health observations must not erase a sibling worker's newer failure."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from taskspindle.store import Store


@pytest.mark.parametrize("previous", [None, "ok", "throttled"])
@pytest.mark.parametrize("failure", ["throttled", "auth_expired"])
def test_health_compare_and_set_preserves_a_newer_failure_from_another_connection(
    tmp_path: Path, previous: str | None, failure: str,
) -> None:
    first = Store.open(tmp_path / "state.sqlite3")
    second = Store.open(first.path)
    try:
        if previous is not None:
            first.set_provider_status("grok", previous, source="previous_turn")
        observed = first.get_provider_status("grok")
        second.set_provider_status("grok", failure, source="newer_sibling")
        newer = second.get_provider_status("grok")
        assert not first.mark_provider_healthy("grok", expected=observed)
        final = first.get_provider_status("grok")
        assert {key: value for key, value in final.items() if key != "last_success_at"} == {
            key: value for key, value in newer.items() if key != "last_success_at"
        }
        assert final["last_success_at"] is not None
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize("previous", [None, "throttled", "auth_expired"])
def test_health_observed_after_failure_can_clear_that_unchanged_failure(
    tmp_path: Path, previous: str | None,
) -> None:
    store = Store.open(tmp_path / "state.sqlite3")
    try:
        if previous is not None:
            store.set_provider_status("grok", previous, source="previous_turn")
        observed = store.get_provider_status("grok")
        assert store.mark_provider_healthy("grok", expected=observed)
        assert store.get_provider_status("grok")["state"] == "ok"
        assert store.get_provider_status("grok")["last_success_at"] is not None
    finally:
        store.close()


def test_competing_writers_preserve_failure_whichever_transaction_wins(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    setup = Store.open(path)
    setup.close()
    barrier = Barrier(2)

    def complete_success():
        store = Store.open(path)
        try:
            observed = store.get_provider_status("grok")
            barrier.wait(timeout=5)
            return store.mark_provider_healthy("grok", expected=observed)
        finally:
            store.close()

    def record_failure():
        store = Store.open(path)
        try:
            barrier.wait(timeout=5)
            store.set_provider_status("grok", "throttled", source="sibling_failure")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        success = pool.submit(complete_success)
        failure = pool.submit(record_failure)
        success.result(timeout=10)
        failure.result(timeout=10)
    store = Store.open(path)
    try:
        assert store.get_provider_status("grok")["state"] == "throttled"
        assert store.get_provider_status("grok")["last_success_at"] is not None
    finally:
        store.close()


def test_model_success_preserves_a_newer_model_refusal_and_records_success(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    first = Store.open(path)
    second = Store.open(path)
    try:
        observed_account = first.get_provider_status("claude")
        observed_model = first.get_provider_model_status("claude", "opus")
        second.set_provider_model_status(
            "claude", "opus", "model_unavailable", source="newer_sibling",
        )
        assert not first.mark_provider_healthy(
            "claude",
            expected=observed_account,
            model="opus",
            expected_model=observed_model,
        )
        row = first.get_provider_model_status("claude", "opus")
        assert row["state"] == "model_unavailable"
        assert row["source"] == "newer_sibling"
        assert row["last_success_at"] is not None
    finally:
        second.close()
        first.close()
