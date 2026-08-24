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

## Running v0

v0 projects every Claude Code session on this machine as a villager.

1. **Emitter** — `hooks/emit.py` is wired into `~/.claude/settings.json` hooks
   (`UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`,
   `SessionEnd`). Each hook appends one protocol event to `~/.burrow/events.jsonl`.
   Sessions pick hooks up on start, so already-running sessions won't appear.
2. **Server + viewer** — `python3 serve.py` then open <http://localhost:8737>.

Event schema and projection rules: [docs/protocol.md](docs/protocol.md).

## Not this project

Game mechanics, inventories, simulated needs, LLM-driven fictional characters, emergent narrative — that is a separate project ([arcadia](https://github.com/0xCommanderKeen/arcadia)).
