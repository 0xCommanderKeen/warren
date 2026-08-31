# UI client routing

Burrow supports multiple UI clients over one authoritative backend. All clients consume
`/state` and `/state/stream`; none consume `/events` or reconstruct projected state.

## Client transport

`createStateTransport(options)` accepts an optional `baseUrl`. It defaults to the current
origin, so a client normally relies on its web server to proxy Burrow:

```js
const state = BurrowStateTransport.createStateTransport({
  fetch: window.fetch.bind(window),
  EventSource: window.EventSource,
  baseUrl: "/burrow",
  onState(snapshot, meta) {
    render(snapshot, meta);
  },
});
```

The option selects only the backend origin or path prefix. Endpoint names and the versioned
state contract do not change. It applies equally to initial polling, unchanged-snapshot
polling, the SSE connection, and reconnects with generation and cursor resume parameters.
A trailing slash is accepted and normalized.

## Development

Run Burrow on its standard local port:

```sh
uv run uvicorn serve:app --host 127.0.0.1 --port 8737
```

Configure the Observatory dev server to proxy both read endpoints to that backend. For
example, a Vite configuration can keep the client same-origin and avoid adding CORS policy:

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
15-second SSE keepalive interval, and must pass query parameters unchanged. The client then
uses the default empty `baseUrl`.

Captured snapshots in `tests/fixtures/state-contract/` let clients render identical state
without a live backend. Run `sh tests/ui-contract.sh` before using a fixture in a client.

The Observatory is an external client maintained in
[`0xCommanderKeen/observatory`](https://github.com/0xCommanderKeen/observatory). Follow that
repository's development and deployment instructions; Burrow supplies only the authoritative
`/state` and `/state/stream` read interfaces. Burrow does not serve `/observatory/`.

## Production

When hosting an external client under the same origin, proxy Burrow's state endpoints under
`/burrow/`. This nginx shape serves Burrow's village at `/village/` and exposes the shared
state transport for independently deployed clients:

```nginx
location /village/ {
    alias /srv/burrow/viewer/;
    try_files $uri $uri/ /village/index.html;
}

location = /burrow/state {
    proxy_pass http://127.0.0.1:8737/state;
    proxy_http_version 1.1;
}

location = /burrow/state/stream {
    proxy_pass http://127.0.0.1:8737/state/stream;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 60s;
    add_header X-Accel-Buffering no;
}

# Existing village delivery telemetry; not part of the public state contract.
location = /burrow/transport/status {
    proxy_pass http://127.0.0.1:8737/transport/status;
    proxy_http_version 1.1;
}
```

External clients set `baseUrl: "/burrow"`. The proxy preserves the SSE body, event type, query
string, and connection lifetime, so the transport retains keepalive, reconnection,
generation, cursor, and reset semantics. Routes for Steward writes are deliberately absent
from this read-only client setup.
