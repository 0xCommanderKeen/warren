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
| create a resident | nothing — it is a file for review, not a deployment |

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

`202` with a `request_id`. The run goes through the scheduler's own fire path, so it
gets the same prompt assembly, the same timeout, the same runner seam, and the same
bracketing events as a scheduled fire — with `routine_started.payload.trigger` set to
`manual` instead of `schedule`, so the ledger can tell prompted work from standing
work.

| status | error | meaning |
|---|---|---|
| 404 | `unknown_resident` | no such resident in the residents tree |
| 404 | `unknown_routine` | the manifest declares no routine by that name |
| 409 | `routine_disabled` | declared but `enabled: false`; enable it in the manifest |
| 409 | `already_running` | the scheduler's overlap rule: skipped, never queued |
| 409 | `resident_invalid` | the resident exists but its manifest does not validate |

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
`delegated_by`, `route`, `depth`, `origin`, and `parent_task_id`, and `routes` names the
doors that resident declares.

`GET /tasks/{task_id}/lineage` is the audit query: the whole chain, root first, with the
origin it attributes to. `404` for a task steward has never seen.

The lifecycle after delivery is the board's, unchanged: `task_claimed` on pickup, then
`task_done`/`task_failed` — all three carrying `parent_task_id`. The declarations both ends
must make, the block grammar, and the guardrails are in
[docs/delegation.md](delegation.md).

### `GET /approvals` · `GET /approvals/{request_id}` · `POST /approvals/{request_id}`

`GET /approvals` lists gated actions. `?status=pending` (the default), `resolved`, or
`all`; anything else is a `422` with `unknown_status`. The default is unchanged from
before the parameter existed, so a panel that never passed it sees exactly what it saw.

`GET /approvals/{request_id}` is the audit query: request, full detail, decision,
decider, and every timestamp, in one call. `404` for an id steward has never seen.

`POST /approvals/{request_id}` answers one:

```json
{"decision": "approve" | "deny" | "edit", "edit": {"subject": "shorter"}}
```

The decision is recorded durably and emitted as `needs_human_resolved` with
`{request_id, decision, decided_by, action}`, under the *resident's* agent id — the
villager burrow has to walk away from your door is the one who knocked.

Decisions are idempotent: the first one wins. `202` the first time; a replay (a
double-tapped notification, a retried request) is `200`, returns the recorded outcome,
changes nothing, and emits nothing. An unknown `request_id` is `404`.

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
  "skills": ["research"], "runner": {"kind": "claude", "model": "claude-opus-5"}
}
```

Writes `residents/<id>/manifest.yaml` and `residents/<id>/soul.md` and reads them back
through the ordinary validator, so what it produces is something `steward validate`
accepts — checked, not claimed. A skeleton that fails validation is removed rather
than left to break the tree for everyone.

`201` with the paths it wrote. **It deploys nothing**: no container, no scheduler
registration, no event on the new resident's behalf. A villager appears in burrow when
it genuinely exists and emits. `409` if the resident already exists — converging an
existing declaration is the nursery's job, not an accidental overwrite of a soul
someone wrote.

### `GET /residents` · `GET /residents/{id}`

Read-only JSON views of validated manifests, including `runner.kind` and
`runner.model`, so "which brain is Hob on" is answerable over HTTP. Manifests that did
not validate are named in `errors` rather than quietly omitted. There is nothing to
redact: a manifest holding a credential-shaped key or an inline secret would have
failed validation and never become a resident at all.

`skills` is what the manifest grants; `effective_skills` is what a session actually
gets — the library's defaults plus those grants, in injection order:

```json
{"skills": [{"id": "read-inbox", "source": "library", "note": "…"}],
 "effective_skills": ["daily-summary", "escalate", "research", "write-journal", "read-inbox"]}
```

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

## Storage

`.steward/state/steward.db` (beside the scheduler's `scheduler.json`; both follow
`$STEWARD_STATE`, and `--db` overrides the database).

| table | holds |
|---|---|
| `jobs` | the board *and* the inboxes: task, status, claimant, lease, artifacts — plus `assignee`, `delegated_by`, `route`, `parent_task_id`, `origin`, `depth` when delegated |
| `approvals` | the request, its full detail, the decision, and whether it was delivered |
| `requests` | every accepted mutating request and what became of it |

SQLite rather than a JSON file because the two interesting writes are both
conditional — claiming an open task and deciding a pending approval — and "the first
writer wins, everyone else reads back what was recorded" is exactly what a
read-modify-write over a JSON file cannot promise.

Schema changes are `ALTER TABLE`, applied at open time. A steward that has been running
since the API landed already has a `steward.db` full of real jobs, and a migration that
drops it is a migration that loses work.
