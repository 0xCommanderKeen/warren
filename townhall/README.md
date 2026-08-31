# Observatory

Read-only fleet telemetry for [Burrow](https://github.com/0xCommanderKeen/burrow), built with Vite, React, and Tailwind CSS.

## Develop

Run Burrow on `http://127.0.0.1:8737`, then:

```sh
pnpm install
pnpm dev
```

Vite proxies `/state` and `/state/stream` to Burrow. To point the UI at another compatible deployment, use `?backend=https://burrow.example.test`.

## Verify and build

```sh
pnpm test
pnpm build
```

The static production application is emitted to `dist/`. Its host must fall back to `index.html` for `/agents/:uuid` and proxy the two read-only state endpoints to Burrow.

## Contract

Observatory consumes only Burrow's complete version-1 snapshot envelopes from `GET /state` and `GET /state/stream`. It does not read the event log or issue writes.
