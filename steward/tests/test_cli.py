"""The CLI is what CI gates on, so its exit codes are part of the contract."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from conftest import (
    REPO_ROOT,
    SECOND_RESIDENT_UID,
    VALID_RESIDENT_UID,
    ResidentWriter,
    ScratchRepo,
    SkillWriter,
    StubWriter,
    valid_manifest,
)
from steward import cli
from steward import events as ev
from steward import notify as nf
from steward.budgets import BudgetGuard
from steward.claims import CLAIM_GRACE_S
from steward.cli import main
from steward.deploy import LocalTransport, TransportError
from steward.journal import latest_entry, write_entry
from steward.manifest import load_manifest
from steward.operator_auth import OPERATOR_CREDENTIAL_PREFIX
from steward.prompt import JOURNAL_MAX_CHARS, assemble_preamble
from steward.scheduler import STALE_TICK_AFTER_S, SchedulerState
from steward.skills import effective_skills, library_for
from steward.store import Store

#: A `claude` current enough for the flag every session carries (steward #206). Doctor
#: probes `--setting-sources` for *every* claude resident, declarations or not, so a stub
#: that answers `--help` with nothing is now a red doctor rather than a quiet one.
CURRENT_CLAUDE = (
    'echo "  --setting-sources <sources>"; echo "  --settings <file-or-json>"; '
    'echo "  --add-dir <directories...>"'
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
def runner() -> CliRunner:
    return CliRunner()


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


def test_events_flush_reports_delivery_and_exits_cleanly(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("CHRONICLE_URL", "https://village.example")
    emitter = cli.ev.EventEmitter.from_env(
        {
            "CHRONICLE_URL": "https://village.example",
            "STEWARD_EVENTS_FALLBACK": str(fallback),
        }
    )
    event = cli.ev.Event(type="routine_started", agent_id="a", project="p")
    assert emitter._queue_record(event, "delivery-cli-0001")
    monkeypatch.setattr(cli.ev.EventEmitter, "_post", lambda *_args: True)

    result = runner.invoke(main, ["events", "flush", "--fallback", str(fallback)])
    assert result.exit_code == 0, result.output
    assert "delivered 1; retired-records 1; pending 0; corrupt 0" in result.output


def test_events_flush_failure_is_visible_and_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("CHRONICLE_URL", "https://village.example")
    emitter = cli.ev.EventEmitter(url="https://village.example", fallback=fallback)
    assert emitter._queue_record(
        cli.ev.Event(type="routine_started", agent_id="a", project="p"),
        "delivery-cli-0002",
    )
    monkeypatch.setattr(cli.ev.EventEmitter, "_post", lambda *_args: False)

    result = runner.invoke(main, ["events", "flush", "--fallback", str(fallback)])
    assert result.exit_code == 1
    assert "delivered 0; retired-records 0; pending 1" in result.output


def test_events_flush_still_drains_pending_when_legacy_read_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("CHRONICLE_URL", "https://village.example")
    emitter = cli.ev.EventEmitter(url="https://village.example", fallback=fallback)
    assert emitter._queue_record(
        cli.ev.Event(type="routine_started", agent_id="a", project="p"),
        "delivery-cli-legacy-error",
    )
    monkeypatch.setattr(
        cli.ev.EventEmitter,
        "import_legacy",
        lambda _self: cli.ev.ImportReport(errors=1, unknown=1),
    )
    monkeypatch.setattr(cli.ev.EventEmitter, "_post", lambda *_args: True)

    result = runner.invoke(
        main,
        ["events", "flush", "--fallback", str(fallback), "--include-legacy", "--format", "json"],
    )
    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["legacy_errors"] == 1
    assert payload["legacy_unknown"] == 1
    assert payload["delivered"] == 1
    assert payload["pending"] == 0


def scheduler_builder(engine: object, cleanup: Mock):
    """Return a CLI builder double whose ownership boundary can be asserted."""

    @contextmanager
    def build(*_args: object, **_kwargs: object) -> Iterator[object]:
        try:
            yield engine
        finally:
            cleanup()

    return build


def test_validate_defaults_to_the_residents_tree(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["validate"])
    assert result.exit_code == 0, result.output
    assert "2 valid resident(s)" in result.output


def test_validate_accepts_explicit_paths(runner: CliRunner, write_resident: ResidentWriter) -> None:
    manifest_path = write_resident()
    result = runner.invoke(main, ["validate", str(manifest_path)])
    assert result.exit_code == 0
    assert "ok:" in result.output


def test_validate_exits_non_zero_on_error(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    data = valid_manifest()
    del data["app_grants"]
    manifest_path = write_resident(data)
    result = runner.invoke(main, ["validate", str(manifest_path)])
    assert result.exit_code == 1
    assert "app_grants" in result.output
    assert "required field is missing" in result.output
    assert "failed:" in result.output


def test_validate_reports_invalid_utf8_without_a_traceback(
    runner: CliRunner, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_bytes(b"\xff\xfe")

    result = runner.invoke(main, ["validate", str(manifest_path)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "manifest is not valid UTF-8" in result.output


def test_validate_reports_json(runner: CliRunner, write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["memory"]
    manifest_path = write_resident(data)
    result = runner.invoke(main, ["validate", "--format", "json", str(manifest_path)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["residents"] == []
    assert payload["diagnostics"][0]["field_path"] == "memory"
    assert payload["diagnostics"][0]["severity"] == "error"
    assert payload["diagnostics"][0]["example"]


def test_validate_json_lists_valid_residents(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    manifest_path = write_resident()
    result = runner.invoke(main, ["validate", "--format", "json", str(manifest_path)])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["residents"][0]["uid"] == VALID_RESIDENT_UID
    assert payload["residents"][0]["agent_id"] == "claude-code:test-agent"


def test_validate_with_no_path_fails_when_it_found_nothing(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge gate must not pass green having validated nothing (steward #137).

    CI runs a bare ``uv run steward validate``, which falls back to a *relative*
    ``residents``, resolved against the process cwd. Rename the tree, move it, or change
    the job's working directory and this used to print ``ok: 0 valid resident(s)`` and
    exit 0 — the step whose stated purpose is that an invalid manifest must never merge,
    reporting success without reading a manifest.
    """
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main, ["validate"])

    assert result.exit_code == 1, result.output
    assert "failed:" in result.output
    assert "this run validated nothing" in result.output


def test_validate_with_no_path_names_the_tree_it_actually_looked_in(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A green run in the wrong directory should be visible in the log by its path."""
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main, ["validate"])

    assert str((tmp_path / "residents").resolve()) in result.output


def test_validate_with_no_path_reports_the_failure_as_json_too(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both reporters read the same result, so neither can disagree about ``ok``."""
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main, ["validate", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["diagnostics"][0]["severity"] == "error"


def test_validate_with_no_path_fails_even_if_the_empty_tree_warning_is_reworded(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate keys on the count, not on the wording (steward #137).

    An earlier draft promoted the diagnostic whose ``problem`` matched
    ``NO_MANIFESTS_PROBLEM`` exactly, which fails *open*: reword that string at the
    source, or reach zero residents down a path that words it differently, and the
    promotion matches nothing and CI silently goes back to exiting 0 on a run that
    validated nothing. A merge gate has to fail closed.
    """
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "NO_MANIFESTS_PROBLEM", "something else entirely")

    result = runner.invoke(main, ["validate"])

    assert result.exit_code == 1, result.output
    assert "failed:" in result.output


def test_validate_on_a_named_empty_tree_is_still_only_a_warning(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Asking about an empty directory is a fair question, and gets a fair answer.

    Only the *defaulted* run is held to "you must have found something": naming a tree is
    a deliberate act, and ``steward validate ./drafts`` before anything is drafted is not
    a failure.
    """
    empty = tmp_path / "drafts"
    empty.mkdir()

    result = runner.invoke(main, ["validate", str(empty)])

    assert result.exit_code == 0, result.output
    assert "ok:" in result.output
    assert "warning" in result.output


def test_validate_rejects_a_missing_path(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(main, ["validate", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_validate_multiple_targets(runner: CliRunner, write_resident: ResidentWriter) -> None:
    first = write_resident()
    second_data = valid_manifest()
    second_data["id"] = "other-agent"
    second = write_resident(second_data, soul=None)
    result = runner.invoke(main, ["validate", str(first.parent), str(second.parent)])
    assert result.exit_code == 1
    assert "soul.file" in result.output


def test_schema_command_emits_json_schema(runner: CliRunner) -> None:
    result = runner.invoke(main, ["schema"])
    assert result.exit_code == 0
    schema = json.loads(result.output)
    assert schema["title"] == "steward resident manifest v0"


def test_schema_output_writes_exactly_what_stdout_prints(runner: CliRunner, tmp_path: Path) -> None:
    """`make schema-write` regenerates the committed artifact through this flag.

    Byte-identical to stdout, or the committed copy and the printed one would be two
    different contracts and tests/test_schema_contract.py would fail for no real reason.
    """
    target = tmp_path / "nested" / "resident-manifest-v0.json"
    printed = runner.invoke(main, ["schema"])
    written = runner.invoke(main, ["schema", "--output", str(target)])

    assert written.exit_code == 0, written.output
    assert not written.output, "--output writes the file; it does not also print it"
    assert target.read_text(encoding="utf-8") == printed.output
    assert printed.output.endswith("}\n")


def test_openapi_command_emits_the_document_the_api_serves_to_nobody(runner: CliRunner) -> None:
    """The offline export that stands in for the schema route steward refuses to serve."""
    result = runner.invoke(main, ["openapi"])
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["info"]["title"] == "steward"
    assert "/residents" in document["paths"]


def test_openapi_output_writes_exactly_what_stdout_prints(
    runner: CliRunner, tmp_path: Path
) -> None:
    """`make openapi-write` regenerates the committed artifact through this flag."""
    target = tmp_path / "nested" / "openapi.json"
    printed = runner.invoke(main, ["openapi"])
    written = runner.invoke(main, ["openapi", "--output", str(target)])

    assert written.exit_code == 0, written.output
    assert not written.output, "--output writes the file; it does not also print it"
    assert target.read_text(encoding="utf-8") == printed.output
    assert printed.output.endswith("}\n")


def test_help_lists_the_commands(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("validate", "schema", "openapi", "doctor", "scheduler", "show"):
        assert command in result.output


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


# ------------------------------------------------------------------------------ journal


def journaling_resident(tmp_path: Path) -> dict:
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(tmp_path / "memory")}
    return data


def test_journal_prints_entries_newest_first(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = journaling_resident(tmp_path)
    path = write_resident(data)
    manifest = load_manifest(path).manifest
    write_entry(manifest, date(2026, 8, 23), "close-of-day", "The night before.")
    write_entry(manifest, date(2026, 8, 24), "close-of-day", "Two drafts still waiting.")

    result = runner.invoke(main, ["journal", "test-agent", "--residents", str(path.parent.parent)])
    assert result.exit_code == 0, result.output
    assert result.output.index("2026-08-24") < result.output.index("2026-08-23")
    assert "Two drafts still waiting." in result.output
    assert "close-of-day" in result.output


def test_journal_honours_the_limit(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(journaling_resident(tmp_path))
    manifest = load_manifest(path).manifest
    for day in (22, 23, 24):
        write_entry(manifest, date(2026, 8, day), "close-of-day", f"the {day}th")

    args = ["--residents", str(path.parent.parent), "--limit", "1"]
    result = runner.invoke(main, ["journal", "test-agent", *args])
    assert result.exit_code == 0
    assert "the 24th" in result.output
    assert "the 22nd" not in result.output


def test_journal_reports_json_for_machines(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(journaling_resident(tmp_path))
    write_entry(load_manifest(path).manifest, date(2026, 8, 24), "close-of-day", "Quiet.")

    args = ["--residents", str(path.parent.parent), "--format", "json"]
    result = runner.invoke(main, ["journal", "test-agent", *args])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["date"] == "2026-08-24"
    assert payload[0]["routine"] == "close-of-day"
    assert payload[0]["text"] == "Quiet."


def test_journal_says_so_when_nothing_has_been_written(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(journaling_resident(tmp_path))
    result = runner.invoke(main, ["journal", "test-agent", "--residents", str(path.parent.parent)])
    assert result.exit_code == 0
    assert "has not written a journal entry yet" in result.output


def test_journal_names_the_residents_it_knows_about(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    path = write_resident()
    result = runner.invoke(main, ["journal", "nobody", "--residents", str(path.parent.parent)])
    assert result.exit_code == 1
    assert "no valid resident 'nobody'" in result.output
    assert "test-agent" in result.output


def test_journal_refuses_a_memory_it_cannot_read_a_journal_out_of(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    data = valid_manifest()
    data["memory"] = {"kind": "file", "path": "/data/test-agent/memory.md"}
    path = write_resident(data)
    result = runner.invoke(main, ["journal", "test-agent", "--residents", str(path.parent.parent)])
    assert result.exit_code == 1
    assert "nowhere to keep one entry per day" in result.output


# --------------------------------------------------------------------------------- show


def show_args(path: Path, tmp_path: Path) -> list[str]:
    """Point at the resident tree and at a throwaway database, never the real one."""
    return ["--residents", str(path.parent.parent), "--db", str(tmp_path / "show.db")]


def test_show_prints_exactly_the_assembled_preamble(
    runner: CliRunner,
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    tmp_path: Path,
) -> None:
    """One assembly, not a second renderer: what is printed is what a session is told.

    The sections themselves are :mod:`tests.test_prompt`'s contract; this asserts only
    that the command adds nothing to and takes nothing from it.
    """
    path = write_resident(journaling_resident(tmp_path))
    write_skill("write-journal", defaults=True)
    write_skill("daily-summary")
    resident = load_manifest(path)
    write_entry(resident.manifest, date(2026, 8, 24), "close-of-day", "Two drafts still waiting.")

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])
    assert result.exit_code == 0, result.output
    expected = assemble_preamble(
        resident.manifest,
        resident.soul.body,
        latest_entry(resident.manifest, source=resident.path),
        effective_skills(resident.manifest, library_for(path.parent.parent)),
    )
    assert result.output == expected + "\n"


def test_show_reports_json_for_machines(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(journaling_resident(tmp_path))
    args = [*show_args(path, tmp_path), "--format", "json"]
    result = runner.invoke(main, ["show", "test-agent", *args])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["resident"] == "test-agent"
    assert payload["journal"] is False
    assert payload["decisions"] == 0
    assert "YOUR CHARTER (AUTHORITATIVE, LAST WORD)" in payload["preamble"]


def test_show_does_not_consume_a_pending_decision(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A preview must not eat the answer the resident's next real session is owed (#74)."""
    path = write_resident(journaling_resident(tmp_path))
    resident = load_manifest(path)
    db = tmp_path / "show.db"
    with Store(db) as store:
        request = store.create_approval_request(
            agent_id=resident.agent_id,
            project="p",
            action="send_email",
            message="…",
            resident=resident.id,
        )
        store.decide(request.request_id, "approve", decided_by="api")

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])
    assert result.exit_code == 0, result.output
    assert "send_email: approve" in result.output
    with Store(db) as after:
        still_waiting = after.undelivered_decisions(resident.id)
    assert [record.request_id for record in still_waiting] == [request.request_id]


def test_show_redacts_a_secret_the_resident_journaled(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The journal is the one section no validator scanned: a model wrote it at runtime."""
    path = write_resident(journaling_resident(tmp_path))
    manifest = load_manifest(path).manifest
    write_entry(manifest, date(2026, 8, 24), "close-of-day", "reused sk-ant-abcdef0123456789ghij")

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])
    assert result.exit_code == 0, result.output
    assert "sk-ant-" not in result.output
    assert "[redacted:secret]" in result.output


def test_show_redacts_the_journal_before_it_caps_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A cap applied first destroys the shape the detector matches on (steward #209).

    `redact_secrets` finds a PEM block by its BEGIN *and* END markers. Cut the entry at
    the injection cap first and a block straddling the cut loses its END, so only its
    lone BEGIN is replaced and the key material prints intact — right after a
    `[redacted:secret]` that makes it look as though the scrub worked.
    """
    path = write_resident(journaling_resident(tmp_path))
    manifest = load_manifest(path).manifest
    key_body = "MIIEowIBAAKCAQEA" + "Zx9Kq3" * 200
    entry = (
        "x" * (JOURNAL_MAX_CHARS - 200)
        + f"-----BEGIN RSA PRIVATE KEY-----\n{key_body}\n-----END RSA PRIVATE KEY-----"
    )
    write_entry(manifest, date(2026, 8, 24), "close-of-day", entry)

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])

    assert result.exit_code == 0, result.output
    assert "MIIEowIBAAKCAQEA" not in result.output
    assert "Zx9Kq3" not in result.output
    assert "[redacted:secret]" in result.output


def test_a_redacted_journal_is_still_capped(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Redacting first must not be a way around the cap the preview still owes."""
    path = write_resident(journaling_resident(tmp_path))
    manifest = load_manifest(path).manifest
    write_entry(manifest, date(2026, 8, 24), "close-of-day", "y" * (JOURNAL_MAX_CHARS * 2))

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])

    assert result.exit_code == 0, result.output
    assert "[truncated at the injection cap]" in result.output
    assert "y" * (JOURNAL_MAX_CHARS + 1) not in result.output


def test_show_redacts_a_secret_a_decision_carries(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A decision's detail and edit are model-written too, and just as unscanned.

    The detail is whatever the session typed into its ``<needs-human>`` block and is stored
    verbatim; the edit is whatever the human answered with. Only burrow's egress redacted
    them until now, so ``steward show`` printed a live key to anyone previewing a preamble.
    """
    path = write_resident(journaling_resident(tmp_path))
    resident = load_manifest(path)
    db = tmp_path / "show.db"
    with Store(db) as store:
        request = store.create_approval_request(
            agent_id=resident.agent_id,
            project="p",
            action="rotate_token",
            message="rotate the deploy key",
            resident=resident.id,
            detail={"cmd": "curl -H 'Authorization: Bearer sk-ant-abcdef0123456789ghij'"},
        )
        store.decide(
            request.request_id,
            "edit",
            decided_by="miha",
            edit={"nested": ["use ghp_abcdefghijklmnopqrstuvwxyz012345 instead"]},
        )

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])
    assert result.exit_code == 0, result.output
    assert "sk-ant-" not in result.output
    assert "ghp_" not in result.output
    assert result.output.count("[redacted:secret]") == 2
    # The decision itself still reads as itself: only the secret is cut.
    assert "rotate_token: edit (decided by miha" in result.output


def test_show_names_the_residents_it_knows_about(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident()
    result = runner.invoke(main, ["show", "nobody", *show_args(path, tmp_path)])
    assert result.exit_code == 1
    assert "no valid resident 'nobody'" in result.output


# ---------------------------------------------------------------------------- scheduler

#: The one routine :func:`mock_resident` declares, and the key the scheduler files it
#: under — the resident id and the routine id, which is how an anchor is addressed.
MOCK_ROUTINE_ID = "inbox-read"
MOCK_ROUTINE_KEY = f"test-agent/{MOCK_ROUTINE_ID}"


def scheduler_state_file(tmp_path: Path) -> Path:
    """Return the state file every scheduler test in this file schedules against.

    One spelling, because ``--state`` and anything that seeds an anchor into it have to
    name the same file: two literals that drifted apart would leave the seed writing
    somewhere the CLI never reads, and the test would pass by asserting the fresh-state
    behaviour it was written to distinguish from.
    """
    return tmp_path / "state.json"


def scheduler_args(path: Path, tmp_path: Path) -> list[str]:
    return [
        "--residents",
        str(path.parent),
        "--state",
        str(scheduler_state_file(tmp_path)),
        "--workdir",
        str(tmp_path),
    ]


def seed_anchor(tmp_path: Path, ago: timedelta, key: str = MOCK_ROUTINE_KEY) -> Path:
    """Write a state file whose anchor for ``key`` is already that far in the past.

    First sight anchors a routine at *now*, so nothing is ever due against a state file
    that has never been written — which is the right answer and a useless fixture. Every
    test that needs a routine to actually be due says so here.
    """
    state = SchedulerState(path=scheduler_state_file(tmp_path))
    state.set_anchor(key, datetime.now(UTC) - ago)
    state.save()
    return state.path


def mock_resident() -> dict:
    data = valid_manifest()
    data["runner"] = {"kind": "mock", "model": "pretend"}
    data["routines"] = [
        {
            "id": MOCK_ROUTINE_ID,
            "schedule": "* * * * *",
            "prompt": "Read the mail.",
            "timeout_s": 60,
            "enabled": True,
        }
    ]
    return data


def test_scheduler_tick_fires_and_reports(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    path = write_resident(mock_resident())
    args = scheduler_args(path, tmp_path)

    first = runner.invoke(main, ["scheduler", "tick", *args])
    assert first.exit_code == 0, first.output
    assert "nothing due" in first.output  # first sight only anchors

    second = runner.invoke(main, ["scheduler", "tick", *args])
    assert second.exit_code == 0, second.output


def test_scheduler_tick_exits_non_zero_on_an_unpersistable_state(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduler that cannot persist its anchor must stop the cron run, not fire blind."""
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    path = write_resident(mock_resident())
    state_dir = tmp_path / "state-is-a-directory"
    state_dir.mkdir()
    args = ["--residents", str(path.parent), "--state", str(state_dir), "--workdir", str(tmp_path)]

    result = runner.invoke(main, ["scheduler", "tick", *args])
    assert result.exit_code == 1
    assert "STEWARD_STATE names a directory" in result.output


def test_scheduler_dry_run_prints_the_prompt_and_emits_nothing(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(fallback))
    path = write_resident(mock_resident())
    state_file = seed_anchor(tmp_path, timedelta(minutes=5))
    before = state_file.read_text(encoding="utf-8")
    result = runner.invoke(
        main, ["scheduler", "tick", *scheduler_args(path, tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert f"would fire {MOCK_ROUTINE_KEY}" in result.output
    assert "YOUR CHARTER (AUTHORITATIVE, LAST WORD)" in result.output
    assert not fallback.exists()
    assert state_file.read_text(encoding="utf-8") == before  # a rehearsal anchors nothing


@pytest.mark.parametrize("command", ["tick", "run"])
def test_scheduler_dry_run_rehearses_only_what_is_due(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    command: str,
) -> None:
    """A rehearsal is a rehearsal of the *next tick*, so it answers the tick's question.

    Printing every routine as "would fire" said something that was not true of any of
    them, and it said it loudest on the fleet with the most routines — the operator
    reading it cannot tell the 07:00 summary that is about to run from the one that runs
    in nine hours (warren#90).
    """
    data = mock_resident()
    data["routines"].append(
        {
            "id": "nightly",
            "schedule": "0 4 * * *",
            "prompt": "Sleep.",
            "timeout_s": 60,
            "enabled": True,
        }
    )
    path = write_resident(data)
    seed_anchor(tmp_path, timedelta(minutes=5))
    result = runner.invoke(
        main, ["scheduler", command, *scheduler_args(path, tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert f"would fire {MOCK_ROUTINE_KEY}" in result.output
    assert "test-agent/nightly" not in result.output


@pytest.mark.parametrize("command", ["tick", "run"])
def test_scheduler_dry_run_on_a_fresh_state_has_nothing_to_rehearse(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    command: str,
) -> None:
    """First sight anchors at now, so nothing is due yet — and the rehearsal says so."""
    path = write_resident(mock_resident())
    result = runner.invoke(
        main, ["scheduler", command, *scheduler_args(path, tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "nothing due" in result.output
    assert "would fire" not in result.output
    assert not scheduler_state_file(tmp_path).exists()


def test_scheduler_run_dry_run_does_not_loop(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(mock_resident())
    seed_anchor(tmp_path, timedelta(minutes=5))
    result = runner.invoke(main, ["scheduler", "run", *scheduler_args(path, tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would fire" in result.output


def test_scheduler_run_stops_after_max_ticks(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("steward.scheduler.MAX_SLEEP_S", 0.01)
    path = write_resident(mock_resident())
    result = runner.invoke(
        main, ["scheduler", "run", *scheduler_args(path, tmp_path), "--max-ticks", "1"]
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("command", [("tick",), ("run", "--max-ticks", "1")])
@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [((True,), 0), ((True, False), 1), ((False,), 1), ((None,), 0)],
)
def test_scheduler_commands_carry_fire_outcomes(  # noqa: PLR0913, PLR0917
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    outcomes: tuple[bool | None, ...],
    expected: int,
) -> None:
    reports = [
        SimpleNamespace(
            fired=ok is not None,
            scheduled=SimpleNamespace(key=f"agent/routine-{index}"),
            result=(
                SimpleNamespace(ok=ok, duration_s=0.1, summary=lambda: "exit 7")
                if ok is not None
                else None
            ),
            skipped_reason="policy refusal" if ok is None else None,
        )
        for index, ok in enumerate(outcomes)
    ]
    engine = SimpleNamespace(
        scheduled=(),
        require_ready=lambda: None,
        tick=lambda: reports,
        run=lambda **_kwargs: reports,
    )
    cleanup = Mock()
    monkeypatch.setattr(cli, "_build_scheduler", scheduler_builder(engine, cleanup))

    result = runner.invoke(main, ["scheduler", *command, "--residents", str(tmp_path)])

    assert result.exit_code == expected, result.output
    cleanup.assert_called_once_with()


@pytest.mark.parametrize(
    ("command", "expected", "message"),
    [(("tick",), 1, "Aborted!"), (("run",), 0, "stopped")],
)
def test_scheduler_commands_release_resources_on_interrupt(  # noqa: PLR0913, PLR0917
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    expected: int,
    message: str,
) -> None:
    def interrupted(**_kwargs: object) -> list[object]:
        raise KeyboardInterrupt

    engine = SimpleNamespace(
        scheduled=(), require_ready=lambda: None, tick=interrupted, run=interrupted
    )
    cleanup = Mock()
    monkeypatch.setattr(cli, "_build_scheduler", scheduler_builder(engine, cleanup))
    result = runner.invoke(main, ["scheduler", *command, "--residents", str(tmp_path)])
    assert result.exit_code == expected, result.output
    assert message in result.output
    cleanup.assert_called_once_with()


@pytest.mark.parametrize("command", [("tick",), ("run", "--max-ticks", "1")])
def test_scheduler_commands_release_resources_on_scheduler_error(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
) -> None:
    def failed(*_args: object, **_kwargs: object) -> list[object]:
        raise cli.SchedulerError("scheduler broke")

    engine = SimpleNamespace(scheduled=(), require_ready=lambda: None, tick=failed, run=failed)
    cleanup = Mock()
    monkeypatch.setattr(cli, "_build_scheduler", scheduler_builder(engine, cleanup))

    result = runner.invoke(main, ["scheduler", *command, "--residents", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "scheduler broke" in result.output
    cleanup.assert_called_once_with()


def test_scheduler_builder_closes_store_when_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Mock()
    monkeypatch.setattr(cli, "_load_or_exit", lambda _residents: [])
    monkeypatch.setattr(cli, "_open_store", lambda _db: store)
    monkeypatch.setattr(cli.Dispatcher, "from_path", lambda *_args, **_kwargs: Mock())
    monkeypatch.setattr(cli, "Scheduler", Mock(side_effect=cli.SchedulerError("build broke")))

    with (
        pytest.raises(cli.SchedulerError, match="build broke"),
        cli._build_scheduler(
            tmp_path, tmp_path / "state.json", tmp_path, tmp_path / "store.db", 60, dry_run=False
        ),
    ):
        pass

    store.close.assert_called_once_with()


@pytest.mark.usefixtures("empty_path")
def test_scheduler_refuses_to_start_without_the_declared_binary(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = mock_resident()
    data["runner"] = {"kind": "claude", "model": "claude-opus-5"}
    path = write_resident(data)
    for command in ("tick", "run"):
        result = runner.invoke(main, ["scheduler", command, *scheduler_args(path, tmp_path)])
        assert result.exit_code == 1, result.output
        assert "not on PATH" in result.output


def test_scheduler_refuses_an_invalid_tree(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    del data["memory"]
    path = write_resident(data)
    result = runner.invoke(main, ["scheduler", "tick", *scheduler_args(path, tmp_path)])
    assert result.exit_code == 1
    assert "memory" in result.output


# --------------------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------------------


def test_serve_refuses_to_start_without_a_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    result = runner.invoke(
        main, ["serve", "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")]
    )
    assert result.exit_code == 1
    assert "STEWARD_TOKEN" in result.output
    assert "--allow-open" in result.output
    assert not (tmp_path / "s.db").exists()


def test_serve_binds_loopback_by_default(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEWARD_TOKEN", "a-shared-secret")
    monkeypatch.setenv("STEWARD_CORS_ORIGINS", "http://village.local")
    served: dict[str, object] = {}
    monkeypatch.setattr(
        "steward.cli.run_server",
        lambda app, *, host, port: served.update(app=app, host=host, port=port),
    )
    result = runner.invoke(
        main, ["serve", "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")]
    )
    assert result.exit_code == 0, result.output
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8801
    assert "http://127.0.0.1:8801" in result.output
    assert "http://village.local" in result.output


def test_serve_says_out_loud_when_it_has_no_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    monkeypatch.delenv("STEWARD_CORS_ORIGINS", raising=False)
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: None)
    result = runner.invoke(
        main,
        [
            "serve",
            "--allow-open",
            "--port",
            "9000",
            "--residents",
            str(tmp_path),
            "--db",
            str(tmp_path / "s.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "without a token" in result.output
    assert "cors: none" in result.output


def test_allow_open_is_refused_on_a_non_loopback_bind(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--allow-open serves every write path with no token; a public bind is refused (#81)."""
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    served: dict[str, object] = {}
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: served.setdefault("ran", True))
    result = runner.invoke(
        main,
        ["serve", "--allow-open", "--host", "0.0.0.0", "--residents", str(tmp_path)],  # noqa: S104
    )
    assert result.exit_code == 1
    assert "loopback" in result.output
    assert "ran" not in served, "the server was never started"


def test_allow_open_is_permitted_on_loopback(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: None)
    for host in ("127.0.0.1", "::1", "localhost"):
        result = runner.invoke(
            main, ["serve", "--allow-open", "--host", host, "--residents", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------------------


def test_skills_lists_the_shipped_library_and_every_resident(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["skills"])
    assert result.exit_code == 0, result.output
    assert "write-journal  [default]" in result.output
    assert "read-inbox  [granted]" in result.output
    assert "hob: escalate, write-journal, vault-keeper, morning-digest" in (result.output)
    assert "burrow-builder" not in result.output
    # Named as a copy the CLI does not discover: since steward #206 a claude session is
    # launched with `--setting-sources ""`, and `.claude/skills` is discovered through the
    # project setting source. The prompt is the delivery path; printing two working
    # channels here would be the claim that is no longer true.
    assert "a copy in .claude/skills/ the session's CLI does not discover" in result.output


def test_skills_reports_json(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["skills", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["library"].endswith("skills")
    assert {"errands", "escalate"} <= {skill["name"] for skill in payload["skills"]}
    assert "vault-keeper" in payload["residents"]["hob"]
    assert payload["diagnostics"] == []


def test_skills_says_so_when_there_is_no_library(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    write_resident()
    result = runner.invoke(main, ["skills", "--residents", str(tmp_path / "residents")])
    assert result.exit_code == 0
    assert "no skills library found" in result.output
    assert "test-agent: none" in result.output


def test_skills_exits_non_zero_on_a_broken_library(
    runner: CliRunner, write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    write_skill("broken", text="---\nname: broken\n---\n\nNo description.\n")
    write_resident()
    result = runner.invoke(main, ["skills", "--residents", str(tmp_path / "residents")])
    assert result.exit_code == 1
    assert "description" in result.output


def test_skills_takes_an_explicit_library(
    runner: CliRunner, write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    write_skill("errands", root=tmp_path / "other-library", defaults=True)
    data = valid_manifest()
    data["skills"] = []
    data["routines"] = []
    write_resident(data)
    result = runner.invoke(
        main,
        [
            "skills",
            "--residents",
            str(tmp_path / "residents"),
            "--skills",
            str(tmp_path / "other-library"),
        ],
    )
    assert result.exit_code == 0
    assert "test-agent: errands" in result.output


def test_validate_takes_an_explicit_library(
    runner: CliRunner, write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    write_skill("read-inbox", root=tmp_path / "other-library")
    data = valid_manifest()
    data["skills"] = ["errands"]
    data["routines"] = []
    manifest_path = write_resident(data)
    result = runner.invoke(
        main, ["validate", str(manifest_path), "--skills", str(tmp_path / "other-library")]
    )
    assert result.exit_code == 1
    assert "not in the skills library" in result.output


# --------------------------------------------------------------------------------------
# the board
# --------------------------------------------------------------------------------------


def board_manifest() -> dict[str, Any]:
    """Build a manifest that opts into the board, with the route its declaration needs."""
    data = valid_manifest()
    data["routes"] = [
        *data["routes"],
        {"id": "job-board", "kind": "job-board", "address": "steward:job-board"},
    ]
    data["board"] = {"claim": True}
    data["runner"] = {"kind": "mock"}
    return data


def test_board_dispatch_claims_and_works_a_task(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Research X")

    result = runner.invoke(
        main, ["board", "dispatch", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "done test-agent" in result.output
    with Store(db) as after:
        assert [job.status for job in after.jobs()] == ["done"]


def test_board_dispatch_dry_run_plans_without_spending(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The first dispatch against shipped residents must be seeable before it spends (#88)."""
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Research X")

    result = runner.invoke(
        main,
        ["board", "dispatch", "--residents", str(residents_dir), "--db", str(db), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "would claim" in result.output
    assert "test-agent" in result.output
    # Nothing was claimed and no session ran: the task is still open.
    with Store(db) as after:
        assert [job.status for job in after.jobs()] == ["open"]


def test_board_dispatch_with_an_empty_board_says_so(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    result = runner.invoke(
        main,
        ["board", "dispatch", "--residents", str(residents_dir), "--db", str(tmp_path / "b.db")],
    )
    assert result.exit_code == 0, result.output
    assert "nothing claimed" in result.output


@pytest.mark.parametrize(("done", "expected"), [((True,), 0), ((True, False), 1), ((False,), 1)])
def test_board_dispatch_carries_clean_partial_and_failed_outcomes(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    done: tuple[bool, ...],
    expected: int,
) -> None:
    reports = tuple(
        SimpleNamespace(
            done=ok,
            delegated=False,
            resident_id="test-agent",
            task=SimpleNamespace(
                task_id=f"task-{index}", title="work", delegated=False, delegated_by=None
            ),
            reason=None if ok else "exit 7",
            raised=(),
            handed_over=(),
        )
        for index, ok in enumerate(done)
    )
    dispatch = SimpleNamespace(reopened=(), expired_approvals=(), reports=reports, planned=())
    monkeypatch.setattr(
        cli.Dispatcher,
        "from_path",
        lambda *_args, **_kwargs: SimpleNamespace(dispatch=lambda: dispatch),
    )

    result = runner.invoke(
        main, ["board", "dispatch", "--residents", str(tmp_path), "--db", str(tmp_path / "b.db")]
    )

    assert result.exit_code == expected, result.output


def test_board_dispatch_scrubs_the_text_a_session_wrote(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch line prints a task title and a handoff message on every sweep.

    Both carry text somebody else wrote — a job posted over the API, a title another
    resident's session chose — and both land in a terminal scrollback (steward #144). A
    knock's message is derived from `soul.name` and the action, so it is here for
    completeness rather than because it can carry a secret.
    """
    leak = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    with Store(tmp_path / "raised.db") as store:
        record = store.create_approval_request(
            agent_id="claude-code:test-agent",
            project="p",
            action="send_email",
            message=f"Testy needs {leak}",
        )
    reports = (
        SimpleNamespace(
            done=True,
            delegated=False,
            resident_id="test-agent",
            task=SimpleNamespace(
                task_id="t1", title=f"file the key {leak}", delegated=False, delegated_by=None
            ),
            reason=None,
            raised=(record,),
            handed_over=(
                SimpleNamespace(
                    task=None, reason="not_permitted", message=f"tried to delegate {leak!r}"
                ),
            ),
        ),
    )
    dispatch = SimpleNamespace(reopened=(), expired_approvals=(), reports=reports, planned=())
    monkeypatch.setattr(
        cli.Dispatcher,
        "from_path",
        lambda *_args, **_kwargs: SimpleNamespace(dispatch=lambda: dispatch),
    )

    result = runner.invoke(
        main, ["board", "dispatch", "--residents", str(tmp_path), "--db", str(tmp_path / "b.db")]
    )

    assert result.exit_code == 0, result.output
    assert "ghp_" not in result.output
    # All three lines printed, and all three scrubbed.
    assert result.output.count("[redacted:secret]") == 3
    assert "file the key" in result.output  # only the secret is cut


def test_board_dispatch_reports_the_deadlines_it_swept(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Abandoned")
        store.claim_next_job(
            claimant="claude-code:ghost", skills=[], lease_expires_at="2020-01-01T00:00:00.000Z"
        )
        store.create_approval_request(
            agent_id="a:b",
            project="p",
            action="spend_money",
            message="…",
            expires_at="2020-01-01T00:00:00.000Z",
        )

    result = runner.invoke(
        main,
        ["board", "dispatch", "--residents", str(residents_dir), "--db", str(db), "--sweep-only"],
    )
    assert result.exit_code == 0, result.output
    assert "lease expired" in result.output
    assert "approval expired: spend_money denied by default" in result.output
    with Store(db) as after:
        assert after.jobs("open"), "a sweep reopens the lease, and claims nothing"


def test_board_list_shows_the_board_and_who_could_take_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Anyone", required_skills=["daily-summary"])
        store.post_job(title="Nobody here", required_skills=["surgery"])

    result = runner.invoke(
        main, ["board", "list", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "claimable by: test-agent" in result.output
    assert "claimable by: nobody on this tree" in result.output
    assert "skills: surgery" in result.output


def test_board_list_reports_json_and_an_empty_board(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    empty = runner.invoke(
        main, ["board", "list", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert "the board is empty" in empty.output

    with Store(db) as store:
        store.post_job(title="One thing")
        store.claim_next_job(claimant="a:b", skills=[], lease_expires_at="2030-01-01T00:00:00.000Z")
    result = runner.invoke(
        main,
        ["board", "list", "--residents", str(residents_dir), "--db", str(db), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["status"] == "claimed"
    assert payload[0]["claimant"] == "a:b"


# --------------------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------------------


def test_approval_raise_records_a_request_a_human_can_answer(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token-free path: a session with a shell, not with steward's API token."""
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("CHRONICLE_URL", raising=False)
    residents_dir = write_resident().parent.parent
    db = tmp_path / "approvals.db"

    result = runner.invoke(
        main,
        [
            "approval", "raise", "test-agent",
            "--action", "send_email",
            "--detail-json", '{"to": "plumber@example.com"}',
            "--expires-in", "4h",
            "--options", "approve,deny",
            "--residents", str(residents_dir),
            "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Testy wants to send email" in result.output
    request_id = result.output.strip().splitlines()[-1]

    with Store(db) as store:
        record = store.approval(request_id)
    assert record is not None
    assert record.pending
    assert record.action == "send_email"
    assert record.detail == {"to": "plumber@example.com"}
    assert record.options == ("approve", "deny")
    assert record.resident == "test-agent"

    emitted = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [event["type"] for event in emitted] == ["needs_human"]
    assert emitted[0]["payload"]["request_id"] == request_id


def test_approval_raise_accepts_a_plain_note(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    residents_dir = write_resident().parent.parent
    db = tmp_path / "approvals.db"
    result = runner.invoke(
        main,
        [
            "approval", "raise", "test-agent",
            "--action", "cancel_thursday",
            "--note", "Should I cancel Thursday?",
            "--residents", str(residents_dir),
            "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as store:
        assert store.pending_approvals()[0].detail == {"note": "Should I cancel Thursday?"}


@pytest.mark.parametrize(
    ("flags", "complaint"),
    [
        (["--action", "Send Email"], "is not a slug"),
        (["--action", "send_email", "--detail-json", "{oops"], "does not parse"),
        (["--action", "send_email", "--detail-json", "[1]"], "must be a JSON object"),
        (["--action", "send_email", "--expires-in", "soon"], "is not a duration"),
        (["--action", "send_email", "--options", "maybe"], "unknown option"),
    ],
)
def test_approval_raise_refuses_a_request_it_cannot_read(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    flags: list[str],
    complaint: str,
) -> None:
    residents_dir = write_resident().parent.parent
    db = tmp_path / "approvals.db"
    result = runner.invoke(
        main,
        [
            "approval", "raise", "test-agent", *flags,
            "--residents", str(residents_dir), "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert complaint in result.output
    with Store(db) as store:
        assert store.approvals() == []


def test_approval_raise_refuses_both_detail_and_note(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident().parent.parent
    result = runner.invoke(
        main,
        [
            "approval", "raise", "test-agent",
            "--action", "send_email",
            "--detail-json", "{}",
            "--note", "also this",
            "--residents", str(residents_dir),
            "--db", str(tmp_path / "a.db"),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "not both" in result.output


def test_approval_raise_needs_a_resident_that_exists(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident().parent.parent
    result = runner.invoke(
        main,
        [
            "approval", "raise", "nobody",
            "--action", "send_email",
            "--residents", str(residents_dir),
            "--db", str(tmp_path / "a.db"),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "no valid resident 'nobody'" in result.output


def test_approval_show_is_the_audit_query(runner: CliRunner, tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    with Store(db) as store:
        record = store.create_approval_request(
            agent_id="claude-code:test-agent",
            project="p",
            action="send_email",
            message="Testy wants to send email",
            resident="test-agent",
            detail={"to": "a@example.com"},
            expires_at="2030-01-01T00:00:00.000Z",
        )
        waiting = runner.invoke(main, ["approval", "show", record.request_id, "--db", str(db)])
        assert "still waiting" in waiting.output
        store.decide(record.request_id, "edit", decided_by="api", edit={"subject": "shorter"})

    result = runner.invoke(main, ["approval", "show", record.request_id, "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "decision:  edit by api" in result.output
    assert "shorter" in result.output
    assert "not yet told to the resident" in result.output
    assert "a@example.com" in result.output

    as_json = runner.invoke(
        main, ["approval", "show", record.request_id, "--db", str(db), "--format", "json"]
    )
    assert json.loads(as_json.output)["decision"] == "edit"


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_approval_show_scrubs_what_the_session_typed(
    runner: CliRunner, tmp_path: Path, output_format: str
) -> None:
    """The audit query is the output most likely to be pasted into an issue (steward #144).

    Both formats: `--format json` is the one more likely to be piped somewhere, not less.
    """
    db = tmp_path / "approvals.db"
    with Store(db) as store:
        record = store.create_approval_request(
            agent_id="claude-code:test-agent",
            project="p",
            action="rotate_token",
            message="need ghp_abcdefghijklmnopqrstuvwxyz0123456789 to rotate",
            detail={"to": "a@example.com", "auth": "Bearer ghp_zyxwvutsrqponmlkjihgfe98765"},
        )
        store.decide(
            record.request_id,
            "edit",
            decided_by="api",
            edit={"note": "use sk-ant-abcdef0123456789ghij"},
        )

    result = runner.invoke(
        main,
        ["approval", "show", record.request_id, "--db", str(db), "--format", output_format],
    )

    assert result.exit_code == 0, result.output
    assert "ghp_" not in result.output
    assert "sk-ant-" not in result.output
    assert "[redacted:secret]" in result.output
    # Only the secret is cut: the action still reads as itself and the address survives.
    assert "rotate_token" in result.output
    assert "a@example.com" in result.output


def test_approval_show_says_when_it_has_never_heard_of_a_request(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(main, ["approval", "show", "nope", "--db", str(tmp_path / "a.db")])
    assert result.exit_code == 1
    assert "no approval request 'nope'" in result.output


def test_a_scheduler_tick_sweeps_the_board_too(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The board is swept on the scheduler's own rhythm, not on a second timer."""
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Picked up by a tick")

    result = runner.invoke(
        main,
        [
            "scheduler", "tick",
            "--residents", str(residents_dir),
            "--state", str(tmp_path / "state.json"),
            "--db", str(db),
            "--workdir", str(tmp_path),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as after:
        assert [job.status for job in after.jobs()] == ["done"]


def test_a_dry_run_tick_touches_no_database(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Not tonight")

    result = runner.invoke(
        main,
        [
            "scheduler", "tick", "--dry-run",
            "--residents", str(residents_dir),
            "--state", str(tmp_path / "state.json"),
            "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as after:
        assert [job.status for job in after.jobs()] == ["open"]


# ------------------------------------------------------- budgets and the watchdog (#8)


def budgeted_manifest(**budgets: object) -> dict[str, Any]:
    """Build a manifest with a mock runner and the budgets a test wants to try."""
    data = valid_manifest()
    data["runner"] = {"kind": "mock", "model": "pretend"}
    if budgets:
        data["budgets"] = dict(budgets)
    return data


def ledger_a_run(db: Path, cost: float, *, resident: str = "test-agent") -> None:
    """Put one finished run on the ledger, as a scheduler would."""
    with Store(db) as store:
        store.record_run(
            resident=resident,
            agent_id="claude-code:test-agent",
            kind="routine",
            trigger="schedule",
            run_id="already-ran",
            ref="daily-summary",
            origin=f"resident:{resident}",
            cost_usd=cost,
            input_tokens=50,
            output_tokens=50,
        )


def test_budget_show_prints_the_gauges_and_the_window(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=5.0)).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 1.5)

    result = runner.invoke(
        main, ["budget", "show", "--residents", str(residents_dir), "--db", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert "daily_cost_usd: 1.50 of 5" in result.output
    assert "daily_tokens: 100 spent, no limit" in result.output
    assert "1 run(s)" in result.output


def test_budget_show_says_no_limit_out_loud(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        ["budget", "show", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code == 0
    assert "test-agent: no limit" in result.output


def test_budget_show_reports_json(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest(daily_tokens=1000)).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 0.0)
    result = runner.invoke(
        main,
        [
            "budget", "show", "test-agent",
            "--residents", str(residents_dir), "--db", str(db), "--format", "json",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["resident"] == "test-agent"
    assert payload[0]["spent"]["tokens"] == 100
    assert payload[0]["window"]["day"]


def test_budget_show_by_origin_rolls_a_chain_up_to_the_question_that_started_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The other half of the ledger: not who spent it, but what it was spent answering."""
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=50.0)).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.record_run(
            resident="test-agent",
            agent_id="claude-code:test-agent",
            kind="delegated",
            run_id="a-letter",
            ref="a-letter",
            origin="task:root",
            cost_usd=3.25,
            input_tokens=40,
            output_tokens=60,
        )

    result = runner.invoke(
        main,
        [
            "budget", "show", "test-agent", "--by-origin",
            "--residents", str(residents_dir), "--db", str(db),
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "by origin" in result.output
    assert "task:root: $3.2500, 100 token(s), 1 run(s)" in result.output


def test_budget_show_by_origin_says_so_when_the_ledger_is_empty(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """An empty rollup is an answer, not a blank space where a number should be."""
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "budget", "show", "--by-origin",
            "--residents", str(residents_dir), "--db", str(tmp_path / "steward.db"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "nothing on the ledger in this window" in result.output


def test_budget_show_by_origin_wraps_the_json_only_when_it_is_asked_for(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The bare list is the shape something is already parsing; the flag is what wraps it."""
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=5.0)).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 1.0)
    result = runner.invoke(
        main,
        [
            "budget", "show", "--by-origin",
            "--residents", str(residents_dir), "--db", str(db), "--format", "json",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["residents"][0]["resident"] == "test-agent"
    # A routine came off no task, so it rolls up under the resident whose day it was.
    assert payload["by_origin"] == [
        {
            "origin": "resident:test-agent",
            "runs": 1,
            "cost_usd": 1.0,
            "tokens": 100,
            "duration_s": 0.0,
        }
    ]


def test_budget_show_refuses_an_unknown_resident(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "budget",
            "show",
            "nobody",
            "--residents",
            str(residents_dir),
            "--db",
            str(tmp_path / "s"),
        ],
    )
    assert result.exit_code == 1
    assert "no valid resident 'nobody'" in result.output


def test_budget_show_names_the_gap_when_a_brain_reported_nothing(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=5.0)).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.record_run(
            resident="test-agent",
            agent_id="claude-code:test-agent",
            kind="routine",
            trigger="schedule",
            run_id="quiet",
            usage_known=False,
        )
    result = runner.invoke(
        main, ["budget", "show", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert "did not report what they cost" in result.output


def test_budget_unpause_lifts_a_pause_and_says_what_it_was(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=1.0)).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 4.0)
    # Trip the budget through the same path a scheduled fire would.
    with Store(db) as store:
        resident = load_manifest(residents_dir / "test-agent" / "manifest.yaml")
        BudgetGuard(store).allow(resident.manifest)

    result = runner.invoke(
        main,
        [
            "budget", "unpause", "test-agent", "--residents", str(residents_dir),
            "--db", str(db),
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "test-agent resumed" in result.output
    assert "daily_cost_usd" in result.output
    with Store(db) as store:
        assert store.budget_pause("test-agent") is None
        assert store.approvals()[0].decision == "approve"


def test_budget_unpause_on_a_running_resident_is_a_successful_no_op(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "budget",
            "unpause",
            "test-agent",
            "--residents",
            str(residents_dir),
            "--db",
            str(tmp_path / "steward.db"),
        ],
    )
    assert result.exit_code == 0
    assert "is not paused by a budget" in result.output


def test_budget_unpause_refuses_an_unknown_resident(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "budget",
            "unpause",
            "typo",
            "--residents",
            str(residents_dir),
            "--db",
            str(tmp_path / "steward.db"),
        ],
    )
    assert result.exit_code == 1
    assert "no valid resident 'typo'" in result.output


def test_watchdog_tick_reports_a_quiet_pass(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A watchdog with nothing to do says so, and names what it could not see."""
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        ["watchdog", "tick", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code == 0, result.output
    # Nothing can actually see this resident's process today — no container is declared,
    # and steward's own state only ever proves stuckness — so it says so rather than
    # printing a green tick it has not earned.
    assert "test-agent: unsupervised" in result.output
    assert "nothing to intervene in" in result.output


def test_watchdog_tick_closes_a_run_that_never_reported_back(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path, monkeypatch
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(log))
    log.write_text(
        json.dumps(
            {
                "v": 0,
                "ts": "2020-01-01T00:00:00.000Z",
                "source": "steward",
                "agent_id": "claude-code:test-agent",
                "project": "test-agent",
                "type": "routine_started",
                "payload": {"routine": "daily-summary", "run_id": "gone"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        main,
        ["watchdog", "tick", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )

    assert result.exit_code == 1, result.output
    assert "closed run gone" in result.output
    emitted = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [e["type"] for e in emitted if e["payload"].get("run_id") == "gone"] == [
        "routine_started",
        "routine_failed",
    ]


def test_watchdog_tick_reports_json(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "watchdog", "tick", "--residents", str(residents_dir),
            "--db", str(tmp_path / "s.db"), "--format", "json",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["interventions"] == 0
    assert payload["health"][0]["resident"] == "test-agent"


def test_watchdog_run_makes_the_passes_it_was_asked_for(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    db = tmp_path / "s.db"
    result = runner.invoke(
        main,
        [
            "watchdog", "run", "--residents", str(residents_dir),
            "--db", str(db), "--interval", "0", "--max-passes", "2",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as store:
        last = store.last_watchdog_pass()
    assert last is not None
    assert last["passes"] == 2


@pytest.mark.parametrize("command", [("tick",), ("run", "--max-passes", "1")])
@pytest.mark.parametrize(("failure", "expected"), [(None, 0), ("gave_up", 1), ("paused", 1)])
def test_watchdog_commands_carry_pass_outcomes(  # noqa: PLR0913, PLR0917
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    failure: str | None,
    expected: int,
) -> None:
    health = SimpleNamespace(resident_id="test-agent", detail="could not restart")
    report = SimpleNamespace(
        gave_up=(health,) if failure == "gave_up" else (),
        paused=("test-agent",) if failure == "paused" else (),
        restarted=(),
        buried=(),
        reopened=(),
        expired_approvals=(),
        health=(),
        __bool__=lambda: failure is not None,
    )
    # `topology` because the CLI asks the watchdog where it is before it asks it to work
    # (#59); an empty survey is the honest answer for a double with no residents.
    dog = SimpleNamespace(
        tick=lambda: report,
        run=lambda **_kwargs: [report],
        topology=lambda: cli.survey([]),
    )
    monkeypatch.setattr(cli.Watchdog, "from_path", lambda *_args, **_kwargs: dog)

    result = runner.invoke(
        main,
        ["watchdog", *command, "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")],
    )

    assert result.exit_code == expected, result.output


def test_watchdog_daemon_interrupt_is_a_clean_operator_stop(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(**_kwargs: object) -> list[object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        cli.Watchdog,
        "from_path",
        lambda *_args, **_kwargs: SimpleNamespace(run=interrupted, topology=lambda: cli.survey([])),
    )
    result = runner.invoke(
        main,
        ["watchdog", "run", "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


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


# ------------------------------------------------------------- supervision topology (#59)


def supervised_manifest(**deploy: object) -> dict[str, Any]:
    """Build a manifest that names a container, so there is something to supervise."""
    data = valid_manifest()
    data["runner"] = {"kind": "claude"}
    data["deploy"] = {"container": "steward-test-agent", **deploy}
    return data


def docker_naming_itself(name: str) -> str:
    """Return a `docker` stub that answers `info` with a name and refuses everything else.

    Only `info`, deliberately: a stub that answered every subcommand the same way would
    have `docker inspect --format {{.State.Running}}` print a daemon name, which the
    watchdog reads as "not running" and dutifully restarts — turning a topology test into
    a restart test.
    """
    return f"case \"$1\" in info) printf '{name}\\t27.3.1\\n' ;; *) exit 1 ;; esac"


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


def test_the_watchdog_does_not_supervise_another_burrows_containers(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The deploy.host partition keeps another burrow out of every watchdog pass."""
    stub_bin("docker", docker_naming_itself("laptop"))
    monkeypatch.setenv("STEWARD_BURROW", "laptop")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(
        main,
        ["watchdog", "tick", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )

    assert result.exit_code == 0, result.output
    assert "nothing here needs docker" in result.output
    assert "steward-test-agent" not in result.output


def test_the_watchdog_refuses_an_absent_local_container_once_per_pass(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stub_bin(
        "docker",
        'case "$1" in info) printf "dxp2800\\t27.3.1\\n" ;; '
        'inspect) echo "No such container: steward-test-agent" >&2; exit 1 ;; esac',
    )
    monkeypatch.setenv("STEWARD_BURROW", "dxp2800")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(
        main,
        ["watchdog", "tick", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )

    refusal = "test-agent: refused: declared container 'steward-test-agent' is absent"
    assert result.exit_code == 0, result.output
    assert result.output.count(refusal) == 1
    assert "unsupervised" not in result.output


def test_the_watchdog_json_report_excludes_another_burrows_residents(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The machine report contains only the residents this daemon is responsible for."""
    stub_bin("docker", docker_naming_itself("laptop"))
    monkeypatch.setenv("STEWARD_BURROW", "laptop")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(
        main,
        [
            "watchdog", "tick", "--residents", str(residents_dir),
            "--db", str(tmp_path / "s.db"), "--format", "json",
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["interventions"] == 0
    assert document["health"] == []
    assert "topology: docker at" not in result.output, "a green line would corrupt the JSON"


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


# --------------------------------------------------------------------------------------
# delegation
# --------------------------------------------------------------------------------------

RECEIVER_SOUL = """---
agent_id: claude-code:receiver-agent
name: Recy
char: Monk
accent: "#a68a4f"
role: test bot
---
A villager that exists only inside a test.
"""


def delegation_fleet(write_resident: ResidentWriter) -> Path:
    """Write a permitted sender and a declared receiver, and return the tree."""
    sender = valid_manifest()
    sender["delegation"] = {"send": True}
    residents_dir = write_resident(sender).parent.parent

    receiver = valid_manifest()
    receiver["uid"] = SECOND_RESIDENT_UID
    receiver["id"] = "receiver-agent"
    receiver["agent_id"] = "claude-code:receiver-agent"
    receiver["home"] = 1
    receiver["soul"]["name"] = "Recy"
    receiver["routes"] = [
        *receiver["routes"],
        {"id": "inbox", "kind": "delegation", "address": "steward:delegation"},
    ]
    write_resident(receiver, soul=RECEIVER_SOUL, root=residents_dir)
    return residents_dir


def test_delegate_hands_work_over_and_prints_the_task_id(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token-free path: a session with a shell, not with steward's API token."""
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("CHRONICLE_URL", raising=False)
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"

    result = runner.invoke(
        main,
        [
            "delegate", "test-agent",
            "--to", "receiver-agent",
            "--route", "inbox",
            "--title", "Read the background",
            "--detail", "everything they need",
            "--residents", str(residents_dir),
            "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "test-agent → receiver-agent via inbox" in result.output
    task_id = result.output.strip().splitlines()[-1]

    with Store(db) as store:
        (waiting,) = store.inbox("receiver-agent")
    assert waiting.task_id == task_id
    assert waiting.detail == "everything they need"

    emitted = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [event["type"] for event in emitted] == ["task_delegated"]


def test_delegate_accepts_a_json_detail(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    result = runner.invoke(
        main,
        [
            "delegate", "test-agent",
            "--to", "receiver-agent", "--route", "inbox", "--title", "Read it",
            "--detail-json", '{"question": "what is on the list?"}',
            "--residents", str(residents_dir), "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as store:
        (waiting,) = store.inbox("receiver-agent")
    assert json.loads(waiting.detail) == {"question": "what is on the list?"}


def test_delegate_refuses_loudly_and_writes_nothing(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    residents_dir = write_resident(valid_manifest()).parent.parent
    db = tmp_path / "delegation.db"
    result = runner.invoke(
        main,
        [
            "delegate", "test-agent",
            "--to", "nobody", "--route", "inbox", "--title", "Read it",
            "--residents", str(residents_dir), "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "refused (unknown_recipient)" in result.output
    with Store(db) as store:
        assert store.jobs() == []
    assert not (tmp_path / "events.jsonl").exists(), "a refusal emits nothing"


def test_delegate_refuses_two_kinds_of_detail(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = delegation_fleet(write_resident)
    both = runner.invoke(
        main,
        [
            "delegate", "test-agent", "--to", "receiver-agent", "--route", "inbox",
            "--title", "t", "--detail", "a", "--detail-json", "{}",
            "--residents", str(residents_dir), "--db", str(tmp_path / "d.db"),
        ],
    )  # fmt: skip
    assert both.exit_code == 1
    assert "not both" in both.output

    broken = runner.invoke(
        main,
        [
            "delegate", "test-agent", "--to", "receiver-agent", "--route", "inbox",
            "--title", "t", "--detail-json", "{oops",
            "--residents", str(residents_dir), "--db", str(tmp_path / "d.db"),
        ],
    )  # fmt: skip
    assert broken.exit_code == 1
    assert "--detail-json does not parse" in broken.output


def test_delegate_needs_a_sender_that_exists(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = delegation_fleet(write_resident)
    result = runner.invoke(
        main,
        [
            "delegate", "ghost", "--to", "receiver-agent", "--route", "inbox", "--title", "t",
            "--residents", str(residents_dir), "--db", str(tmp_path / "d.db"),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "no valid resident 'ghost'" in result.output


def test_inbox_shows_what_is_waiting_and_what_is_not(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    empty = runner.invoke(
        main, ["inbox", "receiver-agent", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert empty.exit_code == 0, empty.output
    assert "nothing open in receiver-agent's inbox" in empty.output
    assert "routes accepting delegated work: inbox" in empty.output

    with Store(db) as store:
        store.delegate_job(
            title="Read the background",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
        )
    listed = runner.invoke(
        main, ["inbox", "receiver-agent", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert "Read the background" in listed.output
    assert "from test-agent via inbox" in listed.output

    payload = json.loads(
        runner.invoke(
            main,
            [
                "inbox", "receiver-agent", "--status", "all", "--format", "json",
                "--residents", str(residents_dir), "--db", str(db),
            ],
        ).output
    )  # fmt: skip
    assert payload[0]["assignee"] == "receiver-agent"


def test_task_lineage_prints_the_whole_chain(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    with Store(db) as store:
        root = store.post_job(title="The root task")
        child = store.delegate_job(
            title="The handed-over half",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
            parent_task_id=root.task_id,
            origin=f"task:{root.task_id}",
        )
    _ = residents_dir

    result = runner.invoke(main, ["task", "lineage", child.task_id, "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert f"origin task:{root.task_id}" in result.output
    assert "The root task" in result.output
    assert "test-agent → receiver-agent" in result.output

    payload = json.loads(
        runner.invoke(
            main, ["task", "lineage", child.task_id, "--db", str(db), "--format", "json"]
        ).output
    )
    assert [item["task_id"] for item in payload] == [root.task_id, child.task_id]

    missing = runner.invoke(main, ["task", "lineage", "nobody", "--db", str(db)])
    assert missing.exit_code == 1


def test_task_lineage_from_the_root_still_shows_the_descendants(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The root is the only id POST /delegate hands back, so it must answer too (#202)."""
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    with Store(db) as store:
        root = store.post_job(title="The root task")
        child = store.delegate_job(
            title="The handed-over half",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
            parent_task_id=root.task_id,
            origin=f"task:{root.task_id}",
        )
    _ = residents_dir

    from_root = runner.invoke(main, ["task", "lineage", root.task_id, "--db", str(db)])
    assert from_root.exit_code == 0, from_root.output
    assert "The handed-over half" in from_root.output
    assert "test-agent → receiver-agent" in from_root.output

    def ids(task_id: str) -> list[str]:
        raw = runner.invoke(
            main, ["task", "lineage", task_id, "--db", str(db), "--format", "json"]
        ).output
        return [item["task_id"] for item in json.loads(raw)]

    assert ids(root.task_id) == [root.task_id, child.task_id]
    assert ids(child.task_id) == ids(root.task_id)


def test_board_list_marks_a_letter_as_a_letter(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A delegated item is on the same table, and the listing must not call it claimable."""
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    with Store(db) as store:
        store.delegate_job(
            title="Read the background",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
        )
    result = runner.invoke(
        main, ["board", "list", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "delegated: test-agent → receiver-agent via inbox" in result.output
    assert "claimable by" not in result.output


def test_board_dispatch_reports_a_letter_it_worked(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path, stub_bin: StubWriter
) -> None:
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    with Store(db) as store:
        store.delegate_job(
            title="Read the background",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
        )
    stub_bin("claude", 'echo \'{"result": "read it", "is_error": false}\'')

    result = runner.invoke(
        main,
        ["board", "dispatch", "--residents", str(residents_dir), "--db", str(db),
         "--workdir", str(tmp_path)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "done receiver-agent" in result.output
    assert "delegated by test-agent" in result.output


# ======================================================================================
# the nursery: `steward new-resident` and `steward retire`
# ======================================================================================

CHARTER_YAML = """mission: Keep the village's notes in order.
duties:
  - Tidy the notes each evening.
rules:
  - Never delete a note without asking.
escalation: Raise needs_human before anything irreversible.
"""


@pytest.fixture
def charter_file(tmp_path: Path) -> Path:
    path = tmp_path / "charter.yaml"
    path.write_text(CHARTER_YAML, encoding="utf-8")
    return path


@pytest.fixture
def nas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalTransport:
    """Point the CLI's default transport at a directory instead of the real NAS.

    ``transport_for`` is the documented seam: the pipeline builds the ssh transport its
    manifest addresses unless somebody hands it one. A CLI test hands it one here rather
    than growing a flag nobody would ever use in production.
    """
    host = LocalTransport(root=tmp_path / "nas")
    monkeypatch.setattr("steward.nursery.transport_for", lambda _target, _env=None: host)
    monkeypatch.setenv("CHRONICLE_URL", "http://dxp2800:8737")
    monkeypatch.setenv("CHRONICLE_TOKEN", "cli-village-token")
    monkeypatch.setenv("STEWARD_URL", "http://dxp2800:8802")
    return host


def new_resident_argv(repo: ScratchRepo, charter: Path, *extra: str) -> list[str]:
    """Build the full command line, so each test varies only what it is about."""
    return [
        "new-resident",
        "--id",
        "note-keeper",
        "--name",
        "Quill",
        "--char",
        "Scribe",
        "--accent",
        "#4f7ea6",
        "--role",
        "note bot",
        "--charter",
        str(charter),
        "--residents",
        str(repo.residents),
        "--repo",
        str(repo.root),
        *extra,
    ]


def test_the_charter_example_is_a_charter_steward_accepts(
    runner: CliRunner, scratch_repo: ScratchRepo, tmp_path: Path, nas: LocalTransport
) -> None:
    """The example a refusal prints is the only spec `--charter` has (warren#90).

    An operator meets it at the moment they got the format wrong, so copying it has to
    produce a charter the validator takes. An example that drifted would document the
    file format wrongly, which is worse than not documenting it at all.
    """
    charter = tmp_path / "from-the-example.yaml"
    charter.write_text(cli.CHARTER_EXAMPLE, encoding="utf-8")

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert not nas.touched


def test_the_readme_carries_the_cli_s_charter_example_verbatim() -> None:
    """The README's charter block is a copy, and a copy is a thing that drifts.

    Both are the documentation of `--charter`, so they have to be the same bytes: the
    test above proves one of them works, and this is what makes that cover the other.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert cli.CHARTER_EXAMPLE in readme, "README's charter block has drifted from the CLI's"


def test_new_resident_raises_a_resident_end_to_end(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 0, result.output
    assert "note-keeper is raised" in result.output
    assert (scratch_repo.residents / "note-keeper" / "soul.md").is_file()
    assert scratch_repo.log()[0] == "feat(residents): declare note-keeper"
    assert (
        nas.root / "docker" / "warren" / "residents" / "note-keeper" / "docker-compose.yaml"
    ).is_file()


@pytest.mark.usefixtures("nas")
def test_new_resident_is_a_no_op_the_second_time(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))
    commits = scratch_repo.log()

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 0, result.output
    assert "converged" in result.output
    assert scratch_repo.log() == commits


def test_new_resident_dry_run_prints_the_plan_and_changes_nothing(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert "plan for note-keeper" in result.output
    assert "docker compose" in result.output
    assert "nothing was written, sent, or committed" in result.output
    assert not (scratch_repo.residents / "note-keeper").exists()
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_new_resident_reports_json_when_asked(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--format", "json"))

    payload = json.loads(result.output)
    assert payload["resident"] == "note-keeper"
    assert payload["provision"]["target"]["container"] == "steward-note-keeper"
    assert "cli-village-token" not in result.output


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_new_resident_register_problems_exit_non_zero(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    report = SimpleNamespace(
        register=SimpleNamespace(problems=("claude is not on PATH",)),
        dry_run=False,
        changed=True,
        resident_id="note-keeper",
        render=lambda: ["raised note-keeper", "register", "  claude is not on PATH"],
        to_dict=lambda: {
            "resident": "note-keeper",
            "register": {"ok": False, "problems": ["claude is not on PATH"]},
        },
    )
    monkeypatch.setattr(cli, "raise_resident", lambda *_args, **_kwargs: report)

    result = runner.invoke(
        main,
        new_resident_argv(
            scratch_repo, charter_file, "--format", output_format, "--no-deploy", "--no-commit"
        ),
    )

    assert result.exit_code == 1, result.output


def test_new_resident_can_skip_the_container_entirely(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))

    assert result.exit_code == 0, result.output
    assert not nas.touched
    assert scratch_repo.log()[0] == "feat(residents): declare note-keeper"


@pytest.mark.usefixtures("nas")
def test_new_resident_can_skip_the_commit(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-commit"))

    assert result.exit_code == 0, result.output
    assert scratch_repo.log() == ["chore: scratch repo"]
    assert (scratch_repo.residents / "note-keeper" / "manifest.yaml").is_file()


def test_new_resident_refuses_a_dirty_worktree(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    (scratch_repo.root / "scratch.txt").write_text("mid-thought\n", encoding="utf-8")

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 1
    assert "uncommitted changes" in result.output
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_new_resident_needs_a_charter(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(
        main,
        [
            "new-resident",
            "--id",
            "note-keeper",
            "--name",
            "Quill",
            "--char",
            "Scribe",
            "--accent",
            "#4f7ea6",
            "--role",
            "note bot",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
        ],
    )

    assert result.exit_code == 1
    assert "--charter is required" in result.output


@pytest.mark.usefixtures("nas")
def test_a_charter_that_is_not_a_charter_says_what_one_looks_like(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    tmp_path: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    charter = tmp_path / "charter.yaml"
    charter.write_text("- just a list\n", encoding="utf-8")

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter))

    assert result.exit_code == 1
    assert "mission" in result.output


@pytest.mark.usefixtures("nas")
def test_a_charter_that_is_not_yaml_at_all_is_named(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    tmp_path: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    charter = tmp_path / "charter.yaml"
    charter.write_text("mission: [unclosed\n", encoding="utf-8")

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter))

    assert result.exit_code == 1
    assert "cannot read the charter" in result.output


@pytest.mark.usefixtures("nas")
def test_a_spec_that_cannot_bind_to_the_schema_names_the_field(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(
        main, new_resident_argv(scratch_repo, charter_file, "--accent", "not-a-colour")
    )

    assert result.exit_code == 1
    assert "accent" in result.output


# ------------------------------------- `steward provision`: the manifest is the source


def hand_write_manifest(repo: ScratchRepo, resident_id: str = "note-keeper") -> Path:
    """Give a declared resident an app grant no `new-resident` flag can say, and commit it."""
    path = repo.residents / resident_id / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["app_grants"] = [{"id": "gmail", "name": "Gmail", "status": "granted"}]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    repo.git("commit", "-am", f"feat(residents): grant {resident_id} Gmail")
    return path


def provision_argv(repo: ScratchRepo, *extra: str) -> list[str]:
    """Build the provision command line, so each test varies only what it is about."""
    return [
        "provision",
        "note-keeper",
        "--residents",
        str(repo.residents),
        "--repo",
        str(repo.root),
        *extra,
    ]


def test_provision_builds_a_manifest_new_resident_would_refuse(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    """The command #270 asked for: the declaration is the source of truth, not the flags."""
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    hand_write_manifest(scratch_repo)
    assert runner.invoke(main, new_resident_argv(scratch_repo, charter_file)).exit_code == 1

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 0, result.output
    assert "note-keeper is provisioned" in result.output
    assert (
        nas.root / "docker" / "warren" / "residents" / "note-keeper" / "docker-compose.yaml"
    ).is_file()


def test_the_refusal_new_resident_gives_names_the_command_that_works(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    hand_write_manifest(scratch_repo)

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 1
    assert "steward provision note-keeper" in result.output
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_provision_commits_nothing_and_does_not_mind_a_dirty_worktree(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    """There is no commit to protect, so there is no dirty-worktree refusal to make."""
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    commits = scratch_repo.log()
    (scratch_repo.root / "scratch.txt").write_text("mid-thought\n", encoding="utf-8")

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 0, result.output
    assert scratch_repo.log() == commits


@pytest.mark.usefixtures("nas")
def test_provision_says_out_loud_when_it_is_building_uncommitted_bytes(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    path = scratch_repo.residents / "note-keeper" / "manifest.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "summary: uncommitted\n", encoding="utf-8")

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 0, result.output
    assert "is not committed" in result.output


def test_provision_dry_run_prints_the_plan_and_touches_nothing(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))

    result = runner.invoke(main, provision_argv(scratch_repo, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert "plan for note-keeper" in result.output
    assert "docker compose" in result.output
    assert "nothing was written, sent, or committed" in result.output
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_provision_reports_json_when_asked(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))

    result = runner.invoke(main, provision_argv(scratch_repo, "--format", "json"))

    payload = json.loads(result.output)
    assert payload["act"] == "provision"
    assert payload["declare"]["written"] is False
    assert payload["provision"]["target"]["container"] == "steward-note-keeper"
    assert "cli-village-token" not in result.output


@pytest.mark.usefixtures("nas")
def test_provision_is_a_no_op_the_second_time(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 0, result.output
    assert "converged" in result.output


@pytest.mark.usefixtures("nas")
def test_provisioning_an_unknown_resident_suggests_the_one_you_meant(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))

    result = runner.invoke(
        main,
        ["provision", "note-keper", "--residents", str(scratch_repo.residents)],
    )

    assert result.exit_code == 1
    assert "did you mean 'note-keeper'" in result.output


def test_provisioning_a_retired_resident_is_refused(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
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
    nas.calls.clear()

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 1
    assert "is retired" in result.output
    assert not nas.calls


def test_provisioning_with_nowhere_to_emit_is_one_line_not_a_traceback(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    monkeypatch.delenv("CHRONICLE_URL", raising=False)

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 1
    assert "could not provision note-keeper" in result.output
    assert "CHRONICLE_URL" in result.output
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_provisioning_a_broken_manifest_prints_the_diagnostics(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    """The field-by-field diagnostics, not just "it does not validate"."""
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    path = scratch_repo.residents / "note-keeper" / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["soul"]["accent"] = "not-a-colour"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 1
    assert "does not validate" in result.output
    assert "accent" in result.output


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_provision_register_problems_exit_non_zero(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    """The container is up and the schedule is not: a zero exit would say only the first."""
    report = SimpleNamespace(
        register=SimpleNamespace(problems=("claude is not on PATH",)),
        dry_run=False,
        changed=True,
        resident_id="note-keeper",
        verb="provisioned",
        render=lambda: ["provisioned note-keeper", "register", "  claude is not on PATH"],
        to_dict=lambda: {
            "resident": "note-keeper",
            "register": {"ok": False, "problems": ["claude is not on PATH"]},
        },
    )
    monkeypatch.setattr(cli, "provision_resident", lambda *_args, **_kwargs: report)

    result = runner.invoke(main, provision_argv(scratch_repo, "--format", output_format))

    assert result.exit_code == 1, result.output


def test_retire_stops_the_container_and_commits_the_decision(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    result = runner.invoke(
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

    assert result.exit_code == 0, result.output
    assert "note-keeper is retired" in result.output
    assert "resident_retired was emitted under the resident's identity" in result.output
    assert scratch_repo.log()[0] == "chore(residents): retire note-keeper"
    assert nas.calls[-2][-2:] == ("down", "--remove-orphans")
    # …and the token goes with it, once the container that was reading it is gone (#157).
    assert nas.calls[-1][:2] == ("rm", "-f")
    assert "claude/" in result.output


def test_retire_no_deploy_marks_and_commits_but_reaches_no_host(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    """--no-deploy is the host-less path: the resident stops, but no ssh is run (#90)."""
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))
    nas.calls.clear()

    result = runner.invoke(
        main,
        [
            "retire",
            "note-keeper",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
            "--no-deploy",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "note-keeper is retired" in result.output
    assert nas.calls == [], "no host was reached"
    assert scratch_repo.log()[0] == "chore(residents): retire note-keeper"


def test_new_resident_reports_a_transport_failure_cleanly(
    runner: CliRunner, tmp_path: Path, charter_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that will not answer is an operator problem, not a traceback (#90)."""

    def boom(*_args: object, **_kwargs: object) -> object:
        raise TransportError("no route to dxp2800")

    monkeypatch.setattr("steward.cli.raise_resident", boom)
    residents_dir = tmp_path / "residents"
    residents_dir.mkdir()

    result = runner.invoke(
        main,
        [
            "new-resident",
            "--id",
            "note-keeper",
            "--name",
            "Quill",
            "--char",
            "Scribe",
            "--accent",
            "#4f7ea6",
            "--role",
            "note bot",
            "--charter",
            str(charter_file),
            "--residents",
            str(residents_dir),
        ],
    )
    assert result.exit_code == 1
    assert "could not reach the host" in result.output
    assert "Traceback" not in result.output


def test_retire_dry_run_changes_nothing(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))
    before = scratch_repo.head()
    nas.calls.clear()

    result = runner.invoke(
        main,
        [
            "retire",
            "note-keeper",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "nothing was stopped, marked, or committed" in result.output
    assert scratch_repo.head() == before
    assert nas.calls == []


@pytest.mark.usefixtures("nas")
def test_retire_reports_json_when_asked(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    result = runner.invoke(
        main,
        [
            "retire",
            "note-keeper",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
            "--format",
            "json",
        ],
    )

    payload = json.loads(result.output)
    assert payload["marked"] is True
    assert payload["stopped"] is True


@pytest.mark.usefixtures("nas")
def test_retiring_a_resident_nobody_declared_exits_non_zero(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(
        main,
        [
            "retire",
            "ghost",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
        ],
    )

    assert result.exit_code == 1
    assert "no resident 'ghost'" in result.output


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


# --------------------------------------------------------------------------------------
# operator credentials (warren#225)
# --------------------------------------------------------------------------------------


def test_minting_prints_the_credential_once_and_stores_only_its_digest(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The terminal is the only place the plaintext ever exists on steward's side."""
    db = tmp_path / "steward.db"

    result = runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])

    assert result.exit_code == 0
    credential = result.output.strip().splitlines()[-1]
    assert credential.startswith(OPERATOR_CREDENTIAL_PREFIX)
    with Store(db) as store:
        assert store.operator_principal(credential) is not None
        assert credential not in db.read_bytes().decode("utf-8", "replace")


def test_a_minted_operator_gets_a_git_author_address_derived_from_their_name(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Git wants an address, and a blank one produces an unparseable author line."""
    db = tmp_path / "steward.db"

    runner.invoke(main, ["operator", "mint", "Miha Zelnik", "--db", str(db)])

    with Store(db) as store:
        assert store.operators()[0].email == "miha-zelnik@steward-operator.localhost"


def test_an_explicit_email_is_what_the_commits_carry(runner: CliRunner, tmp_path: Path) -> None:
    """A real address is better than a derived one, so the flag wins when it is given."""
    db = tmp_path / "steward.db"

    runner.invoke(
        main, ["operator", "mint", "Miha", "--email", "miha@example.invalid", "--db", str(db)]
    )

    with Store(db) as store:
        assert store.operators()[0].email == "miha@example.invalid"


def test_minting_over_a_live_credential_is_refused_and_says_how_to_rotate(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Silently rotating would leave the old holder unable to tell it had stopped working."""
    db = tmp_path / "steward.db"
    runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])

    result = runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])

    assert result.exit_code == 1
    assert "already holds a live credential" in result.output
    assert "steward operator revoke" in result.output


def test_revoking_stops_the_credential_and_keeps_the_row(runner: CliRunner, tmp_path: Path) -> None:
    """A deleted row cannot answer "who could act as this fleet's operator, and until when"."""
    db = tmp_path / "steward.db"
    minted = runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])
    credential = minted.output.strip().splitlines()[-1]

    result = runner.invoke(main, ["operator", "revoke", "Miha", "--db", str(db)])

    assert result.exit_code == 0
    assert "revoked Miha's credential" in result.output
    with Store(db) as store:
        assert store.operator_principal(credential) is None
        assert [record.live for record in store.operators()] == [False]


def test_revoking_a_name_that_holds_nothing_says_so_rather_than_failing(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Nothing was revoked, and nothing pretends otherwise."""
    result = runner.invoke(main, ["operator", "revoke", "Nobody", "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0
    assert "no live operator credential" in result.output


def test_listing_shows_revoked_credentials_rather_than_hiding_them(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The audit question is about credentials that *used* to work, so they are listed."""
    db = tmp_path / "steward.db"
    runner.invoke(main, ["operator", "mint", "Miha", "--note", "townhall", "--db", str(db)])
    runner.invoke(main, ["operator", "mint", "Ana", "--db", str(db)])
    runner.invoke(main, ["operator", "revoke", "Ana", "--db", str(db)])

    result = runner.invoke(main, ["operator", "list", "--db", str(db)])

    assert "live  Miha" in result.output
    assert "townhall" in result.output
    assert "gone  Ana" in result.output


def test_listing_an_empty_table_names_what_that_means(runner: CliRunner, tmp_path: Path) -> None:
    """No credentials is not an error: it means every human is presenting the master token."""
    result = runner.invoke(main, ["operator", "list", "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0
    assert "every human caller is presenting STEWARD_TOKEN" in result.output


def test_listing_as_json_never_carries_a_plaintext_credential(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The machine-readable view is the one most likely to be piped somewhere it should not."""
    db = tmp_path / "steward.db"
    minted = runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])
    credential = minted.output.strip().splitlines()[-1]

    result = runner.invoke(main, ["operator", "list", "--db", str(db), "--format", "json"])

    payload = json.loads(result.output)
    assert payload[0]["name"] == "Miha"
    assert credential not in result.output


def test_an_operator_needs_a_name_to_be_committed_as(runner: CliRunner, tmp_path: Path) -> None:
    """A nameless operator credential would be the master token with extra steps."""
    result = runner.invoke(main, ["operator", "mint", "   ", "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 1
    assert "an operator needs a name" in result.output


# ------------------------------------------------------------- notifications (warren#114)


def tapping_manifest() -> dict[str, Any]:
    data = valid_manifest()
    data["notifications"] = {"transport": "ntfy", "on": ["needs_human"], "note": "Miha's phone"}
    return data


def test_notify_list_prints_the_address_an_operator_has_to_subscribe_to(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    """The derived topic is written down nowhere else, so this command is the setup path."""
    tree = write_resident(tapping_manifest()).parent.parent

    result = runner.invoke(main, ["notify", "list", "--residents", str(tree)])

    assert result.exit_code == 0
    assert "test-agent: ntfy — active" in result.output
    assert "on:      needs_human" in result.output
    assert nf.ntfy_topic(VALID_RESIDENT_UID, "pytest") in result.output
    assert "Miha's phone" in result.output


def test_notify_list_says_plainly_when_a_resident_taps_nobody(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    tree = write_resident().parent.parent
    result = runner.invoke(main, ["notify", "list", "--residents", str(tree)])
    assert result.exit_code == 0
    assert "taps nobody" in result.output


def test_notify_list_marks_a_declaration_that_is_not_live_yet(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    data = tapping_manifest()
    data["notifications"]["status"] = "pending"
    tree = write_resident(data).parent.parent

    result = runner.invoke(main, ["notify", "list", "--residents", str(tree)])

    assert "pending — declared, and silent" in result.output


def test_notify_list_json_is_the_machine_view(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    tree = write_resident(tapping_manifest()).parent.parent

    result = runner.invoke(main, ["notify", "list", "--residents", str(tree), "--format", "json"])

    (row,) = json.loads(result.output)
    assert row["transport"] == "ntfy"
    assert row["enabled"] is True
    assert row["address"].endswith(nf.ntfy_topic(VALID_RESIDENT_UID, "pytest"))


def test_notify_list_over_an_empty_tree_says_so(runner: CliRunner, tmp_path: Path) -> None:
    empty = tmp_path / "residents"
    empty.mkdir()
    result = runner.invoke(main, ["notify", "list", "--residents", str(empty)])
    assert result.exit_code == 0
    assert "no valid residents" in result.output


def test_notify_test_refuses_a_resident_that_never_opted_in(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    """This command proves a declaration; it does not stand in for one."""
    tree = write_resident().parent.parent

    result = runner.invoke(main, ["notify", "test", "test-agent", "--residents", str(tree)])

    assert result.exit_code == 1
    assert "declares no notifications block" in result.output


def test_notify_test_refuses_a_declaration_that_is_not_active(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    data = tapping_manifest()
    data["notifications"]["status"] = "disabled"
    tree = write_resident(data).parent.parent

    result = runner.invoke(main, ["notify", "test", "test-agent", "--residents", str(tree)])

    assert result.exit_code == 1
    assert "status is 'disabled'" in result.output


def test_notify_test_reports_a_transport_it_could_not_reach(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    """The suite points ntfy at a closed loopback port, which is exactly this case."""
    tree = write_resident(tapping_manifest()).parent.parent

    result = runner.invoke(main, ["notify", "test", "test-agent", "--residents", str(tree)])

    assert result.exit_code == 1
    assert "not sent" in result.output


def test_notify_test_says_where_it_landed(
    runner: CliRunner, write_resident: ResidentWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[nf.Tap] = []
    monkeypatch.setattr(
        nf.NtfyTransport, "send", lambda _self, _manifest, tap: bool(sent.append(tap)) or True
    )
    tree = write_resident(tapping_manifest()).parent.parent

    result = runner.invoke(main, ["notify", "test", "test-agent", "--residents", str(tree)])

    assert result.exit_code == 0
    assert "sent —" in result.output
    assert [tap.kind for tap in sent] == ["test"]


# --------------------------------------------------------------------------------------
# steward org (warren#441)
# --------------------------------------------------------------------------------------


ORG_SOUL = """---
agent_id: claude-code:receiver-agent
name: Recy
char: Monk
accent: "#a68a4f"
role: test bot
---
A villager that exists only inside a test.
"""


def org_tree(write_resident: ResidentWriter, tmp_path: Path) -> Path:
    """Build a sender and the receiver its manifest names, in one throwaway tree."""
    sender = valid_manifest()
    sender["delegation"] = {"send": True, "to": ["receiver-agent"]}
    write_resident(sender)
    receiver = valid_manifest()
    receiver["uid"] = SECOND_RESIDENT_UID
    receiver["id"] = "receiver-agent"
    receiver["agent_id"] = "claude-code:receiver-agent"
    receiver["home"] = 1
    receiver["soul"]["name"] = "Recy"
    receiver["budgets"] = {"daily_cost_usd": 5.0}
    receiver["deploy"] = {"mounts": [{"host": "~/Life", "container": "/vault", "mode": "rw"}]}
    receiver["routes"] = [
        *receiver["routes"],
        {"id": "inbox", "kind": "delegation", "address": "steward:delegation", "status": "active"},
    ]
    write_resident(receiver, soul=ORG_SOUL)
    return tmp_path / "residents"


def test_org_prints_the_receiver_indented_under_the_resident_that_may_send_to_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    tree = org_tree(write_resident, tmp_path)

    result = runner.invoke(main, ["org", "--residents", str(tree)])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith("test-agent (")
    receiver = next(line for line in lines if line.strip().startswith("receiver-agent ("))
    assert receiver.startswith("  "), receiver


def test_org_says_no_cap_and_none_rather_than_going_quiet(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Unlimited must not read as unknown, and neither must "touches nothing"."""
    tree = org_tree(write_resident, tmp_path)

    output = runner.invoke(main, ["org", "--residents", str(tree)]).output

    assert "budget: no cap" in output
    assert "mounts: none" in output
    assert "budget: $5/day" in output
    assert "mounts: /vault (rw)" in output


def test_org_names_the_receivers_rather_than_leaving_them_to_the_indentation(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A resident with two managers sits under one row, so the edge has to be said."""
    tree = org_tree(write_resident, tmp_path)

    output = runner.invoke(main, ["org", "--residents", str(tree)]).output

    assert "hands work to: receiver-agent" in output
    assert "hands work to: nobody (no delegation grant)" in output


def test_org_marks_a_receiver_it_would_refuse_rather_than_listing_it_as_a_handoff(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    sender = valid_manifest()
    sender["delegation"] = {"send": True, "to": ["ghost"]}
    write_resident(sender)

    output = runner.invoke(main, ["org", "--residents", str(tmp_path / "residents")]).output

    assert "hands work to: ghost (refused)" in output


def test_org_json_is_the_same_projection_the_api_answers_with(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    tree = org_tree(write_resident, tmp_path)

    result = runner.invoke(main, ["org", "--residents", str(tree), "--format", "json"])

    body = json.loads(result.output)
    assert result.exit_code == 0
    assert body["edges"] == [
        {
            "sender": "test-agent",
            "receiver": "receiver-agent",
            "named": True,
            "deliverable": True,
            "reason": None,
        }
    ]
    assert body["errors"] == []


def test_org_names_a_declared_handoff_that_would_not_deliver(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    sender = valid_manifest()
    sender["delegation"] = {"send": True, "to": ["ghost"]}
    write_resident(sender)

    result = runner.invoke(main, ["org", "--residents", str(tmp_path / "residents")])

    assert result.exit_code == 0
    assert "test-agent -> ghost" in result.output


def test_org_exits_invalid_when_a_manifest_does_not_validate(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The same exit code `validate` uses: a chart drawn from half a tree is not an answer."""
    write_resident(valid_manifest())
    broken = tmp_path / "residents" / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text("version: 0\nid: broken\n", encoding="utf-8")

    result = runner.invoke(main, ["org", "--residents", str(tmp_path / "residents")])

    assert result.exit_code == 1
