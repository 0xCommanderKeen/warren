# Arcadia

Arcadia is the village client for [Chronicle](https://github.com/0xCommanderKeen/burrow). React owns the interface and Phaser owns the canvas, connected through the EventBus bridge supplied by `phaserjs/template-react`.

Arcadia consumes Chronicle's complete, versioned `StateEnvelope` contract. The compatibility fixture is Chronicle's own [`complete-v1.json`](../chronicle/tests/fixtures/state-contract/complete-v1.json), read in-tree through [`src/contract/fixtures/complete-v1.js`](src/contract/fixtures/complete-v1.js) — Arcadia keeps no copy of it, so it cannot drift. Arcadia accepts `snapshot` and `reset` envelopes whose `snapshot.schema_version` is `1`, then renders `snapshot.villagers` directly. It rejects unsupported schema versions before applying any state.

Arcadia is allowed to read only these Chronicle endpoints:

- `GET /state` — complete snapshots, or `204` when unchanged.
- `GET /state/stream` — server-sent `snapshot` and `reset` envelopes.

It does not consume `/events` or recreate Chronicle's projection decisions. The deployed client uses the same-origin `/burrow` prefix; `?backend=` can select another compatible prefix. During development, Vite proxies `/burrow` to `http://127.0.0.1:8737`; set `CHRONICLE_URL` to use another Chronicle origin.

The app loads `/state`, then opens `/state/stream` from the returned generation and cursor. Reconnects first catch up from the last applied boundary and then reopen the stream. Unsupported `schema_version` values replace the village with a visible contract-mismatch screen instead of applying unknown state.

## Steward writes

[`src/steward/StewardClient.js`](src/steward/StewardClient.js) is Arcadia's only Steward write boundary. It owns job posts, approval decisions, resident declarations, and manual routine runs. Feature code supplies declarations or decisions to that client; it must not call Steward directly.

The Steward URL is supplied when the client is created. **The shipped client is same-origin, always**: `?steward=` and `VITE_STEWARD_URL` are read only under `import.meta.env.DEV`, so Vite eliminates both from a built bundle, and the client refuses to send credentials to a base that is not this origin anyway (warren#256). A deployed Arcadia needs neither — `deploy/nginx.conf` proxies Steward's write routes behind the deployed origin. In `pnpm dev` they are how you reach a Steward at all, since the dev server proxies Chronicle but not Steward. The approval prompt hands its bearer token to `setCredentials`; the value can be replaced or cleared at runtime and lives only inside that client instance. Arcadia never writes it to web storage, markup, URLs, or logs. A `401` clears the rejected token and reopens the prompt so an operator can replace it without reloading the page.

Writes are deliberately non-optimistic. A valid Steward receipt leaves the client in `awaiting_confirmation`; the village continues to render Chronicle's last complete snapshot. Pass later Chronicle snapshots to `confirm`. Only the matching projected job, approval, routine run, or resident appearance releases the write lock. The client never reads Chronicle's internal `/events` endpoint.

Only Steward's pre-mutation `401` and `422` refusals release the lock for retry. Network failures, other statuses, malformed receipts, and server/proxy failures are ambiguous and keep writes blocked, because sending again could duplicate work. If the request or receipt retains an exact usable identity, a later matching Chronicle snapshot can reconcile it without another write. Ambiguous routine runs remain blocked because Steward's receipt does not expose the projected run ID.

## Time of day

**Not rebuilt yet.** The retired in-tree viewer tinted the village and Arcadia has not reimplemented it. The rule is kept here because Arcadia is where the village is drawn: Chronicle has no part in it, and no phase, tint or clock reaches a client through the state contract.

The village is tinted by the **real local time of the machine viewing it** — dawn, day, dusk and night, interpolated so there is never a jump. It is a projection of the clock and nothing else: no weather, no seasons, no simulated sky. After dark, a house's windows and doorway light up only while its villager is genuinely home, your porch lights only while somebody is actually knocking, and the working glow, the knock orange and the stale fade all stay legible. Any development override says so on screen, so a pinned tint never passes as the real thing.

## Development

Requires Node.js 24 and pnpm 11.

```sh
pnpm install
pnpm dev
```

The running app always loads live Chronicle state; the contract fixture is compatibility test data only and is never bundled into the app. The village map is authored as a Tiled JSON export in `public/assets/village.tmj`; tile properties define collision, and the `Places` object layer defines homes, the shared visitor Lodge, street, and work anchors. Placeholder SVG tiles keep the asset pipeline replaceable while the scene architecture settles.

## Verification

```sh
pnpm test
pnpm build
```

`pnpm test` parses Chronicle's complete fixture and checks that an unsupported contract version produces a visible error instead of a partially rendered village.

The production Compose/nginx shape, cutover, smoke check, and rollback procedure are in [docs/deployment.md](docs/deployment.md).
