# NAS deployment and cutover

Arcadia owns the NAS origin on port 8737. The static village is served at `/`, the
Observatory is mounted at `/observatory/`, Burrow state is exposed at `/burrow/`, and
Steward write routes remain same-origin. Burrow itself listens on host port 8738 after the
cutover; its old built-in viewer is no longer reachable at `/`.

## Deploy

1. Run `pnpm test` and `pnpm build` from the exact revision being deployed.
2. Copy `dist/`, `deploy/compose.yaml`, and `deploy/nginx.conf` to
   `~/docker/arcadia/` on the NAS. Copy the Observatory build to
   `~/docker/arcadia/observatory-dist/`.
3. In `~/docker/burrow/compose.yaml`, change the Burrow port mapping from
   `8737:8737` to `8738:8737`, then recreate Burrow.
4. Start Arcadia with `docker compose up -d` from `~/docker/arcadia/`.
5. Run `sh deploy/smoke.sh http://dxp2800:8737`. In a browser, enter the Steward token
   in the approval-knock prompt and exercise an existing pending approval if one exists.

The state parser rejects an unknown `schema_version` before applying it and replaces the
village with a visible contract-mismatch screen. The remedy is to deploy a compatible
Arcadia build or make Burrow serve a version supported by deployed clients; do not bypass
the check or partially render the unknown snapshot.

## Roll back

Stop Arcadia, restore Burrow's port mapping to `8737:8737`, and recreate Burrow. This
immediately restores the built-in viewer and the original emitter URL. The Arcadia build
and configuration remain in `~/docker/arcadia/` for diagnosis. No data migration is part
of this cutover, so rollback does not touch Burrow's event log or Steward's database.

## Cutover record

The first cutover completed on 2026-08-27. Arcadia and Observatory were installed under
one nginx origin on `dxp2800:8737`; Burrow moved to host port 8738 and was upgraded from
the stale pre-contract server to the stable `main` deployment bundle and documented
Python 3.14/uv runtime. The automated smoke check verified root delivery, deep-link
fallback, a live schema-version-1 snapshot, an 18-second unbuffered SSE connection with
query parameters, and a same-origin Steward authentication preflight returning 401.
Burrow's event log and Steward database were retained unchanged. The pre-cutover and
pre-runtime-upgrade Compose files are retained beside Burrow's active Compose file.
