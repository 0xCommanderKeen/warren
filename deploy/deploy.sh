#!/bin/sh
# deploy/deploy.sh — push one or more services to the burrow, the same way from a laptop
# and from CI.
#
#   deploy/deploy.sh chronicle            # one service
#   deploy/deploy.sh arcadia townhall     # several, in the order given
#   deploy/deploy.sh all                  # chronicle, arcadia, townhall, steward — in that
#                                         # order, because the clients validate chronicle's
#                                         # contract and nginx's route table derives from
#                                         # steward's (warren#242)
#
# Why a script: the runbooks (chronicle/README.md, arcadia/docs/deployment.md,
# townhall/docs/deployment.md, steward/README.md) were precise enough to be one, and being
# one is what lets .github/workflows/deploy.yml run them on every merge to main. Each step
# below names the runbook it executes. The runbooks stay the explanation; this is the
# mechanism, and the two are kept together by the same tests that pin the runbooks —
# chronicle's file list is read out of its README here, exactly as
# chronicle/tests/test_deployment_bundle.py reads it.
#
# Convergence, not accretion (warren#269): every published directory is `rsync --delete`d
# from a staging copy, so a file the repo removed disappears from the burrow too — the
# tar-over-ssh recipes could only ever add. Data volumes (/data, steward's steward.db) are
# never written by this script; steward's is backed up beside itself before each rollout.
#
# What is deployed is HEAD, and HEAD must be clean: chronicle is staged with `git archive`,
# the SPAs are built from the working tree, and a deploy nobody can name by commit is a
# deploy nobody can roll back. ALLOW_DIRTY=1 says out loud that you want it anyway.
#
# Every deploy leaves a marker on the burrow — ~/docker/<dir>/DEPLOYED-<service>, one
# line: revision, service, time, who — which is what deploy/status.sh reads. The NAS has
# no git; that file is the only thing there that can say what is running.
#
# Preconditions: ssh to $NAS with a key (BatchMode — no prompts), rsync on both ends,
# python3, pnpm and the node versions the CI workflows pin (24 for arcadia, 22 for
# townhall), and, for steward, a docker that can build linux/amd64.
set -eu

NAS="${NAS:-Miha@dxp2800}"
ORIGIN="${ORIGIN:-http://dxp2800:8737}"          # arcadia's nginx: the one public origin
STEWARD_URL="${STEWARD_URL:-http://dxp2800:8802}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
# -rlt and not -a: the burrow's directories carry UGOS ACLs and are owned by whoever
# created them; publishing files should not be a fight about modes and owners.
RSYNC="rsync -rlt --delete --exclude=.DS_Store --exclude=._*"

log() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }

REV="$(git -C "$ROOT" rev-parse HEAD)"
SHORT="$(git -C "$ROOT" rev-parse --short HEAD)"
WHO="${GITHUB_ACTOR:-$(id -un)}@${GITHUB_RUN_ID:+github-actions-run-}${GITHUB_RUN_ID:-$(hostname -s)}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

require_clean() {
    # Tracked modifications or untracked files under the service's own tree; the rest of
    # the repo (issues.md, another service mid-edit) is not this deploy's business.
    dirty="$(git -C "$ROOT" status --porcelain -- "$1")"
    if [ -n "$dirty" ] && [ "${ALLOW_DIRTY:-0}" != "1" ]; then
        printf '%s\n' "$dirty" >&2
        die "$1/ is not clean; commit first, or ALLOW_DIRTY=1 to deploy an unnamed tree"
    fi
}

stamp() {
    # stamp <nas dir> <service>
    $SSH "$NAS" "printf 'rev=%s service=%s at=%s by=%s\n' '$REV' '$2' '$NOW' '$WHO' > ~/docker/$1/DEPLOYED-$2"
}

wait_for() {
    # wait_for <url> <expected status> — up to ~90s
    i=0
    while [ $i -lt 45 ]; do
        code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$1" || true)"
        [ "$code" = "$2" ] && return 0
        i=$((i + 1)); sleep 2
    done
    die "$1 did not answer $2 within 90s (last: ${code:-none})"
}

tests_enabled() { [ "${SKIP_TESTS:-0}" != "1" ]; }

# ---------------------------------------------------------------------------- chronicle
# chronicle/README.md "Running": the tar recipe's file list, restart, /state.
deploy_chronicle() {
    require_clean chronicle
    files="$(python3 - "$ROOT/chronicle/README.md" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r"\x60tar -cf -\s+(.*?)\s+\|\s+ssh\b", text, re.DOTALL)
if not match:
    sys.exit("chronicle/README.md no longer carries the tar-over-ssh recipe")
print(" ".join(match.group(1).split()))
PY
)"
    if tests_enabled; then
        log "chronicle: tests"
        (cd "$ROOT/chronicle" && sh tests/run.sh >/dev/null) || die "chronicle tests failed"
    fi
    stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
    log "chronicle: staging $SHORT — $files"
    paths=""; for f in $files; do paths="$paths chronicle/$f"; done
    # shellcheck disable=SC2086 — the list is the recipe, space-separated on purpose
    (cd "$ROOT" && git archive --format=tar HEAD $paths) | tar -x -C "$stage"
    log "chronicle: publishing to $NAS:~/docker/burrow/app (rsync --delete)"
    $RSYNC "$stage/chronicle/" "$NAS:docker/burrow/app/"
    log "chronicle: restarting"
    $SSH "$NAS" 'cd ~/docker/burrow && docker compose restart burrow' >/dev/null
    wait_for "$ORIGIN/burrow/state" 200
    curl -fsS -m 10 "$ORIGIN/burrow/residents" | grep -q '"residents"' || die "chronicle: /burrow/residents did not answer"
    stamp burrow chronicle
    log "chronicle: $SHORT is live"
}

# ------------------------------------------------------------------------------ arcadia
# arcadia/docs/deployment.md "Deploy", steps 1, 3, 4, 5.
deploy_arcadia() {
    require_clean arcadia
    log "arcadia: build ($(node --version), pnpm $(pnpm --version))"
    (cd "$ROOT/arcadia" && pnpm install --frozen-lockfile >/dev/null \
        && { ! tests_enabled || pnpm test >/dev/null; } \
        && pnpm build >/dev/null) || die "arcadia build failed"
    log "arcadia: publishing dist/ to $NAS:~/docker/arcadia/dist (rsync --delete)"
    $RSYNC "$ROOT/arcadia/dist/" "$NAS:docker/arcadia/dist/"
    before="$($SSH "$NAS" 'md5sum ~/docker/arcadia/nginx.conf ~/docker/arcadia/compose.yaml 2>/dev/null' || true)"
    # The two config files land at the root of ~/docker/arcadia — beside dist/, not over it,
    # so no --delete here.
    rsync -lt "$ROOT/arcadia/deploy/nginx.conf" "$ROOT/arcadia/deploy/compose.yaml" "$NAS:docker/arcadia/"
    after="$($SSH "$NAS" 'md5sum ~/docker/arcadia/nginx.conf ~/docker/arcadia/compose.yaml')"
    if [ "$before" != "$after" ]; then
        log "arcadia: nginx.conf/compose.yaml changed — restarting the origin"
        $SSH "$NAS" 'cd ~/docker/arcadia && docker compose up -d && docker compose restart arcadia' >/dev/null
    else
        # Static assets are bind-mounted read-only: new files are served as they land.
        $SSH "$NAS" 'cd ~/docker/arcadia && docker compose up -d' >/dev/null
    fi
    wait_for "$ORIGIN/" 200
    log "arcadia: smoke"
    (cd "$ROOT/arcadia" && sh deploy/smoke.sh "$ORIGIN") || die "arcadia smoke failed"
    stamp arcadia arcadia
    log "arcadia: $SHORT is live"
}

# ----------------------------------------------------------------------------- townhall
# townhall/docs/deployment.md: build with the mount prefix, publish into arcadia's dir.
deploy_townhall() {
    require_clean townhall
    log "townhall: build --base=/observatory/ ($(node --version), pnpm $(pnpm --version))"
    (cd "$ROOT/townhall" && pnpm install --frozen-lockfile >/dev/null \
        && { ! tests_enabled || pnpm test >/dev/null; } \
        && pnpm build --base=/observatory/ >/dev/null) || die "townhall build failed"
    log "townhall: publishing dist/ to $NAS:~/docker/arcadia/observatory-dist (rsync --delete)"
    $RSYNC "$ROOT/townhall/dist/" "$NAS:docker/arcadia/observatory-dist/"
    curl -fsS -m 10 "$ORIGIN/observatory/" | grep -q 'id="root"' || die "townhall: /observatory/ did not serve the app"
    stamp arcadia townhall
    log "townhall: $SHORT is live"
}

# ------------------------------------------------------------------------------ steward
# steward/README.md "Deployment": the control-plane image travels as `docker save | ssh
# docker load` (no registry), the compose file is steward/deploy/compose.yaml, and the
# secrets are in a .env this script never writes.
deploy_steward() {
    require_clean steward
    if tests_enabled; then
        log "steward: make check"
        (cd "$ROOT/steward" && make check >/dev/null) || die "steward check failed"
    fi
    log "steward: building steward-cp:$SHORT (linux/amd64)"
    (cd "$ROOT/steward" && make image-cp CP_TAG="$SHORT" REVISION="$REV" >/dev/null) || die "steward image build failed"
    log "steward: shipping the image to $NAS"
    (cd "$ROOT/steward" && make image-cp-ship CP_TAG="$SHORT" NAS="$NAS" >/dev/null) || die "steward image ship failed"
    $SSH "$NAS" 'test -f ~/docker/steward/.env' \
        || die "~/docker/steward/.env is missing on $NAS — it must hold STEWARD_TOKEN=… (chmod 600); see steward/deploy/compose.yaml"
    log "steward: stopping, backing up data/, publishing compose.yaml"
    # down before the new compose file lands: the old file is the one that knows the old
    # service names, and --remove-orphans catches whatever it did not. Backups: keep three.
    $SSH "$NAS" 'cd ~/docker/steward && docker compose down --remove-orphans >/dev/null 2>&1; \
        cp -r data "data.bak-$(date -u +%Y%m%dT%H%M%SZ)" && ls -dt data.bak-* | tail -n +4 | xargs -r rm -rf'
    rsync -lt "$ROOT/steward/deploy/compose.yaml" "$NAS:docker/steward/compose.yaml"
    $SSH "$NAS" "cd ~/docker/steward \
        && if grep -q '^STEWARD_IMAGE_TAG=' .env; then sed -i 's/^STEWARD_IMAGE_TAG=.*/STEWARD_IMAGE_TAG=$SHORT/' .env; else printf 'STEWARD_IMAGE_TAG=%s\n' '$SHORT' >> .env; fi \
        && chmod 600 .env && docker compose up -d" >/dev/null
    # 401 is the API saying it is up and that it wants a credential — the smoke check
    # arcadia's origin already makes of the same route.
    wait_for "$STEWARD_URL/residents" 401
    log "steward: doctor"
    $SSH "$NAS" 'cd ~/docker/steward && docker compose exec -T api steward doctor --residents residents' || true
    stamp steward steward
    log "steward: $SHORT is live"
}

# --------------------------------------------------------------------------------- main
[ $# -gt 0 ] || die "usage: deploy/deploy.sh <chronicle|arcadia|townhall|steward|all>..."
$SSH "$NAS" true 2>/dev/null || die "cannot ssh to $NAS non-interactively (key not loaded, or wrong user — the burrow's user is Miha)"

for target in "$@"; do
    case "$target" in
        all) deploy_chronicle; deploy_arcadia; deploy_townhall; deploy_steward ;;
        chronicle) deploy_chronicle ;;
        arcadia) deploy_arcadia ;;
        townhall) deploy_townhall ;;
        steward) deploy_steward ;;
        *) die "unknown service: $target" ;;
    esac
done
