# steward

The control plane for the agent fleet that [chronicle](../chronicle/) watches.

Chronicle is the reader: an ambient pixel-art village that truthfully projects fleet
events and never invents behavior. **Steward is the actor.** It owns agent
lifecycles: it deploys residents, schedules their routines, injects their charters
and personalities into headless sessions, routes approvals back to waiting agents,
and passes work between residents. Everything steward does is emitted as chronicle
protocol events — to the village, steward is just another emitter.

## The split

| concern | owner |
|---|---|
| Rendering the village, panels, boards | chronicle |
| Souls, resident manifests, charters (source of truth, in git) | steward |
| Scheduling routines, launching headless sessions (`claude -p` / Agent SDK) | steward |
| Deploying/retiring resident containers on the NAS | steward |
| Job board storage and dispatch; inter-resident delegation | steward |
| Approval routing (human decision → waiting agent) | steward |
| Watchdog, restarts, per-resident budgets | steward |
| Event log, ingest, SSE | chronicle |

They share **contracts, not code**:

1. **The event protocol** — chronicle's `docs/protocol.md`. Steward adds event types
   (`routine_started`, `routine_finished`, `routine_failed`, `task_posted`,
   `task_claimed`, `task_done`, `task_failed`, `task_session_finished`, `task_delegated`,
   `resident_restarted`,
   structured `needs_human` payloads, `needs_human_resolved`) and chronicle only ever renders
   them.
2. **The resident manifest** — the versioned declaration of a resident's soul,
   charter, skills, memory, routes, and app grants. Steward deploys from it;
   chronicle reads it for display. References and grants only — never credentials.

## The write boundary

Human actions in chronicle's UI (run a routine now, approve a request, post a job,
create a resident) call **steward's own token-gated HTTP API** directly, tailnet
only. Chronicle's server never gets write access to agents, and steward never renders
anything. The UI treats the event stream as the only confirmation of effect: no
optimistic state the fleet hasn't confirmed.

### Operator credentials

`STEWARD_TOKEN` is the credential of a terminal and an environment: it names nobody, it is
the same key that boots the process, and rotating it means a restart. A **browser** needs
something else, so a named operator gets their own credential (warren#225):

```console
$ steward operator mint Miha --email miha@example.invalid --note townhall
minted an operator credential for Miha
commits will be authored by Miha <miha@example.invalid>
this is the only time steward can show it — only its digest is stored:
steward-operator-…

$ steward operator list          # revoked ones are listed too, not hidden
$ steward operator revoke Miha   # takes effect on the next request
```

It reaches exactly what the master token reaches — the session allowlist below is about
*sessions* and is untouched — and what it adds is a name. Writes made with one are
committed by that person rather than by `steward (api)`, jobs are posted by them, and
approvals record them as the decider. Only the SHA-256 digest is stored, there is no HTTP
path that mints or revokes one, and the `steward-operator-` prefix is in steward's
secret-value patterns so a leaked one is refused by validation and scrubbed on egress. See
[docs/api.md](docs/api.md#operator-credentials).

## Status

Ten pieces exist.

**Resident manifests and charters** (#1). Souls and manifests are versioned here and
validated in CI.

**The scheduler and the runner seam** (#2, #11, #21). Routines fire on a cron schedule in
a declared time zone, through one runner abstraction (`claude` / `codex` / a command
template / a mock), with the charter, voice, and journal assembled in one place, and every
run bracketed by real chronicle events. Nothing fires unless `steward scheduler run` is up:
an enabled routine is a declaration, not an animation.

**The resident session lifecycle** (#118). Every real wake-up — scheduled routine,
manual fire, claimed board task, or delegated letter — crosses one `admit` → `run` seam.
It owns the safety-critical provision → context → prompt → runner → completion accounting
→ harvest order. The scheduler still owns occurrences and routine events; the board still
owns claims, leases, and task events; production and mock runners remain interchangeable
at the runner seam.

**Named durable transitions** (#123). Every durable state change and the chronicle fact that
says it happened are coordinated in one place, per domain: posting, claiming, finishing,
failing and lease expiry in `transitions/task.py`; raising, deciding and expiring in
`transitions/approval.py`; accepted handoffs in `transitions/delegation.py`; pause and
resume in `transitions/budget.py`. Callers ask for the domain act and get its durable
result — they no longer interpret a `rowcount`, choose an identity, or decide whether to
emit. The invariant is one sentence: a fact reaches the emitter only after the write
actually won. Refusals, expiries and lost races write nothing and say nothing; the one
deliberate exception, a repeat auto-deny, is named as its own outcome so it cannot happen
anywhere by accident. Persistence and delivery stay two systems. Approval resolution is
the one transition with a durable SQLite outbox: decisions are exactly once, while their
announcements retry at least once and use `request_id` as the consumer idempotency key.
The API lifecycle owns the retry worker; transient failures recover without a client
replay or process restart, and completion effects wait for durable acknowledgement.
The full matrix is `docs/transitions.md`.

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
$ steward scheduler tick --dry-run   # print what is due right now, and the whole prompt
$ steward journal hob         # what Hob has actually written, newest first
$ steward show hob            # the exact preamble Hob's next session opens with
```

`--dry-run` rehearses **this** tick: it prints the routines that are due at this moment
with their assembled prompts, and `nothing due` when none is — which is what a rehearsal
against a state file whose anchors are all fresh should say. It is not a way to dump every
routine's prompt. For a routine that is not due, `steward show <resident>` prints
everything above the task — identity, voice, journal, skills, pending decisions, charter,
assembled by the same module in the same order — and the task section under it is the
routine's own `prompt` from the manifest. `steward doctor` says when each one fires next.

**The HTTP API** (#3). The token-gated write path chronicle's viewer calls directly, so
chronicle's server never gets write access to agents: run a routine now, post a job to the
board, answer an approval, declare a new resident. The contract is acknowledgement, not
effect — an accepted request returns a request id and the word *accepted*, and the work
is confirmed only when the matching protocol event lands in chronicle's log. Tailnet only,
one shared token, documented in [docs/api.md](docs/api.md).

```console
$ STEWARD_TOKEN=… steward serve      # 127.0.0.1:8801 by default; never public
$ steward serve --allow-open         # local dev, with the missing token said out loud
```

**The skills library** (#12). `skills/<name>/SKILL.md` — a named, reusable capability
written as instructions. A documented default set (`write-journal`, `escalate`) is held
by every resident; everything else is granted by name in
a manifest, and a name the library does not have fails validation with the closest match
named. At run time the resident session lifecycle resolves the effective set, injects it into the prompt
under a frame saying a skill is how-to and never authority, and — for runners that take a
copy on disk — writes it into the session's working directory, removing what is no
longer granted. The prompt is the delivery path: a claude session loads no setting sources
([docs/settings-sources.md](docs/settings-sources.md)) and so does not discover the on-disk
copy. The library is shared, so improving a skill improves every resident that holds it.

```console
$ steward skills                     # the library, and each resident's effective set
```

**The job board** (#6). One place work can be dropped for the fleet, instead of prompting
a particular resident. Dispatch is pull-based: a resident that declared `board: {claim:
true}` in its manifest claims the oldest open task whose required skills it holds — its
*effective* set, so a task tagged `research` is claimable only by a resident explicitly
granted that skill — on its own next wake-up, and works it as an ordinary headless session with the
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
answers from chronicle's panel or a notification, and the decision is delivered at the top
of the resident's next session. Two safety properties are the point: **deny by default**
(past `expires_at`, steward resolves the request as `deny` with `decided_by: "expiry"`,
and the gated action never ran) and **first decision wins** (a replay changes nothing and
emits nothing). The grammar and both paths are in [docs/approvals.md](docs/approvals.md).

```console
$ steward approval raise hob --action send_email --detail-json '{"to": "…"}'
$ steward approval show <request_id>  # request, decision, decider, timestamps
```

**Notifications** (warren#114). A village you have to be looking at is no good for a knock,
and neither is a log. A manifest may declare `notifications: {transport: ntfy, on:
[needs_human, task_done]}` — a **one-way** tap, and its own capability dimension rather than
a route kind or a chat channel, because nothing listens for a reply: no session fires and no
answer comes back. Chat (#108) stays conversation-only.

There is no address in the manifest, because there is no address a human should be typing:
an ntfy topic is derived from the resident's `uid` through SHA-256, so it is unguessable in
ntfy's public namespace and cannot drift from the resident it belongs to — and so that
*showing* a uid, which happens in git, in the API and in townhall, never incidentally
discloses the topic. The server and its optional token are environment, never declaration.

A tap's result is discarded and its errors are swallowed: an unreachable ntfy is a log line,
never a failed transition. The POST is synchronous, bounded by a two-second timeout and a
sixty-second circuit breaker — a background thread would make it free and unreliable, and a
one-shot CLI would drop the knock on the way out. A knock the repeat-deny guard answered taps
nobody, exactly as it emits nothing.

Chronicle's own `CHRONICLE_NOTIFY_URL` forwarder still exists and is *not* coordinated with
this: configure both and one knock buzzes twice. Chronicle's is one fleet-wide webhook over
what it ingested; this is one topic per resident over what steward raised, and it also covers
`task_done`. Pick one.

```console
$ steward notify list                 # transport, kinds, and the URL to subscribe to
$ steward notify test hob      # one harmless tap, and whether it landed
```

**Chat** (#108). `routes: {kind: chat}` used to be a description; now it is a doorway.
`steward chat run` polls one Telegram or Discord bot per resident, and every message from a
named operator fires **one ordinary session** — same admission, same budget, same runner
seam, same `routine_started`/`routine_finished` bracket, under the trigger `chat` — whose
final message is redacted, bounded, and sent back into the conversation. Text in, text out.

The route's `address` is a reference (`telegram:pip`) and the bot's token lives in
steward's environment as `STEWARD_CHAT_TOKEN_PIP`, so a manifest can be read, reviewed and
pasted anywhere without that being a disclosure; a token written into one is refused by
validation and scrubbed out of any reply. Only the user ids for that transport in
`STEWARD_CHAT_OPERATORS` are answered, in private chats only — anybody else is dropped
**without a reply**, because a refusal still tells a scanner the bot is live, and the
attempt becomes a `chat_message_dropped` event carrying who knocked and never what they
said — one per sender per door per catch-up window, with the knocks in between counted
into it, so a scanner cannot spend the village's bounded channels on itself. A busy resident is the API's 409 in sentence form: refused with a reason, never
queued. The conversation is a rolling file in the resident's own memory directory, the last
few turns of which are injected as context *beneath* the charter. A dispatch sweep follows
every answered message, so a handoff written mid-conversation is delivered before you have
read the reply.

It is a separate process sharing the scheduler's state directory — which is exactly what
the cross-process session claim (warren#111) is for — and long polling means every
connection is outbound, so nothing on the internet gets a way into the burrow. Notifications
(warren#114) stay the other channel: one-way, nothing listens, no session fires. This bridge
only ever speaks when spoken to. The setup runbook — BotFather or Discord's Developer
Portal, the variables, and the compose service — is [docs/chat.md](docs/chat.md).

```console
$ steward chat list                   # who is reachable, and which variable each bot reads
$ steward chat run --residents residents   # the daemon; nothing arrives unless this is up
```

**Delegation** (#7). A resident can hand work to a neighbour, and steward is the only
arbiter: both manifests have to agree — `delegation: {send: true}` on the sender, an active
route of kind `delegation` on the receiver — and steward enforces what no manifest can see,
a depth cap (default three hops) and a flat refusal of any chain that would revisit a
resident. A session asks with a `<delegate>` block in its output or `steward delegate`;
steward validates, delivers into the receiver's inbox, and emits `task_delegated` naming
both ends, so chronicle can finally show a villager walking to a *specific* neighbour's door.
Delivery is pull-based like everything else: the receiver drains its inbox on its own next
wake-up, ahead of the open board, and works the item as an ordinary session that reads the
letter as a request from a colleague rather than an instruction. Every item records its
parent, its depth, and the origin the chain rolls up to, so what a fleet spent answering
one question attributes to that question. Refusals are structured, write nothing, emit
nothing — and a refused block still knocks at a human's door. The grammar, the guardrails,
and the lineage model are in [docs/delegation.md](docs/delegation.md).

```console
$ steward delegate sender-resident --to hob --route handoff --title "…"
$ steward inbox hob            # what is waiting, from whom, at what depth
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
`routine_started` reached chronicle is found — which it was not while the only thing the
watchdog could read was the log of events chronicle never received.

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
$ steward budget unpause hob  # or approve the knock from chronicle's panel
$ steward watchdog run                # probe, sweep, bury stale runs, check budgets
$ steward watchdog tick               # one pass, then exit (external cron)
```

Delegated work is budgeted work. A letter costs the receiver's day, not the sender's, so
the inbox is gated by the same pause the board is and lands on the ledger under kind
`delegated` — `steward budget show --by-origin` then answers what a fleet spent answering
one question, however many neighbours it went through.

**The nursery** (#4). Raising a resident used to be an SSH ritual: hand-write a soul,
hand-write a compose service, tar it to the NAS, wire `CHRONICLE_TOKEN` and the emitter env by
hand, restart, hope. It is one command now, and three stages — **declare** the soul and
manifest into this repo and commit them, **provision** the container on the NAS, **register**
by checking the scheduler can actually run it and reporting when each routine next fires.

Every stage converges: run it again after a failure and it picks up where it stopped rather
than duplicating anything. The declaration is committed *before* any infrastructure is
touched, so a failed deploy leaves exactly one commit to revert and one command to re-run.
`--dry-run` prints the files, the compose fragment, the exact ssh commands and the next
fires, and provably touches nothing — no commit, no ssh, no scheduler state.

Nothing is emitted on the new resident's behalf, ever. A villager appears in chronicle when it
genuinely exists and emits its own first event, and `steward retire` is the counterpart: it
marks the manifest `retired: true`, commits that, and then stops and removes the container.
A retired resident stops firing routines, claiming board work, taking letters and answering
run-now — and leaves the village the only honest way, by going quiet. The soul and manifest
stay in git; retirement is a lifecycle state, not a deletion.

```console
$ steward new-resident --id note-keeper --name Quill --char Scribe \
    --accent '#4f7ea6' --role 'note bot' --charter charter.yaml --dry-run
$ steward new-resident … --skills write-blog-post   # declare, commit, build, check
$ steward provision note-keeper                 # build the manifest a person wrote
$ steward retire note-keeper                    # stop it, and say so in git
```

`new-resident` describes a resident in flags and so can only ever build one a command line
can describe. `steward provision <id>` is the door for every other resident: it skips
declare and builds `residents/<id>/manifest.yaml` exactly as it stands — routes, app grants,
`runner.placement` and all — which is the only way the fleet's hand-written residents get
onto this path at all (warren#270).

**The control panel** (#13, warren#225). Steward serves no UI. It used to: a browser
console at `/ui`, static HTML and one JavaScript file, no build step. That console was the
fleet's *third* UI and its only browser holder of the master token, so it has been retired
and its every view now lives in [townhall](../townhall) — residents and one resident's whole
record, the new-resident form, the fleet-wide routine ledger, approvals, the job board and
the skills library. Townhall reaches these endpoints same-origin through the NAS's nginx,
with an [operator credential](#operator-credentials) rather than `STEWARD_TOKEN`. No
break-glass was lost: every action it offers has a CLI verb, and ssh plus this CLI is the
real admin hatch — not a static page served by the process being rescued.

Nothing in this repo's build phase is roadmap any more. Chronicle-side rendering
counterparts — the journal panel in a villager's house, the notice board, the letter carried
across the village, the fleet-ops fuel gauges — live in chronicle's issues.

## Deployment

Residents run as docker compose services on the NAS (`dxp2800`), over Tailscale, beside
chronicle's own server at `~/docker/warren/chronicle`. **Steward puts them there.** This section
replaces the manual ritual in chronicle's README for anything that is a resident; the
event server itself is still deployed by hand, now from
[`warren/chronicle/`](../chronicle/README.md) rather than from a repo of its own.

**On the burrow, the control plane provisions its own residents.** A resident whose
`deploy.host` is the burrow the API runs on (`STEWARD_BURROW`) is written into
`~/docker/warren/residents/<id>` through a mount and brought up through the docker socket,
in the API's own process — `steward.deploy.BurrowTransport`. That is what makes townhall's
New resident form and its provision button real on the NAS: the API cannot ssh to the
machine it is already on. From a laptop the same manifests still deploy over ssh.

Run every command below from `warren/steward/`, on a machine with the repo checked out and
ssh access to the NAS. Nothing pulls on the NAS — it has no git of its own; steward pushes
the runtime bundle over ssh, and the deploy directories there are unpacked artifacts. The
one exception is the control plane's **residents checkout** (warren#351): a sparse clone
of this repository under `~/docker/warren/steward/residents-repo`, made once by
`deploy/deploy.sh` through the control-plane image's own git, which the deployed API
reads, writes, commits into and pushes to the branch `burrow/residents`. That checkout
is authoritative for the burrow's residents; `residents/` here is the seed a new burrow
is cloned from. [`deploy/README.md`](../deploy/README.md#the-residents-checkout) has the
whole story and the one-time key setup.

### Where the daemons run

**`steward scheduler run` and `steward watchdog run` run on the burrow whose containers
they supervise** — today, the NAS. The commands above are the exception, not the rule:
declaring and provisioning a resident is multi-host by design (`deploy.host` per manifest,
tar over ssh), and is meant to be run from a laptop. *Supervising* one is not. Both daemons
reach containers by shelling out to a **local** `docker` client — `docker inspect` and
`docker restart` for the watchdog, `docker exec` for a container-placed session — and none
of those calls has ever looked at `deploy.host`.

The failure this prevents is a quiet one, and it is the **watchdog's** half that is quiet.
A watchdog on the wrong machine asks a docker that has never heard of `hob`, gets
nothing, and reports the resident as *unsupervised* — honest about what it could see, and
indistinguishable from a resident that has no container at all. It keeps burying stale runs
and tripping budgets the whole time, so nothing looks wrong. (The scheduler's half already
fails loudly: `check_runner` probes the container for a `placement: container` resident, so
a startup on the wrong machine is a refusal, not a silence.)

So `steward doctor` and `steward watchdog` both print a topology report naming any container
this process cannot reach — the watchdog at startup, before its first pass; doctor with the
other fleet-wide lines, below the per-resident block:

```console
$ steward doctor
…
hob: inbox 0 open via handoff
topology: docker at dxp2800's own docker answers as dxp2800 27.3.1
hob: container hob on dxp2800 — supervised from here
watchdog: last pass …
```

Doctor warns and still exits 0 (it is routinely run from a laptop while the daemons are on
the NAS); the watchdog says it in red, because that process *is* the supervisor.
`STEWARD_BURROW` names this burrow when the machine's hostname is not what manifests call
it — though what `docker info` says about itself is consulted first, and settles it alone
when it matches.

`steward chat run` is the third daemon, and it sits under the same rule for a narrower
reason: it supervises nothing and needs no docker of its own, but it *fires sessions*, so a
chat route on a container-placed resident puts it exactly where the other two are. It also
shares their state directory and their one `steward.db`, which is what makes a message
arriving mid-routine find the resident busy instead of opening a second session.

`DOCKER_HOST` is inherited by every docker call steward makes (measured, unlike a
session's environment), so docker's own remote-endpoint support applies to supervision. It
does **not** relocate execution — a container-placed session also needs the host side of
its memory mount on the control plane's own filesystem, which `workdir_refusal` requires.
The rule, the measurements, what is *not* measured, and the case for teaching supervision
ssh later are in [docs/topology.md](docs/topology.md).

```console
$ export CHRONICLE_URL=http://dxp2800:8737    # arcadia's origin, which proxies /events to
                                           # chronicle on 8738 — not a stale port
$ export CHRONICLE_TOKEN=…                    # the village's shared ingest secret

$ steward new-resident --id note-keeper --name Quill --char Scribe \
    --accent '#4f7ea6' --role 'note bot' --charter charter.yaml --dry-run   # read the plan first
$ steward new-resident … --skills write-blog-post   # declare, commit, build, check
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
optional soul fields; `--residents` names the tree to declare into (default `residents`,
relative to the current directory) and `--repo` the checkout to commit into (default: the
parent of that tree); `--dry-run`, `--allow-dirty`, and `--no-commit` behave as they do for
`retire`; and **`--no-deploy`** is the only host-less path — it declares and checks but
builds no container, for developing a resident before it has a machine. `--skills` is a
grant *on top of* the default set every resident already gets, so naming a default skill
(`write-journal`, `escalate`) is redundant rather than
additive — grant only what a resident holds beyond the defaults. It is not an error, and
steward does not silently drop it: the effective set is the same either way, and the
nursery warns by name so the line can be deleted from a manifest that means nothing by it.

The three stages, and what each one really does:

**Declare.** `residents/<id>/manifest.yaml` and `residents/<id>/soul.md` are written, read
back through the ordinary validator, and committed — before anything else happens, because
the repo is the source of truth and a failed deploy should leave one commit to revert and
one command to re-run. A dirty worktree is refused unless `--allow-dirty` says out loud
that you want it anyway.

**Provision.** The compose fragment is rendered from `steward/templates/`, the runtime
bundle is packed into a tar **in memory**, and the whole thing is piped over
`ssh Miha@dxp2800 tar -xf - -C ~/docker/warren/residents/<id>` — a pipe rather than `scp`, because
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
`~/docker/warren/residents/<id>`, `steward-resident:latest`); override any of them in
[`deploy`](docs/manifest.md#deploy--where-this-resident-runs).

**What a resident's container actually is.** `steward-resident` is built from
[`docker/resident/Dockerfile`](docker/resident/Dockerfile): `node:22-slim` **pinned by
digest**, the `claude` CLI pinned by build arg, python3, and a vendored copy of chronicle's
emitter — built from `chronicle/hooks/` into one stdlib-only file — wired into the same six
claude hooks the Mac uses. Every input is
pinned so that two builds of one commit are one image, and the image says which commit that
was — `org.opencontainers.image.revision`, stamped by `make image` from `git rev-parse
HEAD`. Moving the base digest is its own deliberate commit; the Dockerfile carries the
`docker buildx imagetools inspect` line that produces the new one. `make vendor-emitter`
rebuilds the emitter copy from `../chronicle` — in-tree since the consolidation, so it
needs no second checkout and no `CHRONICLE=` argument — and the suite rebuilds it again and
compares byte for byte, so the copy cannot quietly fall behind its source. CI builds this image on every PR that
touches steward, runs the entrypoint and runs `steward-smoke` against a stub village that
only knows how to answer 204. There is no registry here, so the image travels like
everything else does — a pipe over ssh:

```console
$ make image                                          # linux/amd64, for the NAS
$ make image-ship                                     # docker save | ssh dxp2800 docker load
$ ssh Miha@dxp2800 docker exec steward-<id> steward-smoke
smoke: ok   claude 2.1.243 (Claude Code)
smoke: ok   POST http://dxp2800:8737/events -> 204
smoke: PASS this container can reach the village
```

`steward-smoke` runs inside the container and is [issue
#51](https://github.com/0xCommanderKeen/warren/issues/51)'s acceptance criterion made
executable. The container runs `sleep infinity` because it is a *place for sessions to
happen* rather than a process doing work: steward drives the brain from outside. **Whether
a session happens in there is `runner.placement`** (steward #58) — `local`, the default,
runs it in the process running `steward scheduler run`; `container` runs it as a
`docker exec` into this container. Either way the docker client is the *local* one, which
is why [the daemons run on the burrow](#where-the-daemons-run).

**Secrets never enter this repo.** `CHRONICLE_URL` and `CHRONICLE_TOKEN` are read from steward's
own environment at provision time and written into a `.env` on the host at mode `0600`.
The compose file carries `${CHRONICLE_TOKEN-}` — a reference, not a value. The repo's own
credential scanners are run over everything the nursery writes into the checkout, as a
test. Provisioning without `CHRONICLE_URL` is refused: a container with nowhere to emit is a
resident that would never appear in the village at all.

**The other door.** `new-resident` describes a resident in flags, and refuses to converge
those flags onto a manifest a person has since edited: silently overwriting a soul somebody
wrote is not something a command line should be able to do. That refusal is right, and on
its own it was a dead end. There are no flags for `routes`, for `app_grants`, for a skill's
`note` or for `runner.placement`, so a manifest carrying any of them could never match a
spec built from flags — which left the fleet's oldest and most carefully written residents
with no supported way onto the nursery path at all (warren#270).

`steward provision` is the way in for a manifest somebody wrote:

```console
$ steward provision hob --dry-run    # the same plan, from the declaration itself
$ steward provision hob
```

The declare stage is already done — by a person, in a file, in a commit — so this reads
`residents/<id>/manifest.yaml` as the source of truth and runs provision and register
against it, exactly as it stands. It is `retire`'s counterpart: same argument, same source
of truth, opposite direction.

It writes nothing into the repo, which is why it has no `--commit`, no `--allow-dirty`, and
no dirty-worktree refusal: there is no commit here for a failed deploy to leave behind, so
there is nothing for that refusal to protect, and somebody else's half-finished afternoon
is none of this command's business. What it will not do is ship in silence — a declaration
whose *own* bytes are in no commit is named in a warning, because a container built from
bytes nobody committed is a container nobody can turn back into a diff.

The flags it does have are `--residents`, `--repo`, `--dry-run` and `--format`, and each
means what it means for the other two commands.

**Retiring** is the counterpart, and it goes the other way round on purpose:

```console
$ steward retire note-keeper --dry-run
$ steward retire note-keeper
```

The manifest is marked `retired: true` and committed **first**, then the container is
stopped and removed — because the watchdog would otherwise notice the container go away
and dutifully put it back. A retired resident fires no routines, claims nothing off the
board, receives no letters, and answers `409 resident_retired` to run-now. After the mark is
committed, steward emits the authoritative `resident_retired` lifecycle fact under the
resident's declared identity; it never forges a `session_ended` on the resident's behalf.
The soul and the manifest stay in git.

`steward retire --no-deploy` marks and commits the manifest but reaches no host — the
counterpart to `new-resident --no-deploy`, for a resident whose host is already gone or was
never steward's to stop. It stops taking work the moment the mark is committed either way.
The other flags mirror the two commands: `--dry-run` touches nothing, `--no-commit` writes
the mark without committing it, `--allow-dirty` commits over a dirty worktree, and `--repo`
names the checkout when it is not the parent of the residents tree.

Townhall reaches the same pipelines over HTTP: `POST /residents` with `deploy: true`
([docs/api.md](docs/api.md#post-residents)) is `new-resident`,
`POST /residents/{id}/provision` ([docs/api.md](docs/api.md#post-residentsidprovision)) is
`provision`, and `POST /residents/{id}/retire`
([docs/api.md](docs/api.md#post-residentsidretire)) is this command — one implementation
each, two front doors each, verified by injecting the pipeline into the route rather than by
a convention somebody has to keep. The route takes the whole act or refuses: the
break-glass flags above stay at the terminal, because each of them leaves the retirement
half done in a way only the person who typed it can see.

The HTTP route is confirmation-bound: Townhall rehearses first, then sends the returned
manifest revision with the real request. A missing revision or bytes changed since rehearsal
are named refusals. The checkout lock covers that check, target-manifest dirt inspection,
mark and commit, and is released before Chronicle or host I/O.

## Residents

```
residents/
  hob/       manifest.yaml + soul.md   Hob, the household spirit
  pip/              manifest.yaml + soul.md   Pip, the pipeline canary
skills/
  <name>/SKILL.md                             the shared library both draw on
```

Each manifest declares the resident's soul identity, charter (mission, duties, hard
rules, escalation policy), and the capability dimensions chronicle renders — skills,
memory, routes, app grants, and the tools a session may reach — plus the runner steward
launches sessions through, the
routines it fires, whether the resident takes work off the job board, and whether it may
hand work to another resident. Its `skills`
are grants by name against the shared library — what this resident holds on top of the
default set every resident gets. References and grants only: a credential-shaped key or
an inline secret, in a manifest or in a `SKILL.md`, fails validation and is never stored.

The schema is documented in [docs/manifest.md](docs/manifest.md), and
`steward schema` emits it as JSON Schema so chronicle can read manifests without
translation. The generated copy is committed at
[schema/resident-manifest-v0.json](schema/resident-manifest-v0.json) — the path the
schema's own `$id` promises — and a test fails when it drifts from the models, so a
manifest change that would break chronicle's reader shows up as a diff in the pull request
that makes it. Regenerate with `make schema-write` and read the diff.

```console
$ steward validate                         # the whole residents/ tree
ok: 3 valid resident(s), 0 error(s), 0 warning(s) in residents

$ steward validate residents/hob    # or one resident, or one file
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

A routine only ever fires while `steward scheduler run` is up — and only one of them, per
state file: a second daemon refuses to start and names the pid already holding the lock,
while a cron `steward scheduler tick` beside a running daemon simply takes its turn and
finds nothing due. Missed schedules are not back-filled, an overlapping fire is skipped
rather than queued — and *overlapping* now means across processes too: one resident has one
live session, whether the scheduler daemon, a run-now over the API or a board dispatch
started it, and whoever asks second is told who has it. A run killed at its timeout is
emitted as `routine_failed` — the
village must never show work that is not happening. The same rule governs memory: a day
with no journal entry has no journal entry, and the next session is told nothing rather
than something plausible. And the same rule governs the board and the door: a task nobody
finished goes back to `open` loudly, and a request nobody answered is a `deny`, never a
quiet yes. A restart is announced, a run that never reported back is buried out loud, and
a resident that has spent its day stops and says which number stopped it.

### Driving a session offline

Three of steward's most consequential paths only open when a session actually *says*
something — an escalation, a handoff, a bill. Each has an offline way in, and none of them
needs a model, a network, or a token. `runner.kind: mock` is not one of them: it is
deterministic and reports no usage on purpose, its injectable `behavior` is a Python
callable for tests with no manifest or CLI surface (a manifest field that made a resident
fail or block on demand would be a production footgun), and `--dry-run` forces it precisely
so a rehearsal cannot reach a brain.

**A session that asks for a human, or hands work to a neighbour.** Declare a `command`
runner pointed at a script, and print the control region. This is the whole mechanism —
steward reads the child's stdout, and the child is whatever the manifest names:

```yaml
runner:
  kind: command
  command: [/path/to/session.sh, "{prompt}"]
```

```sh
#!/bin/sh
echo "Working on it."
echo "===STEWARD-ACTIONS==="
echo '<needs-human action="send_email" expires-in="4h">'
echo '{"to": "anna@example.com", "subject": "Re: Thursday"}'
echo '</needs-human>'
echo "===END-STEWARD-ACTIONS==="
```

A tick against that resident emits `routine_started`, `needs_human` and
`routine_finished`, and leaves a real request for `steward approval show` to read. Swap the
block for `<delegate>` ([docs/delegation.md](docs/delegation.md)) to exercise the inbox the
same way.

**A session that costs money.** Cost is not steward's to invent: only the `claude` runner
learns it, by parsing the CLI's own `--output-format json`, and a `codex` or `command` run
is recorded honestly as *usage unknown*. So the offline costed session is a `claude` runner
whose `claude` is a stub first on `PATH` — the same trick the test suite's `stub_bin`
fixture uses:

```sh
#!/bin/sh
case "$1" in --help) echo "--setting-sources --settings --tools --strict-mcp-config --add-dir"; exit 0 ;; esac
echo '{"result": "Done.", "is_error": false, "total_cost_usd": 0.42, "usage": {"input_tokens": 1200, "output_tokens": 300}}'
```

The `--help` arm is what keeps `steward doctor` green: doctor asks the installed binary
what flags it supports, because every claude session carries `--setting-sources` whether a
manifest asked for it or not. (The scheduler's own startup check is cheaper — it only asks
whether the binary answers where sessions run, which for a container-placed resident is
docker rather than this `PATH`.) With `budgets: {daily_cost_usd: 0.25}` on the resident,
one tick ledgers $0.42, trips the cap, pauses the resident and knocks — the whole budget
path, without a model call. The `result` string is the session's output, so a stub that
puts the control region in there exercises both halves at once.

## Environment

Steward reads a handful of environment variables. None is required for `steward validate`;
the scheduler and the API name the ones they need on startup.

| variable | who reads it | meaning |
|---|---|---|
| `STEWARD_STATE` | scheduler | Path to the scheduler's state **file** (its last-fire anchors), not a directory — a `STEWARD_STATE` that names a directory is fatal, because a scheduler that cannot persist an anchor re-fires forever. `steward.db` lands **beside** it, so point this at e.g. `~/.steward/state.json` and the database is `~/.steward/steward.db`. Unset, the file is `.steward/state/scheduler.json` under the current directory — which moves with the current directory, so a daemon wants this set. |
| `STEWARD_TOKEN` | API | The bearer token every endpoint requires. Unset or blank refuses to start unless `--allow-open` says out loud this is loopback-only local development. |
| `STEWARD_EVENTS_FALLBACK` | everything that emits | Steward's complete local event record, read by the watchdog. Remote-bound events wait for acknowledgement in its `.pending` sibling. Defaults to `~/.chronicle/events.jsonl`. |
| `STEWARD_CORS_ORIGINS` | API | Comma-separated origins allowed to call the API from a browser. Unset means same-origin only. |
| `STEWARD_RESIDENTS` | API | The residents tree `create_app()` reads when nothing names one. `steward serve` always passes `--residents` (default `residents`), so on that path the flag decides and this is only read by an embedder that builds the app itself. |
| `STEWARD_COMMIT_IDENTITY` | API | `Name <email>`, the git author the write API commits as when the caller is not a named [operator](#operator-credentials). Anything that is not that exact spelling is ignored rather than half-parsed, leaving commits reading `steward (api)` — which is true. |
| `STEWARD_ALLOW_UNCOMMITTED_WRITES` | API | `1`/`true`/`yes`/`on` accepts a residents tree with no git behind it. Off, a write into a tree outside a checkout is refused `409 not_a_git_checkout` rather than leaving declarations with no history and no author. Never set this on a burrow whose tree lives in the image: the writes would land in the container layer and die on the next deploy (warren#313, warren#351). |
| `STEWARD_PUSH_BRANCH` | API | The branch every commit the write API makes is pushed to afterwards — `burrow/residents` on the NAS. Unset, nothing is pushed (a laptop's checkout, where the person pushes). The push is best effort and never fails a write: the response carries `commit.pushed` — `true`, `false` with the reason in `commit.note`, or `null` when there was nothing to push. |
| `STEWARD_PUSH_REMOTE` | API | The remote that branch is on. Defaults to `origin`, which is what a `git clone` calls it; read only when `STEWARD_PUSH_BRANCH` is set. |
| `STEWARD_MAX_DELEGATION_DEPTH` | delegation | How deep a chain of delegated work may run before steward refuses (default 3). `0` is the fleet-wide kill switch. |
| `STEWARD_REPEAT_DENY_WINDOW_H` | approvals | Whole hours a `deny` goes on answering the same `(resident, action)` a session raises, so a looping resident cannot knock on every wake-up (default 12). `0` is the kill switch: every repeat knocks again. Anything that is not a whole number ≥ 0 is logged as a misconfiguration and the default is used. Steward's own knocks are never guarded — see [docs/approvals.md](docs/approvals.md). |
| `STEWARD_NOTIFY_DISCORD_WEBHOOK` | notifications | Discord fleet webhook for residents declaring `notifications.transport: discord`. One-way only; each payload uses the resident's soul name as its username. |
| `STEWARD_NTFY_URL` | notifications | The ntfy server outbound taps are POSTed to. Defaults to `https://ntfy.sh`, which is safe because the topic is derived and unguessable — point it at a self-hosted instance to keep even that off the public internet. |
| `STEWARD_NTFY_TOKEN` | notifications | Bearer token for a protected ntfy instance. Optional, and never in a manifest. |
| `STEWARD_NTFY_TIMEOUT_S` | notifications | How long one tap may take (default 2s). A tap is a courtesy; it may not cost a run. |
| `STEWARD_NOTIFY_NAMESPACE` | notifications | Folded into every derived topic. Empty by default; set it on a second installation reading the same `residents/` tree, so a developer's test knock does not buzz the operator's real phone. |
| `STEWARD_CHAT_OPERATORS` | chat bridge | Comma-separated `<transport>:<user-id>` identities steward answers; bare ids remain Telegram-compatible. Empty means nobody, and `steward chat run` refuses to start rather than run as an open door. A message from anyone else is dropped without a reply. |
| `STEWARD_CHAT_TOKEN_<REF>` / `STEWARD_CHAT_TOKEN_<TRANSPORT>_<REF>` | chat bridge | Telegram keeps the v0 name (`telegram:pip` reads `STEWARD_CHAT_TOKEN_PIP`); other transports include their name (`discord:pip` reads `STEWARD_CHAT_TOKEN_DISCORD_PIP`), with non-alphanumerics folded to `_`. One bot per route, and never in a manifest: a token written into one is refused by validation. |
| `STEWARD_CHAT_API_URL` | chat bridge | Where the bot API lives. Defaults to `https://api.telegram.org`; the test suite points it at loopback so nothing in this repo can reach the real service. |
| `STEWARD_CHAT_DISCORD_API_URL` | chat bridge | Discord REST base URL. Defaults to `https://discord.com/api/v10`; tests point it at loopback. |
| `STEWARD_CHAT_DISCORD_GUILD` | session harvest | Discord guild id used to resolve allowlisted `posts_to` channel names. |
| `STEWARD_CHAT_POLL_TIMEOUT_S` | chat bridge | How long one `getUpdates` may wait for a message (default 25s). The socket timeout is this plus ten seconds, because the server holds the connection for the whole poll by design. |
| `CHRONICLE_URL` | emitter, nursery | The village's ingest URL. Provisioning a resident without it is refused: a container with nowhere to emit would never appear in the village. The pre-rename `BURROW_URL` is no longer read (warren#361): an environment that still spells it the old way is refused rather than half-configured. |
| `CHRONICLE_TOKEN` | emitter, nursery | The village's shared ingest secret, written into the resident's host `.env` at provision time and never into this repo. `BURROW_TOKEN` is no longer read (warren#361). |
| `STEWARD_SESSION_EMITTER` | runners | Path to chronicle's emitter script on **this host**, for locally placed sessions. Set it and a local session carries steward's six chronicle hooks (`--settings`); unset — the default — it carries none and emits no per-session events. Container placement ignores it: that emitter is baked in the image steward builds. |
| `STEWARD_SESSION_ENV_PASSTHROUGH` | runners | Comma-separated extra variable **names** a locally placed session may inherit, on top of the allowlist below (a container-placed session inherits neither — its compose `.env` is the hatch there). `STEWARD_TOKEN` and `STEWARD_SESSION_TOKEN` are refused however they are spelled, and the refusal is logged. |
| `STEWARD_BURROW` | doctor, watchdog | What this machine is called when its hostname is not the name manifests use for it (`deploy.host`). Read only to *report* whether supervision reaches a container; never to decide where anything runs. A declaration replaces the hostname rather than joining it, and `docker info`'s own answer outranks both. See [docs/topology.md](docs/topology.md). |
| `DOCKER_HOST` | docker, so: watchdog + container placement | Docker's own pointer. Steward never sets it and never reads it to decide anything — but every docker call steward makes inherits it (measured), so docker's own remote-endpoint support applies to *supervision*. It does not relocate *execution*: a container-placed session also needs the host side of its memory mount on this filesystem. Reported as **unverified** unless the daemon at the far end names itself as the declared host — that answer is measured; nothing else here can prove where the endpoint lands. |

Most take a matching CLI flag where a command needs one — `--state`, `--db`, `--host`,
`--allow-open`, `--residents` — and the flag wins over the variable.

### What a session inherits

**A session does not get the machine's settings either.** Every `claude` session is
launched with `--setting-sources ""`, so none of `~/.claude/settings.json`,
`<workdir>/.claude/settings.json` or its `.local` sibling is read: a settings file at any
of those registers hooks that run and sets the permission mode, none of it gated by the
workspace trust flag that used to be the only thing in the way, and the working directory
is the resident's own memory directory. The measurement, and what closing the channel
costs, are in [docs/settings-sources.md](docs/settings-sources.md).

**It gets steward's own six hooks instead.** Beside the closed sources, every claude session
that has an emitter to name carries `--settings '{"hooks": …}'` — chronicle's six per-session
events, declared by steward on argv rather than inherited from anybody's file, so the village
sees what a session did and not only that it ran. A container-placed session always has one
(the emitter is baked in its image); a local one has it where `$STEWARD_SESSION_EMITTER`
names a script, and is quiet otherwise. `steward doctor` prints which, per resident. See
[`docs/manifest.md`](docs/manifest.md#what-steward-declares-instead-six-hooks-and-nothing-else).

**A session does not get steward's environment.** A locally placed session gets an
allowlist (`SESSION_ENV_BASE` in `runners.py`) plus the facts steward deliberately hands
it, and nothing else. The allowlist is the shape of the machine (`PATH`, `HOME`, locale,
proxy and CA settings), steward's own *configuration* (`STEWARD_STATE`,
`STEWARD_MAX_DELEGATION_DEPTH`, `CHRONICLE_URL`, …) so a session's own `steward delegate`
opens the same database under the same caps, and the brain's own provider credentials
(`ANTHROPIC_API_KEY` for a `claude` runner, `OPENAI_API_KEY` for a `codex` one).

A **container-placed** session (`runner.placement: container`, steward #58) gets less
still: only the per-wake facts, passed by name over `docker exec -e` — no allowlist and
no `STEWARD_SESSION_ENV_PASSTHROUGH`, because the container already carries its own
environment from its compose file and `.env` (the operator's hatch there), and the
brain's credentials live on its `/root/.claude` volume. See
[`placement`](docs/manifest.md#placement--where-a-session-runs).

Two names are deliberately missing, and neither is an oversight:

- **`STEWARD_TOKEN`** is the master key into steward's own API — the same secret that
  decides approvals and delegates as any resident. `steward approval raise` and
  `steward delegate` exist precisely so a session never needs it, and until steward #41
  every locally launched session was carrying it anyway.
- **`CHRONICLE_TOKEN`** is one shared ingest secret whose holder can post events as any
  `agent_id`. What a *locally placed* session loses without it depends on the village: where
  chronicle's own ingest token is unset its ingest is open and the session's hook events land
  anyway; where it is set they are rejected, and the hook emitter journals them to
  `~/.chronicle/events.jsonl` — its own outbox, **not** the `events.jsonl.pending` that
  `steward events flush` drains, which is steward's own emitter's queue. So they are not lost
  and they do not arrive. Naming it in `STEWARD_SESSION_ENV_PASSTHROUGH` buys live emission
  at the price of handing every session a secret that can impersonate every other resident;
  per-resident ingest credentials are the real answer and are their own issue. A
  container-placed session needs none of this — `docker exec` runs it in the container's own
  environment, which its compose service already gives both variables.

One name is deliberately added: **`STEWARD_SESSION_TOKEN`**, this run's own scoped
credential — see [the API's two kinds of caller](docs/api.md#two-kinds-of-caller).

Remote-bound events enter a durable queue before POST. Each retry keeps one
`X-Burrow-Delivery-ID`, so a crash after Chronicle accepts an event but before Steward
retires it is deduplicated by current Chronicle servers. Normal emits replay up to 16 older
events, oldest first; operators can drain and inspect the queue explicitly:

```console
$ steward events flush
delivered 7; retired-records 7; pending 0; corrupt 0; foreign-target 0; queue /home/me/.chronicle/events.jsonl.pending
```

The pre-queue `events.jsonl` format recorded every event but no delivery outcome. It is
therefore impossible to infer which historical ID-less lines need replay. `steward events
flush --include-legacy` explicitly queues each distinct valid ID-less line with a stable
ID. Repeating it while that ID is pending is a no-op. Once delivery retires the queue
record, however, there is no durable legacy seen-set, so a later import can offer the same
stable ID again; use the option only when possible duplicates of events originally
delivered without IDs are acceptable. Legacy IDs hash canonical event content together
with the normalized target URL, so JSON formatting changes keep the same ID while the
same event sent to another village gets a different ID. Compatible old-ID records for
one target and canonical event are sent as one POST and retired together: `delivered`
and `--limit` count POST groups, while `retired-records` counts physical queue rows.
History lines carrying a valid
`steward_delivery_id` are modern and are skipped: their queue record already owns retry,
or its acknowledged delivery was retired. Invalid or torn queue records are preserved in
`.pending.corrupt`, reported, and make the command exit non-zero. A failed POST likewise
leaves the suffix pending and exits non-zero.
Records bound to a different historical `CHRONICLE_URL` are not leaked to the current
target; they remain pending, are counted as `foreign-target`, and also exit non-zero.
