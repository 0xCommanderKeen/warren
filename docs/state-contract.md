# UI state contract

Burrow is the sole projection authority for user interfaces. Clients consume complete,
versioned snapshots; they do not read or reduce the event log.

## Public read endpoints

- `GET /state` returns a `StateEnvelope` containing a complete `VillageState`. A client
  may send its last `generation` and `cursor`; `204` means that snapshot is unchanged.
- `GET /state/stream` sends the same envelopes as server-sent `snapshot` events. Clients
  reconnect with their last generation and cursor. A `reset` envelope atomically replaces
  state when the cursor namespace has changed. Proxies must preserve streaming, disable
  buffering, and allow more than the 15-second keepalive interval.

The checked-in [OpenAPI document](openapi.json) is the machine-readable contract. The
complete examples in `tests/fixtures/state-contract/` are portable contract fixtures.
Run `sh tests/ui-contract.sh` in this repository, or run a prospective client's parser
against those fixtures, before integrating a UI.

`GET /events` is an internal diagnostic and audit interface. It is deliberately absent
from OpenAPI and is not a UI contract. `POST /events` is the authenticated emitter ingest
contract, not a UI read API.

## Compatibility policy

`VillageState.schema_version` versions the snapshot shape, not the HTTP transport.

- Additive changes that old clients can safely ignore may retain the current version.
- Removing or renaming a field, changing its type or meaning, or tightening previously
  accepted values requires a new `schema_version`.
- Clients must reject unsupported versions before applying a snapshot and must replace
  their current state atomically after accepting a `snapshot` or `reset` envelope.
- During a version migration, Burrow must either serve a shape understood by all deployed
  clients or expose an explicitly versioned endpoint. A server must not silently emit a
  breaking shape under an existing version.
- Changes to `docs/openapi.json` and the representative fixtures are reviewed like code.
  The contract test fails when runtime OpenAPI drifts from the checked-in document.

Generation orders published snapshots within a running cursor namespace. Cursor identifies
the durable log position and namespace. Neither is a schema-version substitute.
