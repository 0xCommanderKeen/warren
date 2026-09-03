# UI client routing

Chronicle has no user interface. It is the event log, the projection and the HTTP API; every
UI is a separate client consuming the versioned state contract over one authoritative
backend. Clients read `/state` and `/state/stream`; none consume `/events` or reconstruct
projected state for themselves.

Nothing is served to a browser from this repository. Chronicle answers its documented API
paths and 404s everything else, so a client pointed at a path that used to return the
retired in-tree viewer fails loudly instead of rendering a stale page.

## The clients

- **Arcadia** — the pixel-art village, and the successor to the viewer this repository used
  to serve. Phaser and React over the same `/state` contract.
- **Townhall** — the control panel.

Both are maintained outside this service and follow their own development and deployment
instructions. Chronicle supplies only the authoritative `/state` and `/state/stream` read
interfaces, and knows nothing about either client's routes.

## Client transport

A client points itself at a backend origin or path prefix; endpoint names and the versioned
state contract never change with it. Arcadia's `createStateTransport` is the reference
implementation of the read loop and the shape to copy:

- Load `/state` for a complete snapshot, then open `/state/stream` from the generation and
  cursor that response returned.
- Reconnect by catching up from the last applied boundary before reopening the stream.
- Treat every snapshot as a complete replacement. Never fold raw events client-side.
- Refuse an unsupported `snapshot.schema_version` before applying any state, rather than
  rendering a version the client does not understand.

`/state` answers `204` when nothing changed, and carries the current position in
`X-Burrow-State-Generation` and `X-Burrow-State-Cursor`. (`X-Burrow-Cursor` is the internal
`GET /events` header, not part of this contract.)
A stale cursor receives one atomic reset snapshot; see
[state-contract.md](state-contract.md) for the full contract and versioning policy.

## Development

Run Chronicle on its standard local port:

```sh
uv run uvicorn serve:app --host 127.0.0.1 --port 8737
```

Configure the client's dev server to proxy both read endpoints to that backend. For example,
a Vite configuration can keep the client same-origin and avoid adding CORS policy:

```js
export default {
  server: {
    proxy: {
      "/state/stream": { target: "http://127.0.0.1:8737", changeOrigin: true },
      "/state": { target: "http://127.0.0.1:8737", changeOrigin: true },
    },
  },
};
```

Order matters in proxy systems that choose the first matching route: place
`/state/stream` before `/state` if the proxy does not use longest-prefix matching. The proxy
must stream the response instead of buffering it, must not impose a timeout shorter than the
15-second SSE keepalive interval, and must pass query parameters unchanged.

Captured snapshots in `tests/fixtures/state-contract/` let clients render identical state
without a live backend. Run `sh tests/ui-contract.sh` before using a fixture in a client.

## Production

When hosting a client under the same origin, proxy Chronicle's state endpoints under
`/chronicle/`. The client itself is served by its own deployment; this shape exposes only
the shared state transport:

```nginx
location = /chronicle/state {
    proxy_pass http://127.0.0.1:8737/state;
    proxy_http_version 1.1;
}

location = /chronicle/state/stream {
    proxy_pass http://127.0.0.1:8737/state/stream;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 60s;
    add_header X-Accel-Buffering no;
}

# Existing village delivery telemetry; not part of the public state contract.
location = /chronicle/transport/status {
    proxy_pass http://127.0.0.1:8737/transport/status;
    proxy_http_version 1.1;
}
```

Clients set their backend prefix to `/chronicle`. It was `/burrow` before warren#361, and warren's own origin still 301s that spelling for a release. The proxy preserves the SSE body, event type,
query string, and connection lifetime, so the transport retains keepalive, reconnection,
generation, cursor, and reset semantics. Routes for Steward writes are deliberately absent
from this read-only client setup.
