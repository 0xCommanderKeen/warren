# steward

The control plane for the agent fleet that [burrow](https://github.com/0xCommanderKeen/burrow) watches.

Burrow is the reader: an ambient pixel-art village that truthfully projects fleet
events and never invents behavior. **Steward is the actor.** It owns agent
lifecycles: it deploys residents, schedules their routines, injects their charters
and personalities into headless sessions, routes approvals back to waiting agents,
and passes work between residents. Everything steward does is emitted as burrow
protocol events — to the village, steward is just another emitter.

## The split

| concern | owner |
|---|---|
| Rendering the village, panels, boards | burrow |
| Souls, resident manifests, charters (source of truth, in git) | steward |
| Scheduling routines, launching headless sessions (`claude -p` / Agent SDK) | steward |
| Deploying/retiring resident containers on the NAS | steward |
| Job board storage and dispatch; inter-resident delegation | steward |
| Approval routing (human decision → waiting agent) | steward |
| Watchdog, restarts, per-resident budgets | steward |
| Event log, ingest, SSE | burrow |

They share **contracts, not code**:

1. **The event protocol** — burrow's `docs/protocol.md`. Steward adds event types
   (`routine_started`, `routine_finished`, `routine_failed`, `task_posted`,
   `task_claimed`, `task_done`, `task_failed`, `task_delegated`, structured `needs_human`
   payloads, `needs_human_resolved`) and burrow only ever renders them.
2. **The resident manifest** — the versioned declaration of a resident's soul,
   charter, skills, memory, routes, and app grants. Steward deploys from it;
   burrow reads it for display. References and grants only — never credentials.

## The write boundary

Human actions in burrow's UI (run a routine now, approve a request, post a job,
create a resident) call **steward's own token-gated HTTP API** directly, tailnet
only. Burrow's server never gets write access to agents, and steward never renders
anything. The UI treats the event stream as the only confirmation of effect: no
optimistic state the fleet hasn't confirmed.

## Status

Five pieces exist.

**Resident manifests and charters** (#1). Souls and manifests are versioned here and
validated in CI.

**The scheduler and the runner seam** (#2, #11, #21). Routines fire on a cron schedule in
a declared time zone, through one runner abstraction (`claude` / `codex` / a command
template / a mock), with the charter, voice, and journal assembled in one place, and every
run bracketed by real burrow events. Nothing fires unless `steward scheduler run` is up:
an enabled routine is a declaration, not an animation.

**The journal and the soul voice** (#5, #9). A resident closes its day by writing a short
markdown entry into the location its own manifest declares, and the next session opens
with that entry. The resident writes it; steward only asks for it, reads it back, and
keeps the directory bounded — it never summarizes a day on a resident's behalf and never
invents an entry. Souls carry a `## Voice` section, capped and framed as style only, so
what Hob writes reads like Hob. Personality is expressed only through real work products:
a voice adds no event, no movement, no ambient liveliness to the village.

```console
$ steward doctor                     # which brain, where the journal lives, what fires next
$ steward scheduler run              # the daemon: sleep to the next due routine, fire
$ steward scheduler tick             # fire anything due now, then exit (external cron)
$ steward scheduler tick --dry-run   # print what would fire, and the whole prompt
$ steward journal life-agent         # what Hob has actually written, newest first
```

**The HTTP API** (#3). The token-gated write path burrow's viewer calls directly, so
burrow's server never gets write access to agents: run a routine now, post a job to the
board, answer an approval, declare a new resident. The contract is acknowledgement, not
effect — an accepted request returns a request id and the word *accepted*, and the work
is confirmed only when the matching protocol event lands in burrow's log. Tailnet only,
one shared token, documented in [docs/api.md](docs/api.md).

```console
$ STEWARD_TOKEN=… steward serve      # 127.0.0.1:8801 by default; never public
$ steward serve --allow-open         # local dev, with the missing token said out loud
```

**The job board** (#6). One place work can be dropped for the fleet, instead of prompting
a particular resident. Dispatch is pull-based: a resident that declared `board: {claim:
true}` in its manifest claims the oldest open task whose required skills it holds, on its
own next wake-up, and works it as an ordinary headless session. Claiming is a single
conditional `UPDATE … WHERE status = 'open'`, so two residents waking at once can never
both hold one task. A claim is a lease, not a deed — thirty minutes by default; when it
expires the task returns to the board as a `task_failed` with reason `lease_expired`,
because work that quietly vanished would be the board lying about it.

```console
$ steward board list                 # the board, and who could take what is open
$ steward board dispatch             # sweep expired leases, then claim and work
```

**Structured approvals** (#10). A session that reaches an action its charter gates does
not do it — it asks, in a `<needs-human>` block in its output or through `steward approval
raise`, and finishes its turn. Steward turns the ask into a durable request a human
answers from burrow's panel or a notification, and the decision is delivered at the top
of the resident's next session. Two safety properties are the point: **deny by default**
(past `expires_at`, steward resolves the request as `deny` with `decided_by: "expiry"`,
and the gated action never ran) and **first decision wins** (a replay changes nothing and
emits nothing). The grammar and both paths are in [docs/approvals.md](docs/approvals.md).

```console
$ steward approval raise life-agent --action send_email --detail-json '{"to": "…"}'
$ steward approval show <request_id>  # request, decision, decider, timestamps
```

Still roadmap, in this repo's issues: the skills library (#12), delegation (#7), the
watchdog and budgets (#8), deployment (#4), and the management UI (#13). Burrow-side
rendering counterparts — the journal panel in a villager's house, the notice board —
live in burrow's issues.

## Residents

```
residents/
  life-agent/       manifest.yaml + soul.md   Hob, the household spirit
  burrow-builder/   manifest.yaml + soul.md   Maren, who builds the village
```

Each manifest declares the resident's soul identity, charter (mission, duties, hard
rules, escalation policy), and the five capability dimensions burrow renders — skills,
memory, routes, app grants — plus the runner steward launches sessions through, the
routines it fires, and whether the resident takes work off the job board. References and
grants only: a credential-shaped key or an inline secret fails validation and is never
stored.

The schema is documented in [docs/manifest.md](docs/manifest.md), and
`steward schema` emits it as JSON Schema so burrow can read manifests without
translation.

```console
$ steward validate                         # the whole residents/ tree
ok: 2 valid resident(s), 0 error(s), 0 warning(s) in residents

$ steward validate residents/life-agent    # or one resident, or one file
```

Diagnostics always name the file, the field path, the problem, and an example of a valid
value, and the same check is importable (`steward.validate_tree`, `steward.load_manifest`)
so the scheduler, the API, and CI share one load-and-validate path.

## Development

Python 3.14, [uv](https://docs.astral.sh/uv/), ruff, ty, pytest.

```console
make dev       # uv sync --all-groups
make lint      # ruff format --check, ruff check, ty
make format    # ruff format + ruff check --fix
make test      # pytest with coverage
make check     # what CI runs: lint, test, validate
```

A routine only ever fires while `steward scheduler run` is up. Missed schedules are not
back-filled, an overlapping fire is skipped rather than queued, and a run killed at its
timeout is emitted as `routine_failed` — the village must never show work that is not
happening. The same rule governs memory: a day with no journal entry has no journal
entry, and the next session is told nothing rather than something plausible. And the same
rule governs the board and the door: a task nobody finished goes back to `open` loudly,
and a request nobody answered is a `deny`, never a quiet yes.
