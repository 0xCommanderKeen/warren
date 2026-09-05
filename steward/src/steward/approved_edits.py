"""Writes steward performs only against a decision a human actually made.

Every other session grant opens its door on the strength of a manifest key alone: a
resident holds ``skills.write``, so ``POST /skills`` is reachable for the whole life of
every run it starts. That is the right shape for an act whose blast radius is the act
itself — a skill nobody has been granted instructs nobody.

Putting a skill on another resident's manifest is not that act. It is an edit of the rules
that resident is held to, which is human-only by design, and no manifest key should be
able to buy it outright. So ``residents.grant_skill`` (warren#437) opens a door that is
still shut: a session holding it may write only while presenting the id of an approval it
raised, that a human answered ``approve``, that has not expired, that has not already been
spent — and only when the write it is making is the write that approval described.

**The check is about the approval, not about skills.** What a caller presents is a decision
plus a candidate document, and the question is whether the second is what the first says.
This module answers exactly that, and knows one edit shape while doing it. The next shape
— a budget raised, a routine's schedule moved — is a second entry in :data:`EDIT_SHAPES`
and a second ``check``, not a second door.

**Two halves, and they are different questions.** :func:`approved_edit` asks whether this
*decision* may open the door at all: who raised it, what it asked for, how it was answered,
whether it has been spent. The edit's own ``check`` asks whether this *document* is the one
that was approved. Both must pass, and neither substitutes for the other: an untouched
approval for a different skill is as much a refusal as the right document against a denied
request.

**Matching is on what the manifest says, not on its bytes.** Both documents are compared
as parsed YAML, so field order and comments may differ; every value may not. That is the
level the rules actually live at — validation reads the parsed manifest too — and a
comparison of bytes would refuse a session for reflowing a list it was told to add to.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from steward.approvals import GRANT_SKILL_ACTION
from steward.store import ApprovalRecord

__all__ = [
    "EDIT_SHAPES",
    "ApprovedEdit",
    "GrantSkillEdit",
    "UnapprovedEditError",
    "approved_edit",
]

#: The one answer that opens anything. ``edit`` is not accepted and the knock does not
#: offer it: an edited detail is a human writing a *different* request, and the honest way
#: to grant a different skill is to say so and let the session ask about that one.
DECISION_APPROVE = "approve"

#: The key holding the grants an edit may add to.
SKILLS_KEY = "skills"


class UnapprovedEditError(Exception):
    """Raised when a write is not the one a human approved. The message names the act."""


class ApprovedEdit(Protocol):
    """One edit an approved body describes, and the test of whether a document is it.

    Read-only members throughout, so a frozen shape satisfies it — which every shape here
    should be: an edit read out of a decision is a fact about that decision, not a value
    the door adjusts on its way to the write.
    """

    @property
    def resident(self) -> str:
        """The resident whose declaration this edit is about."""

    @property
    def act(self) -> str:
        """Name what was approved, as a phrase a refusal can be built around."""

    def check(self, current: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
        """Raise :class:`UnapprovedEditError` unless ``candidate`` is ``current`` plus this edit."""


@dataclass(frozen=True, slots=True)
class GrantSkillEdit:
    """One skill, added to one resident's ``skills``, and nothing else touched.

    The detail a knock carries is ``{"resident": …, "skill": …}``. Anything else in it is
    ignored on purpose — a knock is prose for a human as much as a record for steward, and
    a ``note`` explaining the grant is the reason a person can answer it at all.
    """

    resident: str
    skill: str

    @classmethod
    def read(cls, detail: Mapping[str, Any]) -> GrantSkillEdit:
        """Read the two names this edit is made of, or say which one the knock omitted."""
        named = {
            key: value.strip()
            for key in ("resident", "skill")
            if isinstance(value := detail.get(key), str) and value.strip()
        }
        missing = [key for key in ("resident", "skill") if key not in named]
        if missing:
            raise UnapprovedEditError(
                f"an approval to {GRANT_SKILL_ACTION} names the resident and the skill in "
                f"its detail, and this one names no {' and no '.join(missing)}"
            )
        return cls(resident=named["resident"], skill=named["skill"])

    @property
    def act(self) -> str:
        """The approved act, in the words a refusal repeats back."""
        return f"grant skill {self.skill!r} to {self.resident!r}"

    def check(self, current: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
        """Refuse any difference between the two manifests beyond the one skill line."""
        changed = _changed_keys(current, candidate, ignoring=SKILLS_KEY)
        if changed:
            raise UnapprovedEditError(
                f"the approval was to {self.act}, and this declaration also changes "
                f"{', '.join(changed)}"
            )
        held = current.get(SKILLS_KEY) or []
        offered = candidate.get(SKILLS_KEY) or []
        if not isinstance(held, list) or not isinstance(offered, list):
            raise UnapprovedEditError(
                f"the approval was to {self.act}, and `{SKILLS_KEY}` is not a list in one "
                "of the two declarations"
            )
        added = _one_addition(held, offered)
        if added is None:
            raise UnapprovedEditError(
                f"the approval was to {self.act}, and this declaration does not leave the "
                f"{len(held)} grant(s) already there untouched with exactly one added"
            )
        name = _skill_id(added)
        if name != self.skill:
            raise UnapprovedEditError(
                f"the approval was to {self.act}, and this declaration grants {name!r}"
            )


#: Every edit shape a decision can open the door to, **by the action its knock named**.
#: The action is what is matched, never the detail: an approval to ``send_email`` carrying
#: a mapping that happens to have ``resident`` and ``skill`` keys must not turn into a
#: declaration edit, or a session could repurpose any yes it was ever given. One entry
#: today; a second one is a second reader and a second ``check``, and the door, the
#: consumption and the refusal vocabulary are already general enough to carry it.
EDIT_SHAPES: Mapping[str, Callable[[Mapping[str, Any]], ApprovedEdit]] = {
    GRANT_SKILL_ACTION: GrantSkillEdit.read,
}


def approved_edit(
    record: ApprovalRecord | None,
    *,
    request_id: str,
    presented_by: str,
    writing_to: str,
    now: str,
) -> ApprovedEdit:
    """Return the edit this decision authorises, or refuse naming what is wrong with it.

    Provenance before content, and every branch here is one of the ways a session could
    otherwise have got a write out of an answer that was not about it: somebody else's
    approval, its own approval about somebody else, an ask still waiting, a no, a deadline
    that has passed, or a yes it already spent.

    ``now`` bounds the whole act rather than only the answer. An approval that has run past
    its deadline is refused even though it was approved in time: the deadline is how long
    the decision was good for, and a yes from last week arriving at the write door is a
    question worth asking a human again rather than one worth honouring quietly.
    """
    if record is None:
        raise UnapprovedEditError(
            f"there is no approval request {request_id!r} for this edit to be made against"
        )
    if record.resident != presented_by:
        raised_by = record.resident or "steward itself"
        raise UnapprovedEditError(
            f"approval {request_id!r} was raised by {raised_by}, not by {presented_by}; a "
            "session writes only against an answer to its own question"
        )
    read = EDIT_SHAPES.get(record.action)
    if read is None:
        raise UnapprovedEditError(
            f"approval {request_id!r} asked about {record.action!r}, which is not an edit "
            f"this door recognises; it opens for: {', '.join(sorted(EDIT_SHAPES))}"
        )
    if record.pending:
        raise UnapprovedEditError(f"approval {request_id!r} is still waiting on a human")
    if record.decision != DECISION_APPROVE:
        answered = f"answered {record.decision!r}" if record.decision else "never answered"
        by = f" by {record.decided_by}" if record.decided_by else ""
        raise UnapprovedEditError(
            f"approval {request_id!r} was {answered}{by}, and only an approve opens this door"
        )
    if record.expires_at is not None and record.expires_at <= now:
        raise UnapprovedEditError(
            f"approval {request_id!r} expired at {record.expires_at}; ask again rather than "
            "acting on an answer that has run out"
        )
    if record.consumed_at is not None:
        raise UnapprovedEditError(
            f"approval {request_id!r} was already spent on a write at {record.consumed_at}; "
            "one approval is one edit"
        )
    if not isinstance(record.detail, Mapping):
        raise UnapprovedEditError(
            f"approval {request_id!r} carries no detail this door can read; an edit is "
            "described by the names in a mapping, not by prose"
        )
    edit = read(record.detail)
    if edit.resident != writing_to:
        raise UnapprovedEditError(
            f"the approval was to {edit.act}, and this edit writes {writing_to!r}"
        )
    return edit


def _skill_id(entry: object) -> str | None:
    """Name the skill one ``skills`` entry grants, in either spelling the manifest takes."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Mapping):
        name = entry.get("id")
        return name if isinstance(name, str) else None
    return None


def _one_addition(current: list[Any], candidate: list[Any]) -> Any | None:  # noqa: ANN401
    """Return the single entry ``candidate`` adds to ``current``, or ``None``.

    Deletion at one index rather than a set difference, so the entries that were already
    there have to survive *unchanged and in order*: a candidate that adds one grant while
    rewriting another's note, or while reordering the list, leaves no index whose removal
    reproduces the original, and is refused like any other unapproved difference.
    """
    if len(candidate) != len(current) + 1:
        return None
    for index in range(len(candidate)):
        if candidate[:index] + candidate[index + 1 :] == current:
            return candidate[index]
    return None


_ABSENT = object()


def _changed_keys(
    current: Mapping[str, Any], candidate: Mapping[str, Any], *, ignoring: str
) -> list[str]:
    """Name every top-level key the two documents disagree about, added and removed too."""
    keys = (set(current) | set(candidate)) - {ignoring}
    return sorted(key for key in keys if current.get(key, _ABSENT) != candidate.get(key, _ABSENT))
