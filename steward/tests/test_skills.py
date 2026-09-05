"""The skills library: what parses, what a resident actually holds, and what lands on disk."""

import os
from pathlib import Path

import pytest

from conftest import REPO_ROOT, ResidentWriter, SkillWriter, valid_manifest
from steward import approvals as ap
from steward import manifest as m
from steward import prompt as p
from steward import skills as sk

LIBRARY = REPO_ROOT / "skills"


def library(tmp_path: Path) -> sk.SkillLibrary:
    return sk.load_library(tmp_path / "skills")


def manifest_with(*granted: str) -> m.ResidentManifest:
    data = valid_manifest()
    data["skills"] = list(granted)
    data["routines"] = []
    return m.ResidentManifest.model_validate(data)


# ------------------------------------------------------------------------------- parsing


def test_a_skill_parses_into_name_description_and_body(write_skill: SkillWriter) -> None:
    path = write_skill("research", description="How to research honestly.", body="Cite it.")
    skill, diagnostics = sk.load_skill(path.parent)
    assert diagnostics == []
    assert skill is not None
    assert (skill.name, skill.description, skill.body) == (
        "research",
        "How to research honestly.",
        "Cite it.",
    )
    assert skill.default is False
    assert skill.path == path


def test_defaults_true_marks_a_skill_as_part_of_the_default_set(write_skill: SkillWriter) -> None:
    skill, _ = sk.load_skill(write_skill("write-journal", defaults=True).parent)
    assert skill is not None
    assert skill.default is True


def test_a_name_that_disagrees_with_its_directory_fails(write_skill: SkillWriter) -> None:
    path = write_skill("research", text="---\nname: reserach\ndescription: Typo.\n---\n\nBody.\n")
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    problems = [d.problem for d in diagnostics]
    assert any("lives in 'research'" in problem for problem in problems)
    assert diagnostics[0].example == "name: research"


def test_a_skill_without_a_description_fails(write_skill: SkillWriter) -> None:
    path = write_skill("research", text="---\nname: research\n---\n\nBody.\n")
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert diagnostics[0].field_path == "description"
    assert "required field is missing" in diagnostics[0].problem


def test_a_skill_without_a_name_fails(write_skill: SkillWriter) -> None:
    path = write_skill("research", text="---\ndescription: Nameless.\n---\n\nBody.\n")
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert diagnostics[0].field_path == "name"
    assert diagnostics[0].example == "name: research"


def test_a_skill_without_frontmatter_fails(write_skill: SkillWriter) -> None:
    path = write_skill("research", text="Just some prose, no frontmatter.\n")
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert "no --- frontmatter block" in diagnostics[0].problem


def test_a_skill_with_an_empty_body_fails(write_skill: SkillWriter) -> None:
    path = write_skill("research", body="   \n")
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert diagnostics[0].field_path == "body"


def test_an_over_cap_body_fails_with_the_number_in_the_diagnostic(
    write_skill: SkillWriter,
) -> None:
    path = write_skill("research", body="verbose " * 2000)
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert str(sk.BODY_MAX_CHARS) in diagnostics[0].problem
    assert "splitting into two skills" in diagnostics[0].problem


def test_an_over_cap_description_fails(write_skill: SkillWriter) -> None:
    path = write_skill("research", description="long " * 200)
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert diagnostics[0].field_path == "description"


def test_a_non_slug_name_fails(write_skill: SkillWriter) -> None:
    path = write_skill("research", text="---\nname: Research Me\ndescription: No.\n---\n\nBody.\n")
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert "is not a slug" in diagnostics[0].problem


def test_unknown_frontmatter_keys_fail(write_skill: SkillWriter) -> None:
    path = write_skill(
        "research", text="---\nname: research\ndescription: X.\nversion: 2\n---\n\nBody.\n"
    )
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert "unknown frontmatter key(s) ['version']" in diagnostics[0].problem


def test_a_non_boolean_defaults_fails(write_skill: SkillWriter) -> None:
    path = write_skill(
        "research", text="---\nname: research\ndescription: X.\ndefaults: sometimes\n---\n\nB.\n"
    )
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert diagnostics[0].field_path == "defaults"


def test_broken_yaml_frontmatter_is_a_diagnostic_not_a_crash(write_skill: SkillWriter) -> None:
    path = write_skill("research", text="---\nname: [unclosed\n---\n\nBody.\n")
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert "not valid YAML" in diagnostics[0].problem


def test_frontmatter_that_is_not_a_mapping_fails(write_skill: SkillWriter) -> None:
    path = write_skill("research", text="---\n- research\n---\n\nBody.\n")
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert "must be a mapping" in diagnostics[0].problem


def test_a_missing_skill_file_is_read_as_a_diagnostic(tmp_path: Path) -> None:
    skill, diagnostics = sk.load_skill(tmp_path / "nowhere")
    assert skill is None
    assert "cannot read skill" in diagnostics[0].problem


# --------------------------------------------------------------------------- credentials


@pytest.mark.parametrize(
    "body",
    [
        "Use sk-abcdefghijklmnopqrstuvwxyz012345 to authenticate.",
        "Log in at https://hob:hunter2fortress@mail.example.com/inbox.",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_an_inline_secret_in_a_skill_body_fails(write_skill: SkillWriter, body: str) -> None:
    """A skill is instructions. Instructions never need a secret in them."""
    path = write_skill("read-inbox", body=body)
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert "document contains" in diagnostics[0].problem


def test_a_credential_shaped_frontmatter_key_fails(write_skill: SkillWriter) -> None:
    path = write_skill(
        "read-inbox",
        text="---\nname: read-inbox\ndescription: X.\napi_key: whatever\n---\n\nBody.\n",
    )
    skill, diagnostics = sk.load_skill(path.parent)
    assert skill is None
    assert any("credential-shaped" in d.problem for d in diagnostics)


# ------------------------------------------------------------------------------- library


def test_the_library_is_sorted_and_addressable_by_name(write_skill: SkillWriter, tmp_path) -> None:
    write_skill("research")
    write_skill("errands")
    loaded = library(tmp_path)
    assert loaded.names == ("errands", "research")
    assert loaded.get("errands") is not None
    assert loaded.get("nothing") is None
    assert "research" in loaded
    assert len(loaded) == 2
    assert [skill.name for skill in loaded] == ["errands", "research"]


def test_a_directory_without_a_skill_file_is_a_complaint(tmp_path: Path) -> None:
    (tmp_path / "skills" / "empty").mkdir(parents=True)
    loaded = library(tmp_path)
    assert loaded.configured is True
    assert "has no SKILL.md" in loaded.diagnostics[0].problem


def test_an_absent_library_is_unconfigured_rather_than_broken(tmp_path: Path) -> None:
    """A residents tree with no skills/ validates exactly as it did before #12."""
    for absent in (None, tmp_path / "nothing-here"):
        loaded = sk.load_library(absent)
        assert loaded.configured is False
        assert loaded.diagnostics == ()
        assert sk.default_skills(loaded) == ()


def test_default_skills_dir_finds_the_library_beside_a_residents_tree(
    write_skill: SkillWriter, tmp_path: Path
) -> None:
    write_skill("research")
    assert sk.default_skills_dir(tmp_path / "residents") == tmp_path / "skills"
    assert sk.default_skills_dir(tmp_path / "elsewhere" / "residents") is None
    assert sk.library_for(tmp_path / "residents").names == ("research",)
    assert sk.library_for(tmp_path / "residents", tmp_path / "nothing").configured is False


# ---------------------------------------------------------------------------- resolution


def test_the_effective_set_is_defaults_first_then_grants(
    write_skill: SkillWriter, tmp_path: Path
) -> None:
    write_skill("write-journal", defaults=True)
    write_skill("daily-summary", defaults=True)
    write_skill("read-inbox")
    write_skill("write-blog-post")
    loaded = library(tmp_path)

    resolved = sk.effective_skills(manifest_with("read-inbox"), loaded)
    assert [skill.name for skill in resolved] == ["daily-summary", "write-journal", "read-inbox"]
    assert [skill.name for skill in sk.default_skills(loaded)] == ["daily-summary", "write-journal"]


def test_granting_a_default_again_does_not_duplicate_it(
    write_skill: SkillWriter, tmp_path: Path
) -> None:
    write_skill("write-journal", defaults=True)
    write_skill("read-inbox")
    resolved = sk.effective_skills(manifest_with("write-journal", "read-inbox"), library(tmp_path))
    assert [skill.name for skill in resolved] == ["write-journal", "read-inbox"]


def test_a_grant_the_library_does_not_have_is_missing_not_substituted(
    write_skill: SkillWriter, tmp_path: Path
) -> None:
    write_skill("read-inbox")
    loaded = library(tmp_path)
    manifest = manifest_with("read-inbox", "reed-inbox")
    assert sk.missing_skills(manifest, loaded) == ("reed-inbox",)
    assert [skill.name for skill in sk.effective_skills(manifest, loaded)] == ["read-inbox"]
    assert sk.effective_names(manifest, loaded) == ("read-inbox", "reed-inbox")


def test_nothing_is_missing_when_no_library_is_configured() -> None:
    assert sk.missing_skills(manifest_with("anything"), sk.SkillLibrary()) == ()


def test_the_unknown_grant_diagnostic_names_the_closest_match(
    write_skill: SkillWriter, tmp_path: Path
) -> None:
    write_skill("read-inbox")
    write_skill("read-calendar")
    diagnostics = sk.grant_diagnostics(
        manifest_with("read-inbx"), library(tmp_path), Path("manifest.yaml")
    )
    assert diagnostics[0].field_path == "skills[0].id"
    assert "is not in the skills library" in diagnostics[0].problem
    assert diagnostics[0].example == "id: read-inbox"


def test_an_unrecognisable_grant_lists_what_the_library_has(
    write_skill: SkillWriter, tmp_path: Path
) -> None:
    write_skill("read-inbox")
    diagnostics = sk.grant_diagnostics(
        manifest_with("zzzzzzzz"), library(tmp_path), Path("manifest.yaml")
    )
    assert "one of: read-inbox" in diagnostics[0].example


def test_describe_missing_says_what_the_library_holds(
    write_skill: SkillWriter, tmp_path: Path
) -> None:
    write_skill("read-inbox")
    message = sk.describe_missing("hob", ["errands"], library(tmp_path))
    assert "hob is granted 'errands'" in message
    assert "it holds: read-inbox" in message


# ------------------------------------------------------------------------- materializing


def granted(*names: str) -> list[sk.Skill]:
    return [
        sk.Skill(name=name, description=f"{name} does a thing.", body="Do it.") for name in names
    ]


def test_materializing_writes_one_skill_file_per_skill(tmp_path: Path) -> None:
    result = sk.materialize(granted("research", "errands"), tmp_path, ".claude/skills")
    assert sorted(result.written) == ["errands", "research"]
    written = (tmp_path / ".claude/skills/research/SKILL.md").read_text(encoding="utf-8")
    assert written.startswith("---\nname: research\n")
    assert "Do it." in written
    assert "written" in result.summary()


def test_materializing_twice_writes_nothing_the_second_time(tmp_path: Path) -> None:
    sk.materialize(granted("research"), tmp_path, ".claude/skills")
    target = tmp_path / ".claude/skills/research/SKILL.md"
    stamp = target.stat().st_mtime_ns
    again = sk.materialize(granted("research"), tmp_path, ".claude/skills")
    assert again.written == ()
    assert again.unchanged == ("research",)
    assert target.stat().st_mtime_ns == stamp


def test_a_changed_skill_body_is_rewritten(tmp_path: Path) -> None:
    sk.materialize(granted("research"), tmp_path, ".claude/skills")
    improved = [sk.Skill(name="research", description="research does a thing.", body="Do it well.")]
    result = sk.materialize(improved, tmp_path, ".claude/skills")
    assert result.written == ("research",)
    assert "Do it well." in (tmp_path / ".claude/skills/research/SKILL.md").read_text("utf-8")


def test_rewriting_a_hardlinked_skill_does_not_clobber_the_external_inode(tmp_path: Path) -> None:
    victim = tmp_path / "outside.txt"
    victim.write_text("precious", encoding="utf-8")
    target = tmp_path / ".claude/skills/research/SKILL.md"
    target.parent.mkdir(parents=True)
    os.link(victim, target)

    sk.materialize(granted("research"), tmp_path, ".claude/skills")

    assert victim.read_text(encoding="utf-8") == "precious"
    assert target.read_text(encoding="utf-8").startswith("---\nname: research\n")
    assert target.stat().st_ino != victim.stat().st_ino


def test_a_skill_no_longer_granted_is_removed(tmp_path: Path) -> None:
    """Steward owns this directory: an ungranted skill is gone from the next session."""
    sk.materialize(granted("research", "errands"), tmp_path, ".claude/skills")
    result = sk.materialize(granted("research"), tmp_path, ".claude/skills")
    assert result.removed == ("errands",)
    assert not (tmp_path / ".claude/skills/errands").exists()
    assert (tmp_path / ".claude/skills/research/SKILL.md").is_file()


def test_a_stray_file_in_stewards_skills_directory_is_removed(tmp_path: Path) -> None:
    root = tmp_path / ".claude/skills"
    root.mkdir(parents=True)
    (root / "leftover.md").write_text("not a skill", encoding="utf-8")
    result = sk.materialize(granted("research"), tmp_path, ".claude/skills")
    assert result.removed == ("leftover.md",)


def test_an_unwritable_skills_directory_raises_a_skill_error(tmp_path: Path) -> None:
    (tmp_path / ".claude").write_text("in the way", encoding="utf-8")
    with pytest.raises(sk.SkillError, match="cannot write skill"):
        sk.materialize(granted("research"), tmp_path, ".claude/skills")


def test_a_symlinked_skills_directory_is_refused_and_the_victim_is_untouched(
    tmp_path: Path,
) -> None:
    """A symlinked skills dir pointing at a victim must never be pruned through (#64)."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("do not delete me", encoding="utf-8")

    workdir = tmp_path / "workdir"
    (workdir / ".claude").mkdir(parents=True)
    (workdir / ".claude" / "skills").symlink_to(victim, target_is_directory=True)

    with pytest.raises(sk.SkillError, match="symlink"):
        sk.materialize(granted("research"), workdir, ".claude/skills")

    assert (victim / "precious.txt").read_text(encoding="utf-8") == "do not delete me"


def test_a_skills_dir_reached_through_a_symlinked_parent_is_refused(tmp_path: Path) -> None:
    """Even when the *parent* is the symlink, the resolved dir escapes and is refused."""
    victim = tmp_path / "victim"
    (victim / "skills").mkdir(parents=True)
    (victim / "skills" / "precious.txt").write_text("keep me", encoding="utf-8")

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".claude").symlink_to(victim, target_is_directory=True)

    with pytest.raises(sk.SkillError, match="outside the workdir"):
        sk.materialize(granted("research"), workdir, ".claude/skills")

    assert (victim / "skills" / "precious.txt").read_text(encoding="utf-8") == "keep me"


def test_a_symlink_inside_the_skills_directory_is_unlinked_not_followed(tmp_path: Path) -> None:
    """A symlinked child is pruned by removing the link, never rmtree'd through (#64)."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep me", encoding="utf-8")

    root = tmp_path / ".claude" / "skills"
    root.mkdir(parents=True)
    (root / "sneaky").symlink_to(victim, target_is_directory=True)

    result = sk.materialize(granted("research"), tmp_path, ".claude/skills")

    assert "sneaky" in result.removed
    assert not (root / "sneaky").exists(), "the link is gone"
    assert (victim / "precious.txt").read_text(encoding="utf-8") == "keep me", "the target is not"


def test_a_materialized_skill_round_trips_through_the_parser(tmp_path: Path) -> None:
    """What lands on disk is what steward parsed: one representation, two readers."""
    original = sk.Skill(name="research", description="Cite things.", body="Read it.", default=True)
    sk.materialize([original], tmp_path, "skills")
    parsed, diagnostics = sk.load_skill(tmp_path / "skills" / "research")
    assert diagnostics == []
    assert parsed is not None
    assert (parsed.name, parsed.description, parsed.body, parsed.default) == (
        original.name,
        original.description,
        original.body,
        original.default,
    )


# ------------------------------------------------------------------------------ rendering


def test_a_skill_renders_with_its_name_and_description(write_skill: SkillWriter) -> None:
    skill, _ = sk.load_skill(write_skill("research", description="Cite things.").parent)
    assert skill is not None
    assert skill.render().startswith("# research — Cite things.")
    assert skill.as_dict()["default"] is False
    assert skill.as_dict()["body_chars"] == len(skill.body)


# ------------------------------------------------------------ the library shipped in this repo


def test_the_shipped_library_parses_and_has_a_default_set() -> None:
    loaded = sk.load_library(LIBRARY)
    assert loaded.diagnostics == (), "\n".join(d.render() for d in loaded.diagnostics)
    assert {skill.name for skill in sk.default_skills(loaded)} == {
        "escalate",
        "write-journal",
    }
    assert {
        "read-inbox",
        "read-calendar",
        "errands",
        "write-blog-post",
        "vault-keeper",
        "morning-digest",
        "write-skill",
        "raise-resident",
    } <= set(loaded.names)


def test_the_shipped_default_set_uses_less_than_a_quarter_of_the_prompt_budget() -> None:
    rendered = p.render_skills(sk.default_skills(sk.load_library(LIBRARY)))
    limit = p.SKILLS_MAX_CHARS // 4
    assert len(rendered) < limit, (
        f"the default set renders at {len(rendered)} characters; a quarter of the "
        f"{p.SKILLS_MAX_CHARS}-character prompt budget is {limit}"
    )


def test_every_shipped_skill_is_written_for_a_session_to_read() -> None:
    for skill in sk.load_library(LIBRARY):
        assert len(skill.body) <= sk.BODY_MAX_CHARS
        assert len(skill.description) <= sk.DESCRIPTION_MAX_CHARS
        assert skill.body.count("\n") >= 20, f"{skill.name} is too thin to be a skill"


def test_hob_holds_the_defaults_plus_his_own_grants() -> None:
    resident = m.load_manifest(REPO_ROOT / "residents" / "hob" / "manifest.yaml")
    resolved = sk.effective_skills(resident.manifest, sk.load_library(LIBRARY))
    names = [skill.name for skill in resolved]
    assert names[:2] == ["escalate", "write-journal"]
    assert names[2:] == ["vault-keeper", "morning-digest"]
    assert "write-blog-post" not in names, "Hob is granted what he was granted, and no more"


def test_a_resident_that_grants_nothing_still_holds_the_defaults(
    write_resident: ResidentWriter, write_skill: SkillWriter, tmp_path: Path
) -> None:
    write_skill("write-journal", defaults=True)
    data = valid_manifest()
    data["skills"] = []
    data["routines"] = []
    resident = m.load_manifest(write_resident(data))
    assert [s.name for s in sk.effective_skills(resident.manifest, library(tmp_path))] == [
        "write-journal"
    ]


# ------------------------------------------------- the vault keeper's skills (warren#383)


def test_the_vault_keeping_skills_are_granted_not_default() -> None:
    """Keeping a vault is one resident's job; the library offers it, nobody holds it unasked."""
    loaded = sk.load_library(LIBRARY)
    for name in ("vault-keeper", "morning-digest"):
        skill = loaded.get(name)
        assert skill is not None, f"{name} is not in the shipped library"
        assert not skill.default, f"{name} must be granted, not handed to every resident"


def test_hrs_two_crafts_are_granted_not_default() -> None:
    """Writing skills and drafting residents is one resident's job, not the fleet's.

    ``write-skill``'s own body says ``defaults: true`` is never the writer's to set
    (warren#412); shipping either of these as a default would be the library breaking its
    own rule on the way in.
    """
    loaded = sk.load_library(LIBRARY)
    for name in ("write-skill", "raise-resident"):
        skill = loaded.get(name)
        assert skill is not None, f"{name} is not in the shipped library"
        assert not skill.default, f"{name} must be granted, not handed to every resident"
        assert skill.description, f"{name} needs a description; it is what triggers it"


@pytest.mark.parametrize("name", ["write-skill", "raise-resident"])
def test_hrs_skill_grant_knock_spells_what_the_write_door_matches_on(name: str) -> None:
    """The literals in these bodies are a contract with `steward.approved_edits`.

    A session writes what the skill told it to write, and steward matches the action slug
    and the two detail keys exactly (warren#437). Rename either side alone and every knock
    Karen raises is refused at the door for a reason nobody can see from the skill — so the
    drift has to be a red test rather than a discovery.
    """
    skill = sk.load_library(LIBRARY).get(name)
    assert skill is not None
    assert f'action="{ap.GRANT_SKILL_ACTION}"' in skill.body
    for key in ("resident", "skill"):
        assert f'"{key}"' in skill.body, f"the knock must name {key} in its detail"
    assert "approval_request_id" in skill.body, "and the edit is made against the decision"


def test_vault_keeper_points_at_the_vaults_own_conventions_rather_than_copying_them() -> None:
    """The vault's CLAUDE.md is the authority; the skill points at it rather than copying it.

    A copy would drift from Miha's own conventions the first time they edited them
    (warren#383), so the phrases that are the vault's own must not appear in the skill.
    """
    skill = sk.load_library(LIBRARY).get("vault-keeper")
    assert skill is not None
    assert "/vault" in skill.body
    assert "CLAUDE.md" in skill.body
    for vault_only in ("Prime directive", "Update, don't duplicate", "Capture selectively"):
        assert vault_only not in skill.body, "the vault's own text belongs in the vault"
