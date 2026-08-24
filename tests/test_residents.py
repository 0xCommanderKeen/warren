"""The residents actually shipped in this repo must always validate."""

from conftest import RESIDENTS_DIR
from steward import manifest as m


def test_the_residents_tree_validates() -> None:
    result = m.validate_tree(RESIDENTS_DIR)
    assert result.ok, "\n".join(d.render() for d in result.diagnostics)
    assert {resident.id for resident in result.residents} == {"life-agent", "burrow-builder"}


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
    assert {grant.id for grant in hob.manifest.app_grants} >= {"gmail", "burrow"}
    assert all(not routine.enabled for routine in hob.manifest.routines), (
        "routines must stay disabled until the scheduler exists — the village never lies"
    )


def test_maren_is_project_scoped() -> None:
    maren = m.load_manifest(RESIDENTS_DIR / "burrow-builder" / "manifest.yaml")
    assert maren.manifest.agent_id is None
    assert maren.manifest.project == "burrow"
    assert maren.manifest.soul.name == "Maren"
    assert maren.manifest.soul.char == "Hunter"
    assert maren.manifest.soul.accent == "#4f7d5b"
    assert maren.manifest.soul.role == "village builder"
    assert isinstance(maren.manifest.charter.escalation, str)


def test_shipped_souls_have_voices_within_the_cap() -> None:
    for resident in m.validate_tree(RESIDENTS_DIR).residents:
        assert resident.soul.voice, f"{resident.id} has no ## Voice section"
        assert len(resident.soul.voice) <= m.VOICE_MAX_CHARS


def test_shipped_manifests_grant_only_declared_skills() -> None:
    for resident in m.validate_tree(RESIDENTS_DIR).residents:
        granted = {skill.id for skill in resident.manifest.skills}
        for routine in resident.manifest.routines:
            assert set(routine.requires) <= granted
