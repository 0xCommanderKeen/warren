# Structured approvals (v0)

A resident running unattended will sooner or later reach something its charter does not
let it do on its own: sending the email it just drafted, spending money, touching
somebody else's calendar. It must not do the thing, and it must not silently skip it
either. It **asks** — and an approval is a question a human can actually answer, rather
than a bare knock at the door.

The pair of events is the whole audit trail:

| event | when |
|---|---|
| `needs_human` | a session reached a gated action and parked |
| `needs_human_resolved` | somebody answered — or nobody did, and it denied itself |

## The two safety properties

**Deny by default.** Every request a *session* raises carries an `expires_at`. Past it,
steward resolves the request as `deny` with `decided_by: "expiry"` and emits
`needs_human_resolved`. A gated action never proceeds because a person went to sleep. The
sweep runs on every scheduler tick, every board dispatch, and every watchdog pass, which
is what makes the deadline real rather than decorative — nothing sweeps a queue nobody
visits. A session's chosen `expires-in` is clamped to a fleet maximum of 30 days, so a
block asking for `expires-in="9999999d"` cannot push its own deadline past the reach of
deny-by-default (steward #66). Between the deadline and the sweep, the request is already
past due: it is not listed as pending (`GET /approvals?status=pending` omits it) and it can
no longer be decided — `POST /approvals/{id}` on it is a `409 approval_expired`, distinct
from the replay a request someone already answered reads back.

Two request shapes steward raises *for itself* have no `expires_at` at all: a budget pause
(`budget_unpause`) and a crash loop (`resident_restart_failed`). The grammar cannot
produce one — `expires-in` only ever parses to a positive number of seconds — and the
reason is that deny-by-default protects nothing here. Both requests are asking permission
to *undo* a stop steward already applied, so the safe state is the current one. Expiring
them would throw away the only thing that can lift the stop while changing nothing for the
better. They wait for a person, because only a person can answer them.

**First decision wins.** Decisions are recorded with a conditional write. A replay — a
double-tapped notification, a retried request — changes nothing, returns the recorded
outcome, and emits nothing new.

## How a session asks

Sessions are headless CLIs (`claude -p`, `codex exec`). There is nothing in-process for
them to call, so the mechanism is a documented protocol. There are two ways in, both
landing in the same row of the same database.

### 1. A block in the session's output

Steward acts on a block **only** from a machine-read region at the very end of the
session's final message, opened by a line reading exactly `===STEWARD-ACTIONS===` and
closed by a line reading exactly `===END-STEWARD-ACTIONS===`:

```
===STEWARD-ACTIONS===
<needs-human action="send_email" expires-in="4h" options="approve,deny,edit">
{"to": "anna@example.com", "subject": "Re: Thursday", "body": "…"}
</needs-human>
===END-STEWARD-ACTIONS===
```

A `<needs-human>` (or `<delegate>`) block **anywhere else** — in the session's prose,
inside a code fence, quoted, or copied verbatim from an attacker-supplied job or task
detail the session was handed — is ignored, and fenced and quoted spans of the region
itself are stripped before it is scanned. This is what stops a job whose detail contains a
control block from making the claiming session act on it just by quoting it back
(steward #62). The prompt every session receives teaches the region in its charter, so a
real session always wraps the blocks it means for steward.

The grammar, exactly:

| part | required | meaning |
|---|---|---|
| `action="…"` | **yes** | a slug naming the action: lowercase letters, digits, `_`, `-` |
| `expires-in="…"` | no | `<number><unit>`, unit one of `s` `m` `h` `d`. Default `24h`, clamped to a fleet maximum of 30 days |
| `options="…"` | no | comma-separated, any of `approve` `deny` `edit`. Default all three |
| the body | no | a JSON **object**, or one plain sentence (stored as `{"note": …}`) |

- Attributes are `name="value"` with double quotes, in any order. An attribute this
  table does not list is an error, not a field steward quietly ignores.
- **Every** block in the output becomes a request, in order. Unlike the `<journal>`
  block, the last one does not win: a session that gated two actions asked two
  questions, and dropping either would let one of them quietly happen, or quietly not.
- The prompt every session receives spells this grammar out in its charter section, so
  nothing has to guess the format.

**A malformed block still knocks.** An escalation steward cannot read is not dropped: it
becomes a request with the action `unreadable_escalation`, carrying the raw block and the
complaint in its detail, and it knocks like any other. A session that tried to ask and
failed must never be mistaken for a session that had nothing to ask.

The same door is used by delegation ([docs/delegation.md](delegation.md)), which is this
mechanism's sibling: a `<delegate>` block steward could not read becomes an
`unreadable_delegation` request, and one it read and *refused* becomes a
`rejected_delegation` request carrying the reason. The session that wrote the block has
already finished, so a person is the only one left to tell.

### 2. `steward approval raise`

For a session with shell access that wants the request registered before its turn ends.

```console
$ steward approval raise life-agent \
    --action send_email \
    --detail-json '{"to": "anna@example.com", "subject": "Re: Thursday"}' \
    --expires-in 4h --options approve,deny
Hob wants to send email
7f1c…-…-…                       # the request_id, on stdout
```

Token-free and local on purpose: it writes to steward's own database rather than calling
the HTTP API, because steward's API token is a credential no session should be holding.
The request appears in `GET /approvals` and knocks in burrow exactly as an output-block
request does. Use `--note` instead of `--detail-json` for an unstructured question.

Bad input is refused loudly with a non-zero exit — a malformed `--detail-json`, an
unparseable `--expires-in`, an option the API cannot accept. Nothing is written.

## Park, do not block

A session that raises an approval **finishes its turn and stops**. Holding a `claude -p`
turn open waiting for a human is expensive and fragile, and a resident sitting on a
paused session is not resting.

The decision is delivered on the resident's **next wake-up** — a scheduled routine, a
board task, or a manual `run-now` fired from burrow's viewer, which uses the same wake
hooks as the scheduler and so delivers decisions and harvests new blocks exactly as a
scheduled fire does (steward #W1) — injected into its preamble as a
`DECISIONS SINCE YOU LAST RAN` section, then marked delivered. Delivery is atomic: two
wake-ups of the same resident at the same instant cannot both be handed the same answer —
one gets it, the other opens without it, and it is delivered exactly once (steward #74).
Told once, in the session it was given to. Re-delivering on every wake-up until some run
happened to succeed would have a resident re-reading "you may send that email" for a week.

A **dry-run** wake-up (`scheduler tick --dry-run`) delivers nothing on purpose: a
rehearsal that consumed a decision would leave the next *real* session without it, so a
dry run assembles the prompt without the `DECISIONS SINCE YOU LAST RAN` section and marks
nothing delivered.

The section is framed as a record, not an order, and it sits *before* the charter, which
keeps the last word. An `approve` authorises exactly the action it names and nothing
beyond it. A decision recorded by `expiry` reads as the deny it is.

> **Deferred:** the *blocking* path — a session that genuinely waits on a decision inside
> one turn — is not implemented. The store and the events already support it; issue #10
> asks for both, and this is the half that works for `claude -p` today.

## What the charter has to do with it

The charter is where gating is declared. Its `rules` and `escalation` block say which
actions a resident may not take alone:

```yaml
rules:
  - Never send email without explicit approval; drafting is always allowed, sending never is.
escalation:
  when:
    - Anything is irreversible, costs money, or is visible to someone outside the household.
  how: needs_human
```

Enforcement is the protocol plus the prompt: the charter says *what* is gated, and the
escalation section of every prompt says *how* to ask. Steward does not intercept a
resident's tools — it cannot, they are the session's own — so a resident that ignores its
charter is a resident with the wrong charter or the wrong brain, and that shows up in the
log rather than being silently prevented.

## Answering, over HTTP

```console
$ curl -sS -H "Authorization: Bearer $STEWARD_TOKEN" \
    http://127.0.0.1:8801/approvals
$ curl -sS -X POST -H "Authorization: Bearer $STEWARD_TOKEN" \
    -d '{"decision": "approve"}' http://127.0.0.1:8801/approvals/<request_id>
```

See [docs/api.md](api.md) for `GET /approvals?status=`, the decision endpoint, and the
`edit` shape.

## Auditing

For any `request_id`, the request, its full detail, the decision, the decider, and every
timestamp:

```console
$ steward approval show <request_id>
Hob wants to send email
request:   7f1c…
resident:  life-agent
action:    send_email
detail:    {"to": "anna@example.com", "subject": "Re: Thursday"}
options:   approve, deny, edit
raised:    2026-08-24T18:02:11.004Z
expires:   2026-08-24T22:02:11.004Z
decision:  approve by api
decided:   2026-08-24T19:40:02.881Z
delivered: 2026-08-25T07:00:03.119Z
```

`--format json` for the same thing machine-readably, and `GET /approvals/{request_id}`
for the same thing over HTTP. Requests, decisions, and deliveries all survive a steward
restart: pending requests are still listed and still expire on schedule.

Three deciders show up in `decided_by`, and the ledger keeps them apart rather than
flattening them into one "denied":

| `decided_by` | what happened |
|---|---|
| a person (`api`, a burrow user) | somebody answered the knock |
| `expiry` | nobody answered before `expires_at`, so deny-by-default answered |
| `repeat` | the resident had already been told no about this action, so steward answered |

A `repeat` row is an ask steward **swallowed**: no `needs_human` event, no push
notification, no pending request. The row still exists, so "what has this resident been
asking for?" stays answerable — a wall of `repeat` denials for one action is a resident
whose charter or brain needs fixing, and that shows up in the log rather than in somebody's
phone at 03:00. The resident is told: the auto-deny is delivered in its next preamble like
any other decision, reading *"you had already been told no about that action recently"*
rather than nothing at all.

The fingerprint is `(resident, action)` and nothing finer — `detail` is free-form JSON, so
two asks that differ only in a timestamp would read as different questions and the guard
would catch nothing. The window is 12 hours, overridable fleet-wide with
`STEWARD_REPEAT_DENY_WINDOW_H` (whole hours; `0` turns the guard off). It is measured from
a *real* decision — a human's deny or an `expiry` one — never from another `repeat`, so one
no cannot renew itself into a permanent ban. Steward's own two knocks, the budget pause and
the crash loop, are exempt: they are one-per-condition already, and the deny they carry
answers a different question.

## The event payloads

`needs_human` — backwards compatible. `message` is still the one-line knock burrow
renders and ntfy forwards; everything else is additive, so a consumer that only knows the
old bare form keeps working.

```json
{
  "type": "needs_human",
  "agent_id": "claude-code:life-agent",
  "payload": {
    "message": "Hob wants to send email",
    "request_id": "7f1c…",
    "action": "send_email",
    "detail": {"to": "anna@example.com", "subject": "Re: Thursday"},
    "options": ["approve", "deny", "edit"],
    "expires_at": "2026-08-24T22:02:11.004Z"
  }
}
```

`needs_human_resolved` — `{request_id, decision, decided_by, action}`, emitted under the
resident's own agent id, because the villager burrow has to walk away from your door is
the one who knocked.

The message is **derived** from the resident's name and the action (`"<name> wants to
<action>"`), never authored by the session, so it can never disagree with the action a
decision is recorded against.

**A knock is scrubbed before it leaves the village.** The `message` and every value in
`detail` — at any depth — are bounded (a knock is a notice, not a transcript) *and*
scanned for secrets before the event is emitted. A secret a session places in a detail
field or an unstructured note — an `sk-…` key, a `BURROW_TOKEN=…` assignment, a PEM
private key, a JWT, a password in a URL — is replaced with `[redacted:secret]`, using the
same detectors that refuse a credential in a manifest. Only the secret is removed; the
rest of the knock is intact, so a person still reads the question. Redaction runs *before*
the length bound, so a secret cut in half by the cap can never surface a live prefix.
