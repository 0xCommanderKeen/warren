# burrow event protocol — v0

Everything in burrow consumes this. Agents (or adapters wrapping them) append one JSON
object per line to the event log. The projection layer never reads agent internals —
only this log.

## Transport (v0.5)

Two transports; the event shape is the contract, not the pipe:

- **HTTP ingest** (preferred): `POST <BURROW_URL>/events` with one JSON event as the
  body → 204. The server appends it to its own log. Emitters set `BURROW_URL`; a
  failed POST trips a 60 s circuit breaker (5 s for loopback, where failure is an
  instant refusal rather than a timeout) and falls back to the local file, so an
  unreachable server never slows an agent down. The breaker is per target.
- **Mirrors**: the same event is POSTed to every `BURROW_MIRROR` target as well
  (default `http://127.0.0.1:8737`, i.e. a local dev server). Delivery to *any*
  target means no local fallback write — the event exists exactly once per log.
  Mirrors carry `BURROW_MIRROR_TOKEN`, never `BURROW_TOKEN`.
- **Local JSONL file**: append one event per line to `~/.burrow/events.jsonl`
  (a single `write()` of < 4 KB is atomic enough on macOS/Linux). Used when
  `BURROW_URL` is unset, or as the fallback above.

HTTP ingest requires one decimal `Content-Length` from 1 through the configured
event limit. Missing, duplicate, non-decimal, non-positive, or oversized lengths
receive a stable 400/413 response and close that connection; bytes with an
untrusted length are never reinterpreted as a keep-alive request.

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

### Strict v0 validation contract

An event is accepted and projected only when all of these checks pass. HTTP
ingestion uses `protocol.validate_event`; browser ingestion uses `validateEvent`
in `viewer/projection.js`. Both adapters run the same fixture matrix in
`tests/fixtures/protocol-v0-validation.json`.

- The record and `payload` are JSON objects, not nulls, arrays, or scalars.
- `v` is the integer `0`; `ts` is a real UTC instant written exactly
  `YYYY-MM-DDTHH:mm:ss.sssZ`. Protocol v0 supports Gregorian years `0001`
  through `9999`; ISO year `0000` and signed or expanded years are invalid.
- `source`, `agent_id`, and `project` are non-empty strings. Optional `cwd` is a
  string when present.
- `type` is one of the types below. Unknown types are invalid in v0.
- Required payload strings are non-empty: `task_started.prompt`,
  `tool_called.tool`, `tool_failed.tool`, `artifact_produced.artifact`, and
  `needs_human.message`. Known optional detail and lineage fields are strings;
  `stop_hook_active`, when present, is boolean.

Invalid HTTP records return 400 and are never appended or notified. Projection
silently ignores the same invalid records. Unknown extension fields remain
allowed.

| field      | meaning                                                                 |
|------------|-------------------------------------------------------------------------|
| `v`        | protocol version, `0` for now                                           |
| `ts`       | UTC ISO-8601 with milliseconds, `Z` suffix                              |
| `source`   | what kind of runner emitted this (`claude-code`, `codex`, `cron`, custom) |
| `agent_id` | stable identity of the villager: `<source>:<session or agent uuid>`     |
| `project`  | human label for grouping (v0: basename of `cwd`)                        |
| `cwd`      | where the agent is working, if it has a working directory               |
| `type`     | one of the types below                                                  |
| `payload`  | type-specific detail, always an object, may be empty                    |

Child lifecycle events may add `parent_agent_id` and `agent_type` to `payload`.
These fields are optional v0 lineage: consumers that know them can relate child and
parent villagers, while existing v0 consumers continue to ignore unknown payload
fields. Parent and child always retain distinct `agent_id` values and lifecycles;
`SubagentStop` emits `session_ended` for only the named child.

## Event types

| type                | emitted when…                              | payload                    |
|---------------------|--------------------------------------------|----------------------------|
| `task_started`      | the agent picks up work                    | `prompt` (truncated ≤140)  |
| `tool_called`       | the agent uses a tool                      | `tool`, `detail` (≤120)    |
| `tool_failed`       | a tool has explicitly failed               | `tool`, optional `error`   |
| `artifact_produced` | the agent writes/edits a file or output    | `artifact` (path)          |
| `heartbeat`         | the agent is known to be working            | bounded tool/phase detail  |
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
- `tool_failed` → **failed**, at home; this does not claim the tool is active
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
present, plus one canonical lineage-bearing record for each active child, and
skipping any whose latest signal is `session_ended` or older than
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

| verb                         | place                         |
|------------------------------|-------------------------------|
| researching                  | the library                   |
| crafting, tinkering          | the workshop                  |
| emailing                     | the post office               |
| delegating                   | another villager's door       |
| every other / unknown verb   | the villager's home work spot |

The mapping is `PLACE_OF_VERB` in `viewer/projection.js`. It consumes the
classification from the existing `VERBS` table, so tool names have one source of
truth. The post office is ready for email/inbox adapters: they classify their tool
as `emailing` in `VERBS` and inherit the shared location without viewer changes.

A place is a destination, not a name: the viewer resolves each one to a
`{ kind }` value — `home`, `door`, `{ building, id }` or `{ plot }` — in
`viewer/destinations.js`, and coordinates, slots, doorway glow and the panel
label all read that one value.

### What delegation may claim

`tool_called` says an `Agent` ran. It does **not** say who the work went to —
the protocol carries no delegate identity, and a subagent has no `agent_id` of
its own until it emits events under one. So the map claims only what the event
supports: the work was handed to *somebody*.

- The destination is drawn from the plots **actually occupied** by villagers in
  the village right now. A villager is never sent to an empty house — that would
  be a delegate the log never mentioned.
- The choice is deterministic in the delegating villager's `agent_id`, and it is
  **held** for as long as that neighbour is still in the village: an arrival or
  a departure elsewhere never nudges it to a different door.
- If nobody else is in the village, the villager stays at its own house. There
  is no one to walk to.
- The panel says "at a neighbour's door" and never names them, because the
  events do not.

When an emitter starts carrying the delegate's identity, `place` can become that
villager and this fallback goes away.

Rules that keep this honest:

- Only the latest event decides. A covered verb walks to its mapped location; any
  other work walks home. Nothing else moves it.
- Two villagers at the same destination take **distinct, stable slots**. A slot is held
  until its villager leaves, so an arrival or a departure never nudges anyone else
  — that would be movement no event asked for.
- A doorway is lit only while somebody is genuinely working there; a villager at
  the library leaves its own house dark.
- Losing signal is not travel: a **stale** villager stays where it was, faded.

`fixtures/meaningful-locations.jsonl` exercises every location end to end — see
`fixtures/README.md`.

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
| `PostToolUse` (`Write`/`Edit`/`MultiEdit`/`NotebookEdit`, with a file or notebook path) | `artifact_produced` |
| `PostToolUse` (every other tool)          | `heartbeat`         |
| `PostToolUseFailure`                      | `tool_failed`       |
| `Notification` (`permission_prompt` or `elicitation_dialog`) | `needs_human` |
| `SubagentStart`                           | `task_started` for that child identity, with lineage |
| `SubagentStop`                            | `session_ended` for only that child identity, with lineage |
| `Stop`                                    | `idle`              |
| `SessionEnd`                              | `session_ended`     |

Every `PostToolUse` now produces exactly one event, so any tool run — however long —
keeps the villager alive. Only the write-like tools claim an artifact: a finished
`Read` has a `file_path` too, and reporting that as `artifact_produced` ("crafted
README.md") would be a lie; it is a heartbeat.

The emitter must never break the agent: it swallows all errors and always exits 0.

Env vars: `BURROW_URL` (POST target, see Transport), `BURROW_TOKEN` (ingest secret,
see Ingest auth — sent as a bearer header, omitted when unset), `BURROW_MIRROR` /
`BURROW_MIRROR_TOKEN` (extra POST targets, see Transport; empty disables). **Resident agents** — services
that outlive any one Claude session, like a bot running `claude -p` per message —
set `BURROW_AGENT_ID` (stable villager identity, e.g. `life-agent`) and optionally
`BURROW_PROJECT` (label). For a resident, `SessionEnd` maps to `idle` instead of
`session_ended`: the session's process died, but the agent-as-service is still
home, resting. Its children remain distinct; a child stop still ends that child,
and its lineage points to the stable resident parent identity.

`BURROW_DETAIL=full|safe|off` is enforced by the shared delivery interface before
the first remote POST or local append. `full` (the default) preserves bounded
detail. `safe` clears `cwd`, reduces artifact and detail sourced from an explicit
file/notebook/path input field to a basename, and replaces free-form commands,
URLs, queries, descriptions/reasons, prompts, messages, and errors with
`[redacted]`. `off` clears `cwd`
and replaces every required private detail value with `[redacted]`. Unknown policy
values fail to `safe`. Identity, project, lifecycle, tool, and lineage remain so
privacy never invents a different lifecycle.

## v0 emitter: Codex hooks

The same `hooks/emit.py` command has runner-specific adapters behind one delivery
path. Its default remains Claude Code; Codex invokes it with `--runner codex` and
emits `source: "codex"` with `agent_id: "codex:<session_id>"`. Subagent callbacks
use `codex:<agent_id>` and retain the parent session identity, `turn_id`, and
`agent_type` in their payload when supplied. Root and parent identities use the
same unbounded canonical value so lineage remains exact even for long session IDs.

| Codex hook | burrow event |
|------------|--------------|
| `SessionStart` | none — a process starting does not say work began |
| `UserPromptSubmit` | `task_started` |
| `PreToolUse` (supported local tool) | `tool_called` |
| `PermissionRequest` | `needs_human` with the bounded approval reason |
| `PostToolUse` (`apply_patch`, exact successful response, with paths in its patch) | one `artifact_produced` per path |
| Successful file/notebook edit completion with a path | `artifact_produced` |
| `PostToolUseFailure`, or a completion with explicit failure evidence | `tool_failed` |
| Other successful or ambiguous completions | `heartbeat` |
| `SubagentStart` | `task_started` for the subagent identity |
| `SubagentStop` | `session_ended` for only the matching subagent, with bounded lineage metadata |
| `Stop` | `heartbeat` with bounded lifecycle metadata for the root session |
| `SessionEnd` | `session_ended` for the root session |

These mappings are deliberately conservative. `PermissionRequest` is the callback
that proves an approval is being elicited, so it knocks; informational callbacks do
not. A failure is emitted only from a failure callback or explicit failure fields,
non-zero exit code, or an unambiguous failed response.

Likewise, another concurrent hook can intercept root `Stop` and continue the flow,
so that callback remains a working heartbeat. `SubagentStop` is scoped to the named
child and removes only that child; root `SessionEnd` removes only the root. The emitter returns an empty JSON object and
never approves, denies, rewrites, blocks, or continues anything.

`PostToolUse` means a tool produced a response, not necessarily that it succeeded.
For `apply_patch`, Burrow emits requested paths only when `tool_response` is the exact
positive success JSON string, `"Done!"`, used by Codex's apply-patch tool output.
Any mixed/partial failure emits `tool_failed` and claims no paths. Missing or
ambiguous success evidence emits one heartbeat and claims no paths.

Successful patches report resulting files: additions and updates produce artifacts,
deletions do not, and an update with `*** Move to:` reports only its destination. A
successful deletion-only patch remains visible as one heartbeat. Tool names in
PreToolUse and PostToolUse payloads are bounded to 120 characters.

The regression fixture in `tests/fixtures/codex-hooks.jsonl` is a fictionalized,
redacted reconstruction of captured hook callbacks. It preserves the documented
wire shape while replacing session, turn, agent, transcript, path, prompt, and tool
content with safe test values. Its fields are aligned with the official Codex hooks
documentation linked below; it is not a raw transcript or a claim that those values
were emitted by a real session.

Omitting `--runner` remains the backward-compatible Claude mode. If `--runner` is
present, its value must be exactly one supported runner and the option may occur only
once. Positional and unknown arguments are not accepted. Invalid, missing, duplicate,
conflicting, or stray arguments exit successfully but emit no event, preventing
malformed Codex setup from being mislabeled as Claude.

### User-level Codex setup

Follow the [official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).
Copy the emitter somewhere stable, then put the configuration below in
`~/.codex/hooks.json`. Replace the URL and token in the command, or omit them for
local-only fallback logging. Use one user-level representation (`hooks.json` or
inline hooks in `config.toml`), not both.

```sh
install -m 700 hooks/emit.py "$HOME/.codex/burrow-emit.py"
```

```json
{
  "description": "Send truthful Codex lifecycle events to Burrow.",
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "BURROW_URL=http://dxp2800:8737 BURROW_TOKEN=REPLACE_ME BURROW_MIRROR= python3 \"$HOME/.codex/burrow-emit.py\" --runner codex", "timeout": 3}]}],
    "PreToolUse": [{"hooks": [{"type": "command", "command": "BURROW_URL=http://dxp2800:8737 BURROW_TOKEN=REPLACE_ME BURROW_MIRROR= python3 \"$HOME/.codex/burrow-emit.py\" --runner codex", "timeout": 3}]}],
    "PermissionRequest": [{"hooks": [{"type": "command", "command": "BURROW_URL=http://dxp2800:8737 BURROW_TOKEN=REPLACE_ME BURROW_MIRROR= python3 \"$HOME/.codex/burrow-emit.py\" --runner codex", "timeout": 3}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "BURROW_URL=http://dxp2800:8737 BURROW_TOKEN=REPLACE_ME BURROW_MIRROR= python3 \"$HOME/.codex/burrow-emit.py\" --runner codex", "timeout": 3}]}],
    "SubagentStart": [{"hooks": [{"type": "command", "command": "BURROW_URL=http://dxp2800:8737 BURROW_TOKEN=REPLACE_ME BURROW_MIRROR= python3 \"$HOME/.codex/burrow-emit.py\" --runner codex", "timeout": 3}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "BURROW_URL=http://dxp2800:8737 BURROW_TOKEN=REPLACE_ME BURROW_MIRROR= python3 \"$HOME/.codex/burrow-emit.py\" --runner codex", "timeout": 3}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "BURROW_URL=http://dxp2800:8737 BURROW_TOKEN=REPLACE_ME BURROW_MIRROR= python3 \"$HOME/.codex/burrow-emit.py\" --runner codex", "timeout": 3}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "BURROW_URL=http://dxp2800:8737 BURROW_TOKEN=REPLACE_ME BURROW_MIRROR= python3 \"$HOME/.codex/burrow-emit.py\" --runner codex", "timeout": 3}]}]
  }
}
```

Open `/hooks` in Codex, inspect the exact command and script, and trust it. Codex
hashes hook definitions, so edits require review again.

### Trust and security review

Treat this as telemetry code. It sends prompt excerpts, tool names and selected
inputs (including shell commands), bounded approval reasons and lifecycle metadata,
successfully applied paths, working directory, project, and stable session/subagent
identifiers. It inspects an `apply_patch` response only to require the exact success
marker; it does **not** transmit tool responses, read transcripts, or send model
responses, file contents, or credentials
unless those secrets were themselves included in a prompt/command/path. Keep the
server private, use `BURROW_TOKEN`, protect both the token-bearing config and
emitter file, and review diffs before re-trusting an update. The example disables
the default localhost mirror; enable it knowingly if a local dev server should
receive the same event stream. The hook is observational only and fails open so a
Burrow outage cannot block Codex.

### Local smoke check

This exercises the real adapter and local fallback without contacting a server:

```sh
smoke_home=$(mktemp -d)
printf '%s\n' '{"session_id":"smoke-1","cwd":"/tmp/burrow-smoke","hook_event_name":"UserPromptSubmit","prompt":"smoke check"}' |
  HOME="$smoke_home" BURROW_MIRROR= python3 hooks/emit.py --runner codex
python3 -m json.tool "$smoke_home/.burrow/events.jsonl"
```

The emitted record should have `source: "codex"`,
`agent_id: "codex:smoke-1"`, and `type: "task_started"`. Remove the temporary
directory afterwards.
