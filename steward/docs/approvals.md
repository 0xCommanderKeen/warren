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

Each of those is written and emitted together, by the approval transition. What that seam
guarantees — a replay emits nothing, an expired request cannot be approved, a repeat
auto-deny is the one durable write that deliberately knocks on nobody — is spelled out
transition by transition in `docs/transitions.md`.

## The two safety properties

**Deny by default.** Every request a *session* raises carries an `expires_at`. Past it,
steward resolves the request as `deny` with `decided_by: "expiry"` and emits
`needs_human_resolved`. A gated action never proceeds because a person went to sleep. The
sweep runs on every scheduler tick, every board dispatch, every watchdog pass, and in a
background worker owned by the API lifespan. A late decision also explicitly sweeps the
row it found overdue; approval GET routes never mutate state. Those serve-owned paths keep
deadlines real when `steward serve` is the only running process. A
session's chosen `expires-in` is clamped to a fleet maximum of 30 days, so a
block asking for `expires-in="9999999d"` cannot push its own deadline past the reach of
deny-by-default (steward #66). Between the deadline and the sweep, the request is already
past due: it is not listed as pending, and the background sweep makes it available under
`resolved`; `POST /approvals/{id}` on it is a `409 approval_expired`,
distinct from the replay a request someone already answered reads back.

Two request shapes steward raises *for itself* have no `expires_at` at all: a budget pause
(`budget_unpause`) and a crash loop (`resident_restart_failed`). The grammar cannot
produce one — `expires-in` only ever parses to a positive number of seconds — and the
reason is that deny-by-default protects nothing here. Both requests are asking permission
to *undo* a stop steward already applied, so the safe state is the current one. Expiring
them would throw away the only thing that can lift the stop while changing nothing for the
better. They wait for a person, because only a person can answer them.

**First decision wins.** Decisions are recorded with a conditional write. In that same
SQLite transaction steward queues the resolution announcement. A replay — a double-tapped
notification or retried request — never changes the decision, but it may finish a still
pending announcement left by a process that died after committing the answer. API startup
also reconciles that queue, and a bounded background poll discovers work committed by
other API processes; an exact
retry or lease deadline can wake the worker sooner. Each accepted decision request is
linked to the approval in the same transaction as the decision/outbox row, and completion
updates every correlated pending request-log entry to `recorded`, so recovery does not
depend on the same client returning. The API returns that per-call ledger id as
`request_id` and the gated request's stable id as `approval_request_id`; repeated replays
create distinct pollable ledger rows, all correlated to the same approval.

Announcement delivery is at least once. A short SQLite lease prevents concurrent API
processes from emitting the same queued item together; a dead process's lease expires and
another process retries it. If a process dies after the receiver accepts the event but
before SQLite records that acknowledgement, the retry can repeat the event. Consumers must
therefore treat `needs_human_resolved.payload.request_id` as its idempotency key. The
decision itself remains exactly once.

After acknowledgement, completion effects use a separate SQLite lease, retry deadline,
attempt counter, and backoff. This matters for multi-process serving: only the worker that
owns the live effects token may finish it, and an abandoned token becomes claimable at its
exact deadline. For a budget approval, deleting the pause, granting the window-scoped
carry-on allowance, and marking completion are one SQLite transaction. A crash is therefore
either wholly before that act or wholly after it; it cannot leave an unpaused resident
without its allowance, or mark incomplete/refused work complete.
The lifecycle worker only reconciles announcement/effects rows after the decision or
expiry transition records them. Deadline detection and the expiry transition remain the
approval sweep's responsibility.

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
The request appears in `GET /approvals` and knocks in chronicle exactly as an output-block
request does. Use `--note` instead of `--detail-json` for an unstructured question.

Bad input is refused loudly with a non-zero exit — a malformed `--detail-json`, an
unparseable `--expires-in`, an option the API cannot accept. Nothing is written.

## Park, do not block

A session that raises an approval **finishes its turn and stops**. Holding a `claude -p`
turn open waiting for a human is expensive and fragile, and a resident sitting on a
paused session is not resting.

The decision is delivered on the resident's **next wake-up** — a scheduled routine, a
board task, or a manual `run-now` fired from chronicle's viewer, which uses the same wake
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
no cannot renew itself into a permanent ban.

The guard only answers for actions a *session chose*, and two kinds of knock are exempt:

- **Steward's own knocks** — the budget pause, the crash loop, a refused delegation. They
  are one-per-condition already, they are about the resident rather than for it, and the
  deny they carry answers a different question.
- **The catch-all actions steward assigns** — `unreadable_escalation`, and delegation's
  `rejected_delegation` / `unreadable_delegation`. One name covers every ask that lands
  under it, so "already denied" would not mean "already asked": a deny of one unreadable
  block would swallow the next one, which was about something else entirely. A malformed
  escalation and a refused handoff reach a person *every* time.

## The event payloads

`needs_human` — backwards compatible. `message` is still the one-line knock chronicle
renders and ntfy forwards; everything else is additive, so a consumer that only knows the
old bare form keeps working.

Steward now has its own way to reach a phone as well, and it is a different road: a resident
that declares [`notifications`](manifest.md#notifications--where-this-residents-outbound-taps-go)
is tapped over ntfy directly by steward, at its own derived per-resident topic, on the
applied branch of the raise. That is one-way and fires no session — see the manifest
reference. It is not a replacement for chronicle's `CHRONICLE_NOTIFY_URL` forwarder and not
coordinated with it: a fleet with both configured gets two pushes for one knock, which is a
choice an operator makes by configuring both.

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
resident's own agent id, because the villager walking away from your door is
the one who knocked.

The message is **derived** from the resident's name and the action (`"<name> wants to
<action>"`), never authored by the session, so it can never disagree with the action a
decision is recorded against.

**A knock is scrubbed before it leaves the village.** The `message` and every value in
`detail` — at any depth — are bounded (a knock is a notice, not a transcript) *and*
scanned for secrets before the event is emitted. A secret a session places in a detail
field or an unstructured note — an `sk-…` key, a `CHRONICLE_TOKEN=…` assignment, a PEM
private key, a JWT, a password in a URL — is replaced with `[redacted:secret]`, using the
same detectors that refuse a credential in a manifest. Only the secret is removed; the
rest of the knock is intact, so a person still reads the question. Redaction runs *before*
the length bound, so a secret cut in half by the cap can never surface a live prefix.

The stored row keeps what the session actually typed, so **every rendering meant for a
human scrubs it again** — each through `approvals.redact_decision`, and the list is the
whole list:

| rendering | why it is the risk |
|---|---|
| `steward show` | made to be pasted into a review |
| `steward approval show`, both formats | the audit query; a terminal scrollback or a screenshot |
| `steward board dispatch`'s `needs human:` line | the same terminal, on every sweep |
| `GET /approvals` and `GET /approvals/{id}` | the console renders it into the DOM; `curl` renders it into a scrollback |

Two readers deliberately get the raw text back, and both are the session that wrote it:
the `DECISIONS SINCE YOU LAST RAN` section of its own next session, and the line
`steward approval raise` echoes to the session that just called it. Redacting either
would misquote the question the resident asked.

Redaction and its inverse are only honest as a pair. An `edit` **replaces** the whole
detail, and the console prefills its edit box from the `detail` it was served — so an
operator wanting to change one key of a request that carried a secret is looking at
`[redacted:secret]` where a value used to be. Without a way back, their only options would
be to retype a live credential into a browser textarea or to drop the key and take the
value away from the resident that needs it.

So `POST /approvals/{id}` **restores** what it withheld: any string in the edit that is
*exactly* what the decider was shown means "I did not touch this", and the stored value is
put back before the decision is recorded. Anything else carrying the marker — one typed by
hand, or one left inside a string that was otherwise edited — is a sentence steward cannot
read, and it is refused (`422 edit_withheld_value`) rather than guessed at. The restore
lives on the same route as the redaction, because whoever withheld a value owes the way
back; a decider that was never served a scrubbed detail has nothing to put back.
