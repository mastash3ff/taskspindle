"""Overlapping same-provider turns keep processes, task artifacts and cancellation isolated."""

import asyncio
import json
from pathlib import Path

from taskspindle import repos, runner, units
from taskspindle.config import Paths
from taskspindle.models import Mode, TaskState
from taskspindle.store import Store
from tests.test_runner import BOOT, PROVIDER, run_task, seed_task
from tests.test_runner import paths as paths
from tests.test_runner import script as script
from tests.test_runner import store as store


async def test_cancel_one_overlapping_worker_preserves_sibling_sessions_outputs_and_usage(
    store: Store, paths: Paths, make_repo, script,
) -> None:
    repository = make_repo()
    tasks = [seed_task(store, paths, mode=Mode.IMPLEMENT, repo=repository) for _ in range(3)]
    for task in tasks[1:]:
        assert store.acquire_lease(PROVIDER, task.id, units.worker_unit_name(task.id), 0, BOOT, limit=4)
    markers = [paths.state_dir / f"started-{index}.json" for index in range(3)]
    scripts = [script({
        "session_id": f"isolated-session-{index}", "capture_env_to": str(markers[index]),
        "block_seconds": 30 if index == 0 else 4,
        "response": f"response from worker {index}", "early_text": f"started worker {index}",
        "write": {"path": "src/worker.txt", "content": f"worker {index}\n"},
        "usage": {"inputTokens": index * 100, "outputTokens": index * 10, "totalTokens": index * 110},
    }) for index in range(3)]
    workers = [asyncio.create_task(run_task(store, paths, task, script_path))
               for task, script_path in zip(tasks, scripts, strict=True)]
    try:
        async with asyncio.timeout(15):
            while not all(marker.exists() for marker in markers):
                assert not any(worker.done() for worker in workers)
                await asyncio.sleep(0.01)
        assert all(store.get_task(task.id).state is TaskState.RUNNING for task in tasks)
        assert len(store.list_leases(PROVIDER)) == 3
        assert runner.request_cancel(tasks[0].id)
        assert await workers[0] is TaskState.CANCELLED
        assert store.get_lease(PROVIDER, tasks[0].id) is None
        assert all(store.get_lease(PROVIDER, task.id) is not None for task in tasks[1:])
        assert await asyncio.gather(*workers[1:]) == [TaskState.RESULT_READY, TaskState.RESULT_READY]
    finally:
        for task, worker in zip(tasks, workers, strict=True):
            if not worker.done():
                runner.request_cancel(task.id)
        await asyncio.gather(*workers, return_exceptions=True)

    finals = [store.get_task(task.id) for task in tasks]
    assert [task.session_id for task in finals] == [f"isolated-session-{index}" for index in range(3)]
    assert len({task.worktree_path for task in finals}) == 3
    assert len({task.transcript_path for task in finals}) == 3
    assert len({task.unit_name for task in finals}) == 3
    assert not (repository / "src/worker.txt").exists()
    for index, task in enumerate(finals):
        env = json.loads(markers[index].read_text())
        task_dir = paths.state_dir / "tasks" / task.id
        assert env["TMPDIR"] == str(task_dir / "tmp")
        assert (task_dir / "agent-1.stderr").exists()
        transcript = json.loads(Path(task.transcript_path).read_text())
        assert f"started worker {index}" in transcript["text"]
        assert all(f"started worker {other}" not in transcript["text"]
                   for other in range(3) if other != index)
        if index:
            assert (Path(task.worktree_path) / "src/worker.txt").read_text() == f"worker {index}\n"
            assert repos.current_head(Path(task.worktree_path)) == task.candidate_sha
            usage = store.list_turn_usage(task_id=task.id)
            assert len(usage) == 1
            assert (usage[0]["input_tokens"], usage[0]["output_tokens"]) == (index * 100, index * 10)
    assert store.list_leases(PROVIDER) == []
