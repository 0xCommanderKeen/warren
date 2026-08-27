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

## Auth

One shared token, exactly like burrow's ingest auth.

- `STEWARD_TOKEN` in steward's environment; `Authorization: Bearer <token>` on the
  request.
- Compared with `hmac.compare_digest`, so a wrong token cannot be discovered one byte
  at a time by timing requests.
- An empty or whitespace-only value **counts as unset**. Unset means the server
  refuses to start, naming the variable, unless `--allow-open` says out loud that this
  is local development.
- Anything else is `401`, and nothing is queued, stored, or emitted.

Reads are gated too. Every endpoint here is a write path except the resident views and
the skills listing, and gating those as well is simpler than explaining which is which
— there is one door. The OpenAPI schema and `/docs` are not served at all, for the same reason.

**The one exception is `/ui`.** The management console's three static files are served
unauthenticated, because the browser has to load the script before there is anything to
ask a human for a token with. They contain no fleet data: everything the console displays
it fetches from the endpoints below, with the token, and a `401` on any of them makes it
forget what it holds and ask again. See [`/ui`](#ui) at the end of this document.

**Tailnet only.** The default bind is `127.0.0.1`; in deployment steward listens on
its tailnet address and is never exposed to the public internet. One shared token is
the whole of its auth, and that is only enough behind a private network.

## CORS

`STEWARD_CORS_ORIGINS` is a comma-separated list of origins allowed to call the API
from a browser — burrow's viewer origin, typically. Unset means no origin is allowed,
so no CORS headers are sent to anyone.

```console
$ STEWARD_CORS_ORIGINS=http://village.local:8080 STEWARD_TOKEN=… steward serve
```

## Endpoints

### `POST /residents/{id}/routines/{routine}/run`

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
with `routine_started.payload.trigger` set to `manual` instead of `schedule`, so the ledger
can tell prompted work from standing work. Because the hooks are the same, a run-now session
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

Hand work from one resident to another. The human path into delegation — a session uses
the `<delegate>` block or `steward delegate`, neither of which holds this token.

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

`GET /approvals` lists gated actions. `?status=pending` (the default), `resolved`, or
`all`; anything else is a `422` with `unknown_status`. The default is unchanged from
before the parameter existed, so a panel that never passed it sees exactly what it saw. A
request past its `expires_at` but not yet swept is **not** returned under `pending`
(steward #66): it denies by default, so listing it as still answerable would let a human
click *approve* on something the sweep is about to close. It reappears under `resolved`
once the deny is recorded.

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

Decisions are idempotent: the first one wins. `202` the first time; a replay (a
double-tapped notification, a retried request) is `200`, returns the recorded outcome,
changes nothing, and emits nothing. An unknown `request_id` is `404`. A request that has
already **expired** is a `409` with `approval_expired` — distinct from the replay of an
already-decided one, because it was never decided: deny-by-default has the last word and the
sweep records the deny (steward #66).

Requests are *created* by the session that reaches a gated action — through a
`<needs-human>` block in its output or `steward approval raise`, both documented in
[docs/approvals.md](approvals.md). The API only ever answers a request; it never invents
one. A request nobody answers before its `expires_at` resolves itself as `deny` with
`decided_by: "expiry"`, and the resolution event lands like any other.

### `POST /residents`

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

**This endpoint never commits.** Not with `deploy: false`, not with `deploy: true`. The
server is not guaranteed to own the checkout it is reading — it may be a tailnet process
on a machine where nobody is watching git — and a commit appearing there is a commit that
surprises somebody. The response says so in `message`, and `declare.commit` is `null`, so
a panel can tell the human what is still theirs to do. `steward new-resident` on a
terminal is the path that commits.

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

Read-only JSON views of validated manifests, including `runner.kind` and
`runner.model`, so "which brain is Hob on" is answerable over HTTP. Manifests that did
not validate are named in `errors` rather than quietly omitted. There is nothing to
redact: a manifest holding a credential-shaped key or an inline secret would have
failed validation and never become a resident at all.

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

Read-only, like the other views: a skill is added by committing a `SKILL.md` and granted
by committing a manifest. There is no HTTP path that writes either.

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
| `run_ledger` | one row per finished session: tokens, money, seconds, and whether the brain reported any of it |
| `budget_pauses` | the residents steward has stopped, and the number that stopped them |
| `budget_allowances` | a human's "carry on", and the moment it runs out |
| `watchdog_attempts` | the restart budget of each resident, so three attempts means three |
| `watchdog_passes` | when the watchdog last swept, which is how `doctor` can say nothing is watching |
| `unbracketed_runs` | the runs steward buried on their session's behalf, so nobody is mourned twice |

SQLite rather than a JSON file because the two interesting writes are both
conditional — claiming an open task and deciding a pending approval — and "the first
writer wins, everyone else reads back what was recorded" is exactly what a
read-modify-write over a JSON file cannot promise.

Schema changes are `ALTER TABLE`, applied at open time. A steward that has been running
since the API landed already has a `steward.db` full of real jobs, and a migration that
drops it is a migration that loses work.
