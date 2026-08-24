"""The job board: who claims what, and what the village is told about it."""

import copy
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import ResidentWriter, valid_manifest
from steward import board as b
from steward import events as ev
from steward.manifest import Resident, load_manifest, validate_path
from steward.runners import Outcome, Runner, RunRequest, RunResult
from steward.scheduler import Scheduler, SchedulerState, load_scheduled
from steward.store import Store

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def sink() -> ev.NullEmitter:
    return ev.NullEmitter()


def board_manifest(**overrides: object) -> dict[str, Any]:
    """Build a manifest that opts into the board, with the route its declaration needs."""
    data = valid_manifest()
    data["routes"] = [
        *data["routes"],
        {"id": "job-board", "kind": "job-board", "address": "steward:job-board"},
    ]
    data["board"] = {"claim": True, "max_claims_per_wake": 1, "lease_s": 1800, "timeout_s": 900}
    data.update(copy.deepcopy(overrides))
    return data


class ScriptedRunner(Runner):
    """A runner that returns a prepared result and remembers what it was asked to run."""

    def __init__(self, result: RunResult | None = None) -> None:
        """Hold the result every run of this runner will return."""
        super().__init__()
        self.result = result or RunResult(outcome=Outcome.OK, output="did the thing", exit_status=0)
        self.requests: list[RunRequest] = []

    def run(self, request: RunRequest) -> RunResult:
        """Record the request and return the prepared result."""
        self.requests.append(request)
        return self.result


type Dispatch = Callable[..., b.Dispatcher]


@pytest.fixture
def make_dispatcher(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> Dispatch:
    def _make(
        manifest: dict[str, Any] | None = None,
        *,
        runner: Runner | None = None,
        residents: list[Resident] | None = None,
    ) -> b.Dispatcher:
        if residents is None:
            path = write_resident(manifest if manifest is not None else board_manifest())
            residents = [load_manifest(path)]
        return b.Dispatcher(
            residents=residents,
            store=store,
            emitter=sink,
            workdir=tmp_path,
            runner_factory=lambda _spec: runner or ScriptedRunner(),
        )

    return _make


def types(sink: ev.NullEmitter) -> list[str]:
    return [event.type for event in sink.events]


# ------------------------------------------------------------------------ opting in


def test_a_resident_without_a_board_block_never_claims(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    """Skills match perfectly. Silence in a manifest is still not consent."""
    store.post_job(title="Summarise the day", required_skills=["daily-summary"])
    dispatcher = make_dispatcher(valid_manifest())
    run = dispatcher.dispatch(NOW)
    assert run.reports == ()
    assert store.jobs("open")[0].title == "Summarise the day"
    assert types(sink) == []


def test_claim_false_is_the_same_as_no_block_at_all(
    make_dispatcher: Dispatch, store: Store
) -> None:
    store.post_job(title="Anything")
    dispatcher = make_dispatcher(board_manifest(board={"claim": False}))
    assert dispatcher.dispatch(NOW).reports == ()
    assert store.jobs("open")


def test_a_board_enabled_resident_claims_and_finishes_without_a_human(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    posted = store.post_job(title="Research X", detail="the long version")
    dispatcher = make_dispatcher()
    (report,) = dispatcher.dispatch(NOW).reports

    assert report.done
    assert report.task.task_id == posted.task_id
    assert store.jobs("done")[0].outcome == "ok"
    assert types(sink) == ["task_claimed", "task_done"]
    claimed, done = sink.events
    assert ev.validate_event(claimed.to_dict()) == ()
    assert claimed.agent_id == "claude-code:test-agent", "burrow walks the claimant"
    assert claimed.payload == {
        "task_id": posted.task_id,
        "title": "Research X",
        "claimant": "claude-code:test-agent",
    }
    assert done.payload["artifacts"] == []


def test_a_failed_run_marks_the_task_failed_with_a_reason(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    store.post_job(title="Something hard")
    runner = ScriptedRunner(RunResult(outcome=Outcome.FAILED, exit_status=2, error="it broke"))
    (report,) = make_dispatcher(runner=runner).dispatch(NOW).reports

    assert not report.done
    failed = store.jobs("failed")[0]
    assert failed.reason is not None
    assert "it broke" in failed.reason
    assert types(sink) == ["task_claimed", "task_failed"]
    assert "it broke" in sink.events[1].payload["reason"]


def test_a_runner_that_cannot_be_built_is_a_failed_task_not_a_crash(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    store.post_job(title="Doomed")

    def explode(_spec: object) -> Runner:
        raise RuntimeError("no brain here")

    dispatcher = b.Dispatcher(
        residents=[load_manifest(write_resident(board_manifest()))],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=explode,
    )
    (report,) = dispatcher.dispatch(NOW).reports
    assert not report.done
    assert report.reason is not None
    assert "no brain here" in report.reason


def test_artifacts_a_run_names_are_recorded_against_the_task(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    store.post_job(title="Write a report")
    runner = ScriptedRunner(
        RunResult(outcome=Outcome.OK, exit_status=0, artifacts=("notes/report.md",))
    )
    (report,) = make_dispatcher(runner=runner).dispatch(NOW).reports
    assert report.artifacts == ("notes/report.md",)
    assert store.jobs("done")[0].artifacts == ("notes/report.md",)
    assert sink.events[-1].payload["artifacts"] == ["notes/report.md"]


# ------------------------------------------------------------------------ skills


def test_skills_are_matched_as_a_subset(make_dispatcher: Dispatch, store: Store) -> None:
    store.post_job(title="Needs surgery", required_skills=["surgery"])
    store.post_job(title="Needs the summary skill", required_skills=["daily-summary"])
    (report,) = make_dispatcher().dispatch(NOW).reports
    assert report.task.title == "Needs the summary skill"
    assert [job.title for job in store.jobs("open")] == ["Needs surgery"]


def test_effective_skills_is_the_grants_today_and_the_seam_tomorrow(
    write_resident: ResidentWriter,
) -> None:
    manifest = load_manifest(write_resident()).manifest
    assert b.effective_skills(manifest) == frozenset({"daily-summary", "write-journal"})


# ------------------------------------------------------------------------ leases


def test_an_expired_lease_reopens_the_task_and_says_so_in_the_log(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    posted = store.post_job(title="Abandoned")
    store.claim_next_job(
        claimant="claude-code:test-agent",
        skills=[],
        lease_expires_at=ev.utc_now_iso(NOW - timedelta(minutes=1)),
    )
    dispatcher = make_dispatcher()
    run = dispatcher.dispatch(NOW)

    assert [job.task_id for job in run.reopened] == [posted.task_id]
    assert types(sink)[0] == "task_failed"
    assert sink.events[0].payload["reason"] == "lease_expired"
    assert sink.events[0].agent_id == "claude-code:test-agent"
    # Swept first, so the same wake-up picks it straight back up.
    assert types(sink)[1:] == ["task_claimed", "task_done"]


def test_a_lease_from_a_resident_this_tree_never_heard_of_still_reopens(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    store.post_job(title="Held by a retired resident")
    store.claim_next_job(
        claimant="claude-code:ghost", skills=[], lease_expires_at=ev.utc_now_iso(NOW)
    )
    make_dispatcher().dispatch(NOW)
    assert sink.events[0].payload["claimant"] == "claude-code:ghost"
    assert sink.events[0].project == ev.API_PROJECT


def test_a_claim_lost_mid_session_is_not_reported_as_done(
    make_dispatcher: Dispatch, store: Store
) -> None:
    """The board keeps its own record; a resident cannot finish work it no longer holds."""
    job = store.post_job(title="Stolen away")
    dispatcher = make_dispatcher()
    resident = dispatcher.residents[0]
    claimed = dispatcher.claim(resident, NOW)
    assert claimed is not None

    store.expire_leases(ev.utc_now_iso(NOW + timedelta(hours=2)))
    report = dispatcher.work(resident, claimed, NOW)
    assert not report.done
    assert report.reason == "lease lost while the session was running"
    assert store.job(job.task_id) is not None
    assert store.jobs("open")[0].task_id == job.task_id


# ------------------------------------------------------------------------ per wake-up


def test_one_claim_per_wake_up_by_default(make_dispatcher: Dispatch, store: Store) -> None:
    store.post_job(title="First")
    store.post_job(title="Second")
    run = make_dispatcher().dispatch(NOW)
    assert len(run.reports) == 1
    assert len(store.jobs("open")) == 1


def test_a_resident_may_declare_more_than_one_claim_per_wake_up(
    make_dispatcher: Dispatch, store: Store
) -> None:
    store.post_job(title="First")
    store.post_job(title="Second")
    store.post_job(title="Third")
    dispatcher = make_dispatcher(board_manifest(board={"claim": True, "max_claims_per_wake": 2}))
    run = dispatcher.dispatch(NOW)
    assert [report.task.title for report in run.reports] == ["First", "Second"]
    assert [job.title for job in store.jobs("open")] == ["Third"]


def test_an_empty_board_is_a_quiet_dispatch(
    make_dispatcher: Dispatch, sink: ev.NullEmitter
) -> None:
    run = make_dispatcher().dispatch(NOW)
    assert not run
    assert types(sink) == []


def test_sweep_only_sweeps_but_claims_nothing(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    store.post_job(title="Not tonight")
    dispatcher = b.Dispatcher.from_path(
        residents_dir, store, emitter=sink, workdir=tmp_path, sweep_only=True
    )
    run = dispatcher.dispatch(NOW)
    assert run.reports == ()
    assert store.jobs("open")


# ------------------------------------------------------------------------ the prompt


def test_a_board_session_is_an_ordinary_session_with_one_more_section(
    make_dispatcher: Dispatch, store: Store
) -> None:
    posted = store.post_job(
        title="Research X", detail="two paragraphs, please", required_skills=["daily-summary"]
    )
    runner = ScriptedRunner()
    dispatcher = make_dispatcher(runner=runner)
    dispatcher.dispatch(NOW)

    (request,) = runner.requests
    assert request.timeout_s == 900
    assert "WHO YOU ARE" in request.prompt
    assert "Flat, factual, short." in request.prompt, "a board session still sounds like itself"
    assert request.prompt.index("YOUR CHARTER") < request.prompt.index("CLAIMED FROM THE JOB BOARD")
    assert posted.task_id in request.prompt
    assert "two paragraphs, please" in request.prompt
    assert request.env["STEWARD_TASK_ID"] == posted.task_id
    assert request.env["BURROW_AGENT_ID"] == "claude-code:test-agent"


def test_a_board_session_is_told_the_decisions_it_is_waiting_on(
    make_dispatcher: Dispatch, store: Store
) -> None:
    store.post_job(title="Follow up")
    runner = ScriptedRunner()
    dispatcher = make_dispatcher(runner=runner)
    record = store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="p",
        action="send_email",
        message="…",
        resident="test-agent",
    )
    store.decide(record.request_id, "approve", decided_by="api")

    dispatcher.dispatch(NOW)
    assert "DECISIONS SINCE YOU LAST RAN" in runner.requests[0].prompt
    assert store.undelivered_decisions("test-agent") == []


def test_a_board_session_can_raise_an_approval_of_its_own(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    store.post_job(title="Send the reply")
    runner = ScriptedRunner(
        RunResult(
            outcome=Outcome.OK,
            exit_status=0,
            output='drafted it\n<needs-human action="send_email">\n{"to": "a"}\n</needs-human>',
        )
    )
    (report,) = make_dispatcher(runner=runner).dispatch(NOW).reports
    assert [record.action for record in report.raised] == ["send_email"]
    assert types(sink) == ["task_claimed", "needs_human", "task_done"]


# ------------------------------------------------------------------- wiring & loading


def test_from_path_picks_up_only_the_board_enabled_residents(
    write_resident: ResidentWriter, store: Store
) -> None:
    root = write_resident(board_manifest()).parent.parent
    write_resident(valid_manifest(), directory="quiet-one", root=root)
    dispatcher = b.Dispatcher.from_path(root, store, emitter=ev.NullEmitter())
    assert [resident.id for resident in dispatcher.residents] == ["test-agent"]


def test_an_invalid_manifest_never_reaches_the_board(write_resident: ResidentWriter) -> None:
    root = write_resident(board_manifest()).parent.parent
    write_resident({"id": "broken"}, directory="broken", root=root)
    assert [resident.id for resident in b.load_board_residents(root)] == ["test-agent"]


def test_the_scheduler_dispatches_the_board_on_every_tick(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """One hook: routines fire first, then the board is swept on the same rhythm."""
    residents_dir = write_resident(board_manifest()).parent.parent
    store.post_job(title="Picked up by a tick")
    dispatcher = b.Dispatcher(
        residents=b.load_board_residents(residents_dir),
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(),
    )
    engine = Scheduler(
        load_scheduled(residents_dir),
        emitter=sink,
        state=SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(),
        hooks=dispatcher,
    )
    engine.tick(NOW)
    assert types(sink) == ["task_claimed", "task_done"]
    assert store.jobs("done")


def test_a_broken_board_does_not_take_the_scheduler_down(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent

    class Exploding:
        def decisions_for(self, resident_id: str) -> str | None:  # noqa: ARG002
            return None

        def harvest(self, manifest: object, output: str) -> object:  # noqa: ARG002
            return []

        def dispatch(self, now: datetime) -> object:  # noqa: ARG002
            raise RuntimeError("the board is on fire")

    engine = Scheduler(
        load_scheduled(residents_dir),
        emitter=ev.NullEmitter(),
        state=SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(),
        hooks=Exploding(),
    )
    assert engine.tick(NOW) == []


def test_a_rehearsal_cannot_eat_a_real_decision(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Delivery is a write. A dry run that consumed one would silence the real session."""
    residents_dir = write_resident(board_manifest()).parent.parent
    record = store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="p",
        action="send_email",
        message="…",
        resident="test-agent",
    )
    store.decide(record.request_id, "approve", decided_by="api")

    dispatcher = b.Dispatcher(
        residents=b.load_board_residents(residents_dir), store=store, emitter=sink
    )
    engine = Scheduler(
        load_scheduled(residents_dir),
        emitter=sink,
        state=SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        dry_run=True,
        hooks=dispatcher,
    )
    item = engine.scheduled[0]
    assert "DECISIONS SINCE YOU LAST RAN" not in engine.build_prompt(item, NOW)
    assert engine.fire(item, now=NOW).fired is False
    assert [r.request_id for r in store.undelivered_decisions("test-agent")] == [record.request_id]


def test_a_scheduler_with_no_hooks_behaves_exactly_as_before(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    engine = Scheduler(
        load_scheduled(residents_dir),
        emitter=ev.NullEmitter(),
        state=SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
    )
    assert engine.tick(NOW) == []
    item = engine.scheduled[0]
    assert engine.decisions_for(item) is None
    assert "DECISIONS SINCE YOU LAST RAN" not in engine.build_prompt(item, NOW)


# ------------------------------------------------------------------- the declaration


def test_claiming_without_a_job_board_route_fails_validation(
    write_resident: ResidentWriter,
) -> None:
    data = valid_manifest()
    data["board"] = {"claim": True}
    result = validate_path(write_resident(data))
    assert not result.ok
    (diagnostic,) = [d for d in result.errors if d.field_path == "board.claim"]
    assert "no route of kind 'job-board'" in diagnostic.problem


def test_claiming_through_a_route_that_is_not_open_yet_fails_validation(
    write_resident: ResidentWriter,
) -> None:
    data = board_manifest()
    data["routes"][-1]["status"] = "pending"
    result = validate_path(write_resident(data))
    assert not result.ok
    assert any("is pending" in d.problem for d in result.errors)


def test_a_lease_that_dies_mid_session_fails_validation(
    write_resident: ResidentWriter,
) -> None:
    data = board_manifest(board={"claim": True, "lease_s": 60, "timeout_s": 900})
    result = validate_path(write_resident(data))
    assert not result.ok
    assert any("must outlive timeout_s" in d.problem for d in result.errors)


def test_hob_is_the_pilot_and_declares_the_board_honestly() -> None:
    resident = load_manifest(Path("residents/life-agent/manifest.yaml"))
    assert resident.manifest.board.claim is True
    assert resident.manifest.board.lease_s > resident.manifest.board.timeout_s
    assert any(
        route.kind == "job-board" and route.status == "active" for route in resident.manifest.routes
    )
