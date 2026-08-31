"""The job board: who claims what, and what the village is told about it."""

import copy
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import ResidentWriter, SkillWriter, valid_manifest
from steward import approvals, prompt
from steward import board as b
from steward import budgets as bg
from steward import events as ev
from steward import sessions as ss
from steward import watchdog as w
from steward.manifest import Resident, ResidentManifest, load_manifest, validate_path
from steward.runners import Outcome, Runner, RunRequest, RunResult
from steward.scheduler import Scheduler, SchedulerState, load_scheduled
from steward.session_auth import (
    SESSION_CREDENTIAL_PREFIX,
    SESSION_TOKEN_ENV,
    SessionPrincipal,
)
from steward.skills import SkillLibrary, library_for
from steward.store import JobRecord, Store
from steward.transitions.approval import ApprovalTransitions

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
        clock: Callable[[], datetime] | None = None,
        guard: ss.RunGuard | None = None,
    ) -> b.Dispatcher:
        if residents is None:
            path = write_resident(manifest if manifest is not None else board_manifest())
            residents = [load_manifest(path)]
        kwargs: dict[str, Any] = {"clock": clock} if clock is not None else {}
        return b.Dispatcher(
            residents=residents,
            store=store,
            emitter=sink,
            workdir=tmp_path,
            runner_factory=lambda _spec, _placement: runner or ScriptedRunner(),
            guard=guard,
            **kwargs,
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

    def explode(_spec: object, _placement: object) -> Runner:
        raise RuntimeError("no brain here")

    class Guard:
        def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
            del manifest, now
            return None

        def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
            del manifest
            return declared_s

        def record(self, manifest: ResidentManifest, **facts: object) -> object:
            del manifest, facts
            assert store.open_runs() == []
            return None

    dispatcher = b.Dispatcher(
        residents=[load_manifest(write_resident(board_manifest()))],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=explode,
        guard=Guard(),
    )
    (report,) = dispatcher.dispatch(NOW).reports
    assert not report.done
    assert report.reason is not None
    assert "no brain here" in report.reason
    assert store.open_runs() == []


def test_a_broken_timeout_guard_fails_each_claim_without_aborting_the_dispatch(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """A guard failure is fail-closed, and one bad claim does not strand the resident's drain."""

    class BrokenGuard:
        def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
            del manifest, now
            return None

        def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
            del manifest, declared_s
            raise RuntimeError("the timeout ledger is on fire")

        def record(self, manifest: ResidentManifest, **facts: object) -> object:
            del manifest, facts
            return None

    first = store.post_job(title="First")
    second = store.post_job(title="Second")
    manifest = board_manifest()
    manifest["board"]["max_claims_per_wake"] = 2  # type: ignore[index]
    runner = ScriptedRunner()
    dispatcher = b.Dispatcher(
        residents=[load_manifest(write_resident(manifest))],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=lambda _spec, _placement: runner,
        guard=BrokenGuard(),
    )

    run = dispatcher.dispatch(NOW)

    assert [report.task.task_id for report in run.reports] == [first.task_id, second.task_id]
    assert all(not report.done for report in run.reports)
    assert all("budget unreadable" in (report.reason or "") for report in run.reports)
    assert runner.requests == []
    assert store.open_runs() == []


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


def test_claimable_skills_is_the_effective_set_not_the_granted_one(
    write_resident: ResidentWriter, write_skill: SkillWriter, tmp_path: Path
) -> None:
    """The seam #12 replaced: matching asks the library, not the manifest's grant list."""
    write_skill("research", defaults=True)
    write_skill("errands")
    data = valid_manifest()
    data["skills"] = ["errands"]
    data["routines"] = []
    manifest = load_manifest(write_resident(data)).manifest
    library = library_for(tmp_path / "residents")

    assert b.claimable_skills(manifest, library) == frozenset({"research", "errands"})
    # With no library configured there are no defaults, and the answer is the grants
    # alone — which is exactly what the placeholder used to return, for every caller.
    assert b.claimable_skills(manifest, SkillLibrary()) == frozenset({"errands"})


def test_a_board_session_is_provisioned_with_its_skills_on_disk(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    sink: ev.NullEmitter,
    tmp_path: Path,
) -> None:
    """The board provisions a claimant exactly as the scheduler provisions a routine."""
    write_skill("research", defaults=True)
    data = board_manifest()
    data["skills"] = []
    data["routines"] = []
    residents_dir = write_resident(data).parent.parent
    store.post_job(title="Look it up")

    b.Dispatcher(
        residents=b.load_board_residents(residents_dir),
        store=store,
        emitter=sink,
        workdir=tmp_path,
        library=library_for(residents_dir),
        runner_factory=lambda _spec, _placement: ScriptedRunner(),
    ).dispatch(NOW)

    written = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    assert written.is_file()
    assert "defaults: true" in written.read_text(encoding="utf-8")


def test_a_grant_the_library_does_not_have_fails_the_task_before_it_runs(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    sink: ev.NullEmitter,
    tmp_path: Path,
) -> None:
    """A refusal, not a shrug — and the claim is closed rather than left to time out.

    The manifest was valid when it was read; the library lost the skill under a
    long-running daemon. Provisioning is where that is caught, on both kinds of wake-up.
    """
    data = board_manifest()
    data["skills"] = ["surgery"]
    data["routines"] = []
    resident = load_manifest(write_resident(data))
    write_skill("research", defaults=True)
    posted = store.post_job(title="Anything at all")

    runner = ScriptedRunner()
    (report,) = (
        b.Dispatcher(
            residents=[resident],
            store=store,
            emitter=sink,
            workdir=tmp_path,
            library=library_for(tmp_path / "residents"),
            runner_factory=lambda _spec, _placement: runner,
        )
        .dispatch(NOW)
        .reports
    )

    assert not report.done
    assert "surgery" in (report.reason or "")
    assert runner.requests == [], "steward never launched a session it could not provision"
    assert [job.status for job in store.jobs()] == ["failed"]
    assert types(sink) == ["task_claimed", "task_failed"]
    assert posted.task_id == report.task.task_id


@pytest.mark.usefixtures("empty_path")
def test_preflight_names_the_grant_before_a_task_is_ever_claimed(
    write_resident: ResidentWriter, write_skill: SkillWriter, tmp_path: Path
) -> None:
    """The same complaint as the test above, heard before the village sees a claim (#37).

    The resident declares no routine, so the scheduler's own ``check()`` has nothing to
    look at: this is the only pre-flight a board-only claimant gets.
    """
    data = board_manifest()
    data["runner"] = {"kind": "mock"}
    data["skills"] = ["surgery"]
    data["routines"] = []
    resident = load_manifest(write_resident(data))
    write_skill("research", defaults=True)
    library = library_for(tmp_path / "residents")

    (complaint,) = b.board_preflight([resident], library, tmp_path)

    assert complaint.startswith(f"{resident.id}: board — ")
    assert "surgery" in complaint
    assert Scheduler([], library=library).check() == [], "no routine, so check() sees nothing"


def test_preflight_says_nothing_about_a_resident_that_does_not_claim(
    write_resident: ResidentWriter, write_skill: SkillWriter, tmp_path: Path
) -> None:
    """Not claiming is a declaration too: an unprovisionable non-claimant fails no task."""
    data = valid_manifest()
    data["skills"] = [*data["skills"], "surgery"]
    resident = load_manifest(write_resident(data))
    write_skill("research", defaults=True)

    assert b.board_preflight([resident], library_for(tmp_path / "residents"), tmp_path) == []


def test_a_default_skill_makes_a_task_claimable_by_a_resident_granted_nothing(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    sink: ev.NullEmitter,
    tmp_path: Path,
) -> None:
    """Post-merge integration: #6's matching runs through #12's resolution.

    Hob-with-no-grants can still take a research task, because research is something
    every resident holds. Under the pre-merge placeholder — grants only — this task
    would have sat open forever with a qualified resident staring straight at it.
    """
    write_skill("research", defaults=True)
    data = board_manifest()
    data["skills"] = []
    data["routines"] = []
    residents_dir = write_resident(data).parent.parent
    store.post_job(title="Look something up", required_skills=["research"])

    runner = ScriptedRunner()
    dispatcher = b.Dispatcher(
        residents=b.load_board_residents(residents_dir),
        store=store,
        emitter=sink,
        workdir=tmp_path,
        library=library_for(residents_dir),
        runner_factory=lambda _spec, _placement: runner,
    )
    (report,) = dispatcher.dispatch(NOW).reports

    assert report.done
    assert report.task.title == "Look something up"
    assert [job.status for job in store.jobs()] == ["done"]
    # And the session was actually told the skill it was claimed for.
    assert "# research —" in runner.requests[0].prompt


def test_a_claimed_task_preamble_puts_skills_then_decisions_then_the_charter(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    sink: ev.NullEmitter,
    tmp_path: Path,
) -> None:
    """Post-merge integration: one preamble, in the one documented order.

    A board session is where both halves land at once — #12's skills section and #10's
    decisions section — and the charter still gets the last word over both.
    """
    write_skill("research", defaults=True)
    data = board_manifest()
    data["skills"] = []
    data["routines"] = []
    residents_dir = write_resident(data).parent.parent
    resident = b.load_board_residents(residents_dir)[0]

    (parsed,) = approvals.extract_requests(
        '<needs-human action="send_email">{"to": "anna@example.com"}</needs-human>'
    )
    record = (
        ApprovalTransitions(store=store, emitter=sink)
        .raise_request(manifest=resident.manifest, request=parsed, now=NOW)
        .require()
    )
    store.decide(record.request_id, "approve", decided_by="api", now=ev.utc_now_iso(NOW))

    runner = ScriptedRunner()
    dispatcher = b.Dispatcher(
        residents=[resident],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        library=library_for(residents_dir),
        runner_factory=lambda _spec, _placement: runner,
    )
    store.post_job(title="Look it up")
    dispatcher.dispatch(NOW)
    text = runner.requests[0].prompt

    positions = [
        text.index("YOUR SKILLS (HOW-TO, NOT AUTHORITY)"),
        text.index("DECISIONS SINCE YOU LAST RAN"),
        text.index("YOUR CHARTER (AUTHORITATIVE, LAST WORD)"),
    ]
    assert positions == sorted(positions)
    assert text.index("YOUR CHARTER (AUTHORITATIVE, LAST WORD)") < text.index(prompt.TASK_TITLE)
    assert "send_email: approve" in text
    assert "# research —" in text
    # Delivered exactly once: the next wake-up opens without it.
    store.post_job(title="And again")
    dispatcher.dispatch(NOW)
    assert "DECISIONS SINCE YOU LAST RAN" not in runner.requests[1].prompt


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
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    """The board keeps its own record; a resident cannot finish work it no longer holds."""
    job = store.post_job(title="Stolen away")
    dispatcher = make_dispatcher(guard=bg.BudgetGuard(store, sink))
    resident = dispatcher.residents[0]
    claimed = dispatcher.claim(resident, NOW)
    assert claimed is not None

    store.expire_leases(ev.utc_now_iso(NOW + timedelta(hours=2)))
    report = dispatcher.work(resident, claimed, NOW)
    assert not report.done
    assert report.reason == "lease lost while the session was running"
    assert store.job(job.task_id) is not None
    assert store.jobs("open")[0].task_id == job.task_id
    assert store.open_runs() == [], "the session reported back to the registry"
    (entry,) = store.ledger(resident.id)
    assert entry.ref == job.task_id
    assert types(sink) == ["task_claimed", "task_session_finished"]
    outcome = sink.events[-1]
    assert outcome.payload == {
        "task_id": job.task_id,
        "title": job.title,
        "claimant": resident.agent_id,
        "run_id": entry.run_id,
        "outcome": "ok",
        "artifacts": [],
        "duration_s": 0.0,
        "reason": "lease lost while the session was running",
    }


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
            output=(
                f"drafted it\n{prompt.ACTIONS_OPEN}\n"
                '<needs-human action="send_email">\n{"to": "a"}\n</needs-human>\n'
                f"{prompt.ACTIONS_CLOSE}"
            ),
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
        runner_factory=lambda _spec, _placement: ScriptedRunner(),
    )
    engine = Scheduler(
        load_scheduled(residents_dir),
        emitter=sink,
        state=SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec, _placement: ScriptedRunner(),
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
        runner_factory=lambda _spec, _placement: ScriptedRunner(),
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
    report = engine.fire(item, now=NOW)
    assert "DECISIONS SINCE YOU LAST RAN" not in report.prompt
    assert report.fired is False
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
    admission = engine.sessions.admit(item.resident, now=NOW, rehearsal=True)
    assert isinstance(admission, ss.Admission)
    preview = engine.sessions.run(admission, ss.RoutineWake(item.routine, "preview"))
    assert "DECISIONS SINCE YOU LAST RAN" not in preview.prompt


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


# ---------------------------------------------------------------------- leases (steward #73)


class LeaseSnooping(Runner):
    """Records the live lease of each task it is asked to work, before the claim is closed."""

    def __init__(self, store: Store) -> None:
        """Hold the store to read the claimed row from mid-session."""
        super().__init__()
        self.store = store
        self.leases: list[str | None] = []

    def run(self, request: RunRequest) -> RunResult:
        """Snapshot this task's lease and finish cleanly."""
        job = self.store.job(request.env["STEWARD_TASK_ID"])
        assert job is not None
        self.leases.append(job.lease_expires_at)
        return RunResult(outcome=Outcome.OK, output="done", exit_status=0)


def test_the_second_claim_in_a_slow_dispatch_gets_a_full_lease(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Each lease is measured from when its claim happens, not from the top of the dispatch."""
    store.post_job(title="First")
    store.post_job(title="Second")
    data = board_manifest(
        board={"claim": True, "max_claims_per_wake": 2, "lease_s": 1800, "timeout_s": 900}
    )
    resident = load_manifest(write_resident(data))
    runner = LeaseSnooping(store)
    early = NOW
    late = NOW + timedelta(minutes=5)
    ticks = iter([early, early, late, late])
    dispatcher = b.Dispatcher(
        residents=[resident],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=lambda _spec, _placement: runner,
        clock=lambda: next(ticks),
    )
    dispatcher.dispatch(NOW)

    lease_one, lease_two = runner.leases
    assert lease_one is not None
    assert lease_two is not None
    assert lease_two > lease_one
    assert lease_two == ev.utc_now_iso(late + timedelta(seconds=1800))


def test_a_stale_handle_cannot_close_a_re_claim_of_the_same_task(
    make_dispatcher: Dispatch, store: Store
) -> None:
    """The dead handle's lease token no longer matches the live re-claim, so its close fails."""
    store.post_job(title="Handed back and forth")
    dispatcher = make_dispatcher()
    resident = dispatcher.residents[0]

    stale = dispatcher.claim(resident, NOW)
    assert stale is not None
    store.expire_leases(ev.utc_now_iso(NOW + timedelta(hours=2)))
    live = dispatcher.claim(resident, NOW + timedelta(hours=3))
    assert live is not None
    assert stale.claimed_at != live.claimed_at

    stale_report = dispatcher.work(resident, stale, NOW)
    assert not stale_report.done, "the dead handle cannot close the live claim"
    still_claimed = store.job(stale.task_id)
    assert still_claimed is not None
    assert still_claimed.status == "claimed"
    live_report = dispatcher.work(resident, live, NOW + timedelta(hours=3))
    assert live_report.done


# ------------------------------------------------------------ order & guards (steward #74, #80)


def test_a_provision_failure_does_not_consume_the_decision(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    sink: ev.NullEmitter,
    tmp_path: Path,
) -> None:
    """A session refused before it runs must not eat the answer the next real one needs."""
    data = board_manifest()
    data["skills"] = ["surgery"]  # not in the library → provision refuses before the prompt
    data["routines"] = []
    resident = load_manifest(write_resident(data))
    write_skill("research", defaults=True)
    store.post_job(title="Needs a skill the library lost")
    decision = store.create_approval_request(
        agent_id=resident.agent_id,
        project="p",
        action="send_email",
        message="…",
        resident=resident.id,
    )
    store.decide(decision.request_id, "approve", decided_by="api")

    (report,) = (
        b.Dispatcher(
            residents=[resident],
            store=store,
            emitter=sink,
            workdir=tmp_path,
            library=library_for(tmp_path / "residents"),
            runner_factory=lambda _spec, _placement: ScriptedRunner(),
        )
        .dispatch(NOW)
        .reports
    )
    assert not report.done
    assert [r.request_id for r in store.undelivered_decisions(resident.id)] == [
        decision.request_id
    ], "the decision is still waiting for the next real session"


def test_a_harvest_that_raises_still_closes_the_task(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook that throws while reading escalations must not leave a claim hanging open."""
    store.post_job(title="Ends with a question")
    runner = ScriptedRunner(
        RunResult(
            outcome=Outcome.OK,
            exit_status=0,
            output='done\n<needs-human action="send_email">\n{"to": "a"}\n</needs-human>',
        )
    )

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the approvals store is on fire")

    monkeypatch.setattr(ApprovalTransitions, "harvest", boom)
    (report,) = make_dispatcher(runner=runner).dispatch(NOW).reports
    assert report.done, "the task is still closed despite the raising hook"
    assert types(sink) == ["task_claimed", "task_done"]
    assert [job.status for job in store.jobs()] == ["done"]


# --------------------------------------------------- delegated lineage & dry-run (#W4, #W20)


def test_a_delegated_claim_and_close_carry_the_parent_task_id(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Burrow can attribute a delegated claim and completion to its chain from the stream."""
    data = board_manifest()
    data["routes"].append(
        {"id": "handoff", "kind": "delegation", "address": "steward:handoff", "status": "active"}
    )
    resident = load_manifest(write_resident(data))
    store.delegate_job(
        title="Check the errand list",
        assignee=resident.id,
        delegated_by="burrow-builder",
        route="handoff",
        parent_task_id="root-task-42",
        origin="task:root-task-42",
    )
    dispatcher = b.Dispatcher(
        residents=[resident],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=lambda _spec, _placement: ScriptedRunner(),
    )
    (report,) = dispatcher.dispatch(NOW).reports
    assert report.delegated
    claimed = next(e for e in sink.events if e.type == "task_claimed")
    done = next(e for e in sink.events if e.type == "task_done")
    assert claimed.payload["parent_task_id"] == "root-task-42"
    assert done.payload["parent_task_id"] == "root-task-42"


def test_a_dry_run_dispatch_reports_what_it_would_claim_without_writing(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter
) -> None:
    """The board's honest rehearsal: no claim, no ledger, no events (steward #88)."""
    posted = store.post_job(title="Would be claimed")
    dispatcher = make_dispatcher()
    dispatcher.dry_run = True

    run = dispatcher.dispatch(NOW)
    assert run.reports == ()
    assert [plan.task.title for plan in run.planned] == ["Would be claimed"]
    assert run.planned[0].source == "board"
    assert run.planned[0].claimant == "claude-code:test-agent"
    # Nothing was written: the task is still open and the stream is silent.
    assert [job.task_id for job in store.jobs(status="open")] == [posted.task_id]
    assert types(sink) == []


def test_the_board_refuses_a_resident_that_would_run_in_cwd(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skills-loading claim whose memory dir is missing must not materialize into cwd."""
    monkeypatch.setattr(Path, "cwd", classmethod(lambda _cls: tmp_path))
    write_skill("research", defaults=True)
    data = board_manifest()  # claude runner, memory stays the absent /data/... path
    data["skills"] = []
    data["routines"] = []
    resident = load_manifest(write_resident(data))
    store.post_job(title="Would wipe the cwd", required_skills=["research"])

    sink = ev.NullEmitter()
    dispatcher = b.Dispatcher(
        residents=[resident],
        store=store,
        emitter=sink,
        workdir=tmp_path,  # equals the (patched) cwd: the dangerous fallback
        library=library_for(tmp_path / "residents"),
        runner_factory=lambda _spec, _placement: ScriptedRunner(),
    )
    run = dispatcher.dispatch(NOW)
    assert run.reports == ()
    assert [job.status for job in store.jobs()] == ["open"], "the notice stays open, unclaimed"
    assert types(sink) == []


def test_the_board_refuses_an_initial_symlink_without_touching_cwd(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared symlink is not authority to provision or launch from cwd (#133)."""
    target = tmp_path / "memory-target"
    target.mkdir()
    memory = tmp_path / "memory"
    memory.symlink_to(target, target_is_directory=True)
    cwd = tmp_path / "cwd"
    sentinel = cwd / ".claude" / "skills" / "keep.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("operator-owned", encoding="utf-8")
    monkeypatch.setattr(Path, "cwd", classmethod(lambda _cls: cwd))
    write_skill("research", defaults=True)
    data = board_manifest(memory={"kind": "directory", "path": str(memory), "journal": "journal"})
    data["skills"] = []
    data["routines"] = []
    resident = load_manifest(write_resident(data))
    store.post_job(title="Do not follow or fall back", required_skills=["research"])
    runner = ScriptedRunner()
    dispatcher = b.Dispatcher(
        residents=[resident],
        store=store,
        emitter=(sink := ev.NullEmitter()),
        workdir=cwd,
        library=library_for(tmp_path / "residents"),
        runner_factory=lambda _spec, _placement: runner,
    )

    run = dispatcher.dispatch(NOW)

    assert run.reports == ()
    assert [job.status for job in store.jobs()] == ["open"]
    assert types(sink) == []
    assert sentinel.read_text(encoding="utf-8") == "operator-owned"
    assert runner.requests == []


def test_a_memory_dir_that_vanishes_after_claim_fails_without_touching_cwd(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final lifecycle check closes an already-claimed task truthfully (#133)."""
    memory = tmp_path / "memory"
    memory.mkdir()
    cwd = tmp_path / "cwd"
    sentinel = cwd / ".claude" / "skills" / "keep.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("operator-owned", encoding="utf-8")
    monkeypatch.setattr(Path, "cwd", classmethod(lambda _cls: cwd))
    write_skill("research", defaults=True)
    data = board_manifest(memory={"kind": "directory", "path": str(memory), "journal": "journal"})
    data["runner"] = {"kind": "command", "command": ["unused", "{prompt}"]}
    data["skills"] = []
    data["routines"] = []
    resident = load_manifest(write_resident(data))
    store.post_job(title="Do not provision in cwd", required_skills=["research"])
    runner = ScriptedRunner()
    dispatcher = b.Dispatcher(
        residents=[resident],
        store=store,
        emitter=(sink := ev.NullEmitter()),
        workdir=cwd,
        library=library_for(tmp_path / "residents"),
        runner_factory=lambda _spec, _placement: runner,
    )
    claim = dispatcher.claim

    def claim_then_vanish(candidate: Resident, now: datetime) -> JobRecord | None:
        job = claim(candidate, now)
        memory.rmdir()
        return job

    monkeypatch.setattr(dispatcher, "claim", claim_then_vanish)
    (report,) = dispatcher.dispatch(NOW).reports

    assert report.status == "failed"
    assert report.reason is not None
    assert "no longer the directory" in report.reason
    assert types(sink) == ["task_claimed", "task_failed"]
    assert runner.requests == []
    assert sentinel.read_text(encoding="utf-8") == "operator-owned"
    assert list(sentinel.parent.iterdir()) == [sentinel]


def test_a_memory_dir_recreated_at_the_same_path_fails_before_provision_or_runner(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claimed task cannot spend an admission issued to the replaced directory."""
    memory = tmp_path / "memory"
    memory.mkdir()
    cwd = tmp_path / "cwd"
    sentinel = cwd / ".claude" / "skills" / "keep.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("operator-owned", encoding="utf-8")
    monkeypatch.setattr(Path, "cwd", classmethod(lambda _cls: cwd))
    write_skill("research", defaults=True)
    data = board_manifest(memory={"kind": "directory", "path": str(memory)})
    data["runner"] = {"kind": "claude", "model": "pretend"}
    data["skills"] = []
    data["routines"] = []
    resident = load_manifest(write_resident(data))
    store.post_job(title="Do not trust a recycled path", required_skills=["research"])
    runner_builds = 0

    def build_runner(_spec: object, _placement: object) -> Runner:
        nonlocal runner_builds
        runner_builds += 1
        return ScriptedRunner()

    dispatcher = b.Dispatcher(
        residents=[resident],
        store=store,
        emitter=(sink := ev.NullEmitter()),
        workdir=cwd,
        library=library_for(tmp_path / "residents"),
        runner_factory=build_runner,
    )
    claim = dispatcher.claim

    def claim_then_replace(candidate: Resident, now: datetime) -> JobRecord | None:
        job = claim(candidate, now)
        memory.rmdir()
        memory.mkdir()
        return job

    monkeypatch.setattr(dispatcher, "claim", claim_then_replace)
    (report,) = dispatcher.dispatch(NOW).reports

    assert report.status == "failed"
    assert report.reason is not None
    assert "filesystem identity changed" in report.reason
    assert types(sink) == ["task_claimed", "task_failed"]
    assert runner_builds == 0
    assert list(memory.iterdir()) == []
    assert sentinel.read_text(encoding="utf-8") == "operator-owned"
    assert list(sentinel.parent.iterdir()) == [sentinel]


def test_a_memory_dir_vanishing_mid_drain_leaves_later_tasks_open(
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    store: Store,
    tmp_path: Path,
) -> None:
    """Every task is rechecked; one completed task cannot authorize the next (#133)."""
    memory = tmp_path / "memory"
    memory.mkdir()
    write_skill("research", defaults=True)
    data = board_manifest(
        memory={"kind": "directory", "path": str(memory), "journal": "journal"},
        board={"claim": True, "max_claims_per_wake": 2, "lease_s": 1800, "timeout_s": 900},
    )
    data["runner"] = {"kind": "claude", "model": "pretend"}
    data["skills"] = []
    data["routines"] = []
    resident = load_manifest(write_resident(data))
    store.post_job(title="First", required_skills=["research"])
    store.post_job(title="Second", required_skills=["research"])

    class VanishingRunner(ScriptedRunner):
        def run(self, request: RunRequest) -> RunResult:
            result = super().run(request)
            shutil.rmtree(memory)
            return result

    run = b.Dispatcher(
        residents=[resident],
        store=store,
        emitter=ev.NullEmitter(),
        workdir=tmp_path / "fallback",
        library=library_for(tmp_path / "residents"),
        runner_factory=lambda _spec, _placement: VanishingRunner(),
    ).dispatch(NOW)

    assert [report.task.title for report in run.reports] == ["First"]
    assert [job.title for job in store.jobs("open")] == ["Second"]


def test_a_dry_run_dispatch_does_not_deliver_a_decision(
    make_dispatcher: Dispatch, store: Store
) -> None:
    """A rehearsal is not a wake-up; it must not consume a pending answer."""
    store.post_job(title="Would be claimed")
    record = store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="p",
        action="send_email",
        message="…",
        resident="test-agent",
    )
    store.decide(record.request_id, "approve", decided_by="api")
    dispatcher = make_dispatcher()
    dispatcher.dry_run = True

    dispatcher.dispatch(NOW)
    assert [r.request_id for r in store.undelivered_decisions("test-agent")] == [record.request_id]


def test_a_dry_run_plans_delegated_letters_and_skips_a_paused_resident(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The rehearsal plans a waiting letter, and reports nothing for a resident it cannot run."""
    data = board_manifest()
    data["routes"].append(
        {"id": "handoff", "kind": "delegation", "address": "steward:handoff", "status": "active"}
    )
    resident = load_manifest(write_resident(data))
    store.delegate_job(
        title="A letter",
        assignee=resident.id,
        delegated_by="burrow-builder",
        route="handoff",
        parent_task_id="root",
    )
    dispatcher = b.Dispatcher(
        residents=[resident], store=store, emitter=sink, workdir=tmp_path, dry_run=True
    )
    run = dispatcher.dispatch(NOW)
    assert [plan.source for plan in run.planned] == ["delegated"]
    assert run.planned[0].to_dict()["source"] == "delegated"
    assert types(sink) == []

    # A paused resident claims nothing, even in rehearsal — and no pause is written.
    store.pause_resident(
        resident=resident.id,
        agent_id=resident.agent_id,
        budget="daily_cost_usd",
        spent=9.0,
        cap=1.0,
    )
    assert dispatcher.dispatch(NOW).planned == ()


# ---------------------------------------------------- the run registry (steward #39)


def test_a_claimed_task_opens_and_closes_a_registry_row(
    make_dispatcher: Dispatch, store: Store
) -> None:
    """A board session is a session, so it belongs in the one place open runs are known."""
    store.post_job(title="Read the mail")

    run = make_dispatcher().dispatch(NOW)

    assert len(run.reports) == 1
    assert store.open_runs() == [], "the task reported back, so its row is answered"


def test_task_board_registry_and_ledger_share_actual_completion(
    write_resident: ResidentWriter,
    store: Store,
    sink: ev.NullEmitter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = NOW + timedelta(minutes=4)
    ticks = iter([NOW, completed])
    closed_at: list[str | None] = []
    original_close = store.mark_run_terminal_published

    def close_run(run_id: str, event_id: str, *, now: str | None = None) -> bool:
        closed_at.append(now)
        return original_close(run_id, event_id, now=now)

    monkeypatch.setattr(store, "mark_run_terminal_published", close_run)
    store.post_job(title="Read the mail")

    class Recording:
        def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
            del manifest, now
            return None

        def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
            del manifest
            return declared_s

        def record(self, manifest: ResidentManifest, **facts: object) -> object:
            del manifest
            recorded.append(facts)
            return None

    recorded: list[dict[str, object]] = []
    dispatcher = b.Dispatcher(
        residents=[load_manifest(write_resident(board_manifest()))],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=lambda _spec, _placement: ScriptedRunner(
            RunResult(outcome=Outcome.OK, duration_s=2)
        ),
        guard=Recording(),
        clock=lambda: next(ticks),
    )

    (report,) = dispatcher.dispatch(NOW).reports

    assert report.task.finished_at == ev.utc_now_iso(completed)
    assert recorded[0]["now"] == completed
    assert closed_at == [ev.utc_now_iso(completed)]


def test_an_accounting_failure_cannot_leave_a_completed_task_registry_row_open(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Shared bookkeeping sees an answered runner's registry row already closed."""

    class BrokenGuard:
        def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
            del manifest, now
            return None

        def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
            del manifest
            return declared_s

        def record(self, manifest: ResidentManifest, **facts: object) -> object:
            del manifest, facts
            assert store.open_runs() == []
            raise OSError("ledger unavailable")

    store.post_job(title="Read the mail")
    dispatcher = b.Dispatcher(
        residents=[load_manifest(write_resident(board_manifest()))],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=lambda _spec, _placement: ScriptedRunner(),
        guard=BrokenGuard(),
    )

    (report,) = dispatcher.dispatch(NOW).reports

    assert report.done
    assert store.open_runs() == []


def test_a_task_whose_session_vanishes_leaves_its_row_open(
    make_dispatcher: Dispatch, store: Store
) -> None:
    """A row the watchdog can read, whatever happened to the events."""

    class Vanishing(Runner):
        def run(self, request: RunRequest) -> RunResult:  # noqa: ARG002 — it never returns
            raise KeyboardInterrupt("the machine went away")

    posted = store.post_job(title="Read the mail")
    with pytest.raises(KeyboardInterrupt):
        make_dispatcher(runner=Vanishing()).dispatch(NOW)

    (open_run,) = store.open_runs()
    assert (open_run.kind, open_run.ref) == ("task", posted.task_id)
    assert open_run.run_id != posted.task_id, "the row names the session, not the task"
    assert open_run.timeout_s == pytest.approx(900.0)


def test_the_ledger_row_names_the_session_the_registry_opened(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """One session, one id, in both tables (steward #124).

    The ledger used to key ``run_id`` on the *task* while the registry keyed it on the
    session, so the two tables named the same session two different things: correlating
    spend to session joined on a word that meant something else in each. ``run_ledger``
    has no uniqueness constraint on ``run_id``, so nothing surfaced the collision.

    The task linkage was never the thing at risk — it travels on ``ref``, which is what
    ``spend_by_origin`` actually joins — so this pins both halves: the ledger names the
    session, and ``ref`` still names the task.
    """
    seen: list[str] = []
    recorded: list[dict[str, object]] = []

    class Peeking(Runner):
        """Read the registry row while it is open, which is only during the run."""

        def run(self, request: RunRequest) -> RunResult:
            del request
            seen.extend(run.run_id for run in store.open_runs())
            return RunResult(outcome=Outcome.OK, output="did the thing", exit_status=0)

    class Recording:
        def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
            del manifest, now
            return None

        def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
            del manifest
            return declared_s

        def record(self, manifest: ResidentManifest, **facts: object) -> object:
            del manifest
            recorded.append(facts)
            return None

    posted = store.post_job(title="Read the mail")
    dispatcher = b.Dispatcher(
        residents=[load_manifest(write_resident(board_manifest()))],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=lambda _spec, _placement: Peeking(),
        guard=Recording(),
    )

    (report,) = dispatcher.dispatch(NOW).reports

    assert report.done
    (registry_run_id,) = seen
    (facts,) = recorded
    assert registry_run_id != posted.task_id, "the registry already knew a session from a task"
    assert facts["run_id"] == registry_run_id, "and now the ledger agrees with it"
    assert facts["ref"] == posted.task_id, "the task travels on ref, where the join reads it"


def test_a_re_claimed_task_opens_a_second_row_of_its_own(
    make_dispatcher: Dispatch, store: Store
) -> None:
    """Claim, die, expire the lease, re-claim: two sessions, and both of them watched.

    The registry used to key its rows on the task id, so the retry's ``open_run`` hit the
    row the first attempt had already closed and was quietly dropped. The second session
    then vanished with nothing open to find — the very death the registry exists for.
    """

    class Vanishing(Runner):
        def run(self, request: RunRequest) -> RunResult:  # noqa: ARG002 — it never returns
            raise KeyboardInterrupt("the machine went away")

    posted = store.post_job(title="Read the mail")
    with pytest.raises(KeyboardInterrupt):
        make_dispatcher(runner=Vanishing()).dispatch(NOW)
    (first,) = store.open_runs()

    # The lease nobody is holding runs out, so the task goes back on the board — the
    # sweep stamps real time, which is why the clock is pushed past it here — and the
    # next dispatch claims it again. That second session vanishes too.
    make_dispatcher(runner=Vanishing()).expire_leases(datetime.now(UTC) + timedelta(days=1))
    with pytest.raises(KeyboardInterrupt):
        make_dispatcher(runner=Vanishing()).dispatch(NOW + timedelta(hours=1))

    ids = {run.run_id for run in store.open_runs()}
    assert first.run_id not in ids, "the sweep chose and published the first attempt's death"
    assert len(ids) == 1, "the retry has a fresh, still-open run of its own"
    assert {run.ref for run in store.open_runs()} == {posted.task_id}


def test_one_dispatch_that_reopens_and_re_claims_still_leaves_the_retry_watched(
    make_dispatcher: Dispatch, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The whole flow in one pass, against the log it actually writes (steward #39).

    ``dispatch`` expires the dead predecessor's lease *and* re-claims the task in the same
    breath. The sweep's ``task_failed`` is stamped at wall clock, so it lands at or after
    the retry's row opens — which is why "a close from the row's own lifetime answers it"
    was not enough: the predecessor's death answered the successor's row, the retry ran
    unwatched, and recovery quietly fell back to the lease sweep alone.
    """

    class Vanishing(Runner):
        def run(self, request: RunRequest) -> RunResult:  # noqa: ARG002 — it never returns
            raise KeyboardInterrupt("the machine went away")

    store.post_job(title="Read the mail")
    with pytest.raises(KeyboardInterrupt):
        make_dispatcher(runner=Vanishing(), clock=lambda: NOW).dispatch(NOW)

    # One pass, past the 1800s lease: it reopens the dead claim and claims it again.
    retried_at = NOW + timedelta(hours=1)
    with pytest.raises(KeyboardInterrupt):
        make_dispatcher(runner=Vanishing(), clock=lambda: retried_at).dispatch(retried_at)
    assert len(store.open_runs()) == 1, "the dead attempt closed and the retry remains watched"

    swept = [e for e in sink.events if e.payload.get("reason") == b.LEASE_EXPIRED]
    first_run_id = swept[0].payload.get("run_id")
    assert first_run_id, "the sweep terminal names the exact dead attempt"
    assert swept[0].ts >= ev.utc_now_iso(retried_at), "and is durably published by the sweep"

    log = tmp_path / "events.jsonl"
    log.write_text("\n".join(e.to_json() for e in sink.events) + "\n", encoding="utf-8")
    stale = w.scan_unbracketed(log, now=retried_at + timedelta(hours=1), registry=store)

    retry = next(run for run in store.open_runs() if run.started_at == ev.utc_now_iso(retried_at))
    assert retry.run_id in {run.run_id for run in stale}, "the retry is found, not silenced"
    assert first_run_id != retry.run_id, "the predecessor's terminal cannot answer the retry"


# ------------------------------------------- scoped per-session credentials (steward #41)


class AskingRunner(Runner):
    """A runner that asks, from inside the session, who its own credential says it is."""

    def __init__(self, store: Store) -> None:
        """Hold the registry this session will interrogate about itself."""
        super().__init__()
        self.store = store
        self.credentials: list[str] = []
        self.principals: list[SessionPrincipal | None] = []

    def run(self, request: RunRequest) -> RunResult:
        """Resolve this session's own credential while its run is still open."""
        credential = request.env[SESSION_TOKEN_ENV]
        self.credentials.append(credential)
        self.principals.append(self.store.session_principal(credential, fresh_since=""))
        return RunResult(outcome=Outcome.OK, output="did the thing", exit_status=0)


def test_a_claimed_task_session_is_handed_a_credential_naming_its_resident(
    make_dispatcher: Dispatch, store: Store
) -> None:
    """Asked from inside the session, against the row this attempt actually opened.

    A test that inserted its own row and queried that would prove the lookup works and
    say nothing about whether the board wrote the digest it hands out.
    """
    runner = AskingRunner(store)
    store.post_job(title="Read the mail")

    make_dispatcher(runner=runner, clock=lambda: NOW).dispatch(NOW)

    (credential,) = runner.credentials
    (principal,) = runner.principals
    assert credential.startswith(SESSION_CREDENTIAL_PREFIX)
    assert principal is not None
    assert principal.resident_id == "test-agent"
    assert store.session_principal(credential, fresh_since="") is None, "the attempt closed"


def test_two_attempts_at_one_task_are_two_credentials(
    make_dispatcher: Dispatch, store: Store
) -> None:
    """A task claimed, dropped on a dead lease and re-claimed is two sessions.

    Sharing one credential across attempts would let a dead session's leaked credential
    keep working for the retry, which is exactly the fencing mistake ``owner_token``
    already exists to avoid.
    """
    runner = AskingRunner(store)
    store.post_job(title="Read the mail")
    make_dispatcher(runner=runner, clock=lambda: NOW).dispatch(NOW)
    store.post_job(title="Read the mail again")
    later = NOW + timedelta(hours=1)
    make_dispatcher(runner=runner, clock=lambda: later).dispatch(later)

    assert len(runner.credentials) == 2
    assert len(set(runner.credentials)) == 2
