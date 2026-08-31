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
Clients vendor those fixtures and check their copies for drift on a schedule. Burrow's CI
does not install, run, or otherwise verify any client; each client is responsible for
testing its parser against its vendored fixture.

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

The checked-in [state-shape binding](state-shape.json) fingerprints `VillageState` and
only the wire models reachable from it. Its canonical JSON representation ignores schema
documentation and object-key order, so route, prose, formatting, and unrelated OpenAPI
changes do not move the fingerprint. The Python contract test compares that fingerprint
with the current snapshot models. When the shape changes, the author must deliberately
choose one of these review-visible responses:

- bump `SCHEMA_VERSION` when an unupdated client cannot safely consume the new shape; or
- for a compatible additive change, run
  `uv run python scripts/export_state_contract.py` and review the re-recorded fingerprint.

Run the exporter after either choice so the artifact always binds the current shape to its
current version.

Contract fixtures must carry the current `SCHEMA_VERSION`; a version bump therefore fails
until every checked-in fixture has been updated. This guard is Burrow's compatibility
enforcement mechanism after clients leave this repository. The fixtures remain portable
test data, not a substitute for the version-to-shape binding.

Generation orders published snapshots within a running cursor namespace. Cursor identifies
the durable log position and namespace. Neither is a schema-version substitute.

## Client adapter policy

The snapshot itself is the shared client interface, and it is the whole interface. Burrow
publishes it unchanged and ships no client code that reshapes it first. Clients should
render the documented arrays directly; there is no published hydration package, and clients
must not recreate projection or domain decisions from `history` or `/events`.

A client may keep a local adapter for its own rendering convenience — resident lookups,
scene-local fields, wrappers its older rendering modules still expect. Those shapes are that
client's business and are not part of the state contract. The rule that matters here is the
direction of change: anything two clients need belongs in the versioned Python snapshot, and
anything one client's presentation needs belongs inside that client. That keeps one
projection authority no matter how many clients exist.
