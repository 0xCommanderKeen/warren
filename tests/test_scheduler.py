"""The scheduler: when things fire, exactly once, and what the village is told."""

import fcntl
import json
import os
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import RESIDENTS_DIR, VALID_SOUL, ResidentWriter, valid_manifest
from steward import events as ev
from steward import journal as j
from steward import manifest as m
from steward import prompt as p
from steward import runners as r
from steward import scheduler as s
from steward import skills as sk
from steward.store import Store

LJUBLJANA = ZoneInfo("Europe/Ljubljana")


def routine(**overrides: object) -> m.Routine:
    data: dict = {
        "id": "daily-summary",
        "schedule": "0 7 * * *",
        "schedule_tz": "Europe/Ljubljana",
        "prompt": "Write the summary.",
        "timeout_s": 900,
        "enabled": True,
    }
    data.update(overrides)
    return m.Routine.model_validate(data)


def manifest_with(*routines: dict, **top: object) -> dict:
    data = valid_manifest()
    data["routines"] = list(routines)
    data["runner"] = {"kind": "mock", "model": "pretend"}
    data.update(top)
    return data


HOURLY = {
    "id": "inbox-read",
    "schedule": "15 * * * *",
    "schedule_tz": "Europe/Ljubljana",
    "prompt": "Read the mail.",
    "timeout_s": 60,
    "enabled": True,
}
DAILY = {
    "id": "daily-summary",
    "schedule": "0 7 * * *",
    "schedule_tz": "Europe/Ljubljana",
    "prompt": "Write the summary.",
    "requires": ["daily-summary"],
    "timeout_s": 900,
    "enabled": True,
}


CLOSER = {
    "id": "close-of-day",
    "schedule": "30 22 * * *",
    "schedule_tz": "Europe/Ljubljana",
    "prompt": "Look back over the day.",
    "timeout_s": 600,
    "enabled": True,
    "journal": "close_of_day",
}


@pytest.fixture
def build(write_resident: ResidentWriter, tmp_path: Path):
    """Build a scheduler over a throwaway resident, with a fallback-only emitter."""

    def _build(
        *routines: dict,
        catchup_s: float = s.DEFAULT_CATCHUP_S,
        runner_factory: s.RunnerFactory = r.build_runner,
        memory: dict | None = None,
    ) -> s.Scheduler:
        data = manifest_with(*routines)
        if memory is not None:
            data["memory"] = memory
        path = write_resident(data)
        return s.Scheduler(
            s.load_scheduled(path.parent),
            emitter=ev.EventEmitter(fallback=tmp_path / "events.jsonl"),
            state=s.SchedulerState(path=tmp_path / "state.json"),
            workdir=tmp_path,
            catchup_s=catchup_s,
            runner_factory=runner_factory,
        )

    return _build


@pytest.fixture
def journaling(build, tmp_path: Path):
    """Build a scheduler whose resident keeps a real journal in a throwaway directory."""

    def _build(*routines: dict, output: str = "done", **memory: object) -> s.Scheduler:
        return build(
            *routines,
            memory={"kind": "directory", "path": str(tmp_path / "memory"), **memory},
            runner_factory=lambda _spec: r.MockRunner(
                behavior=lambda _req: r.RunResult(
                    outcome=r.Outcome.OK, output=output, exit_status=0
                )
            ),
        )

    return _build


def emitted(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- cron and tz


def test_next_fire_is_computed_in_the_routines_own_zone() -> None:
    after = datetime(2026, 8, 24, 4, 0, tzinfo=UTC)  # 06:00 in Ljubljana
    fire = s.next_fire_after(routine(), after)
    assert fire.astimezone(LJUBLJANA).hour == 7
    assert fire.astimezone(UTC) == datetime(2026, 8, 24, 5, 0, tzinfo=UTC)


def test_utc_and_ljubljana_disagree_by_an_hour_in_summer() -> None:
    after = datetime(2026, 8, 24, 4, 0, tzinfo=UTC)
    in_utc = s.next_fire_after(routine(schedule_tz="UTC"), after)
    in_local = s.next_fire_after(routine(), after)
    assert in_utc.astimezone(UTC) - in_local.astimezone(UTC) == timedelta(hours=2)


def test_the_default_zone_is_utc() -> None:
    assert routine(schedule_tz="UTC").schedule_tz == "UTC"
    assert (
        m.Routine.model_validate(
            {"id": "x", "schedule": "0 7 * * *", "prompt": "p", "timeout_s": 60}
        ).schedule_tz
        == "UTC"
    )


def test_spring_forward_does_not_skip_a_day() -> None:
    """29 March 2026: 02:00-03:00 does not exist in Ljubljana. A 02:30 routine still runs."""
    nightly = routine(schedule="30 2 * * *")
    before = datetime(2026, 3, 28, 12, 0, tzinfo=LJUBLJANA)

    first = s.next_fire_after(nightly, before)
    assert first.astimezone(LJUBLJANA).strftime("%Y-%m-%d %H:%M") == "2026-03-29 03:00"

    second = s.next_fire_after(nightly, first)
    assert second.astimezone(LJUBLJANA).strftime("%Y-%m-%d %H:%M") == "2026-03-30 02:30"


def test_autumn_fall_back_fires_once() -> None:
    """25 October 2026: 02:00-03:00 happens twice. One wall-clock slot, one run."""
    nightly = routine(schedule="30 2 * * *")
    before = datetime(2026, 10, 24, 12, 0, tzinfo=LJUBLJANA)

    fire = s.next_fire_after(nightly, before)
    assert fire.astimezone(LJUBLJANA).strftime("%Y-%m-%d %H:%M %Z") == "2026-10-25 02:30 CEST"

    following = s.next_fire_after(nightly, fire)
    assert following.astimezone(LJUBLJANA).strftime("%Y-%m-%d %H:%M") == "2026-10-26 02:30", (
        "the repeated hour is the same slot twice; a second run is one the schedule never asked for"
    )


def test_hourly_and_daily_schedules_agree_with_the_manifest() -> None:
    scheduled = s.load_scheduled(RESIDENTS_DIR)
    keys = {item.key for item in scheduled}
    assert keys == {
        "life-agent/daily-summary",
        "life-agent/inbox-read",
        "life-agent/close-of-day",
        "pip/heartbeat",
    }
    assert all(item.routine.schedule_tz == "Europe/Ljubljana" for item in scheduled)


# ------------------------------------------------------------------------------ due-ness


def test_a_new_routine_is_anchored_at_first_sight_and_does_not_fire_for_existing(build) -> None:
    engine = build(DAILY)
    now = datetime(2026, 8, 24, 9, 4, tzinfo=UTC)
    assert engine.due(now) == []
    assert engine.state.anchor("test-agent/daily-summary") == now


def test_tick_fires_a_due_routine_exactly_once(build, tmp_path: Path) -> None:
    engine = build(HOURLY)
    engine.due(datetime(2026, 8, 24, 10, 0, tzinfo=UTC))  # anchor: 12:00 local

    due_at = datetime(2026, 8, 24, 10, 15, 30, tzinfo=UTC)  # 12:15:30 local
    reports = engine.tick(due_at)
    assert [report.routine_id for report in reports] == ["inbox-read"]
    assert reports[0].fired

    assert engine.tick(due_at + timedelta(seconds=5)) == []
    assert engine.tick(due_at + timedelta(minutes=10)) == []

    types = [event["type"] for event in emitted(tmp_path / "events.jsonl")]
    assert types == ["routine_started", "routine_finished"]


def test_a_disabled_routine_never_fires(build) -> None:
    engine = build({**HOURLY, "enabled": False}, DAILY)
    assert [item.routine.id for item in engine.scheduled] == ["daily-summary"]


def test_steward_does_not_back_fill_a_schedule_it_slept_through(build) -> None:
    engine = build(HOURLY)
    engine.due(datetime(2026, 8, 24, 10, 0, tzinfo=UTC))

    # The daemon was down for hours, and the last occurrence is stale too.
    much_later = datetime(2026, 8, 24, 14, 22, tzinfo=UTC)
    assert engine.tick(much_later) == []
    assert engine.state.anchor("test-agent/inbox-read") == much_later

    # And the next real occurrence fires normally.
    assert len(engine.tick(datetime(2026, 8, 24, 15, 15, 10, tzinfo=UTC))) == 1


def test_a_fire_inside_the_catchup_window_still_runs(build) -> None:
    engine = build(HOURLY, catchup_s=300.0)
    engine.due(datetime(2026, 8, 24, 10, 0, tzinfo=UTC))
    barely_late = datetime(2026, 8, 24, 11, 17, tzinfo=UTC)  # 105s after 13:15 local
    assert len(engine.tick(barely_late)) == 1


# ------------------------------------------------------------------------------ restart


def test_a_restart_neither_refires_nor_forgets(write_resident: ResidentWriter, tmp_path: Path):
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    fallback = tmp_path / "events.jsonl"

    def fresh_daemon() -> s.Scheduler:
        return s.Scheduler(
            s.load_scheduled(path.parent),
            emitter=ev.EventEmitter(fallback=fallback),
            state=s.SchedulerState.load(state_path),
            workdir=tmp_path,
        )

    first = fresh_daemon()
    first.due(datetime(2026, 8, 24, 10, 0, tzinfo=UTC))
    assert len(first.tick(datetime(2026, 8, 24, 10, 15, 10, tzinfo=UTC))) == 1
    assert state_path.is_file()

    # Restart two minutes later. The 12:15 run already happened; it must not happen twice.
    second = fresh_daemon()
    assert second.tick(datetime(2026, 8, 24, 10, 17, tzinfo=UTC)) == []
    assert [e["type"] for e in emitted(fallback)] == ["routine_started", "routine_finished"]

    # The next hour still fires.
    assert len(second.tick(datetime(2026, 8, 24, 11, 15, 10, tzinfo=UTC))) == 1


def test_corrupt_or_missing_state_is_simply_empty(tmp_path: Path) -> None:
    missing = s.SchedulerState.load(tmp_path / "nope.json")
    assert missing.anchors == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert s.SchedulerState.load(broken).anchors == {}

    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text('{"routines": [1, 2]}', encoding="utf-8")
    assert s.SchedulerState.load(wrong_shape).anchors == {}

    bad_anchor = tmp_path / "bad.json"
    bad_anchor.write_text('{"routines": {"a/b": {"anchor": "whenever"}}}', encoding="utf-8")
    assert s.SchedulerState.load(bad_anchor).anchor("a/b") is None


def test_a_naive_anchor_is_read_as_utc(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"routines": {"a/b": {"anchor": "2026-08-24T07:00:00"}}}', encoding="utf-8")
    assert s.SchedulerState.load(path).anchor("a/b") == datetime(2026, 8, 24, 7, 0, tzinfo=UTC)


def test_state_round_trips(tmp_path: Path) -> None:
    state = s.SchedulerState(path=tmp_path / "deep" / "state.json")
    moment = datetime(2026, 8, 24, 7, 0, tzinfo=LJUBLJANA)
    state.set_anchor("a/b", moment)
    state.save()
    assert s.SchedulerState.load(state.path).anchor("a/b") == moment


# --------------------------------------------------------------------------- heartbeat


def test_the_heartbeat_round_trips(tmp_path: Path) -> None:
    state = s.SchedulerState(path=tmp_path / "state.json")
    assert state.last_tick_at() is None, "a state nobody has ticked has never ticked"

    moment = datetime(2026, 8, 24, 7, 0, tzinfo=LJUBLJANA)
    state.record_tick(moment)
    state.save()
    assert s.SchedulerState.load(state.path).last_tick_at() == moment


def test_a_state_file_written_before_the_heartbeat_reads_as_never_ticked(tmp_path: Path) -> None:
    # Old files must stay readable, and the safe reading of a file with no heartbeat in it
    # is that nothing has ticked — never that something is up.
    path = tmp_path / "old.json"
    path.write_text('{"routines": {"a/b": {"anchor": "2026-08-24T07:00:00+00:00"}}}', "utf-8")
    state = s.SchedulerState.load(path)
    assert state.anchor("a/b") is not None
    assert state.last_tick_at() is None


def test_an_unreadable_heartbeat_is_no_heartbeat(tmp_path: Path) -> None:
    junk = tmp_path / "junk.json"
    junk.write_text('{"routines": {}, "last_tick": "whenever"}', encoding="utf-8")
    assert s.SchedulerState.load(junk).last_tick_at() is None

    wrong_type = tmp_path / "wrong.json"
    wrong_type.write_text('{"routines": {}, "last_tick": 17}', encoding="utf-8")
    assert s.SchedulerState.load(wrong_type).last_tick_at() is None

    # A heartbeat survives a routines block that does not parse: they are separate facts.
    partial = tmp_path / "partial.json"
    partial.write_text('{"routines": [1, 2], "last_tick": "2026-08-24T07:00:00+00:00"}', "utf-8")
    assert s.SchedulerState.load(partial).last_tick_at() is not None


def test_liveness_tells_never_ticked_apart_from_stopped(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    state = s.SchedulerState(path=tmp_path / "state.json")

    fresh = s.scheduler_liveness(state, now)
    assert fresh["alive"] is None, "never ticked is its own answer, not a dead daemon"
    assert fresh["last_tick"] is None
    assert fresh["stale_after_s"] == s.STALE_TICK_AFTER_S

    state.record_tick(now - timedelta(seconds=s.STALE_TICK_AFTER_S - 1))
    assert s.scheduler_liveness(state, now)["alive"] is True

    state.record_tick(now - timedelta(seconds=s.STALE_TICK_AFTER_S + 1))
    assert s.scheduler_liveness(state, now)["alive"] is False


def test_a_tick_stamps_the_heartbeat_even_with_nothing_due(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.EventEmitter(fallback=tmp_path / "events.jsonl"),
        state=s.SchedulerState(path=state_path),
        workdir=tmp_path,
    )
    quiet = datetime(2026, 8, 24, 10, 40, tzinfo=UTC)  # nothing is due at :40
    assert engine.tick(quiet) == []

    assert s.SchedulerState.load(state_path).last_tick_at() == quiet


def test_the_daemon_stamps_the_heartbeat_every_iteration(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    # An idle daemon is still a live one, and the loop is the only place that can say so.
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.EventEmitter(fallback=tmp_path / "events.jsonl"),
        state=s.SchedulerState(path=state_path),
        workdir=tmp_path,
    )
    moments = iter(
        [datetime(2026, 8, 24, 10, 40, tzinfo=UTC) + timedelta(minutes=i) for i in range(3)]
    )
    engine.run(max_ticks=2, sleep=lambda _s: None, now_fn=lambda: next(moments))

    assert s.SchedulerState.load(state_path).last_tick_at() == datetime(
        2026, 8, 24, 10, 41, tzinfo=UTC
    )


def _blocking_runner() -> tuple[threading.Event, threading.Event, s.RunnerFactory]:
    """Build a runner that announces it started and then hangs, as a long session would."""
    started = threading.Event()
    release = threading.Event()

    def slow(_request: r.RunRequest) -> r.RunResult:
        started.set()
        release.wait(timeout=5)
        return r.RunResult(outcome=r.Outcome.OK, exit_status=0)

    return started, release, lambda _spec: r.MockRunner(behavior=slow)


def _beat_after(path: Path, after: datetime) -> datetime | None:
    """Poll the state file for a heartbeat stamped at or after ``after``."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        beat = s.SchedulerState.load(path).last_tick_at()
        if beat is not None and beat >= after:
            return beat
        time.sleep(0.01)
    return None


def test_the_heartbeat_keeps_stamping_while_a_fire_blocks_the_daemon(
    build, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bug this exists to prevent: the loop cannot come round while it is inside a
    # 15-minute routine, so a heartbeat that only lands between wake-ups goes stale exactly
    # when a run is in flight — and every reader calls the daemon running it dead.
    monkeypatch.setattr(s, "HEARTBEAT_EVERY_S", 0.01)
    started, release, factory = _blocking_runner()
    engine = build(HOURLY, runner_factory=factory)
    moment = datetime(2026, 8, 24, 10, 15, 30, tzinfo=UTC)
    engine.state.set_anchor(engine.scheduled[0].key, moment - timedelta(hours=1))
    watching_since = datetime.now(UTC)

    daemon = threading.Thread(
        target=lambda: engine.run(max_ticks=1, sleep=lambda _s: None, now_fn=lambda: moment)
    )
    daemon.start()
    try:
        assert started.wait(timeout=5), "the routine never fired"
        beat = _beat_after(tmp_path / "state.json", watching_since)
    finally:
        release.set()
        daemon.join(timeout=5)

    assert beat is not None, "the heartbeat went quiet while the daemon was mid-run"


def test_a_cron_tick_keeps_stamping_while_it_fires(
    build, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other deployment: `steward scheduler tick` fires inside its lock, with no loop to
    # come round at all, so the heartbeat is the only thing that can say it is still here.
    monkeypatch.setattr(s, "HEARTBEAT_EVERY_S", 0.01)
    started, release, factory = _blocking_runner()
    engine = build(HOURLY, runner_factory=factory)
    moment = datetime(2026, 8, 24, 10, 15, 30, tzinfo=UTC)
    engine.state.set_anchor(engine.scheduled[0].key, moment - timedelta(hours=1))
    engine.state.save()
    watching_since = datetime.now(UTC)

    cron = threading.Thread(target=lambda: engine.tick(moment))
    cron.start()
    try:
        assert started.wait(timeout=5), "the routine never fired"
        beat = _beat_after(tmp_path / "state.json", watching_since)
    finally:
        release.set()
        cron.join(timeout=5)

    assert beat is not None, "the heartbeat went quiet while the tick was mid-run"


def test_a_dry_run_leaves_no_heartbeat(write_resident: ResidentWriter, tmp_path: Path) -> None:
    # A rehearsal must not make the fleet look attended: nothing is firing on its account.
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        state=s.SchedulerState(path=state_path),
        workdir=tmp_path,
        dry_run=True,
    )
    engine.run(
        max_ticks=1, sleep=lambda _s: None, now_fn=lambda: datetime(2026, 8, 24, 10, 40, tzinfo=UTC)
    )

    assert not state_path.exists()


def test_a_stamp_writes_the_heartbeat_and_nothing_else(tmp_path: Path) -> None:
    """A heartbeat carries one fact, so an anchor somebody else saved survives it."""
    path = tmp_path / "state.json"
    mine = s.SchedulerState.load(path)
    mine.set_anchor("a/hourly", datetime(2026, 8, 24, 10, 0, tzinfo=UTC))
    mine.save()

    # Another tick process anchors the 10:05 occurrence over the top of it.
    theirs = s.SchedulerState.load(path)
    theirs.set_anchor("a/hourly", datetime(2026, 8, 24, 10, 5, tzinfo=UTC))
    theirs.save()

    # ... and now my heartbeat lands, still holding the 10:00 snapshot in memory.
    mine.stamp(datetime(2026, 8, 24, 10, 6, tzinfo=UTC))

    written = s.SchedulerState.load(path)
    assert written.anchor("a/hourly") == datetime(2026, 8, 24, 10, 5, tzinfo=UTC)
    assert written.last_tick_at() == datetime(2026, 8, 24, 10, 6, tzinfo=UTC)
    assert mine.last_tick_at() == datetime(2026, 8, 24, 10, 6, tzinfo=UTC), "and in memory too"


# -------------------------------------------------------------------------- concurrency


def test_an_overlapping_fire_is_skipped_not_queued(build, tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow(_request: r.RunRequest) -> r.RunResult:
        started.set()
        release.wait(timeout=5)
        return r.RunResult(outcome=r.Outcome.OK, exit_status=0)

    engine = build(HOURLY, runner_factory=lambda _spec: r.MockRunner(behavior=slow))
    item = engine.scheduled[0]

    worker = threading.Thread(target=engine.fire, args=(item,))
    worker.start()
    try:
        assert started.wait(timeout=5)
        overlapping = engine.fire(item)
    finally:
        release.set()
        worker.join(timeout=5)

    assert not overlapping.fired
    assert overlapping.skipped_reason == "already running"
    types = [event["type"] for event in emitted(tmp_path / "events.jsonl")]
    assert types == ["routine_started", "routine_finished"], "the skipped fire emitted nothing"


# ------------------------------------------------------------------------------ outcomes


def test_a_timeout_is_reported_as_routine_failed(build, tmp_path: Path) -> None:
    timed_out = r.RunResult(
        outcome=r.Outcome.TIMEOUT,
        duration_s=900.0,
        error="exceeded its 900s timeout and was killed",
    )
    engine = build(HOURLY, runner_factory=lambda _spec: r.MockRunner(behavior=lambda _: timed_out))
    report = engine.fire(engine.scheduled[0])

    assert report.fired
    events = emitted(tmp_path / "events.jsonl")
    assert [event["type"] for event in events] == ["routine_started", "routine_failed"]
    failure = events[1]["payload"]
    assert failure["error"].startswith("timeout:")
    assert "was killed" in failure["error"]
    assert failure["duration_s"] == 900.0
    assert failure["run_id"] == events[0]["payload"]["run_id"]


def test_a_runner_that_explodes_is_a_failed_routine_not_a_crash(build, tmp_path: Path) -> None:
    def explode(_spec: object) -> r.Runner:
        raise RuntimeError("the config is nonsense")

    engine = build(HOURLY, runner_factory=explode)
    report = engine.fire(engine.scheduled[0])
    assert report.result is not None
    assert report.result.outcome is r.Outcome.FAILED
    events = emitted(tmp_path / "events.jsonl")
    assert events[1]["type"] == "routine_failed"
    assert "the config is nonsense" in events[1]["payload"]["error"]


def test_every_emitted_event_is_a_valid_v0_event(build, tmp_path: Path) -> None:
    engine = build(HOURLY)
    engine.fire(engine.scheduled[0])
    for event in emitted(tmp_path / "events.jsonl"):
        assert ev.validate_event(event) == (), event
        assert event["source"] == "steward"
        assert event["agent_id"] == "claude-code:test-agent"


def test_artifacts_are_reported_when_the_runner_actually_knows_them(build, tmp_path: Path):
    with_artifacts = r.RunResult(
        outcome=r.Outcome.OK, exit_status=0, duration_s=1.5, artifacts=("journal/today.md",)
    )
    engine = build(
        HOURLY, runner_factory=lambda _spec: r.MockRunner(behavior=lambda _: with_artifacts)
    )
    engine.fire(engine.scheduled[0])
    finished = emitted(tmp_path / "events.jsonl")[1]
    assert finished["payload"]["artifacts"] == ["journal/today.md"]
    assert finished["payload"]["outcome"] == "ok"
    assert finished["payload"]["duration_s"] == 1.5


# ---------------------------------------------------------------------------- the prompt


def test_the_session_is_told_charter_voice_and_task(build) -> None:
    engine = build(DAILY)
    prompt = engine.build_prompt(engine.scheduled[0])
    assert "Flat, factual, short." in prompt
    assert "Never send email without explicit approval." in prompt
    assert prompt.index("Flat, factual, short.") < prompt.index("HARD RULES")
    assert prompt.rstrip().endswith("Write the summary.")


def test_the_session_inherits_this_residents_identity(build) -> None:
    engine = build(HOURLY)
    item = engine.scheduled[0]
    env = engine._session_env(item, "run-1")
    assert env["BURROW_AGENT_ID"] == "claude-code:test-agent"
    assert env["BURROW_PROJECT"] == "test-agent"
    assert env["STEWARD_ROUTINE"] == "inbox-read"


def test_the_prompt_reaches_the_runner_intact(build) -> None:
    seen: list[r.RunRequest] = []
    mock = r.MockRunner()

    def factory(_spec: object) -> r.Runner:
        return mock

    engine = build(HOURLY, runner_factory=factory)
    engine.fire(engine.scheduled[0])
    seen = mock.requests
    assert len(seen) == 1
    assert "YOUR CHARTER" in seen[0].prompt
    assert seen[0].timeout_s == 60
    assert seen[0].model == "pretend"


# --------------------------------------------------------------------------- the journal

EVENING = datetime(2026, 8, 24, 20, 30, tzinfo=UTC)  # 22:30 in Ljubljana


def test_only_the_flagged_routine_is_told_to_close_the_day(journaling) -> None:
    engine = journaling(DAILY, HOURLY, CLOSER)
    prompts = {item.routine.id: engine.build_prompt(item, EVENING) for item in engine.scheduled}

    assert p.CLOSING_TITLE in prompts["close-of-day"]
    assert p.CLOSING_TITLE not in prompts["daily-summary"]
    assert p.CLOSING_TITLE not in prompts["inbox-read"]
    assert "2026-08-24.md" in prompts["close-of-day"], "the day is read in the routine's zone"


def test_the_closing_session_is_still_told_who_it_is_and_how_it_writes(journaling) -> None:
    """A close-of-day run is an ordinary session: identity, voice, journal, charter, task."""
    engine = journaling(CLOSER)
    text = engine.build_prompt(engine.scheduled[0], EVENING)

    positions = [
        text.index("WHO YOU ARE"),
        text.index("YOUR WRITING VOICE (STYLE ONLY)"),
        text.index("YOUR CHARTER (AUTHORITATIVE, LAST WORD)"),
        text.index("YOUR TASK RIGHT NOW"),
        text.index(p.CLOSING_TITLE),
    ]
    assert positions == sorted(positions)
    assert "Flat, factual, short." in text
    assert text.index("Flat, factual, short.") < text.index("HARD RULES")


def test_the_closing_instruction_carries_the_markers_and_the_precedence(journaling) -> None:
    engine = journaling(CLOSER)
    text = engine.build_prompt(engine.scheduled[0], EVENING)
    assert "<journal>" in text
    assert "</journal>" in text
    assert text.index("HARD RULES") < text.index(p.CLOSING_TITLE), (
        "the charter still comes first; closing the day is a task like any other"
    )


def test_the_next_session_opens_with_the_last_entry(journaling) -> None:
    engine = journaling(HOURLY)
    manifest = engine.scheduled[0].resident.manifest
    j.write_entry(manifest, date(2026, 8, 23), "close-of-day", "Two drafts are still waiting.")

    text = engine.build_prompt(engine.scheduled[0], EVENING)
    assert "YOUR JOURNAL FROM LAST TIME" in text
    assert "Two drafts are still waiting." in text
    assert text.index("Two drafts are still waiting.") < text.index("HARD RULES")


def test_a_resident_with_no_journal_yet_is_told_about_no_journal(journaling) -> None:
    engine = journaling(HOURLY)
    assert "YOUR JOURNAL FROM LAST TIME" not in engine.build_prompt(engine.scheduled[0], EVENING)


def test_a_session_that_writes_its_own_entry_keeps_it(journaling, tmp_path: Path) -> None:
    engine = journaling(CLOSER)
    manifest = engine.scheduled[0].resident.manifest
    own = j.write_entry(manifest, date(2026, 8, 24), "close-of-day", "Written in my own hand.")

    report = engine.fire(engine.scheduled[0], now=EVENING)
    assert report.journal_path == own
    assert "Written in my own hand." in own.read_text(encoding="utf-8")
    finished = emitted(tmp_path / "events.jsonl")[1]
    assert finished["payload"]["artifacts"] == [], (
        "steward did not write that file, so steward does not claim it"
    )


def test_a_journal_block_in_the_output_is_persisted_and_claimed(journaling, tmp_path: Path):
    engine = journaling(CLOSER, output="All done.\n<journal>The inbox was quiet.</journal>")
    report = engine.fire(engine.scheduled[0], now=EVENING)

    assert report.journal_path is not None
    assert report.journal_path.name == "2026-08-24.md"
    assert "The inbox was quiet." in report.journal_path.read_text(encoding="utf-8")

    finished = emitted(tmp_path / "events.jsonl")[1]
    assert finished["payload"]["artifacts"] == [str(report.journal_path)]


def test_the_sessions_own_file_beats_the_block_it_also_emitted(journaling) -> None:
    engine = journaling(CLOSER, output="<journal>steward's copy</journal>")
    manifest = engine.scheduled[0].resident.manifest
    own = j.write_entry(manifest, date(2026, 8, 24), "close-of-day", "my own words")

    report = engine.fire(engine.scheduled[0], now=EVENING)
    assert report.journal_path == own
    assert "steward's copy" not in own.read_text(encoding="utf-8")


def test_a_run_that_says_nothing_leaves_no_entry_behind(journaling) -> None:
    engine = journaling(CLOSER, output="I ran out of time.")
    report = engine.fire(engine.scheduled[0], now=EVENING)
    assert report.journal_path is None
    assert j.latest_entry(engine.scheduled[0].resident.manifest) is None


def test_a_failed_close_of_day_writes_nothing(build, tmp_path: Path) -> None:
    failed = r.RunResult(outcome=r.Outcome.FAILED, output="<journal>half a thought</journal>")
    engine = build(
        CLOSER,
        memory={"kind": "directory", "path": str(tmp_path / "memory")},
        runner_factory=lambda _spec: r.MockRunner(behavior=lambda _: failed),
    )
    report = engine.fire(engine.scheduled[0], now=EVENING)
    assert report.journal_path is None
    assert j.latest_entry(engine.scheduled[0].resident.manifest) is None


def test_an_ordinary_routine_never_writes_a_journal(journaling) -> None:
    engine = journaling(HOURLY, output="<journal>not my job</journal>")
    report = engine.fire(engine.scheduled[0])
    assert report.journal_path is None
    assert j.latest_entry(engine.scheduled[0].resident.manifest) is None


def test_a_late_evening_close_writes_the_local_day_not_the_utc_one(journaling) -> None:
    engine = journaling(
        {**CLOSER, "schedule": "30 0 * * *"}, output="<journal>after midnight</journal>"
    )
    # 22:30 UTC on the 24th is 00:30 on the 25th where the household is.
    report = engine.fire(engine.scheduled[0], now=datetime(2026, 8, 24, 22, 30, tzinfo=UTC))
    assert report.journal_path is not None
    assert report.journal_path.name == "2026-08-25.md"


def test_closing_the_day_rotates_the_journal(journaling) -> None:
    engine = journaling(CLOSER, output="<journal>tonight</journal>", journal_keep=2)
    manifest = engine.scheduled[0].resident.manifest
    for day in (21, 22, 23):
        j.write_entry(manifest, date(2026, 8, day), "close-of-day", f"the {day}th")

    engine.fire(engine.scheduled[0], now=EVENING)
    assert [e.date.day for e in j.read_entries(manifest, 100)] == [24, 23]


def test_a_memory_block_with_nowhere_to_journal_is_a_startup_error(build, stub_bin) -> None:
    stub_bin("claude", "exit 0")
    engine = build(HOURLY, memory={"kind": "file", "path": "/data/test-agent/memory.md"})
    problems = engine.check()
    assert len(problems) == 1
    assert "memory.kind" in problems[0]
    with pytest.raises(s.SchedulerError, match="nowhere to keep one entry per day"):
        engine.require_ready()


def test_an_unreachable_journal_is_a_missing_journal_not_a_failed_routine(
    journaling, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> str:
        raise OSError("the volume is gone")

    engine = journaling(CLOSER, output="<journal>tonight</journal>")
    monkeypatch.setattr(j, "latest_entry", explode)
    monkeypatch.setattr(j, "persist_close_of_day", explode)

    report = engine.fire(engine.scheduled[0], now=EVENING)
    assert report.fired
    assert report.result is not None
    assert report.result.ok, "a journal steward cannot reach does not fail the routine"
    assert report.journal_path is None
    assert "YOUR JOURNAL FROM LAST TIME" not in report.prompt


def test_a_journal_declaration_steward_cannot_use_never_fails_a_fire(build, tmp_path: Path):
    engine = build(
        HOURLY,
        memory={"kind": "file", "path": str(tmp_path / "memory.md")},
        runner_factory=lambda _spec: r.MockRunner(),
    )
    report = engine.fire(engine.scheduled[0])
    assert report.fired
    assert "YOUR JOURNAL FROM LAST TIME" not in report.prompt


def test_a_project_scoped_resident_gets_a_steward_agent_id(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = manifest_with(HOURLY)
    del data["agent_id"]
    data["project"] = "burrow"
    path = write_resident(data, soul="---\nname: Testy\n---\nbody\n")
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.EventEmitter(fallback=tmp_path / "e.jsonl"),
        state=s.SchedulerState(path=tmp_path / "state.json"),
    )
    assert engine.scheduled[0].agent_id == "steward:test-agent"
    assert engine.scheduled[0].project == "burrow"


def test_the_session_runs_in_the_declared_memory_directory(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    memory = tmp_path / "memory"
    memory.mkdir()
    data = manifest_with(HOURLY)
    data["memory"] = {"kind": "directory", "path": str(memory)}
    path = write_resident(data)
    item = s.load_scheduled(path.parent)[0]
    assert item.workdir(tmp_path / "elsewhere") == memory


def test_an_absent_memory_directory_falls_back(build, tmp_path: Path) -> None:
    engine = build(HOURLY)
    assert engine.scheduled[0].workdir(tmp_path) == tmp_path


# ---------------------------------------------------------------------------- dry run


def test_a_dry_run_prints_a_prompt_and_emits_nothing(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(manifest_with(DAILY, HOURLY))
    fallback = tmp_path / "events.jsonl"
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.EventEmitter(fallback=fallback),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        dry_run=True,
    )
    reports = [engine.fire(item) for item in engine.scheduled]

    assert [report.routine_id for report in reports] == ["daily-summary", "inbox-read"]
    assert all(not report.fired for report in reports)
    assert all(report.skipped_reason == "dry run" for report in reports)
    assert all("YOUR CHARTER" in report.prompt for report in reports)
    assert not fallback.exists(), "a rehearsal is not work"


def test_a_dry_run_leaves_no_state_behind(write_resident: ResidentWriter, tmp_path: Path) -> None:
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        state=s.SchedulerState(path=state_path),
        workdir=tmp_path,
        dry_run=True,
    )
    engine.tick(datetime(2026, 8, 24, 11, 15, tzinfo=UTC))
    assert not state_path.exists()


def test_a_dry_run_never_builds_a_real_runner(write_resident: ResidentWriter, tmp_path: Path):
    data = manifest_with(HOURLY)
    data["runner"] = {"kind": "claude", "model": "claude-opus-5"}
    path = write_resident(data)
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        dry_run=True,
    )
    declared = engine.scheduled[0].resident.manifest.runner
    assert declared.kind == "claude"
    assert isinstance(engine._runner_factory(declared), r.MockRunner)
    engine.require_ready()  # a rehearsal is never blocked by a missing binary


# ----------------------------------------------------------------------- startup checks


@pytest.mark.usefixtures("empty_path")
def test_the_daemon_refuses_to_start_without_the_declared_binary(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = manifest_with(HOURLY)
    data["runner"] = {"kind": "claude", "model": "claude-opus-5"}
    path = write_resident(data)
    engine = s.Scheduler(
        s.load_scheduled(path.parent), state=s.SchedulerState(path=tmp_path / "state.json")
    )
    problems = engine.check()
    assert len(problems) == 1
    assert "not on PATH" in problems[0]
    with pytest.raises(s.SchedulerError, match="not on PATH"):
        engine.require_ready()


def test_an_invalid_residents_tree_never_reaches_the_scheduler(
    write_resident: ResidentWriter,
) -> None:
    data = valid_manifest()
    del data["charter"]
    path = write_resident(data)
    with pytest.raises(s.SchedulerError, match="charter"):
        s.load_scheduled(path.parent)


def test_an_over_cap_voice_is_refused_at_schedule_time_by_name(
    write_resident: ResidentWriter,
) -> None:
    bloated = "---\nname: Testy\n---\nbody\n\n## Voice\n" + ("x" * (m.VOICE_MAX_CHARS + 1))
    path = write_resident(manifest_with(HOURLY), soul=bloated)
    with pytest.raises(s.SchedulerError) as raised:
        s.load_scheduled(path.parent)
    assert "soul.md" in str(raised.value), "the error names the file to edit"
    assert str(m.VOICE_MAX_CHARS) in str(raised.value)


def test_two_routines_may_not_both_close_the_day(write_resident: ResidentWriter) -> None:
    second = {**CLOSER, "id": "also-closing", "schedule": "0 23 * * *"}
    path = write_resident(manifest_with(CLOSER, second))
    with pytest.raises(s.SchedulerError, match="a day that ends more than once is not a day"):
        s.load_scheduled(path.parent)


def test_a_routine_that_fires_hourly_may_not_close_the_day(write_resident: ResidentWriter) -> None:
    path = write_resident(manifest_with({**HOURLY, "journal": "close_of_day"}))
    with pytest.raises(s.SchedulerError, match="fires 24 times a day"):
        s.load_scheduled(path.parent)


def test_an_edited_voice_takes_effect_on_the_next_load(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Edit the soul, and the next session gets the new voice. No steward restart beyond that."""
    path = write_resident(manifest_with(HOURLY))

    def prompt_now() -> str:
        engine = s.Scheduler(
            s.load_scheduled(path.parent),
            emitter=ev.NullEmitter(),
            state=s.SchedulerState(path=tmp_path / "state.json"),
            workdir=tmp_path,
        )
        return engine.build_prompt(engine.scheduled[0])

    assert "Flat, factual, short." in prompt_now()
    soul = path.parent / "soul.md"
    soul.write_text(
        soul.read_text(encoding="utf-8").replace("Flat, factual, short.", "Warmer now."),
        encoding="utf-8",
    )
    assert "Warmer now." in prompt_now()
    assert "Flat, factual, short." not in prompt_now()


# ------------------------------------------------------------------ voice emits nothing


def test_a_voiced_session_emits_no_event_of_its_own(build, tmp_path: Path) -> None:
    """Personality is expressed only in work products. It adds nothing to the village."""
    engine = build(HOURLY)
    prompt = engine.build_prompt(engine.scheduled[0])
    assert "Flat, factual, short." in prompt, "the voice really is in this session"

    engine.fire(engine.scheduled[0])
    events = emitted(tmp_path / "events.jsonl")

    assert [event["type"] for event in events] == ["routine_started", "routine_finished"]
    assert not any("Flat, factual, short." in json.dumps(event) for event in events), (
        "a voice reaches the session and never the event log"
    )


def test_a_voiceless_resident_emits_exactly_the_same_events(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    def types_for(soul: str, name: str) -> list[str]:
        fallback = tmp_path / f"{name}.jsonl"
        path = write_resident(manifest_with(HOURLY), soul=soul, root=tmp_path / name)
        engine = s.Scheduler(
            s.load_scheduled(path.parent),
            emitter=ev.EventEmitter(fallback=fallback),
            state=s.SchedulerState(path=tmp_path / f"{name}-state.json"),
            workdir=tmp_path,
        )
        engine.fire(engine.scheduled[0])
        return [event["type"] for event in emitted(fallback)]

    voiceless = "---\nname: Testy\n---\nA villager with no voice at all.\n"
    assert types_for(VALID_SOUL, "voiced") == types_for(voiceless, "plain")


# --------------------------------------------------------------------------- the daemon


def test_the_daemon_sleeps_until_the_next_occurrence_then_fires(build, tmp_path: Path) -> None:
    clock = [datetime(2026, 8, 24, 11, 0, tzinfo=UTC)]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += timedelta(seconds=max(seconds, 1))

    engine = build(HOURLY)
    reports = engine.run(max_ticks=20, sleep=sleep, now_fn=lambda: clock[0])

    assert [report.routine_id for report in reports] == ["inbox-read"]
    assert slept, "the daemon waits rather than spinning"
    assert max(slept) <= s.MAX_SLEEP_S
    assert [e["type"] for e in emitted(tmp_path / "events.jsonl")] == [
        "routine_started",
        "routine_finished",
    ]


def test_a_daemon_with_nothing_to_do_still_sleeps(build) -> None:
    slept: list[float] = []
    engine = build()
    assert engine.run(max_ticks=2, sleep=slept.append, now_fn=lambda: datetime.now(UTC)) == []
    assert slept == [s.MAX_SLEEP_S, s.MAX_SLEEP_S]


def test_upcoming_reports_every_routine_with_its_next_fire(build) -> None:
    engine = build(DAILY, HOURLY)
    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    upcoming = list(engine.upcoming(now))
    assert {item.routine.id for item, _ in upcoming} == {"daily-summary", "inbox-read"}
    assert all(moment > now for _, moment in upcoming)


def test_upcoming_defaults_to_now(build) -> None:
    engine = build(HOURLY)
    assert len(list(engine.upcoming())) == 1


def test_tick_defaults_to_now(build) -> None:
    engine = build(HOURLY)
    assert engine.tick() == []  # first sight anchors; nothing is due yet


# ------------------------------------------------------------------------------- skills


@pytest.fixture
def provisioned(write_resident: ResidentWriter, write_skill, tmp_path: Path):
    """Build a scheduler over a resident, a real skills library, and a chosen runner kind."""

    def _build(
        *,
        granted: list[str] | None = None,
        runner_kind: str = "claude",
        recorder: list[r.RunRequest] | None = None,
    ) -> s.Scheduler:
        write_skill("write-journal", defaults=True, body="Write the entry.\n")
        write_skill("read-inbox", body="Read the mail. Never send.\n")
        data = manifest_with(HOURLY, skills=granted if granted is not None else ["read-inbox"])
        data["runner"] = {"kind": runner_kind, "model": "pretend"}
        path = write_resident(data)

        def factory(spec: m.Runner) -> r.Runner:
            def behavior(request: r.RunRequest) -> r.RunResult:
                if recorder is not None:
                    recorder.append(request)
                return r.RunResult(outcome=r.Outcome.OK, output="done", exit_status=0)

            return r.MockRunner(spec, behavior=behavior)

        return s.Scheduler(
            s.load_scheduled(path.parent),
            emitter=ev.EventEmitter(fallback=tmp_path / "events.jsonl"),
            state=s.SchedulerState(path=tmp_path / "state.json"),
            workdir=tmp_path / "work",
            runner_factory=factory,
            library=sk.load_library(tmp_path / "skills"),
        )

    return _build


def test_a_session_is_told_the_defaults_plus_its_own_grants(provisioned) -> None:
    engine = provisioned()
    prompt = engine.build_prompt(engine.scheduled[0])
    assert "YOUR SKILLS (HOW-TO, NOT AUTHORITY)" in prompt
    assert prompt.index("# write-journal") < prompt.index("# read-inbox")
    assert prompt.index("# read-inbox") < prompt.index("HARD RULES")
    assert "Read the mail. Never send." in prompt


def test_a_skill_removed_from_a_manifest_is_absent_from_the_next_session(provisioned) -> None:
    with_grant = provisioned().build_prompt(provisioned().scheduled[0])
    without = provisioned(granted=[]).build_prompt(provisioned(granted=[]).scheduled[0])
    assert "read-inbox" in with_grant
    assert "read-inbox" not in without
    assert "write-journal" in without, "the defaults are not something a manifest can drop"


def test_a_claude_session_gets_its_skills_on_disk_before_the_run(
    provisioned, tmp_path: Path
) -> None:
    engine = provisioned()
    engine.fire(engine.scheduled[0])
    root = tmp_path / "work" / ".claude" / "skills"
    assert sorted(p.name for p in root.iterdir()) == ["read-inbox", "write-journal"]
    assert "Read the mail." in (root / "read-inbox" / "SKILL.md").read_text(encoding="utf-8")


def test_materializing_is_idempotent_across_runs(provisioned, tmp_path: Path) -> None:
    engine = provisioned()
    engine.fire(engine.scheduled[0])
    target = tmp_path / "work" / ".claude" / "skills" / "read-inbox" / "SKILL.md"
    stamp = target.stat().st_mtime_ns
    engine.fire(engine.scheduled[0])
    assert target.stat().st_mtime_ns == stamp, "an unchanged skill is not rewritten every hour"


def test_a_revoked_skill_is_removed_from_the_working_directory(provisioned, tmp_path: Path) -> None:
    first = provisioned()
    first.fire(first.scheduled[0])
    root = tmp_path / "work" / ".claude" / "skills"
    assert (root / "read-inbox").is_dir()

    revoked = provisioned(granted=[])
    revoked.fire(revoked.scheduled[0])
    assert not (root / "read-inbox").exists()
    assert (root / "write-journal").is_dir()


def test_a_runner_that_does_not_load_skills_gets_the_prompt_only(
    provisioned, tmp_path: Path
) -> None:
    requests: list[r.RunRequest] = []
    engine = provisioned(runner_kind="mock", recorder=requests)
    engine.fire(engine.scheduled[0])
    assert not (tmp_path / "work" / ".claude").exists()
    assert "Read the mail. Never send." in requests[0].prompt


def stale_grant(
    write_resident, write_skill, tmp_path: Path
) -> tuple[s.ScheduledRoutine, sk.SkillLibrary]:
    """Build a resident granted a skill the library does not have.

    Validation refuses this tree, so getting here means the library changed under a
    manifest that was valid when it was read — which is exactly the race the pre-run
    check exists for. The resident is loaded against an empty library to stand in for
    that, then fired against the real one.
    """
    write_skill("write-journal", defaults=True)
    path = write_resident(manifest_with(HOURLY, skills=["errands"]))
    resident = m.load_manifest(path, skills_dir=tmp_path / "no-library-here")
    item = s.ScheduledRoutine(resident=resident, routine=resident.manifest.routines[0])
    return item, sk.load_library(tmp_path / "skills")


def test_a_granted_skill_the_library_lacks_fails_the_run_loudly(
    write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    """Not a silent skip: a session must never believe it holds what nobody gave it."""
    item, library = stale_grant(write_resident, write_skill, tmp_path)
    engine = s.Scheduler(
        [item],
        emitter=ev.EventEmitter(fallback=tmp_path / "events.jsonl"),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path / "work",
        runner_factory=r.MockRunner,
        library=library,
    )
    report = engine.fire(item)

    assert report.fired is True
    assert report.result is not None
    assert report.result.outcome is r.Outcome.FAILED
    assert "errands" in (report.result.error or "")

    events = emitted(tmp_path / "events.jsonl")
    assert [event["type"] for event in events] == ["routine_started", "routine_failed"]
    assert "does not have" in events[-1]["payload"]["error"]
    assert not (tmp_path / "work" / ".claude").exists(), "nothing is written for a refused run"


def test_a_missing_skill_is_a_startup_complaint_too(
    write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    item, library = stale_grant(write_resident, write_skill, tmp_path)
    engine = s.Scheduler(
        [item], state=s.SchedulerState(path=tmp_path / "state.json"), library=library
    )
    assert any("errands" in complaint for complaint in engine.check())


def test_load_scheduled_refuses_a_tree_whose_grants_do_not_resolve(
    write_resident: ResidentWriter, write_skill
) -> None:
    write_skill("read-inbox")
    path = write_resident(manifest_with(HOURLY, skills=["read-inbx"]))
    with pytest.raises(s.SchedulerError, match="read-inbox"):
        s.load_scheduled(path.parent)


def test_a_scheduler_without_a_library_injects_and_writes_nothing(build, tmp_path: Path) -> None:
    """The behaviour before #12, unchanged: no library, no skills section, no files."""
    engine = build(HOURLY)
    assert "YOUR SKILLS" not in engine.build_prompt(engine.scheduled[0])
    engine.fire(engine.scheduled[0])
    assert not (tmp_path / ".claude").exists()


# ----------------------------------------------------------- state safety (steward #76, #85, #25)


def test_a_directory_shaped_state_is_fatal_and_cleans_the_stray_tmp(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A STEWARD_STATE that names a directory silently disabled persistence; now it is fatal."""
    path = write_resident(manifest_with(HOURLY))
    state = tmp_path / "state.json"
    state.mkdir()  # STEWARD_STATE points at a directory
    stray = tmp_path / "state.json.tmp"
    stray.write_text("half a write from a crash", encoding="utf-8")

    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=state),
        workdir=tmp_path,
    )
    with pytest.raises(s.SchedulerError, match="directory"):
        engine.tick(datetime(2026, 8, 24, 10, 15, tzinfo=UTC))
    assert not stray.exists(), "the stray temp file is cleaned up"


def test_two_ticks_over_one_state_file_fire_an_occurrence_exactly_once(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Two tick processes over one state file must not both fire the same occurrence."""
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    anchor = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)  # 12:00 local
    due_at = datetime(2026, 8, 24, 10, 15, 30, tzinfo=UTC)  # 12:15:30 local

    seed = s.SchedulerState.load(state_path)
    seed.set_anchor("test-agent/inbox-read", anchor)
    seed.save()

    def daemon() -> s.Scheduler:
        return s.Scheduler(
            s.load_scheduled(path.parent),
            emitter=ev.EventEmitter(fallback=tmp_path / "events.jsonl"),
            state=s.SchedulerState.load(state_path),
            workdir=tmp_path,
        )

    first, second = daemon(), daemon()
    reports = [*first.tick(due_at), *second.tick(due_at)]
    assert sum(1 for report in reports if report.fired) == 1


@pytest.mark.parametrize("first_to_wake", ["daemon", "cron"])
def test_a_daemon_and_a_cron_tick_fire_an_occurrence_exactly_once(
    write_resident: ResidentWriter, tmp_path: Path, first_to_wake: str
) -> None:
    """The daemon loop takes the same state lock a cron tick does (steward #25)."""
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    anchor = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)  # 12:00 local
    due_at = datetime(2026, 8, 24, 10, 15, 30, tzinfo=UTC)  # 12:15:30 local

    seed = s.SchedulerState.load(state_path)
    seed.set_anchor("test-agent/inbox-read", anchor)
    seed.save()

    def scheduler() -> s.Scheduler:
        return s.Scheduler(
            s.load_scheduled(path.parent),
            emitter=ev.EventEmitter(fallback=tmp_path / "events.jsonl"),
            state=s.SchedulerState.load(state_path),
            workdir=tmp_path,
        )

    daemon, cron = scheduler(), scheduler()

    def fire_daemon() -> list[s.FireReport]:
        return daemon.run(max_ticks=1, sleep=lambda _seconds: None, now_fn=lambda: due_at)

    def fire_cron() -> list[s.FireReport]:
        return cron.tick(due_at)

    order = [fire_daemon, fire_cron] if first_to_wake == "daemon" else [fire_cron, fire_daemon]
    reports = [report for fire in order for report in fire()]
    assert sum(1 for report in reports if report.fired) == 1


def test_a_second_daemon_refuses_to_start_and_names_the_first(build, tmp_path: Path) -> None:
    """Two daemons over one state file double every session, so the second exits red."""
    first, second = build(HOURLY), build(HOURLY)
    lock_path = tmp_path / "state.json.daemon.lock"
    with first._daemon_lock():
        with pytest.raises(s.SchedulerError, match="another scheduler daemon is already running"):
            second.run(max_ticks=1, sleep=lambda _seconds: None)
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()), (
            "the refusal can name the pid holding the lock"
        )
    # The lock is the daemon's lifetime, not forever: the next daemon starts fine.
    assert second.run(max_ticks=1, sleep=lambda _seconds: None) == []


def test_a_dry_run_daemon_takes_no_locks(write_resident: ResidentWriter, tmp_path: Path) -> None:
    """A rehearsal leaves no sidecar behind and never refuses beside a real daemon."""
    path = write_resident(manifest_with(HOURLY))
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        dry_run=True,
    )
    engine.run(max_ticks=1, sleep=lambda _seconds: None, now_fn=lambda: datetime.now(UTC))
    assert not list(tmp_path.glob("state.json*"))


def test_a_crash_after_saving_the_anchor_does_not_re_fire(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The anchor is saved before the fire, so a crash mid-run does not re-fire on restart."""
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    anchor = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    seed = s.SchedulerState.load(state_path)
    seed.set_anchor("test-agent/inbox-read", anchor)
    seed.save()

    class Crashing(r.Runner):
        """Mimics a session that dies partway: the anchor is already on disk by now."""

        def run(self, request: r.RunRequest) -> r.RunResult:  # noqa: ARG002
            raise RuntimeError("killed mid-run")

    crashed = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.NullEmitter(),
        state=s.SchedulerState.load(state_path),
        workdir=tmp_path,
        runner_factory=lambda _spec: Crashing(),
    )
    crashed.tick(datetime(2026, 8, 24, 10, 15, 30, tzinfo=UTC))

    restarted = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.NullEmitter(),
        state=s.SchedulerState.load(state_path),
        workdir=tmp_path,
    )
    assert restarted.tick(datetime(2026, 8, 24, 10, 16, tzinfo=UTC)) == []


def test_a_heartbeat_does_not_re_fire_what_another_tick_process_already_ran(
    write_resident: ResidentWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat must not be able to undo exactly-once (steward #76) from outside the lock.

    Tick A releases the state lock before its board sweep and then spends minutes in it,
    beating all the while off a snapshot taken before tick B existed. A stamp that wrote
    that whole snapshot would erase the anchor B saved under the lock, and the occurrence
    B just fired would look unfired to the next tick and run a second time.
    """
    monkeypatch.setattr(s, "HEARTBEAT_EVERY_S", 0.01)
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    seed = s.SchedulerState.load(state_path)
    seed.set_anchor("test-agent/inbox-read", datetime(2026, 8, 24, 10, 0, tzinfo=UTC))
    seed.save()

    sweeping, release = threading.Event(), threading.Event()

    class Sweeping:
        """Board hooks that hang in the sweep, as a board of long sessions would."""

        def decisions_for(self, resident_id: str) -> str | None:  # noqa: ARG002
            return None

        def harvest(self, manifest: m.ResidentManifest, output: str) -> object:  # noqa: ARG002
            return None

        def dispatch(self, now: datetime) -> object:  # noqa: ARG002
            sweeping.set()
            release.wait(timeout=5)
            return None

    def scheduler(hooks: s.WakeHooks | None = None) -> s.Scheduler:
        return s.Scheduler(
            s.load_scheduled(path.parent),
            emitter=ev.NullEmitter(),
            state=s.SchedulerState.load(state_path),
            workdir=tmp_path,
            hooks=hooks,
        )

    first = scheduler(Sweeping())
    # Nothing is due at :03, so A anchors nothing and its snapshot stays at 10:00.
    sweeper = threading.Thread(target=lambda: first.tick(datetime(2026, 8, 24, 10, 3, tzinfo=UTC)))
    sweeper.start()
    try:
        assert sweeping.wait(timeout=5), "the board sweep never started"
        second = scheduler()
        fired = second.tick(datetime(2026, 8, 24, 10, 15, 30, tzinfo=UTC))
        assert [report.fired for report in fired] == [True], "B fires the 10:15 occurrence"
        # ... and A keeps beating over the top of what B wrote.
        assert _beat_after(state_path, datetime.now(UTC)) is not None, "A stopped beating"
    finally:
        release.set()
        sweeper.join(timeout=5)

    third = scheduler()
    assert third.tick(datetime(2026, 8, 24, 10, 18, tzinfo=UTC)) == [], "10:15 already ran"


def test_a_beat_declines_a_wake_up_that_is_already_queued_behind_another_process(
    build, tmp_path: Path
) -> None:
    """A beat waits for nobody — not for the flock, and not for a thread waiting on it.

    The daemon takes the state lock once per wake-up (steward #25), and that wait is as
    long as whatever the other process is holding it for: a cron ``tick`` fires inside the
    lock, so a 15-minute routine is a 15-minute wait. The heartbeat runs on its own thread
    precisely so it keeps landing through stretches like that, and the process holding the
    lock is stamping the file anyway — so a beat it cannot make immediately is one it must
    drop, however the queue it would join is spelled.
    """
    engine = build(HOURLY)
    lock_path = (tmp_path / "state.json").with_suffix(".json.lock")

    # Stand in for the cron tick holding the state lock across a long fire: a second open
    # file description conflicts with the scheduler's own exactly as another process would.
    other = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(other, fcntl.LOCK_EX)
    # ... and lets go eventually, so a beat that *did* queue finishes late instead of hanging.
    threading.Timer(2.0, lambda: fcntl.flock(other, fcntl.LOCK_UN)).start()

    waiting = threading.Event()

    def wake_up() -> None:
        waiting.set()
        with engine._state_lock():
            pass

    loop = threading.Thread(target=wake_up, daemon=True)
    loop.start()
    try:
        assert waiting.wait(timeout=5), "the wake-up never started"
        time.sleep(0.2)  # long enough to be inside the blocking flock, holding the gate
        started = time.monotonic()
        with engine._state_lock(wait=False) as held:
            assert not held, "the lock another process holds is not this beat's to take"
        assert time.monotonic() - started < 1.0, "the beat queued behind the wake-up"
    finally:
        loop.join(timeout=5)
        os.close(other)


def test_two_processes_never_write_through_one_temp_state_file(tmp_path: Path) -> None:
    """A shared temp name is a shared half-written file: one process can rename the other's."""
    path = tmp_path / "state.json"
    assert s.temp_state_path(path, pid=101) != s.temp_state_path(path, pid=102)
    assert s.temp_state_path(path) == s.temp_state_path(path, pid=os.getpid())


def test_the_startup_sweep_leaves_another_schedulers_temp_alone(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Clearing a temp a live scheduler is mid-rename on turns its saved anchor into nothing."""
    path = write_resident(manifest_with(HOURLY))
    state_path = tmp_path / "state.json"
    legacy = state_path.with_suffix(".json.tmp")  # written before temps were named per process
    legacy.write_text("half a write from a crash", encoding="utf-8")
    theirs = s.temp_state_path(state_path, pid=os.getpid() + 1)
    theirs.write_text("another scheduler, mid-rename", encoding="utf-8")

    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=state_path),
        workdir=tmp_path,
    )
    engine.tick(datetime(2026, 8, 24, 10, 40, tzinfo=UTC))  # nothing due at :40

    assert not legacy.exists(), "a name no live writer uses is still cleaned up"
    assert theirs.exists(), "another process's temp is not this one's to delete"


# --------------------------------------------------------------- nowhere to run (steward #64)


def test_a_resident_with_an_absent_memory_dir_is_refused_not_run_in_cwd(
    write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    """A skills-loading session whose memory dir is missing must not materialize into cwd."""
    write_skill("write-journal", defaults=True, body="Write it.\n")
    write_skill("read-inbox", body="Read the mail.\n")
    data = manifest_with(HOURLY, skills=["read-inbox"])
    data["runner"] = {"kind": "claude", "model": "pretend"}
    # memory stays valid_manifest's /data/... path, which is not a directory on this host.
    path = write_resident(data)

    seen: list[r.RunRequest] = []

    def factory(spec: m.Runner) -> r.Runner:
        return r.MockRunner(
            spec,
            behavior=lambda req: (
                seen.append(req) or r.RunResult(outcome=r.Outcome.OK, output="done", exit_status=0)
            ),
        )

    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        # No workdir given, so the fallback is the process cwd: the dangerous case.
        runner_factory=factory,
        library=sk.load_library(tmp_path / "skills"),
    )
    report = engine.fire(engine.scheduled[0], now=datetime(2026, 8, 24, 10, 15, tzinfo=UTC))
    assert not report.fired
    assert report.skipped_reason is not None
    assert "current working directory" in report.skipped_reason
    assert seen == [], "the session never ran against the cwd"
    assert any("current working directory" in complaint for complaint in engine.check())


# --------------------------------------------------------------- guarded collaborators (#80)


def test_a_decisions_hook_that_raises_still_brackets_the_run(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A hook that throws while delivering decisions must not take the whole fire down."""
    path = write_resident(manifest_with(HOURLY))

    class Hooks:
        def decisions_for(self, resident_id: str) -> str | None:  # noqa: ARG002
            raise RuntimeError("the inbox is on fire")

        def harvest(self, manifest: m.ResidentManifest, output: str) -> object:  # noqa: ARG002
            return []

        def dispatch(self, now: datetime) -> object:  # noqa: ARG002
            return None

    sink = ev.NullEmitter()
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=sink,
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        hooks=Hooks(),
    )
    report = engine.fire(engine.scheduled[0], now=datetime(2026, 8, 24, 10, 15, tzinfo=UTC))
    assert report.fired
    assert [e.type for e in sink.events] == [ev.ROUTINE_STARTED, ev.ROUTINE_FINISHED]


def test_a_journal_that_raises_a_decode_error_is_a_missing_journal_not_a_crash(
    build, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One undecodable byte raises a ValueError, not an OSError; the fire must survive it."""
    engine = build(HOURLY)

    def undecodable(*_args: object, **_kwargs: object) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "one bad byte")

    monkeypatch.setattr(j, "latest_entry", undecodable)
    report = engine.fire(engine.scheduled[0], now=datetime(2026, 8, 24, 10, 15, tzinfo=UTC))
    assert report.fired, "a bad byte degrades to no journal rather than bricking the routine"


# ------------------------------------------------------------- the run registry (steward #39)


def _engine_with_registry(
    path: Path,
    store: Store,
    tmp_path: Path,
    runner_factory: s.RunnerFactory = r.build_runner,
) -> s.Scheduler:
    """Build a scheduler that writes its runs into steward's own store."""
    return s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        registry=store,
        runner_factory=runner_factory,
    )


def test_a_fire_opens_a_registry_row_and_closes_it(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Steward's own record of the bracket, so the watchdog needs no event to find it."""
    path = write_resident(manifest_with(HOURLY))
    with Store(":memory:") as store:
        engine = _engine_with_registry(path, store, tmp_path)

        report = engine.fire(engine.scheduled[0], now=datetime(2026, 8, 24, 10, 15, tzinfo=UTC))

        assert report.fired
        assert store.open_runs() == [], "the run reported back, so its row is answered"


def test_a_run_that_never_finishes_leaves_its_row_open(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The whole point: a session that dies mid-run is a row the watchdog can still read."""
    path = write_resident(manifest_with(HOURLY))

    def dies(_spec: m.Runner) -> r.Runner:
        raise KeyboardInterrupt("the machine went away")

    with Store(":memory:") as store:
        engine = _engine_with_registry(path, store, tmp_path, runner_factory=dies)
        with pytest.raises(KeyboardInterrupt):
            engine.fire(engine.scheduled[0], now=datetime(2026, 8, 24, 10, 15, tzinfo=UTC))

        (open_run,) = store.open_runs()
        assert (open_run.kind, open_run.ref) == ("routine", "inbox-read")
        assert open_run.timeout_s == pytest.approx(60.0)


def test_a_registry_that_refuses_to_write_is_not_a_failed_routine(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The village hears the bracket even when steward's own bookkeeping is broken."""
    path = write_resident(manifest_with(HOURLY))

    class Broken:
        def open_run(self, **_kwargs: object) -> bool:
            raise RuntimeError("the disk is full")

        def close_run(self, run_id: str, **_kwargs: object) -> bool:  # noqa: ARG002
            raise RuntimeError("the disk is still full")

    sink = ev.NullEmitter()
    engine = s.Scheduler(
        s.load_scheduled(path.parent),
        emitter=sink,
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        registry=Broken(),
    )

    report = engine.fire(engine.scheduled[0], now=datetime(2026, 8, 24, 10, 15, tzinfo=UTC))

    assert report.fired
    assert [e.type for e in sink.events] == [ev.ROUTINE_STARTED, ev.ROUTINE_FINISHED]
