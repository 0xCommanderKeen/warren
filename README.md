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
   `task_claimed`, `task_done`, `task_failed`, `task_delegated`, `resident_restarted`,
   structured `needs_human` payloads, `needs_human_resolved`) and burrow only ever renders
   them.
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

Ten pieces exist.

**Resident manifests and charters** (#1). Souls and manifests are versioned here and
validated in CI.

**The scheduler and the runner seam** (#2, #11, #21). Routines fire on a cron schedule in
a declared time zone, through one runner abstraction (`claude` / `codex` / a command
template / a mock), with the charter, voice, and journal assembled in one place, and every
run bracketed by real burrow events. Nothing fires unless `steward scheduler run` is up:
an enabled routine is a declaration, not an animation.

**The resident session lifecycle** (#118). Every real wake-up — scheduled routine,
manual fire, claimed board task, or delegated letter — crosses one `admit` → `run` seam.
It owns the safety-critical provision → context → prompt → runner → completion accounting
→ harvest order. The scheduler still owns occurrences and routine events; the board still
owns claims, leases, and task events; production and mock runners remain interchangeable
at the runner seam.

**Named durable transitions** (#123). Every durable state change and the burrow fact that
says it happened are coordinated in one place, per domain: posting, claiming, finishing,
failing and lease expiry in `transitions/task.py`; raising, deciding and expiring in
`transitions/approval.py`; accepted handoffs in `transitions/delegation.py`; pause and
resume in `transitions/budget.py`. Callers ask for the domain act and get its durable
result — they no longer interpret a `rowcount`, choose an identity, or decide whether to
emit. The invariant is one sentence: a fact reaches the emitter only on the branch where
the write actually won, and exactly once. Refusals, replays, expiries and lost races write
nothing and say nothing; the one deliberate exception, a repeat auto-deny, is named as its
own outcome so it cannot happen anywhere by accident. Persistence and delivery stay two
systems — there is no bus, no outbox, and no callback out of the store. The full matrix is
`docs/transitions.md`.

**The journal and the soul voice** (#5, #9). A resident closes its day by writing a short
markdown entry into the location its own manifest declares, and the next session opens
with that entry. The resident writes it; steward only asks for it, reads it back, and
keeps the directory bounded — it never summarizes a day on a resident's behalf and never
invents an entry. Souls carry a `## Voice` section, capped and framed as style only, so
what Hob writes reads like Hob. Personality is expressed only through real work products:
a voice adds no event, no movement, no ambient liveliness to the village.

```console
$ steward doctor                     # which brain, the journal, the post, what fires next
$ steward scheduler run              # the daemon: sleep to the next due routine, fire
$ steward scheduler tick             # fire anything due now, then exit (external cron)
$ steward scheduler tick --dry-run   # print what would fire, and the whole prompt
$ steward journal life-agent         # what Hob has actually written, newest first
$ steward show life-agent            # the exact preamble Hob's next session opens with
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

**The skills library** (#12). `skills/<name>/SKILL.md` — a named, reusable capability
written as instructions. A documented default set (`write-journal`, `daily-summary`,
`research`, `escalate`) is held by every resident; everything else is granted by name in
a manifest, and a name the library does not have fails validation with the closest match
named. At run time the resident session lifecycle resolves the effective set, injects it into the prompt
under a frame saying a skill is how-to and never authority, and — for runners that load
skills off disk — writes it into the session's working directory, removing what is no
longer granted. The library is shared, so improving a skill improves every resident that
holds it.

```console
$ steward skills                     # the library, and each resident's effective set
```

**The job board** (#6). One place work can be dropped for the fleet, instead of prompting
a particular resident. Dispatch is pull-based: a resident that declared `board: {claim:
true}` in its manifest claims the oldest open task whose required skills it holds — its
*effective* set, so a task tagged `research` is claimable by a resident that was granted
nothing — on its own next wake-up, and works it as an ordinary headless session with the
same skills, journal, and charter a routine session gets. Claiming is a single
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

**Delegation** (#7). A resident can hand work to a neighbour, and steward is the only
arbiter: both manifests have to agree — `delegation: {send: true}` on the sender, an active
route of kind `delegation` on the receiver — and steward enforces what no manifest can see,
a depth cap (default three hops) and a flat refusal of any chain that would revisit a
resident. A session asks with a `<delegate>` block in its output or `steward delegate`;
steward validates, delivers into the receiver's inbox, and emits `task_delegated` naming
both ends, so burrow can finally show a villager walking to a *specific* neighbour's door.
Delivery is pull-based like everything else: the receiver drains its inbox on its own next
wake-up, ahead of the open board, and works the item as an ordinary session that reads the
letter as a request from a colleague rather than an instruction. Every item records its
parent, its depth, and the origin the chain rolls up to, so what a fleet spent answering
one question attributes to that question. Refusals are structured, write nothing, emit
nothing — and a refused block still knocks at a human's door. The grammar, the guardrails,
and the lineage model are in [docs/delegation.md](docs/delegation.md).

```console
$ steward delegate burrow-builder --to life-agent --route handoff --title "…"
$ steward inbox life-agent            # what is waiting, from whom, at what depth
$ steward task lineage <task_id>      # the whole chain, root first
```

Closing a route stops delivery but not the pile already behind it, and nothing claims a
letter while the door is shut — so `steward doctor` counts every resident's post and fails
on the one case nobody would otherwise notice: open letters behind a `pending` or
`disabled` delegation route.

**The watchdog and budgets** (#8). An agent nobody is watching can fail in two directions,
and steward now answers both. A manifest declares `budgets: {daily_cost_usd, daily_tokens,
max_run_seconds}`; every finished session — routine, board task, delegated item, run-now —
appends what it actually cost to a ledger on disk, and "today" is `[local midnight, next
local midnight)` in the resident's own zone, computed from the calendar at the moment
somebody asks. A daily cap that reset because the daemon bounced would not be a cap. When
one trips, the resident is **paused** — no fires, no claims, run-now answers `409 paused:
budget exceeded` — and steward knocks **once**, naming the budget and the number, through
the same approvals machinery everything else uses. Approving that knock resumes the
resident for the rest of that day; tomorrow's cap applies to tomorrow.

The watchdog is the other direction. `LocalProbe` sees what steward can truthfully see
about itself — a scheduler anchor that stopped advancing, a lease held past its expiry, a
run that started and never came back — and `DockerSupervisor` restarts the container a
manifest names in `deploy.container`, which is now a container [the nursery](#deployment)
actually created. Every intervention emits `resident_restarted` with its reason
and attempt number, because a silent restart is a lie by omission; attempts are bounded
(1m, 5m, 25m, three of them) and then steward stops and asks a person instead. And a
`routine_started` that no closing event ever answered becomes `routine_failed` with
`error: "run never reported back"`, emitted exactly once — the village must never show
eternal work. What "never answered" is read from is steward's own **run registry** (#39):
a row per session, written where the opening event is emitted and closed where the closing
one is. That row exists whatever happened to the events, so a session that died after its
`routine_started` reached burrow is found — which it was not while the only thing the
watchdog could read was the log of events burrow never received.

Age alone is not death. Each registry row also carries a renewable ownership lease and
the absolute path of the complete local event record. The scheduler and board renew the
lease through the whole lifecycle, including accounting, harvesting, delegation, task
closure, and terminal publication after the child exits. The live owner and watchdog race
to store one immutable terminal event under a fencing token/stale-heartbeat condition.
That chosen event has a stable identity, is replayed after either crash window, and closes
only after a remote or fsynced local sink accepts it. Each row's own recorded event-log
path is checked independently; missing, unreadable, or corrupt evidence blocks that path's
rows without hiding healthy rows elsewhere. Legacy migrated rows with no known path are
refused loudly. Only a malformed final line lacking a newline is treated as a torn append.

```console
$ steward budget show                # today's spend against every declared cap
$ steward budget unpause life-agent  # or approve the knock from burrow's panel
$ steward watchdog run                # probe, sweep, bury stale runs, check budgets
$ steward watchdog tick               # one pass, then exit (external cron)
```

Delegated work is budgeted work. A letter costs the receiver's day, not the sender's, so
the inbox is gated by the same pause the board is and lands on the ledger under kind
`delegated` — `steward budget show --by-origin` then answers what a fleet spent answering
one question, however many neighbours it went through.

**The nursery** (#4). Raising a resident used to be an SSH ritual: hand-write a soul,
hand-write a compose service, tar it to the NAS, wire `BURROW_TOKEN` and the emitter env by
hand, restart, hope. It is one command now, and three stages — **declare** the soul and
manifest into this repo and commit them, **provision** the container on the NAS, **register**
by checking the scheduler can actually run it and reporting when each routine next fires.

Every stage converges: run it again after a failure and it picks up where it stopped rather
than duplicating anything. The declaration is committed *before* any infrastructure is
touched, so a failed deploy leaves exactly one commit to revert and one command to re-run.
`--dry-run` prints the files, the compose fragment, the exact ssh commands and the next
fires, and provably touches nothing — no commit, no ssh, no scheduler state.

Nothing is emitted on the new resident's behalf, ever. A villager appears in burrow when it
genuinely exists and emits its own first event, and `steward retire` is the counterpart: it
marks the manifest `retired: true`, commits that, and then stops and removes the container.
A retired resident stops firing routines, claiming board work, taking letters and answering
run-now — and leaves the village the only honest way, by going quiet. The soul and manifest
stay in git; retirement is a lifecycle state, not a deletion.

```console
$ steward new-resident --id note-keeper --name Quill --char Scribe \
    --accent '#4f7ea6' --role 'note bot' --charter charter.yaml --dry-run
$ steward new-resident … --skills research     # declare, commit, build, check
$ steward retire note-keeper                    # stop it, and say so in git
```

**The management console** (#13). A browser console for the fleet, served by steward's own
API at `/ui` — static HTML, CSS, and one JavaScript file, no framework and no build step,
so it runs on a NAS with the internet unplugged. Residents, a new-resident form, the
fleet-wide routine ledger, approvals, the job board, and the skills library, all of it read
from the endpoints above and none of it invented. It is a pure client: the repo stays the
source of truth, and there is no page here that edits a manifest, because there is no
endpoint that would let one. The one thing it does beyond reading is raise a resident —
the form's **deploy** checkbox drives the nursery above through `POST /residents`, and the
ledger then prints the plan and the pipeline's own report back, verbatim. Retired residents
wear a badge and refuse run-now, the same `409` the API gives. Details and the shape of it
are in [Management UI](#management-ui) below.

Nothing in this repo's build phase is roadmap any more. Burrow-side rendering
counterparts — the journal panel in a villager's house, the notice board, the letter carried
across the village, the fleet-ops fuel gauges — live in burrow's issues.

## Deployment

Residents run as docker compose services on the NAS (`dxp2800`), over Tailscale, beside
burrow's own server at `~/docker/burrow`. **Steward puts them there.** This section
replaces the manual ritual in burrow's README for anything that is a resident; burrow's
own server is still deployed by hand from that repo.

```console
$ export BURROW_URL=http://dxp2800:8737
$ export BURROW_TOKEN=…                    # the village's shared ingest secret

$ steward new-resident --id note-keeper --name Quill --char Scribe \
    --accent '#4f7ea6' --role 'note bot' --charter charter.yaml --dry-run   # read the plan first
$ steward new-resident … --skills research     # declare, commit, build, check
```

`--charter` points at a YAML file — a charter is prose somebody thought about, and prose
belongs in a file a diff can review rather than in four shell arguments. It mirrors the
manifest's `charter` block exactly:

```yaml
mission: One paragraph of purpose.
duties:
  - Standing responsibilities, one per line.
rules:
  - Hard constraints, e.g. "Never send email without explicit approval."
escalation: When and how to raise needs_human instead of acting.
```

The other flags: `--skills` grants library skills by name (comma-separated); `--runner`
and `--model` choose the brain (default `claude`); `--project` and `--summary` are the
optional soul fields; `--repo` names the checkout to commit into; `--dry-run`,
`--allow-dirty`, and `--no-commit` behave as they do for `retire`; and **`--no-deploy`** is
the only host-less path — it declares and checks but builds no container, for developing a
resident before it has a machine. `--skills` is a grant *on top of* the default set every
resident already gets, so naming a default skill (`write-journal`, `escalate`) is redundant
rather than additive — grant only what a resident holds beyond the defaults.

The three stages, and what each one really does:

**Declare.** `residents/<id>/manifest.yaml` and `residents/<id>/soul.md` are written, read
back through the ordinary validator, and committed — before anything else happens, because
the repo is the source of truth and a failed deploy should leave one commit to revert and
one command to re-run. A dirty worktree is refused unless `--allow-dirty` says out loud
that you want it anyway.

**Provision.** The compose fragment is rendered from `steward/templates/`, the runtime
bundle is packed into a tar **in memory**, and the whole thing is piped over
`ssh Miha@dxp2800 tar -xf - -C ~/docker/steward-<id>` — a pipe rather than `scp`, because
UGOS's `scp` is broken and the pipe is what has actually worked all along. Then
`docker compose up -d` over the same ssh. Every external command goes through
`steward.runners.run_argv`, which is the only file in steward that starts a process.

The bundle is compared to what is already on the host file by file, so a second run
uploads nothing. `up -d` is issued either way — it is the only thing here that can bring
back a container that is *down*, and reconciling is what a second run is for.

**Register.** There is no second registry. Routines are read off the manifest by the
scheduler on every tick, so "registered" means the manifest is valid, the runner's binary
exists, the journal location is writable, every granted skill resolves — the same
`steward doctor` check — and here is when each routine next fires.

Where a resident lands is a manifest question with documented defaults (`dxp2800`, `Miha`,
`~/docker/steward-<id>`, `steward-resident:latest`); override any of them in
[`deploy`](docs/manifest.md#deploy--where-this-resident-runs).

**What a resident's container actually is.** `steward-resident` is built from
[`docker/resident/Dockerfile`](docker/resident/Dockerfile): `node:22-slim`, the `claude`
CLI pinned by build arg, python3, and a vendored copy of burrow's `hooks/emit.py` wired
into the same six claude hooks the Mac uses. There is no registry here, so the image
travels like everything else does — a pipe over ssh:

```console
$ make image                                          # linux/amd64, for the NAS
$ make image-ship                                     # docker save | ssh dxp2800 docker load
$ ssh Miha@dxp2800 docker exec steward-<id> steward-smoke
smoke: ok   claude 2.1.243 (Claude Code)
smoke: ok   POST http://dxp2800:8737/events -> 204
smoke: PASS this container can reach the village
```

`steward-smoke` runs inside the container and is [issue
#51](https://github.com/0xCommanderKeen/steward/issues/51)'s acceptance criterion made
executable. The container still runs `sleep infinity`: **the scheduler runs sessions
locally**, in the process running `steward scheduler run`, and nothing in steward execs
into a resident's container yet. The image is what makes that step possible, not the step
itself.

**Secrets never enter this repo.** `BURROW_URL` and `BURROW_TOKEN` are read from steward's
own environment at provision time and written into a `.env` on the host at mode `0600`.
The compose file carries `${BURROW_TOKEN-}` — a reference, not a value. The repo's own
credential scanners are run over everything the nursery writes into the checkout, as a
test. Provisioning without `BURROW_URL` is refused: a container with nowhere to emit is a
resident that would never appear in the village at all.

Retiring is the counterpart, and it goes the other way round on purpose:

```console
$ steward retire note-keeper --dry-run
$ steward retire note-keeper
```

The manifest is marked `retired: true` and committed **first**, then the container is
stopped and removed — because the watchdog would otherwise notice the container go away
and dutifully put it back. A retired resident fires no routines, claims nothing off the
board, receives no letters, and answers `409 resident_retired` to run-now; it leaves the
village by going quiet, and steward forges no `session_ended` on its behalf. The soul and
the manifest stay in git.

`steward retire --no-deploy` marks and commits the manifest but reaches no host — the
counterpart to `new-resident --no-deploy`, for a resident whose host is already gone or was
never steward's to stop. It stops taking work the moment the mark is committed either way.
The other flags mirror the two commands: `--dry-run` touches nothing, `--no-commit` writes
the mark without committing it, `--allow-dirty` commits over a dirty worktree, and `--repo`
names the checkout when it is not the parent of the residents tree.

Burrow's viewer reaches the same pipeline through `POST /residents` with `deploy: true`
([docs/api.md](docs/api.md#post-residents)) — one implementation, two front doors. The API
never commits, because the server may not own the checkout it is reading, and it says so
in the response.

## Residents

```
residents/
  life-agent/       manifest.yaml + soul.md   Hob, the household spirit
  burrow-builder/   manifest.yaml + soul.md   Maren, who builds the village
skills/
  <name>/SKILL.md                             the shared library both draw on
```

Each manifest declares the resident's soul identity, charter (mission, duties, hard
rules, escalation policy), and the five capability dimensions burrow renders — skills,
memory, routes, app grants — plus the runner steward launches sessions through, the
routines it fires, whether the resident takes work off the job board, and whether it may
hand work to another resident. Its `skills`
are grants by name against the shared library — what this resident holds on top of the
default set every resident gets. References and grants only: a credential-shaped key or
an inline secret, in a manifest or in a `SKILL.md`, fails validation and is never stored.

The schema is documented in [docs/manifest.md](docs/manifest.md), and
`steward schema` emits it as JSON Schema so burrow can read manifests without
translation. The generated copy is committed at
[schema/resident-manifest-v0.json](schema/resident-manifest-v0.json) — the path the
schema's own `$id` promises — and a test fails when it drifts from the models, so a
manifest change that would break burrow's reader shows up as a diff in the pull request
that makes it. Regenerate with `make schema-write` and read the diff.

```console
$ steward validate                         # the whole residents/ tree
ok: 2 valid resident(s), 0 error(s), 0 warning(s) in residents

$ steward validate residents/life-agent    # or one resident, or one file
```

Diagnostics always name the file, the field path, the problem, and an example of a valid
value, and the same check is importable (`steward.validate_tree`, `steward.load_manifest`)
so the scheduler, the API, and CI share one load-and-validate path.

## Management UI

The operator console. Burrow renders what the fleet *does*; this manages what the fleet
*is*. Steward serves it from its own API, so there is nothing else to deploy:

```console
$ STEWARD_TOKEN=… steward serve
steward api on http://127.0.0.1:8801 (cors: none (same-origin only))
management console on http://127.0.0.1:8801/ui/ (from /srv/steward/ui)
```

<!-- screenshot: ui/ at 1440×900, Residents tab, dark. Replace this comment with
     ![The residents list](docs/images/ui-residents.png) once one is taken. -->

*(Screenshot placeholder — the residents list, dark, at 1440×900.)*

```
ui/
  index.html    the shell: rail, main, the token gate, the pending ledger
  app.css       one stylesheet — no framework, no CDN, no webfont
  app.js        one script — one ROUTES map, one fetch, seven views
```

Six views behind hash routing: **Residents** (list, then soul, charter, voice, effective
skills, routines, budget, journal, inbox), **New resident**, **Routines** (fleet-wide,
with run-now), **Approvals**, **Job board**, and **Skills**.

Four things are worth knowing about it.

**One token, once.** The first load asks for `STEWARD_TOKEN`, keeps it in this tab's
`sessionStorage`, and sends it as a bearer header on every request. A `401` forgets it and
asks again. The three static files are the one thing on the server *not* behind the token,
because the browser has to load the script before there is anything to ask a person with —
they carry no fleet data, and every byte the console displays it fetched with the token.

**No optimistic success.** Every mutating action moves through three states, visible in a
ledger in the corner: **asked** → **accepted** (with the request id steward returned) →
**confirmed** or **failed**. Nothing reaches the last state on the strength of a 202: a
run-now is confirmed by polling `GET /requests/{id}` until steward's own log says `ran`, a
posted job by finding it on the board, a decision by reading the approval record back, a
declaration by watching the new manifest come through the validator. Until then it says
*accepted, not yet confirmed*, and if three minutes pass with no outcome it says that too.

**Empty states say why.** No residents, no routines, an empty journal, an empty library —
each explains what would have to be true for it to be full, because *nothing here* and
*steward cannot see it* are different facts. Refusals render the API's own `error` code and
`message` verbatim, with the whole response one click away.

**It is a client, not an editor.** Skills are read-only, and so are manifests: a skill is
added by committing a `SKILL.md` and granted by committing a manifest. The one thing it
writes is a new resident, and the form's **deploy** checkbox — `window.STEWARD_UI.deploy`
in `index.html`, on now that the endpoint is real — sends `deploy: true` and runs the whole
nursery. What comes back is printed rather than paraphrased: the target, every file in the
bundle, the `.env` key *names*, the exact commands steward ran, the compose fragment
verbatim, and each routine's next fire. Steward still never commits from the server, and
the answer says so. Retired residents wear a badge in the list and the detail header, and
their run-now button is dead with `409 resident_retired` written on it — a control that can
only fail should look like one before it is pressed.

## Development

Python 3.14, Node.js 22, [uv](https://docs.astral.sh/uv/), ruff, ty, pytest. Node runs the
browser-free UI behavior tests as part of pytest; `.node-version` is the supported major.

```console
make dev       # uv sync --all-groups
make lint      # ruff format --check, ruff check, ty
make format    # ruff format + ruff check --fix
make test      # pytest with coverage
make check     # what CI runs: lint, test, validate
```

A routine only ever fires while `steward scheduler run` is up — and only one of them, per
state file: a second daemon refuses to start and names the pid already holding the lock,
while a cron `steward scheduler tick` beside a running daemon simply takes its turn and
finds nothing due. Missed schedules are not back-filled, an overlapping fire is skipped
rather than queued, and a run killed at its timeout is emitted as `routine_failed` — the
village must never show work that is not happening. The same rule governs memory: a day
with no journal entry has no journal entry, and the next session is told nothing rather
than something plausible. And the same rule governs the board and the door: a task nobody
finished goes back to `open` loudly, and a request nobody answered is a `deny`, never a
quiet yes. A restart is announced, a run that never reported back is buried out loud, and
a resident that has spent its day stops and says which number stopped it.

## Environment

Steward reads a handful of environment variables. None is required for `steward validate`;
the scheduler and the API name the ones they need on startup.

| variable | who reads it | meaning |
|---|---|---|
| `STEWARD_STATE` | scheduler | Path to the scheduler's state **file** (its last-fire anchors), not a directory — a `STEWARD_STATE` that names a directory is fatal, because a scheduler that cannot persist an anchor re-fires forever. `steward.db` lands **beside** it, so point this at e.g. `~/.steward/state.json` and the database is `~/.steward/steward.db`. |
| `STEWARD_TOKEN` | API | The bearer token every endpoint requires. Unset or blank refuses to start unless `--allow-open` says out loud this is loopback-only local development. |
| `STEWARD_EVENTS_FALLBACK` | everything that emits | Where events are appended when burrow is unreachable, so nothing a session did is lost. Defaults to `~/.burrow/events.jsonl`. |
| `STEWARD_CORS_ORIGINS` | API | Comma-separated origins allowed to call the API from a browser. Unset means same-origin only. |
| `STEWARD_UI` | API | Directory of the management console's static files. Unset looks for `ui/` beside the package and then in the checkout. |
| `STEWARD_MAX_DELEGATION_DEPTH` | delegation | How deep a chain of delegated work may run before steward refuses (default 3). `0` is the fleet-wide kill switch. |
| `BURROW_URL` | emitter, nursery | The village's ingest URL. Provisioning a resident without it is refused: a container with nowhere to emit would never appear in the village. |
| `BURROW_TOKEN` | emitter, nursery | The village's shared ingest secret, written into the resident's host `.env` at provision time and never into this repo. |

Most take a matching CLI flag where a command needs one — `--state`, `--db`, `--host`,
`--allow-open`, `--residents` — and the flag wins over the variable.
