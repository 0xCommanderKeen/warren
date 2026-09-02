"""The residents actually shipped in this repo must always validate."""

from conftest import PROJECT_AGENT_FIXTURE, REPO_ROOT, RESIDENTS_DIR
from steward import journal
from steward import manifest as m
from steward.skills import default_skills, effective_names, load_library, missing_skills


def test_the_residents_tree_validates() -> None:
    result = m.validate_tree(RESIDENTS_DIR)
    assert result.ok, "\n".join(d.render() for d in result.diagnostics)
    assert {resident.id for resident in result.residents} == {
        "life-agent",
        "pip",
    }
    assert "burrow-builder" not in {resident.id for resident in result.residents}
    assert len({resident.manifest.uid for resident in result.residents}) == len(result.residents)


def test_hob_is_the_life_agent() -> None:
    hob = m.load_manifest(RESIDENTS_DIR / "life-agent" / "manifest.yaml")
    assert hob.manifest.agent_id == "claude-code:life-agent"
    assert hob.manifest.soul.name == "Hob"
    assert hob.manifest.soul.char == "Monk"
    assert hob.manifest.soul.accent == "#a68a4f"
    assert hob.manifest.soul.role == "life bot"
    assert any("without explicit approval" in rule for rule in hob.manifest.charter.rules)
    escalation = hob.manifest.charter.escalation
    assert isinstance(escalation, m.Escalation)
    assert escalation.how == "needs_human"
    assert {grant.id for grant in hob.manifest.app_grants} >= {"gmail", "chronicle"}
    assert {routine.id for routine in hob.manifest.routines} == {
        "daily-summary",
        "inbox-read",
        "close-of-day",
    }
    assert all(routine.enabled for routine in hob.manifest.routines), (
        "Hob's routines are enabled now that the scheduler exists; they still only fire "
        "while `steward scheduler run` is up, so the village shows nothing when it is not"
    )
    assert all(routine.schedule_tz == "Europe/Ljubljana" for routine in hob.manifest.routines), (
        "'resident-local time' has to be written down: the NAS is not the household"
    )


def test_a_project_scoped_fixture_has_no_agent_identity() -> None:
    resident = m.load_manifest(PROJECT_AGENT_FIXTURE)
    assert resident.manifest.agent_id is None
    assert resident.manifest.project == "chronicle"
    assert resident.manifest.soul.name == "Project Agent"
    assert resident.manifest.soul.char == "Hunter"
    assert resident.manifest.soul.accent == "#4f7d5b"
    assert resident.manifest.soul.role == "test project worker"
    assert isinstance(resident.manifest.charter.escalation, str)


def test_hob_closes_his_own_day_and_nobody_elses_routine_does() -> None:
    hob = m.load_manifest(RESIDENTS_DIR / "life-agent" / "manifest.yaml")
    closers = [r for r in hob.manifest.routines if r.journal == m.CLOSE_OF_DAY]
    assert [r.id for r in closers] == ["close-of-day"], (
        "exactly one routine ends Hob's day; the flag says which, so nobody has to "
        "work it out from cron"
    )
    assert closers[0].schedule == "30 22 * * *"
    assert closers[0].schedule_tz == "Europe/Ljubljana"
    assert hob.manifest.memory.journal == "journal"
    assert hob.manifest.memory.journal_keep == m.DEFAULT_KEEP_ENTRIES


def test_no_shipped_resident_declares_a_journal_it_cannot_keep() -> None:
    for resident in m.validate_tree(RESIDENTS_DIR).residents:
        assert journal.journal_complaint(resident.manifest) is None, resident.id


def test_every_shipped_resident_declares_a_daily_cost_cap() -> None:
    """An absent cap and a cap that cannot trip read exactly the same from the console.

    Absent budgets stay legal and are reported honestly as ``"limit": null`` — but for a
    resident steward actually fires, absent means the pause machinery has no threshold to
    trip on, however wrong the day is going (steward #159).
    """
    for resident in m.validate_tree(RESIDENTS_DIR).residents:
        assert resident.manifest.budgets.daily_cost_usd is not None, (
            f"{resident.id} runs uncapped: nothing bounds what a day may cost"
        )


def test_shipped_souls_have_voices_within_the_cap() -> None:
    for resident in m.validate_tree(RESIDENTS_DIR).residents:
        assert resident.soul.voice, f"{resident.id} has no ## Voice section"
        assert len(resident.soul.voice) <= m.VOICE_MAX_CHARS


def test_shipped_manifests_require_only_skills_they_effectively_hold() -> None:
    """Effective, not granted: the default set counts, and a name outside it does not."""
    library = load_library(REPO_ROOT / "skills")
    for resident in m.validate_tree(RESIDENTS_DIR).residents:
        held = set(effective_names(resident.manifest, library))
        for routine in resident.manifest.routines:
            assert set(routine.requires) <= held, routine.id


def test_no_shipped_manifest_grants_a_skill_the_library_does_not_have() -> None:
    library = load_library(REPO_ROOT / "skills")
    for resident in m.validate_tree(RESIDENTS_DIR).residents:
        assert missing_skills(resident.manifest, library) == (), resident.id


def test_no_shipped_manifest_re_grants_a_default_skill() -> None:
    """A grant is what a resident has on top of the defaults; repeating one says nothing."""
    defaults = {skill.name for skill in default_skills(load_library(REPO_ROOT / "skills"))}
    for resident in m.validate_tree(RESIDENTS_DIR).residents:
        repeated = {grant.id for grant in resident.manifest.skills} & defaults
        assert repeated == set(), f"{resident.id} re-grants {sorted(repeated)}"


def test_project_fixture_may_hand_work_to_hob_and_hob_declares_a_door() -> None:
    """The handoff fixture and shipped receiver both declare their half."""
    sender = m.load_manifest(PROJECT_AGENT_FIXTURE)
    hob = m.load_manifest(RESIDENTS_DIR / "life-agent" / "manifest.yaml")

    assert sender.manifest.delegation.send is True
    assert sender.manifest.delegation.may_send_to("life-agent") is True
    assert sender.manifest.delegation.may_send_to("some-other-agent") is False
    assert hob.delegation_routes == ("handoff",)
    assert hob.manifest.delegation.send is False, "Hob receives; he escalates to a person"
    assert sender.delegation_routes == (), "nothing is handed back to the sender"
