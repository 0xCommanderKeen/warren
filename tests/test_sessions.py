"""The resident session lifecycle: one seam for every real wake-up."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import ResidentWriter, valid_manifest
from steward import sessions as sessions_module
from steward.manifest import ResidentManifest, load_manifest
from steward.runners import Outcome, Runner, RunRequest, RunResult
from steward.sessions import (
    Admission,
    DelegatedWake,
    Refusal,
    ResidentSessions,
    RoutineWake,
    SessionHarvest,
    TaskWake,
)
from steward.skills import Materialization, Skill, SkillLibrary

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


class _CapturingRunner(Runner):
    """Return one prepared result and retain the public runner request."""

    def __init__(self, result: RunResult) -> None:
        super().__init__()
        self.result = result
        self.requests: list[RunRequest] = []

    def run(self, request: RunRequest) -> RunResult:
        self.requests.append(request)
        return self.result


class _RecordingGuard:
    """A budget adapter that records when completed spend lands."""

    def __init__(self, refusal: str | None = None) -> None:
        self.records: list[dict[str, object]] = []
        self.refusal = refusal
        self.allow_calls = 0
        self.timeout_calls = 0

    def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
        del manifest, now
        self.allow_calls += 1
        return self.refusal

    def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
        del manifest
        self.timeout_calls += 1
        return min(declared_s, 300)

    def record(self, manifest: ResidentManifest, **facts: object) -> object:
        del manifest
        self.records.append(facts)
        return None


class _RecordingHooks:
    """Deliver context and retain the output harvested by the lifecycle."""

    def __init__(self) -> None:
        self.harvested: list[dict[str, object]] = []
        self.decision_calls = 0

    def decisions_for(self, resident_id: str) -> str | None:
        del resident_id
        self.decision_calls += 1
        return "A human approved the next step."

    def harvest_session(
        self,
        manifest: ResidentManifest,
        output: str,
        *,
        parent_task_id: str | None = None,
        now: datetime | None = None,
    ) -> SessionHarvest:
        del manifest
        self.harvested.append({"output": output, "parent_task_id": parent_task_id, "now": now})
        return SessionHarvest(("approval-1",), ("handoff-1",))


def test_a_routine_runs_and_accounts_through_the_resident_session_seam(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "mock", "model": "pretend"}
    resident = load_manifest(write_resident(data))
    routine = resident.manifest.routines[0]
    runner = _CapturingRunner(
        RunResult(outcome=Outcome.OK, output="done", exit_status=0, duration_s=5)
    )
    guard = _RecordingGuard()
    sessions = ResidentSessions(
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        guard=guard,
        clock=lambda: NOW + timedelta(seconds=5),
    )

    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)
    result = sessions.run(
        admission,
        RoutineWake(routine=routine, run_id="run-1"),
    )

    assert result.result is not None
    assert result.result.ok
    assert result.completed_at == datetime(2026, 8, 24, 12, 0, 5, tzinfo=UTC)
    assert len(runner.requests) == 1
    request = runner.requests[0]
    assert request.prompt.rstrip().endswith("Write the summary.")
    assert request.workdir == tmp_path
    assert request.timeout_s == 300
    assert request.model == "pretend"
    assert request.env == {
        "BURROW_AGENT_ID": "claude-code:test-agent",
        "BURROW_PROJECT": "test-agent",
        "STEWARD_ROUTINE": "daily-summary",
        "STEWARD_RUN_ID": "run-1",
    }
    assert guard.records == [
        {
            "result": result.result,
            "kind": "routine",
            "run_id": "run-1",
            "trigger": "schedule",
            "ref": "daily-summary",
            "origin": "resident:test-agent",
            "now": result.completed_at,
        }
    ]
    assert guard.timeout_calls == 1


def test_completion_uses_one_actual_clock_read_not_runner_duration(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Duration measures the runner; completion is the wall-clock fact after it returns."""
    resident = load_manifest(write_resident(valid_manifest()))
    runner = _CapturingRunner(RunResult(outcome=Outcome.OK, duration_s=5))
    guard = _RecordingGuard()
    completed = NOW + timedelta(minutes=3)
    reads: list[None] = []

    def clock() -> datetime:
        reads.append(None)
        return completed

    sessions = ResidentSessions(
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        guard=guard,
        clock=clock,
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)

    result = sessions.run(admission, RoutineWake(resident.manifest.routines[0], "run-1"))

    assert result.completed_at == completed
    assert result.require_result().duration_s == 5
    assert guard.records[0]["now"] == completed
    assert len(reads) == 1


def test_a_claimed_task_uses_the_same_context_run_account_and_harvest_sequence(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "mock", "model": "pretend"}
    resident = load_manifest(write_resident(data))
    runner = _CapturingRunner(
        RunResult(outcome=Outcome.OK, output="task done", exit_status=0, duration_s=7)
    )
    guard = _RecordingGuard()
    hooks = _RecordingHooks()
    sessions = ResidentSessions(
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        guard=guard,
        hooks=hooks,
        clock=lambda: NOW + timedelta(seconds=7),
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)

    result = sessions.run(
        admission,
        TaskWake(
            task_id="task-1",
            title="Research X",
            detail="Use primary sources.",
            required_skills=("research",),
            timeout_s=900,
            origin="task:task-1",
            run_id="run-task-1",
        ),
    )

    assert result.result is not None
    assert result.result.ok
    assert "Research X" in result.prompt
    assert "A human approved the next step." in result.prompt
    assert runner.requests[0].env == {
        "BURROW_AGENT_ID": "claude-code:test-agent",
        "BURROW_PROJECT": "test-agent",
        "STEWARD_TASK_ID": "task-1",
    }
    assert guard.records[0]["kind"] == "task"
    assert guard.records[0]["now"] == datetime(2026, 8, 24, 12, 0, 7, tzinfo=UTC)
    assert result.raised == ("approval-1",)
    assert result.handed_over == ("handoff-1",)
    assert hooks.harvested == [{"output": "task done", "parent_task_id": "task-1", "now": NOW}]


def test_admission_refusals_happen_before_context_or_runner_creation(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "mock"}
    resident = load_manifest(write_resident(data))
    guard = _RecordingGuard("daily budget exhausted")
    hooks = _RecordingHooks()
    runner_builds = 0

    def build_runner(_spec) -> Runner:
        nonlocal runner_builds
        runner_builds += 1
        return _CapturingRunner(RunResult(outcome=Outcome.OK))

    sessions = ResidentSessions(
        workdir=tmp_path, runner_factory=build_runner, guard=guard, hooks=hooks
    )

    admission = sessions.admit(resident, now=NOW)

    assert admission == Refusal("daily budget exhausted")
    assert hooks.decision_calls == 0
    assert runner_builds == 0
    assert guard.records == []


def test_admission_refuses_an_unsafe_current_working_directory(
    write_resident: ResidentWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "claude"}
    resident = load_manifest(write_resident(data))
    monkeypatch.chdir(tmp_path)
    sessions = ResidentSessions(
        workdir=tmp_path,
        library=SkillLibrary(path=tmp_path / "configured-skills"),
    )

    admission = sessions.admit(resident, now=NOW)

    assert isinstance(admission, Refusal)
    assert "current working directory" in admission.reason


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_provisioning_refuses_a_declared_directory_replaced_after_admission(
    replacement: str,
    write_resident: ResidentWriter,
    tmp_path: Path,
) -> None:
    """A stable admitted path cannot be replaced before destructive provisioning (#133)."""
    memory = tmp_path / "memory"
    memory.mkdir()
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["runner"] = {"kind": "claude"}
    resident = load_manifest(write_resident(data))
    sessions = ResidentSessions(
        workdir=tmp_path / "fallback",
        library=SkillLibrary(path=tmp_path / "configured-skills"),
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)
    memory.rmdir()
    if replacement == "file":
        memory.write_text("not a directory", encoding="utf-8")
    else:
        target = tmp_path / "other"
        target.mkdir()
        memory.symlink_to(target, target_is_directory=True)

    refusal = sessions.revalidate(admission)

    assert isinstance(refusal, Refusal)
    assert "no longer the directory" in refusal.reason


def test_provisioning_refuses_a_declared_directory_recreated_at_the_same_path(
    write_resident: ResidentWriter,
    tmp_path: Path,
) -> None:
    """A pathname is not authority once its admitted directory has been replaced."""
    memory = tmp_path / "memory"
    memory.mkdir()
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["runner"] = {"kind": "claude"}
    resident = load_manifest(write_resident(data))
    sessions = ResidentSessions(
        workdir=tmp_path / "fallback",
        library=SkillLibrary(path=tmp_path / "configured-skills"),
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)
    memory.rmdir()
    memory.mkdir()

    refusal = sessions.revalidate(admission)

    assert isinstance(refusal, Refusal)
    assert "filesystem identity changed" in refusal.reason


def test_materialization_stays_bound_when_workdir_is_replaced_after_revalidation(
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the pathname cannot redirect descriptor-relative mutations (#133)."""
    memory = tmp_path / "memory"
    memory.mkdir()
    moved = tmp_path / "admitted-memory"
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["runner"] = {"kind": "claude"}
    resident = load_manifest(write_resident(data))
    skills = {
        name: Skill(name, f"{name} carefully.", "Do it.")
        for name in ("daily-summary", "write-journal")
    }
    sessions = ResidentSessions(
        workdir=tmp_path / "fallback",
        library=SkillLibrary(path=tmp_path / "skills", skills=skills),
        runner_factory=lambda _spec: _CapturingRunner(RunResult(outcome=Outcome.OK)),
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)
    real_materialize = sessions_module.materialize

    def replace_then_materialize(skills, workdir, subdir, *, workdir_fd=None) -> Materialization:
        memory.rename(moved)
        memory.mkdir()
        (memory / "replacement.txt").write_text("untouched", encoding="utf-8")
        return real_materialize(skills, workdir, subdir, workdir_fd=workdir_fd)

    monkeypatch.setattr(sessions_module, "materialize", replace_then_materialize)

    result = sessions.run(admission, RoutineWake(resident.manifest.routines[0], "run-1"))

    assert result.require_result().outcome is Outcome.OK
    assert (moved / ".claude/skills/daily-summary/SKILL.md").is_file()
    assert sorted(path.name for path in memory.iterdir()) == ["replacement.txt"]


def test_provisioning_refuses_when_the_admitted_directory_moves_filesystems(
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mount replacement changes st_dev even when path and inode appear unchanged."""
    memory = tmp_path / "memory"
    memory.mkdir()
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["runner"] = {"kind": "claude"}
    resident = load_manifest(write_resident(data))
    sessions = ResidentSessions(
        workdir=tmp_path / "fallback",
        library=SkillLibrary(path=tmp_path / "configured-skills"),
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)
    real_stat = Path.stat

    def changed_device(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = real_stat(path, follow_symlinks=follow_symlinks)
        if path == memory and not follow_symlinks:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", changed_device)

    refusal = sessions.revalidate(admission)

    assert isinstance(refusal, Refusal)
    assert "filesystem identity changed" in refusal.reason


def test_provisioning_refuses_a_replacement_during_identity_observation(
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve cannot be paired with stat data from two different directories."""
    memory = tmp_path / "memory"
    memory.mkdir()
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["runner"] = {"kind": "claude"}
    resident = load_manifest(write_resident(data))
    sessions = ResidentSessions(
        workdir=tmp_path / "fallback",
        library=SkillLibrary(path=tmp_path / "configured-skills"),
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)
    real_stat = Path.stat
    observations = 0

    def replaced_between_stats(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        nonlocal observations
        result = real_stat(path, follow_symlinks=follow_symlinks)
        if path == memory and not follow_symlinks:
            observations += 1
            if observations == 2:
                values = list(result)
                values[1] += 1
                return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", replaced_between_stats)

    refusal = sessions.revalidate(admission)

    assert isinstance(refusal, Refusal)
    assert "filesystem identity changed" in refusal.reason


def test_admission_fails_closed_when_declared_memory_vanishes_during_capture(
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capture race cannot downgrade a declared directory into process cwd."""
    memory = tmp_path / "memory"
    memory.mkdir()
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["runner"] = {"kind": "claude"}
    resident = load_manifest(write_resident(data))
    runner = _CapturingRunner(RunResult(outcome=Outcome.OK))
    monkeypatch.chdir(tmp_path)
    sessions = ResidentSessions(
        library=SkillLibrary(path=tmp_path / "configured-skills"),
        runner_factory=lambda _spec: runner,
    )
    real_open = os.open

    def vanish_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(path) == memory:
            memory.rmdir()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(sessions_module.os, "open", vanish_before_open)

    admission = sessions.admit(resident, now=NOW)

    assert isinstance(admission, Refusal)
    assert runner.requests == []
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert not (tmp_path / ".claude").exists()


def test_missing_skills_fail_before_decisions_are_consumed(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "mock"}
    resident = load_manifest(write_resident(data))
    hooks = _RecordingHooks()
    sessions = ResidentSessions(
        workdir=tmp_path,
        library=SkillLibrary(path=tmp_path / "configured-skills"),
        hooks=hooks,
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)

    result = sessions.run(
        admission,
        RoutineWake(resident.manifest.routines[0], "run-1"),
    ).require_result()

    assert not result.ok
    assert "daily-summary" in result.summary()
    assert hooks.decision_calls == 0


def test_journal_and_harvest_failures_do_not_escape_or_erase_the_run_result(
    write_resident: ResidentWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "mock"}
    resident = load_manifest(write_resident(data))
    runner = _CapturingRunner(RunResult(outcome=Outcome.TIMEOUT, output="partial", duration_s=60))
    order: list[str] = []

    class BrokenHooks(_RecordingHooks):
        def decisions_for(self, resident_id: str) -> str | None:
            order.append("decisions")
            return super().decisions_for(resident_id)

        def harvest_session(self, *_args: object, **_kwargs: object) -> SessionHarvest:
            order.append("harvest")
            raise OSError("database unavailable")

    def unreadable(*_args: object, **_kwargs: object) -> str:
        order.append("journal")
        raise UnicodeDecodeError("utf8", b"x", 0, 1, "bad")

    monkeypatch.setattr("steward.sessions.journal.latest_entry", unreadable)
    sessions = ResidentSessions(
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        hooks=BrokenHooks(),
        clock=lambda: NOW + timedelta(seconds=60),
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)

    result = sessions.run(
        admission,
        RoutineWake(resident.manifest.routines[0], "run-1"),
    )

    assert result.require_result().outcome is Outcome.TIMEOUT
    assert result.completed_at == NOW + timedelta(seconds=60)
    assert result.raised == ()
    assert result.handed_over == ()
    assert order == ["journal", "decisions", "harvest"]


def test_timeout_is_resolved_once_before_the_caller_opens_its_registry(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "mock"}
    resident = load_manifest(write_resident(data))
    runner = _CapturingRunner(RunResult(outcome=Outcome.OK))

    class ChangingGuard(_RecordingGuard):
        def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
            del manifest, declared_s
            self.timeout_calls += 1
            return 123 if self.timeout_calls == 1 else 456

    guard = ChangingGuard()
    sessions = ResidentSessions(workdir=tmp_path, runner_factory=lambda _spec: runner, guard=guard)
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)

    registry_timeout = admission.timeout_for(900)
    session = sessions.run(
        admission,
        RoutineWake(resident.manifest.routines[0], "run-1"),
    )

    assert registry_timeout == 123
    assert session.timeout_s == 123
    assert runner.requests[0].timeout_s == 123
    assert guard.timeout_calls == 1


def test_accounting_and_harvest_failures_do_not_erase_the_run_result(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "mock"}
    resident = load_manifest(write_resident(data))
    runner = _CapturingRunner(RunResult(outcome=Outcome.OK, output="done", duration_s=2))
    order: list[str] = []

    class BrokenGuard(_RecordingGuard):
        def record(self, manifest: ResidentManifest, **facts: object) -> object:
            del manifest, facts
            order.append("account")
            raise OSError("ledger unavailable")

    class OrderedHooks(_RecordingHooks):
        def harvest_session(
            self,
            manifest: ResidentManifest,
            output: str,
            *,
            parent_task_id: str | None = None,
            now: datetime | None = None,
        ) -> SessionHarvest:
            order.append("harvest")
            return super().harvest_session(manifest, output, parent_task_id=parent_task_id, now=now)

    sessions = ResidentSessions(
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        guard=BrokenGuard(),
        hooks=OrderedHooks(),
    )
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)

    result = sessions.run(
        admission,
        RoutineWake(resident.manifest.routines[0], "run-1"),
    )

    assert result.require_result().ok
    assert order == ["account", "harvest"]


def test_board_runner_exceptions_preserve_their_zero_duration_result(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "mock"}
    resident = load_manifest(write_resident(data))

    def broken_factory(_spec) -> Runner:
        raise RuntimeError("runner construction failed")

    sessions = ResidentSessions(workdir=tmp_path, runner_factory=broken_factory, clock=lambda: NOW)
    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)

    result = sessions.run(
        admission,
        TaskWake(
            task_id="task-1",
            title="A task",
            detail="Do it.",
            required_skills=(),
            timeout_s=900,
            origin="task:task-1",
            run_id="run-task-1",
        ),
    )

    run_result = result.require_result()
    assert run_result.outcome is Outcome.FAILED
    assert "runner construction failed" in (run_result.error or "")
    assert run_result.duration_s == 0
    assert result.completed_at == NOW


def test_a_delegated_letter_and_a_rehearsal_use_the_same_seam_without_dry_run_writes(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "claude"}
    resident = load_manifest(write_resident(data))
    guard = _RecordingGuard()
    hooks = _RecordingHooks()
    runner = _CapturingRunner(RunResult(outcome=Outcome.OK, output="done"))
    library = SkillLibrary(
        path=tmp_path / "skills",
        skills={
            "daily-summary": Skill("daily-summary", "Summarise the day."),
            "write-journal": Skill("write-journal", "Write a journal entry."),
        },
    )
    sessions = ResidentSessions(
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        library=library,
        guard=guard,
        hooks=hooks,
        residents=[resident],
    )
    wake = DelegatedWake(
        task_id="letter-1",
        title="Read the background",
        detail="The long version.",
        timeout_s=600,
        origin="task:root",
        run_id="run-letter-1",
        delegated_by="gone-agent",
        route="inbox",
        parent_task_id="root",
    )
    rehearsal = sessions.admit(resident, now=NOW, rehearsal=True)
    assert isinstance(rehearsal, Admission)

    preview = sessions.run(rehearsal, wake)

    assert "gone-agent" in preview.prompt
    assert preview.result is None
    assert runner.requests == []
    assert hooks.decision_calls == 0
    assert guard.allow_calls == 0
    assert guard.records == []
    assert not (tmp_path / ".claude").exists()

    admission = sessions.admit(resident, now=NOW)
    assert isinstance(admission, Admission)
    result = sessions.run(admission, wake)

    assert result.require_result().ok
    assert runner.requests[0].env["STEWARD_TASK_ID"] == "letter-1"
    assert guard.records[0]["kind"] == "delegated"
    assert hooks.harvested[-1]["parent_task_id"] == "letter-1"
    assert (tmp_path / ".claude" / "skills" / "daily-summary" / "SKILL.md").is_file()
