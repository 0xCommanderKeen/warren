# The steward HTTP API (v0)

The only write path into the fleet. Human actions in burrow's viewer — run a routine
now, post a job, answer an approval, create a resident — call this API directly from
the browser, so burrow's server never holds write access to agents and stays the pure
reader it claims to be.

```console
$ STEWARD_TOKEN=… steward serve                    # 127.0.0.1:8801
$ steward serve --host 100.x.y.z --port 8801       # the tailnet address
$ steward serve --allow-open                       # local dev, no token, said out loud
```

## The contract: acknowledgement, not effect

Every accepted request returns a `request_id` and one of three words — **accepted**,
**queued**, **recorded**. Never "done", never "ran". The API cannot confirm an effect
because it does not perform one: it hands work to the scheduler, the board, or the
disk, and the effect is confirmed only when the matching protocol event lands in
burrow's log.

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

Two kinds of caller present two kinds of credential. A **human** presents
`STEWARD_TOKEN` and may reach everything; a **session** presents the credential steward
minted for its own run and may reach very little. See
[Two kinds of caller](#two-kinds-of-caller) below.

**The human token** is one shared secret, exactly like burrow's ingest auth.

- `STEWARD_TOKEN` in steward's environment; `Authorization: Bearer <token>` on the
  request.
- Compared with `hmac.compare_digest`, so a wrong token cannot be discovered one byte
  at a time by timing requests.
- An empty or whitespace-only value **counts as unset**. Unset means the server
  refuses to start, naming the variable, unless `--allow-open` says out loud that this
  is local development.
- Anything else is `401`, and nothing is queued, stored, or emitted.
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

**The one exception is `/ui`.** The management console's three static files are served
unauthenticated, because the browser has to load the script before there is anything to
ask a human for a token with. They contain no fleet data: everything the console displays
it fetches from the endpoints below, with the token, and a `401` on any of them makes it
forget what it holds and ask again. See [`/ui`](#ui) at the end of this document.

**Tailnet only.** The default bind is `127.0.0.1`; in deployment steward listens on
its tailnet address and is never exposed to the public internet. One shared human token is
the whole of its auth against an operator — session credentials narrow what a *resident*
may do, not what an intruder may — and that is only enough behind a private network.

## Two kinds of caller

`STEWARD_TOKEN` is a master key, not an identity: one shared secret, one constant-time
compare, no principals. That is the right shape for a human operator and the wrong shape
for a session — with it, the resident that *raises* an approval can also *decide* it, and
a resident can sign a letter with any other resident's name. So a session gets its own
credential instead (steward #41).

| | human | session |
|---|---|---|
| credential | `STEWARD_TOKEN` | `$STEWARD_SESSION_TOKEN`, in the session's environment |
| minted | by the operator, once | by steward, per run, at fire time |
| identity | none — it is a shared secret | the resident whose run it is |
| expires | when the operator rotates it | with the run: on close, timeout, or a stale lease |
| may read | everything | everything |
| may write | everything | `POST /delegate`, and nothing else |

**What a session may reach.** Every `GET`, plus `POST /delegate`. Every other write path
is `403 session_credential_forbidden`, and nothing is recorded — it is refused at the door,
before a route runs. This is an allowlist, so a write path added later is refused until
somebody decides otherwise.

That allowlist is what makes the write API (steward #214) safe to have at all. A resident
that could `PUT` its own declaration would be choosing the rules it is held to, and one
that could write a skill would be handing itself instructions nobody approved — so
`PUT /residents/{id}/declaration`, `POST /skills`, `PUT /skills/{name}` and `POST /reload`
are all refused for a session, each naming the act rather than reciting a policy. Reading
a declaration stays open: a resident that could not see its own charter could not follow
it.

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

Three of those refusals name the act rather than the rule, because the act is the part
worth knowing:

- **`POST /approvals/{request_id}`** — deciding an approval is the human end of the
  escalation boundary. A session that could decide would be answering its own knock, and
  every guarantee downstream of "a human decided", expiry's deny-by-default included,
  would only be as strong as the session not noticing.
- **`POST /residents`** — declaring a resident is a human act.
- **`POST /residents/{id}/routines/{routine}/run`** — firing a routine is a human act; a
  session's own work arrives through the board and its inbox.

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

## CORS

`STEWARD_CORS_ORIGINS` is a comma-separated list of origins allowed to call the API
from a browser — burrow's viewer origin, typically. Unset means no origin is allowed,
so no CORS headers are sent to anyone.

```console
$ STEWARD_CORS_ORIGINS=http://village.local:8080 STEWARD_TOKEN=… steward serve
```

## Endpoints

### `POST /residents/{id}/routines/{routine}/run`

**Human callers only.** A session credential is refused here — see
[Two kinds of caller](#two-kinds-of-caller).

Fire one routine now. Validated against the resident's manifest before anything is
queued: an unknown resident or routine is a `404`, not an enqueue.

```console
$ curl -sS -X POST -H "Authorization: Bearer $STEWARD_TOKEN" \
    http://127.0.0.1:8801/residents/life-agent/routines/inbox-read/run
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
| 409 | `already_running` | the scheduler's overlap rule: skipped, never queued |
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

The lifecycle, all four transitions visible in burrow's log and reconstructible from
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
([docs/manifest.md](manifest.md#the-skills-library)), so `["research"]` is claimable by
any board-enabled resident.

### `POST /delegate` · `GET /residents/{id}/inbox` · `GET /tasks/{id}/lineage`

Hand work from one resident to another. The human path into delegation — a session usually
uses the `<delegate>` block or `steward delegate`, neither of which holds a token at all.
It is also the one write path a session credential reaches, and it behaves differently for
one: see [Two kinds of caller](#two-kinds-of-caller).

```json
{"from": "burrow-builder", "to": "life-agent", "route": "handoff",
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
[Two kinds of caller](#two-kinds-of-caller).

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

**Human callers only.** A session credential is refused here — see
[Two kinds of caller](#two-kinds-of-caller).

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
 "changed": true,
 "declare": {"written": true, "commit": null, "note": "declared and validated"},
 "provision": {"target": {"host": "dxp2800", "user": "Miha",
                          "path": "~/docker/steward-note-keeper",
                          "container": "steward-note-keeper", "image": "steward-resident:latest"},
               "files": ["docker-compose.yaml", ".env", "manifest.yaml", "soul.md"],
               "compose": "services:\n  note-keeper:\n…",
               "compose_changed": true, "env_keys": ["BURROW_TOKEN", "BURROW_URL"],
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

`409 resident_retired` when the id belongs to a retired resident: un-retiring is a
person's decision, written into the manifest and committed, not something an HTTP call
does on the way past.

### Secrets and the nursery

`BURROW_URL` and `BURROW_TOKEN` are read from **steward's own environment** when a
resident is provisioned and templated into a `.env` beside the compose file on the host.
They are never in the compose file (which carries `${BURROW_TOKEN-}`, a reference), never
in a manifest, never in a soul, and never in git — the repo's own credential scanners are
run over everything the nursery writes into the checkout, as a test.

`POST /residents` with `deploy: true` and no `BURROW_URL` in steward's environment is
refused: a container with nowhere to emit is a resident that would never appear in the
village, and finding that out three days later from an empty house is worse than finding
it out now.

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
 "effective_skills": ["daily-summary", "escalate", "research", "write-journal", "read-inbox"]}
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

### `GET /residents/{id}/budget`

Spent against limit for each budget, the window those numbers are counted in, and the
pause state. This is the read burrow's fleet-ops view (burrow #40) draws from; steward
invents no village state to serve it.

```json
{
  "resident": "life-agent",
  "agent_id": "claude-code:life-agent",
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
  "pause": {"resident": "life-agent", "budget": "daily_cost_usd", "spent": 5.2, "cap": 5.0,
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
and `unreported_runs` counts the runs whose brain reported no usage at all (a `codex` or
`command` session has none to give); steward writes those as zero and says how many they
were rather than inventing a number.

### `GET /residents/{id}/journal`

The resident's own journal, **newest first** — the entries it wrote at the close of its
own days, never anything steward synthesised. `?limit=` bounds how many (default 14,
clamped to 100). The console reads this to show a resident's recent history.

```console
$ curl -sS -H "Authorization: Bearer $STEWARD_TOKEN" \
    http://127.0.0.1:8801/residents/life-agent/journal?limit=3
{"resident": "life-agent", "entries": [{"date": "2026-08-24", "routine": "close-of-day",
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
     "holders": ["life-agent", "burrow-builder"]}
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
    {"key": "life-agent/inbox-read", "resident": "life-agent", "resident_name": "Hob",
     "accent": "#a68a4f", "routine": "inbox-read", "schedule": "15 * * * *",
     "schedule_tz": "Europe/Ljubljana", "enabled": true, "retired": false,
     "requires": ["read-inbox"], "timeout_s": 600, "journal": null,
     "anchor": "2026-08-24T21:15:00+00:00", "next_fire": "2026-08-25T02:15:00+02:00",
     "last_request": {"request_id": "…", "outcome": "ran", "detail": {"routine": "…"}}}
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
the last run **somebody asked for through this API**. A routine that fired on its own
schedule leaves its record in burrow's event log, not here. `null` is the ordinary case.

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
 "path": "/residents/life-agent/routines/inbox-read/run", "outcome": "ran",
 "detail": {"routine": "life-agent/inbox-read", "run_id": "…"}}
```

`outcome` is the whole point. A run-now is written as `queued` and becomes `ran`,
`failed`, `skipped: <reason>`, or `refused: already running` when the fire it stands for
finishes. A posted job is `posted`, a decision `recorded`, a declaration `declared`, a
handoff `delegated`. A client polls one of these rather than deciding on its own that a
202 went well.

`GET /requests` is the log, **newest first**, with `?limit=` (default 50, clamped to
1–500). `404 unknown_request` for an id nobody logged — and only *accepted* mutating
requests are logged, so a refused one has no id to look up. That is the same promise as
everywhere else here: nothing is written for a request that was refused.

### `/ui`

The management console: `index.html`, `app.css`, `app.js`, mounted as static files.

**Not behind the token**, and it has to be — the browser must load the script before there
is anything to ask a human for a token with. Three static files with no fleet data in
them; every byte the console displays it fetches from the endpoints above, with the token.

Steward serves whatever `STEWARD_UI` names, else `ui/` in the checkout. A directory with
no `index.html` in it is not mounted at all, because answering `/ui` with a 404 shaped
like a working console is worse than not offering one. An install that ships no console
serves the API and says nothing about a console; `steward serve` prints the URL only when
there is one to print.

The console is a pure client. It calls only the endpoints in this document, writes nothing
the API would not accept from anyone else, and has no path that edits a manifest — because
there is no endpoint that would let it. See the README for what it shows.

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

**Human callers only.** A session credential is `403` on every route in this section — see
[Two kinds of caller](#two-kinds-of-caller).

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

**If the residents tree is not inside a git checkout**, the write is **refused** with
`409 not_a_git_checkout`. A fleet whose declarations have no history and no way back is a
thing to choose out loud rather than to discover on the day somebody needs to undo
something. `STEWARD_ALLOW_UNCOMMITTED_WRITES=1` accepts it, and then every response says
so in `commit.note`.

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

The manifest and the soul move **together** because `agent_id` is in both and validation
insists they agree — split into two endpoints, renaming a resident's agent id would be
impossible, since whichever file you wrote first would be refused for disagreeing with the
other.

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
| 409 | `not_a_git_checkout` | the tree has no git behind it and this steward refuses to write unrecorded |
| 422 | `manifest_invalid` | the tree would not validate; `diagnostics` names the fields |

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
 "skills": ["daily-summary", "escalate", "research", "write-journal"], "errors": []}
```

`409 tree_invalid` when the tree does not validate: nothing is swapped in, and this
process goes on running the last declarations that did — the same judgement the daemon
makes, for the same reason. Swapping in only what parsed would quietly retire every
resident whose manifest happened to be mid-edit.
