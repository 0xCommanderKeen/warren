"""The watchdog: what it can truthfully see, what it does about it, and when it stops.

Two things get proven here more than once, because they are the ones that turn a watchdog
from a safety net into a liability: an intervention is announced, and it happens exactly
once. A restart nobody hears about is a lie by omission; a ``routine_failed`` emitted on
every pass is a village full of deaths that never happened.
"""

import copy
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import ResidentWriter, StubWriter, valid_manifest
from steward import budgets as bg
from steward import events as ev
from steward import watchdog as w
from steward.manifest import Resident, load_manifest
from steward.runners import run_argv
from steward.scheduler import SchedulerState
from steward.store import Store

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def sink() -> ev.NullEmitter:
    return ev.NullEmitter()


def watched_manifest(**overrides: object) -> dict[str, Any]:
    """Build a manifest with a container to supervise and a mock brain."""
    data = copy.deepcopy(valid_manifest())
    data["runner"] = {"kind": "mock", "model": "pretend"}
    data["deploy"] = {"container": "steward-test-agent"}
    data.update(copy.deepcopy(overrides))
    return data


@pytest.fixture
def resident(write_resident: ResidentWriter) -> Resident:
    return load_manifest(write_resident(watched_manifest()))


def started(run_id: str, *, ts: datetime, agent: str = "claude-code:test-agent") -> str:
    """Render one ``routine_started`` line, as the fallback log holds it."""
    return json.dumps(
        {
            "v": 0,
            "ts": ev.utc_now_iso(ts),
            "source": "steward",
            "agent_id": agent,
            "project": "test-agent",
            "type": "routine_started",
            "payload": {"routine": "daily-summary", "run_id": run_id, "trigger": "schedule"},
        }
    )


def finished(run_id: str, *, ts: datetime, agent: str = "claude-code:test-agent") -> str:
    """Render the closing event a live run eventually writes."""
    return json.dumps(
        {
            "v": 0,
            "ts": ev.utc_now_iso(ts),
            "source": "steward",
            "agent_id": agent,
            "project": "test-agent",
            "type": "routine_finished",
            "payload": {"routine": "daily-summary", "run_id": run_id, "outcome": "ok"},
        }
    )


def task_closed(
    task_id: str,
    *,
    ts: datetime,
    agent: str = "claude-code:test-agent",
    type: str = "task_done",  # noqa: A002 — the event's own field name
    run_id: str | None = None,
) -> str:
    """Render the closing event a board session writes: it names the task *and* the run.

    ``run_id=None`` renders the lease sweep's ``task_failed``, which names no session
    because it is the board mourning a claim rather than a run reporting back.
    """
    payload: dict[str, Any] = {"task_id": task_id, "title": "Read the mail", "claimant": agent}
    if run_id:
        payload["run_id"] = run_id
    return json.dumps(
        {
            "v": 0,
            "ts": ev.utc_now_iso(ts),
            "source": "steward",
            "agent_id": agent,
            "project": "test-agent",
            "type": type,
            "payload": payload,
        }
    )


def write_log(path: Path, *lines: str) -> Path:
    """Write a fallback event log, exactly as steward's emitter appends to one."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class StubSupervisor:
    """A supervisor a test drives by hand: it says what it is told and counts restarts."""

    def __init__(
        self, *, alive: bool = True, known: bool = True, restarts_work: bool = True
    ) -> None:
        """Decide up front what this supervisor sees and whether restarting helps."""
        self.kind = "stub"
        self.alive = alive
        self.known = known
        self.restarts_work = restarts_work
        self.restarts: list[str] = []

    def health(self, resident: Resident, now: datetime) -> w.Health:  # noqa: ARG002
        """Report the health this stub was configured with."""
        return w.Health(
            resident_id=resident.id,
            alive=self.alive,
            known=self.known,
            detail="container exited",
            supervisor=self.kind,
        )

    def restart(self, resident: Resident) -> bool:
        """Count the attempt and report whether it worked."""
        self.restarts.append(resident.id)
        if self.restarts_work:
            self.alive = True
        return self.restarts_work


def build(
    resident: Resident,
    store: Store,
    sink: ev.NullEmitter,
    tmp_path: Path,
    **kwargs: Any,  # noqa: ANN401 — the watchdog constructor is what is under test
) -> w.Watchdog:
    """Assemble a watchdog with an empty log and no real supervisors unless asked."""
    kwargs.setdefault("supervisors", ())
    kwargs.setdefault("fallback", tmp_path / "events.jsonl")
    kwargs.setdefault("state", SchedulerState(path=tmp_path / "state.json"))
    return w.Watchdog(residents=[resident], store=store, emitter=sink, **kwargs)


# --------------------------------------------------------------------------------------
# unbracketed runs
# --------------------------------------------------------------------------------------


def test_a_started_run_with_no_closing_event_is_stale(tmp_path: Path) -> None:
    """The scan finds exactly the runs the log never answered."""
    log = write_log(
        tmp_path / "events.jsonl",
        started("gone", ts=NOW - timedelta(hours=2)),
        started("fine", ts=NOW - timedelta(hours=2)),
        finished("fine", ts=NOW - timedelta(hours=1)),
        started("young", ts=NOW - timedelta(seconds=30)),
    )
    stale = w.scan_unbracketed(
        log, now=NOW, timeouts={"claude-code:test-agent/daily-summary": 900.0}
    )
    assert [run.run_id for run in stale] == ["gone"]
    assert stale[0].routine == "daily-summary"


def test_a_run_inside_its_own_timeout_is_not_stale(tmp_path: Path) -> None:
    """Each run is judged against its own deadline plus the grace window."""
    log = write_log(tmp_path / "events.jsonl", started("slow", ts=NOW - timedelta(seconds=800)))
    timeouts = {"claude-code:test-agent/daily-summary": 900.0}
    assert w.scan_unbracketed(log, now=NOW, timeouts=timeouts) == []
    # Past 900 + 120 seconds of grace, it is no longer a slow run; it is a dead one.
    later = NOW + timedelta(seconds=400)
    assert [r.run_id for r in w.scan_unbracketed(log, now=later, timeouts=timeouts)] == ["slow"]


def test_a_run_with_no_known_timeout_still_cannot_hang_forever(tmp_path: Path) -> None:
    """A run with no known deadline still must not be allowed to hang forever."""
    log = write_log(tmp_path / "events.jsonl", started("orphan", ts=NOW - timedelta(hours=6)))
    assert [r.run_id for r in w.scan_unbracketed(log, now=NOW)] == ["orphan"]


def test_a_half_written_line_is_skipped_not_raised_on(tmp_path: Path) -> None:
    """The fallback log is appended to by several processes; a torn last line is ordinary."""
    log = tmp_path / "events.jsonl"
    log.write_text(
        started("gone", ts=NOW - timedelta(hours=2)) + "\n\n{not json at all\n",
        encoding="utf-8",
    )
    assert [r.run_id for r in w.scan_unbracketed(log, now=NOW)] == ["gone"]


def test_a_missing_log_is_no_stale_runs(tmp_path: Path) -> None:
    """A steward that has never fallen back has nothing to bury."""
    assert w.scan_unbracketed(tmp_path / "never-written.jsonl", now=NOW) == []


def test_a_run_only_the_registry_knows_about_is_still_found(store: Store, tmp_path: Path) -> None:
    """The case the log could never answer: every event reached burrow, the session did not."""
    store.open_run(
        run_id="delivered",
        kind="routine",
        agent_id="claude-code:test-agent",
        project="test-agent",
        ref="daily-summary",
        timeout_s=900.0,
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )

    stale = w.scan_unbracketed(tmp_path / "nothing.jsonl", now=NOW, registry=store)

    assert [run.run_id for run in stale] == ["delivered"]
    assert stale[0].routine == "daily-summary"


def test_a_registry_run_is_judged_against_the_timeout_it_was_given(
    store: Store, tmp_path: Path
) -> None:
    """The row carries the deadline the session actually got, budget cap included."""
    store.open_run(
        run_id="capped",
        kind="routine",
        agent_id="claude-code:test-agent",
        ref="daily-summary",
        timeout_s=60.0,
        now=ev.utc_now_iso(NOW - timedelta(seconds=150)),
    )
    log = tmp_path / "nothing.jsonl"
    # The manifest declares 900s; the registry says this run was only ever given 60, and
    # the registry is what a run past 60 + 120 seconds of grace is judged against.
    timeouts = {"claude-code:test-agent/daily-summary": 900.0}

    assert w.scan_unbracketed(log, now=NOW, registry=store, timeouts=timeouts) == []
    later = NOW + timedelta(seconds=60)
    stale = w.scan_unbracketed(log, now=later, registry=store, timeouts=timeouts)
    assert [run.run_id for run in stale] == ["capped"]


def test_a_closed_registry_row_is_not_a_stale_run(store: Store, tmp_path: Path) -> None:
    """A session that reported back closed its own row, and the scan never sees it."""
    store.open_run(
        run_id="done",
        kind="routine",
        agent_id="claude-code:test-agent",
        ref="daily-summary",
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )
    store.close_run("done", now=ev.utc_now_iso(NOW - timedelta(hours=1)))

    assert w.scan_unbracketed(tmp_path / "nothing.jsonl", now=NOW, registry=store) == []


def test_a_closing_event_in_the_log_answers_an_open_registry_row(
    store: Store, tmp_path: Path
) -> None:
    """Steward died between emitting the finish and writing the row. The finish wins."""
    store.open_run(
        run_id="fine",
        kind="routine",
        agent_id="claude-code:test-agent",
        ref="daily-summary",
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )
    log = write_log(tmp_path / "events.jsonl", finished("fine", ts=NOW - timedelta(hours=1)))

    assert w.scan_unbracketed(log, now=NOW, registry=store) == []


def test_a_stale_task_is_closed_in_the_registry_but_not_mourned_twice(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The board's lease sweep owns a task's death; the watchdog only answers the row."""
    store.open_run(
        run_id="task-1",
        kind="task",
        agent_id="claude-code:test-agent",
        project="test-agent",
        ref="task-1",
        timeout_s=900.0,
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )

    report = build(resident, store, sink, tmp_path).tick(NOW)

    assert [run.run_id for run in report.buried] == ["task-1"]
    assert store.open_runs() == [], "the registry row is answered"
    assert [e for e in sink.events if e.type == ev.ROUTINE_FAILED] == [], "and nothing invented"


def test_a_task_closing_event_in_the_log_answers_an_open_registry_row(
    store: Store, tmp_path: Path
) -> None:
    """The same recovery routines get, for the events that name a task rather than a run.

    Steward died between emitting ``task_done`` and answering the row — or the write that
    should have answered it threw. The log says this task finished, so the session that
    finished it is not a resident to report as down (steward #39).
    """
    store.open_run(
        run_id="session-1",
        kind="task",
        agent_id="claude-code:test-agent",
        project="test-agent",
        ref="task-1",
        timeout_s=900.0,
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )
    log = write_log(
        tmp_path / "events.jsonl",
        task_closed("task-1", ts=NOW - timedelta(hours=1), run_id="session-1"),
    )

    assert w.scan_unbracketed(log, now=NOW, registry=store) == []


def test_a_second_attempt_at_one_task_is_watched_like_the_first(
    store: Store, tmp_path: Path
) -> None:
    """A row per session, not per task: a retry that vanishes is still found.

    The board's ordinary flow is claim, die, expire the lease, re-claim. Keying the
    registry on the task id meant the second attempt hit a row the first one had already
    closed, the insert was dropped, and the retry ran unwatched — which is the exact
    death this registry exists to catch.
    """

    def attempt(run_id: str, *, ago: timedelta) -> bool:
        return store.open_run(
            run_id=run_id,
            kind="task",
            agent_id="claude-code:test-agent",
            project="test-agent",
            ref="task-1",
            timeout_s=900.0,
            now=ev.utc_now_iso(NOW - ago),
        )

    attempt("session-1", ago=timedelta(hours=4))
    store.close_run("session-1", now=ev.utc_now_iso(NOW - timedelta(hours=3)))
    assert attempt("session-2", ago=timedelta(hours=2)), "the retry gets a row of its own"

    stale = w.scan_unbracketed(tmp_path / "nothing.jsonl", now=NOW, registry=store)

    assert [run.run_id for run in stale] == ["session-2"]


def test_the_close_of_a_dead_first_attempt_does_not_answer_the_retry(
    store: Store, tmp_path: Path
) -> None:
    """The retry flow again, with the log the first attempt's death actually leaves behind.

    Claim, die, expire the lease, re-claim: the sweep's ``task_failed`` for the first
    attempt is appended to this log and stays there for ever. It names the *task*, which
    the retry shares, so matching on the id alone let one old line silence every later
    attempt — the retry ran unwatched, which is exactly what the registry is for.

    Nor is "only a close after the row opened" enough. One dispatch pass expires the dead
    lease and re-claims the task, so the sweep's ``task_failed`` is stamped *after* the
    retry's row opens: the timestamps here are the ones that flow actually produces.
    """
    opened = NOW - timedelta(hours=2)
    store.open_run(
        run_id="session-2",
        kind="task",
        agent_id="claude-code:test-agent",
        project="test-agent",
        ref="task-1",
        timeout_s=900.0,
        now=ev.utc_now_iso(opened),
    )
    log = write_log(
        tmp_path / "events.jsonl",
        task_closed("task-1", ts=opened + timedelta(milliseconds=1), type="task_failed"),
    )

    stale = w.scan_unbracketed(log, now=NOW, registry=store)

    assert [run.run_id for run in stale] == ["session-2"]
    assert w.answered_runs(log, store) == [], "and nothing quietly closes its row either"
    # And the retry's own close, once it lands, does answer it: it names the session.
    log = write_log(
        tmp_path / "events.jsonl",
        task_closed("task-1", ts=opened + timedelta(milliseconds=1), type="task_failed"),
        task_closed("task-1", ts=NOW - timedelta(hours=1), run_id="session-2"),
    )
    assert w.scan_unbracketed(log, now=NOW, registry=store) == []


def test_a_row_the_log_has_answered_is_closed_rather_than_left_open(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Nobody else ever comes back for it: the scan filters it out before burial can.

    Steward died between emitting the finish and writing the close. The finish means this
    is no death to mourn — and the row still has to go, or ``open_runs`` keeps a session
    that ended hours ago and every pass from here to forever re-reads it.
    """
    store.open_run(
        run_id="fine",
        kind="routine",
        agent_id="claude-code:test-agent",
        project="test-agent",
        ref="daily-summary",
        timeout_s=900.0,
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )
    log = write_log(tmp_path / "events.jsonl", finished("fine", ts=NOW - timedelta(hours=1)))

    report = build(resident, store, sink, tmp_path, fallback=log).tick(NOW)

    assert report.buried == (), "a run the log answered is not a death"
    assert store.open_runs() == [], "and not an open row either"
    assert [e for e in sink.events if e.type == ev.ROUTINE_FAILED] == []


def test_a_task_row_the_log_has_answered_is_closed_too(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The same for the events that name a task: answered, closed, and nothing announced."""
    store.open_run(
        run_id="session-1",
        kind="task",
        agent_id="claude-code:test-agent",
        project="test-agent",
        ref="task-1",
        timeout_s=900.0,
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )
    log = write_log(
        tmp_path / "events.jsonl",
        task_closed("task-1", ts=NOW - timedelta(hours=1), run_id="session-1"),
    )

    report = build(resident, store, sink, tmp_path, fallback=log).tick(NOW)

    assert report.buried == ()
    assert store.open_runs() == []


def test_a_buried_registry_run_is_closed_so_the_next_pass_stays_quiet(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """One death, one event, and a row that is not read as a fresh outage a minute later."""
    store.open_run(
        run_id="gone",
        kind="routine",
        agent_id="claude-code:test-agent",
        project="test-agent",
        ref="daily-summary",
        timeout_s=900.0,
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )
    dog = build(resident, store, sink, tmp_path)

    first = dog.tick(NOW)
    second = dog.tick(NOW + timedelta(minutes=1))

    assert [run.run_id for run in first.buried] == ["gone"]
    assert second.buried == ()
    assert store.open_runs() == []
    failures = [e for e in sink.events if e.type == ev.ROUTINE_FAILED]
    assert [e.payload["run_id"] for e in failures] == ["gone"]


def test_an_unbracketed_run_is_closed_as_routine_failed_exactly_once(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The village never shows eternal work — and never shows one death twice."""
    log = write_log(tmp_path / "events.jsonl", started("gone", ts=NOW - timedelta(hours=2)))
    dog = build(resident, store, sink, tmp_path, fallback=log)

    first = dog.tick(NOW)
    second = dog.tick(NOW + timedelta(minutes=1))

    assert [run.run_id for run in first.buried] == ["gone"]
    assert second.buried == ()
    failures = [e for e in sink.events if e.type == ev.ROUTINE_FAILED]
    assert len(failures) == 1
    assert failures[0].agent_id == "claude-code:test-agent"
    assert failures[0].payload == {
        "routine": "daily-summary",
        "run_id": "gone",
        "error": w.NEVER_REPORTED_BACK,
    }
    # No made-up duration: steward does not know how long that run lasted.
    assert "duration_s" not in failures[0].payload


# --------------------------------------------------------------------------------------
# the local probe
# --------------------------------------------------------------------------------------


def test_a_scheduler_anchor_that_stopped_advancing_is_a_complaint(
    resident: Resident, store: Store, tmp_path: Path
) -> None:
    """Every tick re-anchors what it visits, so a stale anchor means nobody is visiting."""
    state = SchedulerState(path=tmp_path / "state.json")
    state.set_anchor("test-agent/daily-summary", NOW - timedelta(days=3))
    probe = w.LocalProbe(store=store, state=state, fallback=tmp_path / "nothing.jsonl")

    health = probe.health(resident, NOW)

    assert health.down
    assert "daily-summary" in health.detail
    assert probe.restart(resident) is False


def test_a_fresh_anchor_is_nothing_stuck_which_is_not_the_same_as_up(
    resident: Resident, store: Store, tmp_path: Path
) -> None:
    """This probe detects stuckness. Finding none is not a claim that anything is running."""
    state = SchedulerState(path=tmp_path / "state.json")
    state.set_anchor("test-agent/daily-summary", NOW)
    probe = w.LocalProbe(store=store, state=state, fallback=tmp_path / "nothing.jsonl")
    health = probe.health(resident, NOW)
    assert not health.down
    assert not health.known
    assert "not the same as up" in health.detail


def test_a_routine_never_seen_by_a_scheduler_is_not_called_stuck(
    resident: Resident, store: Store, tmp_path: Path
) -> None:
    """Never run and stopped running are different facts, and only one is a fault."""
    probe = w.LocalProbe(
        store=store,
        state=SchedulerState(path=tmp_path / "state.json"),
        fallback=tmp_path / "nothing.jsonl",
    )
    assert not probe.health(resident, NOW).down


def test_a_lease_held_past_its_expiry_is_a_complaint(
    resident: Resident, store: Store, tmp_path: Path
) -> None:
    """The board sweeps these every dispatch, so a standing one means nobody swept."""
    store.post_job(title="tidy the notes")
    store.claim_next_job(
        claimant=resident.agent_id,
        skills=(),
        lease_expires_at=ev.utc_now_iso(NOW - timedelta(hours=1)),
        now=ev.utc_now_iso(NOW - timedelta(hours=2)),
    )
    probe = w.LocalProbe(
        store=store,
        state=SchedulerState(path=tmp_path / "state.json"),
        fallback=tmp_path / "nothing.jsonl",
    )
    health = probe.health(resident, NOW)
    assert health.down
    assert "held past its lease" in health.detail


def test_a_buried_run_stops_counting_against_the_probe(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The log is append-only, so one old line must not keep a resident down for good."""
    log = write_log(tmp_path / "events.jsonl", started("gone", ts=NOW - timedelta(hours=2)))
    state = SchedulerState(path=tmp_path / "state.json")
    probe = w.LocalProbe(store=store, state=state, fallback=log)
    assert probe.health(resident, NOW).down

    build(resident, store, sink, tmp_path, fallback=log, state=state).tick(NOW)

    assert not probe.health(resident, NOW).down


# --------------------------------------------------------------------------------------
# docker
# --------------------------------------------------------------------------------------


def test_docker_reports_a_running_container(resident: Resident, stub_bin: StubWriter) -> None:
    """The happy path, against a real process rather than a mock."""
    stub_bin("docker", 'echo "$@" >> "$TMPDIR/docker-argv.txt"; echo true')
    health = w.DockerSupervisor().health(resident, NOW)
    assert health.known
    assert health.alive
    assert "steward-test-agent" in health.detail


def test_docker_reports_a_stopped_container(resident: Resident, stub_bin: StubWriter) -> None:
    """A container that is not running is down, and the supervisor says which one."""
    stub_bin("docker", "echo false")
    health = w.DockerSupervisor().health(resident, NOW)
    assert health.down
    assert "not running" in health.detail


def test_docker_that_cannot_answer_is_not_a_dead_resident(
    resident: Resident, stub_bin: StubWriter
) -> None:
    """Not being able to see a resident is not the same as that resident being down."""
    stub_bin("docker", 'echo "no such object" >&2; exit 1')
    health = w.DockerSupervisor().health(resident, NOW)
    assert not health.known
    assert not health.down
    assert "could not answer" in health.detail


def test_a_resident_with_no_container_is_unsupervised_not_healthy(
    write_resident: ResidentWriter,
) -> None:
    """An undeclared container is a gap steward names rather than papers over."""
    data = watched_manifest()
    data.pop("deploy")
    resident = load_manifest(write_resident(data))
    supervisor = w.DockerSupervisor()
    health = supervisor.health(resident, NOW)
    assert not health.known
    assert "no deploy.container" in health.detail
    assert supervisor.restart(resident) is False


def test_docker_restart_calls_docker_restart(
    resident: Resident, stub_bin: StubWriter, tmp_path: Path
) -> None:
    """The intervention is the command it claims to be, argv and all."""
    argv_dump = tmp_path / "argv.txt"
    stub_bin("docker", f'printf "%s\\n" "$@" >> "{argv_dump}"')
    assert w.DockerSupervisor().restart(resident) is True
    assert argv_dump.read_text().splitlines() == ["restart", "steward-test-agent"]


def test_a_failed_docker_restart_is_reported_as_failed(
    resident: Resident, stub_bin: StubWriter
) -> None:
    """Docker refusing is an answer, not an exception."""
    stub_bin("docker", 'echo "daemon not running" >&2; exit 1')
    assert w.DockerSupervisor().restart(resident) is False


@pytest.mark.usefixtures("empty_path")
def test_a_missing_docker_is_an_outcome_not_a_crash() -> None:
    """A watchdog that crashes is worse than the thing it was watching."""
    outcome = run_argv(["definitely-not-a-real-binary", "inspect"])
    assert not outcome.ok
    assert "cannot launch" in outcome.summary()


def test_a_command_that_hangs_is_given_up_on(stub_bin: StubWriter) -> None:
    """A control-plane call that does not answer in seconds is itself the answer."""
    stub_bin("slowpoke", "sleep 5")
    outcome = run_argv(["slowpoke"], timeout_s=0.2)
    assert not outcome.ok
    assert "did not answer" in outcome.summary()


# --------------------------------------------------------------------------------------
# restarts, backoff, and giving up
# --------------------------------------------------------------------------------------


def test_a_restart_is_announced_with_its_reason_and_attempt(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """A silent restart would let the village show an unbroken villager. Never."""
    supervisor = StubSupervisor(alive=False)
    dog = build(resident, store, sink, tmp_path, supervisors=[supervisor])

    report = dog.tick(NOW)

    assert supervisor.restarts == ["test-agent"]
    assert [h.resident_id for h in report.restarted] == ["test-agent"]
    restarts = [e for e in sink.events if e.type == ev.RESIDENT_RESTARTED]
    assert len(restarts) == 1
    assert restarts[0].agent_id == "claude-code:test-agent"
    assert restarts[0].payload == {
        "reason": "container exited",
        "attempt": 1,
        "supervisor": "stub",
    }


def test_restarts_back_off_then_stop_and_knock_once(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Three attempts, on the declared schedule, and then a person is woken — once."""

    # docker says the restart worked every time; the container simply dies again before
    # the next pass. That is what a crash loop actually looks like from out here.
    class NeverStaysUp(StubSupervisor):
        def restart(self, resident: Resident) -> bool:
            self.restarts.append(resident.id)
            return True  # docker said yes; the container dies again before the next pass

    supervisor = NeverStaysUp(alive=False)
    dog = build(resident, store, sink, tmp_path, supervisors=[supervisor])

    moments = [
        NOW,  # attempt 1
        NOW + timedelta(seconds=30),  # inside the first backoff: nothing happens
        NOW + timedelta(seconds=61),  # attempt 2
        NOW + timedelta(seconds=90),  # inside the second backoff
        NOW + timedelta(seconds=362),  # attempt 3
        NOW + timedelta(seconds=400),  # inside the third backoff
        NOW + timedelta(seconds=1863),  # out of attempts: give up
        NOW + timedelta(seconds=2000),  # and stay quiet about it
    ]
    reports = [dog.tick(moment) for moment in moments]

    assert len(supervisor.restarts) == w.MAX_ATTEMPTS
    assert [e.payload["attempt"] for e in sink.events if e.type == ev.RESIDENT_RESTARTED] == [
        1,
        2,
        3,
    ]
    assert [i for i, r in enumerate(reports) if r.gave_up] == [6]
    knocks = [e for e in sink.events if e.type == ev.NEEDS_HUMAN]
    assert len(knocks) == 1
    assert knocks[0].payload["action"] == w.RESTART_FAILED_ACTION
    assert knocks[0].payload["detail"]["attempts"] == w.MAX_ATTEMPTS
    assert "container exited" in knocks[0].payload["message"]
    assert store.watchdog_attempt("test-agent").gave_up


def test_a_supervisor_that_cannot_restart_asks_for_a_human_at_once(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Counting failed attempts nobody can win is a slow way of saying "ask a human"."""
    dog = build(
        resident,
        store,
        sink,
        tmp_path,
        supervisors=[StubSupervisor(alive=False, restarts_work=False)],
    )

    report = dog.tick(NOW)

    assert [h.resident_id for h in report.gave_up] == ["test-agent"]
    assert ev.RESIDENT_RESTARTED not in [e.type for e in sink.events]
    knocks = [e for e in sink.events if e.type == ev.NEEDS_HUMAN]
    assert len(knocks) == 1
    assert "nothing here can restart it" in knocks[0].payload["message"]


def test_the_backoff_survives_a_watchdog_that_is_itself_restarted(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Three attempts means three, not three per process."""
    first = StubSupervisor(alive=False)
    build(resident, store, sink, tmp_path, supervisors=[first]).tick(NOW)
    assert store.watchdog_attempt("test-agent").attempts == 1

    # A brand-new watchdog object, the same durable store.
    second = StubSupervisor(alive=False)
    build(resident, store, sink, tmp_path, supervisors=[second]).tick(NOW + timedelta(seconds=5))

    # Still inside the first backoff window, so the fresh process does not restart again.
    assert second.restarts == []


def test_a_resident_that_comes_back_forgets_its_restart_history(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """A bad night should not spend the restart budget of the next one."""
    supervisor = StubSupervisor(alive=False)
    dog = build(resident, store, sink, tmp_path, supervisors=[supervisor])
    dog.tick(NOW)
    assert store.watchdog_attempt("test-agent").attempts == 1

    supervisor.alive = True
    dog.tick(NOW + timedelta(hours=1))

    assert store.watchdog_attempt("test-agent").attempts == 0


def test_a_flapping_container_still_respects_the_three_attempt_cap(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """An 'I cannot tell' reading between crashes must not reset the restart budget."""

    class Flapping:
        kind = "docker"

        def __init__(self) -> None:
            self.pass_no = 0
            self.restarts: list[str] = []

        def health(self, resident: Resident, now: datetime) -> w.Health:  # noqa: ARG002
            self.pass_no += 1
            if self.pass_no % 2 == 1:  # crashed
                return w.Health(
                    resident_id=resident.id,
                    alive=False,
                    detail="container exited",
                    supervisor="docker",
                )
            return w.Health(  # ...and a moment later docker cannot answer at all
                resident_id=resident.id,
                known=False,
                detail="docker could not answer",
                supervisor="docker",
            )

        def restart(self, resident: Resident) -> bool:
            self.restarts.append(resident.id)
            return True  # docker says yes; the container dies again before the next pass

    supervisor = Flapping()
    dog = build(resident, store, sink, tmp_path, supervisors=[supervisor])

    # Twenty passes, each far enough apart that a down pass is always past the backoff.
    for i in range(20):
        dog.tick(NOW + timedelta(minutes=30 * i))

    assert len(supervisor.restarts) == w.MAX_ATTEMPTS, "three attempts, not one per flap"
    assert store.watchdog_attempt("test-agent").gave_up


def test_a_crash_loop_knocks_once_even_when_the_reading_flaps(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """One knock per crash loop — a flap to 'unknown' must not wipe the give-up receipt."""

    class FlappingDead:
        kind = "docker"

        def __init__(self) -> None:
            self.pass_no = 0

        def health(self, resident: Resident, now: datetime) -> w.Health:  # noqa: ARG002
            self.pass_no += 1
            if self.pass_no % 2 == 1:
                return w.Health(
                    resident_id=resident.id,
                    alive=False,
                    detail="container exited",
                    supervisor="docker",
                )
            return w.Health(
                resident_id=resident.id,
                known=False,
                detail="docker could not answer",
                supervisor="docker",
            )

        def restart(self, resident: Resident) -> bool:  # noqa: ARG002
            return False  # nothing here can bring it back

    dog = build(resident, store, sink, tmp_path, supervisors=[FlappingDead()])
    for i in range(12):
        dog.tick(NOW + timedelta(minutes=30 * i))

    knocks = [e for e in sink.events if e.type == ev.NEEDS_HUMAN]
    assert len(knocks) == 1, "one knock per crash loop, not one per flap"


def test_a_dead_container_is_restarted_even_when_the_local_probe_also_complains(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """A LocalProbe complaint must not mask a container DockerSupervisor could restart."""
    local = StubSupervisor(alive=False, restarts_work=False)
    local.kind = "local"
    docker = StubSupervisor(alive=False, restarts_work=True)
    docker.kind = "docker"

    report = build(resident, store, sink, tmp_path, supervisors=[local, docker]).tick(NOW)

    assert local.restarts == ["test-agent"], "the local probe was asked first"
    assert docker.restarts == ["test-agent"], "and docker got its chance despite that"
    assert [h.supervisor for h in report.restarted] == ["docker"]
    assert report.gave_up == ()
    restarts = [e for e in sink.events if e.type == ev.RESIDENT_RESTARTED]
    assert restarts[0].payload["supervisor"] == "docker"


def test_a_run_whose_finish_was_delivered_remotely_is_not_buried(
    resident: Resident, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bracket split across a transient outage is no longer read as a dead run."""
    fallback = tmp_path / "events.jsonl"
    emitter = ev.EventEmitter(url="https://village.example", fallback=fallback)
    monkeypatch.setattr(ev.EventEmitter, "_post", lambda *_args: True)  # burrow takes everything

    ctx = ev.RunContext(
        agent_id=resident.agent_id,
        project=resident.project,
        routine="daily-summary",
        run_id="live",
    )
    assert emitter.emit(ctx.started("schedule")) is True
    assert emitter.emit(ctx.finished(outcome="ok", artifacts=[], duration_s=1.0)) is True

    # Both were delivered remotely — and both are in the local record the watchdog scans.
    stale = w.scan_unbracketed(
        fallback,
        now=datetime.now(UTC) + timedelta(hours=3),
        timeouts=w.timeouts_for([resident]),
    )
    assert stale == [], "a completed run is not unbracketed just because burrow got the finish"


def test_the_watchdog_reloads_scheduler_state_each_tick(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The scheduler advances anchors on disk; a watchdog frozen at boot woke people for it."""
    state_path = tmp_path / "state.json"
    # As it stood when the watchdog was built, this routine had not been visited in days.
    stale = SchedulerState(path=state_path)
    stale.set_anchor("test-agent/daily-summary", NOW - timedelta(days=3))
    stale.save()
    probe = w.LocalProbe(
        store=store, state=SchedulerState.load(state_path), fallback=tmp_path / "e.jsonl"
    )
    dog = w.Watchdog(
        residents=[resident],
        store=store,
        emitter=sink,
        supervisors=[probe],
        state=SchedulerState.load(state_path),
        fallback=tmp_path / "e.jsonl",
    )

    # The scheduler process advances the anchor on disk before the watchdog's next pass.
    advanced = SchedulerState(path=state_path)
    advanced.set_anchor("test-agent/daily-summary", NOW)
    advanced.save()

    report = dog.tick(NOW)

    assert not any(reading.down for reading in report.health), "the fresh on-disk anchor is seen"
    assert [e for e in sink.events if e.type == ev.NEEDS_HUMAN] == [], (
        "no human woken for a fine fleet"
    )


def test_a_supervisor_that_cannot_see_a_resident_triggers_nothing(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Unsupervised is reported as unsupervised, and nothing is bounced over it."""
    supervisor = StubSupervisor(alive=False, known=False)
    report = build(resident, store, sink, tmp_path, supervisors=[supervisor]).tick(NOW)
    assert supervisor.restarts == []
    assert report.restarted == ()
    assert not report.health[0].known


def test_a_broken_supervisor_does_not_take_the_pass_down(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The watchdog outlives its collaborators, or it is not a watchdog."""

    class Exploding:
        kind = "boom"

        def health(self, resident: Resident, now: datetime) -> w.Health:  # noqa: ARG002
            raise RuntimeError("no")

        def restart(self, resident: Resident) -> bool:  # noqa: ARG002
            return False

    report = build(resident, store, sink, tmp_path, supervisors=[Exploding()]).tick(NOW)
    assert not report.health[0].known
    assert report.interventions == 0


def test_one_supervisor_seeing_a_dead_container_beats_another_seeing_a_live_anchor(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The worst *real* answer wins; a healthy reading does not cancel a sick one."""
    healthy = StubSupervisor(alive=True)
    healthy.kind = "local"
    dead = StubSupervisor(alive=False)
    report = build(resident, store, sink, tmp_path, supervisors=[healthy, dead]).tick(NOW)
    assert report.restarted
    assert report.restarted[0].supervisor == "stub"
    assert dead.restarts == ["test-agent"]


# --------------------------------------------------------------------------------------
# the pass as a whole
# --------------------------------------------------------------------------------------


def test_a_pass_sweeps_the_deadlines_the_board_already_knows_how_to_sweep(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """One idea of "expired", visited from two places rather than implemented twice."""
    store.post_job(title="tidy the notes")
    store.claim_next_job(
        claimant=resident.agent_id,
        skills=(),
        lease_expires_at=ev.utc_now_iso(NOW - timedelta(minutes=1)),
        now=ev.utc_now_iso(NOW - timedelta(hours=1)),
    )

    report = build(resident, store, sink, tmp_path).tick(NOW)

    assert [job.title for job in report.reopened] == ["tidy the notes"]
    assert store.jobs("open")
    failures = [e for e in sink.events if e.type == ev.TASK_FAILED]
    assert failures[0].payload["reason"] == "lease_expired"


def test_a_pass_pauses_a_resident_that_has_spent_its_day(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """A cap trips even on a day nothing was scheduled, so the fuel gauge is right."""
    resident = load_manifest(write_resident(watched_manifest(budgets={"daily_cost_usd": 1.0})))
    guard = bg.BudgetGuard(store, sink)
    # Seed the day's spend straight onto the ledger, not through the guard, so the resident
    # is over budget but not yet paused — it is the watchdog's own pass that must pause it.
    store.record_run(
        resident=resident.manifest.id,
        agent_id=resident.manifest.agent_id or "",
        kind="routine",
        run_id="a",
        cost_usd=4.0,
        now=ev.utc_now_iso(NOW),
    )
    dog = build(resident, store, sink, tmp_path, guard=guard)

    first = dog.tick(NOW)
    second = dog.tick(NOW + timedelta(minutes=1))

    assert first.paused == ("test-agent",)
    assert second.paused == ()  # already paused: reported once, knocked once
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1
    assert store.budget_pause("test-agent") is not None


def test_a_pass_is_recorded_so_doctor_can_say_when_anything_last_looked(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """Steward has to be able to say out loud that nothing is watching."""
    assert store.last_watchdog_pass() is None
    dog = build(resident, store, sink, tmp_path)
    dog.tick(NOW)
    dog.tick(NOW + timedelta(minutes=1))
    last = store.last_watchdog_pass()
    assert last is not None
    assert last["passes"] == 2
    assert last["last_pass_at"] == ev.utc_now_iso(NOW + timedelta(minutes=1))


def test_a_broken_sweep_does_not_end_the_pass(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The deadlines are one job of several; losing one must not lose the rest."""

    class BrokenSweeper:
        def dispatch(self, now: datetime) -> object:  # noqa: ARG002
            raise RuntimeError("the board is unreachable")

    log = write_log(tmp_path / "events.jsonl", started("gone", ts=NOW - timedelta(hours=3)))
    report = build(resident, store, sink, tmp_path, fallback=log, sweeper=BrokenSweeper()).tick(NOW)
    assert [run.run_id for run in report.buried] == ["gone"]
    assert report.reopened == ()


def test_run_makes_the_passes_it_was_asked_for(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The loop is a loop; the sleep between passes is injectable, like everywhere else."""
    slept: list[float] = []
    dog = build(resident, store, sink, tmp_path)
    passes = dog.run(interval_s=5.0, max_passes=3, sleep=slept.append, now_fn=lambda: NOW)
    assert len(passes) == 3
    assert slept == [5.0, 5.0]


def test_a_quiet_pass_reports_that_it_did_nothing(
    resident: Resident, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """A watchdog with nothing to do is cheap, silent, and honest about it."""
    report = build(resident, store, sink, tmp_path).tick(NOW)
    assert not report
    assert report.interventions == 0
    assert report.to_dict()["interventions"] == 0
    assert sink.events == []


def test_from_path_leaves_out_a_manifest_it_cannot_read(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """Restarting a container named in a manifest nobody has checked is acting on a rumour."""
    root = tmp_path / "residents"
    write_resident(watched_manifest(), root=root)
    write_resident({"id": "broken", "version": 0}, root=root, directory="broken", soul=None)
    dog = w.Watchdog.from_path(root, store, emitter=ev.NullEmitter())
    assert [r.id for r in dog.residents] == ["test-agent"]


def test_the_default_supervisors_are_the_two_this_module_ships(
    resident: Resident, store: Store
) -> None:
    """The seam is wired by default: what steward can see, plus what #4 will hand it."""
    dog = w.Watchdog(residents=[resident], store=store)
    assert [supervisor.kind for supervisor in dog.supervisors] == ["local", "docker"]
