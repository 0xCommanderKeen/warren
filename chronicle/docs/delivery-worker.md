# Independent emitter delivery

Warren #559 separates observed runtime activity from transport liveness. A hook in
managed mode commits redacted semantic history to the existing primary outbox and
returns under the existing one-second aggregate host boundary. It then records a
replaceable presence observation. No hook starts a delivery process or waits for
HTTP. See [spool guarantees](spool.md) for the unavoidable disk-stall/host-deadline
tradeoff. A filesystem unable to persist anything cannot provide a durability promise.

## Capacity and timing

One supervised service owns a machine/user spool through `delivery-owner.lock`.
Claude and Codex use the same owner; duplicate services cannot send concurrently.
Up to eight destinations have independent concurrent delivery turns. Each turn sends
presence/health first, followed by up to 32 ordered historical records (60 KiB).
Requests time out after two seconds; turns resume after 250 ms. Failure retries use
exponential backoff with jitter, capped at 30 seconds, independently of hooks. Retry
state survives restart in `delivery-status.json`. A destination rejected for bad
credentials or invalid events retains its queue for repair; restarting is not a replay.

Supported sustained arrival rate: **five events/second per primary target**, events
small enough to fit 32 in a 60 KiB batch, at 450 ms successful HTTP response latency.
A turn transports 32 records in about 1.15 seconds including presence and polling.
The controlled 200-record backlog plus 150 new events test demonstrates return to zero.
The 1,024-record test fills the production record capacity and recovers without new
input. Network and filesystem overhead consume additional headroom in real installations.

The existing primary bound remains 1,024 records / 5 MiB across primary destinations.
Mirrors have a separate 1,024-record / 5 MiB spool and cannot retire or consume primary
capacity. Capacity evictions increment explicit cumulative loss counters. Queue depth,
oldest observation, failures and retry time are reported per target, identified by a
redacted hash. Permanent invalid-event rejection blocks the ordered queue until repaired;
it is not silently discarded. Health exposes only a fixed error vocabulary: `dns`,
`connect`, `timeout`, `authentication`, `invalid_event`, `http`, `disk`, `worker`.

Presence has 128 latest agent/session slots per producer; evictions are counted. Session
identity is the runner-qualified agent ID plus the runner's session/thread ID. A session's
persisted initial sequence is its epoch; subsequent sequences advance under the same
stable file lock. The terminal `ended` state cannot be revived within that session.
Chronicle durably fences epochs/sequences and takes the newest session epoch per agent.
Old history never updates this separate authority. Presence expires **60 seconds after
the runtime callback**, not receipt or replay. A long tool call without another callback
therefore becomes unknown, never a fabricated heartbeat. A fresh observed callback has
a **five-second target** with a healthy, connected service within supported capacity
(up to eight destinations). Retry/backoff periods do not meet that connected-service target.

Producer health is unknown **30 seconds after the last observation** even if the producer
cannot report its failure. A queue older than **10 seconds** is delayed despite successful
acknowledgements. Capacity/loss counters remain visible after recovery; they are cumulative
and require investigation, not automatic clearing. Chronicle stores at most 64 producer/
target reports, each with 128 session fences. Expired, evicted sessions cannot establish
freshness by replay: observations retain their original times. Managed history without a
retained presence fence is projected stale. Chronicle recomputes expiry on snapshot/SSE
reads; clients replace complete snapshots on reconnect.

## Installation and lifecycle

Deploy consumers first: Arcadia and Townhall accept snapshot versions 1 and 2. Then deploy
Chronicle version 2 (`/telemetry`, `/events/batch`, presence and producer health in snapshots),
then install workers. An old Arcadia rejects the new top-level health field, which is why
this is a schema-version change.

On macOS or Linux, with the existing `CHRONICLE_URL` and optional `CHRONICLE_TOKEN` set:

```sh
sh chronicle/scripts/install-emitter.sh --service
python3 ~/.local/lib/chronicle-emitter/delivery_service.py status
python3 ~/.local/lib/chronicle-emitter/delivery_service.py restart
python3 ~/.local/lib/chronicle-emitter/delivery_service.py stop
python3 ~/.local/lib/chronicle-emitter/delivery_service.py start
python3 ~/.local/lib/chronicle-emitter/delivery_service.py remove
```

The installer retains a pre-rename `burrow-emitter` installation at its existing path;
use the path it prints. macOS uses a LaunchAgent (`org.warren.chronicle-delivery`) with
RunAtLoad/KeepAlive. Linux uses a systemd user service with Restart=always, enabled at
user service startup. For Linux boot without login, an administrator enables lingering
for that user (`loginctl enable-linger USER`). Credentials are in the user's private
`~/.chronicle/delivery-config.json`, never in a service definition or diagnostics.
Repeat `--service` with new environment values to update credentials and restart.
An ordinary atomic bundle upgrade restarts the installed service. Old sessions continue
calling the stable bundle path and immediately adopt enqueue mode through this marker.

Resident containers use the same generated one-file emitter. Their entrypoint supervises
one `chronicle-emit.py --worker` process when `CHRONICLE_URL` is configured. Its marker and
spools live in the existing persisted `.chronicle` directory. Other container supervisors
may run this same foreground command with their normal restart policy. No pip dependencies
are required for either installed or one-file emitter bundles.

## Recovery and rollback

Check service status and local `delivery-status.json` first. Restore the configured URL
or credentials, then allow the service to drain; do not create new delivery IDs or manually
replay historical semantic events. If a batch response was lost after acceptance, the
stable IDs deduplicate both event append and downstream notification effects on retry.
The server validates the whole batch before appending its ordered prefix.

Before rollback, stop/remove the worker; removal renames the managed-mode config and
preserves primary outbox generations, journals, local history and presence. Reinstalling
an older emitter adopts the unchanged primary outbox format. Preserve the mirror spool
and telemetry files for a later upgrade. Roll Chronicle back next, then consumers. Rolling
back Chronicle while leaving workers active produces explicit failed requests and retains
queues; it does not provide delivery. An old one-file emitter cannot use a managed marker,
so remove/rename that marker before rolling back a resident image.

Do not close the incident solely on repository tests. Record the installed bundle/service,
deployed revisions, controlled interruption, warning expiry and recovery evidence. The
local real-HTTP integration test verifies an installed hook queues before its worker starts,
then drains independently and delivers fresh presence through an SSE reset snapshot.
