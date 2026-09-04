"""The orchestrator, driven end to end against a real repository and the scriptable fake agent.

Nothing here is mocked below the orchestrator: the store is a real sqlite file, the repository is a
real git repository, and a "unit" is the fake backend running :func:`taskspindle.runner.run_worker`
inline. What systemd would have detached, the hook runs in-process, so a test exercises the same
code path a real dispatch does.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from taskspindle import repos, runner, service
from taskspindle.config import Paths
from taskspindle.models import (
    AcceptTaskRequest,
    CleanupState,
    Mode,
    RecordIntegrationRequest,
    ReviewTarget,
    StartTaskRequest,
    TaskState,
)
from taskspindle.providers import Profile
from taskspindle.service import Orchestrator, TaskSpindleError
from taskspindle.store import Store
from tests.fakes.units import FakeUnitBackend

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOT = "boot-under-test"
AUTHOR = "author"
REVIEWER = "reviewer"

PASSING_REVIEW = {
    "verdict": "PASS",
    "summary": "the candidate does what it says",
    "findings": [],
    "checks": ["read the diff"],
}


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        config_file=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
    )


@pytest.fixture
def store(paths: Paths) -> Iterator[Store]:
    store = Store.open(paths.state_dir / "taskspindle.sqlite3")
    yield store
    store.close()


@pytest.fixture
def script(tmp_path: Path) -> Callable[[dict[str, object]], Path]:
    """A factory writing fake-agent scripts, one file per call."""
    written: list[Path] = []

    def _write(body: dict[str, object]) -> Path:
        path = tmp_path / f"script-{len(written)}.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        written.append(path)
        return path

    return _write


def fake_profile(profile_id: str, script_path: Path, *, unbuffered: bool = False) -> Profile:
    """The fake agent as a provider profile, with its script carried in the profile env.

    The reviewer's argv differs from the author's, which is what
    ``providers.reviewer_independent`` looks at when neither profile is first class.
    """
    argv = [sys.executable]
    if unbuffered:
        argv.append("-u")
    argv.extend(["-m", "tests.fakes.fake_agent"])
    return Profile(
        id=profile_id,
        auth="oauth",
        command=tuple(argv),
        env={"PYTHONPATH": str(REPO_ROOT), "TASKSPINDLE_FAKE_SCRIPT": str(script_path)},
    )


class Harness:
    """An orchestrator wired to the fake backend, plus the switch that runs workers inline.

    ``defer`` is what a real dispatch looks like from the server's side: the unit has been
    started and the turn has *not* finished yet. :meth:`run_pending` then plays the workers out.
    """

    def __init__(self, orchestrator: Orchestrator, backend: FakeUnitBackend) -> None:
        self.orchestrator = orchestrator
        self.backend = backend
        self.inline = True
        self.pending: list[str] = []
        self.backend.on_start = self._on_start

    def defer(self) -> None:
        """Let dispatch start units without their workers running yet."""
        self.inline = False

    def run_pending(self) -> None:
        while self.pending:
            self._run(self.pending.pop(0))

    def _on_start(self, unit: str, argv: tuple[str, ...]) -> None:
        if "taskspindle.runner" not in argv:
            return
        task_id = argv[argv.index("--task") + 1]
        if self.inline:
            self._run(task_id)
        else:
            self.pending.append(task_id)

    def _run(self, task_id: str) -> None:
        asyncio.run(
            runner.run_worker(
                self.orchestrator.store,
                task_id,
                profiles=self.orchestrator.profiles,
                paths=self.orchestrator.paths,
                boot=BOOT,
                signals=False,
            )
        )


@pytest.fixture
def harness(store: Store, paths: Paths, script) -> Harness:
    """An orchestrator with two independent fake providers, both able to run inline."""
    author = fake_profile(
        AUTHOR,
        script({"response": "added the file", "write": {"path": "src/new.txt", "content": "hello\n"}}),
    )
    reviewer = fake_profile(
        REVIEWER, script({"response": json.dumps(PASSING_REVIEW)}), unbuffered=True
    )
    backend = FakeUnitBackend()
    orchestrator = Orchestrator(
        store=store,
        paths=paths,
        profiles={AUTHOR: author, REVIEWER: reviewer},
        units=backend,
        boot=BOOT,
        parent_env={"PATH": "/usr/bin", "HOME": str(paths.state_dir)},
    )
    return Harness(orchestrator, backend)


def implement_request(repo: Path, provider: str = AUTHOR, **overrides: object) -> StartTaskRequest:
    fields: dict[str, object] = {
        "provider": provider,
        "mode": Mode.IMPLEMENT,
        "prompt": "add the thing",
        "repository": str(repo),
        "acceptance_criteria": "the thing exists",
        "path_prefixes": ["src"],
        "verification_commands": ["true"],
        "candidate_message": "Add the thing",
        "timeout_s": 60,
    }
    fields.update(overrides)
    return StartTaskRequest(**fields)


def authorize(harness: Harness, repo: Path) -> None:
    harness.orchestrator.authorize_repository(
        str(repo), [AUTHOR, REVIEWER], [mode.value for mode in Mode]
    )


def build_candidate(harness: Harness, repo: Path) -> str:
    """Start an implement task and let the inline worker take it to RESULT_READY."""
    authorize(harness, repo)
    started = harness.orchestrator.start_task(implement_request(repo))
    assert started["state"] == TaskState.RESULT_READY.value
    return str(started["task_id"])


def whole_diff(harness: Harness, task_id: str, page: int = 262144) -> bytes:
    """Retrieve every page of a candidate diff, exactly as an accepting session must."""
    body = b""
    while True:
        result = harness.orchestrator.task_diff(task_id, len(body), page)
        body += base64.b64decode(result["data"])
        if len(body) >= result["size"]:
            return body


def review_candidate(harness: Harness, repo: Path, task_id: str) -> str:
    """Run an independent review of the task's candidate and return the review task id."""
    status = harness.orchestrator.task_status(task_id)
    started = harness.orchestrator.start_task(
        StartTaskRequest(
            provider=REVIEWER,
            mode=Mode.REVIEW,
            prompt="review it",
            timeout_s=60,
            review_target=ReviewTarget(
                kind="candidate", task_id=task_id, candidate_sha=status["candidate_sha"]
            ),
        )
    )
    assert started["state"] == TaskState.COMPLETED.value
    return str(started["task_id"])


def accept_request(
    harness: Harness, repo: Path, task_id: str, review_task_id: str
) -> AcceptTaskRequest:
    status = harness.orchestrator.task_status(task_id)
    return AcceptTaskRequest(
        task_id=task_id,
        expected_state_version=status["state_version"],
        candidate_sha=status["candidate_sha"],
        diff_digest=status["diff_digest"],
        inspection_summary="read every hunk",
        expected_target_head=repos.current_head(repo),
        review_task_id=review_task_id,
        commit_message="Add the thing",
    )


# -- starting and dispatching ------------------------------------------------------------------


def test_an_authorized_implement_task_runs_to_a_candidate(harness: Harness, make_repo) -> None:
    repo = make_repo()
    policy = harness.orchestrator.authorize_repository(str(repo), [AUTHOR], ["implement"])
    assert [(grant["provider"], grant["mode"]) for grant in policy["grants"]] == [
        (AUTHOR, "implement")
    ]

    started = harness.orchestrator.start_task(implement_request(repo))
    task_id = started["task_id"]

    assert started["state"] == TaskState.RESULT_READY.value
    status = harness.orchestrator.task_status(task_id)
    assert status["changed_paths"] == ["src/new.txt"]
    assert status["warnings"] == []
    assert status["check_summary"] == {"total": 1, "passed": 1, "ok": True}

    snapshot = harness.orchestrator.store.get_artifact(task_id, 0, "root_snapshot")
    assert json.loads(Path(snapshot["path"]).read_text())["head"] == repos.current_head(repo)

    unit, argv = harness.backend.started[0]
    assert unit == f"taskspindle-worker-{task_id}"
    assert argv == (sys.executable, "-m", "taskspindle.runner", "--task", task_id)


def test_an_unauthorized_repository_is_refused(harness: Harness, make_repo) -> None:
    repo = make_repo()

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.start_task(implement_request(repo))

    assert excinfo.value.code == service.GRANT_MISSING
    assert harness.backend.started == []


def test_dirty_paths_inside_the_prefixes_refuse_to_start(harness: Harness, make_repo) -> None:
    repo = make_repo()
    authorize(harness, repo)
    (repo / "src").mkdir()
    (repo / "src" / "wip.txt").write_text("work in progress\n")

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.start_task(implement_request(repo))

    assert excinfo.value.code == service.INVALID_REQUEST
    assert excinfo.value.details["code"] == service.DIRTY_OVERLAP
    assert excinfo.value.details["paths"] == ["src/wip.txt"]
    tasks = harness.orchestrator.list_tasks()["tasks"]
    assert [task["state"] for task in tasks] == [TaskState.FAILED.value]


def test_a_second_task_waits_for_the_provider_lease(harness: Harness, make_repo) -> None:
    repo = make_repo()
    authorize(harness, repo)
    harness.defer()

    first = harness.orchestrator.start_task(implement_request(repo))["task_id"]
    second = harness.orchestrator.start_task(implement_request(repo))["task_id"]

    assert harness.orchestrator.task_status(second)["state"] == TaskState.QUEUED.value
    assert harness.orchestrator.task_status(second)["unit_name"] is None
    assert [unit for unit, _ in harness.backend.started] == [f"taskspindle-worker-{first}"]

    harness.run_pending()
    assert harness.orchestrator.dispatch_queued() == [second]
    assert harness.orchestrator.task_status(first)["state"] == TaskState.RESULT_READY.value
    assert harness.orchestrator.task_status(second)["unit_name"] == f"taskspindle-worker-{second}"


# -- inspecting ---------------------------------------------------------------------------------


def test_the_diff_is_paged_and_every_page_is_receipted(harness: Harness, make_repo) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)

    first = harness.orchestrator.task_diff(task_id, 0, 40)
    assert first["offset"] == 0
    assert first["length"] == 40
    assert first["size"] > 40

    second = harness.orchestrator.task_diff(task_id, 40, 262144)
    assert second["offset"] == 40
    assert second["length"] == first["size"] - 40
    assert second["receipt_id"] > first["receipt_id"]

    body = base64.b64decode(first["data"]) + base64.b64decode(second["data"])
    assert b"src/new.txt" in body
    assert service.diff_fully_retrieved(
        harness.orchestrator.store, task_id, first["digest"], first["size"]
    ) == []


def test_accepting_a_partly_read_candidate_is_refused(harness: Harness, make_repo) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    harness.orchestrator.task_diff(task_id, 0, 40)
    review_task_id = review_candidate(harness, repo, task_id)

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.accept_task(
            accept_request(harness, repo, task_id, review_task_id)
        )

    assert excinfo.value.code == service.DIFF_NOT_FULLY_RETRIEVED
    assert harness.orchestrator.task_status(task_id)["state"] == TaskState.RESULT_READY.value


def test_accepting_without_a_review_is_refused(harness: Harness, make_repo) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    whole_diff(harness, task_id)

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.accept_task(accept_request(harness, repo, task_id, "ts_absent"))

    assert excinfo.value.code == service.REVIEW_REQUIRED


# -- reviewing and accepting ---------------------------------------------------------------------


def test_a_review_of_the_candidate_unblocks_acceptance(harness: Harness, make_repo) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    whole_diff(harness, task_id)
    review_task_id = review_candidate(harness, repo, task_id)

    review = harness.orchestrator.store.get_review_for(review_task_id)
    assert review["subject_task_id"] == task_id
    assert review["verdict"] == "PASS"
    assert review["provider"] == REVIEWER

    accepted = harness.orchestrator.accept_task(
        accept_request(harness, repo, task_id, review_task_id)
    )

    assert accepted["state"] == TaskState.ACCEPTING.value
    journal = harness.orchestrator.store.read_journal(task_id)
    assert journal["phase"] == "probing"
    assert journal["target_head"] == repos.current_head(repo)
    unit, argv = harness.backend.started[-1]
    assert unit == f"taskspindle-accept-{task_id}"
    assert argv == (sys.executable, "-m", "taskspindle.accept", "--task", task_id)


def test_a_reviewer_that_is_not_independent_is_refused(harness: Harness, make_repo) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    status = harness.orchestrator.task_status(task_id)

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.start_task(
            StartTaskRequest(
                provider=AUTHOR,
                mode=Mode.REVIEW,
                prompt="review my own work",
                review_target=ReviewTarget(
                    kind="candidate", task_id=task_id, candidate_sha=status["candidate_sha"]
                ),
            )
        )

    assert excinfo.value.code == service.REVIEWER_NOT_INDEPENDENT


def test_a_scope_violation_blocks_acceptance(harness: Harness, make_repo, script) -> None:
    repo = make_repo()
    harness.orchestrator.profiles[AUTHOR] = fake_profile(
        AUTHOR,
        script({"response": "wrote docs", "write": {"path": "docs/x.txt", "content": "notes\n"}}),
    )
    task_id = build_candidate(harness, repo)
    whole_diff(harness, task_id)
    review_task_id = review_candidate(harness, repo, task_id)

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.accept_task(
            accept_request(harness, repo, task_id, review_task_id)
        )

    assert excinfo.value.code == service.ACCEPT_BLOCKED
    assert excinfo.value.details["warnings"] == ["SCOPE_VIOLATION:docs/x.txt"]
    assert harness.orchestrator.task_status(task_id)["state"] == TaskState.RESULT_READY.value


# -- continuing, cancelling and cleaning up -------------------------------------------------------


def test_continuing_a_candidate_opens_a_repair_turn(harness: Harness, make_repo) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    harness.defer()
    status = harness.orchestrator.task_status(task_id)

    updated = harness.orchestrator.continue_task(
        task_id, status["state_version"], "also add a docstring"
    )

    assert updated["state"] == TaskState.REPAIRING.value
    pending = [
        turn for turn in harness.orchestrator.store.list_turns(task_id) if turn["ended_at"] is None
    ]
    assert len(pending) == 1
    assert pending[0]["kind"] == "repair"
    assert pending[0]["revision"] == 2
    assert "also add a docstring" in pending[0]["prompt"]
    assert harness.backend.started[-1][0] == f"taskspindle-worker-{task_id}"


def test_continuing_a_task_that_cannot_take_a_turn_is_refused(
    harness: Harness, make_repo
) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    review_task_id = review_candidate(harness, repo, task_id)
    status = harness.orchestrator.task_status(review_task_id)

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.continue_task(review_task_id, status["state_version"], "again")

    assert excinfo.value.code == service.ILLEGAL_TRANSITION


def test_cancelling_a_queued_task_settles_it_here(harness: Harness, make_repo) -> None:
    repo = make_repo()
    authorize(harness, repo)
    harness.defer()
    first = harness.orchestrator.start_task(implement_request(repo))["task_id"]
    waiting = harness.orchestrator.start_task(implement_request(repo))["task_id"]
    status = harness.orchestrator.task_status(waiting)
    assert status["state"] == TaskState.QUEUED.value

    cancelled = harness.orchestrator.cancel_task(waiting, status["state_version"])

    assert cancelled["state"] == TaskState.CANCELLED.value
    assert harness.backend.killed == []
    # The lease the waiting task never held still belongs to the task that did.
    assert harness.orchestrator.store.get_lease(AUTHOR)["task_id"] == first


def test_cancelling_a_running_task_signals_its_unit(harness: Harness, make_repo) -> None:
    repo = make_repo()
    authorize(harness, repo)
    harness.defer()
    task_id = harness.orchestrator.start_task(implement_request(repo))["task_id"]
    status = harness.orchestrator.task_status(task_id)

    cancelled = harness.orchestrator.cancel_task(task_id, status["state_version"])

    assert cancelled["state"] == TaskState.CANCELLING.value
    assert harness.backend.killed == [(f"taskspindle-worker-{task_id}", "SIGTERM")]


def test_cleanup_refuses_a_dirty_worktree_without_force(harness: Harness, make_repo) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    status = harness.orchestrator.task_status(task_id)
    harness.orchestrator.reject_task(task_id, status["state_version"], "not what I meant")
    worktree = Path(status["worktree_path"])
    (worktree / "leftover.txt").write_text("still here\n")

    refused = harness.orchestrator.cleanup_task(task_id)

    assert refused["cleanup_state"] == CleanupState.FAILED.value
    assert refused["retained"] == [str(worktree)]
    assert worktree.exists()

    forced = harness.orchestrator.cleanup_task(task_id, force=True)

    assert forced["cleanup_state"] == CleanupState.COMPLETE.value
    assert str(worktree) in forced["removed"]
    assert not worktree.exists()
    assert repos.run_git(
        ["for-each-ref", f"refs/taskspindle/{task_id}/"], cwd=repo
    ).stdout == b""


def test_cleanup_refuses_a_task_that_is_still_working(harness: Harness, make_repo) -> None:
    repo = make_repo()
    authorize(harness, repo)
    harness.defer()
    task_id = harness.orchestrator.start_task(implement_request(repo))["task_id"]

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.cleanup_task(task_id)

    assert excinfo.value.code == service.ILLEGAL_TRANSITION


# -- the surface itself ---------------------------------------------------------------------------


def test_capabilities_describes_the_providers_and_the_limits(harness: Harness) -> None:
    capabilities = harness.orchestrator.capabilities()

    assert [profile["id"] for profile in capabilities["providers"]] == [AUTHOR, REVIEWER]
    assert capabilities["providers"][0]["second_class"] is True
    assert capabilities["versions"]["acp"] == "0.12.0"
    assert capabilities["limits"] == {
        "timeout_s": [60, 14400],
        "diff_page_bytes": 262144,
        "concurrent_turns_per_provider": 1,
    }
    assert "not an OS sandbox" in capabilities["isolation"]


def test_revoking_a_grant_leaves_the_tasks_alone(harness: Harness, make_repo) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)

    revoked = harness.orchestrator.revoke_repository(str(repo), [AUTHOR], ["implement"])

    assert revoked["revoked"] == 1
    active = {
        (grant["provider"], grant["mode"]) for grant in revoked["grants"] if grant["active"]
    }
    assert (AUTHOR, "implement") not in active
    assert (REVIEWER, "review") in active
    assert harness.orchestrator.task_status(task_id)["state"] == TaskState.RESULT_READY.value

    policies = harness.orchestrator.list_repository_policies()["repositories"]
    assert [policy["display_path"] for policy in policies] == [str(repo)]


def test_an_unknown_provider_is_an_invalid_request(harness: Harness, make_repo) -> None:
    repo = make_repo()

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.authorize_repository(str(repo), ["nobody"], ["consult"])

    assert excinfo.value.code == service.INVALID_REQUEST
    assert excinfo.value.details["code"] == "PROFILE_UNKNOWN"


def test_an_acknowledged_root_mutation_stops_blocking_acceptance(
    harness: Harness, make_repo, script
) -> None:
    repo = make_repo()
    harness.orchestrator.profiles[AUTHOR] = fake_profile(
        AUTHOR,
        script(
            {
                "response": "and a note in the root",
                "write": {"path": "src/new.txt", "content": "hello\n"},
                "write_abs": {"path": str(repo / "intruder.txt"), "content": "sneaky\n"},
            }
        ),
    )
    task_id = build_candidate(harness, repo)
    whole_diff(harness, task_id)
    review_task_id = review_candidate(harness, repo, task_id)
    assert harness.orchestrator.task_status(task_id)["warnings"] == [
        "ROOT_MUTATION:intruder.txt"
    ]

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.accept_task(
            accept_request(harness, repo, task_id, review_task_id)
        )
    assert excinfo.value.code == service.ACCEPT_BLOCKED
    assert excinfo.value.details["warnings"] == ["ROOT_MUTATION:intruder.txt"]

    status = harness.orchestrator.task_status(task_id)
    harness.orchestrator.record_integration(
        RecordIntegrationRequest(
            task_id=task_id,
            expected_state_version=status["state_version"],
            kind="root_mutation_acknowledged",
            summary="I put that file there myself",
        )
    )

    accepted = harness.orchestrator.accept_task(
        accept_request(harness, repo, task_id, review_task_id)
    )
    assert accepted["state"] == TaskState.ACCEPTING.value


def test_a_manual_integration_records_the_head_it_landed_at(
    harness: Harness, make_repo
) -> None:
    repo = make_repo()
    task_id = build_candidate(harness, repo)
    status = harness.orchestrator.task_status(task_id)

    recorded = harness.orchestrator.record_integration(
        RecordIntegrationRequest(
            task_id=task_id,
            expected_state_version=status["state_version"],
            kind="manual_integration",
            resulting_head="0" * 40,
            summary="cherry-picked it by hand",
        )
    )

    assert recorded["state"] == TaskState.ACCEPTED.value
    assert harness.orchestrator.task_status(task_id)["target_head"] == "0" * 40
    kinds = [event["kind"] for event in harness.orchestrator.store.list_events(task_id)]
    assert "INTEGRATION_RECORDED" in kinds


def test_an_ambiguous_recovery_asks_for_a_person(harness: Harness, make_repo) -> None:
    repo = make_repo()
    authorize(harness, repo)
    harness.defer()
    task_id = harness.orchestrator.start_task(implement_request(repo))["task_id"]
    # The unit is gone but the worker was alive a moment ago: nothing may be assumed.
    harness.orchestrator.store.heartbeat(task_id)
    harness.backend.states.pop(f"taskspindle-worker-{task_id}")

    status = harness.orchestrator.task_status(task_id)

    assert status["state"] == TaskState.RECOVERY_AMBIGUOUS.value
    assert status["evidence"]["reason"] == "unit_missing_fresh_heartbeat"
    assert "systemctl --user status" in status["manual_action"]

    with pytest.raises(TaskSpindleError) as excinfo:
        harness.orchestrator.continue_task(task_id, status["state_version"], "carry on")

    assert excinfo.value.code == service.MANUAL_RECOVERY_REQUIRED
    assert excinfo.value.details["evidence"]["to"] == TaskState.RECOVERY_AMBIGUOUS.value
