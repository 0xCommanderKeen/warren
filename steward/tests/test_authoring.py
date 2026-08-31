"""The write path: what may be written, what may not, and what git is told about it.

Every test here uses a real git repository in a temp directory, because the promise being
tested is that steward commits — and a fake git would only prove that the fake agrees with
itself.
"""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import (
    SECOND_RESIDENT_UID,
    VALID_SOUL,
    ResidentWriter,
    ScratchRepo,
    SkillWriter,
    valid_manifest,
)
from steward import authoring as au
from steward.manifest import MANIFEST_FILENAME
from steward.nursery import CommitIdentity

PRINCIPAL = "the operator holding STEWARD_TOKEN"
REQUEST_ID = "req-0001"

#: The skills `valid_manifest` grants. A configured-but-empty library turns every grant
#: into a diagnostic, so a fixture that writes a resident must write its skills too.
GRANTED = ("daily-summary", "write-journal")


@pytest.fixture
def fleet(
    scratch_repo: ScratchRepo, write_resident: ResidentWriter, write_skill: SkillWriter
) -> ScratchRepo:
    """Build a committed checkout with one valid resident and the skills it is granted."""
    for name in GRANTED:
        write_skill(name, root=scratch_repo.skills)
    write_resident(valid_manifest(), root=scratch_repo.residents)
    scratch_repo.git("add", "-A")
    scratch_repo.git("commit", "-m", "feat: the fleet as it stands")
    return scratch_repo


def declaration_of(repo: ScratchRepo, resident_id: str = "test-agent") -> au.Declaration:
    """Read a resident's declaration back the way a form would load it."""
    return au.read_declaration(repo.residents, resident_id, "soul.md")


def edited(declaration: au.Declaration, **changes: object) -> au.Declaration:
    """Return the declaration with those top-level manifest keys changed."""
    data = yaml.safe_load(declaration.manifest_text)
    data.update(changes)
    return au.Declaration(
        manifest_text=yaml.safe_dump(data, sort_keys=False), soul_text=declaration.soul_text
    )


def write(repo: ScratchRepo, resident_id: str, declaration: au.Declaration) -> au.WriteResult:
    """Run the real write path against the scratch checkout."""
    return au.write_declaration(
        repo.residents,
        resident_id,
        declaration,
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        skills_dir=repo.skills,
    )


# --------------------------------------------------------------------------------------
# the gate: an invalid manifest is never written
# --------------------------------------------------------------------------------------


def test_an_edit_that_does_not_validate_is_refused_and_changes_nothing(fleet: ScratchRepo) -> None:
    """The whole promise of the endpoint, asserted three ways: no write, no commit, no loss."""
    before = (fleet.residents / "test-agent" / MANIFEST_FILENAME).read_text(encoding="utf-8")
    head = fleet.head()

    with pytest.raises(au.AuthoringError) as refused:
        write(fleet, "test-agent", edited(declaration_of(fleet), summary="x" * 5_000))

    assert refused.value.reason == "manifest_invalid"
    assert (fleet.residents / "test-agent" / MANIFEST_FILENAME).read_text(
        encoding="utf-8"
    ) == before
    assert fleet.head() == head
    assert fleet.git("status", "--porcelain").stdout.strip() == ""


def test_a_refusal_carries_the_field_that_was_wrong(fleet: ScratchRepo) -> None:
    """A form needs to put a red border round one input, not print a paragraph."""
    with pytest.raises(au.AuthoringError) as refused:
        write(fleet, "test-agent", edited(declaration_of(fleet), summary="x" * 5_000))

    fields = {diagnostic.field_path for diagnostic in refused.value.diagnostics}
    assert "summary" in fields


def test_the_charter_caps_are_surfaced_rather_than_swallowed(fleet: ScratchRepo) -> None:
    """The prompt-neutralisation caps are a validation error like any other, and must show."""
    declaration = declaration_of(fleet)
    data = yaml.safe_load(declaration.manifest_text)
    data["charter"]["mission"] = "m" * 3_000

    with pytest.raises(au.AuthoringError) as refused:
        write(
            fleet,
            "test-agent",
            au.Declaration(yaml.safe_dump(data), declaration.soul_text),
        )

    fields = {diagnostic.field_path for diagnostic in refused.value.diagnostics}
    assert "charter.mission" in fields


def add_second_resident(repo: ScratchRepo, write_resident: ResidentWriter) -> dict[str, Any]:
    """Put a second valid resident in the tree, soul and manifest agreeing."""
    second = copy.deepcopy(valid_manifest())
    second["id"] = "second-agent"
    second["agent_id"] = "claude-code:second-agent"
    second["uid"] = SECOND_RESIDENT_UID
    write_resident(
        second,
        directory="second-agent",
        soul=VALID_SOUL.replace("claude-code:test-agent", "claude-code:second-agent"),
        root=repo.residents,
    )
    return second


def test_a_duplicate_uid_is_caught_because_the_whole_tree_is_validated(
    fleet: ScratchRepo, write_resident: ResidentWriter
) -> None:
    """A single-file check could not see this, and `steward validate` would have failed."""
    add_second_resident(fleet, write_resident)

    # Give the second resident the first one's uid: valid on its own, a collision in a fleet.
    colliding = edited(declaration_of(fleet, "second-agent"), uid=valid_manifest()["uid"])

    with pytest.raises(au.AuthoringError) as refused:
        write(fleet, "second-agent", colliding)

    assert "uid" in {diagnostic.field_path for diagnostic in refused.value.diagnostics}


def test_diagnostics_name_the_real_tree_not_the_scratch_copy(fleet: ScratchRepo) -> None:
    """A complaint about a temp directory that no longer exists helps nobody."""
    with pytest.raises(au.AuthoringError) as refused:
        write(fleet, "test-agent", edited(declaration_of(fleet), summary="x" * 5_000))

    for diagnostic in refused.value.diagnostics:
        assert "steward-authoring-" not in str(diagnostic.file)
        assert str(fleet.residents) in str(diagnostic.file)


def test_a_tree_wide_warning_is_reported_without_refusing_the_write(
    fleet: ScratchRepo, write_resident: ResidentWriter
) -> None:
    """Warnings inform, errors refuse — and the write path must not confuse the two.

    Two residents journalling into one directory is a warning by design: a tree can be
    arranged that way on purpose. It is still worth telling whoever just saved the form,
    so it comes back on the result rather than being dropped.
    """
    add_second_resident(fleet, write_resident)
    declaration = declaration_of(fleet, "second-agent")
    data = yaml.safe_load(declaration.manifest_text)
    data["memory"] = copy.deepcopy(valid_manifest()["memory"])

    result = write(
        fleet, "second-agent", au.Declaration(yaml.safe_dump(data), declaration.soul_text)
    )

    assert result.commit.committed
    assert any("memory.path" in d.field_path for d in result.validation.warnings)


# --------------------------------------------------------------------------------------
# the happy path, and what git was told
# --------------------------------------------------------------------------------------


def test_an_accepted_edit_lands_and_is_committed(fleet: ScratchRepo) -> None:
    """Written, validated, and in git — the audit trail is the point, not a side effect."""
    result = write(fleet, "test-agent", edited(declaration_of(fleet), summary="A tidier summary."))

    assert result.commit.committed
    assert result.commit.sha == fleet.head()
    data = yaml.safe_load((fleet.residents / "test-agent" / MANIFEST_FILENAME).read_text())
    assert data["summary"] == "A tidier summary."
    assert fleet.git("status", "--porcelain").stdout.strip() == ""


def test_the_commit_names_the_principal_and_the_request(fleet: ScratchRepo) -> None:
    """`git show` answers who and which call; `GET /requests/{id}` answers when and what."""
    write(fleet, "test-agent", edited(declaration_of(fleet), summary="A tidier summary."))

    body = fleet.git("log", "-1", "--format=%an <%ae>%n%s%n%b").stdout
    assert au.DEFAULT_IDENTITY.spec in body
    assert "update test-agent via the API" in body
    assert f"{au.REQUEST_TRAILER}: {REQUEST_ID}" in body
    assert PRINCIPAL in body


def test_a_configured_identity_is_the_author_and_the_committer(fleet: ScratchRepo) -> None:
    """Both, so the commit works on a server with no ambient git identity at all."""
    identity = CommitIdentity(name="Miha", email="miha@example.invalid")

    au.write_declaration(
        fleet.residents,
        "test-agent",
        edited(declaration_of(fleet), summary="Signed by a person."),
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        skills_dir=fleet.skills,
        identity=identity,
    )

    who = fleet.git("log", "-1", "--format=%an <%ae>|%cn <%ce>").stdout.strip()
    assert who == f"{identity.spec}|{identity.spec}"


def test_a_write_that_changes_nothing_makes_no_second_commit(fleet: ScratchRepo) -> None:
    """Saving a form without changing anything must not grow a pile of empty commits."""
    commits = fleet.log()

    result = write(fleet, "test-agent", declaration_of(fleet))

    assert not result.commit.committed
    assert result.commit.sha is None
    assert "already what is in git" in result.commit.note
    assert fleet.log() == commits


def test_the_commit_takes_only_stewards_own_paths(fleet: ScratchRepo) -> None:
    """Somebody's half-finished afternoon must never be swept into steward's commit."""
    (fleet.root / "README.md").write_text("a change nobody asked steward to commit\n")

    write(fleet, "test-agent", edited(declaration_of(fleet), summary="A tidier summary."))

    touched = fleet.git("show", "--name-only", "--format=", "HEAD").stdout.split()
    assert touched == ["residents/test-agent/manifest.yaml"]
    assert "README.md" in fleet.git("status", "--porcelain").stdout


def test_a_dirty_checkout_does_not_stop_the_control_plane(fleet: ScratchRepo) -> None:
    """The nursery refuses a dirty tree; a long-running server must not, and cannot afford to."""
    (fleet.root / "half-done.txt").write_text("someone is mid-edit\n")

    result = write(fleet, "test-agent", edited(declaration_of(fleet), summary="Still fine."))

    assert result.commit.committed


def test_the_revision_changes_when_the_files_do(fleet: ScratchRepo) -> None:
    """The fingerprint a form carries so two editors cannot silently overwrite each other."""
    before = au.revision_of(*au.declaration_paths(fleet.residents, "test-agent", "soul.md"))

    result = write(fleet, "test-agent", edited(declaration_of(fleet), summary="Changed."))

    assert result.revision != before


# --------------------------------------------------------------------------------------
# no git, and saying so
# --------------------------------------------------------------------------------------


def test_a_tree_outside_a_checkout_refuses_the_write(
    tmp_path: Path, write_resident: ResidentWriter, write_skill: SkillWriter
) -> None:
    """No history, no author, no way back — so steward declines rather than writing quietly."""
    residents = tmp_path / "loose" / "residents"
    skills = tmp_path / "loose" / "skills"
    skills.mkdir(parents=True)
    for name in GRANTED:
        write_skill(name, root=skills)
    write_resident(valid_manifest(), root=residents)
    before = (residents / "test-agent" / MANIFEST_FILENAME).read_text(encoding="utf-8")

    with pytest.raises(au.AuthoringError) as refused:
        au.write_declaration(
            residents,
            "test-agent",
            au.Declaration(before.replace("A resident", "An edited resident"), None),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            skills_dir=skills,
        )

    assert refused.value.reason == "not_a_git_checkout"
    assert "STEWARD_ALLOW_UNCOMMITTED_WRITES" in str(refused.value)


def test_the_uncommitted_mode_writes_and_says_so_in_the_response(
    tmp_path: Path, write_resident: ResidentWriter, write_skill: SkillWriter
) -> None:
    """An operator may accept a fleet with no audit trail — but never by leaving a field out."""
    residents = tmp_path / "loose" / "residents"
    skills = tmp_path / "loose" / "skills"
    skills.mkdir(parents=True)
    for name in GRANTED:
        write_skill(name, root=skills)
    write_resident(valid_manifest(), root=residents)
    declaration = au.read_declaration(residents, "test-agent", "soul.md")

    result = au.write_declaration(
        residents,
        "test-agent",
        edited(declaration, summary="Written without a net."),
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        skills_dir=skills,
        allow_uncommitted=True,
    )

    assert not result.commit.committed
    assert "no audit trail" in result.commit.note
    data = yaml.safe_load((residents / "test-agent" / MANIFEST_FILENAME).read_text())
    assert data["summary"] == "Written without a net."


# --------------------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------------------


def test_a_new_skill_is_written_parsed_and_committed(fleet: ScratchRepo) -> None:
    """A skill added over HTTP is a skill in git, indistinguishable from a hand-written one."""
    document = au.SkillDocument(
        name="triage",
        description="Sort the inbox before anything else.",
        body="Read every message. Answer what you can. Escalate what you cannot.",
    )

    result = au.write_skill(
        fleet.residents,
        fleet.skills,
        document,
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        created=True,
    )

    assert result.commit.committed
    written, revision = au.read_skill_document(fleet.skills, "triage")
    assert written.description == document.description
    assert revision == result.revision
    assert "add triage via the API" in fleet.git("log", "-1", "--format=%s").stdout


def test_a_skill_whose_description_would_break_its_own_frontmatter_is_refused(
    fleet: ScratchRepo,
) -> None:
    """`Skill.document` builds frontmatter with f-strings, so the round trip is the real check."""
    document = au.SkillDocument(
        name="triage",
        description="Sort the inbox\nname: something-else",
        body="Read every message.",
    )

    with pytest.raises(au.AuthoringError) as refused:
        au.write_skill(
            fleet.residents,
            fleet.skills,
            document,
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=True,
        )

    assert refused.value.reason == "skill_invalid"
    assert not (fleet.skills / "triage").exists()


def test_an_oversized_skill_body_is_refused_before_anything_is_written(
    fleet: ScratchRepo,
) -> None:
    """The library's own cap, surfaced rather than silently truncated."""
    with pytest.raises(au.AuthoringError):
        au.write_skill(
            fleet.residents,
            fleet.skills,
            au.SkillDocument(name="huge", description="Too much.", body="x" * 9_000),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=True,
        )

    assert not (fleet.skills / "huge").exists()


def test_editing_a_skill_keeps_its_place_in_the_library(fleet: ScratchRepo) -> None:
    """An update is an update: one file rewritten, one commit, no second skill."""
    result = au.write_skill(
        fleet.residents,
        fleet.skills,
        au.SkillDocument(
            name="daily-summary",
            description="Write the day up, briefly.",
            body="Say what happened. Say what did not.",
        ),
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        created=False,
    )

    assert result.commit.committed
    assert "update daily-summary via the API" in fleet.git("log", "-1", "--format=%s").stdout
    written, _ = au.read_skill_document(fleet.skills, "daily-summary")
    assert written.description == "Write the day up, briefly."


def test_a_skill_that_would_break_a_resident_is_refused(fleet: ScratchRepo) -> None:
    """A skill is validated against the fleet reading it, not only against itself."""
    unreadable = fleet.skills / "daily-summary" / "SKILL.md"
    before = unreadable.read_text(encoding="utf-8")

    with pytest.raises(au.AuthoringError):
        au.write_skill(
            fleet.residents,
            fleet.skills,
            au.SkillDocument(name="daily-summary", description="", body="Still here."),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
        )

    assert unreadable.read_text(encoding="utf-8") == before


def test_reading_an_unknown_skill_says_so(fleet: ScratchRepo) -> None:
    """A 404 in the making, with the reason code the route keys off."""
    with pytest.raises(au.AuthoringError) as refused:
        au.read_skill_document(fleet.skills, "nobody-wrote-this")

    assert refused.value.reason == "unknown_skill"
