# Chronicle ingestion and Village State protocols

Agents (or adapters wrapping them) append one JSON object per line to the event
log. This v0 event protocol is the ingestion and audit contract only. Python
validates and projects that evidence; the production browser never consumes it.

## Projected-state delivery — v1

`GET /state` returns an envelope containing one complete, bounded Village State.
The snapshot carries `schema_version`, `generation`, `cursor`, `evaluated_at`,
Villagers, Resident declarations, tasks, approvals, journals, routines, moods,
diagnostics, capacity declarations, and control capabilities. It contains JSON
values only and is atomically replaceable.

Clients may send `generation` and `cursor` query parameters. An unchanged state
returns 204. A cursor from another log/server generation returns a `reset`
envelope containing the complete current snapshot; otherwise a newer state is a
`snapshot` envelope. `GET /state/stream?generation=N` publishes those same full
envelopes as snapshot SSE messages. Full snapshots are intentional; there is no
delta language.

Clock-dependent transitions are evaluated by Python during polling and snapshot
stream publication, so stale/absent state changes without synthetic events.
Write controls remain non-optimistic: their HTTP result is not village truth;
only a later authoritative snapshot changes visible state.

## Event ingestion transport (v0.6)

Two transports; the event shape is the contract, not the pipe:

- **HTTP ingest** (preferred): `POST <CHRONICLE_URL>/events` with one JSON event as the
  body → 204. The server appends it to its own log. Emitters set `CHRONICLE_URL`; a
  failed POST trips a per-target circuit breaker, appends the privacy-filtered
  event to `~/.chronicle/primary-outbox.jsonl`, and leaves it in the local log.
  Later hooks replay at most 16 pending events per primary, oldest first, before
  their new event. The outbox is capped at 1,024 records and 5 MiB. Rewrites use
  a stable sidecar lock, an fsynced replacement, atomic rename, and directory
  fsync. The live outbox remains the only authority until atomic rename; any
  orphan staging file is discarded, even when it is empty or valid JSONL,
  because syntax cannot prove that the intended generation was complete.
  Every record receives a stable total enqueue order before lock acquisition;
  replay and compaction sort by it (legacy records use event timestamp,
  delivery ID, and target). Lock-contention journals share those aggregate
  caps. One stable transaction lock protects every authority snapshot,
  publication, and retirement. Lock order is thread lock, transaction lock,
  then the nonblocking main lock; auxiliary commits never take the main lock.
  Replacements are fsynced and renamed before superseded journals are removed.
  Torn suffix quarantine retains the newest 8 files within 256 KiB,
  deleting and directory-fsyncing older evidence deterministically.
  The server's delivery-ID acceleration ledger is independently capped at 1,024
  records/5 MiB and uses one same-size atomic replacement (10 MiB physical
  crash-copy ceiling at defaults). Eviction does not weaken replay deduplication:
  a ledger miss consults a local SQLite delivery-ID index. The index reconciles an
  unindexed live suffix, notices archive-set changes, and is discarded and rebuilt
  after missing, incompatible, or corrupt state. It is acceleration only; retained
  live and archive JSONL remain canonical and can reproduce it completely. Normal
  unique ingest performs an indexed membership lookup rather than parsing retained
  rows. Exactly-once ingest therefore applies only while that event/archive authority
  is retained, not globally forever.
- **Mirrors**: the same event is POSTed to every `CHRONICLE_MIRROR` target as well
  (default `http://127.0.0.1:8737`, i.e. a local dev server). A mirror never
  acknowledges or drains a primary's outbox. Mirrors carry
  `CHRONICLE_MIRROR_TOKEN`, never `CHRONICLE_TOKEN`.
- **Local JSONL file**: append one event per line to `~/.chronicle/events.jsonl`
  (a single `write()` of < 4 KB is atomic enough on macOS/Linux). Used when
  `CHRONICLE_URL` is unset, or as the fallback above. If rotation owns the live
  log lock, the hook appends to a fsynced deferred journal behind a stable
  sidecar lock. Recovery atomically hands that journal to an immutable replay
  generation; per-record replay IDs in the live log make a crash after replay
  fsync but before generation cleanup idempotent. Active and replay generations
  share a newest-first retention ceiling of 1,024 records and 5 MiB encoded.
  Oldest victims are durably diagnosed before bounded atomic replacement removes
  them. A crash can leave disjoint active and replay authority while the next
  compaction writes one same-size pending copy: three capped generations, or
  15 MiB at defaults. Torn suffixes are non-authoritative and deterministically
  retain only the newest 8 files within 256 KiB, for a 15.25 MiB total ceiling
  excluding small stable lock files. Retained deferred records
  replay once while their IDs remain in the live log. This is not global
  exactly-once retention: capacity victims are intentionally dropped, and an ID
  aged out by log rotation no longer suppresses a surviving replay copy.

Independent primary and mirror targets are contacted concurrently, with at most
eight target workers. Each primary's separate durable queue carries its last
attempt generation, so successive hook processes fairly reach every configured
primary instead of repeatedly selecting the first eight. Each POST has a 750 ms
timeout. The actual hook runs all transport work—including persistence,
diagnostics, and local fallback—in a helper process; the host reserves bounded
time inside that same deadline to kill it and poll nonblocking for reaping
at the shared 1 second deadline. This bounds host-hook waiting even when a
filesystem syscall does not return. Work committed before termination remains
durable, and atomic files remain on their previous authoritative generation, but
the event currently being handled can be absent if the helper is terminated
before its first durable commit. No finite synchronous deadline can also promise
completion of a stalled durable write. Configured primaries beyond the worker cap
are durably queued; excess best-effort mirrors are explicitly reported rather
than silently omitted. Every error is swallowed and the hook exits zero. Bounded
payload-free diagnostics serialize cross-process updates, keep exact counters,
and retain only the 20 newest records in
`~/.chronicle/transport-diagnostics.json`.

Primary requests carry a random `X-Burrow-Delivery-ID`. A retry retains that ID;
the server records accepted IDs in a fsynced sidecar ledger and returns 204
without appending a duplicate. The ledger survives restart and live-log rotation.

HTTP ingest requires one decimal `Content-Length` from 1 through the configured
event limit. Missing, duplicate, non-decimal, non-positive, or oversized lengths
receive a stable 400/413 response and close that connection; bytes with an
untrusted length are never reinterpreted as a keep-alive request.

## Ingest auth

The village never lies, so nothing on the tailnet may write to it unasked — a forged
event is a lie with extra steps. Ingest is protected by one shared secret:

- The server reads `CHRONICLE_TOKEN`. **When set**, `POST /events` must present it as
  `Authorization: Bearer <token>` (preferred) or `X-Burrow-Token: <token>`; anything
  else gets `401 unauthorized` and is not logged. Comparison is constant-time, and an
  empty/whitespace-only value counts as unset.
- **When unset**, ingest is open — exactly today's behavior, which is what local-only
  mode (`python3 serve.py` with no `CHRONICLE_URL`) relies on.
- **GET is never gated.** `/state`, `/events`, `/villagers` and `/transport/status` stay open;
  the token guards writes, not reads. The event log is still a map of everything the
  fleet does, so the server stays off the public internet either way.

Emitters send `CHRONICLE_TOKEN` from their own env. A 401 is treated as just another
failed POST: circuit breaker trips, the event is appended to the local JSONL file. **A
wrong token loses no events** — only their remoteness — which is what makes the rollout
below safe.

### Rollout order

The token must never be required before it is sent, or in-flight agents silently fall
back to local logs and the village looks emptier than the fleet is.

1. **Server first, token unset.** Deploy the `CHRONICLE_TOKEN`-aware `serve.py` with the
   var *not* set. Behavior is unchanged; every existing emitter keeps working.
2. **Emitters next.** Roll `CHRONICLE_TOKEN=<secret>` out to every emitter (Mac hooks,
   `life-agent` container, any other resident). A server with no token set ignores the
   header, so emitters can be updated one at a time with no coordination.
3. **Server last, token set.** Once every emitter sends the secret, set `CHRONICLE_TOKEN`
   on the server and restart. Ingest is now closed; anything still unconfigured degrades
   to its local log rather than disappearing.

Rotating the secret runs the same loop in reverse: unset on the server, re-issue to
emitters, set again.
`GET /events?since=<cursor>` remains available for internal audit and diagnostics.
It returns complete JSONL records after that position and supplies the next cursor
in `X-Burrow-Cursor`. It is not a production client transport; clients use the
projected-state endpoints described above.

Server-issued cursors are opaque, versioned values with four explicit identity layers:
`v1:<boot-id>:<device>:<inode>:<generation>:<offset>`. The random boot ID changes
for every constructed HTTP server instance; device and inode identify the open live
file; generation increments when that instance rewrites the inode in place during
rotation; offset is the next complete JSONL byte. Offset zero is still formatted
with the full identity, including for an empty or not-yet-created log. Any identity
mismatch produces a reset and complete replay of the current live log. Thus a cursor
from before a restart cannot alias the new server even if the inode and generation
repeat. Legacy numeric and pre-v1 structured cursors are accepted where syntactically
valid, but are reset-only because they cannot prove boot identity. Malformed,
negative, or oversized cursors receive HTTP 400.

Boot identity belongs to the HTTP server lifecycle, not module import state. Multiple
server instances in one process have distinct IDs; one instance retains its ID across
in-process log rotation. If a server socket is constructed before a process fork,
the parent retains its namespace while every child refreshes to a distinct,
process-bound boot ID before serving. That child ID then remains stable across its
requests and in-process log rotations.

`GET /events/stream?since=<cursor>` likewise remains an internal raw-event diagnostic
stream. Production live updates use `GET /state/stream`, which sends complete
`snapshot` envelopes. A cursor-namespace change is represented by a complete `reset`
envelope, allowing a client to replace all rendered state atomically.

Closing a browser tab, navigating away, or a proxy closing an SSE socket is an
expected lifecycle event: broken-pipe, connection-reset, and connection-aborted
errors end that handler quietly. Other I/O errors remain visible as server faults.

`GET /transport/status` returns counters for ingest deduplication and notification
delivery pressure. Before returning 204, accepted knocks enter a separate fsynced
journal; a journal failure returns 503 so the emitter retains the delivery. Workers
recover undelivered knocks from it at server startup, after restart, and after
in-memory queue saturation. Append and recovery coordinate on a stable sidecar
lock. Recovery atomically renames the active journal to an immutable replay
generation and fsyncs the directory before releasing appenders; a concurrent
append therefore lands in a new active generation. Replay generations survive
crashes and are safe to scan repeatedly because durable delivered/drop ledgers
make claims idempotent. Before external delivery, a knock takes one of 32 stable
shard locks, rechecks both terminal ledgers, performs the POST, and durably
records its outcome before release. Every POST carries a stable, hashed ASCII
`X-Burrow-Delivery-ID` derived from the knock identity so capable receivers can
deduplicate retries. The shard suppresses concurrent sends, but receiver acceptance
before the local delivered ledger fsync is at-least-once across process crash.
Issue #38's exactly-once contract concerns primary event replay, not knock forwarding.
A durable delivered-key ledger prevents a duplicate delivery ID from knocking
again while that key remains in the retained knock authority. Forwarding uses two
workers, a 64-item memory queue, and at most three attempts. The journal,
delivered ledger, and terminal-drop ledger each retain at most 1,024 keys/records
and 5 MiB (configurable with `CHRONICLE_KNOCK_RECORDS` and `CHRONICLE_KNOCK_BYTES`).
Auxiliary knock state therefore has a 3,072-record/15 MiB logical ceiling. Each
journal can have disjoint active and replay authority plus one pending
publication (15 MiB); each terminal ledger can have its live file plus one
pending replacement (10 MiB each). The truthful aggregate crash-copy ceiling
is therefore 35 MiB at defaults, excluding small stable lock files. Recovery
collapses a surviving journal copy before publishing another replay generation.
Terminal ledgers contain only fixed-size hashed ASCII projections of knock
identities. Terminal publication and journal retirement share one stable
transaction order: already-terminal sources are compacted before another ledger
insertion can evict their suppression, then the new outcome is published, then
its source is retired. A crash after outcome publication can leave both copies;
recovery converges them without exposing the source for another POST. A capacity
victim is fsynced to the terminal-drop ledger before atomic compaction removes
it, so restart cannot resurrect it. Status `delivered` and `dropped` are the
current retained counts read from those authoritative ledgers, not lifetime
totals; they survive restart and can decrease at bounded-retention eviction.
Retries, failures, and saturation are current-process counters. The browser
runtime polls this report through its live status interface.

## Event shape

```json
{
  "v": 0,
  "ts": "2026-08-24T14:03:22.114Z",
  "source": "claude-code",
  "agent_id": "claude-code:9f2c81aa-…",
  "project": "chronicle",
  "cwd": "/Users/miha/Life/NAS/warren/chronicle",
  "type": "tool_called",
  "payload": { "tool": "Read", "detail": "README.md" }
}
```

### Strict v0 validation contract

An event is accepted and projected only when all of these checks pass.
`protocol.validate_event` is the single validator used by HTTP ingestion and
`village_state.project_village`; its fixture matrix lives in
`tests/fixtures/protocol-v0-validation.json`.
The stricter journal observation fields are additionally shared through
`tests/fixtures/journal-observations.json`.

- The record and `payload` are JSON objects, not nulls, arrays, or scalars.
- `v` is the integer `0`; `ts` is a real UTC instant written exactly
  `YYYY-MM-DDTHH:mm:ss.sssZ`. Protocol v0 supports Gregorian years `0001`
  through `9999`; ISO year `0000` and signed or expanded years are invalid.
- `source`, `agent_id`, and `project` are non-empty strings. Optional `cwd` is a
  string when present.
- `type` is one of the types below. Unknown types are invalid in v0.
- Required payload strings are non-empty: `task_started.prompt`,
  `tool_called.tool`, `tool_failed.tool`, `artifact_produced.artifact`, and
  `needs_human.message`. Job events require non-empty `task_id` and `title`;
  claims and completions also require `claimant`, exactly equal to the top-level
  `agent_id` that Steward emits them under. Optional `parent_task_id`, when present on
  any job event, is a non-empty string. Known optional detail fields are strings,
  except that `needs_human.detail` is admitted as an extension value so a malformed
  structured attempt can still degrade to its plain message. The approval projection
  requires that value to be an object or null before it offers controls;
  `stop_hook_active`, when present, is boolean.
- `task_posted.required_skills` follows Steward's exact `list[str]` contract. The
  list may be empty and individual strings may be blank or whitespace-only; Chronicle
  preserves those values instead of rejecting or normalizing valid evidence.
- Steward's own lifecycle facts are validated as strictly as the job events, and
  `source: "steward"` is the whole of their authority. `task_delegated` names both ends —
  `from`, exactly equal to the emitting `agent_id` because the carrier is the villager
  that walks, `to`, and the `route` the letter was delivered into — always carries
  `parent_task_id` explicitly (`null` starts a chain; blank is invalid, unlike the job
  events where the field is simply absent), and a non-negative integer `depth`.
  `task_session_finished` repeats the close contract (`task_id`, `title`, `claimant`
  equal to `agent_id`, non-empty `artifacts` strings) and adds the late run's `run_id`,
  `outcome`, finite non-negative `duration_s`, and the `reason` its claim was gone.
  `resident_restarted` carries a non-empty `reason` and an `attempt` counted from one,
  plus an optional non-empty `supervisor`. `chat_message_dropped` carries the `route` and
  `address` that were knocked on, who knocked (`from`), and the `reason` they were not
  answered — and never what they said — plus an optional `suppressed`, a non-negative
  integer counting the other knocks that record stands for.

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
| `needs_human_resolved` | Steward recorded the human's decision   | `request_id`, `decision`, `decided_by`, `action` |
| `idle`              | the agent finished its turn and is resting | —                          |
| `session_ended`     | the agent is gone (villager leaves)        | —                          |
| `routine_started`   | Steward began a declared routine           | `routine`, `run_id`, `trigger` |
| `routine_finished`  | that run ended successfully                 | `routine`, `run_id`, `outcome`, `artifacts`, `duration_s` |
| `routine_failed`    | that run ended unsuccessfully                | `routine`, `run_id`, `error`; optional `duration_s` |
| `task_posted`       | Steward accepted a job                       | `task_id`, `title`, `required_skills`, `posted_by` |
| `task_claimed`      | a resident atomically claimed that job       | `task_id`, `title`, `claimant` |
| `task_done`         | the claimant finished the job                | `task_id`, `title`, `claimant`, `artifacts` |
| `task_failed`       | the claimant failed or its lease expired      | `task_id`, `title`, `claimant`, `reason` |
| `task_session_finished` | a claimant's session reported back after losing its claim | `task_id`, `title`, `claimant`, `run_id`, `outcome`, `artifacts`, `duration_s`, `reason` |
| `task_delegated`    | Steward accepted a handoff and put it in somebody's inbox | `task_id`, `title`, `from`, `to`, `route`, `parent_task_id`, `depth` |
| `resident_restarted` | Steward's watchdog took a resident down and brought it back | `reason`, `attempt`; optional `supervisor` |
| `chat_message_dropped` | somebody knocked on a resident's chat route and was deliberately not answered | `route`, `address`, `from`, `reason`; optional `suppressed` |
| `journal_written`   | Steward observed a successful close produce a real readable daily file | `routine`, `day`, `path` |

Routine events are projected into a separate bounded ledger keyed by agent, routine,
and run id, and are also first-class villager activity. A valid start can create or
refresh a villager and reads as working; a finish reads as resting; a failure reads as
failed. All three remain in the villager's bounded visible history, including for a
scheduled resident that emits no runner-authored session events. Human descriptions
name the routine and terminal outcome or error, with deterministic seconds when a
duration is present. A ledger start without a matching close becomes stale after 30
minutes; malformed routine payloads are diagnosed and skipped instead of partially
rendered.
Within a run, the canonical start is the earliest producer timestamp (with a stable
field tie-break), and the canonical terminal is the latest producer timestamp. A
failure beats a finish at the same timestamp. A terminal before the canonical start
does not close the run. The newest canonical start selects the current run, so delayed
append replay cannot roll the ledger or villager back; append order is retained only
for visible history and for ordering independent kinds of activity. Lifecycle identities
are structured `(agent_id, routine, run_id)` tuples, never delimiter-concatenated text.
All string tie-break fields are compared fieldwise by Unicode scalar value; optional
numeric duration fields compare by presence and then numeric value. The village keeps
at most 80 canonical run identities per agent independently of its at-most-80 visible
events. A close without a retained matching start remains hidden (while bounded close
authority may match a later start), and canonical start/terminal witnesses consume
slots inside the visible 80-record transport allowance rather than extending it.
Only events whose source is exactly `steward` are routine lifecycle evidence. This is
Steward's `EVENT_SOURCE` contract; a `routine_*` event from any other producer is
diagnosed and ignored, including for run-now acknowledgement.

### Steward's lifecycle facts

`task_delegated`, `task_session_finished` and `resident_restarted` are ordinary villager
activity: they enter the villager's bounded visible history and read as sentences — “handed
“Draft the letter” to codex:keeper”, “reported back on “Research X” after losing the claim”,
“was restarted (attempt 2): container was not running”.

A late session report describes the *run*, not the row: the lease sweep remains the
authority on the task, so `task_session_finished` never claims, closes or reopens one.

`task_delegated` is the exception that is both at once. It is the delegator's visible
action *and* the event that opens the row, because Steward writes a handoff into the same
table as a posted job — open, unclaimed, addressed to one resident. So it folds into the
`tasks` ledger as that row's origin: `state: "open"`, `posted_by` the delegator, no
required skills, and `assignee` naming who it is for. Both are the `agent_id` spellings
the event carries, not the resident ids Steward's own store addresses a handoff by, so
`assignee` compares directly against a later `claimant`. Only that resident may claim it,
so an open row with an addressee is not work anybody can take. Rotation follows the same
rule: whatever keeps a handoff — the ledger, the villager's history, mood — keeps the
newest transition on its row with it, because an origin retained alone would show claimed
or finished work as open.

`chat_message_dropped` is **ambient**, the one class of event that is filed under a
villager without being that villager's action. It records that an outsider knocked on a
resident's chat route and was deliberately not answered, and it is not evidence the
resident is alive, present, or working. So it never creates a villager, never keeps one
in the village, and never decides its state, its visible line, its clock or its mood
anchor: a stranger messaging a resident's bot at three in the morning must not make the
village show that resident at work. It rides along in the history of a villager that
exists for its own reasons, and — because a resident may have no villager at all when a
stranger finds its bot — every drop is also a bounded `diagnostics` record carrying
`kind: "chat_message_dropped"`, the agent and project, the `route` and `address` knocked
on, who knocked (`from`), the `reason`, the `suppressed` count (zero when the event carried
none) and the event `ts`. Those named fields only: the
raw record still reaches that villager's history exactly as it arrived, which is why
Steward keeps the stranger's text out of the event in the first place.

Rotation holds the same line: `retention` imports the reducer's ambient set rather than
mirroring it, so a knock is never carried forward as a villager's state witness and a
departed villager cannot be resurrected by somebody ringing its bell.

It is also the one event type an *outsider* causes, which is why its **volume** is bounded
as deliberately as its meaning (warren#278). A knock is a diagnostic and a retained event
like any other, so without a share of its own a scanner that finds a resident's bot decides
what an operator can see: the newest 200 diagnostics would fill with knocks, and a
villager's own tools, tasks and sessions would age out of its retained history early.
Neither is data loss — but both are an outsider choosing what the projection shows, which
is the thing it is otherwise careful about.

So every channel a knock lands in is *split* rather than merely bounded, by one shared rule
(`village_state.ambient_share`, which `retention` imports the way it imports the ambient set):
the fleet's own records are served first out of everything but the outsider's floor, and the
outsider then takes whatever is genuinely left. The floors are `ambient_events_per_agent` (8
of a villager's 80 retained events), `ambient_events_per_villager` (4 of the 40 rendered on
its card) and `ambient_diagnostics` (40 of the snapshot's 200). A floor rather than a
ceiling, deliberately: a knock storm alone still fills a channel nobody else wants, so "the
newest 200" stays true of a full one. Both halves keep their newest records and append order
survives the split. `capacity` publishes the two projection floors; rotation's is in
`retention-policy.json`.

Steward bounds the same storm at the other end: it records one knock per stranger per door
per reason per catch-up window and counts the rest into `payload.suppressed`, an optional
non-negative integer saying how many *other* knocks that one record stands for. A record
therefore stands for `1 + suppressed` knocks, and the count is carried into the diagnostic so
a fold that shows "one line per sender" can still show a true total. The two halves are
complementary, and this one is what holds when the limiter is outrun — by a scanner rotating
sender ids, by a daemon that restarted, or by a Steward too old to have a limiter at all.

### Journal-written observation

`journal_written` is emitted only by `source: "steward"`. Its `agent_id` and
`project` are the scheduled resident identities from Steward's manifest; the
runner is not substituted. `routine` is a 1–128 character lowercase slug matching
`^[a-z0-9][a-z0-9-]*$`. `day` is a real Gregorian `YYYY-MM-DD` date in the
routine's `schedule_tz` (years 0001–9999). `path` is 1–2048 Unicode scalar values
(code points), not UTF-16 code units. Its first and last scalar may not be one of
the Unicode White_Space values U+0009–U+000D, U+0020, U+0085, U+00A0, U+1680,
U+2000–U+200A, U+2028, U+2029, U+202F, U+205F, or U+3000. C0 controls
U+0000–U+001F and DEL U+007F are forbidden anywhere; other characters,
including internal U+0085 and astral scalars, are preserved literally. Unpaired
UTF-16 surrogate code points U+D800–U+DFFF are not Unicode scalars and are
rejected. The final slash- or backslash-separated segment must be exactly
`<day>.md`. The path is
escaped text evidence, never a URL, link, or fetch target. Additive payload
extensions are permitted.

The semantic key is `(agent_id, day)`. First valid append owns it; equal immutable
`project`/`routine`/`path` replays do not refresh recency or animation, while the
first incompatible replay is retained as a diagnosed collision and cannot replace
the canonical fact. Producer timestamps never choose ownership. The journal fold
keeps the 40 highest `(day, agent_id)` keys globally, comparing the Gregorian day
first and then `agent_id` by Unicode scalar value. This gives same-day ties a
deterministic cross-runtime order independent of arrival grouping. Once full, its
lowest retained key is a monotonic frontier: an event at or below that frontier is
ignored, so an evicted key cannot return, manufacture a new canonical identity, or
trigger another eviction. First append still owns every retained key. One
representative incompatible append and one collision diagnostic are kept per
retained key; later conflicts and exact replays consume no diagnostic capacity.
Collision diagnostics are derived from those retained conflicted records and are
therefore not part of a shared FIFO: all 40 retained keys may each expose one even
under malformed-input pressure, and evicting a key removes its diagnostic. Malformed
validation details have their own newest-40 bound while the total malformed counter
remains exact. Fleet labels both counts so a bounded detail list is not presented as
the complete malformed history.
Reset discards the authority and rebuilds it from empty. Rotation derives this
bounded journal authority from the full pre-rotation log before allocating the
remaining ordinary evidence inside the global 4,000-line transport
window, then preserves original append order while merging the retained records.
Village rotation and grouped bootstrap use the same bounded projection-witness
rule over the complete validated segment. Ordinary agents still enter through the
newest 4,000 raw-record window; an agent with valid Steward routine evidence also
brings its later ordinary superseders from before that window. Terminal and expired
agents consume no ordinary witness capacity. If routine identities alone exceed
the cap, their newest routine append chooses the candidate set. Newest live
candidates are admitted first
with their latest state, latest lineage declaration, and heartbeat action support;
remaining capacity keeps newest visible history, at most 80 events per agent — divided so
that ambient events are guaranteed 8 of those and get no more than 8 when the agent's own
records want the rest, which is what stops a knock storm ageing a resident's own tools,
tasks and sessions out of its history (warren#278). The
composed projection has at most 4,000 witnesses. At the boundary, an agent whose
indivisible support does not fit is omitted, then older optional history is
truncated. Preselected journal and approval facts participate in the same set union,
so an overlapping pending knock is charged once rather than reserved and selected
again. All retained facts preserve append order.

The public JavaScript projection selector accepts repeated raw JSON records because
each parse has a distinct append position. A direct-object source may not repeat the
same object identity; such aliases are rejected deterministically at the selector
boundary instead of being mistaken for one append position.

For 60 seconds after the canonical event timestamp, a matching valid Resident works
at its own home, “writing the journal.” One ownership resolver serves projection,
resident detail, directory recency and Fleet links. It accepts an exact declared
`agent_id`, or one unambiguous declared project only for a lineage-free root. A
`payload.parent_agent_id` on the observation or any retained child lineage makes a
shared-project match a Visitor fact, including when lineage is appended before or
after the journal. Invalid, ambiguous and unmatched declarations likewise remain
Fleet-only and diagnostic. Pending approvals and subsequently appended
ordinary events keep their normal precedence; `session_ended` removes the villager
immediately. Expiry recomputes the ordinary projection from its original evidence
rather than refreshing it. Visitors and unmatched or invalid resident declarations
never gain a house or writing animation. The event records observation only: it does
not fabricate body text, refresh the direct Steward journal read, send a notification,
or make a request.
`duration_s` is deliberately optional on failures: Steward's watchdog closes vanished
runs without inventing a duration it cannot know, and Chronicle renders that absence as
`duration unknown`. When present, durations must be finite, non-negative numbers in
both protocol adapters.

The browser uses monotonic ingestion order only to decide recency under bounded ledger
capacity. Close-only events live in a separate 200-run staging area, selected by the
same terminal comparator and ingestion recency. They do not consume or displace the
200 renderable routine keys. A later exact matching start atomically promotes both
events into the renderable ledger, including when events arrive in separate SSE
publications at saturation. Staging overflow is reported as capacity pressure, not as
malformed telemetry.
The browser reports a poll as `recovering` from the moment it starts until a successful
`/events` response has been projected with its ending cursor. Only then is the transport
`polling`. A cached cursor never makes an in-flight or failed recovery observable, so Run
Now stays disabled without requesting credentials, declarations, or execution while the
transport is `recovering`, `reconnecting`, or `disconnected`.
Within one agent/routine/run lifecycle, the terminal with the greatest valid
event timestamp is authoritative, so a delayed older replay cannot replace newer truth.
Equal-time conflicts use a stable field-order tie-break that conservatively prefers
`routine_failed`. A run-now request boundary selects one fresh
manual start using the resident manifest's match contract: exact-agent manifests require
that agent, while project manifests accept the distinct Steward runner for that project.
Once that exact agent/routine/run identity is confirmed, its exact terminal event may close
the acknowledgement after a cursor-generation reset; unrelated starts and terminals remain
ineligible. For an unconfirmed request, the first reset/bootstrap publication is never
acknowledgement evidence: Chronicle advances the request boundary to that publication's ending
cursor, rejects all snapshot-contained lifecycle records, and considers only later records in
the same new cursor generation. This makes a reset recoverable without attributing replay.
The UI may navigate to the project's currently active owner without narrowing
this manifest-based correlation. If no matching start arrives within 15 seconds, the
request remains an active, explicitly unacknowledged uncertainty and Run Now stays
disabled. Steward lifecycle events do not include the POST request id, so retry cannot
be attributed safely while that outcome is uncertain. The same page permits no retry
for that correlation until an exact fresh start and its real terminal evidence resolve
the original request; late exact lifecycle evidence remains eligible indefinitely.
The acknowledgement table holds at most 200 correlations. If all slots contain unresolved
requests, Run Now refuses before opening credential UI or sending a request, restores its
enabled control, and announces the capacity refusal as an alert. Unresolved correlations are
never evicted to make room. Each unresolved correlation also remembers at most 20 close-only
run identities. Eviction advances a per-correlation terminal-evidence-loss watermark. A later
matching start at or before that watermark, without retained exact terminal evidence, becomes
`indeterminate` rather than falsely `running`; Run Now remains disabled. Exact terminal
evidence retained or received later still resolves the state to completed or failed.

The village notice board is a bounded cross-agent view of this stream. It keeps
the 30 most recent valid `artifact_produced` events from the live log
window, newest first, including artifacts from agents who have since left.

The separate job board is reconstructed only from valid Steward `task_posted`,
`task_delegated`, `task_claimed`, `task_done`, and `task_failed` events. A row opens on a
post or on a handoff: a post requires the complete Steward payload, including non-empty
`posted_by`, a handoff requires both ends and its route, and a failure requires its
reason. The board is keyed by `task_id`, one row per job: a second origin for a row that
already exists restates it and never opens another card. It supplies the canonical title,
skills, poster and addressee — it does not say the job is untaken, so it cannot touch the
state or the claimant. An untaken row's clock is its posted age and the newest origin
supplies that too; once a transition has moved the row the clock belongs to that
transition. A transition read before the event that opens its row is held until that event
arrives, because rotation retains a row's newest origin and its newest transition and a
restated origin is the later of the two. Held evidence is not an invented job: a transition
whose row never opens is discarded, as it always was. Rotation's board selection keeps at
most 24 task identities, also keyed by `task_id`. Capacity is applied after every valid
event, including each record in a grouped bootstrap/reset response. A later transition can
reintroduce an identity whose origin was already evicted, but cannot recover that missing
metadata; only a genuinely re-observed post or handoff can. That 24 bounds what the board
selection keeps, not what the retained log can still show: a handoff is also the
delegator's own activity, so villager retention keeps one on its own terms even when the
board selection evicted its row, and such a row keeps its newest transition too rather
than reappearing as an untaken job. The snapshot's own `tasks` capacity is the outer
bound in every case. This makes one-event SSE delivery, grouped
replay, and rotation/reset batch-invariant without inventing skills. The greatest event
timestamp is the current state. Equal-millisecond facts use a constant-space total order:
origin (post or handoff), lease expiry, claim, ordinary failure, then done, with stable
event identity deciding conflicts within one kind. Exact duplicates compare equal. This
preserves Steward's expiry-then-reclaim hand-off while even 10,000 distinct
same-millisecond transitions retain only the latest origin and transition. Those two ends
are tracked apart: a row's standing is read from its newest transition, not its newest
event of any kind, so an origin restated after a close neither unfinishes that row nor
takes capacity from an open one. A later origin supplies the canonical title, skills and
addressee even when its claim arrived first. Open and claimed jobs remain.
When a claim or terminal event is retained without the event that opened its row, Chronicle
renders required skills as unavailable rather than inventing an empty requirement set.
An observed empty skills list renders as “no required skills”. Every blank or whitespace-only
entry in a non-empty list renders as an accessible “unnamed skill” marker, one marker per
entry, so it cannot be confused with either an empty list or unavailable orphan metadata.
Steward's `task_failed` with `reason: "lease_expired"` means its lease sweep has reopened
that task: Chronicle therefore renders it open again, clears the former claimant, and retains
the reason/former claimant as truthful retry context. A present former claimant links to
that villager, an absent one is marked absent, and missing claimant evidence is explicitly
unknown rather than invented. Other failures and done jobs remain
visibly terminal for 15 minutes after their close event and then leave. Relative ages and
terminal expiry advance from the wall clock even when no event arrives; this refresh changes
only the task-list region, preserving the focused post form and its draft.
The browser performs one strict v0 parse/validation pass and gives every projection the
same explicitly branded accepted batch. The trusted task fold rejects an unbranded batch;
the raw public fold is fail-closed unless supplied the shared strict validator. Task-shaped
rejections retain task diagnostics, but globally invalid records can never reach the board
through a second handwritten validator. A cursor reset
discards the entire reduced
board before folding the grouped baseline, so work absent from that authority cannot haunt
the square.

Posting is a direct browser-to-Steward `POST /jobs`; Chronicle's server remains a reader.
The Tailnet URL and bearer token live only in JavaScript memory and the password field is
cleared when its dialog closes. Chronicle draws no optimistic task. It requires non-empty
`task_id` and `request_id` values from Steward's `202` response, then acknowledges only an
exact matching `task_posted` event beyond the validated pre-request cursor. Another task, an old replay,
or reset baseline cannot acknowledge it. Only Steward's `401` authentication gate and
`422` request validation are contractually before `/jobs` mutates its store, so only those
HTTP refusals are definitive and retryable. A `5xx`, other HTTP/proxy status, network failure,
invalid acceptance, or telemetry reset is explicitly ambiguous and blocks duplicate retry.
If an ambiguous response—including a malformed `202` envelope—supplies an independently
valid non-empty task identity, Chronicle retains only that safe identity and a later exact valid
event reconciles it; the malformed envelope itself is never acknowledgement. An invalid task
identity remains unidentified ambiguity. A valid `202` that arrives after the request-start
deadline retains its validated `task_id` and `request_id` for exact reconciliation, while the
tracker records that the deadline elapsed and never relabels the response as timely success.
The absence of matching
evidence for 15 seconds from request start is an explicit timeout. The deadline transition
notifies the renderer while the HTTP request is still in flight. Only already-staged or future
exact post-boundary `task_posted` evidence may then confirm the job and unblock another post;
the late response alone cannot rewrite that already-visible truth or clear the operator's draft. Exact
validated evidence staged before an
SSE readiness marker is replayed to correlation before recovery polling advances past its
cursor, so a connection error or invalid readiness marker cannot leave a visible accepted
job to time out. If more than 24 distinct posts arrive before Steward's response identifies
the requested task, correlation becomes explicitly indeterminate instead of evicting possible
proof. While a request is active, a timed-out request is still awaiting HTTP or is known
accepted, or its outcome is ambiguous/indeterminate, the accessible form is disabled to
prevent an unsafe duplicate; a definitive refusal remains visible and retryable. This bounded
table retains at most 40 post attempts and refuses before sending when every slot is unresolved. A `401` clears the
rejected in-memory credential and exposes accessible change/clear controls so an operator
can correct it without reloading; credentials never enter storage, rendered markup, or logs.

| `POST /jobs` outcome | Mutation certainty | Retry policy |
|---|---|---|
| `401` | definitely rejected by the auth dependency | correct credentials, then retry |
| `422` | definitely rejected by request validation | correct the form, then retry |
| `202` with valid acceptance | accepted; await exact `task_posted` | blocked until exact evidence |
| other HTTP status, invalid response, timeout, or network failure | may have mutated | blocked; reconcile only an identified exact event |

### Structured approval knocks

`needs_human` remains backwards compatible. A payload with only `message` is the
same plain-text knock as before. If any of `action`, `detail`, `options`, or
`request_id` is present, the approval projection requires the complete Steward
shape:

```json
{
  "message": "May I send the note?",
  "request_id": "…",
  "action": "send_email",
  "detail": {"to": "anna@example.com", "subject": "Thursday"},
  "options": ["approve", "deny", "edit"],
  "expires_at": "2026-08-26T10:00:00.000Z"
}
```

`action` is Steward's lowercase action slug, `detail` is a required key whose value
is an object or explicit null, and
`options` is a non-empty ordered list of `approve`, `deny`, and `edit`. Steward
preserves repeated entries, and Chronicle renders one accessible button per entry in
the same order with index-keyed focus identity.
Detection is by payload shape, never by `source`, so Codex, Claude, or another
emitter can raise the same real request. A partial or malformed structured attempt
is still a valid legacy `needs_human`: Chronicle shows its `message`, emits a bounded
diagnostic, and offers no buttons.

Valid requests are bounded to 40 identities and queue per villager newest first.
The immutable lifecycle identity is the exact `(request_id, resident agent_id,
project, action, detail, options, message, expires_at-presence/value)` request:
objects compare independent of key order and JSON numbers compare in the shared
browser-consumer number domain (`1` equals `1.0`), while array/option order remains
meaningful. Wire identity strings are never trimmed during correlation.
Steward owns `request_id` as a global primary key, any protocol producer may emit
the request, and only `source: "steward"` may emit its close. If a corrupt or combined
log reuses one request ID with any incompatible immutable field, Chronicle keeps
bounded diagnostic evidence but quarantines the ID-only controls; neither identity
can rewrite the question/options, inherit its close, or be submitted ambiguously.
A pending request owns the doorstep even if later activity arrives and even after
the ordinary 12-hour activity window. Once that exact pending lifecycle is known,
the first subsequently appended valid `needs_human_resolved` with the same
`request_id`, resident `agent_id`, project, and action releases it. Producer
timestamps are descriptive and never reorder this authority. Resolution events must
come from `source: "steward"` and their decision is exactly `approve`, `deny`, or
`edit`. A close appended before its request is unknown at that point: it is ignored
with a bounded diagnostic and cannot later bind when a matching request appears.
Likewise, request duplicates never replace the first appended immutable request.
Exact close replays and all later conflicts are ignored with bounded, deduplicated
diagnostics, so neither an earlier timestamp nor an equal timestamp can replace the
rendered decision. `tests/fixtures/approval-lifecycle.json` and
`approval-identity.json` were written as shared vectors driving the JavaScript
projection against Python rotation. The JavaScript side is gone (warren#219), but
the rules were always rotation's, so `tests/test_retention_approvals.py` enforces
both files against the Python that owns them: identity against
`retention._approval_lifecycle_identity`, lifecycle against the single close
`retention._approval_keep_indexes` carries forward.

The panel keeps at most five newest confirmed request cards (action, detail and
decision) alongside any newer pending queue. Closing one card therefore does not erase
the decision while another remains actionable, and keyed rerenders move keyboard focus
to the next enabled approval control. A resident whose ordinary latest event is
`session_ended` remains terminal after its parked approval closes; the close cannot
resurrect it as resting. Rotation retains that terminal evidence with the approval so
live delivery and reset replay agree.

An option calls Steward directly; Chronicle's server remains read-only:

```http
POST <steward>/approvals/<url-encoded-request_id>
Authorization: Bearer <STEWARD_TOKEN>
Content-Type: application/json

{"decision":"approve"}
```

`edit` sends the deliberately small free-text bridge
`{"decision":"edit","edit":{"note":"…"}}`; building a field-aware editor is out
of scope. Credentials live only in browser memory. Steward records the decision
durably, emits `needs_human_resolved`, and only then returns the first-write `202`
receipt with `status: "recorded"`. That receipt is not resolution evidence. Chronicle
starts a 15-second acknowledgement deadline at click time. Within one telemetry
generation it stages bounded exact post-cursor close evidence, but acknowledges only
the first close selected by the authoritative approval projection for the exact
immutable request identity. The HTTP
request is pending, a definitive
refusal is retryable, and a timeout, telemetry reset, network error, proxy/server
error, or malformed receipt is ambiguous and blocks retry to prevent a duplicate.
`401`, `404`, and `422` are contractually pre-decision and therefore definitive. A
`409` is definitive only when its parsed body is exactly Steward's trusted
`{"detail":{"error":"approval_expired","message":"…"}}` envelope; invalid JSON,
proxy bodies, other codes, or extra envelope fields remain ambiguous and block unsafe
retry. A `200` means a replay of an already-recorded answer; Steward
emits no new event for it, so Chronicle calls it indeterminate and waits for retained
exact log evidence rather than resolving optimistically.

Closing-event evidence uses the same SSE readiness staging as run and job
acknowledgements. Pre-ready evidence is published only at a valid exact `ready`
cursor (or before recovery polling advances past it). Reset, namespace change, and
the shared 4,000-record staging overflow conservatively invalidate correlation. A
reset clears all staged generation evidence and revalidates every possibly-delivered
attempt against the replayed exact lifecycle and resolution fingerprint. An
acknowledged attempt remains acknowledged only if the identical close survives;
missing, pending, collided, or different replay authority becomes explicitly
indeterminate/ambiguous and remains retry-blocked. Only observing that exact
authoritative close again after the reset can recover acknowledgement.

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
- no event for 12 hours → dropped from ordinary village activity. A valid structured
  approval is the exception: its villager remains at the door until its exact close.
- an **ambient** event (`chat_message_dropped`) → nothing at all. It is somebody else's
  action filed under this villager, so it decides no state, refreshes no clock, and
  cannot by itself put a villager in the village. See
  [Steward's lifecycle facts](#stewards-lifecycle-facts).

These rules have exactly one implementation:
`village_state.project_village()`, exercised by `tests/test_village_state.py`.

### Why `heartbeat` and not another `tool_called`

`PreToolUse` says a tool *started*; a 40-minute build then emits nothing until it ends,
so a busy agent and a wedged one look identical and both fade to stale. `PostToolUse`
gives us a second true fact — *that tool finished* — so the emitter sends it for every
tool. It is deliberately **not** reused `tool_called` semantics: re-emitting
`tool_called` on completion would claim a tool was invoked when none was, doubling
every tool in the log and lying about what the agent is doing right now. A separate
type keeps the stale rule (last signal wins) working unchanged while letting the
projection ignore heartbeats when deciding what to *show*.

## Operational mood glyph

`village_state._mood()` is the sole mood reducer. It consumes validated,
append-ordered events and has no DOM, random input, or client-owned threshold. Each agent's
greatest retained valid timestamp is anchor `A`; every displayed duration is
non-negative **log age as of A**. Consequently an unchanged log is byte-stable
across wall-clock ticks, and events for another agent cannot age a mood. Ambient
events are excluded for the same reason they decide no state: a stranger knocking is
not this agent's evidence, so it neither anchors nor ages the reading.

The reducer observes exactly four operational signals. Its rolling terminal
stream uses `(A-24h,A]`, append order, failures `tool_failed`, `routine_failed`,
and `task_failed`, and successes `heartbeat`, `routine_finished`, and
`task_done`; the trailing failure streak saturates at three, while the separate
failure count counts every rolling-window failure and displays three or more as
`3+`.
Work density is the sum of one maximum-weight witness in UTC quarter-hour
buckets `floor(A/15m)-7..floor(A/15m)`: task/routine starts and claims weigh 3,
artifacts and journals 2, and tool calls and heartbeats 1. Human interaction is
the append-newest authoritative exact approval close or root `task_started`
from `claude-code`/`codex` with no `parent_agent_id`. The fourth signal is the
oldest unresolved canonical structured approval (collisions remain unresolved)
or plain/fallback knock not superseded by a later ordinary lifecycle event.
Mood evaluates canonical requests independently of the approval panel's
40-request presentation capacity, so an older pending request cannot disappear
behind newer questions.
Orphans, jobs, routines, journals, child/custom starts, and invalid v0 records
do not acquire meanings outside those exact lists.

Enough evidence means an unresolved need, a failure streak of at least two, or
six distinct signal witnesses spanning at least 30 log-minutes with at least
two of failure, workload, and interaction observed. Scores are: failure
`unobserved=0, 0=+2, 1=-1, 2=-3, 3=-5`; workload `active=+2,
saturated=-2`, otherwise zero; interaction `recent=+1, old=-1`, otherwise zero;
and unresolved age `≤1h=-1, >1–6h=-3, >6h=-5`. Equality stays in the named
lower-risk range. Precedence is insufficient `?`, need older than 6h `!`,
failure streak 3 `×`, saturated work `▲`, then total `≥4` steady `●`, `1..3`
active `◆`, or `≤0` watchful `◇`.

`retention.carry_forward(...).witnesses["moods"]` preserves
the anchor, the complete append-ordered terminal frontier currently in
`(A-24h,A]`, one maximum-weight event per relevant bucket, latest interaction,
approval/plain-knock authority, and six threshold witnesses selected from the
reducer's same deduplicated contributing set in append order. The complete
frontier is necessary because a timestamp-disordered future anchor may expire
an append-later success and expose any earlier outcome. They also retain a
backup six-witness selection with unresolved
plain, fallback, and structured needs excluded, so a future superseder or exact
close cannot consume the only evidence-threshold witness. Resolved structured
approvals cannot be made exact forever in
fixed space: unrestricted request IDs let a future incompatible append
invalidate any chosen suffix and reveal an arbitrarily old decision. Chronicle
therefore makes that information limit explicit. Full-log and retained-log
reduction both admit at most 256 irreducible approval facts plus the latest
root prompt per agent, and the encoded capsule is capped at 32 KiB. Replayed immutable knocks, orphan closes, and superseded
roots are folded away before this bound is evaluated. Within
those count and byte bounds they are exact. Crossing either sets a durable global `overflow` bit,
discards all authority, copy, and order manifests, and renders `? authority history
uncertain` with `authority.complete=false`; it never presents a guessed suffix
or retained partial signal/score as exact. Once a request ID collides, every
candidate remains unresolved and every close for that ID is ignored for Mood
through the rest of the source epoch. An archive can retain the raw history, but exact answers after
unbounded future ID reuse require an external unbounded index or a bounded
request-ID namespace.

Compaction stores bounded authority in one reserved `mood-authority-v1`
capsule. Its logical `events` have safe decimal `ordinals`; `copies` names authority
ordinals also represented by raw witnesses. `raw_ordinals` contains only Mood's
independently selected raw witnesses; fixed-width `raw_indexes` locates that
sparse set among records retained for other projections, and fixed-width
`raw_count` separates the rotated source epoch from genuinely later appends.
The logical state has exactly `events`, `ordinals`, `copies`, `raw_ordinals`,
`raw_indexes`, `raw_count`, `overflow`, and `observed`; the direct form adds only
`_burrow_internal`, while the encoded envelope has exactly `_burrow_internal`,
`encoding`, and `graph`. All ordinal and index arrays, including `copies`, are
strictly increasing in canonical append order. Missing, duplicate, reordered,
or surplus fields invalidate the entire capsule.
Thus approval, task, journal, or presence retention cannot consume Mood's byte
budget or change its append interleaving.

Capsules use the shallow `typed-binary64-v1` graph for both emission and the
32 KiB measurement. Every node has an explicit null, boolean, string, array,
object, or number tag; children precede their parent and containers refer to
child indexes. Object keys sort by their ASCII-escaped UTF-16 JSON spelling.
The root is the last node and is never referenced; every earlier scalar or
container node has exactly one incoming reference. Decoders iteratively
re-encode the decoded value and require exact graph equality, so unused nodes,
shared scalars, shared containers, and compressed DAGs cannot amplify beyond
the measured 32 KiB canonical tree.
Number nodes carry the exact big-endian IEEE-754 binary64 bits, with both zeros
normalized to positive zero and every non-finite result (including a legal
extreme JSON exponent such as `1e400`) normalized to one `nonfinite` token.
The graph is unambiguous with user objects because tags are interpreted only
inside the reserved internal envelope. It stays shallow regardless of public
detail depth, so identity, freezing, capsule emission, parsing, structural
comparison, and byte measurement all stay iterative.

One implementation produces these bytes: `typed_json.py`. It had a JavaScript
twin, `viewer/typed-json.js`, that had to agree byte for byte; the viewer was
deleted (warren#219) and the obligation is now to data already on disk. Rotated
logs and their archives carry `typed-binary64-v1` capsules, and a capsule that
fails to decode is dropped silently along with Mood authority history — so the
escaping and the binary64 tokens remain a storage format. `tests/test_typed_json.py`
pins them with golden strings, including the surviving binary64 vectors in
`tests/fixtures/mood-capsule-parity.json`. Durable notification identity uses a
different encoder in `notification_persistence.py` and is not covered by these
rules. Cycles, repeated direct-object container identities, and
non-JSON direct inputs fail closed before graph expansion without hiding raw
public evidence. Legacy direct capsules retain their defensive
64-container metadata limit; ordinary valid public records and the typed graph
do not inherit it. A capsule is accepted only at physical raw batch index zero, when every copy ordinal maps
to canonically identical capsule and raw events, and there is exactly one
capsule. Malformed, surplus, cross-agent, reordered, or multiple
capsules are ignored atomically and cannot suppress public evidence. Recognized
internal markers never become public rejection diagnostics. The
capsule is transport metadata, not a v0 event: ingestion rejects it,
projections and boards never count it as an event or rejection, and it cannot
create or refresh presence. Browser compaction and Python rotation replace it
atomically. The canonical union of the terminal frontier and every other
ordinary Mood witness is bounded to 160 records per agent. A 161st required
record enters the same durable global authority-overflow state as the authority
count or byte limits; full-log derivation applies this decision too, so it never
presents signals that rotation cannot preserve exactly. The lower 24-hour
boundary is open and the upper boundary closed. Overflow clears only when a
genuinely new complete source epoch replaces the retained history.
The `observed` field records the compact authority cardinality (or the
saturating value 257 after overflow), so discarded roots and duplicate facts
cannot make grouped and incremental overflow decisions diverge. Once overflow
is known, cross-agent lifecycle completion is not attempted; uncertain raw
attachments remain inside the same strict 160-record per-owner ceiling.
Breakdown ages are rendered losslessly through milliseconds, so
threshold-adjacent evidence is never rounded onto the safer side. This evidence
is attached only to an already
projected villager; specialized events never manufacture presence. Every view of
a resident reads the same `Mood` object. The glyph that used to render it,
`viewer/mood-glyph.js`, was deleted with the viewer (warren#219): the object now
travels in the `/state` envelope and each client owns its own presentation
(Townhall renders `mood.name` on the fleet page). What Chronicle owes a client is
the object, not the widget — stale alpha stays an unscored presence wrapper
rather than a score, so a client cannot read missing evidence as a calm resident.

## Log rotation

The live log is a window, not an archive. When `events.jsonl` grows past
`CHRONICLE_MAX_LOG` bytes (default 5 MiB, `0` disables), the server rolls it into
`<CHRONICLE_ARCHIVE or archive/>/events-20260824T170430Z.jsonl` and starts the live
file again from the **carry-forward tail** derived from the same latest 4,000
lines the viewer reads: the last 80 visible events of every villager the
projection would still draw — plus its latest liveness-only heartbeat when
present, plus one canonical lineage-bearing record for each active child, and
skipping any whose latest signal is `session_ended` or older than
the 12 h drop window — in their original order. Separately, it keeps the canonical post
and latest transition for the same bounded 24 task IDs the job board selects, using the
same per-event capacity and constant-space equal-time order, tracking the newest origin and
the newest transition in separate slots so a restated origin cannot evict the claim beneath
it. An already-evicted post is not reconstructed merely because a transition for that task
appears later in the retained input.
Task-ID retention crosses agent groups: a central `steward:api` post remains paired with
claim/done/failed/lease-expiry evidence after the claimant session ends.
Structured approvals are independently retained by lifecycle: at most 40 bounded
request records (including one representative incompatible collision). Rotation
preserves the first request and first subsequent matching close by original log index;
later replays/conflicts and unknown closes are isolated from ordinary retention and
discarded, so rotation cannot let an orphan bind to a future knock. For every retained
journal resident, lifecycle authority is selected from the complete segment on both
sides of the journal: a pending request survives whether it precedes or follows the
observation and despite later ordinary activity. Pending requests survive the ordinary
activity drop window;
resolved pairs stay paired, capacity removes whole pairs, and resolution-only evidence
never becomes villager liveness. A retained approval also carries a later ordinary
`session_ended` record so its eventual close cannot manufacture liveness after reset.
That 40-record rule belongs only to approval-panel presentation. Operational
Mood's bounded identity authority and explicit overflow state travel in the
internal capsule above; it
neither expands panel history nor changes the public JSONL event protocol.
Rotation therefore cannot create a ghost knock or resurrect a parked session by keeping
only one part of the authoritative projection.

That tail is exactly the input the rules above consume, so the village and job board render
identically across a rotation/reset: same states, same panel history and no resurrected
task posts. It also means the
live log has a floor of (live agents × 80 events); a very busy fleet can sit
above the threshold, and rotation then waits rather than copying the log next to
itself.

Rotation is checked after every accepted `POST /events` and before every
`GET /events` (local mode has no POSTs — emitters append to the file themselves).
All three paths take the same process lock, and file appends and rotation also
take an advisory lock on the log shared with the bundled emitter. Rotation
archives a snapshot and rewrites the live file in place, retaining its inode so
even an already-open append descriptor continues writing to the live log.
The process-local cursor generation increments only after that rewrite is durable;
connected polling and SSE clients then receive one reset followed by the complete
carry-forward tail, without gaps or duplicate delivery across transport handoff.

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

The mapping is `_place()` in `village_state.py`; tool names and their public
snapshot locations therefore have one Python source of truth. The post office is
ready for email/inbox adapters through their `Email` and `Inbox` tool names.

A place is a destination, not a name: `/state` carries it as a `{ kind }` value —
`home`, `door`, `{ building, id }` or `{ plot }` — and the client resolves that
one value into map geometry. The resolver was `viewer/destinations.js` until the
viewer was deleted (warren#219); Arcadia's `src/game/villageModel.js` does it now.
Coordinates, slots, doorway glow and panel labels all stay downstream of the
single value Chronicle publishes, so a new client inherits the mapping rather
than inventing one.

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
`CHRONICLE_NOTIFY_URL` is set, it asynchronously attempts a POST at that URL:

- body: `<villager name> · <project>` then the `message`, verbatim
- headers: `Title: <name> is at your door (<project>)`, `Tags: door`,
  `Priority: high` — ntfy reads these, so `https://ntfy.sh/<topic>` works as-is;
  any endpoint that accepts a POST works too (Telegram via a small relay)
- `CHRONICLE_NOTIFY_TOKEN` (optional) adds `Authorization: Bearer …` for private topics
- `CHRONICLE_NOTIFY_TIMEOUT` (optional, default 5 s) bounds the request

Two properties matter more than the format:

- **A delivered knock is claimed once.** A producer `delivery_id` is authoritative.
  The plain legacy fallback is `(agent_id, ts, message)`. Without a producer ID, a
  structured request uses a versioned SHA-256 identity over exact agent, project,
  request, action, message and event strings plus JSON-semantically normalized detail,
  ordered options (including repeats), expiry presence/value, timestamp, source and
  protocol version. Terminal ledgers therefore retain no detail or other secret.
  Pre-v3 structured terminal hashes cannot prove which immutable shape they represented,
  so they are intentionally not aliases: the safe upgrade tradeoff is at most one
  re-notification of an old structured event rather than false suppression by a plain
  or differently shaped event. Plain legacy keys and producer delivery IDs remain
  compatible. Identity is never arrival, so an emitter retry or
  replay does not duplicate a successful delivery. A failed delivery remains eligible.
- **Notifying never blocks ingest.** The POST runs on a daemon thread and swallows
  every error; a down notification service must not slow an agent down or lose an
  event. Unset `CHRONICLE_NOTIFY_URL` and nothing is sent at all.

This is a transport for something that already happened. It never invents a knock —
one `needs_human` event, one notification.
For a valid structured approval the push title is its `action` and the UTF-8 body is
its JSON `detail`, so the decision is legible without opening Chronicle. A malformed
structured attempt uses the unchanged legacy villager/project/message notification.
The durable claim, delivery ID, retry, and one-event/one-notification semantics are
identical for both shapes.

## The one rule, restated for implementers

Never emit an event for something that did not happen, and never render state that no
event supports. Filler is forbidden on both sides of the log.

## v0 emitter: Claude Code hooks

`hooks/emit.py` adapts Claude Code hook callbacks to this protocol:

| Claude Code hook                          | chronicle event        |
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

### Installed emitter bundle

`emit.py` depends on its sibling `durable.py`; supported deployments install both
with the fail-open `chronicle-emit` launcher. From the repository root, use the
canonical installer:

```sh
sh scripts/install-emitter.sh
```

Use the stable command `$HOME/.local/lib/chronicle-emitter/chronicle-emit` in hooks.
The launcher locates its sibling files independently of the caller's working
directory and returns zero even if the bundle is incomplete or the emitter hits
an unexpected runtime failure. Transport failures remain handled by `emit.py` and
fall back to its bounded durable local storage.

### One-file bundle, for deployments that cannot carry a directory

**Two shapes, one emitter.** The *installed bundle* above is a directory — `emit.py`,
`durable.py` and the launcher, installed together. The *one-file bundle* below is the
same emitter flattened into a single script, for a host that can only take one file.
Where both are in play, say which; bare "the bundle" is ambiguous.

Some hosts can only take a single file: steward's resident image is built from one
directory with no pip in it, and vendors the emitter into it. `hooks/build.py`
flattens the two source files into one self-contained stdlib-only script for exactly
that case.

```sh
python3 hooks/build.py --output /somewhere/emit.py   # or to stdout, with no --output
```

The artifact is `emit.py` verbatim with its `import durable` block replaced by
`durable.py`'s own source, materialized as a module — no rewriting, no import
analysis, and a traceback still names `durable.py` and the right line. The build is
deterministic: the same two sources produce byte-identical output, which is what lets
a consumer rebuild it and compare rather than trust a checksum somebody recorded.
`tests/test_bundle.py` holds it to being one file, stdlib-only across both embedded
sources, compilable, fail-open, deterministic, and — the assertion that matters, since
the emitter swallows everything by charter — actually able to deliver an event.

It is built on demand and committed nowhere in this repository. The copy in
`steward/docker/resident/burrow-emit.py` is refreshed by `make vendor-emitter` in
`warren/steward/`, and both suites compare it against a fresh build.

> **Both spellings work during the rename.** Every `CHRONICLE_*` variable below
> is also read under its pre-rename `BURROW_*` name, and the same is true of the
> server's settings. The new spelling wins wherever both are set. A hook's
> environment is fixed when its session starts, so sessions already running when
> the emitter is updated keep sending the old names — dropping them would take
> those sessions quiet without any error. The old names go away in a later
> release; nothing has to be renamed in lockstep with a deploy. The installed
> bundle likewise still answers to `burrow-emit` alongside `chronicle-emit`.

Env vars: `CHRONICLE_URL` (POST target, see Transport), `CHRONICLE_TOKEN` (ingest secret,
see Ingest auth — sent as a bearer header, omitted when unset), `CHRONICLE_MIRROR` /
`CHRONICLE_MIRROR_TOKEN` (extra POST targets, see Transport; empty disables). **Resident agents** — services
that outlive any one Claude session, like a bot running `claude -p` per message —
set `CHRONICLE_AGENT_ID` (stable villager identity, e.g. `life-agent`) and optionally
`CHRONICLE_PROJECT` (label). For a resident, `SessionEnd` maps to `idle` instead of
`session_ended`: the session's process died, but the agent-as-service is still
home, resting. Its children remain distinct; a child stop still ends that child,
and its lineage points to the stable resident parent identity.

`CHRONICLE_DETAIL=full|safe|off` is enforced by the shared delivery interface before
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

| Codex hook | chronicle event |
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
For `apply_patch`, Chronicle emits requested paths only when `tool_response` is the exact
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

## Runner setup

This section is the single source for runner hook configuration. Install the
[shared emitter bundle](#installed-emitter-bundle) once. Claude Code invokes the
stable command without a runner option from its documented lifecycle hooks;
Codex uses the same command with `--runner codex`. Do not install separate emitter
copies or configure duplicate user-level hook representations.

The Claude Code hook names and their exact event mapping are listed in
[v0 emitter: Claude Code hooks](#v0-emitter-claude-code-hooks). Point each configured
command at `$HOME/.local/lib/chronicle-emitter/chronicle-emit` and provide `CHRONICLE_URL`
and `CHRONICLE_TOKEN` through protected hook environment configuration.

Merge this hook object into `~/.claude/settings.json`, replacing `REPLACE_ME`.
The repeated command is intentional: every lifecycle callback enters the same
fail-open adapter, which determines the event from the callback body.

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}],
    "PreToolUse": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}],
    "PostToolUseFailure": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}],
    "Notification": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}],
    "SubagentStart": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\""}]}]
  }
}
```

### User-level Codex setup

Follow the [official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).
Install the [emitter bundle](#installed-emitter-bundle), then put the configuration below in
`~/.codex/hooks.json`. Replace the URL and token in the command, or omit them for
local-only fallback logging. Use one user-level representation (`hooks.json` or
inline hooks in `config.toml`), not both.

```json
{
  "description": "Send truthful Codex lifecycle events to Chronicle.",
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME CHRONICLE_MIRROR= \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\" --runner codex", "timeout": 3}]}],
    "PreToolUse": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME CHRONICLE_MIRROR= \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\" --runner codex", "timeout": 3}]}],
    "PermissionRequest": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME CHRONICLE_MIRROR= \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\" --runner codex", "timeout": 3}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME CHRONICLE_MIRROR= \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\" --runner codex", "timeout": 3}]}],
    "SubagentStart": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME CHRONICLE_MIRROR= \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\" --runner codex", "timeout": 3}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME CHRONICLE_MIRROR= \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\" --runner codex", "timeout": 3}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME CHRONICLE_MIRROR= \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\" --runner codex", "timeout": 3}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "CHRONICLE_URL=http://dxp2800:8737 CHRONICLE_TOKEN=REPLACE_ME CHRONICLE_MIRROR= \"$HOME/.local/lib/chronicle-emitter/chronicle-emit\" --runner codex", "timeout": 3}]}]
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
server private, use `CHRONICLE_TOKEN`, protect both the token-bearing config and
emitter file, and review diffs before re-trusting an update. The example disables
the default localhost mirror; enable it knowingly if a local dev server should
receive the same event stream. The hook is observational only and fails open so a
Chronicle outage cannot block Codex.

### Local smoke check

This exercises the real adapter and local fallback without contacting a server:

```sh
smoke_home=$(mktemp -d)
printf '%s\n' '{"session_id":"smoke-1","cwd":"/tmp/chronicle-smoke","hook_event_name":"UserPromptSubmit","prompt":"smoke check"}' |
  HOME="$smoke_home" CHRONICLE_MIRROR= "$HOME/.local/lib/chronicle-emitter/chronicle-emit" --runner codex
python3 -m json.tool "$smoke_home/.chronicle/events.jsonl"
```

The emitted record should have `source: "codex"`,
`agent_id: "codex:smoke-1"`, and `type: "task_started"`. Remove the temporary
directory afterwards.
