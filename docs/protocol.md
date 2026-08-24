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

## Log rotation

The live log is a window, not an archive. When `events.jsonl` grows past
`BURROW_MAX_LOG` bytes (default 5 MiB, `0` disables), the server rolls it into
`<BURROW_ARCHIVE or archive/>/events-20260824T170430Z.jsonl` and starts the live
file again from the **carry-forward tail** derived from the same latest 4,000
lines the viewer reads: the last 80 visible events of every villager the
projection would still draw — plus its latest liveness-only heartbeat when
present, and skipping any whose latest signal is `session_ended` or older than
the 12 h drop window — in their original order.

That tail is exactly the input the rules above consume, so the village renders
identically across a rotation: same states, same panel history. It also means the
live log has a floor of (live agents × 80 events); a very busy fleet can sit
above the threshold, and rotation then waits rather than copying the log next to
itself.

Rotation is checked after every accepted `POST /events` and before every
`GET /events` (local mode has no POSTs — emitters append to the file themselves).
All three paths take the same process lock, and file appends and rotation also
take an advisory lock on the log shared with the bundled emitter. Rotation
archives a snapshot and rewrites the live file in place, retaining its inode so
even an already-open append descriptor continues writing to the live log.

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

Env vars: `BURROW_URL` (POST target, see Transport). **Resident agents** — services
that outlive any one Claude session, like a bot running `claude -p` per message —
set `BURROW_AGENT_ID` (stable villager identity, e.g. `life-agent`) and optionally
`BURROW_PROJECT` (label). For a resident, `SessionEnd` maps to `idle` instead of
`session_ended`: the session's process died, but the agent-as-service is still
home, resting.
