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
  `BURROW_EVENTS=/data/events.jsonl`. Deploy code updates with a tar-over-ssh pipe
  (UGOS scp is broken): `tar -cf - serve.py viewer | ssh Miha@dxp2800 'tar -xf - -C
  ~/docker/burrow/app'`, then `docker compose restart burrow`.
- **Mac emitter** — `hooks/emit.py` wired into `~/.claude/settings.json` hooks
  (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`,
  `SessionEnd`) with `BURROW_URL=http://dxp2800:8737`. Off the tailnet it falls
  back to `~/.burrow/events.jsonl` locally. Sessions pick hooks up on start, so
  already-running sessions won't appear.
- **Life Agent emitter** — same script at `/root/.claude/burrow-emit.py` inside the
  `life-agent` container (via the `claude-config` volume), with
  `BURROW_AGENT_ID=life-agent BURROW_PROJECT=life` so it appears as one resident
  villager that rests between turns instead of leaving.
- **Local-only mode** — `python3 serve.py` and no `BURROW_URL` still works: same
  viewer over the local log.

Event schema, transports, and projection rules: [docs/protocol.md](docs/protocol.md).

## Souls

Villagers get persistent identity from **soul files**: `~/.burrow/villagers/*.md`
(override the directory with `BURROW_VILLAGERS`; on the NAS, mount it next to the
event log). Frontmatter pins who the file is for and how they look; the body is
free-form markdown shown when you click the villager (description, skills, anything).

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

## Not this project

Game mechanics, inventories, simulated needs, LLM-driven fictional characters, emergent narrative — that is a separate project ([arcadia](https://github.com/0xCommanderKeen/arcadia)).
