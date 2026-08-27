# Arcadia

Arcadia is the village client for [Burrow](https://github.com/0xCommanderKeen/burrow). React owns the interface and Phaser owns the canvas, connected through the EventBus bridge supplied by `phaserjs/template-react`.

This repository consumes Burrow's complete, versioned `StateEnvelope` contract. The vendored [`complete-v1.json`](src/contract/fixtures/complete-v1.json) is the current compatibility fixture. Arcadia accepts `snapshot` and `reset` envelopes whose `snapshot.schema_version` is `1`, then renders `snapshot.villagers` directly. It rejects unsupported schema versions before applying any state.

Arcadia is allowed to read only these Burrow endpoints:

- `GET /state` — complete snapshots, or `204` when unchanged.
- `GET /state/stream` — server-sent `snapshot` and `reset` envelopes.

It does not consume `/events` or recreate Burrow's projection decisions. During development, Vite proxies both allowed paths (the `/state` prefix) to `http://127.0.0.1:8737`; set `BURROW_URL` to use another Burrow origin.

## Steward writes

[`src/steward/StewardClient.js`](src/steward/StewardClient.js) is Arcadia's only Steward write boundary. It owns job posts, approval decisions, resident declarations, and manual routine runs. Feature code supplies declarations or decisions to that client; it must not call Steward directly.

The Steward URL is supplied when the client is created. Its bearer token is supplied with `setCredentials`, can be replaced or cleared at runtime, and lives only inside that client instance: Arcadia never writes it to web storage, markup, URLs, or logs. A `401` clears the rejected token.

Writes are deliberately non-optimistic. A valid Steward receipt leaves the client in `awaiting_confirmation`; the village continues to render Burrow's last complete snapshot. Pass later Burrow snapshots to `confirm`. Only the matching projected job, approval, routine run, or resident appearance releases the write lock. The client never reads Burrow's internal `/events` endpoint.

Only Steward's pre-mutation `401` and `422` refusals release the lock for retry. Network failures, other statuses, malformed receipts, and server/proxy failures are ambiguous and keep writes blocked, because sending again could duplicate work.

## Development

Requires Node.js 24 and pnpm 11.

```sh
pnpm install
pnpm dev
```

The initial screen deliberately uses the vendored fixture. A later transport slice can replace that import with either allowed endpoint without changing the parser or renderer. The village map is authored as a Tiled JSON export in `public/assets/village.tmj`; tile properties define collision, and the `Places` object layer defines homes, the shared visitor Lodge, street, and work anchors. Placeholder SVG tiles keep the asset pipeline replaceable while the scene architecture settles.

## Verification

```sh
pnpm test
pnpm build
```

`pnpm test` parses Burrow's complete fixture and checks that an unsupported contract version produces a visible error instead of a partially rendered village.
