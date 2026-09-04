"""Delegation: a resident hands work to a neighbour, and steward is the only arbiter.

A resident working alone in a headless session sooner or later reaches work that is not
its own — a project agent wants background reading before touching the protocol, while a
receiver wants an errand written up by somebody who does that for a living. It cannot call the other
resident: they are separate processes, woken on separate schedules, and neither is
listening. So it asks steward to **deliver a letter**, and steward decides whether the
letter may be delivered at all.

**Both declarations have to agree, and steward checks both.**

- The sender's manifest must say ``delegation: {send: true}`` — and, when it names a
  ``to:`` allowlist, must name this receiver (:class:`steward.manifest.Delegation`).
- The receiver's manifest must declare a **route** of kind ``delegation`` with status
  ``active``, and the sender must name it (:attr:`steward.manifest.Route.accepts_delegation`).
  ``routes`` is already the answer to "how does work reach this resident", and a letter
  from a neighbour is work reaching it.

Neither half can waive the other, and neither is inferred from good intentions. Two more
guardrails are steward's alone, because no manifest can see far enough to enforce them:
a chain may not run deeper than :data:`DEFAULT_MAX_DEPTH`, and it may never revisit a
resident — ``A → B → A`` is refused, so two residents cannot hand one task back and forth
until the budget is gone.

**The mechanism is a protocol, not a function call**, exactly as approvals are
(:mod:`steward.approvals`). Two ways in, both landing in the same row of the same table:

1. **A block in the session's output**, parsed by :func:`extract_handoffs`::

       <delegate to="hob" route="inbox">
       {"title": "Check the errand list", "detail": "…"}
       </delegate>

2. **``steward delegate <from> --to … --route … --title …``** — a local, token-free CLI
   for a session with shell access that wants the handoff registered before its turn ends.

**A refusal is never silent.** The CLI and the API answer the caller with a structured
reason and write nothing. A block harvested out of a finished session has nobody left to
answer, so the refusal knocks: it becomes an approval request under
:data:`REJECTED_ACTION` (or :data:`UNREADABLE_ACTION` for a block steward could not read
at all), carrying the reason and the raw block, and a person hears about it — every time,
because both of those are catch-all actions and the refusal knock is exempt from the
repeat-deny guard. A resident that tried to hand work over and failed must never look like
a resident that had nothing to hand over.

**An accepted handoff is a task addressed to one resident.** It is written into the same
``jobs`` table the board uses, with an ``assignee``, and from there the board machinery
does the rest: the receiver picks it up on its own next wake-up, works it as an ordinary
provisioned session, and closes it with ``task_done``/``task_failed`` — every one of those
events carrying the ``parent_task_id``, so the whole chain is readable from the log.
Delivery is pull-based, like everything else here: nobody is woken up to receive a letter.
"""

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from typing import Any

from steward import approvals, prompt
from steward import events as ev
from steward.input_bounds import (
    DETAIL_MAX_CHARS,
    TITLE_MAX_CHARS,
    validate_identifier,
    validate_work_text,
)
from steward.manifest import (
    DELEGATION_ROUTE_KIND,
    Resident,
    closest_match,
    redact_secrets,
    retired_complaint,
)
from steward.store import STATUS_CLAIMED, JobRecord, Store
from steward.transitions.approval import ApprovalTransitions
from steward.transitions.delegation import DelegationTransitions

__all__ = [
    "BLOCK_CLOSE",
    "BLOCK_OPEN",
    "DEFAULT_MAX_DEPTH",
    "DETAIL_MAX_CHARS",
    "HUMAN_SENDER",
    "MAX_DEPTH_ENV",
    "REJECTED_ACTION",
    "TITLE_MAX_CHARS",
    "UNREADABLE_ACTION",
    "DelegationError",
    "Delegator",
    "Delivery",
    "Handoff",
    "answered_letters_preamble",
    "extract_handoffs",
    "max_depth",
    "origin_for",
]

log = logging.getLogger("steward.delegation")

#: The markers a session writes around a handoff in its final output.
BLOCK_OPEN = "<delegate"
BLOCK_CLOSE = "</delegate>"

#: How many hops a chain of delegated work may run before steward refuses. Small on
#: purpose: three residents deep is already a fleet doing something a human should have
#: been asked about, and every extra hop spends somebody's budget on somebody's behalf.
DEFAULT_MAX_DEPTH = 3

#: Where the fleet's depth cap is configured, so every entry point reads one number.
MAX_DEPTH_ENV = "STEWARD_MAX_DELEGATION_DEPTH"

#: The action recorded when a session tried to delegate and steward could not read it.
UNREADABLE_ACTION = "unreadable_delegation"

#: The action recorded when steward read the handoff and refused it.
REJECTED_ACTION = "rejected_delegation"

#: Who a handoff is recorded as sent by when a person asked for it over the API.
HUMAN_SENDER = "api"

# A worker's answer is context, not an archive. Keep each reply useful without letting one
# verbose session crowd the sender's charter out of its next prompt.
ANSWER_MESSAGE_MAX_CHARS = 4_000


def answered_letters_preamble(records: Sequence[JobRecord]) -> str | None:
    """Render terminal delegated tasks for the sender's next session."""
    if not records:
        return None
    answers: list[str] = []
    for record in records:
        message = redact_secrets(record.final_message).strip()
        if len(message) > ANSWER_MESSAGE_MAX_CHARS:
            message = message[: ANSWER_MESSAGE_MAX_CHARS - 1].rstrip() + "…"
        if not message:
            message = "(no final message)"
        receiver = record.assignee or "unknown receiver"
        answers.append(f"{record.title} — {receiver} — {record.status}\n{message}")
    return "\n\n".join(answers)


#: Why a handoff was refused. Every rejection carries exactly one of these, so a session,
#: a panel, or a test can key on the reason rather than parse the sentence.
UNKNOWN_RECIPIENT = "unknown_recipient"
RETIRED_RECIPIENT = "retired_recipient"
RETIRED_SENDER = "retired_sender"
SELF_DELEGATION = "self_delegation"
NOT_PERMITTED = "not_permitted"
RECIPIENT_NOT_ALLOWED = "recipient_not_allowed"
UNKNOWN_ROUTE = "unknown_route"
ROUTE_NOT_DELEGABLE = "route_not_delegable"
ROUTE_INACTIVE = "route_inactive"
MAX_DEPTH_EXCEEDED = "max_depth_exceeded"
UNKNOWN_PARENT = "unknown_parent"
CYCLE = "cycle"
UNREADABLE_BLOCK = "unreadable_block"

_BLOCK = re.compile(
    re.escape(BLOCK_OPEN) + r"(?P<attrs>[^>]*)>(?P<body>.*?)" + re.escape(BLOCK_CLOSE),
    re.DOTALL,
)
_ATTRIBUTE = re.compile(r'(?P<name>[a-z][a-z-]*)\s*=\s*"(?P<value>[^"]*)"')

KNOWN_ATTRIBUTES = frozenset({"to", "route"})


def max_depth(env: Mapping[str, str] | None = None) -> int:
    """Return the fleet's delegation depth cap: ``$STEWARD_MAX_DELEGATION_DEPTH`` or 3.

    One number, read in one place, so the CLI, the API, and the dispatcher cannot
    disagree about how deep a chain may run. ``0`` is a legitimate value and it is the
    kill switch: no handoff is ever hop zero, so nothing is delivered at all. Anything
    that is not a whole number ≥ 0 is a misconfiguration, and steward says so and falls
    back to the default rather than quietly running with a cap nobody chose.
    """
    source = os.environ if env is None else env
    raw = (source.get(MAX_DEPTH_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_DEPTH
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        log.warning(
            "%s=%r is not a depth; using the default of %d",
            MAX_DEPTH_ENV,
            raw,
            DEFAULT_MAX_DEPTH,
        )
        return DEFAULT_MAX_DEPTH
    return value


class DelegationError(Exception):
    """Raised when a handoff is refused, carrying the reason a caller can key on."""

    def __init__(self, reason: str, message: str) -> None:
        """Hold the structured reason alongside the sentence a person reads."""
        self.reason = reason
        super().__init__(message)


# --------------------------------------------------------------------------------------
# the grammar
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Handoff:
    """One parsed handoff, or one complaint about a block that could not be parsed."""

    raw: str
    to: str = ""
    route: str = ""
    title: str = ""
    detail: str = ""
    problem: str | None = None

    @property
    def ok(self) -> bool:
        """True when steward could read the handoff the session wrote."""
        return self.problem is None


def _read_attributes(attrs: str) -> dict[str, str]:
    """Parse the block's attributes, refusing anything this grammar does not define."""
    attributes = {m.group("name"): m.group("value") for m in _ATTRIBUTE.finditer(attrs)}
    unknown = sorted(set(attributes) - KNOWN_ATTRIBUTES)
    if unknown:
        known = ", ".join(sorted(KNOWN_ATTRIBUTES))
        raise DelegationError(
            UNREADABLE_BLOCK, f"unknown attribute(s) {unknown}; this block takes: {known}"
        )
    leftover = _ATTRIBUTE.sub("", attrs).strip()
    if leftover:
        raise DelegationError(
            UNREADABLE_BLOCK,
            f"could not read {leftover!r} as an attribute; every attribute is "
            f'name="value" with double quotes',
        )
    return attributes


def parse_body(body: str) -> tuple[str, str]:
    """Turn a block body into ``(title, detail)``, or say why it is not one.

    The body is a JSON object, because a handoff has two named parts and guessing which
    half of a paragraph was the title would be steward inventing the work. ``title`` is
    required — a letter with no subject line is one nobody can triage — and ``detail`` is
    optional but is the only thing the receiving session will actually be told beyond it.
    Any other key is refused rather than dropped: a sender that thought it was passing a
    deadline should not find out later that nobody read it.
    """
    text = body.strip()
    if not text:
        raise DelegationError(
            UNREADABLE_BLOCK,
            'the block needs a JSON body naming the work, e.g. {"title": "Check the '
            'errand list", "detail": "…"}',
        )
    try:
        loaded = json.loads(text)
    except ValueError as exc:
        raise DelegationError(
            UNREADABLE_BLOCK, f"the body is not JSON: {exc}; it must be a JSON object"
        ) from None
    if not isinstance(loaded, Mapping):
        raise DelegationError(UNREADABLE_BLOCK, "the body must be a JSON object")
    unknown = sorted(set(loaded) - {"title", "detail"})
    if unknown:
        raise DelegationError(
            UNREADABLE_BLOCK,
            f"unknown key(s) {unknown} in the body; a handoff carries title and detail, "
            f"and everything a receiver needs belongs in detail",
        )
    title = str(loaded.get("title") or "").strip()
    if not title:
        raise DelegationError(UNREADABLE_BLOCK, "the body needs a non-empty title")
    detail = loaded.get("detail") or ""
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    return title[:TITLE_MAX_CHARS], detail.strip()[:DETAIL_MAX_CHARS]


def _read_slug(attributes: Mapping[str, str], name: str, example: str) -> str:
    value = attributes.get(name, "").strip()
    if not value:
        raise DelegationError(
            UNREADABLE_BLOCK, f'the block needs a {name}, e.g. {name}="{example}"'
        )
    return value


def _read_block(raw: str, attrs: str, body: str) -> Handoff:
    """Build one handoff from a matched block, raising on anything unreadable."""
    attributes = _read_attributes(attrs)
    title, detail = parse_body(body)
    return Handoff(
        raw=raw,
        to=_read_slug(attributes, "to", "hob"),
        route=_read_slug(attributes, "route", "inbox"),
        title=title,
        detail=detail,
    )


def _parse_block(raw: str, attrs: str, body: str) -> Handoff:
    """Read one block, or come back carrying the complaint instead of the handoff."""
    try:
        return _read_block(raw, attrs, body)
    except DelegationError as exc:
        return Handoff(raw=raw, problem=str(exc))


def extract_handoffs(output: str) -> list[Handoff]:
    """Return every ``<delegate>`` block in a session's output, in order.

    Every block, not just the last: a session that asked two neighbours for two things
    asked twice, and dropping either would leave one of them quietly undone.

    An unparseable block comes back with :attr:`Handoff.problem` set rather than being
    skipped, and an opening marker with no closing one is itself reported — a truncated
    handoff is the likeliest shape of a session killed mid-sentence, and it must knock
    rather than vanish.
    """
    text = output or ""
    found = [
        _parse_block(match.group(0), match.group("attrs"), match.group("body"))
        for match in _BLOCK.finditer(text)
    ]
    consumed = _BLOCK.sub("", text)
    if BLOCK_OPEN in consumed:
        dangling = consumed[consumed.index(BLOCK_OPEN) :][:DETAIL_MAX_CHARS]
        found.append(
            Handoff(
                raw=dangling,
                problem=(
                    f"an opening {BLOCK_OPEN}…> has no closing {BLOCK_CLOSE}; the handoff "
                    f"was cut off, so steward cannot tell what was being handed over"
                ),
            )
        )
    return found


# --------------------------------------------------------------------------------------
# lineage and attribution
# --------------------------------------------------------------------------------------


def origin_for(parent: JobRecord | None, sender_id: str) -> str:
    """Name the accountable origin a delegated item rolls up to.

    Budget attribution is the whole point: every hop of a chain records the same origin,
    so what a fleet spent answering one question rolls up to that question rather than to
    whichever resident happened to be last in the line. The vocabulary is three prefixes:

    - ``task:<id>`` — the chain descends from a task on the board;
    - ``resident:<id>`` — a resident started it on its own initiative, in a routine or an
      unattached session;
    - ``human:<who>`` — a person asked for it directly.

    Inherited from the parent whenever there is one, so the root keeps the credit and the
    bill.
    """
    if parent is not None:
        return parent.origin or f"task:{parent.task_id}"
    return f"resident:{sender_id}" if sender_id != HUMAN_SENDER else f"human:{HUMAN_SENDER}"


@dataclass(frozen=True, slots=True)
class Delivery:
    """What one harvested handoff came to: a delivered letter, or a refusal that knocked."""

    handoff: Handoff
    task: JobRecord | None = None
    reason: str | None = None
    message: str | None = None
    #: The approval request a refusal raised, so a person hears about it.
    knock: approvals.ApprovalRecord | None = None

    @property
    def accepted(self) -> bool:
        """True when the handoff was enqueued into somebody's inbox."""
        return self.task is not None


# --------------------------------------------------------------------------------------
# the arbiter
# --------------------------------------------------------------------------------------


@dataclass
class Delegator:
    """Validates and enqueues handoffs. The one place a delegation is allowed or refused.

    Held by :class:`steward.board.Dispatcher` (so a session's block is harvested on the
    same rhythm approvals are), by ``steward delegate``, and by ``POST /delegate``. All
    three go through :meth:`delegate`, so "may this happen?" has one answer that does not
    depend on who asked.
    """

    residents: Sequence[Resident]
    store: Store
    emitter: ev.Emitter = field(default_factory=ev.NullEmitter)
    max_depth: int = field(default_factory=max_depth)

    # -- the seams a decision is carried out through -------------------------------------

    @cached_property
    def deliveries(self) -> DelegationTransitions:
        """Where an accepted handoff becomes a durable letter and a ``task_delegated``.

        Reached only past every check below. Everything this class refuses, it refuses
        before touching this property, which is what makes "a refusal writes nothing and
        emits nothing" a property of the shape rather than of remembering to return early.
        """
        return DelegationTransitions(store=self.store, emitter=self.emitter)

    @cached_property
    def knocks(self) -> ApprovalTransitions:
        """Where a *refused* handoff becomes a question for a person instead."""
        return ApprovalTransitions(store=self.store, emitter=self.emitter)

    # -- lookups -----------------------------------------------------------------------

    def resident(self, resident_id: str) -> Resident | None:
        """Return the resident with this id, or ``None`` when the tree has none."""
        return next((r for r in self.residents if r.id == resident_id), None)

    @property
    def receivers(self) -> tuple[str, ...]:
        """The ids of residents that declare a route work may be delegated into."""
        return tuple(sorted(r.id for r in self.residents if r.delegation_routes and not r.retired))

    # -- validation --------------------------------------------------------------------

    @staticmethod
    def _validate_input(handoff: Handoff, parent_task_id: str | None) -> None:
        try:
            validate_work_text(handoff.title, handoff.detail)
            validate_identifier(handoff.to, "to")
            validate_identifier(handoff.route, "route")
            if parent_task_id is not None:
                validate_identifier(parent_task_id, "parent_task_id")
        except ValueError as exc:
            raise DelegationError(UNREADABLE_BLOCK, str(exc)) from None

    def _find_receiver(self, handoff: Handoff, sender_id: str) -> Resident:
        receiver = self.resident(handoff.to)
        if receiver is None:
            close = closest_match(handoff.to, [r.id for r in self.residents])
            known = ", ".join(self.receivers) or "nobody"
            hint = f" — did you mean {close!r}?" if close else ""
            raise DelegationError(
                UNKNOWN_RECIPIENT,
                f"no valid resident {handoff.to!r} to delegate to{hint} "
                f"(residents accepting delegated work: {known})",
            )
        complaint = retired_complaint(receiver)
        if complaint is not None:
            # A retired resident gets its own reason code, not "unknown_recipient" (steward
            # #W21): a panel keying on the code must be able to tell a typo from a resident
            # that has left the village. The message still says which, so a human need not
            # guess, but the code no longer conflates "no such address" with "gone quiet".
            raise DelegationError(RETIRED_RECIPIENT, complaint)
        if receiver.id == sender_id:
            raise DelegationError(
                SELF_DELEGATION,
                f"{sender_id!r} cannot delegate to itself; that is one session pretending "
                f"to be two, and the work is already yours",
            )
        return receiver

    def _check_sender_active(self, sender: Resident | None) -> None:
        """Refuse a handoff from a retired resident (steward #67).

        Retirement was only ever checked on the *receiver*, so a resident marked retired
        could still hand work out — it stops firing and stops claiming, but a stray block in
        a last session's output, or a local ``steward delegate`` run against it, still
        delivered. A resident that has left the village delegates nothing on its way out.
        """
        if sender is None:
            return
        complaint = retired_complaint(sender)
        if complaint is not None:
            raise DelegationError(
                RETIRED_SENDER,
                f"{sender.id!r} is retired and may not delegate: {complaint}",
            )

    def _claimed_by(self, sender: Resident) -> list[JobRecord]:
        """Return the tasks this sender is working on right now — its in-flight lineage.

        A board notice records the ``claimant`` by agent id; a delegated letter records the
        resident it was addressed to as ``assignee``. Either is work this resident is
        currently holding, and therefore the true parent of anything it delegates.
        """
        return [
            job
            for job in self.store.jobs(STATUS_CLAIMED)
            if job.claimant == sender.agent_id or job.assignee == sender.id
        ]

    def _resolve_parent(
        self, sender: Resident | None, parent_task_id: str | None
    ) -> JobRecord | None:
        """Decide the real parent of this handoff, without trusting a supplied id (steward #67).

        Depth and cycle used to derive from a caller-supplied ``parent_task_id`` alone, so a
        session could escape both by simply omitting it — ``A → B → A`` was accepted and the
        depth cap was unreachable. The truth is the task the *sender* is working on: it is
        looked up here, and a supplied id is honoured only when it is that task (a harvest
        names it) or the sender is holding no task to contradict it — a human on the API, who
        may name the chain they are delegating on behalf of, or a routine that legitimately
        continues one. A sender that names a parent it is not actually working delegates from
        the chain it *is* in, so omitting or forging the parent escapes nothing.
        """
        claimed = self._claimed_by(sender) if sender is not None else []
        if parent_task_id:
            supplied = self.store.job(parent_task_id)
            if supplied is None:
                # Dropping the parent would keep the letter and lose the chain: the item
                # would look like a resident's own idea, and the spend attribute to nobody.
                raise DelegationError(
                    UNKNOWN_PARENT,
                    f"no task {parent_task_id!r} to descend from; a handoff that names a "
                    f"parent steward has never seen would be delivered with its lineage "
                    f"silently lost",
                )
            if not claimed or any(job.task_id == supplied.task_id for job in claimed):
                return supplied
        return max(claimed, key=lambda job: job.depth) if claimed else None

    def _check_permission(self, sender: Resident | None, receiver: Resident) -> None:
        """Check the sending half: the sender's own manifest has to permit this."""
        if sender is None:
            # A person asked. The token is the permission, and there is no manifest to
            # consult: a human may hand work to any resident whose route accepts it.
            return
        delegation = sender.manifest.delegation
        if not delegation.send:
            raise DelegationError(
                NOT_PERMITTED,
                f"{sender.id!r} does not declare delegation: {{send: true}}, so it may not "
                f"hand work to anybody; silence in a manifest is not consent",
            )
        if not delegation.may_send_to(receiver.id):
            allowed = ", ".join(sorted(delegation.to))
            raise DelegationError(
                RECIPIENT_NOT_ALLOWED,
                f"{sender.id!r} may delegate only to: {allowed}; {receiver.id!r} is not on "
                f"its declared list",
            )

    def _check_route(self, receiver: Resident, route_id: str) -> None:
        """Check the receiving half: an active route of the right kind, named by the sender."""
        route = receiver.route(route_id)
        if route is None:
            open_routes = ", ".join(receiver.delegation_routes) or "none"
            raise DelegationError(
                UNKNOWN_ROUTE,
                f"{receiver.id!r} declares no route {route_id!r} "
                f"(routes accepting delegated work: {open_routes})",
            )
        if route.kind != DELEGATION_ROUTE_KIND:
            raise DelegationError(
                ROUTE_NOT_DELEGABLE,
                f"route {route_id!r} of {receiver.id!r} is kind {route.kind!r}, not "
                f"{DELEGATION_ROUTE_KIND!r}; steward delivers only into a route that "
                f"declares it takes delegated work",
            )
        if route.status != "active":
            raise DelegationError(
                ROUTE_INACTIVE,
                f"route {route_id!r} of {receiver.id!r} is {route.status!r}; delivering "
                f"into a channel that is not open yet is a lie the village would render",
            )

    def _check_depth(self, depth: int) -> None:
        if depth > self.max_depth:
            raise DelegationError(
                MAX_DEPTH_EXCEEDED,
                f"this handoff would be hop {depth} of a chain, and steward stops at "
                f"{self.max_depth}; work this far from the person who asked for it needs "
                f"a person, not another resident",
            )

    def _check_cycle(self, chain: Sequence[JobRecord], sender_id: str, receiver_id: str) -> None:
        """Refuse a handoff whose lineage would revisit a resident.

        Everyone the chain has already passed through counts — the residents that sent
        each hop and the residents that received them — because ``A → B → A`` and
        ``A → B → C → A`` are the same mistake at different lengths. Without this, two
        residents can hand one task back and forth until somebody's budget is gone.
        """
        visited = {sender_id}
        for record in chain:
            visited.update(part for part in (record.delegated_by, record.assignee) if part)
        if receiver_id in visited:
            raise DelegationError(
                CYCLE,
                f"{receiver_id!r} is already in this task's lineage "
                f"({self._chain_path(chain, sender_id, receiver_id)}); a chain that revisits "
                f"a resident is a loop, and steward will not start one",
            )

    @staticmethod
    def _chain_path(chain: Sequence[JobRecord], sender_id: str, receiver_id: str) -> str:
        """Render the whole chain a cycle would close, root sender first, no duplicate hop.

        The lineage records name each hop's ``delegated_by`` and ``assignee``; walking them
        gives the root sender once, then every resident the work passed through, then the
        resident about to be handed it again (steward #W5). Appending ``sender_id`` only when
        it is not already the last hop is what stops the receiver-of-the-last-hop being
        printed twice.
        """
        parts: list[str] = []
        for record in chain:
            if not parts and record.delegated_by:
                parts.append(record.delegated_by)
            if record.assignee:
                parts.append(record.assignee)
        if not parts or parts[-1] != sender_id:
            parts.append(sender_id)
        parts.append(receiver_id)
        return " → ".join(parts)

    # -- the write ---------------------------------------------------------------------

    def delegate(
        self,
        *,
        sender: Resident | None,
        handoff: Handoff,
        parent_task_id: str | None = None,
    ) -> JobRecord:
        """Validate one handoff and enqueue it, or raise :class:`DelegationError`.

        Nothing is written and nothing is emitted for a refusal. ``sender=None`` is a
        person asking over the API: the sending half of the check is the token itself,
        and every other guardrail still applies.
        """
        if not handoff.ok:
            raise DelegationError(UNREADABLE_BLOCK, handoff.problem or "unreadable handoff")
        self._validate_input(handoff, parent_task_id)
        sender_id = sender.id if sender is not None else HUMAN_SENDER
        self._check_sender_active(sender)

        receiver = self._find_receiver(handoff, sender_id)
        self._check_permission(sender, receiver)
        self._check_route(receiver, handoff.route)

        parent = self._resolve_parent(sender, parent_task_id)
        chain = self.store.ancestry(parent.task_id) if parent is not None else []
        depth = (parent.depth if parent is not None else 0) + 1
        self._check_depth(depth)
        self._check_cycle(chain, sender_id, receiver.id)

        task = self.deliveries.deliver(
            title=handoff.title,
            detail=handoff.detail,
            assignee=receiver.id,
            delegated_by=sender_id,
            route=handoff.route,
            parent_task_id=parent.task_id if parent is not None else None,
            origin=origin_for(parent, sender_id),
            depth=depth,
            sender_agent_id=sender.agent_id if sender is not None else ev.API_AGENT_ID,
            sender_project=sender.project if sender is not None else ev.API_PROJECT,
            recipient_agent_id=receiver.agent_id,
        ).require()
        log.info(
            "%s delegated %s (%s) to %s via route %s at depth %d",
            sender_id,
            task.task_id,
            task.title,
            receiver.id,
            handoff.route,
            depth,
        )
        return task

    # -- harvesting a session's output --------------------------------------------------

    def harvest(
        self,
        sender: Resident,
        output: str,
        *,
        parent_task_id: str | None = None,
        now: datetime | None = None,
    ) -> list[Delivery]:
        """Turn every ``<delegate>`` block a finished session wrote into a letter.

        Called for both session types — a routine and a claimed task — so "how does a
        resident hand work over?" has one answer that does not depend on why it woke up.
        A refusal knocks rather than disappearing, because the session that asked has
        already finished and there is nobody left to tell.

        Only the session's machine-read region is scanned (:func:`steward.prompt.harvestable`),
        with quoted and fenced parts stripped first, so a ``<delegate>`` block a session
        quoted back from an attacker-supplied job or task detail it was handed is not
        mistaken for the session actually delegating (steward #62).
        """
        delivered: list[Delivery] = []
        for handoff in extract_handoffs(prompt.harvestable(output)):
            try:
                task = self.delegate(sender=sender, handoff=handoff, parent_task_id=parent_task_id)
            except DelegationError as exc:
                delivered.append(self._knock(sender, handoff, exc, now))
                continue
            delivered.append(Delivery(handoff=handoff, task=task))
        return delivered

    def _knock(
        self,
        sender: Resident,
        handoff: Handoff,
        error: DelegationError,
        now: datetime | None,
    ) -> Delivery:
        """Raise a refused handoff as an approval request, so a person hears about it.

        The knock is steward's own — steward refused, steward wrote the message — so it
        goes through :meth:`ApprovalTransitions.knock`, which is exempt from the
        repeat-deny guard. Both actions here
        are catch-alls: *every* refusal of this resident's, whatever recipient or route or
        title it was about, is filed under :data:`REJECTED_ACTION` or
        :data:`UNREADABLE_ACTION`. A deny on one of them — and a deny is the natural way to
        dismiss a knock that is only telling you something — would otherwise answer for the
        next refusal too, and that one would vanish with nobody left to tell.
        """
        unreadable = error.reason == UNREADABLE_BLOCK
        name = sender.manifest.soul.name
        if unreadable:
            action = UNREADABLE_ACTION
            message = f"{name} tried to delegate work and steward could not read the request"
        else:
            action = REJECTED_ACTION
            message = f"{name} tried to delegate {handoff.title!r} to {handoff.to!r}, refused"
        log.warning("%s: delegation refused (%s) — %s", sender.id, error.reason, error)
        detail: dict[str, Any] = {
            "reason": error.reason,
            "problem": str(error),
            "to": handoff.to,
            "route": handoff.route,
            "title": handoff.title,
            "raw": handoff.raw[:DETAIL_MAX_CHARS],
        }
        record = self.knocks.knock(
            manifest=sender.manifest,
            request=approvals.NeedsHuman(raw=handoff.raw, action=action, detail=detail),
            now=now or datetime.now(UTC),
            message=message,
        ).require()
        return Delivery(handoff=handoff, reason=error.reason, message=str(error), knock=record)
