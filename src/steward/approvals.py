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

**A malformed block still knocks.** An escalation steward cannot parse is not dropped
and is not silently ignored: it becomes a request with the action
:data:`UNREADABLE_ACTION`, carrying the raw block and the complaint, so a person hears
about it. A session that tried to ask and failed is a session that must not be mistaken
for a session that had nothing to ask.
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from steward import events as ev
from steward.manifest import ResidentManifest
from steward.store import APPROVAL_DECISIONS, ApprovalRecord, Store

__all__ = [
    "BLOCK_CLOSE",
    "BLOCK_OPEN",
    "DEFAULT_EXPIRES_IN_S",
    "DETAIL_MAX_CHARS",
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
]

log = logging.getLogger("steward.approvals")

#: The markers a session writes around a request in its final output.
BLOCK_OPEN = "<needs-human"
BLOCK_CLOSE = "</needs-human>"

#: How long a request is good for when the session names no ``expires-in``. A day is
#: long enough to survive one night's sleep and short enough that a forgotten request
#: resolves itself rather than sitting pending forever.
DEFAULT_EXPIRES_IN_S = 24 * 60 * 60

#: A request is a question, not a transcript: the detail is bounded before it is stored.
DETAIL_MAX_CHARS = 8000

#: The action recorded when a session tried to escalate and steward could not read it.
UNREADABLE_ACTION = "unreadable_escalation"

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


def raise_request(  # noqa: PLR0913 — the collaborators plus the request, all keyword-only
    store: Store,
    emitter: ev.Emitter,
    *,
    manifest: ResidentManifest,
    request: NeedsHuman,
    message: str | None = None,
    now: datetime | None = None,
    request_id: str | None = None,
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
    )
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
    """
    return [
        raise_request(store, emitter, manifest=manifest, request=request, now=now)
        for request in extract_requests(output)
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
            "deny: nobody answered in time, so the answer is no."
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
    """
    records = store.undelivered_decisions(resident_id)
    if not records:
        return None, []
    store.mark_delivered([record.request_id for record in records])
    return decisions_preamble(records), records
