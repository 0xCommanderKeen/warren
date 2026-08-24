# burrow

A living village that shows what your AI agents are actually doing.

Each real agent — the one summarizing your day, reviewing your code, reading your email, researching, or supervising the others — is a villager with a house. When an agent works, its villager works. When it's idle, it rests. When it needs you, it walks to your door and knocks.

**burrow is not a game and not a simulation.** It is an ambient interface to a real agent fleet. The village is a projection of live events; it never invents behavior.

## The one rule

**The village never lies.** A sprite doing something means the agent behind it is genuinely doing that right now. No ambient filler animation, no faked liveliness. The shape of the village *is* the state of the fleet:

- Nobody moving → nothing is running.
- A villager at your door → something needs your attention.

## Architecture (four layers)

1. **The fleet** — real agents, running wherever they run. burrow does not own them.
2. **Event protocol** — agents emit structured events (`task_started`, `tool_called`, `artifact_produced`, `needs_human`, `idle`). This is the core of the project; everything else consumes it.
3. **Projection** — maps events to village state (agent started reading inbox → villager walks to the post office).
4. **Client** — pixel-art renderer. Click a villager to see its current task, its log, and chat with it.

## Running (v0.5)

One village for the whole fleet, served from the NAS over Tailscale:
<http://dxp2800:8737>. Never exposed to the public internet — the event log is a
map of everything the fleet does.

- **Server** — Docker Compose at `~/docker/burrow` on the NAS (`dxp2800`):
  `python:3.12-slim` running `serve.py` with `BURROW_HOST=0.0.0.0`,
  `BURROW_EVENTS=/data/events.jsonl`, `BURROW_TOKEN=<shared secret>`. Deploy code
  updates with a tar-over-ssh pipe (UGOS scp is broken): `tar -cf - serve.py viewer |
  ssh Miha@dxp2800 'tar -xf - -C ~/docker/burrow/app'`, then `docker compose restart
  burrow`.
  `BURROW_EVENTS=/data/events.jsonl`. Deploy code *and souls* with a tar-over-ssh
  pipe (UGOS scp is broken): `tar -cf - serve.py viewer villagers | ssh Miha@dxp2800
  'tar -xf - -C ~/docker/burrow/app'`, then `docker compose restart burrow`. Souls
  ship with the code, so `/villagers` on the NAS matches the repo after every
  deploy — no manual file copying.
- **Mac emitter** — `hooks/emit.py` wired into `~/.claude/settings.json` hooks
  (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`,
  `SessionEnd`) with `BURROW_URL=http://dxp2800:8737` and `BURROW_TOKEN=<same
  secret>`. Off the tailnet it falls back to `~/.burrow/events.jsonl` locally.
  Sessions pick hooks up on start, so already-running sessions won't appear.
- **Life Agent emitter** — same script at `/root/.claude/burrow-emit.py` inside the
  `life-agent` container (via the `claude-config` volume), with
  `BURROW_AGENT_ID=life-agent BURROW_PROJECT=life` and the same `BURROW_URL` /
  `BURROW_TOKEN` pair, so it appears as one resident villager that rests between
  turns instead of leaving.
- **Local-only mode** — `python3 serve.py` and no `BURROW_URL` still works: same
  viewer over the local log. Leave `BURROW_TOKEN` unset and ingest stays open.

### Ingest auth

Anything on the tailnet could otherwise POST fake events, and the village never lies.
When the server has `BURROW_TOKEN` set, `POST /events` must carry it as
`Authorization: Bearer <token>` (or `X-Burrow-Token`) or it gets a 401. GET endpoints —
the viewer, `/events`, `/villagers` — are not gated. With the var unset, ingest is open,
which is what local dev uses.

Emitters send the token from their own `BURROW_TOKEN`. A rejected POST is just a failed
POST: the event falls back to the local JSONL file, so a missing token costs visibility,
never events. **Roll it out server-first:** deploy the token-aware server with the var
unset, set `BURROW_TOKEN` on every emitter, then set it on the server and restart. Full
order and rotation: [docs/protocol.md](docs/protocol.md#ingest-auth).
  viewer over the local log.
- **Knocks on your phone** — set `BURROW_NOTIFY_URL` on the server and every
  `needs_human` event is also pushed there once, with the villager's name, project
  and message (`https://ntfy.sh/<your-topic>` works out of the box;
  `BURROW_NOTIFY_TOKEN` for a private topic). Unset means no notifications, and a
  dead notification service can never block or lose an event.
- **Log rotation** — the server keeps `events.jsonl` bounded on its own. Past
  `BURROW_MAX_LOG` bytes (default 5 MiB) it rolls the log into
  `archive/events-<UTC timestamp>.jsonl` and restarts it from the tail the
  village still needs, so nothing on screen changes. Archives are plain JSONL and
  keep the full history; set `BURROW_ARCHIVE` to put them elsewhere (on the NAS,
  they land next to the log in the mounted volume). `BURROW_MAX_LOG=0` turns
  rotation off.

Event schema, transports, and projection rules: [docs/protocol.md](docs/protocol.md).

## Testing the viewer

The village has no fleet of its own to test against, so there is a fixture that
writes a synthetic event log:

```sh
python3 tests/fixture_walks.py --fresh          # nine agents, a transition every 3 s
BURROW_EVENTS=/tmp/burrow-fixture.jsonl python3 serve.py 8899
```

It forces the longest walks on the map — corner plots to your door and back — so
pathfinding, fence gates and the knock queue all get exercised. The viewer checks
its own map on boot and logs one line; `__burrow.village.checkMap()` in the console
re-runs it and returns any spot that stands on solid ground or cannot be reached.

## Souls

Villagers get persistent identity from **soul files**, versioned in this repo under
[`villagers/`](villagers) — one `*.md` per villager, and the source of truth. Editing
a villager is: edit the file → commit → deploy. Point `BURROW_VILLAGERS` at another
directory to override (handy for a local scratch village).

Frontmatter pins who the file is for and how they look; the body is free-form
markdown shown when you click the villager (description, skills, anything).

```md
---
project: burrow          # match ephemeral sessions by project…
# agent_id: life-agent   # …or a resident agent by its stable id (wins over project)
name: Maren
char: Hunter             # sprite: Villager…Villager5, Woman, Boy, OldMan,
                         # Princess, Hunter, Noble, Monk
accent: "#4f7d5b"
role: village builder
---
Works on burrow itself — the village you are looking at.

## Skills
- pixel-art viewer
```

Agents without a soul file get a stable hash-based name and sprite. The viewer's
pixel art is the CC0 [Ninja Adventure pack](https://pixel-boy.itch.io/ninja-adventure-asset-pack)
(see `viewer/assets/README.md`).

## Working on burrow

Work is tracked as [GitHub issues](https://github.com/0xCommanderKeen/burrow/issues)
with status labels. The convention, for humans and agents alike:

- `status:ready` — free to pick up. **When you start, swap it to `status:in-progress`**
  and drop a comment saying who/what is working on it; that's the claim.
- `status:blocked` — a "Blocked by" issue must close first; swap to `status:ready`
  when it does.
- `needs:decision` — don't start; a human call is required first (see issue body).
- `status:parked` — decision record, deliberately not scheduled.

Remove the status label when the issue closes.

Tests are plain stdlib scripts under `tests/`, run directly:
`python3 tests/test_ingest_auth.py`.
### Tests

The projection — the rules from [docs/protocol.md](docs/protocol.md) that turn an
event log into villagers — lives in `viewer/projection.js`. The viewer loads it as
a plain `<script>`; the tests `require()` the same file. No build step, no
framework, no install:

```sh
node --test                      # from the repo root: everything under tests/
node tests/projection.test.js    # or just this one file
```

Cases are driven by fixture event logs in `tests/fixtures/*.jsonl`, all written
against a fixed `now` (`2026-08-24T12:00:00Z`) so the 30-minute stale and 12-hour
drop windows land exactly on their edges. Change a projection rule and a fixture
should have to change with it.

## Not this project

Game mechanics, inventories, simulated needs, LLM-driven fictional characters, emergent narrative — that is a separate project ([arcadia](https://github.com/0xCommanderKeen/arcadia)).
