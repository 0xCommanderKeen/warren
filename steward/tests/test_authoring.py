"""The write path: what may be written, what may not, and what git is told about it.

Every test here uses a real git repository in a temp directory, because the promise being
tested is that steward commits — and a fake git would only prove that the fake agrees with
itself.
"""

import copy
import os
import re
import stat
import threading
import time
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
    bare_origin,
    branch_head,
    valid_manifest,
)
from steward import authoring as au
from steward.manifest import MANIFEST_FILENAME
from steward.nursery import CommitIdentity
from steward.runners import CommandOutcome

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
    second["home"] = 1
    write_resident(
        second,
        directory="second-agent",
        soul=VALID_SOUL.replace("claude-code:test-agent", "claude-code:second-agent"),
        root=repo.residents,
    )
    return second


def test_a_duplicate_uid_edit_is_refused_as_identity_replacement(
    fleet: ScratchRepo, write_resident: ResidentWriter
) -> None:
    """A single-file check could not see this, and `steward validate` would have failed."""
    add_second_resident(fleet, write_resident)

    # Give the second resident the first one's uid: valid on its own, a collision in a fleet.
    colliding = edited(declaration_of(fleet, "second-agent"), uid=valid_manifest()["uid"])

    with pytest.raises(au.AuthoringError) as refused:
        write(fleet, "second-agent", colliding)

    assert refused.value.reason == "resident_identity_changed"


@pytest.mark.parametrize("field", ["uid", "agent_id"])
def test_an_ordinary_declaration_edit_cannot_replace_identity(
    fleet: ScratchRepo, field: str
) -> None:
    declaration = declaration_of(fleet)
    replacement = SECOND_RESIDENT_UID if field == "uid" else "resident:replacement"

    with pytest.raises(au.AuthoringError) as refused:
        write(fleet, "test-agent", edited(declaration, **{field: replacement}))

    assert refused.value.reason == "resident_identity_changed"
    assert field in str(refused.value)


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


def test_unchanged_declaration_ignores_and_preserves_same_target_staging(
    fleet: ScratchRepo,
) -> None:
    target = fleet.residents / "test-agent" / MANIFEST_FILENAME
    head_bytes = target.read_bytes()
    target.write_text(
        edited(declaration_of(fleet), summary="The operator's staged bytes.").manifest_text,
        encoding="utf-8",
    )
    fleet.git("add", str(target.relative_to(fleet.root)))
    target.write_bytes(head_bytes)
    index = fleet.root / ".git" / "index"
    staged_before = index.read_bytes()
    head = fleet.head()

    result = write(fleet, "test-agent", declaration_of(fleet))

    assert not result.commit.committed
    assert fleet.head() == head
    assert index.read_bytes() == staged_before
    assert (
        "operator's staged bytes" in fleet.git("show", f":{target.relative_to(fleet.root)}").stdout
    )


def test_unchanged_skill_ignores_and_preserves_same_target_staging(fleet: ScratchRepo) -> None:
    target = fleet.skills / "daily-summary" / "SKILL.md"
    original, _ = au.read_skill_document(fleet.skills, "daily-summary")
    head_bytes = target.read_bytes()
    target.write_text(
        au.SkillDocument(
            name="daily-summary", description="The operator's staged bytes.", body=original.body
        ).text(),
        encoding="utf-8",
    )
    fleet.git("add", str(target.relative_to(fleet.root)))
    target.write_bytes(head_bytes)
    index = fleet.root / ".git" / "index"
    staged_before = index.read_bytes()
    head = fleet.head()

    result = au.write_skill(
        fleet.residents,
        fleet.skills,
        original,
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        created=False,
    )

    assert not result.commit.committed
    assert fleet.head() == head
    assert index.read_bytes() == staged_before
    assert (
        "operator's staged bytes" in fleet.git("show", f":{target.relative_to(fleet.root)}").stdout
    )


def test_successful_index_reconciliation_preserves_index_mode(fleet: ScratchRepo) -> None:
    index = fleet.root / ".git" / "index"
    index.chmod(0o640)

    write(fleet, "test-agent", edited(declaration_of(fleet), summary="A committed change."))

    assert stat.S_IMODE(index.stat().st_mode) == 0o640


def test_unborn_shared_repository_index_uses_git_permissions(
    tmp_path: Path, write_resident: ResidentWriter, write_skill: SkillWriter
) -> None:
    repo = ScratchRepo(root=tmp_path / "shared-checkout")
    repo.residents.mkdir(parents=True)
    repo.skills.mkdir(parents=True)
    repo.git("init", "-b", "main")
    repo.git("config", "core.sharedRepository", "group")
    for name in GRANTED:
        write_skill(name, root=repo.skills)
    write_resident(valid_manifest(), root=repo.residents)

    previous_umask = os.umask(0o077)
    try:
        write(repo, "test-agent", edited(declaration_of(repo), summary="The first commit."))
    finally:
        os.umask(previous_umask)

    index = repo.root / ".git" / "index"
    assert stat.S_IMODE(index.stat().st_mode) == 0o660


def test_unborn_index_creator_wins_before_reconciliation_lock(
    tmp_path: Path,
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first-index creator at the old compare/publish gap keeps its target bytes."""
    repo = ScratchRepo(root=tmp_path / "unborn-race")
    repo.residents.mkdir(parents=True)
    repo.skills.mkdir(parents=True)
    repo.git("init", "-b", "main")
    for name in GRANTED:
        write_skill(name, root=repo.skills)
    write_resident(valid_manifest(), root=repo.residents)
    target = repo.residents / "test-agent" / MANIFEST_FILENAME
    requested = edited(declaration_of(repo), summary="Steward's committed bytes.")
    staged = edited(declaration_of(repo), summary="The operator's staged bytes.").manifest_text
    index_lock = repo.root / ".git" / "index.lock"
    real_open = au.os.open
    raced = False

    def create_index_then_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        if Path(path) == index_lock and not raced:
            raced = True
            worktree = target.read_text(encoding="utf-8")
            target.write_text(staged, encoding="utf-8")
            repo.git("add", str(target.relative_to(repo.root)))
            target.write_text(worktree, encoding="utf-8")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(au.os, "open", create_index_then_open)

    result = write(repo, "test-agent", requested)

    assert raced
    assert result.commit.committed
    assert (
        yaml.safe_load(repo.git("show", f":{target.relative_to(repo.root)}").stdout)["summary"]
        == "The operator's staged bytes."
    )
    assert (
        yaml.safe_load(repo.git("show", f"HEAD:{target.relative_to(repo.root)}").stdout)["summary"]
        == "Steward's committed bytes."
    )


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


@pytest.mark.parametrize("failure", ["write-tree", "commit"])
@pytest.mark.parametrize("kind", ["declaration", "skill"])
def test_a_git_failure_restores_the_target_and_index_exactly(
    fleet: ScratchRepo, failure: str, kind: str
) -> None:
    """A refused request must not consume either Git state or a caller's working bytes."""
    unrelated = fleet.root / "README.md"
    unrelated.write_text("staged by somebody else\n", encoding="utf-8")
    fleet.git("add", "README.md")
    index = fleet.root / ".git" / "index"
    index_before = index.read_bytes()

    declaration = edited(declaration_of(fleet), summary="The requested declaration.")
    skill = au.SkillDocument(
        name="daily-summary",
        description="The requested skill.",
        body="Read the day and summarise it.\n",
    )
    target = (
        fleet.residents / "test-agent" / MANIFEST_FILENAME
        if kind == "declaration"
        else fleet.skills / "daily-summary" / "SKILL.md"
    )
    target_before = target.read_bytes()
    target.chmod(0o600)
    mode_before = target.stat().st_mode

    def failing_git(argv: list[str]) -> CommandOutcome:
        command = "commit-tree" if failure == "commit" else failure
        if command in argv:
            return CommandOutcome(tuple(argv), exit_status=1, stderr="injected failure")
        return au.run_argv(argv)

    def attempt() -> au.WriteResult:
        if kind == "declaration":
            return au.write_declaration(
                fleet.residents,
                "test-agent",
                declaration,
                request_id=REQUEST_ID,
                principal=PRINCIPAL,
                skills_dir=fleet.skills,
                git=failing_git,
            )
        return au.write_skill(
            fleet.residents,
            fleet.skills,
            skill,
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
            git=failing_git,
        )

    with pytest.raises(au.AuthoringError) as refused:
        attempt()

    assert refused.value.reason == "commit_failed"
    assert target.read_bytes() == target_before
    assert target.stat().st_mode == mode_before
    assert index.read_bytes() == index_before


@pytest.mark.parametrize("kind", ["declaration", "skill"])
def test_a_failed_write_never_erases_unrelated_staging_added_during_it(
    fleet: ScratchRepo, kind: str
) -> None:
    """The checkout index belongs to its operator even while Steward holds its own lock."""
    unrelated = fleet.root / "README.md"
    staged_bytes = b"staged concurrently by an editor\n"
    unrelated.write_bytes(staged_bytes)

    def failing_commit(argv: list[str]) -> CommandOutcome:
        if "commit-tree" in argv:
            fleet.git("add", "README.md")
            return CommandOutcome(tuple(argv), exit_status=1, stderr="injected failure")
        return au.run_argv(argv)

    def attempt() -> au.WriteResult:
        if kind == "declaration":
            return au.write_declaration(
                fleet.residents,
                "test-agent",
                edited(declaration_of(fleet), summary="A refused edit."),
                request_id=REQUEST_ID,
                principal=PRINCIPAL,
                skills_dir=fleet.skills,
                git=failing_commit,
            )
        return au.write_skill(
            fleet.residents,
            fleet.skills,
            au.SkillDocument(name="daily-summary", description="A refused edit.", body="Do it.\n"),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
            git=failing_commit,
        )

    with pytest.raises(au.AuthoringError) as refused:
        attempt()

    assert refused.value.reason == "commit_failed"
    assert fleet.git("show", ":README.md").stdout.encode() == staged_bytes


@pytest.mark.parametrize("kind", ["declaration", "skill"])
def test_nothing_fallible_runs_after_the_commit_advances_head(
    fleet: ScratchRepo, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A successful commit cannot become a refusal followed by a worktree rollback."""
    committed = False
    real_revision_of = au.revision_of

    def observing_git(argv: list[str]) -> CommandOutcome:
        nonlocal committed
        outcome = au.run_argv(argv)
        if "commit" in argv and outcome.ok:
            committed = True
        return outcome

    def revision_before_commit_only(*paths: Path) -> str:
        if committed:
            raise OSError("injected post-commit read failure")
        return real_revision_of(*paths)

    monkeypatch.setattr(au, "revision_of", revision_before_commit_only)

    if kind == "declaration":
        result = au.write_declaration(
            fleet.residents,
            "test-agent",
            edited(declaration_of(fleet), summary="Committed exactly once."),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            skills_dir=fleet.skills,
            git=observing_git,
        )
    else:
        result = au.write_skill(
            fleet.residents,
            fleet.skills,
            au.SkillDocument(
                name="daily-summary", description="Committed exactly once.", body="Do it.\n"
            ),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
            git=observing_git,
        )

    assert result.commit.committed
    assert result.commit.sha == fleet.head()


@pytest.mark.parametrize("kind", ["declaration", "skill"])
def test_a_transient_head_resolution_failure_restores_authored_state(
    fleet: ScratchRepo, kind: str
) -> None:
    """An existing repo with an unreadable HEAD is not an unborn empty repository."""
    target = (
        fleet.residents / "test-agent" / MANIFEST_FILENAME
        if kind == "declaration"
        else fleet.skills / "daily-summary" / "SKILL.md"
    )
    before = target.read_bytes()
    head = fleet.head()

    def failing_head(argv: list[str]) -> CommandOutcome:
        if "rev-parse" in argv and "--verify" in argv and "HEAD" in argv:
            return CommandOutcome(tuple(argv), exit_status=128, stderr="injected transient failure")
        return au.run_argv(argv)

    def attempt() -> au.WriteResult:
        if kind == "declaration":
            return au.write_declaration(
                fleet.residents,
                "test-agent",
                edited(declaration_of(fleet), summary="Must be restored."),
                request_id=REQUEST_ID,
                principal=PRINCIPAL,
                skills_dir=fleet.skills,
                git=failing_head,
            )
        return au.write_skill(
            fleet.residents,
            fleet.skills,
            au.SkillDocument(
                name="daily-summary", description="Must be restored.", body="Do it.\n"
            ),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
            git=failing_head,
        )

    with pytest.raises(au.AuthoringError) as refused:
        attempt()

    assert refused.value.reason == "commit_failed"
    assert target.read_bytes() == before
    assert fleet.head() == head


def test_an_external_head_advance_cannot_be_overwritten_by_the_authored_tree(
    fleet: ScratchRepo,
) -> None:
    """Publishing is a HEAD compare-and-swap, so an outside commit wins intact."""
    old_head = fleet.head()
    external = fleet.root / "README.md"
    advanced = False

    def advancing_git(argv: list[str]) -> CommandOutcome:
        nonlocal advanced
        if "update-ref" in argv and not advanced:
            advanced = True
            external.write_text("the outside writer's bytes\n", encoding="utf-8")
            fleet.git("add", "README.md")
            fleet.git("commit", "-m", "external: advance HEAD")
        return au.run_argv(argv)

    with pytest.raises(au.AuthoringError) as refused:
        au.write_declaration(
            fleet.residents,
            "test-agent",
            edited(declaration_of(fleet), summary="Must not replace the external tree."),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            skills_dir=fleet.skills,
            git=advancing_git,
        )

    assert refused.value.reason == "commit_failed"
    assert fleet.head() != old_head
    assert fleet.git("log", "-1", "--format=%s").stdout.strip() == "external: advance HEAD"
    assert fleet.git("show", "HEAD:README.md").stdout == "the outside writer's bytes\n"
    assert yaml.safe_load(declaration_of(fleet).manifest_text)["summary"] != (
        "Must not replace the external tree."
    )


def test_an_external_same_target_commit_survives_refusal(fleet: ScratchRepo) -> None:
    """Rollback preserves a same-target writer that wins between write and HEAD CAS."""
    target = fleet.residents / "test-agent" / MANIFEST_FILENAME
    competitor = edited(declaration_of(fleet), summary="The outside writer won.").manifest_text
    advanced = False

    def advancing_git(argv: list[str]) -> CommandOutcome:
        nonlocal advanced
        if "update-ref" in argv and not advanced:
            advanced = True
            target.write_text(competitor, encoding="utf-8")
            fleet.git("add", str(target.relative_to(fleet.root)))
            fleet.git("commit", "-m", "external: replace the same target")
        return au.run_argv(argv)

    with pytest.raises(au.AuthoringError) as refused:
        au.write_declaration(
            fleet.residents,
            "test-agent",
            edited(declaration_of(fleet), summary="Steward's losing bytes."),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            skills_dir=fleet.skills,
            git=advancing_git,
        )

    assert refused.value.reason == "commit_failed"
    assert fleet.git("log", "-1", "--format=%s").stdout.strip() == (
        "external: replace the same target"
    )
    assert fleet.git("show", f"HEAD:{target.relative_to(fleet.root)}").stdout == competitor
    assert target.read_text(encoding="utf-8") == competitor


def test_unsafe_winner_survives_while_other_declaration_target_rolls_back(
    fleet: ScratchRepo,
) -> None:
    """A non-regular winner cannot mask publication failure or abort later rollback."""
    declaration = declaration_of(fleet)
    manifest = fleet.residents / "test-agent" / MANIFEST_FILENAME
    soul = fleet.residents / "test-agent" / declaration.soul_filename
    old_soul = soul.read_bytes()
    winner = fleet.root / "winner"
    winner.write_text("outside writer\n", encoding="utf-8")

    def failing_publication(argv: list[str]) -> CommandOutcome:
        if "update-ref" in argv:
            manifest.unlink()
            manifest.symlink_to(winner)
            return CommandOutcome(tuple(argv), exit_status=1, stderr="injected publication failure")
        return au.run_argv(argv)

    with pytest.raises(au.AuthoringError) as refused:
        au.write_declaration(
            fleet.residents,
            "test-agent",
            au.Declaration(
                manifest_text=edited(declaration, summary="Steward's losing bytes.").manifest_text,
                soul_text=f"{declaration.soul_text}\nSteward's losing soul.\n",
            ),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            skills_dir=fleet.skills,
            git=failing_publication,
        )

    assert refused.value.reason == "commit_failed"
    assert manifest.is_symlink()
    assert manifest.readlink() == winner
    assert soul.read_bytes() == old_soul


def test_one_restore_failure_does_not_mask_publication_failure_or_abort_later_restore(
    fleet: ScratchRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback is best-effort per target; the publication refusal remains authoritative."""
    declaration = declaration_of(fleet)
    manifest, soul = au.declaration_paths(fleet.residents, "test-agent", declaration.soul_filename)
    old_soul = soul.read_bytes()
    real_restore = au._FileState.restore

    def fail_manifest_restore(state: au._FileState) -> None:
        if state.path == manifest:
            raise OSError("injected restore failure")
        real_restore(state)

    monkeypatch.setattr(au._FileState, "restore", fail_manifest_restore)

    def failing_publication(argv: list[str]) -> CommandOutcome:
        if "update-ref" in argv:
            return CommandOutcome(tuple(argv), exit_status=1, stderr="injected publication failure")
        return au.run_argv(argv)

    with pytest.raises(au.AuthoringError) as refused:
        au.write_declaration(
            fleet.residents,
            "test-agent",
            au.Declaration(
                manifest_text=edited(declaration, summary="Steward's losing bytes.").manifest_text,
                soul_text=f"{declaration.soul_text}\nSteward's losing soul.\n",
            ),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            skills_dir=fleet.skills,
            git=failing_publication,
        )

    assert refused.value.reason == "commit_failed"
    assert soul.read_bytes() == old_soul


@pytest.mark.parametrize("kind", ["declaration", "skill"])
def test_a_writer_in_the_old_replace_capture_gap_survives_rollback(
    fleet: ScratchRepo, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Authored state is the installed intent, never bytes recaptured after replacement."""
    target = (
        fleet.residents / "test-agent" / MANIFEST_FILENAME
        if kind == "declaration"
        else fleet.skills / "daily-summary" / "SKILL.md"
    )
    competitor = (
        edited(declaration_of(fleet), summary="The gap writer won.").manifest_text
        if kind == "declaration"
        else "---\nname: daily-summary\ndescription: The gap writer won.\n---\n\nOutside.\n"
    )
    real_replace = Path.replace
    raced = False

    def racing_replace(source: Path, destination: Path) -> Path:
        nonlocal raced
        result = real_replace(source, destination)
        if destination == target and "steward-write" in source.name and not raced:
            raced = True
            target.write_text(competitor, encoding="utf-8")
        return result

    monkeypatch.setattr(Path, "replace", racing_replace)

    def failing_head(argv: list[str]) -> CommandOutcome:
        if "rev-parse" in argv and "--verify" in argv and "HEAD" in argv:
            return CommandOutcome(tuple(argv), exit_status=128, stderr="injected failure")
        return au.run_argv(argv)

    def attempt() -> au.WriteResult:
        if kind == "declaration":
            return au.write_declaration(
                fleet.residents,
                "test-agent",
                edited(declaration_of(fleet), summary="Steward's losing bytes."),
                request_id=REQUEST_ID,
                principal=PRINCIPAL,
                skills_dir=fleet.skills,
                git=failing_head,
            )
        return au.write_skill(
            fleet.residents,
            fleet.skills,
            au.SkillDocument("daily-summary", "Steward's losing bytes.", "Do it.\n"),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
            git=failing_head,
        )

    with pytest.raises(au.AuthoringError):
        attempt()

    assert raced
    assert target.read_text(encoding="utf-8") == competitor


@pytest.mark.parametrize("kind", ["declaration", "skill"])
def test_a_symlink_target_is_refused_without_touching_its_referent(
    fleet: ScratchRepo, kind: str
) -> None:
    """Neither declaration nor skill authoring follows a link presented as its target."""
    target = (
        fleet.residents / "test-agent" / MANIFEST_FILENAME
        if kind == "declaration"
        else fleet.skills / "daily-summary" / "SKILL.md"
    )
    requested = edited(declaration_of(fleet), summary="Must not follow the link.")
    referent = fleet.root / f"outside-{kind}.txt"
    referent.write_text("outside bytes stay put\n", encoding="utf-8")
    target.unlink()
    target.symlink_to(referent)
    head = fleet.head()

    def attempt() -> au.WriteResult:
        if kind == "declaration":
            return au.write_declaration(
                fleet.residents,
                "test-agent",
                requested,
                request_id=REQUEST_ID,
                principal=PRINCIPAL,
                skills_dir=fleet.skills,
            )
        return au.write_skill(
            fleet.residents,
            fleet.skills,
            au.SkillDocument("daily-summary", "Must not follow the link.", "No.\n"),
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
        )

    with pytest.raises(au.AuthoringError) as refused:
        attempt()

    assert refused.value.reason == "unsafe_target"
    assert target.is_symlink()
    assert referent.read_text(encoding="utf-8") == "outside bytes stay put\n"
    assert fleet.head() == head


@pytest.mark.parametrize("kind", ["declaration", "skill"])
def test_a_fifo_target_is_promptly_refused_without_opening_it_for_blocking_read(
    fleet: ScratchRepo, kind: str
) -> None:
    """Special files are rejected before an authoring worker can block or touch a device."""
    target = (
        fleet.residents / "test-agent" / MANIFEST_FILENAME
        if kind == "declaration"
        else fleet.skills / "daily-summary" / "SKILL.md"
    )
    declaration = edited(declaration_of(fleet), summary="Must not wait on the FIFO.")
    document = au.SkillDocument("daily-summary", "Must not wait on the FIFO.", "No.\n")
    target.unlink()
    os.mkfifo(target)
    head = fleet.head()
    index = (fleet.root / ".git" / "index").read_bytes()

    def attempt() -> au.WriteResult:
        if kind == "declaration":
            return write(fleet, "test-agent", declaration)
        return au.write_skill(
            fleet.residents,
            fleet.skills,
            document,
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
        )

    with pytest.raises(au.AuthoringError) as refused:
        attempt()

    assert refused.value.reason == "unsafe_target"
    assert stat.S_ISFIFO(target.lstat().st_mode)
    assert fleet.head() == head
    assert (fleet.root / ".git" / "index").read_bytes() == index


@pytest.mark.parametrize("kind", ["declaration", "skill"])
def test_pre_staged_identical_request_does_not_own_worktree_after_unrelated_head_advance(
    fleet: ScratchRepo, kind: str
) -> None:
    """A baseline index match is not an external write made during the transaction."""
    target = (
        fleet.residents / "test-agent" / MANIFEST_FILENAME
        if kind == "declaration"
        else fleet.skills / "daily-summary" / "SKILL.md"
    )
    old_worktree = target.read_bytes()
    declaration = edited(declaration_of(fleet), summary="The pre-staged request bytes.")
    document = au.SkillDocument("daily-summary", "The pre-staged request bytes.", "Do it.\n")
    requested = (
        declaration.manifest_text.encode() if kind == "declaration" else document.text().encode()
    )
    target.write_bytes(requested)
    fleet.git("add", str(target.relative_to(fleet.root)))
    target.write_bytes(old_worktree)
    index = (fleet.root / ".git" / "index").read_bytes()
    old_head = fleet.head()
    tree = fleet.git("rev-parse", f"{old_head}^{{tree}}").stdout.strip()
    external_head = fleet.git(
        "commit-tree", tree, "-p", old_head, "-m", "external: unrelated plumbing advance"
    ).stdout.strip()
    advanced = False

    def advancing_git(argv: list[str]) -> CommandOutcome:
        nonlocal advanced
        if "update-ref" in argv and not advanced:
            advanced = True
            fleet.git("update-ref", "HEAD", external_head, old_head)
        return au.run_argv(argv)

    def attempt() -> au.WriteResult:
        if kind == "declaration":
            return au.write_declaration(
                fleet.residents,
                "test-agent",
                declaration,
                request_id=REQUEST_ID,
                principal=PRINCIPAL,
                skills_dir=fleet.skills,
                git=advancing_git,
            )
        return au.write_skill(
            fleet.residents,
            fleet.skills,
            document,
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            created=False,
            git=advancing_git,
        )

    with pytest.raises(au.AuthoringError) as refused:
        attempt()

    assert refused.value.reason == "commit_failed"
    assert fleet.head() == external_head
    assert (
        fleet.git("show", f"HEAD:{target.relative_to(fleet.root)}").stdout.encode() == old_worktree
    )
    assert target.read_bytes() == old_worktree
    assert (fleet.root / ".git" / "index").read_bytes() == index
    assert fleet.git("show", f":{target.relative_to(fleet.root)}").stdout.encode() == requested


def test_an_identical_byte_external_commit_owns_the_target_after_cas_refusal(
    fleet: ScratchRepo,
) -> None:
    """Winning HEAD ownership, not byte difference, decides failed-publication rollback."""
    target = fleet.residents / "test-agent" / MANIFEST_FILENAME
    intended = edited(declaration_of(fleet), summary="Both writers chose these bytes.")
    advanced = False

    def advancing_git(argv: list[str]) -> CommandOutcome:
        nonlocal advanced
        if "update-ref" in argv and not advanced:
            advanced = True
            fleet.git("add", str(target.relative_to(fleet.root)))
            fleet.git("commit", "-m", "external: publish the identical target")
        return au.run_argv(argv)

    with pytest.raises(au.AuthoringError) as refused:
        au.write_declaration(
            fleet.residents,
            "test-agent",
            intended,
            request_id=REQUEST_ID,
            principal=PRINCIPAL,
            skills_dir=fleet.skills,
            git=advancing_git,
        )

    assert refused.value.reason == "commit_failed"
    assert fleet.git("log", "-1", "--format=%s").stdout.strip() == (
        "external: publish the identical target"
    )
    assert target.read_text(encoding="utf-8") == intended.manifest_text
    assert fleet.git("status", "--porcelain").stdout.strip() == ""


def test_the_receipt_oid_does_not_depend_on_ref_update_output(fleet: ScratchRepo) -> None:
    """The machine-readable commit-tree OID is retained when publication prints anything."""

    def noisy_update_ref(argv: list[str]) -> CommandOutcome:
        outcome = au.run_argv(argv)
        if "update-ref" in argv and outcome.ok:
            return CommandOutcome(tuple(argv), exit_status=0, stdout="not a commit summary\n")
        return outcome

    result = au.write_declaration(
        fleet.residents,
        "test-agent",
        edited(declaration_of(fleet), summary="An exact receipt."),
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        skills_dir=fleet.skills,
        git=noisy_update_ref,
    )

    assert re.fullmatch(r"[0-9a-f]{40,64}", result.commit.sha or "")
    assert result.commit.sha == fleet.head()


@pytest.mark.parametrize("stage_timing", ["pre-existing", "concurrent"])
def test_success_preserves_same_target_staging(fleet: ScratchRepo, stage_timing: str) -> None:
    """Steward refreshes only a target index entry nobody else staged."""
    target = fleet.residents / "test-agent" / MANIFEST_FILENAME
    staged = edited(declaration_of(fleet), summary="The operator's staged bytes.").manifest_text
    requested = edited(declaration_of(fleet), summary="Steward's committed bytes.")
    if stage_timing == "pre-existing":
        target.write_text(staged, encoding="utf-8")
        fleet.git("add", str(target.relative_to(fleet.root)))
        target.write_text(declaration_of(fleet).manifest_text, encoding="utf-8")

    staged_during_publish = False

    def staging_git(argv: list[str]) -> CommandOutcome:
        nonlocal staged_during_publish
        outcome = au.run_argv(argv)
        if (
            stage_timing == "concurrent"
            and "update-ref" in argv
            and outcome.ok
            and not staged_during_publish
        ):
            staged_during_publish = True
            target.write_text(staged, encoding="utf-8")
            fleet.git("add", str(target.relative_to(fleet.root)))
        return outcome

    result = au.write_declaration(
        fleet.residents,
        "test-agent",
        requested,
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        skills_dir=fleet.skills,
        git=staging_git,
    )

    assert result.commit.committed
    assert (
        yaml.safe_load(fleet.git("show", f":{target.relative_to(fleet.root)}").stdout)["summary"]
        == "The operator's staged bytes."
    )
    assert (
        yaml.safe_load(fleet.git("show", f"HEAD:{target.relative_to(fleet.root)}").stdout)[
            "summary"
        ]
        == "Steward's committed bytes."
    )


def test_concurrent_declaration_writers_serialize_revision_through_commit(
    fleet: ScratchRepo,
) -> None:
    """Only one request made against one revision may write and commit its bytes."""
    paths = au.declaration_paths(fleet.residents, "test-agent", "soul.md")
    revision = au.revision_of(*paths)
    entered_commit = threading.Event()
    release_commit = threading.Event()

    def slow_first_git(argv: list[str]) -> CommandOutcome:
        if threading.current_thread().name == "first" and "commit-tree" in argv:
            entered_commit.set()
            assert release_commit.wait(timeout=5)
        return au.run_argv(argv)

    outcomes: dict[str, object] = {}

    def writer(name: str, summary: str, git=au.run_argv) -> None:  # type: ignore[no-untyped-def]
        try:
            outcomes[name] = au.write_declaration(
                fleet.residents,
                "test-agent",
                edited(declaration_of(fleet), summary=summary),
                request_id=f"req-{name}",
                principal=PRINCIPAL,
                skills_dir=fleet.skills,
                expected_revision=revision,
                git=git,
            )
        except au.AuthoringError as exc:
            outcomes[name] = exc

    first = threading.Thread(
        target=writer, args=("first", "First bytes.", slow_first_git), name="first"
    )
    second = threading.Thread(target=writer, args=("second", "Second bytes."), name="second")
    first.start()
    assert entered_commit.wait(timeout=5)
    second.start()
    time.sleep(0.2)
    release_commit.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert isinstance(outcomes["first"], au.WriteResult)
    assert isinstance(outcomes["second"], au.AuthoringError)
    assert outcomes["second"].reason == "stale_revision"  # type: ignore[union-attr]
    assert yaml.safe_load(paths[0].read_text())["summary"] == "First bytes."
    assert "req-first" in fleet.git("log", "-1", "--format=%b").stdout


def test_concurrent_skill_writers_serialize_revision_through_commit(fleet: ScratchRepo) -> None:
    """Skill replacement has the same one-revision/one-commit boundary as declarations."""
    path = fleet.skills / "daily-summary" / "SKILL.md"
    revision = au.revision_of(path)
    entered_commit = threading.Event()
    release_commit = threading.Event()

    def slow_first_git(argv: list[str]) -> CommandOutcome:
        if threading.current_thread().name == "first-skill" and "commit-tree" in argv:
            entered_commit.set()
            assert release_commit.wait(timeout=5)
        return au.run_argv(argv)

    outcomes: dict[str, object] = {}

    def writer(name: str, description: str, git=au.run_argv) -> None:  # type: ignore[no-untyped-def]
        try:
            outcomes[name] = au.write_skill(
                fleet.residents,
                fleet.skills,
                au.SkillDocument(name="daily-summary", description=description, body="Do it.\n"),
                request_id=f"req-{name}",
                principal=PRINCIPAL,
                created=False,
                expected_revision=revision,
                git=git,
            )
        except au.AuthoringError as exc:
            outcomes[name] = exc

    first = threading.Thread(
        target=writer, args=("first", "First skill bytes.", slow_first_git), name="first-skill"
    )
    second = threading.Thread(target=writer, args=("second", "Second skill bytes."))
    first.start()
    assert entered_commit.wait(timeout=5)
    second.start()
    time.sleep(0.2)
    release_commit.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert isinstance(outcomes["first"], au.WriteResult)
    assert isinstance(outcomes["second"], au.AuthoringError)
    assert outcomes["second"].reason == "stale_revision"  # type: ignore[union-attr]
    document, _ = au.read_skill_document(fleet.skills, "daily-summary")
    assert document.description == "First skill bytes."
    assert "req-first" in fleet.git("log", "-1", "--format=%b").stdout


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


# --------------------------------------------------------------------------------------
# the library an edit is judged against is the one beside the real tree
# --------------------------------------------------------------------------------------


def test_an_edit_is_judged_against_the_library_beside_the_tree(
    scratch_repo: ScratchRepo, write_resident: ResidentWriter, write_skill: SkillWriter
) -> None:
    """A default skill grants itself to every resident — if the library is found at all.

    Found beside the *real* tree, that is. The candidate copy lives in a temp directory with
    no library beside it, and a validation that inferred the library from the copy judged
    every routine requiring a default skill as ungranted — so a resident living on the
    defaults, as Hob does, could not be saved at all. Seen on the burrow (warren#351).
    """
    for name in GRANTED:
        write_skill(name, root=scratch_repo.skills, defaults=True)
    manifest = valid_manifest()
    manifest["skills"] = []
    manifest["routines"][0]["requires"] = ["daily-summary"]
    write_resident(manifest, root=scratch_repo.residents)
    scratch_repo.git("add", "-A")
    scratch_repo.git("commit", "-m", "feat: a resident living on the defaults")
    declaration = declaration_of(scratch_repo)

    result = au.write_declaration(
        scratch_repo.residents,
        "test-agent",
        edited(declaration, summary="Judged against the library beside the tree."),
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
    )

    assert result.commit.committed


# --------------------------------------------------------------------------------------
# the push: the burrow's commits leave the burrow (warren#351)
# --------------------------------------------------------------------------------------

BURROW_BRANCH = "burrow/residents"
PUSH = au.PushTarget(remote="origin", branch=BURROW_BRANCH)


def with_remote(repo: ScratchRepo, tmp_path: Path) -> Path:
    """Give the scratch checkout an ``origin`` it can push to: a bare repo beside it."""
    return bare_origin(repo.root, tmp_path / "origin.git")


def pushed_write(repo: ScratchRepo, declaration: au.Declaration) -> au.WriteResult:
    """Run the write path the way the deployed API runs it: committed, then pushed."""
    return au.write_declaration(
        repo.residents,
        "test-agent",
        declaration,
        request_id=REQUEST_ID,
        principal=PRINCIPAL,
        skills_dir=repo.skills,
        push=PUSH,
    )


def test_an_accepted_edit_is_pushed_to_the_burrow_branch(
    fleet: ScratchRepo, tmp_path: Path
) -> None:
    """The commit is the record; the push is what keeps that record off a NAS with no backup."""
    remote = with_remote(fleet, tmp_path)

    result = pushed_write(fleet, edited(declaration_of(fleet), summary="Pushed as well."))

    assert result.commit.committed
    assert result.commit.pushed is True
    assert branch_head(remote, BURROW_BRANCH) == result.commit.sha == fleet.head()
    assert "pushed to origin burrow/residents" in result.commit.note
    assert result.commit.to_dict()["pushed"] is True


def test_a_push_that_fails_never_fails_the_write(fleet: ScratchRepo, tmp_path: Path) -> None:
    """A burrow that cannot reach GitHub answers "committed, not pushed" and carries on."""
    fleet.git("remote", "add", "origin", str(tmp_path / "nowhere.git"))

    result = pushed_write(fleet, edited(declaration_of(fleet), summary="Kept locally."))

    assert result.commit.committed
    assert result.commit.pushed is False
    assert fleet.head() == result.commit.sha
    assert "committed as" in result.commit.note
    assert "NOT pushed to origin burrow/residents" in result.commit.note
    assert result.commit.to_dict()["pushed"] is False
    data = yaml.safe_load((fleet.residents / "test-agent" / MANIFEST_FILENAME).read_text())
    assert data["summary"] == "Kept locally."


def test_a_push_is_never_forced(fleet: ScratchRepo, tmp_path: Path) -> None:
    """History somebody else put on the branch is not steward's to overwrite."""
    remote = with_remote(fleet, tmp_path)
    fleet.git("commit", "--allow-empty", "-m", "chore: written somewhere else")
    fleet.git("push", "--quiet", "origin", f"HEAD:refs/heads/{BURROW_BRANCH}")
    elsewhere = fleet.head()
    fleet.git("reset", "--hard", "--quiet", "HEAD~1")

    result = pushed_write(fleet, edited(declaration_of(fleet), summary="Diverged."))

    assert result.commit.committed
    assert result.commit.pushed is False
    assert branch_head(remote, BURROW_BRANCH) == elsewhere


def test_nothing_is_pushed_when_nothing_was_committed(fleet: ScratchRepo, tmp_path: Path) -> None:
    """A converged save has no commit to push, and does not go to the network to say so."""
    remote = with_remote(fleet, tmp_path)

    result = pushed_write(fleet, declaration_of(fleet))

    assert not result.commit.committed
    assert result.commit.pushed is None
    assert branch_head(remote, BURROW_BRANCH) is None


def test_a_write_with_no_push_configured_reports_no_push(fleet: ScratchRepo) -> None:
    """``pushed: null`` is "there was nothing to push to", distinct from a push that failed."""
    result = write(fleet, "test-agent", edited(declaration_of(fleet), summary="Local only."))

    assert result.commit.committed
    assert result.commit.pushed is None
    assert "pushed" not in result.commit.note


def test_a_push_is_bounded_and_pushes_head_by_name(fleet: ScratchRepo) -> None:
    """One bounded ``git push`` of HEAD to the branch by its full ref: no force, no guessing."""
    seen: list[tuple[list[str], float | None]] = []

    def observing_git(argv: list[str], timeout_s: float | None = None) -> CommandOutcome:
        seen.append((list(argv), timeout_s))
        return CommandOutcome(argv=tuple(argv), exit_status=0)

    report = au.push_commit(fleet.root, PUSH, git=observing_git)

    assert report.pushed
    assert seen == [
        (
            [
                "git",
                "-C",
                str(fleet.root),
                "push",
                "--quiet",
                "origin",
                "HEAD:refs/heads/burrow/residents",
            ],
            au.PUSH_TIMEOUT_S,
        )
    ]
