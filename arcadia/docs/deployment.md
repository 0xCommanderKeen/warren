# NAS deployment and cutover

Arcadia owns the NAS origin on port 8737. The static village is served at `/`, townhall is
mounted at `/observatory/`, Burrow state is exposed at `/burrow/`, and Steward write
routes remain same-origin. Burrow itself listens on host port 8738 after the cutover; its
old built-in viewer is no longer reachable at `/`.

This origin serves builds of **two** directories of the warren monorepo — `arcadia/` and
`townhall/` — so a deploy is driven from a checkout of
<https://github.com/0xCommanderKeen/warren>, and the working directory matters at every
step below. The NAS has no git and no clone: `~/docker/arcadia/` holds unpacked build
output, and nothing there pulls. Every deploy is pushed.

## Deploy

Run steps 1–3 from a warren checkout on a machine that can build; steps 4–5 talk to the NAS.

1. From `warren/arcadia/`: `pnpm install --frozen-lockfile && pnpm test && pnpm build`, at
   the exact revision being deployed. The village lands in `warren/arcadia/dist/`.
2. From `warren/townhall/`:
   `pnpm install --frozen-lockfile && pnpm test && pnpm build --base=/observatory/`.
   **The `--base` flag is not optional.** townhall's `vite.config.js` sets no `base`, so a
   plain `pnpm build` emits root-absolute `/assets/…` URLs and serves a white page from the
   `/observatory/` mount. Building at the deployed revision with the flag reproduces the
   asset filenames live on the NAS today (`index-D-vL4-mh.js`, `index-DXUM8-vx.css`); a
   plain build does not. That build lands in `warren/townhall/dist/` and is published to a
   *different* directory on the NAS than arcadia's — see step 3. If the mount path ever
   changes, `--base` changes with it: read "The `/observatory/` path" below.
3. Publish both builds and arcadia's two config files. UGOS's `scp` is broken, so
   everything travels as a tar over ssh, the same way chronicle and steward deploy:

   ```sh
   # from warren/arcadia/ — the village, then its two config files
   tar -cf - dist | ssh Miha@dxp2800 'tar -xf - -C ~/docker/arcadia'
   tar -cf - -C deploy compose.yaml nginx.conf \
     | ssh Miha@dxp2800 'tar -xf - -C ~/docker/arcadia'

   # from warren/townhall/ — its dist becomes the *contents* of observatory-dist
   tar -cf - -C dist . \
     | ssh Miha@dxp2800 'tar -xf - -C ~/docker/arcadia/observatory-dist'
   ```

   The `-C` flags are what put the files where the compose file expects them: the config
   pair lands at the root of `~/docker/arcadia/`, and townhall's build lands *inside*
   `observatory-dist/` rather than as a nested `dist/` directory.

   `compose.yaml` and `nginx.conf` live at the *root* of `~/docker/arcadia/` on the NAS,
   not under a `deploy/` subdirectory — that is what the compose file's
   `./nginx.conf:/etc/nginx/nginx.conf:ro` mount resolves to.
4. `ssh Miha@dxp2800 'cd ~/docker/arcadia && docker compose up -d'`. Static assets are
   bind-mounted read-only, so a build-only change needs no restart — the container serves
   the new files as soon as they land. Changing `nginx.conf` does need
   `docker compose restart arcadia` (or `up -d` after a compose change).
5. Run `sh deploy/smoke.sh http://dxp2800:8737` from `warren/arcadia/`. In a browser, enter
   the Steward token in the approval-knock prompt and exercise an existing pending approval
   if one exists.

## The `/observatory/` path

townhall is still served at `/observatory/`, under the name it had when it was its own
repo. Renaming the path to `/townhall/` is cosmetic and deliberately **not** done yet
(warren#218) — it is a two-sided change, not an nginx edit:

- nginx: the `location ^~ /observatory/` block, its `= /observatory` 301, and the
  `@observatory_index` fallback all move to `/townhall/`; the volume can keep its name or
  become `townhall-dist`. `arcadia/src/deployment.test.js` asserts on this file's literal
  text and moves with it.
- townhall: `vite.config.js` sets no `base`, so the prefix is supplied per build by
  `--base=/observatory/` (step 2). A path rename means building `--base=/townhall/`
  instead — better, moving `base` into `vite.config.js` so the default build is right —
  plus base-prefix handling in the router (`src/App.jsx` reads and writes
  `window.location.pathname` unprefixed, so deep links under a prefix are already the weak
  spot).

Two options when it is convenient:

- **Rename with a redirect** — mount at `/townhall/` and keep
  `location = /observatory { return 301 /townhall/; }` (plus `^~ /observatory/`) so old
  bookmarks survive.
- **Leave it.** The path is a URL, not an identifier; nothing reads it programmatically.
  It costs nothing to keep, and warren#216 has not renamed the code's `burrow`/observatory
  identifiers either.

Whichever is chosen, rebuild townhall with a matching `base` in the same deploy — the
nginx alias alone will serve an `index.html` whose assets 404.

The state parser rejects an unknown `schema_version` before applying it and replaces the
village with a visible contract-mismatch screen. The remedy is to deploy a compatible
Arcadia build or make Burrow serve a version supported by deployed clients; do not bypass
the check or partially render the unknown snapshot.

## Roll back

To undo a bad *arcadia build*, republish the previous revision's `dist/` — step 3 alone,
no restart. That is the everyday rollback now that the origin is established.

To undo the whole 2026-08-27 cutover: stop Arcadia, restore Burrow's port mapping to
`8737:8737`, and recreate Burrow. The Arcadia build and configuration remain in
`~/docker/arcadia/` for diagnosis. No data migration was part of that cutover, so rollback
does not touch Burrow's event log or Steward's database.

That rollback no longer restores a UI. It used to bring back Burrow's built-in viewer at
`/`; warren#219 removed that viewer from chronicle, which is now backend-only, so undoing
the cutover today leaves an API with nothing in front of it. Arcadia is the village.

## Cutover record

The first cutover completed on 2026-08-27. Arcadia and Observatory were installed under
one nginx origin on `dxp2800:8737`; Burrow moved to host port 8738 and was upgraded from
the stale pre-contract server to the stable `main` deployment bundle and documented
Python 3.14/uv runtime. The automated smoke check verified root delivery, deep-link
fallback, a live schema-version-1 snapshot, an 18-second unbuffered SSE connection with
query parameters, and a same-origin Steward authentication preflight returning 401.
Burrow's event log and Steward database were retained unchanged. The pre-cutover and
pre-runtime-upgrade Compose files are retained beside Burrow's active Compose file.
