"""The scheduler: when things fire, exactly once, and what the village is told."""

import json
import threading
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
