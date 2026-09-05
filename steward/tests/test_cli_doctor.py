"""CLI behavior: doctor."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from conftest import (
    REPO_ROOT,
    ResidentWriter,
    ScratchRepo,
    SkillWriter,
    StubWriter,
    valid_manifest,
)
from steward import events as ev
from steward.budgets import BudgetGuard
from steward.claims import CLAIM_GRACE_S
from steward.cli import main
from steward.manifest import load_manifest
from steward.scheduler import STALE_TICK_AFTER_S, SchedulerState
from steward.store import Store
from support.cli import (
    CURRENT_CLAUDE,
    board_manifest,
    budgeted_manifest,
    docker_naming_itself,
    ledger_a_run,
    new_resident_argv,
    supervised_manifest,
)
from support.cli import (
    charter_file as charter_file,  # noqa: PLC0414 — pytest fixture discovery
)
from support.cli import (
    nas as nas,  # noqa: PLC0414 — pytest fixture discovery
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

#: The shipped tree's two containers, provisioned and answering. `docker exec` has two
#: callers in `doctor` now — the `claude --help` flag probe and the `test -f <emitter>`
#: one steward #264 added — so the stub dispatches on the command, not only the container.
OPERATOR_BURROW_DOCKER = (
    'case "$1" in '
    "info) printf 'dxp2800\\t27.3.1\\n' ;; "
    "inspect) case \"$4\" in steward-hob|steward-pip) printf 'true\\n' ;; "
    "*) exit 1 ;; esac ;; "
    'exec) case "$2" in steward-hob|steward-pip) ;; *) exit 1 ;; esac; '
    f'case "$3" in test) exit 0 ;; *) {CURRENT_CLAUDE} ;; esac ;; '
    "*) exit 1 ;; esac"
)


@pytest.fixture
def on_operator_burrow(
    monkeypatch: pytest.MonkeyPatch, stub_bin: StubWriter, tmp_path: Path
) -> Path:
    """Run shipped-tree diagnostics where the two proposed containers are provisioned."""
    stub_bin("docker", OPERATOR_BURROW_DOCKER)
    monkeypatch.setenv("STEWARD_BURROW", "dxp2800")
    monkeypatch.setenv("HOME", str(tmp_path))
    hob_memory = tmp_path / "docker" / "warren" / "residents" / "hob" / "memory"
    hob_memory.mkdir(parents=True)
    (tmp_path / "docker" / "warren" / "residents" / "pip" / "memory").mkdir(parents=True)
    return hob_memory


# ------------------------------------------------------------------------------- doctor


def test_doctor_with_no_path_fails_when_it_found_nothing(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A health check that found no residents must not silently report healthy (#176)."""
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))

    result = runner.invoke(main, ["doctor", "--db", str(tmp_path / "steward.db")])

    assert result.exit_code == 1, result.output
    assert "failed:" in result.output
    assert "this run validated nothing" in result.output
    assert str((tmp_path / "residents").resolve()) in result.output


def test_doctor_on_a_named_empty_tree_warns_but_does_not_fail(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicitly named empty tree is valid, but its warning must remain visible."""
    empty = tmp_path / "drafts"
    empty.mkdir()
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))

    result = runner.invoke(main, ["doctor", str(empty), "--db", str(tmp_path / "steward.db")])

    assert result.exit_code == 0, result.output
    assert "ok: 0 valid resident(s), 0 error(s), 1 warning(s)" in result.output
    assert "no resident manifests found" in result.output


@pytest.mark.usefixtures("on_operator_burrow")
def test_doctor_names_the_brain_and_the_next_fire(
    runner: CliRunner, stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "hob: runner claude (claude-opus-5) in container steward-hob — ready" in result.output
    assert "pip: runner claude (claude-haiku-4-5-20251001) in container steward-pip — ready" in (
        result.output
    )
    assert "hob/morning-digest: '0 8 * * *' Europe/Ljubljana" in result.output


@pytest.mark.usefixtures("empty_path")
def test_doctor_fails_loudly_when_the_container_runtime_is_missing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "docker could not answer for container 'steward-hob'" in result.output
    assert "docker could not answer for container 'steward-pip'" in result.output


#: A `claude --help` that knows how to bound a session, and one too old to.
NEW_CLAUDE_HELP = (
    'echo "  --setting-sources <sources>"; echo "  --tools <tools...>"; '
    'echo "  --strict-mcp-config"'
)


OLD_CLAUDE_HELP = 'echo "  --allowed-tools <tools...>"'


def test_doctor_says_what_every_resident_may_reach(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    """Printed for the unbounded ones too: that is the question this dimension answers.

    A report that only mentioned the bounded residents would answer "which of these can
    reach anything" by omission, which is the silence the declaration exists to end.
    """
    stub_bin("claude", NEW_CLAUDE_HELP)
    data = valid_manifest()
    data["tools"] = ["Read", "Glob"]
    bounded = runner.invoke(main, ["doctor", str(write_resident(data).parent)])

    assert bounded.exit_code == 0
    assert "test-agent: tools Read, Glob" in bounded.output

    unbounded = runner.invoke(main, ["doctor", str(write_resident(valid_manifest()).parent)])
    assert "test-agent: tools unrestricted" in unbounded.output


def test_doctor_fails_when_the_installed_brain_cannot_hold_a_declared_bound(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    """The one failure validation cannot reach: the CLI is not in the manifest.

    A `claude` too old to know `--tools` accepts the declaration, launches, and hands the
    session everything — the boundary is still written down and no longer true. Nothing at
    run time notices, so doctor is where the manifest and the installed binary meet.
    """
    stub_bin("claude", OLD_CLAUDE_HELP)
    data = valid_manifest()
    data["tools"] = ["Read"]
    result = runner.invoke(main, ["doctor", str(write_resident(data).parent)])

    assert result.exit_code == 1
    assert "--tools" in result.output


def test_doctor_says_where_a_resident_may_work_beyond_its_own_directory(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    """A widening grant is worth saying out loud even when nothing is wrong."""
    stub_bin("claude", NEW_CLAUDE_HELP + '; echo "  --add-dir <directories...>"')
    data = valid_manifest()
    data["workspace"] = ["/data/library/books"]
    result = runner.invoke(main, ["doctor", str(write_resident(data).parent)])

    assert result.exit_code == 0
    assert "test-agent: workspace /data/library/books" in result.output


def test_doctor_prints_extra_mounts_beside_the_workspace(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    stub_bin("claude", NEW_CLAUDE_HELP + '; echo "  --add-dir <directories...>"')
    data = valid_manifest()
    data["deploy"] = {
        "mounts": [{"host": "~/docker/life/vault", "container": "/vault", "mode": "rw"}],
    }
    data["workspace"] = ["/vault"]

    result = runner.invoke(main, ["doctor", str(write_resident(data).parent)])

    assert result.exit_code == 0, result.output
    assert "test-agent: workspace /vault" in result.output
    assert "test-agent: mount ~/docker/life/vault -> /vault (rw)" in result.output


def test_doctor_fails_when_the_brain_cannot_widen_a_session(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    """`--add-dir` is as much a flag the installed CLI has to have as `--tools` is."""
    stub_bin("claude", NEW_CLAUDE_HELP)  # no --add-dir
    data = valid_manifest()
    data["workspace"] = ["/data/library/books"]
    result = runner.invoke(main, ["doctor", str(write_resident(data).parent)])

    assert result.exit_code == 1
    assert "--add-dir" in result.output


def test_doctor_asks_the_brain_about_a_resident_that_declared_nothing_at_all(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    """Declaring nothing stopped meaning *asking nothing* (steward #206).

    Every claude session is launched with `--setting-sources`, whatever the manifest
    says, so an old CLI is now a problem for every claude resident: it exits 1 on the
    unknown option rather than running with the host's settings, and that is a failed
    session at the resident's next fire unless doctor says it here.
    """
    stub_bin("claude", OLD_CLAUDE_HELP)
    result = runner.invoke(main, ["doctor", str(write_resident(valid_manifest()).parent)])

    assert result.exit_code == 1
    assert "--setting-sources" in result.output


def test_doctor_reports_an_invalid_tree(runner: CliRunner, write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["charter"]
    path = write_resident(data)
    result = runner.invoke(main, ["doctor", str(path.parent)])
    assert result.exit_code == 1
    assert "charter" in result.output


def test_doctor_says_so_when_nothing_is_scheduled(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    data = valid_manifest()
    data["routines"] = []
    path = write_resident(data)
    result = runner.invoke(main, ["doctor", str(path.parent)])
    assert result.exit_code == 0
    assert "no enabled routines" in result.output


@pytest.mark.usefixtures("on_operator_burrow")
def test_doctor_says_whether_the_village_will_see_the_session(
    runner: CliRunner, stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-session telemetry is a choice, and a choice nobody can see is a bug (#264).

    Both shipped residents are container-placed, so their sessions carry steward's own
    hooks and the report says which emitter runs them. Neither is an error: a fleet may run
    quiet, but not silently quiet.
    """
    stub_bin("claude", CURRENT_CLAUDE)
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    monkeypatch.chdir(REPO_ROOT)

    result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "hob: per-session events via /opt/steward/chronicle-emit.py" in result.output
    assert "pip: per-session events via /opt/steward/chronicle-emit.py" in result.output


def test_doctor_says_when_a_local_placement_has_no_emitter_to_run(
    runner: CliRunner,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_resident: ResidentWriter,
) -> None:
    """The quiet default, said out loud, with the name of the thing that would end it."""
    stub_bin("claude", CURRENT_CLAUDE)
    monkeypatch.delenv("STEWARD_SESSION_EMITTER", raising=False)
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    manifest = write_resident({**valid_manifest(), "id": "quiet"}, directory="quiet")

    result = runner.invoke(
        main, ["doctor", str(manifest.parent.parent), "--db", str(tmp_path / "steward.db")]
    )

    assert "quiet: per-session events off — no emitter for local placement" in result.output
    # Named with the process it has to be set in: doctor reads its own environment, and the
    # process that will actually launch the session is the scheduler.
    assert "export STEWARD_SESSION_EMITTER in the environment the scheduler runs in" in (
        result.output
    )


def test_doctor_says_when_a_local_sessions_events_cannot_be_delivered_from_here(
    runner: CliRunner,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_resident: ResidentWriter,
) -> None:
    """Hooks that fire are half the answer; whether they deliver is the other (warren#449).

    A locally placed session inherits no `CHRONICLE_TOKEN`, so against a token-guarded
    village every per-session event 401s and journals to an outbox nothing drains. Without
    this line the symptom is an empty village three days later. Yellow and uncounted — the
    operator may have chosen it — but never silent, and carrying the outbox's own reading
    so it says *how long* rather than only *whether*.
    """
    stub_bin("claude", CURRENT_CLAUDE)
    emitter = tmp_path / "chronicle-emit.py"
    emitter.write_text("", encoding="utf-8")
    stub_bin("python3", 'echo "chronicle emitter outbox: stalled; 41/500 queued"')
    monkeypatch.setenv("STEWARD_SESSION_EMITTER", str(emitter))
    monkeypatch.setenv("CHRONICLE_URL", "http://dxp2800:8737")
    monkeypatch.setenv("CHRONICLE_TOKEN", "shared-ingest-secret")
    monkeypatch.delenv("STEWARD_SESSION_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    manifest = write_resident({**valid_manifest(), "id": "local"}, directory="local")

    result = runner.invoke(
        main, ["doctor", str(manifest.parent.parent), "--db", str(tmp_path / "steward.db")]
    )

    assert result.exit_code == 0, result.output
    assert f"local: per-session events via {emitter}" in result.output
    assert "rejected 401" in result.output
    assert "~/.chronicle/events.jsonl" in result.output
    assert "chronicle emitter outbox: stalled; 41/500 queued (read here)" in result.output
    # The secret itself is never a thing a report prints.
    assert "shared-ingest-secret" not in result.output


def test_doctor_warns_without_an_outbox_reading_when_the_emitter_cannot_answer(
    runner: CliRunner,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_resident: ResidentWriter,
) -> None:
    """The mixed case an operator on an old emitter actually hits (warren#449).

    `--status` is a flag the vendored emitter grew late, and a copy that predates it exits
    0 having printed nothing. The evidence is optional; the warning is not, and the line
    has to say the reading is missing rather than trail off looking like a reading of zero.
    """
    stub_bin("claude", CURRENT_CLAUDE)
    emitter = tmp_path / "chronicle-emit.py"
    emitter.write_text("", encoding="utf-8")
    stub_bin("python3", "exit 0")
    monkeypatch.setenv("STEWARD_SESSION_EMITTER", str(emitter))
    monkeypatch.setenv("CHRONICLE_URL", "http://dxp2800:8737")
    monkeypatch.setenv("CHRONICLE_TOKEN", "shared-ingest-secret")
    monkeypatch.delenv("STEWARD_SESSION_ENV_PASSTHROUGH", raising=False)
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    manifest = write_resident({**valid_manifest(), "id": "local"}, directory="local")

    result = runner.invoke(
        main, ["doctor", str(manifest.parent.parent), "--db", str(tmp_path / "steward.db")]
    )

    assert result.exit_code == 0, result.output
    assert "rejected 401" in result.output
    assert "no outbox reading here" in result.output


def test_doctor_stays_quiet_about_delivery_when_the_village_wants_no_token(
    runner: CliRunner,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_resident: ResidentWriter,
) -> None:
    """The warning has to be able to *not* fire, or it is decoration rather than a report.

    Where this host holds no ingest token the village wants none and the hook events land,
    so there is nothing to warn about and the emitter is never even asked for its outbox.
    """
    stub_bin("claude", CURRENT_CLAUDE)
    emitter = tmp_path / "chronicle-emit.py"
    emitter.write_text("", encoding="utf-8")
    stub_bin("python3", 'echo "chronicle emitter outbox: stalled; 41/500 queued"')
    monkeypatch.setenv("STEWARD_SESSION_EMITTER", str(emitter))
    monkeypatch.setenv("CHRONICLE_URL", "http://localhost:8737")
    monkeypatch.delenv("CHRONICLE_TOKEN", raising=False)
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    manifest = write_resident({**valid_manifest(), "id": "local"}, directory="local")

    result = runner.invoke(
        main, ["doctor", str(manifest.parent.parent), "--db", str(tmp_path / "steward.db")]
    )

    assert result.exit_code == 0, result.output
    assert f"local: per-session events via {emitter}" in result.output
    assert "rejected 401" not in result.output
    assert "chronicle emitter outbox" not in result.output


@pytest.mark.usefixtures("on_operator_burrow")
def test_doctor_is_red_when_the_container_has_no_emitter_to_run(
    runner: CliRunner, stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failure with no other symptom, made loud (#264).

    A resident on an image built before the emitter was baked under its current name runs
    perfectly: the hook command ends in `|| true`, so a missing script costs telemetry
    rather than denying every tool call. Doctor is the only place that can notice, so it
    looks rather than asserting — and counts it, unlike the merely-quiet case.
    """
    stub_bin("claude", CURRENT_CLAUDE)
    stub_bin(
        "docker",
        'case "$1" in '
        "info) printf 'dxp2800\\t27.3.1\\n' ;; "
        "inspect) printf 'true\\n' ;; "
        # The image is there and its claude answers; only the emitter is missing.
        f'exec) case "$3" in test) exit 1 ;; *) {CURRENT_CLAUDE} ;; esac ;; '
        "*) exit 1 ;; esac",
    )
    monkeypatch.setenv("STEWARD_BURROW", "dxp2800")
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    monkeypatch.chdir(REPO_ROOT)

    result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "/opt/steward/chronicle-emit.py is not in steward-hob" in result.output
    assert "re-ship the image and re-provision" in result.output


def test_doctor_claims_no_hooks_for_a_brain_that_is_never_given_any(
    runner: CliRunner,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_resident: ResidentWriter,
) -> None:
    """`--settings` is a claude flag; a codex resident must not be reported as emitting.

    `required_flags` answers `()` for every other kind, so a line about this resident's
    hooks would be a confident claim about a channel that does not exist for it — and the
    `off` line would be worse still, since exporting the variable it names would change
    nothing.
    """
    stub_bin("codex", "exit 0")
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    manifest = write_resident(
        {**valid_manifest(), "id": "coder", "runner": {"kind": "codex"}}, directory="coder"
    )

    result = runner.invoke(
        main, ["doctor", str(manifest.parent.parent), "--db", str(tmp_path / "steward.db")]
    )

    assert "per-session events" not in result.output


def test_doctor_says_where_the_journal_lives_and_who_closes_the_day(
    runner: CliRunner,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    on_operator_burrow: Path,
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert f"hob: journal {on_operator_burrow}/journal" in result.output
    assert "closed by close-of-day" in result.output
    assert "burrow-builder" not in result.output


def test_doctor_warns_when_the_journal_location_is_not_writable(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    """Doctor probes writability and says so — a warning, not a failure (#89)."""
    stub_bin("claude", CURRENT_CLAUDE)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the journal's parent should be", encoding="utf-8")
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(blocker / "memory"), "journal": "journal"}
    path = write_resident(data)

    result = runner.invoke(main, ["doctor", str(path.parent)])
    assert result.exit_code == 0, result.output  # a container path unwritable here is a warning
    assert "not writable" in result.output


def test_doctor_complains_about_a_memory_that_cannot_hold_a_journal(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    data = valid_manifest()
    data["memory"] = {"kind": "file", "path": "/data/test-agent/memory.md"}
    path = write_resident(data)
    result = runner.invoke(main, ["doctor", str(path.parent)])
    assert result.exit_code == 1
    assert "journal — memory.kind is 'file'" in result.output


def test_doctor_preflights_a_board_only_claimant(
    runner: CliRunner,
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    stub_bin: StubWriter,
    tmp_path: Path,
) -> None:
    """A claimant with no routine is invisible to the scheduler's check — doctor is it (#37).

    The refusal itself is steward #64's: a resident whose memory directory is not one on
    this host would run — and materialize skills into, and delete files from — whatever
    directory steward happened to be launched in. The scheduler refuses that for every
    resident it schedules; nothing asked it of a resident that only claims. A warning, not
    a failure: the missing path may be a container path this host was never meant to have.
    """
    stub_bin("claude", CURRENT_CLAUDE)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the memory directory should be", encoding="utf-8")
    data = board_manifest()
    data["runner"] = {"kind": "claude"}
    data["memory"] = {"kind": "directory", "path": str(blocker / "memory")}
    data["skills"] = []
    data["routines"] = []
    residents_dir = write_resident(data).parent.parent
    write_skill("research", defaults=True)

    result = runner.invoke(main, ["doctor", str(residents_dir)])

    assert result.exit_code == 0, result.output
    assert "board — " in result.output
    assert "current working directory" in result.output


def test_doctor_says_a_board_claimant_is_ready_to_claim(
    runner: CliRunner, write_resident: ResidentWriter, write_skill: SkillWriter, tmp_path: Path
) -> None:
    """The green line: a claimant with a working directory of its own is ready to claim."""
    write_skill("research", defaults=True)
    data = board_manifest()
    data["memory"] = {"kind": "directory", "path": str(tmp_path / "memory")}
    data["skills"] = []
    data["routines"] = []
    residents_dir = write_resident(data).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir)])

    assert result.exit_code == 0, result.output
    assert "test-agent: board — claimant" in result.output


def test_doctor_names_a_board_claimants_missing_skill(
    runner: CliRunner, write_resident: ResidentWriter, write_skill: SkillWriter, tmp_path: Path
) -> None:
    """Issue #37's headline scenario: a claimant granted a skill the library does not hold.

    Doctor exits non-zero and names the grant — and it is *validation* that says so, not the
    board pre-flight below it. ``validate_paths`` resolves the same library the pre-flight
    would and turns a grant that names nothing into an error for every resident, claimant or
    not, so doctor stops before it prints any resident's line at all. The third assertion is
    the point of this test: the pre-flight was written believing doctor validated without a
    library and that its own missing-skill leg was the only thing that could catch this. It
    is the reverse. Pinning the real order here keeps that belief from coming back as a
    rewrite of the check that actually works.
    """
    write_skill("research", defaults=True)
    data = board_manifest()
    data["memory"] = {"kind": "directory", "path": str(tmp_path / "memory")}
    data["skills"] = ["surgery"]
    data["routines"] = []
    residents_dir = write_resident(data).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir)])

    assert result.exit_code == 1, result.output
    assert "surgery" in result.output
    assert "board — " not in result.output, "validation had already stopped doctor"


def test_doctor_will_not_call_a_claimant_ready_because_it_looked_at_no_library(
    runner: CliRunner,
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    stub_bin: StubWriter,
    tmp_path: Path,
) -> None:
    """The pre-flight must read the library beside the *tree*, however the target is named.

    ``steward doctor residents/<id>`` is a shape validation accepts, and the library is at
    ``residents/../skills`` in every shape. Resolving it from the target instead found
    ``residents/<id>/skills``, got an unconfigured library back — and an unconfigured
    library makes :func:`steward.sessions.workdir_refusal` return ``None``, because a run
    that materializes no skills deletes nothing. The claimant was then called ready on the
    grounds that nothing had been checked.
    """
    stub_bin("claude", CURRENT_CLAUDE)
    write_skill("research", defaults=True)
    data = board_manifest()
    data["runner"] = {"kind": "claude"}
    data["memory"] = {"kind": "directory", "path": str(tmp_path / "absent" / "memory")}
    data["skills"] = []
    data["routines"] = []
    resident_dir = write_resident(data).parent

    result = runner.invoke(main, ["doctor", str(resident_dir)])

    assert result.exit_code == 0, result.output
    assert "current working directory" in result.output


@pytest.mark.usefixtures("empty_path")
def test_doctor_tells_a_claimant_with_no_binary_it_must_not_keep_claiming(
    runner: CliRunner, write_resident: ResidentWriter, write_skill: SkillWriter, tmp_path: Path
) -> None:
    """The pre-flight's red line, said in the claimant's own terms and counted as a problem.

    The runner line above it already fails doctor over the same missing binary — this is the
    second half of the sentence, not a second verdict: "this resident cannot run, *so it
    must not be left claiming*". The board line has to be red for the claimant whose only
    wake-up is a claim, or the fleet reads as one broken routine rather than a resident
    that will take a task off the board and drop it.
    """
    write_skill("research", defaults=True)
    (tmp_path / "memory").mkdir()
    data = board_manifest()
    data["runner"] = {"kind": "claude"}
    data["memory"] = {"kind": "directory", "path": str(tmp_path / "memory")}
    data["skills"] = []
    data["routines"] = []
    residents_dir = write_resident(data).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir)])

    assert result.exit_code == 1, result.output
    assert "test-agent: board — " in result.output
    assert result.output.count("not on PATH") == 2, "once as a runner, once as a claimant"


def test_doctor_reports_the_budget_and_the_watchdog(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    data = budgeted_manifest(daily_cost_usd=5.0)
    data["runner"] = {"kind": "claude"}
    residents_dir = write_resident(data).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 2.0)

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "test-agent: budget daily_cost_usd: 2 of 5" in result.output
    assert "watchdog: has never made a pass" in result.output


def test_doctor_says_nothing_here_needs_docker_when_nothing_declares_a_container(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    """The ordinary case must stay one quiet line, and must not shell out to docker."""
    stub_bin("claude", CURRENT_CLAUDE)
    stub_bin("docker", "echo 'doctor must not have asked me'; exit 1")
    residents_dir = write_resident(valid_manifest()).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0, result.output
    assert "nothing here needs docker" in result.output
    assert "doctor must not have asked me" not in result.output


def test_doctor_names_the_burrow_that_supervises_a_container(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    stub_bin("docker", docker_naming_itself("dxp2800"))
    monkeypatch.setenv("STEWARD_BURROW", "dxp2800")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0, result.output
    assert "burrow dxp2800 fires: test-agent" in result.output
    assert "docker at dxp2800's own docker answers as dxp2800 27.3.1" in result.output
    assert "container steward-test-agent on dxp2800 — supervised from here" in result.output


def test_doctor_warns_but_does_not_fail_over_a_container_on_another_burrow(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Doctor is routinely run from a laptop while the daemons live on the NAS (#59).

    So the gap is said out loud and is not an exit code — the same judgement
    `_report_scheduler` makes about a state file this host cannot see. The watchdog,
    which *is* the supervisor, says it in red instead.
    """
    stub_bin("claude", CURRENT_CLAUDE)
    stub_bin("docker", docker_naming_itself("laptop"))
    monkeypatch.setenv("STEWARD_BURROW", "laptop")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0, result.output
    assert "container steward-test-agent runs on dxp2800" in result.output
    assert "the watchdog cannot see it" in result.output


def test_doctor_says_when_docker_itself_did_not_answer(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    stub_bin("docker", "exit 1")
    monkeypatch.setenv("STEWARD_BURROW", "dxp2800")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0, result.output
    assert "docker did not answer" in result.output
    assert "nothing is supervising test-agent" in result.output


def test_doctor_fails_loudly_when_completed_spend_was_dropped(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    data = budgeted_manifest()
    data["runner"] = {"kind": "claude"}
    residents_dir = write_resident(data).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.health.record(
            kind="ledger_write",
            resident="test-agent",
            run_id="lost-run",
            error="database is locked",
        )

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code != 0
    assert "budget health: 1 durable failure(s)" in result.output
    assert "ledger_write for test-agent run lost-run" in result.output


def test_doctor_says_a_paused_resident_will_not_fire_tonight(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    data = budgeted_manifest(daily_cost_usd=1.0)
    data["runner"] = {"kind": "claude"}
    residents_dir = write_resident(data).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 9.0)
    with Store(db) as store:
        BudgetGuard(store).allow(
            load_manifest(residents_dir / "test-agent" / "manifest.yaml").manifest
        )

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 1
    assert "budget — paused: budget exceeded" in result.output


def test_doctor_names_the_last_watchdog_pass(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("claude", CURRENT_CLAUDE)
    data = budgeted_manifest()
    data["runner"] = {"kind": "claude"}
    residents_dir = write_resident(data).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.record_watchdog_pass(interventions=2, now="2026-08-24T12:00:00.000Z")

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "watchdog: last pass 2026-08-24T12:00:00.000Z" in result.output
    assert "2 intervention(s)" in result.output


def test_doctor_says_whether_a_scheduler_is_up(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list of next fires below is a promise; this is the line that says who keeps it."""
    stub_bin("claude", CURRENT_CLAUDE)
    data = budgeted_manifest()
    data["runner"] = {"kind": "claude"}
    residents_dir = write_resident(data).parent.parent
    state_path = tmp_path / "scheduler.json"
    monkeypatch.setenv("STEWARD_STATE", str(state_path))
    db = tmp_path / "steward.db"

    never = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])
    assert never.exit_code == 0, never.output
    assert "scheduler: has never ticked" in never.output

    state = SchedulerState(path=state_path)
    state.record_tick(datetime.now(UTC))
    state.save()
    up = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])
    assert "scheduler: last tick" in up.output
    assert "— up" in up.output

    state.record_tick(datetime.now(UTC) - timedelta(seconds=STALE_TICK_AFTER_S + 1))
    state.save()
    stopped = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])
    # A stopped daemon is still not doctor's exit code: doctor runs on laptops too.
    assert stopped.exit_code == 0, stopped.output
    assert "so nothing is firing the routines below" in stopped.output


def receiving_manifest(*, status: str = "active") -> dict[str, Any]:
    """Build a resident that declares one delegation route, open or shut."""
    data = budgeted_manifest()
    data["routes"] = [
        *data["routes"],
        {"id": "handoff", "kind": "delegation", "address": "steward:delegation", "status": status},
    ]
    return data


def deliver_a_letter(db: Path, *, assignee: str = "test-agent") -> None:
    """Put one open item in a resident's inbox, as a handoff would."""
    with Store(db) as store:
        store.delegate_job(
            title="Read the background",
            assignee=assignee,
            delegated_by="sender-agent",
            route="handoff",
        )


def test_doctor_counts_the_inbox_behind_an_open_route(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(receiving_manifest()).parent.parent
    db = tmp_path / "steward.db"
    deliver_a_letter(db)

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "test-agent: inbox 1 open via handoff" in result.output


def test_doctor_fails_on_letters_stacked_behind_a_closed_route(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The blind spot steward #46 names: pickup stopped, the pile kept growing."""
    residents_dir = write_resident(receiving_manifest(status="disabled")).parent.parent
    db = tmp_path / "steward.db"
    deliver_a_letter(db)

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 1
    assert "1 open letter(s) behind a closed route: handoff (disabled)" in result.output
    assert "nothing will pick them up" in result.output


def test_doctor_says_a_closed_route_with_no_post_is_only_closed(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    # Worth saying out loud, not worth failing on: a route somebody is still wiring up is
    # a door that is shut, and nothing is waiting behind it.
    residents_dir = write_resident(receiving_manifest(status="pending")).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0, result.output
    assert "test-agent: inbox 0 open — route closed: handoff (pending)" in result.output


def test_doctor_says_so_when_a_resident_takes_no_letters(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0, result.output
    assert "test-agent: inbox — takes no letters" in result.output


# ------------------------------------------ the cross-process session claim (warren#111)


def test_doctor_names_the_process_running_a_resident(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Where an operator finds out what the 409 they just got was actually about."""
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.claim_resident(
            "test-agent",
            token="t",
            holder="dxp2800:4242",
            kind="routine",
            ref="daily-summary",
            run_id="run-abc",
            stale_before=ev.utc_now_iso(),
        )

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "test-agent: running — routine 'daily-summary' (run run-abc) held by dxp2800:4242" in (
        result.output
    )


def test_doctor_says_a_stale_claim_frees_itself(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A crashed holder is a fact worth showing, and worth showing as temporary."""
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    db = tmp_path / "steward.db"
    long_ago = datetime.now(UTC) - timedelta(seconds=CLAIM_GRACE_S * 10)
    with Store(db) as store:
        store.claim_resident(
            "test-agent",
            token="t",
            holder="dxp2800:4242",
            kind="routine",
            ref="daily-summary",
            stale_before=ev.utc_now_iso(),
            now=ev.utc_now_iso(long_ago),
        )

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "test-agent: a stale session claim is on file" in result.output
    assert "the next fire reclaims it" in result.output


def test_doctor_says_nothing_is_running_after_a_session_ends(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.claim_resident("test-agent", token="t", holder="h", stale_before=ev.utc_now_iso())
        store.release_resident_claim("test-agent", token="t")

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "test-agent: no session running" in result.output


@pytest.mark.usefixtures("nas")
def test_doctor_says_a_retired_resident_fires_nothing(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    tmp_path: Path,
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))
    runner.invoke(
        main,
        [
            "retire",
            "note-keeper",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
        ],
    )

    result = runner.invoke(
        main, ["doctor", str(scratch_repo.residents), "--db", str(tmp_path / "doctor.db")]
    )

    assert "retired — fires nothing" in result.output
    assert "no enabled routines" in result.output
