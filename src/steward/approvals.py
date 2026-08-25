"""Structured approvals: a knock at the door that can actually be answered.

A resident's charter classifies actions. Drafting an email is autonomous; sending it,
spending money, or anything else the charter's hard rules gate is not. When a headless
session reaches a gated action it does not do it — it *asks*, and steward turns the ask
into a durable request a human can answer from burrow's panel or a push notification.

**The mechanism is a protocol, not a function call.** Sessions are headless CLIs
(``claude -p``, ``codex exec``), so there is nothing in-process for them to call. Two
paths, both documented in ``docs/approvals.md``, both landing in the same store row:

1. **A block in the session's output** — the same shape as the ``<journal>`` fallback
   in :mod:`steward.journal`, parsed here by :func:`extract_requests`::

       <needs-human action="send_email" expires-in="4h" options="approve,deny,edit">
       {"to": "anna@example.com", "subject": "Re: Thursday"}
       </needs-human>

2. **``steward approval raise``** — a local, token-free CLI a session with shell access
   calls directly, for work that wants the request registered before the turn ends.

**Park, do not block.** A session that raises an approval finishes its turn and stops.
Holding a ``claude -p`` turn open waiting for a human is expensive and fragile, and a
resident sitting on a paused session is not resting. The decision is delivered on the
resident's *next* wake-up, injected into its preamble by :func:`decisions_preamble` and
marked delivered exactly once. (A blocking path — a session that genuinely waits — is
deferred; the store and the events already support it.)

**Expiry is deny-by-default.** :func:`expire` runs on every tick and every dispatch:
past ``expires_at``, the request is resolved as ``deny`` with ``decided_by: "expiry"``,
``needs_human_resolved`` is emitted, and the gated action never ran. A human going to
sleep must never be the reason something irreversible happened.

**A deny answers for a while.** The decisions preamble tells a resident that a deny means
"do not ask again this session"; :func:`raise_request` is the enforcement. When the same
resident raises the same action again within :data:`DEFAULT_REPEAT_DENY_WINDOW_H` hours of
being told no, the ask is recorded as an auto-deny (``decided_by: "repeat"``) and nobody's
phone buzzes. A looping resident cannot knock on every wake-up. The guard answers only for
actions a *session chose*: steward's own knocks — a budget pause, a watchdog give-up —
pass ``repeat_guard=False``, and the slugs steward assigns itself
(:data:`REPEAT_GUARD_EXEMPT_ACTIONS`) are never swallowed, because one deny of a catch-all
would stand in for every future ask that lands under it.

**A malformed block still knocks.** An escalation steward cannot parse is not dropped
and is not silently ignored: it becomes a request with the action
:data:`UNREADABLE_ACTION`, carrying the raw block and the complaint, so a person hears
about it — every time, including the second one, because that action is exempt from the
repeat guard. A session that tried to ask and failed is a session that must not be
mistaken for a session that had nothing to ask.
"""

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from steward import events as ev
from steward import prompt
from steward.manifest import ResidentManifest, redact_mapping, redact_secrets
from steward.store import APPROVAL_DECISIONS, DECIDED_BY_REPEAT, ApprovalRecord, Store

__all__ = [
    "BLOCK_CLOSE",
    "BLOCK_OPEN",
    "DEFAULT_EXPIRES_IN_S",
    "DEFAULT_REPEAT_DENY_WINDOW_H",
    "DETAIL_MAX_CHARS",
    "MAX_EXPIRES_IN_S",
    "REPEAT_DENY_WINDOW_ENV",
    "REPEAT_GUARD_EXEMPT_ACTIONS",
    "UNREADABLE_ACTION",
    "ApprovalError",
    "NeedsHuman",
    "decisions_preamble",
    "deliver_decisions",
    "expire",
    "extract_requests",
    "harvest",
    "human_message",
    "parse_duration",
    "raise_request",
    "redact_decision",
    "repeat_deny_window_s",
]

log = logging.getLogger("steward.approvals")

#: The markers a session writes around a request in its final output.
BLOCK_OPEN = "<needs-human"
BLOCK_CLOSE = "</needs-human>"

#: How long a request is good for when the session names no ``expires-in``. A day is
#: long enough to survive one night's sleep and short enough that a forgotten request
#: resolves itself rather than sitting pending forever.
DEFAULT_EXPIRES_IN_S = 24 * 60 * 60

#: The furthest out a session may push its own deadline (steward #66). Deny-by-default is
#: the whole safety property, and a session that names ``expires-in="9999999d"`` is a
#: session pushing its knock to the year 7502 — deny-by-default made unreachable, and, when
#: the seconds are added to a datetime, an ``OverflowError`` on the way there. Thirty days
#: is far longer than any real question should wait and short enough to stay a real date.
MAX_EXPIRES_IN_S = 30 * 24 * 60 * 60

#: A request is a question, not a transcript: the detail is bounded before it is stored.
DETAIL_MAX_CHARS = 8000

#: The action recorded when a session tried to escalate and steward could not read it.
UNREADABLE_ACTION = "unreadable_escalation"

#: How long a deny goes on answering for the same resident and action (steward #33). Half
#: a day covers the wake-ups between one night's sleep and the next without turning a
#: single no into a permanent ban: a resident that still wants the thing tomorrow may ask
#: tomorrow, and a human who meant "never" says so in the charter, which is where forever
#: belongs. ``0`` turns the guard off and every repeat knocks again.
DEFAULT_REPEAT_DENY_WINDOW_H = 12
REPEAT_DENY_WINDOW_ENV = "STEWARD_REPEAT_DENY_WINDOW_H"

#: Actions the repeat guard never answers for. The guard's fingerprint is
#: ``(resident, action)``, which only means "the same question asked twice" when the
#: action is one a *session chose*. :data:`UNREADABLE_ACTION` is not: steward assigns it
#: to every escalation it could not read, so a deny of one malformed block would stand in
#: for the next malformed block too — a different intended action, swallowed unheard, in
#: exactly the case this module promises never to swallow. :mod:`steward.delegation` has
#: two catch-alls of its own and keeps them out of the guard the other way, by passing
#: ``repeat_guard=False``, because approvals cannot import it.
REPEAT_GUARD_EXEMPT_ACTIONS = frozenset({UNREADABLE_ACTION})

ACTION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_BLOCK = re.compile(
    re.escape(BLOCK_OPEN) + r"(?P<attrs>[^>]*)>(?P<body>.*?)" + re.escape(BLOCK_CLOSE),
    re.DOTALL,
)
_ATTRIBUTE = re.compile(r'(?P<name>[a-z][a-z-]*)\s*=\s*"(?P<value>[^"]*)"')
_DURATION = re.compile(r"^(?P<count>\d+)\s*(?P<unit>[smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

KNOWN_ATTRIBUTES = frozenset({"action", "expires-in", "options"})


class ApprovalError(Exception):
    """Raised when a request cannot be built from what a caller supplied."""


# --------------------------------------------------------------------------------------
# the grammar
# --------------------------------------------------------------------------------------


def parse_duration(text: str) -> int:
    """Turn ``30m`` / ``4h`` / ``2d`` / ``90s`` into seconds. Raises on anything else.

    A deliberately tiny vocabulary. "How long until this denies itself" is a safety
    property, and a format that guesses at ``tomorrow`` or ``4 hours-ish`` would be a
    safety property nobody can predict.
    """
    match = _DURATION.match(text.strip())
    if match is None:
        raise ApprovalError(
            f"expires-in {text!r} is not a duration; write it as <number><unit> with "
            f"unit s, m, h, or d — for example '4h'"
        )
    seconds = int(match.group("count")) * _UNITS[match.group("unit")]
    if seconds <= 0:
        raise ApprovalError(f"expires-in {text!r} is not a positive duration")
    if seconds > MAX_EXPIRES_IN_S:
        log.info(
            "expires-in %r exceeds the fleet maximum of %d s; clamping so deny-by-default "
            "stays reachable and no date overflows",
            text,
            MAX_EXPIRES_IN_S,
        )
        return MAX_EXPIRES_IN_S
    return seconds


def parse_options(text: str) -> tuple[str, ...]:
    """Parse a comma-separated option list, rejecting anything a human cannot answer."""
    options = tuple(part.strip() for part in text.split(",") if part.strip())
    if not options:
        raise ApprovalError("options is empty; leave the attribute off to offer all three")
    unknown = [option for option in options if option not in APPROVAL_DECISIONS]
    if unknown:
        known = ", ".join(APPROVAL_DECISIONS)
        raise ApprovalError(f"unknown option(s) {unknown}; the API accepts only: {known}")
    return options


def parse_detail(body: str) -> Mapping[str, Any]:
    """Turn a block body into the request's detail.

    A JSON object is used as-is, because that is what a panel renders into fields. Plain
    prose becomes ``{"note": …}`` rather than being refused: a resident that explains
    itself in a sentence has still asked a real question. Something that *starts* like
    JSON and does not parse is a mistake, though, and it is raised rather than quietly
    filed under ``note`` where nobody would find the typo.
    """
    text = body.strip()
    if not text:
        return {}
    if text.startswith(("{", "[")):
        try:
            loaded = json.loads(text)
        except ValueError as exc:
            raise ApprovalError(f"detail looks like JSON but does not parse: {exc}") from None
        if not isinstance(loaded, Mapping):
            raise ApprovalError("detail JSON must be an object, so a panel can render fields")
        return dict(loaded)
    return {"note": text[:DETAIL_MAX_CHARS]}


@dataclass(frozen=True, slots=True)
class NeedsHuman:
    """One parsed request, or one complaint about a block that could not be parsed."""

    raw: str
    action: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)
    options: tuple[str, ...] = APPROVAL_DECISIONS
    #: How long before this denies itself. ``None`` means it never does, which the
    #: grammar cannot produce — :func:`parse_duration` only ever returns a positive
    #: number of seconds — and which only a caller inside steward may ask for. It is for
    #: the one shape of request where deny-by-default protects nothing: a budget pause
    #: (:mod:`steward.budgets`) is *already* the safe state, so expiring the request would
    #: throw away the only thing that can lift it while changing nothing for the better.
    expires_in_s: int | None = DEFAULT_EXPIRES_IN_S
    problem: str | None = None

    @property
    def ok(self) -> bool:
        """True when steward could read the request the session wrote."""
        return self.problem is None

    def expires_at(self, now: datetime) -> str | None:
        """Return the moment this request denies itself, or ``None`` when it never does."""
        if self.expires_in_s is None:
            return None
        return ev.utc_now_iso(now + timedelta(seconds=self.expires_in_s))


def _read_attributes(attrs: str) -> dict[str, str]:
    """Parse the block's attributes, refusing anything this grammar does not define."""
    attributes = {m.group("name"): m.group("value") for m in _ATTRIBUTE.finditer(attrs)}
    unknown = sorted(set(attributes) - KNOWN_ATTRIBUTES)
    if unknown:
        known = ", ".join(sorted(KNOWN_ATTRIBUTES))
        raise ApprovalError(f"unknown attribute(s) {unknown}; this block takes: {known}")
    leftover = _ATTRIBUTE.sub("", attrs).strip()
    if leftover:
        raise ApprovalError(
            f"could not read {leftover!r} as an attribute; every attribute is "
            f'name="value" with double quotes'
        )
    return attributes


def _read_action(attributes: Mapping[str, str]) -> str:
    """Return the action the block names, or say why it does not name one."""
    action = attributes.get("action", "").strip()
    if not action:
        raise ApprovalError('the block needs an action, e.g. action="send_email"')
    if not ACTION_PATTERN.match(action):
        raise ApprovalError(
            f"action {action!r} is not a slug; use lowercase letters, digits, '_' and '-'"
        )
    return action


def _read_block(raw: str, attrs: str, body: str) -> NeedsHuman:
    """Build one request from a matched block, raising on anything unreadable."""
    attributes = _read_attributes(attrs)
    expires_in = attributes.get("expires-in")
    options = attributes.get("options")
    return NeedsHuman(
        raw=raw,
        action=_read_action(attributes),
        detail=parse_detail(body),
        options=parse_options(options) if options is not None else APPROVAL_DECISIONS,
        expires_in_s=parse_duration(expires_in) if expires_in is not None else DEFAULT_EXPIRES_IN_S,
    )


def _parse_block(raw: str, attrs: str, body: str) -> NeedsHuman:
    """Read one block, or come back carrying the complaint instead of the request."""
    try:
        return _read_block(raw, attrs, body)
    except ApprovalError as exc:
        return NeedsHuman(raw=raw, action=UNREADABLE_ACTION, problem=str(exc))


def extract_requests(output: str) -> list[NeedsHuman]:
    """Return every ``<needs-human>`` block in a session's output, in order.

    Every block, not just the last one: unlike a journal — where one entry per day means
    the last block wins — a session that gated two actions asked two questions, and
    dropping either would let one of them quietly happen or quietly not happen.

    An unparseable block comes back with :attr:`NeedsHuman.problem` set rather than being
    skipped, and an opening marker with no closing one is itself reported: a truncated
    escalation is the most likely shape of a session that was killed mid-ask.
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
            NeedsHuman(
                raw=dangling,
                action=UNREADABLE_ACTION,
                problem=(
                    f"an opening {BLOCK_OPEN}…> has no closing {BLOCK_CLOSE}; the request "
                    f"was cut off, so steward cannot tell what was being asked"
                ),
            )
        )
    return found


# --------------------------------------------------------------------------------------
# raising, delivering, expiring
# --------------------------------------------------------------------------------------


def human_message(manifest: ResidentManifest, action: str) -> str:
    """Derive the one-line knock burrow renders and ntfy forwards.

    Kept simple and derived rather than session-authored, so the message can never
    disagree with the ``action`` a decision is recorded against.
    """
    return f"{manifest.soul.name} wants to {action.replace('_', ' ')}"


def repeat_deny_window_s(env: Mapping[str, str] | None = None) -> int:
    """Return how long a deny keeps answering, in seconds.

    ``$STEWARD_REPEAT_DENY_WINDOW_H`` overrides :data:`DEFAULT_REPEAT_DENY_WINDOW_H`, in
    whole hours. ``0`` is a legitimate value and it is the kill switch: no ask is ever
    inside a zero-length window, so every repeat knocks again. Anything that is not a
    whole number ≥ 0 is a misconfiguration, and steward says so and falls back to the
    default rather than quietly running with a window nobody chose.
    """
    source = os.environ if env is None else env
    raw = (source.get(REPEAT_DENY_WINDOW_ENV) or "").strip()
    hours = DEFAULT_REPEAT_DENY_WINDOW_H
    if raw:
        try:
            hours = int(raw)
        except ValueError:
            hours = -1
        if hours < 0:
            log.warning(
                "%s=%r is not a number of hours; using the default of %d",
                REPEAT_DENY_WINDOW_ENV,
                raw,
                DEFAULT_REPEAT_DENY_WINDOW_H,
            )
            hours = DEFAULT_REPEAT_DENY_WINDOW_H
    return hours * 3600


def _denied_recently(store: Store, resident: str, action: str, moment: datetime) -> bool:
    """Report whether this resident has already been told no about this action, recently.

    The fingerprint is deliberately coarse: ``(resident, action)`` and nothing else. A
    request's ``detail`` is free-form, un-normalized JSON (:func:`parse_detail`), so two
    asks that differ only in a timestamp inside it would read as different questions to
    any comparison and the guard would catch nothing. Coarse means a resident denied
    ``send_email`` to one address cannot ask about a *different* address for the rest of
    the window either — the trade steward makes on purpose, because the failure it exists
    to stop is a resident knocking on every wake-up.

    That trade is only payable when the action names what the resident asked for. For the
    slugs in :data:`REPEAT_GUARD_EXEMPT_ACTIONS` it does not: they are catch-alls steward
    assigns, and every ask that lands under one is a *different* question wearing the same
    name. Those are never answered by an earlier deny.
    """
    if action in REPEAT_GUARD_EXEMPT_ACTIONS:
        return False
    window_s = repeat_deny_window_s()
    if window_s <= 0:
        return False
    since = ev.utc_now_iso(moment - timedelta(seconds=window_s))
    return store.recent_denials(resident, action, since) > 0


def raise_request(  # noqa: PLR0913 — the collaborators plus the request, all keyword-only
    store: Store,
    emitter: ev.Emitter,
    *,
    manifest: ResidentManifest,
    request: NeedsHuman,
    message: str | None = None,
    now: datetime | None = None,
    request_id: str | None = None,
    repeat_guard: bool = True,
) -> ApprovalRecord:
    """Persist one request and knock. Returns the record the human will answer.

    A request that could not be parsed is persisted too, under
    :data:`UNREADABLE_ACTION`, with the raw block and the complaint in its detail. That
    is the whole reason this function takes a :class:`NeedsHuman` rather than an action
    string: an escalation steward failed to read still has to reach a person.

    ``message`` overrides the derived one-liner, and only steward itself passes it. A
    *session* never gets to write its own knock — :func:`human_message` derives that from
    the action, so the message can never disagree with what the decision is recorded
    against — but steward raising a request on a resident's behalf knows something the
    action name cannot carry: :mod:`steward.budgets` names the number that tripped a cap,
    and :mod:`steward.delegation` says "steward refused this handoff" in words the action
    slug has no room for.

    ``repeat_guard`` is on for everything a *session* chose to ask, and off for the two
    knocks steward raises about a resident rather than for it: a budget pause
    (:mod:`steward.budgets`) and a watchdog give-up (:mod:`steward.watchdog`) are
    one-per-condition by their own conditional insert, they never expire, and the deny a
    human gives them is the answer to a *different* question — turning that deny into a
    reason to swallow the next pause would hide the thing steward most needs to say. The
    same reasoning exempts the catch-all actions steward assigns rather than a session
    choosing them (:data:`REPEAT_GUARD_EXEMPT_ACTIONS`), whichever way ``repeat_guard`` is
    set.
    """
    moment = now or datetime.now(UTC)
    agent_id = manifest.agent_id or f"steward:{manifest.id}"
    project = manifest.project or manifest.id
    detail: dict[str, Any] = dict(request.detail)
    if request.problem is not None:
        log.warning(
            "%s: could not read a needs-human block — %s; raising it anyway so somebody sees it",
            manifest.id,
            request.problem,
        )
        detail = {"problem": request.problem, "raw": request.raw[:DETAIL_MAX_CHARS]}

    repeat = repeat_guard and _denied_recently(store, manifest.id, request.action, moment)
    record = store.create_approval_request(
        agent_id=agent_id,
        project=project,
        action=request.action,
        message=message or human_message(manifest, request.action),
        resident=manifest.id,
        detail=detail,
        options=request.options,
        expires_at=request.expires_at(moment),
        request_id=request_id,
        denied_by=DECIDED_BY_REPEAT if repeat else None,
    )
    if repeat:
        log.info(
            "%s: %s was already denied within the last %d h — auto-denied as a repeat, "
            "nobody knocked",
            manifest.id,
            request.action,
            repeat_deny_window_s() // 3600,
        )
        return record
    emitter.emit(
        ev.needs_human_event(
            message=record.message,
            request_id=record.request_id,
            action=record.action,
            agent_id=record.agent_id,
            project=record.project,
            detail=record.detail,
            options=record.options,
            expires_at=record.expires_at,
        )
    )
    return record


def harvest(
    store: Store,
    emitter: ev.Emitter,
    *,
    manifest: ResidentManifest,
    output: str,
    now: datetime | None = None,
) -> list[ApprovalRecord]:
    """Turn every ``<needs-human>`` block in a finished session's output into a request.

    The one place a session's output becomes an approval, called by both session types —
    the scheduler's routines and the board's claimed tasks — so "how does a resident
    ask?" has a single answer that does not depend on why it woke up.

    Only the session's machine-read region is scanned (:func:`steward.prompt.harvestable`),
    and quoted or fenced parts of it are stripped first, so a ``<needs-human>`` block a
    session quoted back from an attacker-supplied job or task detail is not mistaken for the
    session actually asking (steward #62).
    """
    return [
        raise_request(store, emitter, manifest=manifest, request=request, now=now)
        for request in extract_requests(prompt.harvestable(output))
    ]


def expire(store: Store, emitter: ev.Emitter, now: datetime | None = None) -> list[ApprovalRecord]:
    """Deny every request whose deadline has passed, and close the loop in the log.

    Called on every scheduler tick and every board dispatch, which is what makes the
    deadline real rather than decorative: nothing sweeps a queue that nobody visits.
    """
    moment = ev.utc_now_iso(now or datetime.now(UTC))
    expired = store.expire_approvals(moment)
    for record in expired:
        log.info(
            "approval %s (%s) expired at %s — denied by default",
            record.request_id,
            record.action,
            record.expires_at,
        )
        emitter.emit(
            ev.needs_human_resolved_event(
                request_id=record.request_id,
                decision="deny",
                action=record.action,
                agent_id=record.agent_id,
                project=record.project,
                decided_by=record.decided_by or "expiry",
            )
        )
    return expired


def _render_decision(record: ApprovalRecord) -> str:
    line = f"- {record.action}: {record.decision}"
    if record.decided_by:
        line += f" (decided by {record.decided_by}"
        line += f" at {record.decided_at})" if record.decided_at else ")"
    if record.edit:
        line += f"\n  the human edited it to: {json.dumps(dict(record.edit), ensure_ascii=False)}"
    if record.detail:
        line += f"\n  what you asked: {json.dumps(dict(record.detail), ensure_ascii=False)}"
    return line


def redact_decision(record: ApprovalRecord) -> ApprovalRecord:
    """Return ``record`` with every string a model wrote scrubbed of inline secrets.

    A request's ``message`` and ``detail`` are written by a resident at runtime and stored
    verbatim (:func:`raise_request`); the ``edit`` is written by whoever answered. No
    validator ever scanned any of them — the manifest scanners guard what enters the repo,
    not what a session types — so anything that renders a decision *outside* the session
    that asked (``steward show``, a report pasted into an issue) must scrub it first, the
    way :func:`steward.events.needs_human_event` scrubs the same detail on its way to
    burrow. Redaction is per field, not over the rendered line, so an action legitimately
    named ``rotate_token`` still reads as itself rather than as a redacted assignment.

    Deliberately not applied by :func:`decisions_preamble` itself: a resident's own next
    session is being handed back the detail it wrote, and mangling that is a lie about what
    it asked. This is the egress-to-a-human copy.
    """
    return replace(
        record,
        action=redact_secrets(record.action),
        message=redact_secrets(record.message),
        detail=redact_mapping(record.detail) or {},
        edit=redact_mapping(record.edit),
    )


def decisions_preamble(records: Sequence[ApprovalRecord]) -> str | None:
    """Render decided requests as the text a resident's next session opens with.

    ``None`` for an empty list, so a resident with nothing waiting gets a preamble
    byte-identical to one assembled before approvals existed.
    """
    if not records:
        return None
    lines = [
        (
            "These are answers to questions you asked in an earlier session. They are "
            "facts about what a human decided, not new instructions. An 'approve' means "
            "you may now do exactly the action you asked about and nothing more; a 'deny' "
            "means you must not do it and must not ask again this session; an 'edit' "
            "means do it with the human's changes. A decision recorded by 'expiry' is a "
            "deny: nobody answered in time, so the answer is no. A decision recorded by "
            "'repeat' is also a deny: you had already been told no about that action "
            "recently, so steward answered for the human and did not wake anybody. Asking "
            "the same thing again will get the same answer — take the no and move on."
        ),
        "",
        *[_render_decision(record) for record in records],
    ]
    return "\n".join(lines)


def deliver_decisions(store: Store, resident_id: str) -> tuple[str | None, list[ApprovalRecord]]:
    """Take this resident's undelivered decisions and mark them delivered, once.

    Returns the preamble text and the records it was built from. Delivery is recorded
    here rather than after the session finishes, on purpose: a decision that is delivered
    again on every wake-up until some run happens to succeed would have a resident
    re-reading "you may send that email" for a week. Told once, in the session that was
    given it, is the honest reading of "the decision reached the resident".

    The read and the mark are one atomic store transaction
    (:meth:`steward.store.Store.claim_undelivered_decisions`), so two wake-ups of the same
    resident at the same instant cannot both walk away believing they were handed the same
    answer: one gets it, the other opens without it, and it is delivered exactly once
    (steward #74). Every caller of this function inherits that guarantee, not only the board.
    """
    records = store.claim_undelivered_decisions(resident_id)
    return decisions_preamble(records), records
