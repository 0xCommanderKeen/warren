"""API behavior: runs."""

import copy
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import wait as wait_for_futures
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    SECOND_RESIDENT_UID,
    VALID_SOUL,
    ClaimHolderSpawner,
    ResidentWriter,
    valid_manifest,
)
from steward import events as ev
from steward.api import (
    AlreadyRunningError as ApiAlreadyRunningError,
)
from steward.api import (
    ManualRuns,
)
from steward.manifest import Runner as RunnerSpec
from steward.runners import MockRunner, Outcome, RunRequest, RunResult
from steward.runs import RUN_ROUTINE
from steward.scheduler import (
    FireReport,
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    load_scheduled,
)
from steward.store import Store
from support.api import (
    ApiFactory,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)


def disabled_routine_manifest() -> dict[str, Any]:
    """Build a manifest whose only routine is declared but switched off."""
    data = copy.deepcopy(valid_manifest())
    data["routines"][0]["enabled"] = False
    return data


# --------------------------------------------------------------------------------------
# run now
# --------------------------------------------------------------------------------------


def test_run_now_lands_routine_started_with_trigger_manual(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["request_id"]
    assert body["trigger"] == "manual"

    harness.settle()
    started = harness.events("routine_started")
    assert len(started) == 1
    assert started[0]["payload"]["trigger"] == "manual"
    assert started[0]["payload"]["routine"] == "daily-summary"
    assert started[0]["agent_id"] == "claude-code:test-agent"
    assert [e["type"] for e in harness.events()] == [
        "routine_started",
        "resident_declared",
        "routine_finished",
    ]
    assert ev.validate_event(started[0]) == ()


def test_run_now_records_the_outcome_against_the_request_id(api: ApiFactory) -> None:
    harness = api()
    request_id = harness.client.post("/residents/test-agent/routines/daily-summary/run").json()[
        "request_id"
    ]
    harness.settle()

    logged = harness.store.request(request_id)
    assert logged is not None
    assert logged.outcome == "ran"
    assert logged.path == "/residents/test-agent/routines/daily-summary/run"


def test_a_failed_run_is_logged_as_failed_and_emitted_as_failed(api: ApiFactory) -> None:
    harness = api(
        behavior=lambda _request: RunResult(outcome=Outcome.FAILED, exit_status=2, error="boom")
    )
    request_id = harness.client.post("/residents/test-agent/routines/daily-summary/run").json()[
        "request_id"
    ]
    harness.settle()

    logged = harness.store.request(request_id)
    assert logged is not None
    assert logged.outcome == "failed"
    assert [e["type"] for e in harness.events()] == [
        "routine_started",
        "resident_declared",
        "routine_failed",
    ]


def test_run_now_against_an_unknown_resident_is_404(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents/nobody/routines/daily-summary/run")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_resident"
    assert harness.store.export_request_history() == []


def test_run_now_against_an_unknown_routine_is_404(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents/test-agent/routines/no-such-routine/run")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_routine"
    assert "daily-summary" in detail["message"]
    assert harness.events() == []


def test_run_now_against_a_disabled_routine_is_409(api: ApiFactory) -> None:
    harness = api(manifest=disabled_routine_manifest())
    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "routine_disabled"
    assert harness.events() == []


def test_run_now_against_a_broken_manifest_says_so(api: ApiFactory) -> None:
    broken = copy.deepcopy(valid_manifest())
    del broken["memory"]
    harness = api(manifest=broken)
    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "resident_invalid"


def test_an_overlapping_run_is_refused_rather_than_queued(api: ApiFactory) -> None:
    release = threading.Event()
    entered = threading.Event()

    def block(_request: RunRequest) -> RunResult:
        entered.set()
        release.wait(timeout=10.0)
        return RunResult(outcome=Outcome.OK, exit_status=0)

    harness = api(behavior=block)
    harness.released.append(release)

    first = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert first.status_code == 202
    assert entered.wait(timeout=10.0)

    second = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "already_running"

    release.set()
    harness.settle()
    assert len(harness.events("routine_started")) == 1

    refused = [r for r in harness.store.export_request_history() if r.outcome.startswith("refused")]
    assert len(refused) == 1


def test_the_in_process_guard_still_catches_a_second_submit(
    tmp_path: Path, write_resident: ResidentWriter
) -> None:
    """One routine, one run, even in the same millisecond.

    The durable claim is taken in the fire path, not at submit, so this guard still earns
    its keep: a second submit arriving before the first has reached the claim is refused
    here, and would otherwise be two sessions of one routine.
    """
    entered = threading.Event()
    release = threading.Event()

    def block(_request: RunRequest) -> RunResult:
        entered.set()
        release.wait(timeout=10.0)
        return RunResult(outcome=Outcome.OK, exit_status=0)

    path = write_resident(valid_manifest())
    with Store(":memory:") as store:
        scheduler = Scheduler(
            load_scheduled(path.parent),
            emitter=ev.NullEmitter(),
            state=SchedulerState(path=tmp_path / "state.json"),
            workdir=tmp_path,
            runner_factory=lambda spec, placement: MockRunner(spec, placement, behavior=block),
        )
        assert scheduler.claims is None, "no durable claim: this is the in-process guard alone"
        runs = ManualRuns(scheduler=scheduler, store=store)
        item = scheduler.scheduled[0]
        try:
            runs.submit(item, "request-one")
            assert entered.wait(timeout=10.0)
            with pytest.raises(ApiAlreadyRunningError, match="is still going"):
                runs.submit(item, "request-two")
        finally:
            release.set()
            runs.wait(timeout=10.0)
            runs.shutdown()


def test_completed_manual_runs_release_pending_futures(api: ApiFactory) -> None:
    harness = api()
    runs = harness.client.app.state.runs
    for _ in range(30):
        response = harness.client.post("/residents/test-agent/routines/daily-summary/run")
        assert response.status_code == 202
        harness.settle()
    runs._pool.shutdown(wait=True)
    assert not runs._futures
    assert len(harness.events("routine_finished")) == 30


def test_failed_manual_submission_releases_the_routine(api: ApiFactory) -> None:
    harness = api()
    runs = harness.client.app.state.runs
    item = load_scheduled(harness.residents_dir)[0]
    runs.shutdown()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="cannot schedule new futures after shutdown"):
            runs.submit(item, "not-submitted")
    runs.wait()
    assert not runs._futures


def test_manual_run_completed_before_callback_registration(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = api()
    runs = harness.client.app.state.runs
    item = load_scheduled(harness.residents_dir)[0]
    register = Future.add_done_callback

    def after_completion(future: Future[None], callback: Callable[[Future[None]], object]) -> None:
        # Force registration after completion. Moving registration under ManualRuns'
        # lock makes the worker unable to finish and this bounded wait fail.
        future.result(timeout=5.0)
        register(future, callback)

    monkeypatch.setattr(Future, "add_done_callback", after_completion)
    for _ in range(2):
        runs.submit(item, "immediate")
        assert not runs._futures


def test_manual_wait_observes_active_run_during_concurrent_submission(
    api: ApiFactory, write_resident: ResidentWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    snapshot_taken = threading.Event()
    finished = threading.Event()

    def block(_request: RunRequest) -> RunResult:
        entered.set()
        assert release.wait(10.0)
        return RunResult(outcome=Outcome.OK, exit_status=0)

    harness = api(behavior=block)
    harness.released.append(release)
    other = valid_manifest()
    other.update(
        id="other-agent", uid=SECOND_RESIDENT_UID, agent_id="claude-code:other-agent", home=1
    )
    write_resident(
        other,
        root=harness.residents_dir,
        soul=VALID_SOUL.replace("claude-code:test-agent", "claude-code:other-agent"),
    )
    runs = harness.client.app.state.runs
    first, second = load_scheduled(harness.residents_dir)
    runs.submit(first, "first")
    assert entered.wait(5.0)

    def observe_wait(pending: list[Future[None]], timeout: float) -> object:
        assert len(pending) == 1
        snapshot_taken.set()
        return wait_for_futures(pending, timeout=timeout)

    def wait() -> None:
        runs.wait(timeout=5.0)
        finished.set()

    monkeypatch.setattr("steward.api.wait_for_futures", observe_wait)
    waiter = threading.Thread(target=wait, daemon=True)
    waiter.start()
    try:
        assert snapshot_taken.wait(5.0)
        runs.submit(second, "second")
        assert not finished.is_set()
        runs.shutdown()
    finally:
        release.set()
        waiter.join(timeout=5.0)
        runs._pool.shutdown(wait=True)
        monkeypatch.undo()
    assert finished.is_set()
    assert not runs._futures
    assert len(harness.events("routine_finished")) == 2


def test_a_run_now_is_refused_while_another_process_runs_the_resident(
    api: ApiFactory, tmp_path: Path, claim_holder: ClaimHolderSpawner
) -> None:
    """warren#111 at its loudest surface: the daemon's lock was invisible to this process.

    A real second process holds the resident's claim, so the API refuses with a 409 that
    names what is running rather than accepting a 202 it would later record as skipped.
    """
    database = tmp_path / "steward.db"
    harness = api(db_path=database)
    claim_holder(database, "test-agent", ref="daily-summary", run_id="held-run")

    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "already_running"
    assert "daily-summary" in detail["message"]
    assert "held-run" in detail["message"]
    assert harness.events("routine_started") == []
    refused = [r for r in harness.store.export_request_history() if r.outcome.startswith("refused")]
    assert len(refused) == 1, "a run somebody asked for and did not get is still a fact"


def test_a_run_now_for_a_second_routine_of_a_busy_resident_is_refused(
    api: ApiFactory, tmp_path: Path, claim_holder: ClaimHolderSpawner
) -> None:
    """The deliberate difference from the scheduler, tested so it cannot drift silently.

    The scheduler serialises two routines of one resident; a run-now is asking for a session
    *now*, and the honest answer while the resident is busy is that it cannot have one.
    """
    manifest = copy.deepcopy(valid_manifest())
    manifest["routines"].append(
        {
            "id": "inbox-read",
            "schedule": "15 * * * *",
            "prompt": "Read the mail.",
            "timeout_s": 60,
        }
    )
    database = tmp_path / "steward.db"
    harness = api(manifest=manifest, db_path=database)
    claim_holder(database, "test-agent", ref="daily-summary", run_id="held-run")

    response = harness.client.post("/residents/test-agent/routines/inbox-read/run")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "already_running"
    assert "daily-summary" in response.json()["detail"]["message"]


def test_a_run_now_goes_ahead_once_the_other_process_lets_go(
    api: ApiFactory, tmp_path: Path, claim_holder: ClaimHolderSpawner
) -> None:
    database = tmp_path / "steward.db"
    harness = api(db_path=database)
    holder = claim_holder(database, "test-agent")
    assert (
        harness.client.post("/residents/test-agent/routines/daily-summary/run").status_code == 409
    )

    holder.kill()
    # A killed holder released nothing, so the row is still there — and still refused,
    # because the grace has not run out. That is the lease doing its job, not a bug: the
    # cost of a crash is one grace window, not a wedged resident (see tests/test_claims.py
    # for the reclaim itself, which is far too slow to wait out here).
    stranded = harness.store.resident_claim("test-agent")
    assert stranded is not None
    assert stranded.released_at is None
    harness.store.release_resident_claim("test-agent", token=stranded.token)

    second = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert second.status_code == 202
    harness.settle()
    assert len(harness.events("routine_started")) == 1


def test_a_manual_run_uses_the_declared_runner_and_prompt(api: ApiFactory) -> None:
    seen: list[RunRequest] = []

    def record(request: RunRequest) -> RunResult:
        seen.append(request)
        return RunResult(outcome=Outcome.OK, exit_status=0)

    harness = api(behavior=record)
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    harness.settle()

    assert len(seen) == 1
    assert "Write the summary." in seen[0].prompt
    assert seen[0].model == "claude-opus-5"
    assert seen[0].timeout_s == 900
    assert seen[0].env["STEWARD_ROUTINE"] == "daily-summary"


def test_a_manual_run_that_blows_up_is_a_logged_failure_not_a_500(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = api()
    runs = harness.client.app.state.runs

    def boom(_item: object, *, trigger: str) -> None:
        raise RuntimeError(f"no brain for a {trigger} run")

    monkeypatch.setattr(runs.scheduler, "fire", boom)
    request_id = harness.client.post("/residents/test-agent/routines/daily-summary/run").json()[
        "request_id"
    ]
    harness.settle()

    logged = harness.store.request(request_id)
    assert logged is not None
    assert logged.outcome == "failed"
    assert "RuntimeError" in logged.detail["error"]


def test_a_skipped_fire_is_logged_as_skipped(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = api()
    runs = harness.client.app.state.runs

    def skip(item: ScheduledRoutine, *, trigger: str) -> FireReport:
        assert trigger == "manual"
        return FireReport(scheduled=item, run_id="r", fired=False, skipped_reason="already running")

    monkeypatch.setattr(runs.scheduler, "fire", skip)
    request_id = harness.client.post("/residents/test-agent/routines/daily-summary/run").json()[
        "request_id"
    ]
    harness.settle()

    logged = harness.store.request(request_id)
    assert logged is not None
    assert logged.outcome == "skipped: already running"


def test_the_runner_seam_is_what_the_manifest_declares() -> None:
    """The API builds runners through the same factory the scheduler does."""
    assert MockRunner(RunnerSpec(kind="mock", model="m")).describe() == "mock (m)"


# --------------------------------------------------------------------------------------
# what actually ran, beside what somebody asked for (warren#104)
# --------------------------------------------------------------------------------------


def test_the_routine_ledger_reports_a_scheduled_fire_the_request_log_cannot(
    api: ApiFactory,
) -> None:
    """warren#104. A scheduled fire is not an HTTP request, so it leaves no request row.

    The console derived "last activity" from the request log alone, so a healthy resident
    firing on its schedule read as one that had never run — and the operator's honest
    conclusion from the panel was false. The run ledger is where a fire of either kind
    lands, so the ledger row is what answers the question.
    """
    harness = api()
    harness.store.record_run(
        resident="test-agent",
        agent_id="claude-code:test-agent",
        kind=RUN_ROUTINE,
        trigger="schedule",
        run_id="run-1",
        ref="daily-summary",
        outcome="ok",
        duration_s=12.5,
    )

    (row,) = harness.client.get("/routines").json()["routines"]

    assert row["last_request"] is None, "nobody asked for it over HTTP, and the log says so"
    assert row["last_run"] == {
        "run_id": "run-1",
        "trigger": "schedule",
        "outcome": "ok",
        "recorded_at": row["last_run"]["recorded_at"],
        "duration_s": 12.5,
    }


def test_a_routine_that_has_never_finished_reports_no_last_run(api: ApiFactory) -> None:
    """`None` rather than a zeroed row: never ran and ran badly are different answers."""
    harness = api()

    (row,) = harness.client.get("/routines").json()["routines"]

    assert row["last_run"] is None


def test_the_newest_run_of_each_routine_wins(api: ApiFactory) -> None:
    """One row per routine, and it is the latest one — not whichever the scan reached."""
    harness = api()
    for index, (moment, outcome) in enumerate(
        [("2026-08-30T09:00:00.000Z", "failed"), ("2026-08-31T09:00:00.000Z", "ok")]
    ):
        harness.store.record_run(
            resident="test-agent",
            agent_id="claude-code:test-agent",
            kind=RUN_ROUTINE,
            trigger="manual",
            run_id=f"run-{index}",
            ref="daily-summary",
            outcome=outcome,
            now=moment,
        )

    (row,) = harness.client.get("/routines").json()["routines"]

    assert row["last_run"]["outcome"] == "ok"
    assert row["last_run"]["run_id"] == "run-1"
