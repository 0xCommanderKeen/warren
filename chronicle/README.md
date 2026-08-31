# chronicle

The backend behind a living village that shows what your AI agents are actually doing.

Each real agent — the one summarizing your day, reviewing your code, reading your email, researching, or supervising the others — is a villager with a house. When an agent works, its villager works. When it's idle, it rests. When it needs you, it walks to your door and knocks.

**chronicle is not a game and not a simulation.** It is an ambient interface to a real agent fleet. The village is a projection of live events; it never invents behavior.

**chronicle runs no browser code.** It is the event protocol, the log, the projection and the
HTTP API, in Python. Every UI is a separate client consuming the versioned state contract:
**Arcadia** renders the pixel-art village, **Townhall** is the control panel. Requests for
paths this repository used to serve a viewer on now 404 — see
[docs/ui-clients.md](docs/ui-clients.md).

## The one rule

**The village never lies.** A sprite doing something means the agent behind it is genuinely doing that right now. No ambient filler animation, no faked liveliness. The shape of the village *is* the state of the fleet:

- Nobody moving → nothing is running.
- A villager at your door → something needs your attention.

## Architecture (four layers)

1. **The fleet** — real agents, running wherever they run. chronicle does not own them.
2. **Event protocol** — agents emit structured events (`task_started`, `tool_called`, `artifact_produced`, `needs_human`, `idle`). This is the core of the project; everything else consumes it.
3. **Projection** — maps events to village state (agent started reading inbox → villager walks to the post office).
4. **Clients** — pixel-art renderers and control panels, each its own project. They read
   complete snapshots from `/state` and `/state/stream` and render them; they never fold raw
   events. chronicle ships none of them.

## What one snapshot carries

Everything a client shows is already reduced here. A `VillageState` is complete on its
own: no client folds the raw log to get a field, and none of these arrays is a view a
client assembles for itself. Alongside `villagers` — identity, residency, home or lodge,
current state, place, the retained `history` tail, `mood`, `pending_approval_ids` — a
snapshot carries:

- **`artifacts`** — the newest 30 `artifact_produced` records, each with its path, maker,
  project and time. Empty means the *retained log* holds no artifacts, which is not the
  same claim as the fleet having produced none.
- **`tasks`** — Steward's current job queue, folded from `task_posted`, `task_claimed`,
  `task_done` and `task_failed` by task ID, so a job's lifecycle survives being posted in
  one session and claimed in another. A `task_failed` whose reason is `lease_expired`
  reopens the job rather than failing it — matching Steward's own queue — and keeps naming
  the attempt that expired. A blank skill name in an otherwise valid event stays a blank
  name; that is not the same fact as an empty requirement list.
- **`approvals`** — one record per `needs_human` carrying a `request_id`: the action, the
  semantic detail, Steward's approve/deny/edit options, the declared expiry, and the
  decision once a matching `needs_human_resolved` arrives. The question is immutable — a
  request ID reused with a different question is quarantined as a `collision` instead of
  overwritten, and a resolution that does not match on agent, project and action is a
  diagnostic rather than a decision. Nothing here evaluates the expiry: `expires_at` is
  carried through as declared, and deciding what an elapsed one means is the client's.
- **`journals`** — journal *metadata* observed from Steward's `journal_written`: day,
  agent, project, routine, path, observed time. Never the entry text. First valid append
  owns each `(agent_id, day)`, and the highest 40 keys survive log rotation with one
  representative collision diagnosed per retained key.
- **`routines`** — scheduled runs and their outcome, duration and artifacts.
- **`residents`** and **`diagnostic_residents`** — the manifest fleet that validated, and
  actionable diagnostics for the declarations that did not. Both publish only the
  allow-listed display and capability metadata from the
  [resident-manifest guide](docs/resident-manifest.md): never raw manifest objects, never
  app credentials.
- **`diagnostics`**, **`capacity`** and **`capabilities`** — malformed records, the bounds
  this server actually applied, and which event families this build projects (`ingest`,
  `approvals`, `jobs`, `routines`), so a client can tell an older backend from a feature
  the fleet is simply not using.

Every villager also carries a **`mood`**: one deterministic operational reading built from
retained failures, work density, exact human interactions and unresolved needs — not
personality, not sentiment. It is computed here, by the single reducer in
`village_state.py`, from validated events in append order, and every duration in it is
anchored to the newest retained log timestamp rather than to a clock. A client that sits
idle overnight therefore renders the same reading it was given, and events for one agent
can never age another's. The exact signals, thresholds and precedence are in
[the protocol](docs/protocol.md#operational-mood-glyph).

Be careful what you read into the resident arrays, because they assert less than a control
panel usually wants. A valid manifest means a complete, safe *declaration* exists — not
that this service tested a credential or reached anything. A `status_ref` is validated as a
reference and echoed; nothing here ever fetches it, so its text is never evidence of live
health. An incomplete declaration is published as a diagnostic rather than as an absence,
and a missing manifest directory reads as an empty fleet — there is no "the feed is
unavailable" signal to give, so a client showing one is reporting its own read, not the
projection's. Matching is deliberate too: exact agent identity wins before a project
fallback, the project fallback applies only when exactly one manifest claims that project,
and a Claude or Codex child carrying `parent_agent_id` never inherits its parent's home.

Bounds are published rather than implied: `capacity` reports the retention this server
applied to villagers, per-villager history, tasks, approvals, journals, routines and
diagnostics. Full arrays keep the newest records, so a finished job leaves `tasks` because
newer ones pushed it out, not because a timer retired it.

## It observes writes; it never performs them

Every write in the fleet — posting a job, deciding an approval, declaring a resident,
running a routine — is a client talking to Steward with a credential a human typed at
runtime. chronicle has no outbound Steward client, proxies nothing to it, and holds no
credentials of its own. A write reaches this service only later, as the event that write
produced.

That is what keeps the one rule enforceable across a write, and it is why both clients are
built to distrust their own receipts. A job enters `tasks` when its `task_posted` event
arrives, not when the POST was accepted — a claim or a completion for a job this log never
saw posted is dropped rather than invented. A villager stops `knocking` on
`needs_human_resolved`, not on a `200`, and one whose session already ended stays at your
door for exactly as long as its approval is still open.

Which refusals are safe to retry, how an ambiguous delivery is reconciled later, and where
the operator credential lives are each that client's business, and documented with it:
[arcadia](../arcadia/README.md) is the pixel village — the notice board, the job board, the
knock at the door — and [townhall](../townhall/README.md) is the governance console and the
full write surface. Resident journal text, inboxes and spend come from Steward directly,
read by the client that needs them: the projection carries journal *metadata* and nothing
of the other two.

## Running

### Canonical operator path

There is one supported setup path for both Claude Code and Codex:

1. Install the shared emitter with `sh scripts/install-emitter.sh`.
2. Configure Claude Code or Codex using the copyable hook definitions in the
   [protocol guide](docs/protocol.md#runner-setup). Both runners invoke the same
   installed bundle; Codex adds `--runner codex`.
3. Add or edit `villagers/*.resident.json`, then validate every manifest with
   `python3 -m unittest tests.test_residents`. App-grant references and status
   references describe public configuration/health locations only. Chronicle never
   stores app credentials; those stay in the owning app or secret store.
4. Run the authoritative test suite with `sh tests/run.sh`.
5. Deploy the exact tested tree with the tar-over-SSH command below and restart
   the service. Check `/residents` for public validation diagnostics and open the
   village at both phone and desktop widths.

The detailed protocol sections explain event mappings and privacy, but the steps
above are the canonical install, validation, test, and deployment sequence.

One village for the whole fleet, served from the NAS over Tailscale. Since the
2026-08-27 cutover arcadia owns the origin on port 8737 and this service answers
on **host port 8738** (<http://dxp2800:8738>), proxied same-origin under
`http://dxp2800:8737/burrow/`. Never exposed to the public internet — the event
log is a map of everything the fleet does.

**Run every command below from `warren/chronicle/`.** Since the 2026-08-31
consolidation this service is a directory in the warren monorepo
(<https://github.com/0xCommanderKeen/warren>), not its own checkout; the archived
`burrow` repo is not what gets deployed. The tar recipe packs paths relative to
the working directory, so running it one level up silently bundles the wrong tree.

The NAS has no git installed and holds no clone: `~/docker/burrow/app` is an
unpacked copy of the tree the tar below carried. Nothing there pulls — every
deploy is pushed from a machine that has the repo checked out.

> **The `BURROW_*` spellings below still work.** Every setting here is read under
> both its `CHRONICLE_*` name and its pre-rename `BURROW_*` name, with the new one
> winning where both are set, so the compose file and `.env` already on the NAS
> keep working across this deploy and can be re-spelled whenever it suits. The
> deployed *paths* — `~/docker/burrow`, the `/burrow/` proxy prefix,
> `~/.burrow/` — are deliberately not renamed here; they are directory names the
> NAS already has, not identifiers, and moving them is a separate operator step.

- **Server** — Docker Compose at `~/docker/burrow` on the NAS (`dxp2800`), which
  maps host `8738` to the container's `8737`:
  `ghcr.io/astral-sh/uv:python3.14-bookworm-slim` installs the locked environment with
  `uv sync --frozen --no-dev` and runs `uv run uvicorn serve:app --host 0.0.0.0 --port 8737`
  with `CHRONICLE_HOST=0.0.0.0`,
  `CHRONICLE_EVENTS=/data/events.jsonl`, `CHRONICLE_TOKEN=<shared secret>`. Deploy code
  and all runtime support and resident manifests with the authoritative
  tar-over-ssh recipe (UGOS scp is broken): `tar -cf - pyproject.toml uv.lock serve.py config.py event_log.py state_coordinator.py village_state.py retention.py
  retention-policy.json
  approval_protocol.py journal_observations.py notification_persistence.py protocol.py
  residents.py typed_json.py hooks villagers | ssh
  Miha@dxp2800 'tar -xf - -C ~/docker/burrow/app'`, then
  `ssh Miha@dxp2800 'cd ~/docker/burrow && docker compose restart burrow'`. Manifests
  ship with the code, so `/villagers` on the NAS matches the repo after every
  deploy — no manual file copying. `tests/test_deployment_bundle.py` parses this
  exact command and boots the tree it packs, so the file list and the test move
  together — that is what keeps the recipe from drifting into a tree that does not
  start.
  Clients consume complete authoritative Village State snapshots from
  `/state` and the live snapshot feed at `/state/stream`; the response disables nginx
  buffering itself. If another reverse proxy is placed in front, keep streaming
  responses unbuffered and give them an idle timeout longer than the 15-second
  keepalive interval. Polling automatically carries the same cursor while a stream
  is unavailable and retries SSE every two seconds.
  Snapshot generations and cursors are explicit. A stale cursor receives one
  atomic reset snapshot; the browser never folds raw events. Raw `/events`
  retrieval remains an internal diagnostic and audit interface. The public UI
  read contract, checked-in OpenAPI, fixtures, and versioning policy are documented
  in [docs/state-contract.md](docs/state-contract.md).
  Development proxies and external-client production state routing are documented in
  [docs/ui-clients.md](docs/ui-clients.md).
- **Mac emitter** — the installed `chronicle-emit` bundle described in the
  [protocol guide](docs/protocol.md#installed-emitter-bundle), wired into
  `~/.claude/settings.json` hooks
  (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`,
  `SubagentStart`, `SubagentStop`, `SessionEnd`) with
  `CHRONICLE_URL=http://dxp2800:8737` and `CHRONICLE_TOKEN=<same
  secret>`. Off the tailnet it records the privacy-filtered event in the bounded
  durable primary outbox and `~/.chronicle/events.jsonl`; later hooks replay
  oldest-first after connectivity returns.
  Sessions pick hooks up on start, so already-running sessions won't appear.
  Every event is also mirrored to a local dev server if one is running — see
  [Working on chronicle locally](#working-on-chronicle-locally).
- **Codex emitter** — the same installed command invoked with `--runner codex` from
  user-level Codex hooks. It uses a distinct `codex:` identity without changing
  the default Claude adapter. Copyable configuration, trust review, and a smoke
  check are in [the protocol guide](docs/protocol.md#user-level-codex-setup).
- **Life Agent emitter** — the same bundle installed at
  `/root/.claude/burrow/` inside the `life-agent` container (via the
  `claude-config` volume), invoked as `/root/.claude/burrow/burrow-emit` — a
  deployed path, unchanged by the identifier rename, and one the bundle still
  answers to. It converges on the new name whenever that container is next
  provisioned. It runs with
  `CHRONICLE_AGENT_ID=life-agent CHRONICLE_PROJECT=life` and the same `CHRONICLE_URL` /
  `CHRONICLE_TOKEN` pair, so it appears as one resident villager that rests between
  turns instead of leaving.
- **Local-only mode** — `uv run uvicorn serve:app --host 127.0.0.1 --port 8737` and no
  `CHRONICLE_URL` still works: same
  API over the local log. Leave `CHRONICLE_TOKEN` unset and ingest stays open.

### Working on chronicle locally

Nothing has to be deployed to the NAS to see a change work on real events. The
emitter mirrors every event to `http://127.0.0.1:8737` as well as to the village,
so:

```sh
uv sync --frozen
uv run uvicorn serve:app --host 127.0.0.1 --port 8737
                                # then open http://127.0.0.1:8737
```

is a live copy of your own fleet — the same sessions, the same villagers, the same
knocks — while the shared village keeps receiving everything as usual. The mirror is
best-effort and off the critical path: when nothing is listening on 8737 the refused
connection costs nothing, and the event still reaches the NAS. Turn it off with
`CHRONICLE_MIRROR=` (empty), point it somewhere else with `CHRONICLE_MIRROR=http://host:port`
(comma-separated for several), and give it a token with `CHRONICLE_MIRROR_TOKEN` — the
village's `CHRONICLE_TOKEN` is deliberately never sent to a mirror.

A mirror success never acknowledges the shared village. Primary failures remain in
`~/.chronicle/primary-outbox.jsonl` until a later hook replays them. Delivery attempts
to independent targets run concurrently and rotate fairly through a durable queue.
The complete transport path runs in a killable helper under a documented one-second
host-hook budget; stalled persistence, diagnostics, or fallback cannot hold up the
hosting agent. A helper killed before its first durable commit can still lose that
current event—the exact durability/deadline tradeoff is documented in the protocol.
One stable outbox transaction lock orders main and journal authorities by a
pre-lock enqueue ID and enforces their aggregate caps. Lock contention and capped
targets are durably deferred and diagnosed. Serialized bounded payload-free
counters and recent failures are inspectable in
`~/.chronicle/transport-diagnostics.json`.
Local-log rotation contention uses the same crash-safe pattern: a stable-lock
deferred journal, atomic handoff, and idempotent replay IDs. Active plus replay
deferred authority retains the newest 1,024 records within 5 MiB; capacity drops
are counted in the transport diagnostics before victims are retired. A crash-time
pending publication makes the physical ceiling three capped generations (15 MiB),
plus at most 8 non-authoritative torn-tail files within 256 KiB.

Two caveats worth knowing:

- The local server's log defaults to `~/.chronicle/events.jsonl`, which is also the
  emitter's offline fallback. Use `CHRONICLE_EVENTS=/tmp/chronicle-dev.jsonl uv run uvicorn serve:app`
  to keep a dev run's history separate.
- Hook *env* is read when a session starts, so changing `CHRONICLE_MIRROR` only affects
  sessions started afterwards. Updating the installed emitter bundle applies
  immediately — its files are re-read on every hook.

No live fleet handy (or testing a projection rule that needs a specific sequence)?
Replay a fixture instead: see [fixtures/README.md](fixtures/README.md). Run the whole
test suite with `sh tests/run.sh`.

### Ingest auth

Anything on the tailnet could otherwise POST fake events, and the village never lies.
When the server has `CHRONICLE_TOKEN` set, `POST /events` must carry it as
`Authorization: Bearer <token>` (or `X-Burrow-Token`) or it gets a 401. That single POST is
the only gated request there is: every GET — `/state`, `/state/stream`, `/events`,
`/villagers`, `/residents`, `/transport/status` — is open, deliberately, so a client needs
no credential to watch the fleet. With the var unset, ingest is open too, which is what
local dev uses. Request framing is checked before the token, so an oversized or
badly-framed POST is refused with a `400` or `413` whether or not it carried one.

Emitters send the token from their own `CHRONICLE_TOKEN`. A rejected POST is just a failed
POST: the event falls back to the local JSONL file, so a missing token costs visibility,
never events. **Roll it out server-first:** deploy the token-aware server with the var
unset, set `CHRONICLE_TOKEN` on every emitter, then set it on the server and restart. Full
order and rotation: [docs/protocol.md](docs/protocol.md#ingest-auth).

### Knocks on your phone

A village you have to be looking at is no good for a knock. Set `CHRONICLE_NOTIFY_URL` on the
server and every `needs_human` event is also pushed there with a stable receiver dedupe ID.
Plain knocks carry the villager's name, project and message; structured approvals carry the
action as title and the detail as body (`https://ntfy.sh/<your-topic>` works out of the box;
`CHRONICLE_NOTIFY_TOKEN` for a private topic). Unset means no notifications, and a dead
notification service can never block or lose an event.

Forwarding uses two workers and a 64-knock memory queue backed by a pre-acknowledgement
fsynced journal. Restart and startup/saturation recovery use stable-lock atomic journal
handoff, and a fixed shard lock spans durable terminal recheck, external notification and
outcome. The knock journal and each delivered/drop ledger are independently capped at 1,024
records/5 MiB. Disjoint active/replay journal authority plus a pending publication can
occupy 15 MiB; live plus pending copies of both fixed-size hashed-ASCII terminal ledgers add
20 MiB (35 MiB total at defaults). Recovery converges before another handoff, with capacity
victims durably terminal-dropped first. `GET /transport/status` reports `delivered` and
`dropped` as the current bounded counts in their authoritative durable ledgers; these
survive restart but can decrease when older keys leave the retention window. Retry, failure
and saturation counters describe only the current server process. That report is published
for clients, but it is delivery telemetry and not part of the state contract.

### Log rotation

The server keeps `events.jsonl` bounded on its own. Past `CHRONICLE_MAX_LOG` bytes (default
5 MiB) it rolls the log into `archive/events-<UTC timestamp>.jsonl` and restarts it from the
tail the projection still needs, so the next snapshot is unchanged and no client sees the
village move. Archives are plain JSONL and keep the full history; set `CHRONICLE_ARCHIVE` to
put them elsewhere (on the NAS, they land next to the log in the mounted volume).
`CHRONICLE_MAX_LOG=0` turns rotation off.

Rotation carries a little state across the cut so the projection does not lose meaning at
the seam. One reserved non-event capsule (at most 32 KiB) carries bounded mood approval
identity, stable append order and an explicit conservative overflow state; it is ignored by
every ordinary projection and cannot create presence. Exact future invalidation over
unrestricted request IDs is impossible in fixed space, so overflow resolves to an uncertain
mood instead of a guess. The delivery-ID acceleration ledger is separately bounded to 1,024
records/5 MiB plus one atomic-copy allowance (10 MiB physical at defaults); retained
live/archive events remain the dedupe authority after ledger eviction. This is exactly-once
replay within retained event authority, not a global-forever guarantee.

Event schema, transports, and projection rules: [docs/protocol.md](docs/protocol.md).

## Driving a client without a fleet

This service has no fleet of its own to test against, so there is a fixture that writes a
synthetic event log for a client to render:

```sh
python3 tests/fixture_walks.py --fresh          # nine agents, a transition every 3 s
CHRONICLE_EVENTS=/tmp/chronicle-fixture.jsonl python3 serve.py 8899
```

Nine agents cycle between working, knocking and resting — eight of them on the far corner
plots — so the snapshots it produces force the longest routes across the map. Whichever
client is pointed at that server gets its pathfinding, its fence gates and its knock queue
exercised without a real agent running anywhere.

## Residents and visitors

Residents get persistent identity and a reserved home from versioned **resident
manifests** under [`villagers/`](villagers). Every other projected identity is a
Visitor based at the shared lodge. Editing a resident is: edit the JSON file → run
the tests → commit → deploy. Point `CHRONICLE_VILLAGERS` at another directory to
override it (handy for a local scratch village).

The [resident-manifest v1 guide](docs/resident-manifest.md) documents the validated
schema for identity, soul, skills, durable memory, routes, app grants, and the stable
home number. `GET /residents` reports valid declarations and actionable diagnostics;
both resident endpoints publish only the guide's allow-listed display and capability
metadata, never raw manifest objects or app credentials. Exact agent identity wins
before a project fallback. Invalid or incomplete declarations remain Visitors and
advertise no access.

Visitors get a stable hash-based name and sprite for their event identity. Sprites belong to
the client that draws them; the CC0 [Ninja Adventure pack](https://pixel-boy.itch.io/ninja-adventure-asset-pack)
this village is drawn with now lives with Arcadia's assets, attribution included.

## Working on chronicle

Work is tracked as [GitHub issues](https://github.com/0xCommanderKeen/warren/issues)
with status labels. The convention, for humans and agents alike:

- `status:ready` — free to pick up. **When you start, swap it to `status:in-progress`**
  and drop a comment saying who/what is working on it; that's the claim.
- `status:blocked` — a "Blocked by" issue must close first; swap to `status:ready`
  when it does.
- `needs:decision` — don't start; a human call is required first (see issue body).
- `status:parked` — decision record, deliberately not scheduled.

Remove the status label when the issue closes.

Tests are plain scripts throughout the repository. For example, the root-level
server test can be run directly with `python3 test_serve.py`.

### Tests

The authoritative test command discovers every tracked `test_*.py` file anywhere in the
repository and runs each exactly once. One language, no framework, no build step:

```sh
sh tests/run.sh
sh tests/run.sh --list  # show the tests that would run
```

The authoritative projection lives in `village_state.py` and `state_coordinator.py`
publishes complete snapshots. That is the whole reduction: no second reducer in a client,
no browser tests here, no build step:

```sh
python3 -m unittest tests.test_village_state tests.test_state_coordinator
sh tests/ui-contract.sh
```

Projection tests use fixed evaluation times so stale and absent clock boundaries
remain deterministic. `tests/ui-contract.sh` checks that the published contract and the
captured fixtures clients render against still match the snapshot models.

## Not this project

Game mechanics, inventories, simulated needs, LLM-driven fictional characters, emergent narrative — that is a separate concern, and lives in this monorepo's [arcadia/](../arcadia/).
