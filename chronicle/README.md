# burrow

The backend behind a living village that shows what your AI agents are actually doing.

Each real agent — the one summarizing your day, reviewing your code, reading your email, researching, or supervising the others — is a villager with a house. When an agent works, its villager works. When it's idle, it rests. When it needs you, it walks to your door and knocks.

**burrow is not a game and not a simulation.** It is an ambient interface to a real agent fleet. The village is a projection of live events; it never invents behavior.

**burrow runs no browser code.** It is the event protocol, the log, the projection and the
HTTP API, in Python. Every UI is a separate client consuming the versioned state contract:
**Arcadia** renders the pixel-art village, **Townhall** is the control panel. Requests for
paths this repository used to serve a viewer on now 404 — see
[docs/ui-clients.md](docs/ui-clients.md).

## The one rule

**The village never lies.** A sprite doing something means the agent behind it is genuinely doing that right now. No ambient filler animation, no faked liveliness. The shape of the village *is* the state of the fleet:

- Nobody moving → nothing is running.
- A villager at your door → something needs your attention.

## Architecture (four layers)

1. **The fleet** — real agents, running wherever they run. burrow does not own them.
2. **Event protocol** — agents emit structured events (`task_started`, `tool_called`, `artifact_produced`, `needs_human`, `idle`). This is the core of the project; everything else consumes it.
3. **Projection** — maps events to village state (agent started reading inbox → villager walks to the post office).
4. **Clients** — pixel-art renderers and control panels, each its own project. They read
   complete snapshots from `/state` and `/state/stream` and render them; they never fold raw
   events. burrow ships none of them.

The notice board in the village square opens a fleet-wide review of the 30 most
recent `artifact_produced` events. It shows each artifact path, its maker and
project, and how recently it was produced; an empty board says plainly that the
recent event log contains no artifacts.

Beside it, the job board shows Steward's bounded current queue from `task_posted`,
`task_claimed`, `task_done`, and `task_failed` events. Terminal work expires after a
short visible window. A `task_failed` event whose reason is `lease_expired` instead
reopens the job, matching Steward's queue, and identifies the expired attempt without
leaving a ghost claim. Its
post form writes directly to Steward with changeable browser-memory credentials; a job
appears only after its exact, contract-valid event reaches Burrow.
Blank skill names in valid Steward evidence are shown as explicit unnamed-skill markers,
distinct from an empty requirement list and from unavailable post evidence.
The form prevents overlapping or ambiguity-driven duplicate submissions. Only Steward's
pre-mutation `401` and `422` refusals are retryable; server/proxy failures remain ambiguous
unless their task identity is later proved by the exact event. Lease-expiry retry context
links a present former claimant and marks an absent one explicitly.
Rotation retains task lifecycle by task ID across the central poster and claimant
sessions; an orphan transition says required skills are unavailable instead of claiming
there were none. Post acknowledgement times out 15 seconds from request start even when
Steward's HTTP response is still in flight. A late valid response retains its safe identities
but remains visibly timed out until an exact post-boundary event proves the job; a malformed
acceptance with a valid task identity follows the same evidence-only reconciliation rule.

A structured `needs_human` opens an approval queue on that villager with the action,
detail, and Steward-defined approve/deny/edit options. The browser POSTs directly to
Steward with memory-only credentials, but the villager leaves the door only after the
exact `needs_human_resolved` event reaches Burrow. Refusals, timeouts, ambiguous
delivery, credential correction, and duplicate prevention stay explicit. Plain and
malformed structured knocks retain the legacy text-only behavior. Structured phone
notifications use the action as title and detail as body while preserving the same
durable one-event/one-notification claim. The immutable question (including semantic
detail/options, message, and expiry) is quarantined if one request ID is reused
incompatibly; equal-time decisions use append order. Recent confirmed cards remain
beside a newer pending queue, while a parked session stays ended after its approval
closes. Only Steward's exact parsed `approval_expired` 409 envelope permits safe retry.

Every projected Resident and Visitor also carries one inspectable operational
mood glyph beside its name. It is a deterministic summary of retained failures,
work density, exact human interactions, and unresolved needs—not personality or
sentiment. Its native details disclosure says exactly which evidence was
observed and anchors ages to the log, so a quiet browser clock never changes the
claim. Inactive declarations receive no glyph; stale presence only fades the
same unmodified reading. The exact reducer and thresholds are documented in
[the protocol](docs/protocol.md#operational-mood-glyph).

The **fleet ledger** beside the census is the compact operational view. It keeps
the newest 200 validated events from the shared parsed stream and filters them by
search text, project, runner/source, event-derived state, and villager. Its
needs-you leaf contains only villagers whose latest retained event remains
`needs_human`. Resident leaves show stable homes and all five manifest dimensions;
Visitors remain grouped under the lodge. “Configured” means a complete safe
declaration exists—not that Burrow tested credentials or an external service. A
`status_ref` is only an opaque locator for where status is established; its text
is never interpreted as live health. “Missing” means no declaration, “invalid”
comes from public diagnostics or an incomplete public record, and “externally
unavailable” means the resident feed itself cannot currently be read.
Missing declarations, invalid/incomplete manifests, an unavailable resident feed,
malformed event records, stale telemetry, disconnection, and empty results are each
named explicitly. No credentials or raw manifest objects are rendered.

Resident detail panels also read identity directly from Steward. The charter is the
manifest-declared mission, duties, hard rules, and escalation policy, visually separated
from log-observed work. The latest seven journal entries come from Steward's read-only
`GET /residents/{id}/journal?limit=7` endpoint, newest first. Those reads use the same
browser-memory-only URL and bearer token as the other Steward controls; Burrow's server
never proxies or caches them. Unreachable and malformed reads remain explicit, with the
last successful in-memory entries labeled stale rather than mistaken for an empty journal.
If Burrow's own resident-manifest feed is unavailable, cached Steward associations are also
marked externally unavailable and stale until the exact local declaration can be read again.
Fleet resident rows show charter state and journal recency. Visitors instead explain that
temporary lodge occupants have no resident soul, manifest, charter, or journal.
A strict Steward `journal_written` observation adds a
separate “observed written” recency without inventing text or refreshing that direct read.
For its first 60 seconds a matched Resident writes at a small illuminated home desk. Matching
requires an exact declared agent or one unambiguous project-root declaration: a Claude or
Codex child with `parent_agent_id` never borrows its parent's project home. Child, ambiguous,
invalid, and unmatched observations stay only in Fleet history with an explicit diagnostic.
First append owns each resident/day,
and the highest 40 canonical `(day, agent_id)` keys survive log rotation with one
representative collision diagnosed per retained key.

## Running

### Canonical operator path

There is one supported setup path for both Claude Code and Codex:

1. Install the shared emitter with `sh scripts/install-emitter.sh`.
2. Configure Claude Code or Codex using the copyable hook definitions in the
   [protocol guide](docs/protocol.md#runner-setup). Both runners invoke the same
   installed bundle; Codex adds `--runner codex`.
3. Add or edit `villagers/*.resident.json`, then validate every manifest with
   `python3 -m unittest tests.test_residents`. App-grant references and status
   references describe public configuration/health locations only. Burrow never
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

- **Server** — Docker Compose at `~/docker/burrow` on the NAS (`dxp2800`), which
  maps host `8738` to the container's `8737`:
  `ghcr.io/astral-sh/uv:python3.14-bookworm-slim` installs the locked environment with
  `uv sync --frozen --no-dev` and runs `uv run uvicorn serve:app --host 0.0.0.0 --port 8737`
  with `BURROW_HOST=0.0.0.0`,
  `BURROW_EVENTS=/data/events.jsonl`, `BURROW_TOKEN=<shared secret>`. Deploy code
  and all runtime support and resident manifests with the authoritative
  tar-over-ssh recipe (UGOS scp is broken): `tar -cf - pyproject.toml uv.lock serve.py config.py event_log.py state_coordinator.py village_state.py retention.py
  retention-policy.json
  approval_protocol.py journal_observations.py notification_persistence.py protocol.py
  residents.py hooks villagers | ssh
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
- **Mac emitter** — the installed `burrow-emit` bundle described in the
  [protocol guide](docs/protocol.md#installed-emitter-bundle), wired into
  `~/.claude/settings.json` hooks
  (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`,
  `SubagentStart`, `SubagentStop`, `SessionEnd`) with
  `BURROW_URL=http://dxp2800:8737` and `BURROW_TOKEN=<same
  secret>`. Off the tailnet it records the privacy-filtered event in the bounded
  durable primary outbox and `~/.burrow/events.jsonl`; later hooks replay
  oldest-first after connectivity returns.
  Sessions pick hooks up on start, so already-running sessions won't appear.
  Every event is also mirrored to a local dev server if one is running — see
  [Working on burrow locally](#working-on-burrow-locally).
- **Codex emitter** — the same installed command invoked with `--runner codex` from
  user-level Codex hooks. It uses a distinct `codex:` identity without changing
  the default Claude adapter. Copyable configuration, trust review, and a smoke
  check are in [the protocol guide](docs/protocol.md#user-level-codex-setup).
- **Life Agent emitter** — the same bundle installed at
  `/root/.claude/burrow/` inside the `life-agent` container (via the
  `claude-config` volume), invoked as `/root/.claude/burrow/burrow-emit`, with
  `BURROW_AGENT_ID=life-agent BURROW_PROJECT=life` and the same `BURROW_URL` /
  `BURROW_TOKEN` pair, so it appears as one resident villager that rests between
  turns instead of leaving.
- **Local-only mode** — `uv run uvicorn serve:app --host 127.0.0.1 --port 8737` and no
  `BURROW_URL` still works: same
  API over the local log. Leave `BURROW_TOKEN` unset and ingest stays open.

### Working on burrow locally

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
`BURROW_MIRROR=` (empty), point it somewhere else with `BURROW_MIRROR=http://host:port`
(comma-separated for several), and give it a token with `BURROW_MIRROR_TOKEN` — the
village's `BURROW_TOKEN` is deliberately never sent to a mirror.

A mirror success never acknowledges the shared village. Primary failures remain in
`~/.burrow/primary-outbox.jsonl` until a later hook replays them. Delivery attempts
to independent targets run concurrently and rotate fairly through a durable queue.
The complete transport path runs in a killable helper under a documented one-second
host-hook budget; stalled persistence, diagnostics, or fallback cannot hold up the
hosting agent. A helper killed before its first durable commit can still lose that
current event—the exact durability/deadline tradeoff is documented in the protocol.
One stable outbox transaction lock orders main and journal authorities by a
pre-lock enqueue ID and enforces their aggregate caps. Lock contention and capped
targets are durably deferred and diagnosed. Serialized bounded payload-free
counters and recent failures are inspectable in
`~/.burrow/transport-diagnostics.json`.
Local-log rotation contention uses the same crash-safe pattern: a stable-lock
deferred journal, atomic handoff, and idempotent replay IDs. Active plus replay
deferred authority retains the newest 1,024 records within 5 MiB; capacity drops
are counted in the transport diagnostics before victims are retired. A crash-time
pending publication makes the physical ceiling three capped generations (15 MiB),
plus at most 8 non-authoritative torn-tail files within 256 KiB.

Two caveats worth knowing:

- The local server's log defaults to `~/.burrow/events.jsonl`, which is also the
  emitter's offline fallback. Use `BURROW_EVENTS=/tmp/burrow-dev.jsonl uv run uvicorn serve:app`
  to keep a dev run's history separate.
- Hook *env* is read when a session starts, so changing `BURROW_MIRROR` only affects
  sessions started afterwards. Updating the installed emitter bundle applies
  immediately — its files are re-read on every hook.

No live fleet handy (or testing a projection rule that needs a specific sequence)?
Replay a fixture instead: see [fixtures/README.md](fixtures/README.md). Run the whole
test suite with `sh tests/run.sh`.

### Ingest auth

Anything on the tailnet could otherwise POST fake events, and the village never lies.
When the server has `BURROW_TOKEN` set, `POST /events` must carry it as
`Authorization: Bearer <token>` (or `X-Burrow-Token`) or it gets a 401. GET endpoints —
`/state`, `/events`, `/villagers` — are not gated. With the var unset, ingest is open,
which is what local dev uses.

Emitters send the token from their own `BURROW_TOKEN`. A rejected POST is just a failed
POST: the event falls back to the local JSONL file, so a missing token costs visibility,
never events. **Roll it out server-first:** deploy the token-aware server with the var
unset, set `BURROW_TOKEN` on every emitter, then set it on the server and restart. Full
order and rotation: [docs/protocol.md](docs/protocol.md#ingest-auth).
  API over the local log.
- **Knocks on your phone** — set `BURROW_NOTIFY_URL` on the server and every
  `needs_human` event is also pushed there with a stable receiver dedupe ID. Plain knocks carry the
  villager's name, project, and message; structured approvals carry action and detail
  (`https://ntfy.sh/<your-topic>` works out of the box;
  `BURROW_NOTIFY_TOKEN` for a private topic). Unset means no notifications, and a
  dead notification service can never block or lose an event. Forwarding uses two
  workers and a 64-knock memory queue backed by a pre-acknowledgement fsynced journal. Restart and
  startup/saturation recovery use stable-lock atomic journal handoff. A fixed
  shard lock spans durable terminal recheck, external notification, and outcome.
  The knock journal and each delivered/drop ledger are independently capped at
  1,024 records/5 MiB. Disjoint active/replay journal authority plus a pending
  publication can occupy 15 MiB; live plus pending copies of both fixed-size
  hashed-ASCII terminal ledgers add 20 MiB (35 MiB total at defaults). Recovery converges before another handoff, with capacity victims
  durably terminal-dropped first. `GET /transport/status` reports `delivered` and
  `dropped` as the current bounded counts in their authoritative durable ledgers;
  these survive restart but can decrease when older keys leave the retention window.
  Retry, failure, and saturation counters describe only the current server process.
  Live clients consume this report.
- **Log rotation** — the server keeps `events.jsonl` bounded on its own. Past
  `BURROW_MAX_LOG` bytes (default 5 MiB) it rolls the log into
  `archive/events-<UTC timestamp>.jsonl` and restarts it from the tail the
  village still needs, so nothing on screen changes. Archives are plain JSONL and
  keep the full history; set `BURROW_ARCHIVE` to put them elsewhere (on the NAS,
  they land next to the log in the mounted volume). `BURROW_MAX_LOG=0` turns
  rotation off. One reserved non-event capsule (at most 32 KiB) carries bounded Mood approval
  identity, stable append order, and an explicit conservative overflow state
  across rotations; it is ignored by every ordinary projection and cannot
  create presence. Exact future invalidation over unrestricted request IDs is
  impossible in fixed space, so overflow displays an uncertain mood instead of
  guessing. The delivery-ID acceleration ledger is separately bounded to
  1,024 records/5 MiB plus one atomic-copy allowance (10 MiB physical at
  defaults); retained live/archive events remain the dedupe authority after
  ledger eviction. This is exactly-once replay within retained event authority,
  not a global-forever guarantee.

Event schema, transports, and projection rules: [docs/protocol.md](docs/protocol.md).

## Time of day

The village is tinted by the **real local time of the machine viewing it** —
dawn, day, dusk and night, interpolated so there is never a jump. It is a
projection of the clock and nothing else: no weather, no seasons, no simulated
sky. After dark, a house's windows and doorway light up only while its villager
is genuinely home, your porch lights only while somebody is actually knocking,
and the working glow, the knock orange and the stale fade all stay legible.

The tint is a client concern and burrow ships no client: the rule it must obey is that
the phase comes from the real clock, and any development override says so on screen so a
pinned tint never passes as the real thing.

## Driving a client without a fleet

The village has no fleet of its own to test against, so there is a fixture that
writes a synthetic event log for a client to render:

```sh
python3 tests/fixture_walks.py --fresh          # nine agents, a transition every 3 s
BURROW_EVENTS=/tmp/burrow-fixture.jsonl python3 serve.py 8899
```

It forces the longest walks on the map — corner plots to your door and back — so
pathfinding, fence gates and the knock queue all get exercised in whichever client is
pointed at that server.

## Residents and visitors

Residents get persistent identity and a reserved home from versioned **resident
manifests** under [`villagers/`](villagers). Every other projected identity is a
Visitor based at the shared lodge. Editing a resident is: edit the JSON file → run
the tests → commit → deploy. Point `BURROW_VILLAGERS` at another directory to
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

## Working on burrow

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
