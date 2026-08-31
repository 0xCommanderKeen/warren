# Deployment

Townhall is a static single-page application. Build it from `warren/townhall/` and publish
the contents of `dist/`.

**The build needs the path prefix it will be served under.** `vite.config.js` sets no
`base`, so a plain `pnpm build` emits root-absolute asset URLs (`/assets/…`) — correct for
a site at `/`, and a white page under the `/observatory/` mount it actually gets on the
NAS. The deployed build is made with the flag:

```sh
pnpm install --frozen-lockfile
pnpm build --base=/observatory/
```

This is not a guess about the live deployment: building at this revision with that flag
reproduces the exact asset filenames currently served from
`~/docker/arcadia/observatory-dist/` (`index-D-vL4-mh.js`, `index-DXUM8-vx.css`). Plain
`pnpm build` produces different, root-absolute paths.

The flag lives in this runbook rather than in `vite.config.js`, which means it can be
forgotten. Moving `base: "/observatory/"` into the config would make the default build
correct and is worth doing next time the mount path is touched — see "Serving under a path
prefix" below.

## Where it actually runs

Townhall has no origin of its own. On the NAS it is served by **arcadia's** nginx at
`http://dxp2800:8737/observatory/`, from `~/docker/arcadia/observatory-dist/` — the
directory arcadia's compose file bind-mounts as `/srv/observatory`. Publishing a townhall
build therefore means shipping into arcadia's deploy directory, and the runbook that owns
that origin end to end is [`arcadia/docs/deployment.md`](../../arcadia/docs/deployment.md).
Follow it rather than reinventing the copy step here; the short version is:

```sh
# from warren/townhall/, after pnpm build
tar -cf - -C dist . | ssh Miha@dxp2800 'tar -xf - -C ~/docker/arcadia/observatory-dist'
```

The mount is read-only and static, so new files are served as they land — no restart.

The path is still `/observatory/`, the name this directory had as its own repo. Renaming
it to `/townhall/` is tracked as an open decision in arcadia's runbook; it requires a
matching vite `base` and a rebuild, not just an nginx edit (see below).

## What a host must provide

Any host — arcadia's nginx, or a standalone one for local preview — owns three rules:

1. Serve static files from the build directory.
2. Fall back to `index.html` for browser routes such as `/agents/:uuid`.
3. Proxy `/state` and `/state/stream` to chronicle without buffering the stream.

On the NAS, rule 3 is already satisfied by arcadia's nginx: it proxies `/state` and
`/state/stream` into chronicle at `host.docker.internal:8738` (chronicle moved off 8737
when arcadia took the origin in the 2026-08-27 cutover). Townhall consumes those
root-relative paths as-is.

A standalone nginx site can use this shape — note the port is chronicle's **8738**, not
the 8737 the origin now answers on:

```nginx
location / {
    try_files $uri $uri/ /index.html;
}

location = /state {
    proxy_pass http://127.0.0.1:8738/state$is_args$args;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    add_header Cache-Control no-store always;
}

location = /state/stream {
    proxy_pass http://127.0.0.1:8738/state/stream$is_args$args;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 1h;
    add_header X-Accel-Buffering no always;
    add_header Cache-Control no-store always;
}
```

Townhall may instead connect directly to a CORS-enabled chronicle deployment through the
`backend` query parameter. Same-origin proxying is preferred because it avoids exposing a
second public origin and keeps browser routing independent of API location.

## Serving under a path prefix

`vite.config.js` sets no `base`, so the prefix has to be supplied at build time
(`--base=/observatory/`, as above) and the router in `src/App.jsx` reads and writes
`window.location.pathname` with no prefix stripping. Mounting the build under any prefix —
`/observatory/`, `/townhall/`, anything — means building with `base` set to that prefix and
teaching the router to strip it. An nginx `alias` on its own gets you an `index.html` whose
assets 404.

Deep links are the loose end: assets resolve from `base`, but `src/App.jsx` treats
`window.location.pathname` as an app route, so under a prefix it sees `/observatory/agents/…`
rather than `/agents/…`. Whoever moves `base` into `vite.config.js` should strip
`import.meta.env.BASE_URL` in the same change.
