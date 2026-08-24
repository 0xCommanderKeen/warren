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
The viewer bootstraps and falls back through `GET /events?since=<cursor>`. The
response body contains only complete JSONL lines after that position, and
`X-Burrow-Cursor` supplies the cursor for the next request. Omit `since` (or use `0`)
for a full bootstrap. If a log is truncated or rotated, the server starts again at
byte zero and includes `X-Burrow-Reset: 1`; consumers must discard their reduced
state before folding in that response.

Live updates use `GET /events/stream?since=<cursor>` with `text/event-stream`. Each
JSON event is one SSE `data` message and its `id` is the cursor immediately after
that event. Reconnect with that value in `Last-Event-ID` (preferred automatically by
`EventSource`) or `since`; both transports use the same cursor, so switching between
SSE and polling neither duplicates nor skips an event. A rotation emits an SSE
`reset` event before replaying the new live log. The stream sends keepalive comments
and `X-Accel-Buffering: no` so the NAS reverse proxy does not hold events in a
buffer.

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
| `heartbeat`         | a tool the agent was running finished      | `tool`                     |
| `needs_human`       | the agent is blocked on the human          | `message`                  |
| `idle`              | the agent finished its turn and is resting | —                          |
| `session_ended`     | the agent is gone (villager leaves)        | —                          |

The village notice board is a bounded cross-agent view of this stream. It keeps
the 30 most recent valid `artifact_produced` events from the viewer's live log
window, newest first, including artifacts from agents who have since left.

## Projection rules (v0)

The villager's state is decided by its **latest** event:

- `task_started`, `tool_called`, `artifact_produced` → **working** (the payload is what
  it is doing right now)
- `heartbeat` → **working**, but it is *liveness only*: it refreshes the clock and
  never becomes the action shown. The villager keeps doing whatever the last
  `task_started` / `tool_called` / `artifact_produced` said, and heartbeats stay out
  of the villager's event log so a long build doesn't bury the real actions. (If the
  only signal in view is a heartbeat, the villager is simply "working".)
- `needs_human` → **at your door**
- `idle` → **resting**
- `session_ended` → removed from the village
- no event for 30 minutes while "working" → shown as **stale** (faded), because a
  villager frozen mid-swing would be a lie
- no event for 12 hours → dropped from the village entirely, whatever the state

These rules have exactly one implementation: `reduce()` in `viewer/projection.js`,
loaded by the viewer and exercised by `tests/projection.test.js` (`node --test`,
see the README).

### Why `heartbeat` and not another `tool_called`

`PreToolUse` says a tool *started*; a 40-minute build then emits nothing until it ends,
so a busy agent and a wedged one look identical and both fade to stale. `PostToolUse`
gives us a second true fact — *that tool finished* — so the emitter sends it for every
tool. It is deliberately **not** reused `tool_called` semantics: re-emitting
`tool_called` on completion would claim a tool was invoked when none was, doubling
every tool in the log and lying about what the agent is doing right now. A separate
type keeps the stale rule (last signal wins) working unchanged while letting the
projection ignore heartbeats when deciding what to *show*.

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

## Where the work happens (places)

The same latest event also decides *where* the villager stands. `tool_called` is
classified into a verb (`WebSearch`/`WebFetch` → researching, `Read` → reading, …)
and some verbs belong to a shared building rather than the villager's own house:

| verb          | place         |
|---------------|---------------|
| researching   | the library   |

Rules that keep this honest:

- Only the latest event decides. Research → the villager walks to the library;
  any other work → it walks home. Nothing else moves it.
- Two villagers at the same place take **distinct, stable slots**. A slot is held
  until its villager leaves, so an arrival or a departure never nudges anyone else
  — that would be movement no event asked for.
- A doorway is lit only while somebody is genuinely working there; a villager at
  the library leaves its own house dark.
- Losing signal is not travel: a **stale** villager stays where it was, faded.

`fixtures/library-walk.jsonl` is the worked example — see `fixtures/README.md`.

## Knocks reach you off-screen

A villager at your door is useless if the village isn't on a screen you are looking
at, so the server can forward it. When it ingests a `needs_human` event and
`BURROW_NOTIFY_URL` is set, it asynchronously attempts a POST at that URL:

- body: `<villager name> · <project>` then the `message`, verbatim
- headers: `Title: <name> is at your door (<project>)`, `Tags: door`,
  `Priority: high` — ntfy reads these, so `https://ntfy.sh/<topic>` works as-is;
  any endpoint that accepts a POST works too (Telegram via a small relay)
- `BURROW_NOTIFY_TOKEN` (optional) adds `Authorization: Bearer …` for private topics
- `BURROW_NOTIFY_TIMEOUT` (optional, default 5 s) bounds the request

Two properties matter more than the format:

- **A delivered knock is claimed once.** Identity is `(agent_id, ts, message)`, not
  arrival, so an emitter retry or replay does not duplicate a successful delivery.
  A failed delivery remains eligible for a later replay.
- **Notifying never blocks ingest.** The POST runs on a daemon thread and swallows
  every error; a down notification service must not slow an agent down or lose an
  event. Unset `BURROW_NOTIFY_URL` and nothing is sent at all.

This is a transport for something that already happened. It never invents a knock —
one `needs_human` event, one notification.

## The one rule, restated for implementers

Never emit an event for something that did not happen, and never render state that no
event supports. Filler is forbidden on both sides of the log.

## v0 emitter: Claude Code hooks

`hooks/emit.py` adapts Claude Code hook callbacks to this protocol:

| Claude Code hook                          | burrow event        |
|-------------------------------------------|---------------------|
| `UserPromptSubmit`                        | `task_started`      |
| `PreToolUse` (any tool)                   | `tool_called`       |
| `PostToolUse` (`Write`/`Edit`/`MultiEdit`/`NotebookEdit`, with a `file_path`) | `artifact_produced` |
| `PostToolUse` (every other tool)          | `heartbeat`         |
| `Notification`                            | `needs_human`       |
| `Stop`                                    | `idle`              |
| `SessionEnd`                              | `session_ended`     |

Every `PostToolUse` now produces exactly one event, so any tool run — however long —
keeps the villager alive. Only the write-like tools claim an artifact: a finished
`Read` has a `file_path` too, and reporting that as `artifact_produced` ("crafted
README.md") would be a lie; it is a heartbeat.

The emitter must never break the agent: it swallows all errors and always exits 0.

Env vars: `BURROW_URL` (POST target, see Transport) and `BURROW_TOKEN` (ingest secret,
see Ingest auth — sent as a bearer header, omitted when unset). **Resident agents** — services
that outlive any one Claude session, like a bot running `claude -p` per message —
set `BURROW_AGENT_ID` (stable villager identity, e.g. `life-agent`) and optionally
`BURROW_PROJECT` (label). For a resident, `SessionEnd` maps to `idle` instead of
`session_ended`: the session's process died, but the agent-as-service is still
home, resting.
