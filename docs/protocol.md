# burrow event protocol — v0

Everything in burrow consumes this. Agents (or adapters wrapping them) append one JSON
object per line to the event log. The projection layer never reads agent internals —
only this log.

## Transport (v0)

Append-only JSONL file: `~/.burrow/events.jsonl`. One event per line. Writers append
a single line atomically (a single `write()` of < 4 KB is atomic enough on macOS/Linux
for line-oriented logs). Later versions may move to a socket or HTTP ingest; the event
shape is the contract, not the file.

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
