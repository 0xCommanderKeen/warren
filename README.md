# Arcadia

Arcadia is the village client for [Burrow](https://github.com/0xCommanderKeen/burrow). React owns the interface and Phaser owns the canvas, connected through the EventBus bridge supplied by `phaserjs/template-react`.

This repository consumes Burrow's complete, versioned `StateEnvelope` contract. The vendored [`complete-v1.json`](src/contract/fixtures/complete-v1.json) is the current compatibility fixture. Arcadia accepts `snapshot` and `reset` envelopes whose `snapshot.schema_version` is `1`, then renders `snapshot.villagers` directly. It rejects unsupported schema versions before applying any state.

Arcadia is allowed to read only these Burrow endpoints:

- `GET /state` — complete snapshots, or `204` when unchanged.
- `GET /state/stream` — server-sent `snapshot` and `reset` envelopes.

It does not consume `/events` or recreate Burrow's projection decisions. The deployed client uses the same-origin `/burrow` prefix; `?backend=` can select another compatible prefix. During development, Vite proxies `/burrow` to `http://127.0.0.1:8737`; set `BURROW_URL` to use another Burrow origin.

The app loads `/state`, then opens `/state/stream` from the returned generation and cursor. Reconnects first catch up from the last applied boundary and then reopen the stream. Unsupported `schema_version` values replace the village with a visible contract-mismatch screen instead of applying unknown state.

## Steward writes

[`src/steward/StewardClient.js`](src/steward/StewardClient.js) is Arcadia's only Steward write boundary. It owns job posts, approval decisions, resident declarations, and manual routine runs. Feature code supplies declarations or decisions to that client; it must not call Steward directly.

The Steward URL is supplied when the client is created; the shipped client reads `?steward=` and then `VITE_STEWARD_URL`, falling back to same-origin. The approval prompt hands its bearer token to `setCredentials`; the value can be replaced or cleared at runtime and lives only inside that client instance. Arcadia never writes it to web storage, markup, URLs, or logs. A `401` clears the rejected token.

Writes are deliberately non-optimistic. A valid Steward receipt leaves the client in `awaiting_confirmation`; the village continues to render Burrow's last complete snapshot. Pass later Burrow snapshots to `confirm`. Only the matching projected job, approval, routine run, or resident appearance releases the write lock. The client never reads Burrow's internal `/events` endpoint.

Only Steward's pre-mutation `401` and `422` refusals release the lock for retry. Network failures, other statuses, malformed receipts, and server/proxy failures are ambiguous and keep writes blocked, because sending again could duplicate work. If the request or receipt retains an exact usable identity, a later matching Burrow snapshot can reconcile it without another write. Ambiguous routine runs remain blocked because Steward's receipt does not expose the projected run ID.

## Development

Requires Node.js 24 and pnpm 11.

```sh
pnpm install
pnpm dev
```

The running app always loads live Burrow state; the vendored fixture is compatibility test data only. The village map is authored as a Tiled JSON export in `public/assets/village.tmj`; tile properties define collision, and the `Places` object layer defines homes, the shared visitor Lodge, street, and work anchors. Placeholder SVG tiles keep the asset pipeline replaceable while the scene architecture settles.

## Verification

```sh
pnpm test
pnpm build
```

`pnpm test` parses Burrow's complete fixture and checks that an unsupported contract version produces a visible error instead of a partially rendered village.

The production Compose/nginx shape, cutover, smoke check, and rollback procedure are in [docs/deployment.md](docs/deployment.md).
