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
        assert first.get_provider_status("grok") == newer
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
    finally:
        store.close()
