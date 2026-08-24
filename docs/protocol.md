# burrow event protocol — v0

Everything in burrow consumes this. Agents (or adapters wrapping them) append one JSON
object per line to the event log. The projection layer never reads agent internals —
only this log.

## Transport (v0.5)

Two transports; the event shape is the contract, not the pipe:

- **HTTP ingest** (preferred): `POST <BURROW_URL>/events` with one JSON event as the
  body → 204. The server appends it to its own log. Emitters set `BURROW_URL`; a
  failed POST trips a 60 s circuit breaker and falls back to the local file, so an
  unreachable server never slows an agent down.
- **Local JSONL file**: append one event per line to `~/.burrow/events.jsonl`
  (a single `write()` of < 4 KB is atomic enough on macOS/Linux). Used when
  `BURROW_URL` is unset, or as the fallback above.

## Ingest auth

The village never lies, so nothing on the tailnet may write to it unasked — a forged
event is a lie with extra steps. Ingest is protected by one shared secret:

- The server reads `BURROW_TOKEN`. **When set**, `POST /events` must present it as
  `Authorization: Bearer <token>` (preferred) or `X-Burrow-Token: <token>`; anything
  else gets `401 unauthorized` and is not logged. Comparison is constant-time, and an
  empty/whitespace-only value counts as unset.
- **When unset**, ingest is open — exactly today's behavior, which is what local-only
  mode (`python3 serve.py` with no `BURROW_URL`) relies on.
- **GET is never gated.** `/`, `/events`, `/villagers`, and the static viewer stay open;
  the token guards writes, not reads. The event log is still a map of everything the
  fleet does, so the server stays off the public internet either way.

Emitters send `BURROW_TOKEN` from their own env. A 401 is treated as just another
failed POST: circuit breaker trips, the event is appended to the local JSONL file. **A
wrong token loses no events** — only their remoteness — which is what makes the rollout
below safe.

### Rollout order

The token must never be required before it is sent, or in-flight agents silently fall
back to local logs and the village looks emptier than the fleet is.

1. **Server first, token unset.** Deploy the `BURROW_TOKEN`-aware `serve.py` with the
   var *not* set. Behavior is unchanged; every existing emitter keeps working.
2. **Emitters next.** Roll `BURROW_TOKEN=<secret>` out to every emitter (Mac hooks,
   `life-agent` container, any other resident). A server with no token set ignores the
   header, so emitters can be updated one at a time with no coordination.
3. **Server last, token set.** Once every emitter sends the secret, set `BURROW_TOKEN`
   on the server and restart. Ingest is now closed; anything still unconfigured degrades
   to its local log rather than disappearing.

Rotating the secret runs the same loop in reverse: unset on the server, re-issue to
emitters, set again.

## Event shape

```json
{
  "v": 0,
  "ts": "2026-08-24T14:03:22.114Z",
  "source": "claude-code",
  "agent_id": "claude-code:9f2c81aa-…",
  "project": "burrow",
  "cwd": "/Users/miha/Work/hobbies/burrow",
  "type": "tool_called",
  "payload": { "tool": "Read", "detail": "README.md" }
}
```

| field      | meaning                                                                 |
|------------|-------------------------------------------------------------------------|
| `v`        | protocol version, `0` for now                                           |
| `ts`       | UTC ISO-8601 with milliseconds, `Z` suffix                              |
| `source`   | what kind of runner emitted this (`claude-code`, later: `cron`, custom) |
| `agent_id` | stable identity of the villager: `<source>:<session or agent uuid>`     |
| `project`  | human label for grouping (v0: basename of `cwd`)                        |
| `cwd`      | where the agent is working, if it has a working directory               |
| `type`     | one of the types below                                                  |
| `payload`  | type-specific detail, always an object, may be empty                    |

## Event types

| type                | emitted when…                              | payload                    |
|---------------------|--------------------------------------------|----------------------------|
| `task_started`      | the agent picks up work                    | `prompt` (truncated ≤140)  |
| `tool_called`       | the agent uses a tool                      | `tool`, `detail` (≤120)    |
| `artifact_produced` | the agent writes/edits a file or output    | `artifact` (path)          |
| `needs_human`       | the agent is blocked on the human          | `message`                  |
| `idle`              | the agent finished its turn and is resting | —                          |
| `session_ended`     | the agent is gone (villager leaves)        | —                          |

## Projection rules (v0)

The villager's state is decided by its **latest** event:

- `task_started`, `tool_called`, `artifact_produced` → **working** (the payload is what
  it is doing right now)
- `needs_human` → **at your door**
- `idle` → **resting**
- `session_ended` → removed from the village
- no event for 30 minutes while "working" → shown as **stale** (faded), because a
  villager frozen mid-swing would be a lie

## The one rule, restated for implementers

Never emit an event for something that did not happen, and never render state that no
event supports. Filler is forbidden on both sides of the log.

## v0 emitter: Claude Code hooks

`hooks/emit.py` adapts Claude Code hook callbacks to this protocol:

| Claude Code hook                          | burrow event        |
|-------------------------------------------|---------------------|
| `UserPromptSubmit`                        | `task_started`      |
| `PreToolUse` (any tool)                   | `tool_called`       |
| `PostToolUse` (`Write`/`Edit`/`NotebookEdit`) | `artifact_produced` |
| `Notification`                            | `needs_human`       |
| `Stop`                                    | `idle`              |
| `SessionEnd`                              | `session_ended`     |

The emitter must never break the agent: it swallows all errors and always exits 0.

Env vars: `BURROW_URL` (POST target, see Transport) and `BURROW_TOKEN` (ingest secret,
see Ingest auth — sent as a bearer header, omitted when unset). **Resident agents** — services
that outlive any one Claude session, like a bot running `claude -p` per message —
set `BURROW_AGENT_ID` (stable villager identity, e.g. `life-agent`) and optionally
`BURROW_PROJECT` (label). For a resident, `SessionEnd` maps to `idle` instead of
`session_ended`: the session's process died, but the agent-as-service is still
home, resting.
