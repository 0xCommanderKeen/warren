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

`202` with a `task_id`. The task is persisted (SQLite, so the claim in steward #6 can
be a single atomic `UPDATE … WHERE status = 'open'`) and announced as `task_posted`
with `{task_id, title, required_skills, posted_by: "api"}`. No resident is prompted:
dispatch is pull-based, and a resident claims work on its own next wake-up.

`GET /jobs` lists the board. Everything is `open` today — claiming, leases, and the
rest of the lifecycle land with the job board itself.

### `GET /approvals` · `POST /approvals/{request_id}`

`GET` lists the gated actions still waiting on a human. `POST` answers one:

```json
{"decision": "approve" | "deny" | "edit", "edit": {"subject": "shorter"}}
```

The decision is recorded durably and emitted as `needs_human_resolved` with
`{request_id, decision, decided_by, action}`, under the *resident's* agent id — the
villager burrow has to walk away from your door is the one who knocked.

Decisions are idempotent: the first one wins. `202` the first time; a replay (a
double-tapped notification, a retried request) is `200`, returns the recorded outcome,
changes nothing, and emits nothing. An unknown `request_id` is `404`.

Requests are *created* by the session that reaches a gated action, through
`Store.create_approval_request(...)` — steward #10 wires that up. The API only ever
answers a request; it never invents one.

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
| `jobs` | the board: task, required skills, status, claimant |
| `approvals` | the request, its full detail, and the decision it received |
| `requests` | every accepted mutating request and what became of it |

SQLite rather than a JSON file because the two interesting writes are both
conditional — claiming an open task and deciding a pending approval — and "the first
writer wins, everyone else reads back what was recorded" is exactly what a
read-modify-write over a JSON file cannot promise.
