"""The nursery: two files in git, a container on a host, and a schedule that checks out."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import ScratchRepo
from steward.board import (
    delegation_residents,
    load_board_residents,
    load_residents,
)
from steward.deploy import LocalTransport, SshTransport, TransportError
from steward.manifest import (
    Diagnostic,
    ValidationResult,
    active_residents,
    retired_complaint,
    scan_for_credentials,
    scan_text_for_secrets,
    validate_tree,
)
from steward.nursery import (
    NewResident,
    NurseryError,
    NurseryReport,
    commit_paths,
    declare_resident,
    raise_resident,
    retire_resident,
    set_retired,
    worktree_complaint,
)
from steward.runners import CommandOutcome
from steward.scheduler import load_scheduled

CHARTER: dict[str, Any] = {
    "mission": "Keep the village's notes in order.",
    "duties": ["Tidy the notes each evening."],
    "rules": ["Never delete a note without asking."],
    "escalation": "Raise needs_human before anything irreversible.",
}


def spec(**overrides: Any) -> NewResident:  # noqa: ANN401 — a test factory takes anything
    """Build the minimum a caller must say to declare a resident."""
    data: dict[str, Any] = {
        "id": "note-keeper",
        "name": "Quill",
        "char": "Scribe",
        "accent": "#4f7ea6",
        "role": "note bot",
        "charter": CHARTER,
    }
    return NewResident.model_validate(data | overrides)


def test_a_declared_resident_passes_the_validator(tmp_path: Path) -> None:
    created = declare_resident(spec(), tmp_path)

    assert created.manifest_path.is_file()
    assert created.soul_path.is_file()
    result = validate_tree(tmp_path)
    assert result.ok, [d.render() for d in result.errors]
    assert [resident.id for resident in result.residents] == ["note-keeper"]


def test_the_declaration_deploys_nothing_and_schedules_nothing(tmp_path: Path) -> None:
    created = declare_resident(spec(), tmp_path)
    manifest = yaml.safe_load(created.manifest_path.read_text(encoding="utf-8"))

    assert manifest["routines"] == []
    assert sorted(path.name for path in created.directory.iterdir()) == ["manifest.yaml", "soul.md"]


def test_the_skeleton_declares_a_journal_directory_and_no_empty_deploy_block(
    tmp_path: Path,
) -> None:
    """The skeleton matches docs/manifest.md: a journal directory and no deploy block.

    docs/manifest.md says the journal is a directory and an ordinary resident declares no
    deploy block — so the skeleton must not write `journal.md` or an empty `deploy` that
    reads as "this container runs nothing" (#90).
    """
    created = declare_resident(spec(), tmp_path)
    manifest = yaml.safe_load(created.manifest_path.read_text(encoding="utf-8"))

    assert manifest["memory"]["journal"] == "journal"
    assert "deploy" not in manifest
    # …and with no deploy block the resolved command is the documented default, not nothing.
    from steward.deploy import target_for  # noqa: PLC0415

    assert target_for(created.resident.manifest).command == ("sleep", "infinity")


def test_identity_is_derived_from_the_runner_when_nobody_says(tmp_path: Path) -> None:
    claude = declare_resident(spec(), tmp_path)
    codex = declare_resident(
        spec(id="scribe-two", runner={"kind": "codex", "model": "gpt-5"}), tmp_path
    )
    assert claude.resident.manifest.agent_id == "claude-code:note-keeper"
    assert codex.resident.manifest.agent_id == "codex:scribe-two"
    assert codex.resident.manifest.runner.model == "gpt-5"


def test_a_project_scoped_resident_keeps_its_project(tmp_path: Path) -> None:
    created = declare_resident(spec(project="burrow"), tmp_path)
    assert created.resident.manifest.agent_id is None
    assert created.resident.manifest.project == "burrow"
    assert "project: burrow" in created.soul_path.read_text(encoding="utf-8")


def test_the_soul_body_carries_the_frontmatter_and_a_voice(tmp_path: Path) -> None:
    created = declare_resident(spec(soul_body="Quill keeps the notes.", voice="Terse."), tmp_path)
    text = created.soul_path.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert "name: Quill" in text
    assert "Quill keeps the notes." in text
    assert "## Voice" in text
    assert created.resident.soul.voice == "Terse."


def test_declaring_over_an_existing_resident_is_refused(tmp_path: Path) -> None:
    declare_resident(spec(), tmp_path)
    with pytest.raises(NurseryError, match="already exists"):
        declare_resident(spec(), tmp_path)


def test_the_declared_paths_are_reported_for_review(tmp_path: Path) -> None:
    created = declare_resident(spec(summary="Keeps the notes."), tmp_path)
    payload = created.to_dict()

    assert payload["id"] == "note-keeper"
    assert payload["manifest_path"].endswith("note-keeper/manifest.yaml")
    assert payload["soul_path"].endswith("note-keeper/soul.md")
    assert payload["agent_id"] == "claude-code:note-keeper"


def test_a_skeleton_that_does_not_validate_is_not_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-valid skeleton would break `steward validate` for everyone until deleted."""
    broken = ValidationResult(
        diagnostics=(
            Diagnostic(file=tmp_path, field_path="soul", problem="pretend", example="pretend"),
        )
    )
    monkeypatch.setattr("steward.nursery.validate_manifest", lambda _path: broken)

    with pytest.raises(NurseryError, match="does not validate"):
        declare_resident(spec(), tmp_path)
    assert not (tmp_path / "note-keeper").exists()


def test_a_declaration_that_cannot_bind_to_the_schema_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NurseryError, match="cannot declare"):
        declare_resident(spec(agent_id="no-colon-here"), tmp_path)
    assert not (tmp_path / "note-keeper").exists()


# ======================================================================================
# the whole pipeline: declare -> provision -> register, against a fake host
# ======================================================================================


VILLAGE_TOKEN = "s3cret-village-token-nobody-should-see"
VILLAGE = {"BURROW_URL": "http://dxp2800:8737", "BURROW_TOKEN": VILLAGE_TOKEN}

ROUTINE = {
    "id": "tidy-notes",
    "schedule": "0 20 * * *",
    "schedule_tz": "Europe/Ljubljana",
    "prompt": "Tidy the notes.",
    "timeout_s": 600,
}


@pytest.fixture
def host(tmp_path: Path) -> LocalTransport:
    """Return a directory that plays the NAS and remembers everything it was asked."""
    return LocalTransport(root=tmp_path / "nas")


def raise_into(repo: ScratchRepo, host: LocalTransport, **kwargs: Any) -> NurseryReport:  # noqa: ANN401
    """Run the whole pipeline into a scratch checkout and a fake host."""
    return raise_resident(
        kwargs.pop("spec", None) or spec(),
        residents_dir=repo.residents,
        repo=repo.root,
        transport=host,
        env=VILLAGE,
        **kwargs,
    )


def edit_manifest(
    repo: ScratchRepo,
    resident_id: str = "note-keeper",
    **fields: Any,  # noqa: ANN401 — a manifest holds whatever the schema holds
) -> Path:
    """Edit a declared manifest by hand, the way a human does after the skeleton lands."""
    path = repo.residents / resident_id / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) | fields
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def add_routine(repo: ScratchRepo, resident_id: str = "note-keeper") -> Path:
    """Give a declared resident a routine, and commit it as a person would."""
    path = edit_manifest(repo, resident_id, routines=[ROUTINE])
    repo.git("commit", "-am", "feat(residents): give note-keeper a routine")
    return path


# ------------------------------------------------------------------ the happy path


def test_the_pipeline_declares_commits_provisions_and_checks(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """One command, three stages, and every one of them actually happened."""
    report = raise_into(scratch_repo, host)

    # declare: two files in the repo, and one commit naming the resident.
    assert report.declare.written
    assert report.declare.manifest_path.is_file()
    assert report.declare.soul_path.is_file()
    assert report.declare.commit == scratch_repo.head()
    assert scratch_repo.log()[0] == "feat(residents): declare note-keeper"

    # provision: the bundle is on the host and the container was asked to come up.
    landed = host.root / "docker" / "steward-note-keeper"
    assert (landed / "docker-compose.yaml").is_file()
    assert (landed / "soul.md").is_file()
    assert (landed / "memory").is_dir()
    assert host.calls[-1][:2] == ("docker", "compose")
    assert host.calls[-1][-2:] == ("up", "-d")

    # register: nothing to schedule yet, and the scheduler is not complaining.
    assert report.register is not None
    assert report.register.ok
    assert report.register.fires == ()
    assert report.changed


def test_the_deployed_container_is_wired_to_the_village(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """BURROW_AGENT_ID, BURROW_PROJECT, BURROW_URL and BURROW_TOKEN, exactly as Hob is."""
    raise_into(scratch_repo, host)
    compose = yaml.safe_load(host.read("~/docker/steward-note-keeper/docker-compose.yaml") or "")
    environment = compose["services"]["note-keeper"]["environment"]

    assert environment["BURROW_AGENT_ID"] == "claude-code:note-keeper"
    assert environment["BURROW_PROJECT"] == "note-keeper"
    assert environment["BURROW_URL"].startswith("${BURROW_URL")
    assert environment["BURROW_TOKEN"].startswith("${BURROW_TOKEN")
    assert host.read("~/docker/steward-note-keeper/.env") == (
        f"BURROW_TOKEN={VILLAGE_TOKEN}\nBURROW_URL=http://dxp2800:8737\n"
    )


def test_routines_are_reported_with_the_moment_they_next_fire(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """Registration is a check and a next-fire, because there is no second registry."""
    raise_into(scratch_repo, host)
    add_routine(scratch_repo)

    report = raise_into(scratch_repo, host, now=datetime(2026, 6, 15, 12, 0, tzinfo=UTC))

    assert report.register is not None
    assert [routine for routine, _ in report.register.fires] == ["tidy-notes"]
    assert report.register.fires[0][1].startswith("2026-06-15T20:00")


def test_a_runner_the_machine_cannot_launch_is_a_problem_on_the_deploy(
    scratch_repo: ScratchRepo,
    host: LocalTransport,
    empty_path: None,  # noqa: ARG001 — the fixture is the setup
) -> None:
    """Deployed, and `claude` is still not on PATH. Both facts, and neither is hidden."""
    raise_resident(
        spec(),
        residents_dir=scratch_repo.residents,
        transport=host,
        env=VILLAGE,
        commit=False,
    )
    edit_manifest(scratch_repo, routines=[ROUTINE])

    report = raise_resident(
        spec(),
        residents_dir=scratch_repo.residents,
        transport=host,
        env=VILLAGE,
        commit=False,
    )

    assert report.provision is not None
    assert report.provision.sent
    assert report.register is not None
    assert not report.register.ok
    assert "not on PATH" in report.register.problems[0]


# ---------------------------------------------------------------------- converging


def test_running_it_twice_is_a_no_op_the_second_time(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """Converged: no second commit, no second upload, and still exit-zero."""
    raise_into(scratch_repo, host)
    commits = scratch_repo.log()
    sent = list(host.sent)

    second = raise_into(scratch_repo, host)

    assert not second.changed
    assert second.declare.written is False
    assert second.declare.commit is None
    assert second.provision is not None
    assert second.provision.sent is False
    assert second.provision.compose_changed is False
    assert scratch_repo.log() == commits
    assert host.sent == sent


def test_a_converged_run_still_reconciles_the_container(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """`up -d` is issued every time: it is the only thing here that can revive a dead one."""
    raise_into(scratch_repo, host)
    host.calls.clear()

    raise_into(scratch_repo, host)

    assert [call for call in host.calls if call[-2:] == ("up", "-d")]


def test_an_edited_manifest_is_shipped_again(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """A routine added by hand reaches the host on the next run, without a new commit."""
    raise_into(scratch_repo, host)
    add_routine(scratch_repo)

    report = raise_into(scratch_repo, host)

    assert report.provision is not None
    assert report.provision.sent
    assert "tidy-notes" in (host.read("~/docker/steward-note-keeper/manifest.yaml") or "")


# ----------------------------------------------------------------------- refusals


def test_a_dirty_worktree_is_refused_before_anything_happens(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    (scratch_repo.root / "notes.txt").write_text("half an afternoon\n", encoding="utf-8")

    with pytest.raises(NurseryError, match="uncommitted changes"):
        raise_into(scratch_repo, host)

    assert not (scratch_repo.residents / "note-keeper").exists()
    assert not host.touched


def test_a_dirty_worktree_can_be_overridden_out_loud(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    (scratch_repo.root / "notes.txt").write_text("half an afternoon\n", encoding="utf-8")

    report = raise_into(scratch_repo, host, allow_dirty=True)

    assert report.declare.commit
    assert any("uncommitted changes" in warning for warning in report.warnings)


def test_a_name_collision_names_the_fields_that_disagree(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """Same id, different declaration: refused, because a soul is somebody's work."""
    raise_into(scratch_repo, host)

    with pytest.raises(NurseryError, match="do not match") as caught:
        raise_into(scratch_repo, host, spec=spec(role="something else entirely"))

    assert "soul" in str(caught.value)


def test_a_field_a_human_added_later_is_not_a_collision(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """Routines, budgets and deploy blocks are outside the spec, so they never collide."""
    raise_into(scratch_repo, host)
    add_routine(scratch_repo)

    report = raise_into(scratch_repo, host)

    assert report.declare.note == "already declared and unchanged"


def test_raising_a_retired_resident_is_refused_until_a_person_says_so(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    raise_into(scratch_repo, host)
    retire_resident(
        "note-keeper", residents_dir=scratch_repo.residents, repo=scratch_repo.root, transport=host
    )

    with pytest.raises(NurseryError, match="is retired"):
        raise_into(scratch_repo, host)


def test_a_deployment_with_no_village_to_emit_into_is_refused(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """A container that cannot emit is a resident that would never appear in burrow."""
    with pytest.raises(TransportError, match="BURROW_URL"):
        raise_resident(
            spec(),
            residents_dir=scratch_repo.residents,
            repo=scratch_repo.root,
            transport=host,
            env={},
        )


# ------------------------------------------------------- the host is not there


def test_an_unreachable_host_leaves_the_commit_intact_and_re_runnable(
    scratch_repo: ScratchRepo, tmp_path: Path
) -> None:
    """The declaration is in git first, precisely so this failure is recoverable."""
    down = LocalTransport(root=tmp_path / "nas", unreachable=True)

    with pytest.raises(NurseryError, match="cannot reach"):
        raise_into(scratch_repo, down)

    assert scratch_repo.log()[0] == "feat(residents): declare note-keeper"
    assert (scratch_repo.residents / "note-keeper" / "manifest.yaml").is_file()

    # …and the re-run, once the NAS is back, finishes the job without duplicating anything.
    back = LocalTransport(root=tmp_path / "nas")
    report = raise_into(scratch_repo, back)
    assert report.provision is not None
    assert report.provision.sent
    assert scratch_repo.log().count("feat(residents): declare note-keeper") == 1


def test_a_container_that_refuses_to_start_is_reported_as_itself(
    scratch_repo: ScratchRepo, tmp_path: Path
) -> None:
    sulking = LocalTransport(root=tmp_path / "nas", fail_on="up")

    with pytest.raises(NurseryError, match="docker compose up failed"):
        raise_into(scratch_repo, sulking)

    assert scratch_repo.log()[0] == "feat(residents): declare note-keeper"


# ------------------------------------------------------------------------ dry run


def test_a_dry_run_touches_nothing_at_all(scratch_repo: ScratchRepo, host: LocalTransport) -> None:
    """The whole promise, asserted three ways: no repo, no host, no commit."""
    before = scratch_repo.head()

    report = raise_into(scratch_repo, host, dry_run=True)

    assert report.dry_run
    assert not report.changed
    assert not (scratch_repo.residents / "note-keeper").exists()
    assert not host.touched
    assert host.calls == []
    assert host.sent == []
    assert scratch_repo.head() == before
    assert scratch_repo.git("status", "--porcelain").stdout == ""


def test_a_dry_run_prints_the_whole_plan(scratch_repo: ScratchRepo, host: LocalTransport) -> None:
    """Files, the compose fragment, the exact argv, and the next fires."""
    report = raise_into(scratch_repo, host, dry_run=True)
    plan = "\n".join(report.render())

    assert "note-keeper/manifest.yaml" in plan
    assert "note-keeper/soul.md" in plan
    assert "docker-compose.yaml" in plan
    assert "docker compose -f ~/docker/steward-note-keeper/docker-compose.yaml" in plan
    assert "up -d" in plan
    assert "diff not computed" in plan
    assert "services:" in plan  # the whole fragment, since there is no diff to show
    assert report.provision is not None
    assert VILLAGE_TOKEN not in plan


def test_a_dry_run_needs_no_village_address(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """#84: a rehearsal reaches no host, so BURROW_URL being unset is a warning, not a crash."""
    report = raise_resident(
        spec(),
        residents_dir=scratch_repo.residents,
        repo=scratch_repo.root,
        transport=host,
        env={},  # no BURROW_URL, no BURROW_TOKEN
        dry_run=True,
    )

    assert report.dry_run
    assert not report.changed
    assert not host.touched
    assert not (scratch_repo.residents / "note-keeper").exists()
    assert any("BURROW_URL" in warning for warning in report.warnings)
    assert report.provision is not None
    assert report.provision.env_keys == ()
    assert "services:" in "\n".join(report.render())


def test_a_dry_run_never_writes_scheduler_state(
    scratch_repo: ScratchRepo, host: LocalTransport, tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state" / "scheduler.json"
    monkeypatch.setenv("STEWARD_STATE", str(state))

    raise_into(scratch_repo, host, dry_run=True)

    assert not state.exists()


def test_a_dry_run_on_a_dirty_tree_warns_instead_of_refusing(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """A rehearsal is exactly what you want while your tree is dirty."""
    (scratch_repo.root / "notes.txt").write_text("half an afternoon\n", encoding="utf-8")

    report = raise_into(scratch_repo, host, dry_run=True)

    assert any("a real run would refuse" in warning for warning in report.warnings)
    assert not host.touched


# ------------------------------------------------------------------------ secrets


def test_no_secret_ever_lands_in_the_checkout(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """Asserted against the repo's own credential scanners, over everything the nursery wrote."""
    raise_into(scratch_repo, host)

    for path in sorted(scratch_repo.root.rglob("*")):
        if not path.is_file() or ".git/" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert VILLAGE_TOKEN not in text, path
        assert scan_text_for_secrets(text, path, "body") == [], path

    manifest = yaml.safe_load(
        (scratch_repo.residents / "note-keeper" / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert scan_for_credentials(manifest, scratch_repo.root) == []
    assert validate_tree(scratch_repo.residents).ok
    assert "BURROW_TOKEN" not in scratch_repo.git("log", "-p").stdout


def test_the_secret_reaches_the_host_and_only_the_host(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    raise_into(scratch_repo, host)

    assert VILLAGE_TOKEN in (host.read("~/docker/steward-note-keeper/.env") or "")
    assert VILLAGE_TOKEN not in (
        host.read("~/docker/steward-note-keeper/docker-compose.yaml") or ""
    )


def test_the_report_names_the_secrets_without_showing_them(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    report = raise_into(scratch_repo, host)

    assert report.provision is not None
    assert report.provision.env_keys == ("BURROW_TOKEN", "BURROW_URL")
    assert VILLAGE_TOKEN not in json.dumps(report.to_dict())
    assert VILLAGE_TOKEN not in "\n".join(report.render())


# ------------------------------------------------------------------------- retiring


def test_retiring_stops_the_container_and_records_the_decision(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    raise_into(scratch_repo, host)
    host.calls.clear()

    report = retire_resident(
        "note-keeper", residents_dir=scratch_repo.residents, repo=scratch_repo.root, transport=host
    )

    assert report.marked
    assert report.stopped
    assert host.calls[-1][-2:] == ("down", "--remove-orphans")
    assert scratch_repo.log()[0] == "chore(residents): retire note-keeper"
    assert report.commit == scratch_repo.head()


def test_a_retired_resident_keeps_its_files_and_keeps_validating(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """Retirement is a lifecycle state, not a deletion."""
    raise_into(scratch_repo, host)
    retire_resident(
        "note-keeper", residents_dir=scratch_repo.residents, repo=scratch_repo.root, transport=host
    )

    result = validate_tree(scratch_repo.residents)
    assert result.ok
    assert [r.id for r in result.residents] == ["note-keeper"]
    assert result.residents[0].retired
    assert (scratch_repo.residents / "note-keeper" / "soul.md").is_file()


def test_retiring_twice_converges(scratch_repo: ScratchRepo, host: LocalTransport) -> None:
    raise_into(scratch_repo, host)
    retire_resident(
        "note-keeper", residents_dir=scratch_repo.residents, repo=scratch_repo.root, transport=host
    )
    commits = scratch_repo.log()

    again = retire_resident(
        "note-keeper", residents_dir=scratch_repo.residents, repo=scratch_repo.root, transport=host
    )

    assert not again.marked
    assert again.stopped
    assert scratch_repo.log() == commits


def test_retiring_something_that_was_never_deployed_says_so(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    raise_into(scratch_repo, host, provision=False)

    report = retire_resident(
        "note-keeper", residents_dir=scratch_repo.residents, repo=scratch_repo.root, transport=host
    )

    assert report.marked
    assert not report.stopped
    assert "nothing at" in report.note


def test_retiring_against_an_unreachable_host_does_not_report_success(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """The container is still running, so `steward retire` must not say it is not.

    `_stop_retired_container` decides whether there is anything to stop by reading the
    compose file, and `SshTransport.read` used to fold every failure — no ssh binary, a
    host that never answered, an auth refusal — into the same `None` a missing file gives.
    So an unreachable NAS returned "nothing at ~/docker/<id> on dxp2800 to stop", the
    function completed normally, and `steward retire` printed `<id> is retired` in green
    and exited 0 while the container kept firing and kept spending (steward #136).

    A `LocalTransport` cannot show this: it raises on an unreachable host, which is the
    behaviour the real one was missing. So this drives a real `SshTransport` over an ssh
    that never connects.
    """
    raise_into(scratch_repo, host)

    def never_connects(
        argv: Sequence[str],
        timeout_s: float = 20.0,  # noqa: ARG001 — part of the signature run_argv has
        *,
        stdin: bytes | None = None,  # noqa: ARG001 — likewise
    ) -> CommandOutcome:
        return CommandOutcome(argv=tuple(argv), error="'ssh' did not answer within 20s")

    with pytest.raises(NurseryError, match="could not be reached"):
        retire_resident(
            "note-keeper",
            residents_dir=scratch_repo.residents,
            repo=scratch_repo.root,
            transport=SshTransport(command=never_connects),
        )

    # The mark is committed before the host is touched, and stays committed: that order is
    # what keeps the watchdog from restarting a resident nobody could stop.
    assert scratch_repo.log()[0] == "chore(residents): retire note-keeper"
    assert validate_tree(scratch_repo.residents).residents[0].retired


def test_retiring_marks_the_manifest_before_it_touches_the_host(
    scratch_repo: ScratchRepo, tmp_path: Path
) -> None:
    """Otherwise the watchdog would notice the container die and put it straight back."""
    host = LocalTransport(root=tmp_path / "nas")
    raise_into(scratch_repo, host)
    host.unreachable = True

    with pytest.raises(NurseryError, match="could not be reached"):
        retire_resident(
            "note-keeper",
            residents_dir=scratch_repo.residents,
            repo=scratch_repo.root,
            transport=host,
        )

    assert scratch_repo.log()[0] == "chore(residents): retire note-keeper"
    assert validate_tree(scratch_repo.residents).residents[0].retired


def test_retiring_an_unknown_resident_suggests_the_one_you_meant(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    raise_into(scratch_repo, host)

    with pytest.raises(NurseryError, match="note-keeper"):
        retire_resident(
            "note-keper",
            residents_dir=scratch_repo.residents,
            repo=scratch_repo.root,
            transport=host,
        )


def test_a_dry_run_retirement_stops_and_commits_nothing(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    raise_into(scratch_repo, host)
    before = scratch_repo.head()
    host.calls.clear()

    report = retire_resident(
        "note-keeper",
        residents_dir=scratch_repo.residents,
        repo=scratch_repo.root,
        transport=host,
        dry_run=True,
    )

    assert report.dry_run
    assert host.calls == []
    assert scratch_repo.head() == before
    assert not validate_tree(scratch_repo.residents).residents[0].retired
    assert "down --remove-orphans" in "\n".join(report.render())


def test_retiring_without_deploy_marks_but_reaches_no_host(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """The `--no-deploy` counterpart: mark and commit, but leave the host alone (#90)."""
    raise_into(scratch_repo, host)
    host.calls.clear()
    host.sent.clear()

    report = retire_resident(
        "note-keeper",
        residents_dir=scratch_repo.residents,
        repo=scratch_repo.root,
        transport=host,
        deploy=False,
    )

    assert report.marked
    assert not report.stopped
    assert report.commands == ()
    assert host.calls == []  # no ssh, no `docker compose down`
    assert report.commit == scratch_repo.head()
    assert validate_tree(scratch_repo.residents).residents[0].retired
    assert "deploy skipped" in report.note


def test_a_retire_dry_run_plans_the_mark_before_the_stop(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """docs/manifest.md order: `retired: true` lands before the container is stopped (#90)."""
    raise_into(scratch_repo, host)

    report = retire_resident(
        "note-keeper",
        residents_dir=scratch_repo.residents,
        repo=scratch_repo.root,
        transport=host,
        dry_run=True,
    )

    lines = report.render()
    mark = next(index for index, line in enumerate(lines) if "retired: true" in line)
    stop = next(index for index, line in enumerate(lines) if "down --remove-orphans" in line)
    assert mark < stop


def test_retirement_emits_nothing_on_the_residents_behalf(
    scratch_repo: ScratchRepo, host: LocalTransport, isolated_events: Path
) -> None:
    """It leaves the village by going quiet. A forged session_ended would be a lie."""
    raise_into(scratch_repo, host)
    retire_resident(
        "note-keeper", residents_dir=scratch_repo.residents, repo=scratch_repo.root, transport=host
    )

    assert not isolated_events.exists()


# ------------------------------------------------------ what retirement excludes


def working_tree(scratch_repo: ScratchRepo, host: LocalTransport) -> Path:
    """Raise a resident that fires, claims, and takes letters — everything retirement stops."""
    raise_into(scratch_repo, host)
    return edit_manifest(
        scratch_repo,
        routines=[ROUTINE],
        board={"claim": True, "lease_s": 1800, "timeout_s": 900},
        routes=[
            {"id": "job-board", "kind": "job-board", "address": "steward:job-board"},
            {"id": "handoff", "kind": "delegation", "address": "steward:delegation"},
        ],
    )


def test_a_retired_resident_is_excluded_from_scheduling_and_the_board(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    working_tree(scratch_repo, host)
    assert [item.key for item in load_scheduled(scratch_repo.residents)] == [
        "note-keeper/tidy-notes"
    ]
    assert [r.id for r in load_board_residents(scratch_repo.residents)] == ["note-keeper"]

    set_retired(scratch_repo.residents / "note-keeper" / "manifest.yaml")

    assert load_scheduled(scratch_repo.residents) == []
    assert load_board_residents(scratch_repo.residents) == []
    assert delegation_residents(load_residents(scratch_repo.residents)) == []


def test_the_reason_a_retired_resident_is_refused_is_the_same_everywhere(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    path = working_tree(scratch_repo, host)
    set_retired(path)
    resident = validate_tree(scratch_repo.residents).residents[0]

    complaint = retired_complaint(resident)
    assert complaint is not None
    assert "retired: true" in complaint
    assert active_residents([resident]) == []


# --------------------------------------------------------- git, at its own seam


def refusing_git(reason: str) -> Any:  # noqa: ANN401 — a fake git answers like run_argv
    """Build a git that fails whichever subcommand a test names."""

    def _git(argv: Sequence[str], *_args: object, **_kwargs: object) -> CommandOutcome:
        parts = tuple(str(part) for part in argv)
        failing = reason in parts
        # `git diff --cached --quiet` exits 1 when there *are* staged changes, which is
        # the ordinary path — a fake that answered 0 would look like "nothing to commit".
        has_changes = "diff" in parts
        return CommandOutcome(
            argv=parts,
            exit_status=1 if failing or has_changes else 0,
            stderr="git said no" if failing else "",
        )

    return _git


def test_a_directory_that_is_not_a_checkout_is_named_as_such(tmp_path: Path) -> None:
    assert "not a git checkout" in (
        worktree_complaint(tmp_path, git=refusing_git("rev-parse")) or ""
    )


def test_a_git_that_cannot_read_the_worktree_says_so(tmp_path: Path) -> None:
    assert "could not read the worktree" in (
        worktree_complaint(tmp_path, git=refusing_git("status")) or ""
    )


def test_a_long_list_of_dirty_paths_is_counted_rather_than_printed(
    scratch_repo: ScratchRepo,
) -> None:
    for index in range(9):
        (scratch_repo.root / f"scratch-{index}.txt").write_text("x\n", encoding="utf-8")

    complaint = worktree_complaint(scratch_repo.root) or ""

    assert "+4 more" in complaint


def test_a_git_that_cannot_stage_or_commit_is_reported_as_git(
    scratch_repo: ScratchRepo,
) -> None:
    path = scratch_repo.root / "README.md"
    with pytest.raises(NurseryError, match="could not stage"):
        commit_paths(scratch_repo.root, [path], "subject", git=refusing_git("add"))
    with pytest.raises(NurseryError, match="could not commit"):
        commit_paths(scratch_repo.root, [path], "subject", git=refusing_git("commit"))


def test_marking_a_manifest_is_a_text_edit_that_keeps_the_comments(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """A model round-trip would silently throw away everything a person wrote in here."""
    raise_into(scratch_repo, host)
    path = scratch_repo.residents / "note-keeper" / "manifest.yaml"
    path.write_text(
        "# Quill keeps the notes.\n" + path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert set_retired(path)
    assert not set_retired(path)  # already says so
    text = path.read_text(encoding="utf-8")
    assert "# Quill keeps the notes." in text
    assert "retired: true" in text

    assert set_retired(path, retired=False)
    assert "retired: false" in path.read_text(encoding="utf-8")
    assert not validate_tree(scratch_repo.residents).residents[0].retired


def test_a_manifest_that_never_mentioned_retirement_gains_the_field(tmp_path: Path) -> None:
    """Every manifest written before #4 landed is one of these."""
    path = tmp_path / "manifest.yaml"
    path.write_text("id: old-timer\n", encoding="utf-8")

    assert set_retired(path, retired=False)
    assert "retired: false" in path.read_text(encoding="utf-8")
    assert set_retired(path)
    assert "retired: true" in path.read_text(encoding="utf-8")


def test_a_manifest_that_stopped_validating_is_refused_rather_than_deployed(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    raise_into(scratch_repo, host)
    edit_manifest(scratch_repo, memory={"kind": "directory"})

    with pytest.raises(NurseryError, match="does not validate"):
        raise_into(scratch_repo, host, allow_dirty=True)


def test_a_run_that_declares_nothing_new_still_reports_where_it_lives(
    scratch_repo: ScratchRepo, host: LocalTransport
) -> None:
    """The plan of a converged run names the compose file as unchanged, not as missing."""
    raise_into(scratch_repo, host)

    plan = "\n".join(raise_into(scratch_repo, host).render())

    assert "compose unchanged" in plan
    assert "already declared and unchanged" in plan
    assert "no enabled routines" in plan
