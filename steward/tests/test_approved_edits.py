"""What a decision authorises, and what it refuses to authorise (warren#437)."""

import copy
from collections.abc import Callable
from typing import Any

import pytest

from steward import events as ev
from steward.approved_edits import (
    GRANT_SKILL_ACTION,
    GrantSkillEdit,
    UnapprovedEditError,
    approved_edit,
)
from steward.store import ApprovalRecord

NOW = "2026-09-04T09:00:00Z"
LATER = "2026-09-04T11:00:00Z"


def _record(**overrides: Any) -> ApprovalRecord:  # noqa: ANN401 — record fields
    """Build one decided approval, in the shape the escalate knock leaves behind."""
    fields: dict[str, Any] = {
        "request_id": "req-1",
        "agent_id": "claude-code:karen",
        "project": "karen",
        "action": GRANT_SKILL_ACTION,
        "message": "Grant series-detection to shelf-worker?",
        "detail": {"resident": "shelf-worker", "skill": "series-detection"},
        "options": ("approve", "deny"),
        "status": "resolved",
        "created_at": NOW,
        "resident": "karen",
        "decision": "approve",
        "decided_by": "miha",
        "decided_at": NOW,
    }
    return ApprovalRecord(**(fields | overrides))


def _read(record: ApprovalRecord | None, **overrides: Any) -> Any:  # noqa: ANN401
    settings: dict[str, Any] = {
        "request_id": "req-1",
        "presented_by": "karen",
        "writing_to": "shelf-worker",
        "now": LATER,
    }
    return approved_edit(record, **(settings | overrides))


def _manifest(*skills: Any) -> dict[str, Any]:  # noqa: ANN401 — either grant spelling
    return {
        "id": "shelf-worker",
        "charter": {"mission": "Shelve the books.", "rules": ["Never delete a file."]},
        "skills": list(skills),
    }


# -- what the decision has to be -------------------------------------------------------


def test_an_approved_unspent_request_names_the_edit_it_authorises() -> None:
    edit = _read(_record())

    assert isinstance(edit, GrantSkillEdit)
    assert (edit.resident, edit.skill) == ("shelf-worker", "series-detection")
    assert edit.act == "grant skill 'series-detection' to 'shelf-worker'"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"resident": "hob"}, "raised by hob", id="somebody else's approval"),
        pytest.param({"resident": ""}, "raised by steward itself", id="steward's own knock"),
        pytest.param({"action": "send_email"}, "'send_email'", id="a yes about another act"),
        pytest.param(
            {"status": "pending", "decision": None, "decided_by": None, "decided_at": None},
            "still waiting on a human",
            id="nobody has answered yet",
        ),
        pytest.param({"decision": "deny"}, "answered 'deny' by miha", id="a no"),
        pytest.param(
            {"decision": "deny", "decided_by": "expiry"},
            "answered 'deny' by expiry",
            id="deny-by-default",
        ),
        pytest.param({"decision": "edit"}, "answered 'edit'", id="an edited detail"),
        pytest.param({"expires_at": NOW}, f"expired at {NOW}", id="a yes that has run out"),
        pytest.param(
            {"consumed_at": NOW, "consumed_by": "write-1"},
            "already spent",
            id="a yes already spent",
        ),
    ],
)
def test_a_decision_that_is_not_a_live_unspent_yes_opens_nothing(
    overrides: dict[str, Any], expected: str
) -> None:
    with pytest.raises(UnapprovedEditError, match=expected):
        _read(_record(**overrides))


def test_an_approval_nobody_raised_is_named_rather_than_crashed_on() -> None:
    with pytest.raises(UnapprovedEditError, match="no approval request 'req-1'"):
        _read(None)


def test_an_approval_about_another_resident_does_not_open_this_declaration() -> None:
    with pytest.raises(UnapprovedEditError, match="this edit writes 'hob'"):
        _read(_record(), writing_to="hob")


def test_a_deadline_still_ahead_leaves_the_decision_good() -> None:
    """The freshness bound is a real bound, not a check that can never fail."""
    assert _read(_record(expires_at=LATER), now=NOW).resident == "shelf-worker"


def test_a_knock_that_named_no_skill_is_a_refusal_rather_than_a_traceback() -> None:
    with pytest.raises(UnapprovedEditError, match="names no skill"):
        _read(_record(detail={"resident": "shelf-worker"}))


def test_the_bound_a_deadline_puts_on_an_answer_is_read_from_the_clock_it_is_given() -> None:
    """The route hands it steward's own clock, so the test can hand it a datetime too."""
    record = _record(expires_at=ev.utc_now_iso())

    with pytest.raises(UnapprovedEditError, match="expired at"):
        _read(record, now=ev.utc_now_iso())


# -- what the document has to be -------------------------------------------------------


def _edit() -> GrantSkillEdit:
    return GrantSkillEdit(resident="shelf-worker", skill="series-detection")


def test_the_one_approved_line_added_to_the_grants_already_there_is_the_match() -> None:
    current = _manifest("read-shelf", {"id": "write-journal", "note": "end of run"})
    candidate = copy.deepcopy(current)
    candidate["skills"].append({"id": "series-detection", "note": "Reads the series index."})

    _edit().check(current, candidate)


def test_the_first_grant_a_resident_holds_is_a_match_too() -> None:
    _edit().check(_manifest(), _manifest("series-detection"))


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(
            lambda m: m["charter"].__setitem__("mission", "Something else entirely."),
            "also changes charter",
            id="a charter rewritten alongside the grant",
        ),
        pytest.param(
            lambda m: m.__setitem__("session_grants", ["skills.write"]),
            "also changes session_grants",
            id="a door opened alongside the grant",
        ),
        pytest.param(
            lambda m: m.pop("charter"),
            "also changes charter",
            id="a charter dropped alongside the grant",
        ),
        pytest.param(
            lambda m: m["skills"].append("read-invoices"),
            "does not leave the 2 grant",
            id="two skills where one was approved",
        ),
        pytest.param(
            lambda m: m["skills"].__setitem__(-1, "read-invoices"),
            "grants 'read-invoices'",
            id="a different skill than the one approved",
        ),
        pytest.param(
            lambda m: m["skills"].__setitem__(0, "shelf-reader"),
            "does not leave the 2 grant",
            id="a grant already held, rewritten",
        ),
        pytest.param(
            lambda m: m["skills"].__setitem__(1, {"id": "write-journal", "note": "changed"}),
            "does not leave the 2 grant",
            id="another grant's note, rewritten",
        ),
        pytest.param(
            lambda m: m["skills"].reverse(),
            "does not leave the 2 grant",
            id="the list reordered around the new line",
        ),
        pytest.param(
            lambda m: m["skills"].pop(0),
            "does not leave the 2 grant",
            id="a grant removed alongside the one added",
        ),
        pytest.param(
            lambda m: m.__setitem__("skills", "series-detection"),
            "is not a list",
            id="skills written as one string",
        ),
    ],
)
def test_any_difference_beyond_the_approved_line_is_refused(
    mutate: Callable[[dict[str, Any]], object], expected: str
) -> None:
    current = _manifest("read-shelf", {"id": "write-journal", "note": "end of run"})
    candidate = copy.deepcopy(current)
    candidate["skills"].append({"id": "series-detection", "note": "Reads the series index."})
    mutate(candidate)

    with pytest.raises(UnapprovedEditError, match=expected):
        _edit().check(current, candidate)


def test_a_declaration_identical_to_the_one_on_disk_grants_nothing_and_is_refused() -> None:
    """A no-op PUT would otherwise spend a human's yes on nothing at all."""
    current = _manifest("read-shelf")

    with pytest.raises(UnapprovedEditError, match="does not leave the 1 grant"):
        _edit().check(current, copy.deepcopy(current))
