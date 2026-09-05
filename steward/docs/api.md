# The steward HTTP API (v0)

The only write path into the fleet. Human actions in chronicle's viewer — run a routine
now, post a job, answer an approval, create a resident — call this API directly from
the browser, so chronicle's server never holds write access to agents and stays the pure
reader it claims to be.

```console
$ STEWARD_TOKEN=… steward serve                    # 127.0.0.1:8801
$ steward serve --host 100.x.y.z --port 8801       # the tailnet address
$ steward serve --allow-open                       # local dev, no token, said out loud
```

This page is the prose contract. The machine-readable copy of the same routes is
`docs/openapi.json`, exported offline with `make openapi-write` — steward serves no schema
of its own, because every route here is a write path and nothing is answered
unauthenticated, `docs_url` and `openapi_url` included. It says what this page says about
the credential too: a bearer scheme on every operation and the `401` every route can
answer, so a client generated from it presents the token rather than meeting a blanket
refusal. Townhall's console reads that file in-tree and fails its own suite when its
hand-written client drifts from it (warren#321); `tests/test_openapi_contract.py` fails
when the file drifts from the routes. The request ledger list/detail, routine list,
and run-now receipt publish explicit response models, including required nullable fields.
The run path's refusal envelopes are also typed. Real HTTP responses and shared Townhall
rendering fixtures validate against the committed document; other response endpoints
remain in the checked [migration inventory](response-migration.md).

## The contract: acknowledgement, not effect

Every accepted request returns a `request_id` and one of three words — **accepted**,
**queued**, **recorded**. Never "done", never "ran". The API cannot confirm an effect
because it does not perform one: it hands work to the scheduler, the board, or the
disk, and the effect is confirmed only when the matching protocol event lands in
chronicle's log.

| you asked for | the effect is confirmed by |
|---|---|
| run a routine now | `routine_started`, then `routine_finished` / `routine_failed` |
| post a job | `task_posted` (and later `task_claimed` / `task_done`) |
| decide an approval | `needs_human_resolved`, then the resident's next action |
| delegate to a resident | `task_delegated`, then `task_claimed` / `task_done` |
| create a resident (`deploy: false`) | nothing — it is a file for review, not a deployment |
| create a resident (`deploy: true`) | the resident's own first event, and nothing sooner |

The UI must treat the event stream as the only source of truth. No optimistic state:
a village that shows work the fleet has not confirmed is a village that lies.

The other half of that promise is that **a request that cannot work fails immediately
and specifically** rather than queueing into silence. Unknown resident, unknown
routine, routine disabled in the manifest, a run already in flight — each is its own
status and its own error code, and nothing is written for a request that was refused.

Every accepted mutating request is logged to `.steward/state/steward.db` with its
outcome, so a queued action that later failed is traceable. A failure surfaces as a
truthful event (`routine_failed`, `needs_human`) and never as a synthesized success.

## Body validation

Malformed bodies and bodies outside these limits are FastAPI/Pydantic `422` responses.
Validation happens before a route runs, so a rejected body writes no request or domain
row, emits no event, and reaches no resident prompt. The limits are repeated at the
durable transition/delegation seams where a CLI or an in-process caller can otherwise
bypass HTTP validation.

| body | field | limit |
|---|---|---|
| `POST /jobs` | `title`, `detail` | 200 and 8,000 Unicode characters |
| `POST /jobs` | `required_skills` | 100 items; each identifier 100 characters |
| `POST /delegate` | `title`, `detail` | 200 and 8,000 Unicode characters |
| `POST /delegate` | `to`, `route`, `from`, `parent_task_id` | 100 characters each |
| `POST /approvals/{id}` | `edit` | 16 KiB compact UTF-8 JSON; 8 container levels; 100 members/items per container; 1,000 values; strings 8,000 and keys 200 characters |

The work-text limits deliberately match the `<delegate>` block grammar, so the same
task cannot grow larger depending on which door it entered through. Identifier limits
comfortably cover resident/route slugs and steward's generated IDs while bounding lookup
and near-match work. Approval edits get a structural budget as well as a serialized byte
budget because they are stored verbatim and rendered into the resident's next prompt;
the validator walks iteratively before serializing, so deeply nested input does not cause
recursive validation work.

These are all API bodies: `JobPost`, `HandoffPost`, `ApprovalDecision`, and
`ResidentPost`. `ResidentPost` inherits the shared `NewResident` declaration model used
by the CLI, including its manifest/domain validation; it writes reviewable manifest and
soul files rather than job/event/prompt payloads, so its broader declaration prose is not
part of the work-envelope limits above. Route path and query parameters are separate
inputs and retain their endpoint-specific validation. SQLite stores these values in
`TEXT` columns and supplies no length constraint itself.

The OpenAPI schema and interactive docs are disabled, as described below, so these
limits are documented here rather than advertised at `/openapi.json`.

## Auth

Three kinds of caller present three kinds of credential. The **master token**
(`STEWARD_TOKEN`) reaches everything and names nobody; a named **operator credential**
reaches the same things and names a person; a **session** presents the credential steward
minted for its own run and may reach very little. See
[Three kinds of caller](#three-kinds-of-caller) below.

**The master token** is one shared secret, exactly like chronicle's ingest auth.

- `STEWARD_TOKEN` in steward's environment; `Authorization: Bearer <token>` on the
  request.
- Compared with `hmac.compare_digest`, so a wrong token cannot be discovered one byte
  at a time by timing requests.
- An empty or whitespace-only value **counts as unset**. Unset means the server
  refuses to start, naming the variable, unless `--allow-open` says out loud that this
  is local development.
- Invalid credentials receive `401`, then `429` when their source exhausts its failure allowance.
  No work is queued; bounded authentication summaries enter the request log.
- Exactly one `Authorization` header is required. Duplicate fields are `401`, even when
  one or both values contain the right token, so intermediaries cannot disagree about
  which credential wins.

Approval-decision request bodies have a 128 KiB wire limit, enforced while ASGI chunks
arrive and before JSON parsing. Six times the 16 KiB semantic edit budget admits the
worst legal JSON spelling, where each ASCII content byte is written as a six-byte
`\uXXXX` escape; two further edit budgets cover the escaped decision/envelope and
bounded formatting slack. Arbitrary whitespace, duplicate-key padding, malformed JSON,
and oversized strings remain bounded. Crossing the wire limit returns
`413 approval_body_too_large`; structurally or semantically invalid bodies within it
return `422`. Neither refusal has side effects.

Reads are gated too. Every endpoint here is a write path except the resident views and
the skills listing, and gating those as well is simpler than explaining which is which
— there is one door. The OpenAPI schema and `/docs` are not served at all, for the same reason.

**There is no exception.** Steward used to mount a management console at `/ui` and serve
its three static files unauthenticated, because a browser has to load a script before there
is anything to ask a human for a token with. That console is retired (warren#225) — its
views live in townhall, which is served from its own origin and reaches these routes
same-origin through the NAS's nginx with an operator credential. Steward is a pure API: no
byte it serves is unauthenticated.

**Tailnet only.** The default bind is `127.0.0.1`; in deployment steward listens on
its tailnet address and is never exposed to the public internet. One shared human token is
the whole of its auth against an operator — session credentials narrow what a *resident*
may do, not what an intruder may — and that is only enough behind a private network.

## Three kinds of caller

`STEWARD_TOKEN` is a master key, not an identity: one shared secret, one constant-time
compare, no principals. That is the right shape for a terminal and the wrong shape
for a session — with it, the resident that *raises* an approval can also *decide* it, and
a resident can sign a letter with any other resident's name. So a session gets its own
credential instead (steward #41).

It is also the wrong shape for a **browser**. Townhall is a write surface on the shared
origin, and pasting the master token into a tab hands every operator the key that also
boots the server, that nobody can revoke without a redeploy, and that names nobody in the
audit trail it leaves behind. So a named operator gets their own credential too
(warren#225) — see [Operator credentials](#operator-credentials).

| | master token | operator | session |
|---|---|---|---|
| credential | `STEWARD_TOKEN` | `steward-operator-…`, minted by name | `$STEWARD_SESSION_TOKEN`, beside `$STEWARD_URL` in the session's environment |
| minted | by the operator, once, in the environment | `steward operator mint <name>` | by steward, per run, at fire time |
| identity | none — it is a shared secret | the person it was minted for | the resident whose run it is |
| stored | in steward's environment | as a SHA-256 digest in `operator_credentials` | as a SHA-256 digest in `open_runs` |
| ends | when the operator rotates it and restarts | `steward operator revoke <name>`, on the next request | with the run: on close, timeout, or a stale lease |
| may read | everything | everything | everything |
| may write | everything | everything | `POST /delegate`; named manifest grants may open narrow doors |
| commits as | `steward (api)` | that person, by name | `<resident> (session)` when granted |

The write allowlist below is about **sessions** and is untouched by operator credentials.
It exists to keep a running resident out of human acts, and an operator is the human.

**What a session may reach.** Every `GET`, plus `POST /delegate`. Named `session_grants`
may open five deliberately narrow doors beside that permanent allowlist:

- `skills.write` permits `POST /skills` and `PUT` of an ungranted skill. It may not set
  `defaults: true` or replace a skill any resident manifest already grants.
- `residents.declare` permits `POST /residents` with `deploy: false` only. The skeleton is
  validated and committed exactly like an operator's, authored by the session resident.
- `residents.dry_run` permits `POST /residents/{id}/provision` with an explicit
  `dry_run: true` only. It returns the full bundle, compose, command, next-fire and
  environment-key plan without reaching a host.
- `residents.rehearse` permits `POST /residents/{id}/rehearse`: one throwaway turn run
  from a declaration, in a scratch directory, with no container, no mounts, no memory
  directory, no credential, no tools and no steward event. **Never implied by
  `residents.dry_run`**, and that is the point of it being a
  name of its own (warren#446): a dry run reads a plan and costs nothing, a rehearsal runs
  a model turn and spends the caller's own budget line.
- `residents.grant_skill` permits `PUT /residents/{id}/declaration` **only against an
  approval a human answered** — see [An approved declaration
  edit](#an-approved-declaration-edit). It is the one grant that opens nothing by itself.

The narrowings and every other write path are `403 session_credential_forbidden`, naming
the act, and nothing is recorded. In particular, retirement stays closed under every grant,
and so does any declaration edit that no decision covers. The allowlist consults the
resident named by the live credential; request data cannot name a different one. Adding a
route does not make it session-reachable by accident.

That allowlist is what makes the write API (steward #214) safe to have at all. A resident
that could `PUT` its own declaration would be choosing the rules it is held to, and one
that could write a skill without a grant would be handing itself instructions nobody
approved — so `POST /reload` remains closed, declaration edits open only for one approved
line at a time, and skill writes require the narrow grant above. Each refusal names the
act. Reading a declaration stays open: a resident that could not see its own charter could
not follow it.

Reads are *not* narrowed, and that is a decision rather than an omission. A locally placed
session already has `steward.db` and the residents tree on the same disk — that is how
`steward delegate` and `steward approval raise` work at all — so narrowing what it may read
over HTTP would move nothing it could not read directly.

**And that cuts both ways, so say it plainly: this is a boundary, not a sandbox.** A
session with shell access and `$STEWARD_STATE` can open `steward.db` and write to it — it
can record an approval decision with `sqlite3` that the API would have refused it. What
these refusals buy is that steward's *own* write path no longer treats a session as an
operator: the API stops being the easy door, every refusal is logged, and a session that
takes the other road has to do something no resident's charter describes. Real containment
needs the session to have neither the database nor the residents tree, which is container
placement's job, not this issue's.

Six of those refusals name the act rather than the rule, because the act is the part
worth knowing:

- **`POST /approvals/{request_id}`** — deciding an approval is the human end of the
  escalation boundary. A session that could decide would be answering its own knock, and
  every guarantee downstream of "a human decided", expiry's deny-by-default included,
  would only be as strong as the session not noticing.
- **`POST /residents`** — declaring a resident is a human act unless the caller holds
  `residents.declare`; even then, deployment remains human-only.
- **`POST /residents/{id}/provision`** — provisioning is starting a container on a machine
  over ssh. `residents.dry_run` opens only its no-host planning form. Named separately from
  declaring, and matched ahead of it, because the two are different acts and a refusal that
  called this one "declaring" would be describing something the caller did not try.
- **`POST /residents/{id}/rehearse`** — a rehearsal runs a model turn and spends money.
  `residents.dry_run` buys the free half of "check this declaration before building it"
  and deliberately does not buy this half.
- **`POST /residents/{id}/retire`** — retiring is ending a resident: a mark in git, a
  container stopped, a village token removed. A session that could do it would be deciding
  which of its colleagues carries on, or dismissing itself. Matched ahead of declaring for
  the same reason provisioning is.
- **`POST /residents/{id}/routines/{routine}/run`** — firing a routine is a human act; a
  session's own work arrives through the board and its inbox.
- **`PUT /residents/{id}/declaration`** — a resident's charter, skills and routines are
  written *about* it rather than by it. `residents.grant_skill` opens one skill line at a
  time and only against a human's yes; everything else about this door stays shut.

**`POST /delegate` derives the sender from the credential.** Omitting `from` no longer
means "a person asked" for a session caller: it means *this* resident, and its charter is
checked exactly as a `<delegate>` block's would be. A body naming a different `from` is
`403 sender_not_the_caller` rather than honoured. The chain needs no separate rule —
`Delegator._resolve_parent` already derives the parent from the tasks the sender is
actually holding (steward #67), so binding the sender binds the lineage with it.

**The credential expires with its run**, on the run's own ownership lease: it is accepted
while the run is open, no terminal fact has been chosen for it, and its heartbeat is fresher
than the watchdog's grace window. There is no second clock. A credential that leaked into a
transcript is worthless by the time anybody reads the transcript, and only its SHA-256 is
stored, so a copy of `steward.db` yields no live credentials.

**Raising an approval is not on this list, and does not need to be.** There is no endpoint
to raise one at all — the routes are `GET /approvals`, `GET /approvals/{request_id}` and
the human-only `POST /approvals/{request_id}`. A session raises through a `<needs-human>`
block or `steward approval raise`, both of which are token-free and local. This credential
buys denial and identity, not new reach.

**`--allow-open` has no boundary**, and does not pretend to. There is no token to compare,
so every caller is the human one and a session can reach any route with no header at all.
The credential is still minted, so nothing about a run changes shape between modes — only
what the API is able to enforce does.

## Operator credentials

The credential a browser holds (warren#225). It reaches exactly what `STEWARD_TOKEN`
reaches — the session allowlist is about sessions and is untouched — and what it adds is a
name, which is what makes an audit trail worth keeping.

```
$ steward operator mint Miha --email miha@example.invalid --note townhall
minted an operator credential for Miha
commits will be authored by Miha <miha@example.invalid>
this is the only time steward can show it — only its digest is stored:
steward-operator-XQ0v…

$ steward operator list
live  Miha  <miha@example.invalid>
      minted 2026-08-31T18:04:11.902Z · live · townhall

$ steward operator revoke Miha
revoked Miha's credential at 2026-08-31T19:22:03.118Z
```

- **Printed once.** Only `sha256(credential)` reaches `steward.db`, so a copy of the
  database yields no live credentials and steward cannot show one again. Losing one means
  revoking and minting another.
- **Revocable, immediately.** `revoke` stamps the row and the next request presenting it is
  `401`. The row is kept rather than deleted: *who could act as this fleet's operator, and
  until when* is exactly what an audit asks, and a missing row cannot answer it.
- **Named.** A write made with one is committed by that person rather than by
  `steward (api)`, the commit trailer says so, and a job posted or an approval decided with
  one records their name instead of `api`. `STEWARD_COMMIT_IDENTITY` remains what the
  master token and open mode commit as.
- **No HTTP path mints, revokes, or lists one.** A credential that could mint its successor
  would make revocation a suggestion, so the terminal is the only door.
- **Recognisable, and therefore redactable.** The `steward-operator-` prefix is in
  steward's secret-value patterns, so one pasted into a manifest fails validation and one
  echoed by a session never survives into a village event.

There is no expiry. A second clock would be a promise steward would then have to keep
across restarts, and revocation is the honest mechanism: it is one command, it is
immediate, and it leaves a record.

## CORS

`STEWARD_CORS_ORIGINS` is a comma-separated list of origins allowed to call the API
from a browser — chronicle's viewer origin, typically. Unset means no origin is allowed,
so no CORS headers are sent to anyone. Allowed origins can use GET, POST, and PUT
with `Authorization` and `Content-Type` headers; OPTIONS preflights require no token.

```console
$ STEWARD_CORS_ORIGINS=http://village.local:8080 STEWARD_TOKEN=… steward serve
```

## Endpoints

### `POST /residents/{id}/routines/{routine}/run`

**Human callers only** — the master token or an operator credential. A session credential is refused here; see
[Three kinds of caller](#three-kinds-of-caller).

Fire one routine now. Validated against the resident's manifest before anything is
queued: an unknown resident or routine is a `404`, not an enqueue.

```console
$ curl -sS -X POST -H "Authorization: Bearer $STEWARD_TOKEN" \
    http://127.0.0.1:8801/residents/hob/routines/inbox-read/run
{"request_id": "…", "status": "accepted", "trigger": "manual", …}
```

`202` with a `request_id`. The run goes through the scheduler's own fire path **with the
same wake hooks the scheduler daemon runs with**, so it gets the same prompt assembly, the
same timeout, the same runner seam, and the same bracketing events as a scheduled fire —
with `routine_started.payload.trigger` set to `manual` instead of `schedule`. The same
trigger is persisted on the open-run registry and finished-run ledger, so reconciliation
can tell prompted work from standing work even after the event stream is unavailable.
Because the hooks are the same, a run-now session
delivers the resident's pending approval decisions into its preamble and harvests any
`<needs-human>`/`<delegate>` blocks it emits, exactly as a scheduled fire does — a manual
fire is a fire, not a lesser one (steward #W1).

| status | error | meaning |
|---|---|---|
| 404 | `unknown_resident` | no such resident in the residents tree |
| 404 | `unknown_routine` | the manifest declares no routine by that name |
| 409 | `routine_disabled` | declared but `enabled: false`; enable it in the manifest |
| 409 | `already_running` | the overlap rule: skipped, never queued. Either this process already has that routine in flight, or another process — the scheduler daemon, a board dispatch — holds the resident's session claim; the message names which, and what is running (warren#111) |
| 409 | `budget_exceeded` | `paused: budget exceeded`; see [`GET /residents/{id}/budget`](#get-residentsidbudget) |
| 409 | `resident_invalid` | the resident exists but its manifest does not validate |
| 409 | `resident_retired` | the manifest declares `retired: true`; it takes no work at all |

The run's consumption — tokens, money, seconds — lands on the durable ledger like any
other session's, and its timeout is capped by the manifest's `budgets.max_run_seconds`.
Asking for a run now is not a way around a budget the same human set.

### `POST /jobs` · `GET /jobs`

```json
{"title": "Research X", "detail": "…", "required_skills": ["research"]}
```

`202` with a `task_id`. The task is persisted (SQLite, so a claim is a single atomic
`UPDATE … WHERE status = 'open'`) and announced as `task_posted` with
`{task_id, title, required_skills, posted_by: "api"}`. **No resident is prompted:**
dispatch is pull-based, and a resident claims work on its own next wake-up.

`GET /jobs` lists the board, oldest first, with `?status=open|claimed|done|failed` to
narrow it. Work delegated to one named resident lives in the same table and shows up here
with its `assignee` set, but it is never on the open board: nobody else can claim it.
Anything else is a `422` with `unknown_status` rather than a silently ignored parameter.
Each job carries its `claimant`, `lease_expires_at`, `outcome`, `reason`, and `artifacts`
as they become true.

The lifecycle, all four transitions visible in chronicle's log and reconstructible from
events alone:

| event | when | agent id |
|---|---|---|
| `task_posted` | this endpoint accepted it | `steward:api` |
| `task_claimed` | one resident atomically claimed it | the claimant |
| `task_done` | the claimant finished; carries `artifacts` | the claimant |
| `task_failed` | the claimant gave up, or its lease expired | the claimant |

`task_done` and `task_failed` also carry the `run_id` of the session that did the work,
because a task id is not a session: claim, die, expire the lease, re-claim, and every
attempt shares the task id. The one exception is a lease expiry, which is the board
mourning a claim rather than a session reporting back and so names no run.

Claiming is documented with the manifest's `board` block in
[docs/manifest.md](manifest.md#board--job-board-participation). A resident claims only
what it declared it would, and only tasks whose `required_skills` are a subset of the
skills it holds — its *effective* set, the library's defaults plus its own grants
([docs/manifest.md](manifest.md#the-skills-library)), so `["research"]` is claimable only
by a board-enabled resident explicitly granted `research`.

### `POST /delegate` · `GET /residents/{id}/inbox` · `GET /tasks/{id}/lineage`

Hand work from one resident to another. The human path into delegation — a session usually
uses the `<delegate>` block or `steward delegate`, neither of which holds a token at all.
It is also the one write path a session credential reaches, and it behaves differently for
one: see [Three kinds of caller](#three-kinds-of-caller).

```json
{"from": "sender-resident", "to": "hob", "route": "inbox",
 "title": "Check the errand list", "detail": "…", "parent_task_id": "…"}
```

`202` with a `task_id`, the `depth` it landed at, and the `origin` the chain rolls up to.
The item is delivered into the receiver's inbox and announced as `task_delegated` naming
both ends; **no resident is prompted**, and the receiver works it on its own next wake-up.

`from` names the resident handing the work over, and its manifest is checked exactly as it
would be for a block — a person must not be able to make a resident do what its own
declaration forbids. Omit `from` and the *person* is the sender: the token is the
permission, and the receiver's declared route is the whole of the agreement.

**Unless the caller is a session**, in which case `from` is derived from its credential.
Omitting it then means *this* resident rather than "a person asked", and naming a different
resident is `403 sender_not_the_caller`.

Steward is the sole arbiter, and a refusal writes nothing and emits nothing:

| status | error | meaning |
|---|---|---|
| 404 | `unknown_resident` | no such sender in the residents tree |
| 404 | `unknown_recipient` | no valid resident by that id (the near miss is named) |
| 404 | `unknown_parent` | the named `parent_task_id` is a task steward has never seen |
| 409 | `not_permitted` | the sender's manifest has no `delegation: {send: true}` |
| 409 | `recipient_not_allowed` | the sender's `to:` list does not name this receiver |
| 409 | `self_delegation` | a resident cannot hand work to itself |
| 409 | `unknown_route` | the receiver declares no route by that id |
| 409 | `route_not_delegable` | the route exists but is not of kind `delegation` |
| 409 | `route_inactive` | the route is `pending` or `disabled` |
| 409 | `max_depth_exceeded` | the chain is already as long as steward allows |
| 409 | `cycle` | the receiver is already somewhere in this task's lineage |
| 403 | `sender_not_the_caller` | a session credential named a `from` that is not its own resident |

`GET /residents/{id}/inbox` lists what is waiting for a resident — open by default,
`?status=open|claimed|done|failed|all` to narrow, anything else a `422`. Each item carries
`delegated_by`, `route`, `depth`, `origin`, and `parent_task_id`.

`routes` names the doors that resident declares, **with their status** — `{"id": "handoff",
"status": "disabled", "accepts": false}` — and `pending` is the open count whatever
`?status=` asked for. Both because a route somebody shut stops pickup while the letters
already delivered keep waiting: a caller shown only the accepting routes would see no door
at all and could not say why nothing is moving.

`GET /tasks/{task_id}/lineage` is the audit query: the whole chain, root first, with the
origin it attributes to. `404` for a task steward has never seen.

The lifecycle after delivery is the board's, unchanged: `task_claimed` on pickup, then
`task_done`/`task_failed` — all three carrying `parent_task_id`. The declarations both ends
must make, the block grammar, and the guardrails are in
[docs/delegation.md](delegation.md).

### `GET /approvals` · `GET /approvals/{request_id}` · `POST /approvals/{request_id}`

The two reads are open to both kinds of caller; **the decision is human-only** — see
[Three kinds of caller](#three-kinds-of-caller).

`GET /approvals` lists gated actions. `?status=pending` (the default), `resolved`, or
`all`; anything else is a `422` with `unknown_status`. The default is unchanged from
before the parameter existed, so a panel that never passed it sees exactly what it saw.
The API's lifespan runs the ordinary expiry transition in the background, including when
no scheduler, dispatcher, or watchdog daemon is running. It records the deny, emits
`needs_human_resolved`, and leaves the decision ready for the resident's next wake-up.
GET routes remain read-only: a pending-list read hides a row already past its deadline,
but neither polling nor looking up an unknown id causes a sweep.

`GET /approvals/{request_id}` is the audit query: request, full detail, decision,
decider, and every timestamp, in one call. `404` for an id steward has never seen.

`POST /approvals/{request_id}` answers one:

```json
{"decision": "approve" | "deny" | "edit", "edit": {"subject": "shorter"}}
```

The decision is recorded durably and emitted as `needs_human_resolved` with
`{request_id, decision, decided_by, action}`, under the *resident's* agent id — the
villager walking away from your door is the one who knocked. If no durable event sink
accepts it immediately, the response and request log say
`recorded_announcement_pending`; the lifecycle-owned worker retries without another
client request or restart. Completion effects such as budget resume happen only after
that acknowledgement and are themselves crash-recoverable. A replay consults the durable
outbox row: it returns `recorded_announcement_pending` and wakes the worker while delivery
is pending, but returns the ordinary idempotent `recorded` response after acknowledgement.
Every accepted response returns its own request-log id as `request_id`, suitable for
`GET /requests/{request_id}`, and names the gated request separately as
`approval_request_id`. Each replay therefore has a distinct correlated request-log row;
when recovery completes, all rows correlated to that approval become `recorded`.

The decision must be one of that request's `options`. A globally known but unoffered
decision is a `409` with `approval_decision_not_offered` and the request's `offered`
set; the row remains pending and no resolution event is emitted.

Decisions are idempotent: the first one wins. `202` the first time; a replay (a
double-tapped notification, a retried request) is `200`, returns the recorded outcome,
changes nothing, and emits nothing. An unknown `request_id` is `404`. A request that has
already **expired** is a `409` with `approval_expired` — distinct from the replay of an
already-decided one. Deny-by-default has the last word; the late POST explicitly sweeps
the row to `deny` and emits its resolution (steward #66, #143).

The lifecycle worker reconciles announcement and completion-effects rows only after a
decision or expiry transition has recorded them. It does not decide when deadlines expire;
the existing approval sweep owns that transition.

Requests are *created* by the session that reaches a gated action — through a
`<needs-human>` block in its output or `steward approval raise`, both documented in
[docs/approvals.md](approvals.md). The API only ever answers a request; it never invents
one. A request nobody answers before its `expires_at` resolves itself as `deny` with
`decided_by: "expiry"`, and the resolution event lands like any other.

### `POST /residents`

**Human callers only** — the master token or an operator credential. A session credential is refused here; see
[Three kinds of caller](#three-kinds-of-caller).

```json
{
  "id": "note-keeper", "name": "Quill", "char": "Scribe",
  "accent": "#4f7ea6", "role": "note bot",
  "charter": {"mission": "…", "duties": ["…"], "rules": ["…"], "escalation": "…"},
  "skills": ["research"], "runner": {"kind": "claude", "model": "claude-opus-5"},
  "deploy": false
}
```

Runs the same `steward.nursery.raise_resident` pipeline `steward new-resident` runs —
the endpoint and the command are one implementation, verified by a test that injects the
pipeline rather than by a convention somebody has to keep.

**`deploy` defaults to `false`**, so the endpoint's old behaviour is still its default:
`residents/<id>/manifest.yaml` and `residents/<id>/soul.md` are written and read back
through the ordinary validator — checked, not claimed — and nothing else happens. No
container, no scheduler registration, no event on the new resident's behalf. A skeleton
that fails validation is removed rather than left to break the tree for everyone.

**`deploy: true`** additionally provisions the container and checks the schedule. Asking
for that is asking steward to reach a machine over ssh and start something on it, which
is not a thing a request should be able to do by leaving a field out.

**This endpoint commits, since steward #214.** It used to commit nothing, on the grounds
that the server may not own the checkout it is reading. The cost of that was that every
resident raised from a control panel was left with no history and no author — the newest
declarations in the fleet being the only unrecorded ones, which is exactly backwards.

`declare.commit` is still `null`: that is the *nursery's* commit, and the API still asks
the nursery not to make one, because the nursery's commit is bound up with its
dirty-worktree refusal, which is right for a terminal and wrong for a long-running server.
The commit that actually happened is the top-level `commit` key, and it stages only the two
files that were written. See [Writing declarations and skills](#writing-declarations-and-skills)
for the shared rules, including what happens when the tree is not in a checkout at all.

`201` with the paths it wrote and the full report:

```json
{"request_id": "…", "status": "accepted", "message": "…",
 "id": "note-keeper", "manifest_path": "residents/note-keeper/manifest.yaml",
 "soul_path": "residents/note-keeper/soul.md",
 "changed": true, "act": "raise",
 "declare": {"written": true, "commit": null, "note": "declared and validated"},
 "provision": {"target": {"host": "dxp2800", "user": "Miha",
                          "path": "~/docker/warren/residents/note-keeper",
                          "container": "steward-note-keeper", "image": "steward-resident:latest"},
               "files": ["docker-compose.yaml", ".env", "manifest.yaml", "soul.md"],
               "compose": "services:\n  note-keeper:\n…",
               "compose_changed": true, "env_keys": ["CHRONICLE_TOKEN", "CHRONICLE_URL"],
               "commands": ["ssh Miha@dxp2800 docker compose … up -d"], "sent": true},
 "register": {"ok": true, "problems": [],
              "next_fires": [{"routine": "tidy-notes", "at": "2026-06-15T20:00:00+02:00"}]}}
```

`env_keys` is **names only, forever**. The values are read from steward's own environment
at provision time, written into a `.env` on the host at mode `0600`, and never returned,
logged, or committed — see [the nursery's secret rule](#secrets-and-the-nursery).

Posting the **same body twice** is `201` and converged: `declare.written` is `false`,
`provision.sent` is `false`, `changed` is `false`, and nothing was written or uploaded.
Posting a **different** declaration under a name that already exists is `409
resident_not_declared`, naming the fields that disagree — the skeleton is a starting
point somebody then edits, and a request must not be able to overwrite a soul from a form.
The refusal names the door that does work:
[`POST /residents/{id}/provision`](#post-residentsidprovision) builds the declaration as it
stands, which is how a resident carrying a field no body can express gets built at all.

`409 resident_retired` when the id belongs to a retired resident: un-retiring is a
person's decision, written into the manifest and committed, not something an HTTP call
does on the way past.

`409 provision_refused` when `deploy: true` and there was nobody to ask — in practice a
steward whose own environment has no `CHRONICLE_URL` to give the container. **This used to
be a `500` with a traceback**, which told a control panel nothing it could act on. The
declare stage has already written its two files by the time the deploy is attempted and
nothing has committed them, so the refusal says what the next move is: post the same body
again once the address is exported, and it converges on the skeleton rather than colliding
with it. Same error name as the provision door's, because it is the same refusal.

### `POST /residents/{id}/provision`

**Human callers only** — the master token or an operator credential. A session credential is refused here; see
[Three kinds of caller](#three-kinds-of-caller).

```json
{"dry_run": false}
```

The body is optional and holds one field, because **the manifest is the request**. Everything
`POST /residents` takes in a body, this reads off `residents/<id>/manifest.yaml`.

That is the whole point of the door (warren#270). `POST /residents` assembles a declaration
from what a caller says and refuses to converge it onto a manifest somebody has since
edited — rightly, since a form must not be able to overwrite a soul. But no body can express
a `route`, an `app_grant`, a skill's `note` or a `runner.placement`, so a resident carrying
one of those could never match, and had no way onto the nursery path at all. This endpoint
skips declare entirely: the declaration is already there, and provision and register run
against it exactly as it stands.

Runs the same `steward.nursery.provision_resident` pipeline `steward provision` runs — one
implementation, verified by a test that injects the pipeline into the route.

**`200`, not `202`.** The container is up and the schedule has been checked by the time this
answers, so there is nothing left to acknowledge later — see
[The contract](#the-contract-acknowledgement-not-effect) for why the difference matters.

**`dry_run: true`** returns the whole plan — the bundle, the compose fragment, the exact
argv a real run would issue, and the next fire of every routine — and reaches no host at
all: nothing is sent, nothing is run, nothing is written. It is the rehearsal a control
panel offers before the button that does it for real.

**This endpoint commits nothing, and that is not the reservation `POST /residents` used to
make.** It writes nothing into the checkout, so there is no declaration for the write path
to record. The declaration being provisioned was committed by whoever wrote it — and when
it was *not*, that comes back in `warnings` naming the files, because a container built
from bytes in no commit is a container nobody can turn back into a diff, and refusing over
a commit this endpoint cannot make would leave the caller nowhere to go.

`200` with the same report `POST /residents` returns. Both carry `act`, which says which
door the run came through — `"raise"` there, `"provision"` here — so a reader of the report
never has to infer it from which fields happen to be filled in:

```json
{"request_id": "…", "message": "…",
 "resident": "hob", "act": "provision", "changed": true, "dry_run": false,
 "declare": {"written": false, "commit": null,
             "note": "already declared; provisioned from the manifest itself"},
 "provision": {"target": {"host": "dxp2800", "container": "steward-hob", "…": "…"},
               "files": ["docker-compose.yaml", ".env", "manifest.yaml", "soul.md"],
               "compose_changed": true, "env_keys": ["CHRONICLE_TOKEN", "CHRONICLE_URL"],
               "sent": true},
 "register": {"ok": true, "problems": [], "next_fires": []},
 "warnings": []}
```

Provisioning the **same manifest twice** is `200` and converged: the bundle already on the
host is compared file by file and not re-sent, `sent` and `changed` are `false`, and
`docker compose up -d` is issued anyway — reconciling a container that is *down* is what a
second run is for.

The HTTP counterpart to `steward retire` is
[`POST /residents/{id}/retire`](#post-residentsidretire), added by warren#331. warren#270
left it out deliberately — retiring stops a container and scrubs a village token, and
nothing had asked for it — and what asked for it was the console: a page that could show a
`retired` badge and had no way to make one true.

The refusals:

| status | `error` | when |
|---|---|---|
| `404` | `unknown_resident` | no `residents/<id>/` in the tree; the message names the id you probably meant |
| `409` | `resident_retired` | the manifest says retired — coming back is a person's decision written into the file and committed, not something an HTTP call does on the way past |
| `409` | `declaration_invalid` | the declaration on disk does not validate, so steward will not deploy from it; the message names the fields that failed. Its own name, not the `422 manifest_invalid` of the write paths: that one means *the bytes you sent* would not validate, and no bytes were sent here |
| `409` | `provision_failed` | the host answered and refused — a bundle that would not land, a `docker compose up` that failed. The declaration is untouched, so fixing the host and re-running picks up where it stopped |
| `409` | `provision_refused` | there was nobody to ask: the host did not answer, or steward's own environment has no `CHRONICLE_URL` to give the container. The message says which |


### `POST /residents/{id}/retire`

**Human callers only** — the master token or an operator credential. A session credential is
refused here; see [Three kinds of caller](#three-kinds-of-caller).

```json
{"dry_run": false, "revision": "sha256:…"}
```

The manifest is the request. First send `{"dry_run": true}`; its answer includes the
declaration `revision`. Execution requires that revision and refuses if the declaration
changed, so the confirmed plan is always the plan that runs.

**Retirement is not a manifest edit**, which is the whole argument for this route existing.
Writing `retired: true` through `PUT /residents/{id}/declaration` marks the resident and
leaves its container running on the host with a live `CHRONICLE_TOKEN` in the `.env` beside it
— the half that matters most left undone. This runs the whole act, in the one order that is
safe:

1. `retired: true` into `residents/<id>/manifest.yaml`, read straight back through the
   ordinary validator;
2. commit it;
3. emit the authoritative `resident_retired` lifecycle fact;
4. `docker compose down --remove-orphans`;
5. `rm -f` the `.env` and the compose file.

**Marked before stopped**, always. `retired: true` is what takes the resident out of the
scheduler, the board, delegation, run-now — and out of the watchdog, which would otherwise
notice the container go away and dutifully restart it. And **the `.env` is removed after the
stop**, because `docker compose down` reads it: `CHRONICLE_URL` is interpolated as
`${CHRONICLE_URL:?…}`, so scrubbing first makes the stop fail on a missing variable.

What is deliberately **left**: `residents/<id>/` and its history — retirement is a lifecycle
state, not a deletion — the resident's memory directory on the host, and `claude/`, which is
bind-mounted to `/root/.claude` and holds whatever a `docker exec … claude` login wrote.
Steward never wrote that directory's contents and a re-provision does not restore them, so
removing it would make the documented way back silently require a re-login. Every report
says so rather than leaving it to be discovered.

Runs the same `steward.nursery.retire_resident` pipeline `steward retire` runs — one
implementation, verified by a test that injects the pipeline into the route.

**`200`, not `202`.** The container is down and the credential is gone by the time this
answers.

**`dry_run: true`** returns the plan, its `revision`, what would be marked and the exact argv
a real run would issue — and marks nothing, commits nothing and reaches no host.

**This one commits through the nursery**, unlike `POST /residents`, which asks the pipeline
not to and commits afterwards through `steward.authoring`. The reason is the order above:
retirement's commit belongs *between* the mark and the stop, and the only code inside that
sequence is the pipeline. What comes with the nursery's commit is the nursery's
target-manifest dirty refusal, named rather than hidden. Unrelated checkout work is tolerated
because the commit names exactly one path; uncommitted bytes in that resident's manifest are
not. Revision, dirt, mark and commit are serialized under the checkout's authoring lock,
which is released before Chronicle or host I/O. The commit is authored by the operator whose
credential made the request, exactly as every other write here is.

```json
{"request_id": "…", "message": "…",
 "resident": "hob", "manifest_path": "residents/hob/manifest.yaml",
 "marked": true, "stopped": true, "scrubbed": true,
 "commands": ["ssh Miha@dxp2800 docker compose … down --remove-orphans",
              "ssh Miha@dxp2800 rm -f ~/docker/warren/residents/hob/.env …"],
 "commit": "9f2c…", "dry_run": false, "note": "retired",
 "revision": "sha256:…",
 "push": {"pushed": true, "remote": "origin", "branch": "burrow/residents",
          "note": "pushed to origin burrow/residents"}}
```

`push` is what became of the commit afterwards when `STEWARD_PUSH_BRANCH` is set (see
[the write API](#writing-declarations-and-skills), warren#351) — and `null` when nothing was committed or
nothing is configured. `marked` is `false` when the manifest already said retired, `stopped` is `false` when there
was nothing on the host to stop, and `scrubbed` is `false` when there was no `.env` to
remove — "the token is gone" and "this run took it away" are different sentences, and only
the second is what `scrubbed` reports. A **local-placed** resident that ships inside the
burrow's own image has no container of its own, so a retirement of one marks and commits and
`note` says there was nothing at that path to stop; it stops running on the burrow's next
deploy.

The way **back** is `retired: false`, committed, and then
[`POST /residents/{id}/provision`](#post-residentsidprovision) — two steps, because
un-retiring is a person's decision written into a file rather than a button that undoes one.

The refusals:

| status | `error` | when |
|---|---|---|
| `404` | `unknown_resident` | no such resident, by id or by uid |
| `409` | `resident_invalid` | the id names a directory whose manifest does not validate; `steward validate` says which field |
| `409` | `resident_retired` | it is already retired, so there is nothing here to end. The message names the way back. Reconciling a *half-finished* retirement — marked, container still up — is `steward retire <id>` at a terminal, which is deliberately not what this route does |
| `409` | `declaration_invalid` | the declaration on disk stopped validating between this route reading it and the pipeline loading it. Nothing was marked |
| `409` | `retirement_rehearsal_required` | execution omitted the revision returned by a successful rehearsal |
| `409` | `stale_retirement_plan` | the declaration changed after rehearsal; rehearse the current bytes |
| `409` | `worktree_refused` | this resident's manifest has uncommitted changes, or the residents tree is not in a checkout; unrelated dirty paths are tolerated. Nothing was marked or stopped |
| `409` | `commit_failed` | git refused the stage or the commit. **The mark is on disk and in no commit**, so the resident has already stopped taking work — every path reads `retired` off the file, not out of git — with no history of the decision. The host was not reached. Commit that file to finish, or set `retired: false` to undo it |
| `409` | `retire_failed` | the host answered and refused — a `docker compose down` that failed, credentials that could not be removed. **The mark is committed by then**, and the message says so: re-run `steward retire <id>` once the host answers |
| `409` | `retire_refused` | there was nobody to ask |

Everything down to and including `worktree_refused` changed nothing at all; `commit_failed`
and the two below it stopped part-way. The request log says which: the first three are
settled before the request is even logged, a refusal that left the tree exactly as it found
it is recorded as `refused: <reason>`, and one that stopped after the mark or after the
commit is `stopped part-way: <reason>` — because a row reading "refused" over a request that
left a commit in git is the one row an audit trail cannot recover from.

### Secrets and the nursery

`CHRONICLE_URL` and `CHRONICLE_TOKEN` are read from **steward's own environment** when a
resident is provisioned and templated into a `.env` beside the compose file on the host.
They are never in the compose file (which carries `${CHRONICLE_TOKEN-}`, a reference), never
in a manifest, never in a soul, and never in git — the repo's own credential scanners are
run over everything the nursery writes into the checkout, as a test.

`POST /residents` with `deploy: true` and `POST /residents/{id}/provision` are both refused
with no `CHRONICLE_URL` in steward's environment: a container with nowhere to emit is a
resident that would never appear in the village, and finding that out three days later from
an empty house is worse than finding it out now.

### `GET /residents` · `GET /residents/{id}`

Read-only JSON views of validated manifests, including the durable `uid` plus
`runner.kind` and `runner.model`, so both "which record is Hob?" and "which brain is Hob
on?" are answerable over HTTP. Manifests that did not validate are named in `errors`
rather than quietly omitted. There is nothing to redact: a manifest holding a
credential-shaped key or an inline secret would have failed validation and never become
a resident at all.

A **retired** resident is listed, with `"retired": true`. Hiding it would leave a fleet
view unable to answer what used to run here, and retirement is a lifecycle state rather
than a deletion — see [`retired`](manifest.md#retired--the-lifecycle-state) for the full
list of what it stops.

`skills` is what the manifest grants; `effective_skills` is what a session actually
gets — the library's defaults plus those grants, in injection order:

```json
{"skills": [{"id": "read-inbox", "source": "library", "note": "…"}],
 "effective_skills": ["escalate", "write-journal", "read-inbox"]}
```

`tools` and `workspace` are what a session may reach, and where it may act. `tools` is
either a list of tool names or the string `"unrestricted"` — never absent, and never null,
for the reason a budget reports a limit of `null` rather than omitting the gauge: unlimited
is something a reader is told rather than something they infer from a missing key, so
"which residents can reach anything" is one pass over this endpoint. `workspace` is a list
of absolute directories opened to the session beyond the working directory it is otherwise
confined to, and is usually empty.

```json
{"tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
 "workspace": ["/data/library/books"]}
```

```json
{"tools": "unrestricted", "workspace": []}
```

See [`tools`](manifest.md#tools--what-a-session-may-reach) and
[`workspace`](manifest.md#workspace--where-a-session-may-act) for what each one actually
enforces, and for why a bounded list and a permission mode are different axes.

`GET /residents` additionally carries a small `budget` block on every resident, so a
fleet view can draw fuel gauges without a round trip each:

```json
{"budget": {"declared": true, "paused": false, "summary": "daily_cost_usd: 1.25 of 5",
            "spent_usd": 1.25, "tokens": 20400, "runs": 6,
            "budgets": [{"budget": "daily_cost_usd", "spent": 1.25, "limit": 5.0,
                         "remaining": 3.75, "exhausted": false}],
            "window": {"tz": "Europe/Ljubljana", "day": "2026-08-24", "start": "…", "end": "…"}}}
```

A resident with no caps reports `"declared": false` and `"summary": "no limit"` rather
than omitting the block — a panel that simply left the gauge out would let *unlimited*
read as *unknown*.

`voice` is the soul's own `## Voice` section, exactly the text steward injects into a
session for this resident. `null` means the soul declares no voice, which is a real
answer: sessions for it get none. `board` and `delegation` are the two blocks that say
what work a resident takes on beyond its own routines — whether it claims from the board,
and whether it may hand work to a neighbour and to whom.

`notifications` is the resident's *declaration* of where steward's outbound taps go —
`{transport, on, status, note}`, exactly as the manifest carries it. The derived ntfy topic
is deliberately **not** in this payload, or in any other a browser can reach: on ntfy the
topic is the capability, to read and to write. `steward notify list`, at a terminal, is the
one place it is printed. See
[docs/manifest.md](manifest.md#notifications--where-this-residents-outbound-taps-go).

### `GET /residents/{id}/budget`

Spent against limit for each budget, the window those numbers are counted in, and the
pause state. This is the read chronicle's fleet-ops view (chronicle #40) draws from; steward
invents no village state to serve it.

```json
{
  "resident": "hob",
  "agent_id": "claude-code:hob",
  "window": {"tz": "Europe/Ljubljana", "day": "2026-08-24",
             "start": "2026-08-23T22:00:00.000Z", "end": "2026-08-24T22:00:00.000Z"},
  "spent": {"runs": 6, "input_tokens": 18000, "output_tokens": 2400, "tokens": 20400,
            "cost_usd": 5.2, "duration_s": 812.4, "unreported_runs": 0},
  "budgets": [
    {"budget": "daily_cost_usd", "spent": 5.2, "limit": 5.0, "remaining": 0.0, "exhausted": true},
    {"budget": "daily_tokens", "spent": 20400, "limit": null, "remaining": null, "exhausted": false}
  ],
  "max_run_seconds": 900,
  "paused": true,
  "pause": {"resident": "hob", "budget": "daily_cost_usd", "spent": 5.2, "cap": 5.0,
            "reason": "daily_cost_usd: 5.20 of 5", "request_id": "…",
            "window_end": "2026-08-24T22:00:00.000Z", "paused_at": "…"},
  "allowance": null,
  "summary": "paused: budget exceeded"
}
```

Everything here is a sum over rows steward wrote when runs finished, inside a window
computed from the calendar **at the moment of the request**. A steward that restarted an
hour ago answers exactly what one that has been up all day answers — a daily cap that
resets because the daemon bounced is not a cap. `"limit": null` means no cap is declared,
and `unreported_runs` counts runs without usable cost accounting. Priced Codex runs
carry an API-equivalent estimate, not a billed-dollar receipt: `spent.estimated_cost_runs`
counts them, and each ledger entry carries `cost_estimate` with the exact rates and token
counts used. Origin rollups also expose `estimated_cost_runs`. A capped Codex resident
pauses when accounting is unknown; see [Codex cost accounting](manifest.md#codex-cost-accounting).

### `GET /org`

The org chart, computed from the manifests and nothing else (warren#441). Who may hand
work to whom is already written down twice over — `delegation.send`/`delegation.to` on the
sending manifest, an active route of kind `delegation` on the receiving one — so this
route derives the chart rather than storing one. Nothing here reads the ledger, the host
or the clock: two calls over an unchanged tree answer the same bytes.

```json
{
  "nodes": [
    {"id": "hob", "uid": "…", "name": "Hob", "role": "vault keeper", "accent": "#a68a4f",
     "summary": "Keeps the vault.", "retired": false, "rank": 0,
     "session_grants": ["skills.write"],
     "app_grants": [{"id": "chronicle", "status": "granted"}],
     "mounts": [{"host": "~/Life", "container": "/vault", "mode": "rw"}],
     "budget": {"declared": true, "daily_cost_usd": 10.0, "daily_tokens": null,
                "max_run_seconds": null},
     "delegates": true, "accepts": ["inbox"]}
  ],
  "edges": [
    {"sender": "hob", "receiver": "pip", "named": true, "deliverable": true, "reason": null}
  ],
  "errors": []
}
```

`rank` is the layout fact: 0 for a resident nobody may hand work to, and one more than
whoever may hand work to it otherwise. It is computed here rather than in each surface so
the terminal's indentation and the panel's rows cannot disagree about who is above whom.
Two residents that may delegate to each other are a cycle with no top, and everyone in one
keeps rank 0.

**An edge is drawn even when it will not deliver.** `delegation.to: [pip]` aimed at a
resident whose door is shut is a declared intention that does not work, and a chart that
dropped it would answer "there is no such grant" about a grant that is in the file. Such an
edge carries `"deliverable": false` and the reason. `named` separates the two grants a
sender can hold: `true` when `delegation.to` picked this receiver, `false` when the
allowlist is empty and the receiver is reachable because it opened a door.

`budget` is the **declared** cap, not today's spend — an org chart answers what a resident
is allowed to do, and `GET /residents/{id}/budget` is where the ledger lives. `declared:
false` with every cap `null` is said out loud, because unlimited must not read as unknown.

`errors` carries what validation refused, the way `GET /residents` does: a manifest steward
could not read is not a node, and a fleet that has gone quiet says why rather than
answering an empty chart.

`steward org` prints the same projection for a terminal (`--format json` emits this exact
document), and Townhall's Org page draws it.

### `GET /residents/{id}/journal`

The resident's own journal, **newest first** — the entries it wrote at the close of its
own days, never anything steward synthesised. `?limit=` bounds how many (default 14,
clamped to 100). A control panel reads this to show a resident's recent history.

```console
$ curl -sS -H "Authorization: Bearer $STEWARD_TOKEN" \
    http://127.0.0.1:8801/residents/hob/journal?limit=3
{"resident": "hob", "entries": [{"date": "2026-08-24", "routine": "close-of-day",
                                        "text": "Two drafts still waiting."}]}
```

`404` for an unknown resident, and `409 journal_unreadable` when the manifest's `memory`
cannot hold a journal (a `file` memory has nowhere to keep one entry per day). An empty
journal is an empty list, not an error — a resident that has never written one has still
answered the question.

An exhausted budget **pauses** the resident. While it is paused:

- scheduled fires and board claims are skipped with a logged reason;
- `POST /residents/{id}/routines/{routine}/run` returns
  `409 {"error": "budget_exceeded", "message": "paused: budget exceeded (…)"}` and writes
  nothing — it is refused before it is accepted, like every other refusal here;
- exactly **one** `needs_human` was emitted, naming the budget and the number that tripped
  it, and no more are emitted however many fires are refused.

Unpausing goes through the ordinary approvals machinery: `POST /approvals/{request_id}`
with `{"decision": "approve"}` resumes the resident and reports `"resumed": "<id>"`
alongside the usual `needs_human_resolved`. `deny` is a real answer too — it leaves the
resident paused. `steward budget unpause <id>` does the same thing from a terminal,
resolving the same request and emitting the same event. Either way the resident gets an
**allowance until the end of the window that tripped**: carrying on means today, not
forever, and tomorrow's cap applies to tomorrow.

### `GET /skills`

The skills library, and who holds each skill.

```json
{
  "library": "/srv/steward/skills",
  "skills": [
    {"name": "research", "description": "Answer a question from real sources…",
     "default": true, "path": "skills/research/SKILL.md", "body_chars": 2391,
     "holders": ["hob", "sender-resident"]}
  ],
  "errors": []
}
```

`default: true` means every resident holds it without a grant, so `holders` is every
valid resident. A skill nobody holds reports `"holders": []` — that is a real answer,
not an omission. A `SKILL.md` that does not parse is named in `errors` and left out of
`skills`, the same way a broken manifest is handled above.

The library is read from disk on every request, so a skill written through the endpoints
below is in the next listing rather than the next restart.

Skills *are* writable over HTTP since steward #214 — see
[Writing declarations and skills](#writing-declarations-and-skills). Granting one to a
particular resident is still a manifest edit, which is `PUT /residents/{id}/declaration`.

### `GET /routines`

Every routine of every valid resident, fleet-wide: the standing-work ledger. Assembled
from three things steward already knows and nothing it does not.

```json
{
  "routines": [
    {"key": "hob/inbox-read", "resident": "hob", "resident_name": "Hob",
     "accent": "#a68a4f", "routine": "inbox-read", "schedule": "15 * * * *",
     "schedule_tz": "Europe/Ljubljana", "enabled": true, "retired": false,
     "requires": ["read-inbox"], "timeout_s": 600, "journal": null,
     "anchor": "2026-08-24T21:15:00+00:00", "next_fire": "2026-08-25T02:15:00+02:00",
     "last_request": {"request_id": "…", "outcome": "ran", "detail": {"routine": "…"}},
     "last_run": {"run_id": "…", "trigger": "schedule", "outcome": "ok",
                  "recorded_at": "2026-08-25T02:15:41.220Z", "duration_s": 41.2}}
  ],
  "state_path": "/srv/steward/.steward/state/scheduler.json",
  "scheduler": {"last_tick": "2026-08-25T09:31:02+00:00", "stale_after_s": 360.0,
                "alive": true},
  "errors": []
}
```

`next_fire` is computed from the cron expression in the routine's own zone, and is `null`
for a disabled routine — a routine that is off has no next occurrence to promise.

`retired` is the resident's, not the routine's. A retired resident's routines are **listed
and never fire**: `retired: true`, `next_fire: null`, and run-now on one is `409
resident_retired`. They are listed rather than hidden because "what used to run here" is a
question a ledger should be able to answer; they carry no next fire because
`load_scheduled` leaves retired residents out, so there is no occurrence to promise.

`anchor` is the scheduler's state file, re-read on every request because the daemon is a
different process. It is called an anchor rather than a last run because that is what it
is: the moment the next occurrence is computed from, which is the last fire *or* the
moment steward first saw the routine. `null` means it has never fired, and calling that a
last run would let a routine that has never worked look like one that has.

`last_request` is the newest entry in the request log for this routine — what became of
the last run **somebody asked for through this API**. A scheduled fire is not an HTTP
request, so it never appears here, and `null` is the ordinary case.

`last_run` is the newest **run ledger** row for this routine, and it is the one to read for
"did this actually fire" (warren#104). Every finished session writes a ledger row whatever
started it, so this carries `trigger` — `schedule` or `manual` — and the `outcome` the run
came to. `null` means no run of this routine has ever finished, which is not the same as
one that failed.

Both are here because they answer different questions and their disagreement is a
diagnosis. A panel that showed only `last_request` reported a healthy resident firing on
its schedule as one that had never run, and the operator's honest conclusion from it —
"it only runs when I trigger it manually" — was false. A `last_request` of `queued` with an
older `last_run` is the opposite case: something was asked for and has not happened.

`scheduler` is the heartbeat the rest of this ledger has to be read against: `last_tick` is
when a scheduler process was last alive against that state file — stamped every tick,
**including the ticks where nothing was due**, so an idle daemon still counts as a live one,
and stamped every 60s by the scheduler's own heartbeat thread **while it is inside a run**,
so the fifteen minutes a daily summary takes do not read as fifteen minutes of nobody home.
`alive` says whether that is within `stale_after_s` of now. The threshold is not a new
number: it is the heartbeat's cadence (60s) plus the catch-up window a fire may be late by
(300s), so anything older could not have fired on time anyway.

`alive` has three values, and the third is the point. `true` is up, `false` is a daemon
that stopped, and `null` means **nothing has ever ticked** — a fresh install, where every
`next_fire` below is a promise with nobody to keep it. `null` is also `last_tick`, and a
state file written before this field existed reads as never ticked, which is the safe way
round.

A routine's declaration and the fact that something is firing them are separate questions;
this endpoint now answers both. Manifests that did not validate are named in `errors`,
exactly as in `GET /residents`.

### `GET /requests` · `GET /requests/{request_id}`

Accepted requests, and what became of them. This is the endpoint that makes *accepted*
survivable as an answer: everything above returns a `request_id` and refuses to claim an
effect, and this is where the effect eventually shows up.

```console
$ curl -sS -H "Authorization: Bearer $STEWARD_TOKEN" \
    http://127.0.0.1:8801/requests/2b8f…
{"request_id": "2b8f…", "received_at": "2026-08-24T23:45:33.975Z", "method": "POST",
 "path": "/residents/hob/routines/inbox-read/run", "outcome": "ran",
 "detail": {"routine": "hob/inbox-read", "run_id": "…"}}
```

`outcome` is the whole point. A run-now is written as `queued` and becomes `ran`,
`failed`, `skipped: <reason>`, or `refused: already running` when the fire it stands for
finishes. A posted job is `posted`, a decision `recorded`, a declaration `declared`, a
handoff `delegated`. A client polls one of these rather than deciding on its own that a
202 went well.

`GET /requests` is the log, **newest first**, with `?limit=` (default 50, clamped to
1–500). `?resident=hob` filters by the exact resident segment of `/residents/{id}/…`
(or `detail.resident` for collection writes such as resident creation), before applying
the limit. Timestamp ties use reverse insertion order. Both filters and limits run in
SQLite; `/routines` separately looks up only the newest request for each declared routine.
The complete audit trail remains available to deliberate Store callers through
`export_request_history()` (oldest first).

`404 unknown_request` for an id nobody logged — and only *accepted* mutating
requests are logged, so a refused one has no id to look up. That is the same promise as
everywhere else here: nothing is written for a request that was refused.

## Storage

`.steward/state/steward.db` (beside the scheduler's `scheduler.json`; both follow
`$STEWARD_STATE`, and `--db` overrides the database).

| table | holds |
|---|---|
| `jobs` | the board *and* the inboxes: task, status, claimant, lease, artifacts — plus `assignee`, `delegated_by`, `route`, `parent_task_id`, `origin`, `depth` when delegated |
| `approvals` | the request, its full detail, the decision, and whether it was delivered |
| `requests` | every accepted mutating request and what became of it |
| `run_ledger` | one row per finished session: work kind, routine trigger (`schedule`/`manual`, empty for legacy or non-routine rows), tokens, money, seconds, and whether the brain reported any of it |
| `open_runs` | one row per session awaiting an answer, including work kind and routine trigger; the watchdog reconciles these rows |
| `budget_pauses` | the residents steward has stopped, and the number that stopped them |
| `budget_allowances` | a human's "carry on", and the moment it runs out |
| `watchdog_attempts` | the restart budget of each resident, so three attempts means three |
| `watchdog_passes` | when the watchdog last swept, which is how `doctor` can say nothing is watching |
| `operator_credentials` | one row per named human operator: their git author identity, the SHA-256 digest of their credential, and when it was minted and revoked |

Budget accounting has one deliberately separate durable file: `steward.db.health.jsonl`.
It records a bounded, redacted error when a completed run cannot be ledgered or its
post-run pause cannot be enforced. It is independent of SQLite so a persistent database
lock cannot hide its own failure; `steward doctor` reports any entry as unhealthy. SQLite
writes wait up to 15 seconds for a competing writer before this evidence is recorded.
| `unbracketed_runs` | the runs steward buried on their session's behalf, so nobody is mourned twice |

SQLite rather than a JSON file because the two interesting writes are both
conditional — claiming an open task and deciding a pending approval — and "the first
writer wins, everyone else reads back what was recorded" is exactly what a
read-modify-write over a JSON file cannot promise.

Schema changes are `ALTER TABLE`, applied at open time. A steward that has been running
since the API landed already has a `steward.db` full of real jobs, and a migration that
drops it is a migration that loses work.

## Writing declarations and skills

Added in steward #214. Every UI before it was read-only by construction: manifests and
skills are files in the residents tree that only a commit could change. These endpoints
move the *typing* into a control panel while keeping every guarantee that rule bought.

Four rules hold for all of them.

Declaration updates are human-only, with one exception: a session holding
`residents.grant_skill` may add one approved skill line, described under [An approved
declaration edit](#an-approved-declaration-edit). A new skeleton may also be written with
the narrow `residents.declare` session grant, and skill writes accept the similarly narrow
`skills.write` grant — both described under [Three kinds of
caller](#three-kinds-of-caller).

**An invalid write is never written.** The candidate is applied to a throwaway *copy* of
the tree and validated there with the same gate `steward validate` runs, and only a copy
that passes is applied for real. It is the whole tree rather than the one file, because a
duplicate `uid` and two residents sharing a journal directory are only visible across
residents — an API that checked one file would be weaker than CI, and "it passed the API
and broke the build" is the failure these endpoints exist to prevent. A refusal has
written nothing and committed nothing.

**Refusals carry structured diagnostics**, so a form can highlight the field rather than
print a paragraph. Every validation cap applies and is surfaced, never swallowed:

```json
{"detail": {"error": "manifest_invalid",
            "message": "the declaration for 'hob' does not validate: charter.mission: …",
            "diagnostics": [{"file": "residents/hob/manifest.yaml", "field": "charter.mission",
                             "problem": "…exceeds the 2000 character limit",
                             "example": "mission: Keep the village's notes in order.",
                             "severity": "error"}]}}
```

**Every accepted write is committed by steward**, staging *only* the files it wrote —
never `git add -A`, so a checkout with somebody's half-finished afternoon in it is neither
swept into the commit nor a reason to refuse. The commit names the request:

```
chore(residents): update hob via the API

Written over the steward API by a holder of STEWARD_TOKEN, over the steward API.
Steward-Request-Id: 4d9e01be-c649-44bf-82f0-75ad5cac45d2
```

The author is `steward (api) <steward-api@localhost>` unless `STEWARD_COMMIT_IDENTITY` is
set to a `Name <email>`. Deliberately not a person by default: `STEWARD_TOKEN` is a shared
secret with no principal behind it, and an author line naming somebody would be a guess
dressed up as an audit record. The request id in the trailer is the honest link —
`GET /requests/{id}` turns it into a method, a path and a moment. Both author *and*
committer are set, so the commit works on a server with no ambient `git config` identity.

`committed: false` with `sha: null` is the converged answer, not a failure: what is on
disk was already what is in git.

**The commit is pushed afterwards** when `STEWARD_PUSH_BRANCH` names a branch — on a
burrow, `burrow/residents`, so the history the checkout is authoritative for exists
somewhere that is not one disk on a NAS (warren#351). `commit.pushed` says what came of
it: `true`; `false`, with git's reason appended to `commit.note` (`"…; NOT pushed to
origin burrow/residents (…)"`), which never fails the write — the save was durable before
the push started, and the next write that commits, or the next deploy, carries every
commit the branch is missing; or `null`, meaning there was nothing to push — no commit was made, or no branch
is configured. The push is of `HEAD` to the branch by its full ref, bounded, and never
forced: history somebody else put on the branch is refused, not overwritten.
`POST /residents/{id}/retire` commits through the nursery and reports its push under a
top-level `push` key (`{pushed, remote, branch, note}`, or `null`) for the same reason.

**If the residents tree is not inside a git checkout**, the write is **refused** with
`409 not_a_git_checkout`. A fleet whose declarations have no history and no way back is a
thing to choose out loud rather than to discover on the day somebody needs to undo
something. `STEWARD_ALLOW_UNCOMMITTED_WRITES=1` accepts it, and then every response says
so in `commit.note` — but never on a burrow whose tree lives in the image, where an
accepted write lands in the container layer and dies on the next deploy; the deployed
control plane mounts a real checkout instead (`deploy/README.md`, "The residents
checkout").

### `GET /residents/{id}/declaration` · `PUT /residents/{id}/declaration`

The editable source of one resident — both files, together. Not the same thing as
[`GET /residents/{id}`](#get-residents--get-residentsid), which is a projection assembled
from a validated model; this is what is actually in git, comments and field order and all.

```json
{"id": "hob", "uid": "…",
 "manifest": {"version": 0, "id": "hob", "…": "…"},
 "text": "# the manifest as YAML, byte for byte\nversion: 0\n…",
 "soul": "---\nagent_id: claude-code:hob\n---\n…",
 "soul_file": "soul.md",
 "revision": "sha256:…",
 "paths": ["residents/hob/manifest.yaml", "residents/hob/soul.md"]}
```

`PUT` takes that shape back. Exactly one of `manifest` (the mapping a form builds, which
steward serialises — convenient, and it rewrites the file, so comments do not survive) or
`text` (the YAML itself, written byte for byte, which is how comments are kept). Giving
both, or neither, is a `422`. `soul` is optional; omitting it leaves the soul untouched.

The manifest and the soul move **together** because identity and display frontmatter must
agree. `uid` and `agent_id` are immutable through this endpoint: changing either returns
`409 resident_identity_changed`; use an explicit operator migration or replace the Resident.

It is a **full replacement, not a patch**. Merging a partial edit means steward deciding
what a missing key meant — cleared, or untouched? — and the declaration is the wrong file
to be clever with.

`revision` is optional optimistic concurrency: send the one the `GET` returned and a
second editor who loaded the same version gets `409 stale_revision` instead of silently
overwriting the first. Omit it to overwrite deliberately, which is what a script wants.

| status | error | meaning |
|---|---|---|
| 404 | `unknown_resident` | `PUT` updates; `POST /residents` is how one is declared |
| 409 | `stale_revision` | somebody changed it first; re-read and reapply |
| 409 | `soul_file_changed` | renaming `soul.file` would orphan the old file; do it in the checkout |
| 409 | `resident_identity_changed` | ordinary edits cannot replace `uid` or `agent_id` |
| 409 | `not_a_git_checkout` | the tree has no git behind it and this steward refuses to write unrecorded |
| 422 | `manifest_invalid` | the tree would not validate; `diagnostics` names the fields |
| 422 | `approval_not_needed` | a human caller sent `approval_request_id`; they are the approval |
| 403 | `edit_not_approved` | a session's edit is not the one a human said yes to |
| 403 | `session_credential_forbidden` | a session with no `residents.grant_skill`, or holding it and naming no request |

#### An approved declaration edit

Added in warren#437, and the only way a session ever writes here. Everything above is
about a human caller; this is the narrow door beside it, and it is shut until a person
opens it.

A session holding `residents.grant_skill` sends the ordinary `PUT` body plus one field:

```json
{"text": "# the whole manifest, with one skill line added
…",
 "approval_request_id": "5f0e…"}
```

Steward writes only when **all** of these hold. Any one of them failing is `403
edit_not_approved` naming which, and nothing is written, committed, or spent:

- the request id names an approval **this resident raised** — not steward's own knock, and
  not a colleague's;
- its action is one this door recognises, which today means `grant_skill`;
- it was answered `approve`. Pending, `deny`, expiry's deny-by-default and an `edit`
  decision all refuse; the knock offers `approve,deny` for that reason;
- it has not passed its `expires_at`. The deadline bounds the whole act, not only how long
  there was to answer: a yes from last week arriving at the write door is a question worth
  asking again;
- it has not already been spent. **One approval is one edit** — the decision is claimed
  before the tree is touched, so two sessions presenting the same id cannot both write;
- the candidate declaration **is the edit the approval described**.

**What "matches" means.** The approval's detail names a resident and a skill
(`{"resident": "shelf-worker", "skill": "series-detection"}`). Steward reads the
declaration currently on disk, parses both it and the candidate as YAML, and requires that:

- the resident in the detail is the resident in the path;
- every top-level key except `skills` is **equal** — a key changed, added or removed
  anywhere else is refused naming it (`… and this declaration also changes charter`);
- `skills` is the current list with **exactly one entry inserted**, and that entry grants
  the skill the approval named. Removing an index has to reproduce the current list
  exactly, so reordering the list, rewriting another grant's note, or dropping a grant
  alongside the one added are all refused;
- no `soul` is sent. The approved edit is one line of one manifest; a soul document riding
  along is a second change nobody answered.

Matching is on what the manifest **says**, not on its bytes: field order and comments may
differ, values may not. The write then lands with the revision of the declaration the match
was made against, so it cannot apply to bytes nobody compared.

The commit is authored `<resident> (session) <<resident>-session@localhost>` like every
other session write, and the response carries what the write was made against:

```json
{"request_id": "…", "status": "accepted", "revision": "sha256:…",
 "commit": {"committed": true, "sha": "a1b2c3d", "…": "…"},
 "approval": {"request_id": "5f0e…",
              "act": "grant skill 'series-detection' to 'shelf-worker'"}}
```

`approval` is `null` for a human caller. A human sending `approval_request_id` is `422
approval_not_needed` rather than obeyed: they *are* the approval, and steward will not put
a spent mark against a decision nobody used.

**A refusal gives the decision back.** The claim is taken before the write and released if
the write refuses — a manifest the fleet will not validate must not cost a human's yes.
What it does not survive is the process dying mid-write, which fails closed: the decision
reads as spent, nothing was written, and the honest next move is to ask again.

### `GET /skills/{name}` · `POST /skills` · `PUT /skills/{name}`

One skill's frontmatter and body, and the two ways to write one.

```json
{"name": "triage", "description": "Sort the inbox before anything else.",
 "body": "Read every message. …", "defaults": false,
 "revision": "sha256:…", "path": "skills/triage/SKILL.md"}
```

`POST /skills` adds one and `201`s; a name that already exists is `409 skill_exists`
rather than an overwrite, because "add" and "rewrite" must not be the same button.
`PUT /skills/{name}` replaces one and `404`s for a name nobody wrote. Both take
`description`, `body`, `defaults`, and an optional `revision`.

A session credential reaches these two write routes only when its resident manifest grants
`skills.write`. For that caller, `defaults: true` is refused and `PUT` is refused when any
manifest already grants the named skill. The master token and operator credentials retain
the full form. A session-authored commit uses `<resident> (session)
<<resident>-session@localhost>` and keeps the same `Steward-Request-Id` trailer.

**`defaults: true` is a grant to the entire fleet.** A default skill is held by every
resident without any manifest saying so, which makes this one flag the largest blast
radius in the API. It is validated accordingly: the whole residents tree is re-validated
against a library holding the candidate, so a skill that would break somebody's grant is
refused here rather than discovered at the next wake-up.

The write goes into the **library** — the git-tracked `skills/` tree — and never into a
session's materialized `.claude/skills/`, which `steward.skills.materialize` owns and
prunes wholesale on every session launch. A skill written there would be deleted silently
at the next wake-up.

If the fleet has no library yet, the first `POST /skills` creates `skills/` beside the
residents tree. That is a bigger change than it looks — a configured-but-empty library
turns every existing grant into an error, where an absent one leaves grants unchecked — so
it goes through the same gate as everything else, and a first skill that would invalidate
the fleet is refused before the directory exists.

### `GET /secrets` · `PUT /secrets/{name}`

The credential write path (warren#462). Adding a bot to Discord used to end in an ssh
heredoc appending a token to the burrow's `.env`, followed by
`docker compose up -d --force-recreate chat`. The paste is a step no agent session can
take — a classifier refuses it, correctly, because it is a credential going into a shell —
and the recreate is a blunt way to say "one more token exists". Both are replaced by this
endpoint and the chat daemon's own reload.

`PUT /secrets/{name}` stores one credential as one file, mode 600, in the burrow's
`secrets/` directory (`STEWARD_SECRETS_DIR`, `/secrets` in every container). The name is an
environment variable name — `STEWARD_CHAT_TOKEN_DISCORD_HOB` — because that is the slot the
value fills: resolution is **file first, environment second**, so a token already in the
`.env` keeps working and one written here simply wins for that name.

```json
PUT /secrets/STEWARD_CHAT_TOKEN_DISCORD_HOB
{"value": "…"}

{"request_id": "…", "name": "STEWARD_CHAT_TOKEN_DISCORD_HOB", "set": true}
```

**The value never comes back.** There is no `GET /secrets/{name}`, no value in the listing,
no value in the `secret_written` event, and no value in the request log — which also means
the refusals below name the rule rather than quoting what they refused. Every consumer of
these values is a process on the burrow that reads the file directly.

**Human callers only.** A session credential is refused with `403
session_credential_forbidden` before anything is written: a resident that could set
`STEWARD_CHAT_TOKEN_DISCORD_PIP` could take Pip's identity, which is the boundary
`app_grants` exists to hold.

| status | error | meaning |
|---|---|---|
| 422 | `invalid_secret_name` | not an environment variable name (upper case, digits, `_`) |
| 422 | `invalid_secret_value` | blank, more than one line, or longer than 8,192 characters |
| 403 | `session_credential_forbidden` | a resident session tried to set a credential |

`GET /secrets` lists the slots and never their values: every slot a declared chat route
asks for, every file in the directory, and every `STEWARD_CHAT_TOKEN_*` still in the
environment — with `set`, where it came from (`file` or `env`), and which route claims it.

```json
{"directory": "/secrets",
 "secrets": [{"name": "STEWARD_CHAT_TOKEN_DISCORD_HOB", "set": true, "source": "file",
              "route": {"resident": "hob", "route": "discord", "address": "discord:hob"}}]}
```

There is no delete. Unsetting a credential is rare, is not what this endpoint is for, and
stays an ssh step — `rm` on the burrow, then restart what held it.

**Reaching the fleet.** The chat daemon re-reads the tree and the secrets directory on its
route-recheck timer (five minutes), so a token set here becomes `reachable, bot @Name` in
`steward chat list` without a container recreate. `POST /reload` still only reloads the API
process; nothing else changed about it.

### `POST /reload`

Re-reads the residents tree and the skills library into **this process**.

The scheduler daemon is a *different* process, usually started by `steward serve`, and no
HTTP call can reach into it. It does not need one: it watches both trees itself and
reloads on its next wake-up, which is within a minute. A tree that stops validating does
not stop it — the last declarations that did validate keep running, and the reason is
logged once.

What this endpoint is for is the API's own long-lived collaborators — the run-now
scheduler and the board dispatcher — which are assembled at startup and would otherwise
fire a routine against the manifest that was on disk when the server booted. The read
views need no reload at all; they re-read the tree on every request.

```json
{"request_id": "…", "status": "reloaded", "residents": 3, "routines": 7,
 "skills": ["escalate", "write-journal"], "errors": []}
```

`409 tree_invalid` when the tree does not validate: nothing is swapped in, and this
process goes on running the last declarations that did — the same judgement the daemon
makes, for the same reason. Swapping in only what parsed would quietly retire every
resident whose manifest happened to be mid-edit.

### Rotate the master token

Configure `STEWARD_TOKEN` with the new secret, `STEWARD_TOKEN_PREVIOUS` with the old
secret, and `STEWARD_TOKEN_PREVIOUS_UNTIL` with an explicit UTC deadline, for example
`2026-09-07T12:00:00Z`. The old token is accepted only before that instant, even without
a restart. All three settings are required for rotation; open mode cannot be combined
with rotation. Remove both previous-token settings when finished. Expired configuration
is allowed at startup but does not enable the expired token.

1. Inventory consumers without printing credentials: the API's environment, service
   configuration, scripts using `STEWARD_URL`, and CI secret names. Do not assume every
   daemon uses the master token. Resident sessions use their own credentials.
2. Generate/store the new secret securely. Configure the API with new/current plus
   old/previous and the deadline, then recreate **the API**. The shared compose `.env`
   is read at container creation, so editing it alone does not update a running process.
3. Verify both credentials against a read endpoint, then migrate identified clients/CI
   separately. Avoid secrets in shell history, command arguments, or diagnostic output.
4. Monitor the API's structured `master_token_slot` log field (`current`/`previous`;
   at most one message per slot per minute). Accepted write receipts carry the slot too.
   `steward doctor` reports active/expired previous-token configuration on the machine
   where it runs. Neither silence in the log nor a clean local doctor proves all clients
   have migrated; exercise each inventoried consumer.
5. Remove the two previous settings and recreate the API. Verify new-token success and
   old-token 401. If migration needs rollback before the deadline, restore the old token
   as current and remove the previous settings, then recreate the API and revert migrated
   clients. Never extend overlap indefinitely to hide an unknown consumer.

Operator credentials and session grants are unchanged. Both accepted master tokens receive
identical approval-body guards. Token values and token-derived identifiers are never included
in rotation audit output. This runbook is an operational procedure; passing implementation
checks does not mean a live rotation has been performed.

### Failed authentication

Each API worker permits five immediate failed authentications per peer address and refills
one allowance every 12 seconds. Exhausted callers receive `429 auth_throttled` and an integer
`Retry-After` in seconds. Valid master, operator and session credentials remain usable from
the same address; session authorization refusals are still 403 and do not consume allowance.
This limits failure responses, not TCP connections or the cost of checking valid credentials.

Buckets are process-local, synchronized, expire after five idle minutes, and retain at most
1,024 sources with least-recently-used eviction. Restarting a worker or evicting a source
resets that source's allowance. Multi-worker limits are per worker, not fleet-wide.
The policy is injectable through `ApiConfig.auth_failure_policy` for embedded callers.

The key is the ASGI peer address, never a header read by the auth gate. The checked-in NAS
nginx proxy forwards no client-IP header, so proxied callers share its peer bucket. Direct
callers behind the same NAT also share a bucket. To enable finer attribution, configure the
proxy to replace forwarding headers with its observed peer and configure Uvicorn to trust
only that proxy's address; never use wildcard trusted proxies on an externally reachable
API. This change does not enable forwarded-header trust or change network deployment.

The request log now also contains `method: AUTH`, `path: /auth`, `outcome: auth_failures`
summary rows. These are diagnostics, not accepted mutations: `detail.failed`,
`detail.throttled`, `detail.window_seconds`, and `detail.sources_sample` describe aggregated
failures. The sample holds at most 32 normalized addresses. No actual request path, query,
body, header or bearer value is persisted. The first failure is recorded immediately;
subsequent failures trigger at most one aggregate write per minute across the whole worker.
Graceful shutdown flushes the last aggregate; abrupt termination can lose that last window.
Existing request-history retention applies. A summary may contain failures from many sources.

### Deployment configuration

Before validating or declaring residents with omitted deploy host/user, set
`STEWARD_DEPLOY_HOST` and `STEWARD_DEPLOY_USER` for that installation. Manifest fields
win over these settings individually. Missing, blank or syntactically unsafe values
produce a validation refusal before provisioning. No host or SSH user is inferred from
the process's account or machine. Environment values are read when resolving settings,
not frozen when the Python module imports. Embedded callers can pass a
`DeploymentSettings` snapshot to `target_for` to keep installations independent.

The NAS `deploy/compose.yaml` explicitly supplies `dxp2800` / `Miha` through its shared
service environment (overridable via `.env`). The shipped Hob and Pip manifests already
name that same host/user. For laptop CLI use against declarations omitting placement,
export the intended installation settings first. The API bind address stays `127.0.0.1`;
these are resident deployment settings, not listener configuration.
