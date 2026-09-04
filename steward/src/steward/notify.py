"""Notifications: the one-way taps steward sends a human, and the transports that carry them.

A **route** is how work reaches a resident. A **notification** is the opposite direction and
the opposite shape: steward taps a person on the shoulder about a resident — a ``needs_human``
at 2am, a board task that finished — and nothing listens for a reply. No session fires, no
answer comes back, nothing can arrive through it. That is why it is not a chat channel and
not a route kind (warren#114): chat is a conversation and this is a notice.

What this module owns is everything after a manifest has said *yes, tap me*:

- **where a tap goes** — :func:`ntfy_topic` derives an unguessable per-resident ntfy topic
  from the resident's ``uid``;
- **what a tap says** — :func:`tap_for` turns one chronicle event into a title and a body,
  redacted before it is bounded;
- **how it is carried** — :class:`Transport`, with :class:`NtfyTransport` as the first and
  only implementation today;
- **whether to send at all** — :class:`Notifier`, which reads the resident's own
  :class:`steward.manifest.Notifications` block and never raises at its caller.

Two rules run through all of it, and both are about not making things worse:

**A tap can never break the thing it is reporting.** Every result is discarded and every
failure is swallowed into a log line: an unreachable ntfy must not turn a completed
transition into a failed one, exactly as an unreachable village does not
(:func:`steward.transitions.outcome.applied`).

Forgotten, then — but not *fired and* forgotten. The POST is **synchronous**, bounded by a
two second timeout and a sixty second circuit breaker, and it is deliberately not moved onto
a thread. A background thread would make a tap cost the caller nothing and would also make it
unreliable in the two places steward most often runs: a one-shot CLI (``steward approval
raise``) exits the moment its work is done, and a daemon thread killed at interpreter exit
drops the knock *silently* — trading two seconds for exactly the failure mode this module
exists to avoid. The same call sites already accept a two second event POST, so the honest
cost of a tap is a second one.

A failed tap is still *said* — ``log.warning`` naming the resident, the kind and the reason,
including every tap the breaker swallows while the window is open — because a notification
that silently stops arriving is indistinguishable from a fleet with nothing to say. It is
deliberately not an *event*: a chronicle event about steward's own plumbing would render in
the village as though a villager did something, and an event about a failed notification is a
fact that could itself be notified about.

**Nothing leaves without redaction.** Human-facing text is scrubbed with
:func:`steward.manifest.redact_secrets` and only then bounded — redact-then-bound, never the
reverse, so a secret cut in half by a length cap can never surface a live prefix (steward
#65). This matters more here than at the event boundary: ``needs_human`` payloads are already
scrubbed on their way to chronicle, but a ``task_done`` title and its artifact paths are not.
"""

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from email.header import Header
from typing import Any, Protocol

from steward import events as ev
from steward.manifest import (
    NOTIFICATION_KINDS,
    NOTIFICATION_TRANSPORTS,
    ResidentManifest,
    redact_secrets,
)

__all__ = [
    "BODY_MAX_CHARS",
    "DEFAULT_NTFY_URL",
    "NAMESPACE_ENV",
    "NTFY",
    "NTFY_TOKEN_ENV",
    "NTFY_URL_ENV",
    "TITLE_MAX_CHARS",
    "TOPIC_PREFIX",
    "NotificationReport",
    "Notifier",
    "NtfyTransport",
    "Tap",
    "Transport",
    "ntfy_topic",
    "probe_tap",
    "tap_for",
]

log = logging.getLogger("steward.notify")

#: The one transport that exists. Spelled once here and checked against the manifest
#: vocabulary at import time, so "which transports are there" has a single answer.
NTFY = "ntfy"

#: Where ntfy lives when nobody says otherwise. The public instance is the sane default
#: precisely because a derived topic is safe in a public namespace; a self-hosted server is
#: one environment variable away.
DEFAULT_NTFY_URL = "https://ntfy.sh"

NTFY_URL_ENV = "STEWARD_NTFY_URL"
NTFY_TOKEN_ENV = "STEWARD_NTFY_TOKEN"  # noqa: S105 — an env var name, not a credential
NTFY_TIMEOUT_ENV = "STEWARD_NTFY_TIMEOUT_S"

#: An extra string folded into every derived topic. Empty by default and almost always left
#: that way — its one job is to keep two steward installations that read the *same*
#: ``residents/`` tree off each other's phones. The uids in a manifest are the same bytes on
#: a laptop and on the NAS, so without a namespace a developer testing a knock locally would
#: buzz the operator's real phone, and the fix would have to be "do not run steward here".
NAMESPACE_ENV = "STEWARD_NOTIFY_NAMESPACE"

#: How long one tap may take. Short on purpose: this runs inside a durable transition, and
#: the same two seconds the event POST gets is the most a shoulder-tap may cost a run.
NTFY_TIMEOUT_S = 2.0

#: How long a failing transport is left alone after it fails, so a dead ntfy costs one
#: timeout a minute rather than one per knock. The emitter's own idiom, and its own spelling
#: (:data:`steward.events.BREAKER_SECONDS`), one module over.
BREAKER_SECONDS = 60.0

#: What every derived topic starts with, so an operator scanning their ntfy subscriptions can
#: tell which ones came from here. It adds no guessability: the 160 bits after it do the work.
TOPIC_PREFIX = "steward-"

#: Base32 characters of digest kept after the prefix: 32 of them, five bits each, 160 bits.
TOPIC_CHARS = 32

#: Domain separation for the topic hash. A uid may one day key something else derived; a
#: labelled hash keeps the two independent, and lets this derivation be versioned if the
#: shape ever has to change (it would change every topic, so it would be a migration).
TOPIC_DOMAIN = "steward/notify/ntfy/v1"

#: An ntfy title rides in an HTTP header, and a header is not a place for an essay.
TITLE_MAX_CHARS = 200

#: The body is the message itself. Generous, but bounded: a knock is a notice, and the
#: session that printed a 200KB escalation must not be able to make it a 200KB push.
BODY_MAX_CHARS = 3800

#: One detail value, rendered into a tap's body.
DETAIL_MAX_CHARS = 1200

#: ntfy priorities, as its own ``Priority`` header spells them.
PRIORITY_HIGH = "high"
PRIORITY_DEFAULT = "default"

if set(NOTIFICATION_TRANSPORTS) != {NTFY}:  # pragma: no cover — a guard against drift
    raise RuntimeError(
        f"manifest transports {NOTIFICATION_TRANSPORTS} and steward.notify disagree; "
        f"a manifest may declare a transport nothing here can deliver"
    )


# --------------------------------------------------------------------------------------
# where a tap goes
# --------------------------------------------------------------------------------------


def ntfy_topic(uid: uuid.UUID | str, namespace: str = "") -> str:
    """Derive this resident's ntfy topic from its ``uid``. Stable, unguessable, one-way.

    ntfy has no accounts in the path that matters here: a topic *is* the capability. Anyone
    who knows the string can subscribe to it and can publish into it, on ntfy.sh and on any
    self-hosted instance without auth. So the question this function answers is not "what is
    a tidy name for this resident's topic" — it is "what value can live in a public namespace".

    **Why a hash rather than the uid itself.** The uid is already unguessable (UUID4, minted
    by the nursery, 122 random bits), so ``steward-<uid>`` would be just as hard to *guess*.
    That is not the risk. The risk is the other direction: the uid was minted to be an
    *identifier*, and identifiers get shown. It is in the manifest in git, in the JSON schema,
    in ``GET /residents``, in townhall's markup, in a screenshot, in a paste. Not one of those
    places was designed while asking "and does this also grant read and write on the operator's
    phone?" — and if the topic were the uid, every one of them would silently be doing that.
    A one-way function lets the uid keep being the printable identifier it was minted as while
    the topic stays a secret nothing else has a reason to hand out. It also runs the other way:
    a topic that leaks says nothing about which resident it belongs to.

    What this is **not** is a boundary against somebody holding the repo. They have the uid,
    they have this function, they can compute the topic — and they already have the manifests,
    the charters and the API token, so the topic is not the interesting thing they hold. The
    property being bought is narrower and worth the four lines it costs: *the topic is never
    incidentally disclosed by disclosing the uid.*

    ``namespace`` is folded in so two installations reading one ``residents/`` tree — a laptop
    checkout and the NAS — derive different topics from the same uid. See :data:`NAMESPACE_ENV`.

    The input is normalised through :class:`uuid.UUID` so a manifest that spelled its uid in
    upper case or with braces derives the same topic as one that did not: a topic that changed
    because somebody reformatted a file would be an operator resubscribing for nothing.
    """
    try:
        normalized = str(uuid.UUID(str(uid)))
    except ValueError:
        # Not reachable from a validated manifest (``uid`` is a ``UUID4`` field). Kept honest
        # rather than crashing a caller that built a manifest some other way: the exact string
        # is hashed, which is stable and unguessable, and simply is not canonicalised.
        normalized = str(uid)
    digest = hashlib.sha256(f"{TOPIC_DOMAIN}|{namespace}|{normalized}".encode()).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"{TOPIC_PREFIX}{encoded[:TOPIC_CHARS]}"


# --------------------------------------------------------------------------------------
# what a tap says
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tap:
    """One outbound notice, transport-independent: a title, a body, and how loud it is.

    Deliberately not an :class:`steward.events.Event`. An event is a fact about the village
    that chronicle renders and the watchdog reads; a tap is a sentence for a person on a
    phone, and it is built *from* an event rather than being one. Keeping them separate is
    what lets a second transport (telegram, one day) render the same tap its own way without
    learning the protocol.
    """

    kind: str
    title: str
    body: str
    priority: str = PRIORITY_DEFAULT
    tags: tuple[str, ...] = ()


def _human(text: object, limit: int) -> str:
    """Scrub a string of secrets and *then* bound it. Never the other way round.

    The repo rule (steward #65) in one function, because every human-facing string here has
    to obey it: bounding first can leave the live first half of a key in the output, and a
    cap applied to a redacted string can only ever cut the word ``[redacted]``.
    """
    return redact_secrets(str(text))[:limit]


def _detail_lines(detail: object) -> list[str]:
    """Render an approval's ``detail`` as body lines, redacted and bounded."""
    if not isinstance(detail, Mapping) or not detail:
        return []
    rendered = json.dumps(dict(detail), ensure_ascii=False, sort_keys=True)
    return [_human(rendered, DETAIL_MAX_CHARS)]


def _lead(manifest: ResidentManifest) -> str:
    """Build the first line of every body: who this is about, and where it lives."""
    return f"{manifest.soul.name} · {manifest.chronicle_project}"


def _knock_tap(manifest: ResidentManifest, event: ev.Event) -> Tap:
    """Render a ``needs_human`` — the one a person has to get out of bed for."""
    payload = event.payload
    body = [_lead(manifest), f"action: {payload.get('action', '')}"]
    body.extend(_detail_lines(payload.get("detail")))
    if payload.get("options"):
        body.append(f"options: {', '.join(str(option) for option in payload['options'])}")
    if payload.get("expires_at"):
        body.append(f"expires: {payload['expires_at']}")
    body.append(f"request: {payload.get('request_id', '')}")
    return Tap(
        kind=ev.NEEDS_HUMAN,
        title=payload.get("message") or f"{manifest.soul.name} needs a human",
        body="\n".join(body),
        priority=PRIORITY_HIGH,
        tags=("door",),
    )


def _task_done_tap(manifest: ResidentManifest, event: ev.Event) -> Tap:
    """Render a finished board task or delegated letter."""
    payload = event.payload
    body = [_lead(manifest), f"task: {payload.get('task_id', '')}"]
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, Sequence) and not isinstance(artifacts, str) and artifacts:
        body.append(f"artifacts: {', '.join(str(item) for item in artifacts)}")
    return Tap(
        kind=ev.TASK_DONE,
        title=f"{manifest.soul.name} finished: {payload.get('title', '')}",
        body="\n".join(body),
        tags=("white_check_mark",),
    )


#: One renderer per declarable kind, keyed by the chronicle event type it reads. A map
#: rather than an ``if`` cascade because the manifest already has a closed vocabulary for
#: these and the two have to agree: the guard below fails the import when a kind a manifest
#: may declare has no renderer here, which would otherwise be a declaration that validates,
#: reads as configured, and silently sends nothing.
_RENDERERS: Mapping[str, Callable[[ResidentManifest, ev.Event], Tap]] = {
    ev.NEEDS_HUMAN: _knock_tap,
    ev.TASK_DONE: _task_done_tap,
}

if set(_RENDERERS) != set(NOTIFICATION_KINDS):  # pragma: no cover — a guard against drift
    raise RuntimeError(
        f"manifest kinds {NOTIFICATION_KINDS} and steward.notify's renderers "
        f"{sorted(_RENDERERS)} disagree; a manifest may declare a kind nothing here renders"
    )


def tap_for(manifest: ResidentManifest, event: ev.Event) -> Tap | None:
    """Turn one chronicle event into the notice a person actually reads, or ``None``.

    ``None`` means "this event is not a tap" — the honest answer for every type steward emits
    that a human should not be woken for. Which types are tappable is
    :data:`steward.manifest.NOTIFICATION_KINDS`; which of those a *resident* has asked for is
    its own ``notifications.on``, and that question belongs to :meth:`Notifier.tap`.

    The resident's own name leads, not its id: the tap arrives on a phone, where "Hob wants to
    send email" is a sentence and ``hob/needs_human`` is a log line.

    Redaction and bounding happen **here**, once, rather than in each renderer — so a renderer
    added tomorrow cannot forget the repo rule, and cannot get redact-then-bound backwards.
    """
    render = _RENDERERS.get(event.type)
    if render is None:
        return None
    drafted = render(manifest, event)
    return replace(
        drafted,
        title=_human(drafted.title, TITLE_MAX_CHARS),
        body=_human(drafted.body, BODY_MAX_CHARS),
    )


def probe_tap(manifest: ResidentManifest) -> Tap:
    """Build the tap ``steward notify test`` sends: proof the wiring works, and nothing else.

    Named as its own thing rather than faked out of an event, because a rehearsal must not be
    able to look like a real knock on a phone at 2am. And named ``probe_`` rather than
    ``test_`` because a module-level ``test_`` function taking one argument is something
    pytest would try to collect and hand a fixture, the moment anybody star-imports this.
    """
    return Tap(
        kind="test",
        title=f"{manifest.soul.name}: notification test",
        body=(
            f"{manifest.soul.name} · {manifest.chronicle_project}\n"
            f"steward can reach this topic. Nothing happened; this is a test."
        ),
        tags=("wave",),
    )


# --------------------------------------------------------------------------------------
# how a tap is carried
# --------------------------------------------------------------------------------------


class Transport(Protocol):
    """Anything that can put a :class:`Tap` in front of a person.

    The seam warren#114 exists to draw. ntfy is the first implementation and telegram is the
    obvious second, and the shape says what a second one has to bring: a name a manifest can
    declare, somewhere to send to that it works out for itself, and a send that reports
    whether it landed without ever raising at its caller. Notably absent is any notion of a
    reply — a transport that grew one would be a chat bridge, which is warren#108's job and
    stays there.
    """

    @property
    def name(self) -> str:
        """The name a manifest declares to select this transport."""
        ...

    def address(self, manifest: ResidentManifest) -> str:
        """Where this resident's taps go, as an operator would have to subscribe to it."""
        ...

    def send(self, manifest: ResidentManifest, tap: Tap) -> bool:
        """Deliver one tap. Returns whether it landed; never raises."""
        ...


def _header_text(value: str, limit: int = TITLE_MAX_CHARS) -> str:
    """Render a string as a safe HTTP header value: one line, bounded, ASCII on the wire.

    Two separate hazards, and the first is the serious one. A header value carrying a newline
    is header injection — a title carrying one would be *writing headers*,
    and the text here comes from a session's own output. So every control character becomes a
    space before anything else happens. The second is mundane: ntfy's ``Title`` must be ASCII,
    so a name with an accent in it is RFC 2047 encoded exactly as chronicle's knock forwarder
    does it, rather than being mangled or refused by the server.
    """
    collapsed = " ".join(value.split())[:limit]
    if collapsed.isascii():
        return collapsed
    return Header(collapsed, charset="utf-8", maxlinelen=0).encode()


@dataclass
class NtfyTransport:
    """ntfy: a POST whose body is the message and whose title rides in a header.

    Proven before steward had any use for it — chronicle has forwarded ``needs_human`` knocks
    this way for months — and re-implemented here rather than reused, because the two are
    different systems with different failure budgets and share contracts rather than code.
    What is borrowed is the shape: plain-text body, ``Title``/``Priority``/``Tags`` headers,
    an optional bearer token for a protected topic, and every error swallowed.

    The circuit breaker is :class:`steward.events.EventEmitter`'s, one module over: after a
    failure the transport stops trying for :data:`BREAKER_SECONDS`, so an ntfy that is down costs
    one timeout a minute instead of one per knock during a storm of them.
    """

    base_url: str = DEFAULT_NTFY_URL
    token: str | None = None
    timeout_s: float = NTFY_TIMEOUT_S
    namespace: str = ""
    clock: Callable[[], float] = time.monotonic
    _breaker_until: float = field(default=0.0, repr=False)

    @property
    def name(self) -> str:
        """The manifest word that selects this transport."""
        return NTFY

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> NtfyTransport:
        """Build a transport from the environment. No manifest ever carries any of this.

        The server and the token are operator configuration, not resident declaration: one
        ntfy serves the whole fleet, and a token in a manifest would be a credential in git.
        """
        source = os.environ if env is None else env
        raw_timeout = (source.get(NTFY_TIMEOUT_ENV) or "").strip()
        try:
            timeout_s = float(raw_timeout) if raw_timeout else NTFY_TIMEOUT_S
        except ValueError:
            log.warning(
                "%s=%r is not a number of seconds; using %.1fs",
                NTFY_TIMEOUT_ENV,
                raw_timeout,
                NTFY_TIMEOUT_S,
            )
            timeout_s = NTFY_TIMEOUT_S
        if timeout_s <= 0:
            timeout_s = NTFY_TIMEOUT_S
        return cls(
            base_url=((source.get(NTFY_URL_ENV) or "").strip() or DEFAULT_NTFY_URL).rstrip("/"),
            token=(source.get(NTFY_TOKEN_ENV) or "").strip() or None,
            timeout_s=timeout_s,
            namespace=(source.get(NAMESPACE_ENV) or "").strip(),
        )

    def topic(self, manifest: ResidentManifest) -> str:
        """Return this resident's derived topic: the bare name, without the server."""
        return ntfy_topic(manifest.uid, self.namespace)

    def address(self, manifest: ResidentManifest) -> str:
        """Return the URL an operator subscribes to.

        Treat it like a password: on ntfy the topic *is* the capability, to read and to write.
        """
        return f"{self.base_url}/{self.topic(manifest)}"

    def send(self, manifest: ResidentManifest, tap: Tap) -> bool:
        """POST one tap at this resident's topic. Never raises, and never waits long."""
        url = self.address(manifest)
        if not url.startswith(("http://", "https://")):
            log.warning("ntfy target %r is not an http(s) URL; %s taps nowhere", url, manifest.id)
            return False
        now = self.clock()
        if now < self._breaker_until:
            # Said out loud, every time, and not only on the failure that opened the window.
            # The breaker is there so a dead ntfy costs one timeout a minute rather than one
            # per knock — but each knock it swallows is still a knock that did not reach a
            # phone, and a notification that silently stops arriving is exactly what this
            # module claims not to allow. One line per dropped tap is what makes "what did I
            # miss while ntfy was down" answerable from the log.
            log.warning(
                "%s: dropped a %s tap — ntfy failed recently and is not being retried for "
                "another %.0fs",
                manifest.id,
                tap.kind,
                self._breaker_until - now,
            )
            return False
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": _header_text(tap.title),
            "Priority": _header_text(tap.priority, limit=16),
            "Markdown": "no",
        }
        if tap.tags:
            headers["Tags"] = _header_text(",".join(tap.tags), limit=64)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(  # noqa: S310 — scheme checked just above
            url, data=tap.body.encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s):  # noqa: S310
                return True
        except (OSError, urllib.error.URLError, ValueError) as exc:
            self._breaker_until = self.clock() + BREAKER_SECONDS
            log.warning(
                "%s: could not tap %s over ntfy — %s: %s; not retrying for %.0fs",
                manifest.id,
                tap.kind,
                type(exc).__name__,
                exc,
                BREAKER_SECONDS,
            )
            return False


# --------------------------------------------------------------------------------------
# whether to send at all
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotificationReport:
    """What one resident declared, and where its taps would land. The operator's answer.

    A record rather than a dictionary, like :class:`steward.board.BoardReport` and
    :class:`steward.manifest.Diagnostic`, because the CLI renders it in two formats and a
    field renamed in one place should not be a ``KeyError`` discovered in the other.

    ``address`` is the derived ntfy topic URL, and it is the one place in steward that prints
    one. On ntfy the topic *is* the capability — to read and to write — so this record is
    written to a terminal and deliberately reaches no HTTP response.
    """

    resident: str
    transport: str | None
    status: str
    on: tuple[str, ...]
    enabled: bool
    address: str | None
    note: str | None

    def to_dict(self) -> dict[str, Any]:
        """Render as the JSON object ``steward notify list --format json`` prints."""
        return {
            "resident": self.resident,
            "transport": self.transport,
            "status": self.status,
            "on": list(self.on),
            "enabled": self.enabled,
            "address": self.address,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Notifier:
    """Reads a resident's declaration and, if it says so, taps a human. Never raises.

    One of these per owner, built from the environment by default, so the two call sites that
    tap — an approval knock and a finished board task — need no wiring of their own and a test
    can hand either of them a fake transport instead.

    Everything about *whether* to send lives here rather than at the call sites, which is what
    keeps the answer identical for a knock a session raised, a knock steward raised about a
    paused budget, and a task that closed: the resident's own ``notifications`` block, and
    nothing else.
    """

    transports: Mapping[str, Transport]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Notifier:
        """Build the transports steward knows about, configured from the environment."""
        return cls({NTFY: NtfyTransport.from_env(env)})

    def transport_for(self, manifest: ResidentManifest) -> Transport | None:
        """Return the transport this resident's taps go through, or ``None`` when it taps nobody.

        ``None`` covers three different silences on purpose — no block, a block still
        ``pending``, and a transport name this build has never heard of — because from a
        caller's side they are the same fact: nothing is going out. The third one is the only
        one worth a log line, and it gets one, because validation should have refused it and a
        manifest that got past validation into a build with fewer transports is worth saying
        out loud.
        """
        declared = manifest.notifications
        if not declared.enabled or declared.transport is None:
            return None
        transport = self.transports.get(declared.transport)
        if transport is None:
            log.warning(
                "%s declares notification transport %r, which this steward cannot deliver "
                "through; nobody is being tapped",
                manifest.id,
                declared.transport,
            )
        return transport

    def tap(self, manifest: ResidentManifest, event: ev.Event) -> bool:
        """Tap a human about one event, if this resident asked to be tapped about it.

        Returns whether a tap actually landed, which is a fact a test and a CLI can use and
        which no caller in steward branches on: a knock that reached SQLite and chronicle
        happened whether or not a phone buzzed, and a transition that failed because ntfy was
        down would be steward inventing a failure out of a courtesy.
        """
        declared = manifest.notifications
        if event.type not in declared.on:
            return False
        transport = self.transport_for(manifest)
        if transport is None:
            return False
        tap = tap_for(manifest, event)
        if tap is None:  # pragma: no cover — ``on`` is bounded to the kinds tap_for renders
            return False
        return self.send(manifest, tap, transport=transport)

    def send(
        self, manifest: ResidentManifest, tap: Tap, *, transport: Transport | None = None
    ) -> bool:
        """Hand one built tap to a transport, swallowing whatever it does. Never raises.

        The last line of defence, and it is broad on purpose. :meth:`NtfyTransport.send`
        already swallows its own errors; this catches what a *future* transport forgets to,
        because the promise this module makes to a durable transition — that reporting an
        event cannot fail the event — must not depend on every transport author remembering it.
        """
        carrier = transport if transport is not None else self.transport_for(manifest)
        if carrier is None:
            return False
        try:
            return carrier.send(manifest, tap)
        except Exception:  # a courtesy must never fail the work it reports on
            log.warning(
                "%s: notification transport %r raised while sending a %s tap; ignoring it",
                manifest.id,
                getattr(carrier, "name", "?"),
                tap.kind,
                exc_info=True,
            )
            return False

    def describe(self, manifest: ResidentManifest) -> NotificationReport:
        """Report one resident's notification wiring, address included.

        Deliberately **not** :meth:`transport_for`: that method answers "should I send", and
        this one answers "what did this resident declare, and where would it go". The
        difference is `status` — a `pending` block resolves no transport for sending and still
        has to show its address here, because that address is precisely what the operator has
        to go and subscribe to before flipping it to `active`. Reporting nothing for the block
        somebody is in the middle of wiring up would make this command useless at the one
        moment it is needed.
        """
        declared = manifest.notifications
        transport = self.transports.get(declared.transport or "")
        return NotificationReport(
            resident=manifest.id,
            transport=declared.transport,
            status=declared.status,
            on=tuple(declared.on),
            enabled=declared.enabled,
            address=transport.address(manifest) if transport is not None else None,
            note=declared.note,
        )
