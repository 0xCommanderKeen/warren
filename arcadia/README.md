# Arcadia

Arcadia is the village client for [Chronicle](https://github.com/0xCommanderKeen/burrow). React owns the interface; a small Three.js renderer presents an original, expandable miniature village. Buildings, people, paths, and scenery are generated from geometry in this repository.

Arcadia consumes Chronicle's complete, versioned `StateEnvelope` contract. The compatibility fixture is Chronicle's own [`complete-v1.json`](../chronicle/tests/fixtures/state-contract/complete-v1.json), read in-tree through [`src/contract/fixtures/complete-v1.js`](src/contract/fixtures/complete-v1.js) — Arcadia keeps no copy of it, so it cannot drift. Arcadia accepts `snapshot` and `reset` envelopes whose `snapshot.schema_version` is `1`, then renders `snapshot.villagers` directly. It rejects unsupported schema versions before applying any state.

Arcadia is allowed to read only these Chronicle endpoints:

- `GET /state` — complete snapshots, or `204` when unchanged.
- `GET /state/stream` — server-sent `snapshot` and `reset` envelopes.

It does not consume `/events` or recreate Chronicle's projection decisions. The deployed client uses the same-origin `/chronicle` prefix (`/burrow` 301s there for a release after warren#361); `?backend=` can select another compatible prefix in development only. Production builds always use `/chronicle`, so a link cannot substitute another state feed. During development, Vite proxies `/chronicle` to `http://127.0.0.1:8737`; set `CHRONICLE_URL` to use another Chronicle origin.

The app loads `/state`, then opens `/state/stream` from the returned generation and cursor. Reconnects first catch up from the last applied boundary and then reopen the stream. Unsupported `schema_version` values replace the village with a visible contract-mismatch screen instead of applying unknown state.

## Steward writes

[`src/steward/StewardClient.js`](src/steward/StewardClient.js) is Arcadia's only Steward write boundary. It owns job posts, approval decisions, resident declarations, and manual routine runs. Feature code supplies declarations or decisions to that client; it must not call Steward directly.

The Steward URL is supplied when the client is created. **The shipped client is same-origin, always**: `?steward=` and `VITE_STEWARD_URL` are read only under `import.meta.env.DEV`, so Vite eliminates both from a built bundle, and the client refuses to send credentials to a base that is not this origin anyway (warren#256). A deployed Arcadia needs neither — `deploy/nginx.conf` proxies Steward's write routes behind the deployed origin. In `pnpm dev` they are how you reach a Steward at all, since the dev server proxies Chronicle but not Steward. The approval prompt hands its bearer token to `setCredentials`; the value can be replaced or cleared at runtime and lives only inside that client instance. Arcadia never writes it to web storage, markup, URLs, or logs. A `401` clears the rejected token and reopens the prompt so an operator can replace it without reloading the page.

Writes are deliberately non-optimistic. A valid Steward receipt leaves the client in `awaiting_confirmation`; the village continues to render Chronicle's last complete snapshot. Pass later Chronicle snapshots to `confirm`. Only the matching projected job, approval, routine run, or resident appearance releases the write lock. The client never reads Chronicle's internal `/events` endpoint.

Only Steward's pre-mutation `401` and `422` refusals release the lock for retry. Network failures, other statuses, malformed receipts, and server/proxy failures are ambiguous and keep writes blocked, because sending again could duplicate work. If the request or receipt retains an exact usable identity, a later matching Chronicle snapshot can reconcile it without another write. Ambiguous routine runs remain blocked because Steward's receipt does not expose the projected run ID.

## Time of day

Lighting follows the viewing machine's real local clock, blending dawn, day, dusk, and night. It is presentation only: Chronicle supplies no simulated weather or clock. Working or resting occupants illuminate their buildings after dark; labels and state indicators remain readable. Reduced-motion preferences pause movement by default.

## Development

Requires Node.js 24 and pnpm 11.

```sh
pnpm install
pnpm dev
```

The running app loads live Chronicle state; compatibility fixtures are never bundled as village activity. To preview another Chronicle instance:

```sh
CHRONICLE_URL=http://dxp2800:8738 pnpm dev --host 127.0.0.1
```

## Village interface

Residents have stable homes; visitors share expandable lodges and all working agents use one shared workshop. The square, archive, and noticeboard provide places to explore people, records, and attention requests. New neighborhoods expand the land without moving existing homes. Validated layout allocations persist locally; browser storage contains identifiers and plot positions, never credentials or event history.

Click a home, lodge, or the workshop to enter its furnished cutaway interior. Actual occupants appear inside, with an accessible roster and their real work details; Back to village restores the outdoor view. Indoor agents appear outdoors only while travelling. Personal desks and beds keep their positions across population changes, room visits, and reloads. Vacant reserved places remain furnished without inventing occupants. Click occupied furniture or its work card to inspect the agent’s recorded task, activity, and artifacts. Select an agent or building in the scene or accessible directory to inspect real work, routines, artifacts, and history. Outdoor labels show current occupancy; the Buildings tab provides expandable occupant previews before entry. The workshop, communal lodge, and personalized homes have distinct architecture. Search people, projects, and buildings, follow an agent, reset or rotate the camera, pan and zoom, pause motion, or choose lighter rendering. Unsupported WebGL and context loss leave the directory and operational controls usable. Keyboard users can reach the same selections through the directory and civic navigation.

Agents move along paths only when projected state changes their destination. Initial snapshots do not replay invented travel or announcements. Arrivals and completed-work notices derive from new Chronicle snapshot boundaries. Follow mode opens the selected agent’s current room as its projected location changes; the location breadcrumb and overview button make navigation explicit. Camera position, rendering quality, and motion preferences persist locally after validation.

Enter the archive to search completed tasks, artifacts, and journals by recorded project and agent. Inspect metadata, copy paths, or open recorded HTTP(S) artifact links. File contents and task-to-artifact associations are not supplied by the current contract, so the archive does not invent previews or associations.

Agent dossiers show exact pending questions and approval options beside recorded failure context. Contextual and global approval views share one authentication/submission state and the existing non-optimistic Steward boundary. Recovery links lead to recorded context; presentation does not invent retry endpoints or change authority.

See [the village design and verification notes](docs/living-village.md) for module boundaries and performance evidence.

## Verification

```sh
pnpm test
pnpm build
pnpm exec playwright install chromium
pnpm test:browser
```

`pnpm test:browser` runs the production build at desktop, tablet, and phone widths, checks canvas fitting, overflow, selection, motion controls, stream updates, populations of 0/5/25/100 agents, graphics-context failure, and the production backend restriction. Set `PLAYWRIGHT_CHANNEL=chrome` to use an installed Chrome locally. CI installs Chromium and runs both suites.

`pnpm test` parses Chronicle's complete fixture and checks that an unsupported contract version produces a visible error instead of a partially rendered village.

The production Compose/nginx shape, cutover, smoke check, and rollback procedure are in [docs/deployment.md](docs/deployment.md).
