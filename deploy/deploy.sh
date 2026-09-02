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
# Convergence, not accretion (warren#269): every published directory is made equal to a
# staged copy — changed files replaced, files the repo removed removed — where the
# tar-over-ssh recipes could only ever add. Data volumes (/data, steward's steward.db) are
# never written by this script; steward's is backed up beside itself before each rollout.
#
# What is deployed is HEAD, and HEAD must be clean: chronicle is staged with `git archive`,
# the SPAs are built from the working tree, and a deploy nobody can name by commit is a
# deploy nobody can roll back. ALLOW_DIRTY=1 says out loud that you want it anyway.
#
# Every deploy leaves a marker on the burrow — ~/docker/<dir>/DEPLOYED-<service>, one
# line: revision, service, time, who — which is what deploy/status.sh reads. The NAS has
# no git; that file is the only thing there that can say what is running. (The residents
# checkout under ~/docker/steward is the one exception: it is git, through the
# control-plane image, and it names its own revision — see ensure_checkout.)
#
# Preconditions: ssh to $NAS with a key (BatchMode — no prompts), tar and python3 on
# both ends, pnpm and the node versions the CI workflows pin (24 for arcadia, 22 for
# townhall), and, for steward, a docker that can build linux/amd64.
set -eu

NAS="${NAS:-Miha@dxp2800}"
ORIGIN="${ORIGIN:-http://dxp2800:8737}"          # arcadia's nginx: the one public origin
STEWARD_URL="${STEWARD_URL:-http://dxp2800:8802}"
# The burrow's residents checkout (warren#351): a sparse clone of this repository on a
# branch of its own, which the control plane commits into and pushes. Made once here and
# never reset here. The branch is the one steward/deploy/compose.yaml sets as
# STEWARD_PUSH_BRANCH; the two must agree or the deploy's push and the API's push diverge.
CHECKOUT_URL="${CHECKOUT_URL:-git@github.com:0xCommanderKeen/warren.git}"
CHECKOUT_BRANCH="${CHECKOUT_BRANCH:-burrow/residents}"
case "$CHECKOUT_URL$CHECKOUT_BRANCH" in
    *[\'\"\ ]*) printf 'deploy: CHECKOUT_URL and CHECKOUT_BRANCH may not contain quotes or spaces\n' >&2; exit 1 ;;
esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"

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

quietly() {
    # quietly <label> <command...> — a step whose output only matters when it fails.
    label="$1"; shift
    out="$(mktemp)"
    if "$@" >"$out" 2>&1; then
        rm -f "$out"
    else
        cat "$out" >&2; rm -f "$out"
        die "$label failed"
    fi
}

tests_enabled() { [ "${SKIP_TESTS:-0}" != "1" ]; }

# publish <local dir> <remote dir, relative to ~> — make the remote directory equal to
# the local one.
#
# This is `rsync --delete` without rsync. UGOS ships an rsync that tries to become root
# and refuses any path outside its own sandbox (its scp is broken the same way), so the
# bytes travel the way every runbook already sends them — a tar over ssh — into a
# staging directory beside the target, and a python3 on the burrow then converges the
# target onto it: a changed file is written beside and renamed over, a file the repo no
# longer has is removed, and the target directory itself is never replaced. That last
# part is the point of doing it per file: docker bind-mounts an inode, not a path, so a
# directory swapped out from under a running container is a directory that container
# can no longer see.
publish() {
    local_dir="$1"; remote_dir="$2"
    COPYFILE_DISABLE=1 tar --no-xattrs -cf - -C "$local_dir" . \
        | $SSH "$NAS" "rm -rf ~/$remote_dir.incoming && mkdir -p ~/$remote_dir.incoming ~/$remote_dir && tar -xf - -C ~/$remote_dir.incoming"
    $SSH "$NAS" "python3 - ~/$remote_dir.incoming ~/$remote_dir" <<'PY'
import filecmp, os, shutil, sys

src, dst = sys.argv[1], sys.argv[2]
changed = removed = 0

for root, dirs, files in os.walk(src):
    rel = os.path.relpath(root, src)
    droot = dst if rel == "." else os.path.join(dst, rel)
    os.makedirs(droot, exist_ok=True)
    for name in files:
        s, t = os.path.join(root, name), os.path.join(droot, name)
        if os.path.isfile(t) and not os.path.islink(t) and filecmp.cmp(s, t, shallow=False):
            continue
        tmp = t + ".incoming"
        shutil.copyfile(s, tmp)
        shutil.copymode(s, tmp)
        os.replace(tmp, t)
        changed += 1

for root, dirs, files in os.walk(dst, topdown=False):
    rel = os.path.relpath(root, dst)
    sroot = src if rel == "." else os.path.join(src, rel)
    for name in files:
        if not os.path.lexists(os.path.join(sroot, name)):
            os.remove(os.path.join(root, name)); removed += 1
    for name in dirs:
        if not os.path.isdir(os.path.join(sroot, name)):
            shutil.rmtree(os.path.join(root, name)); removed += 1

shutil.rmtree(src)
print(f"converged {dst}: {changed} written, {removed} removed")
PY
}

# publish_files <remote dir, relative to ~> <local dir> <file>... — a few named files
# into a directory that holds other things too (no convergence: nothing is removed).
publish_files() {
    remote_dir="$1"; local_dir="$2"; shift 2
    COPYFILE_DISABLE=1 tar --no-xattrs -cf - -C "$local_dir" "$@" | $SSH "$NAS" "tar -xf - -C ~/$remote_dir"
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
        quietly "chronicle tests" sh -c 'cd "$1" && sh tests/run.sh' _ "$ROOT/chronicle"
    fi
    stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
    log "chronicle: staging $SHORT — $files"
    paths=""; for f in $files; do paths="$paths chronicle/$f"; done
    # shellcheck disable=SC2086 — the list is the recipe, space-separated on purpose
    (cd "$ROOT" && git archive --format=tar HEAD $paths) | tar -x -C "$stage"
    log "chronicle: publishing to $NAS:~/docker/burrow/app"
    publish "$stage/chronicle" docker/burrow/app
    $SSH "$NAS" 'test -f ~/docker/burrow/.env' \
        || die "~/docker/burrow/.env is missing on $NAS — it holds CHRONICLE_NOTIFY_URL (and CHRONICLE_TOKEN when ingest is closed); see chronicle/deploy/compose.yaml"
    publish_files docker/burrow "$ROOT/chronicle/deploy" compose.yaml
    log "chronicle: recreating"
    # Recreate rather than restart: the code is a bind mount, so `up -d` alone would see
    # nothing to do, and nothing this container holds lives outside /data any more.
    $SSH "$NAS" 'cd ~/docker/burrow && docker compose up -d --force-recreate' >/dev/null 2>&1
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
    quietly "arcadia install" sh -c 'cd "$1" && pnpm install --frozen-lockfile' _ "$ROOT/arcadia"
    if tests_enabled; then quietly "arcadia tests" sh -c 'cd "$1" && pnpm test' _ "$ROOT/arcadia"; fi
    quietly "arcadia build" sh -c 'cd "$1" && pnpm build' _ "$ROOT/arcadia"
    log "arcadia: publishing dist/ to $NAS:~/docker/arcadia/dist"
    publish "$ROOT/arcadia/dist" docker/arcadia/dist
    before="$($SSH "$NAS" 'md5sum ~/docker/arcadia/nginx.conf ~/docker/arcadia/compose.yaml 2>/dev/null' || true)"
    # The two config files land at the root of ~/docker/arcadia — beside dist/, not over it.
    publish_files docker/arcadia "$ROOT/arcadia/deploy" nginx.conf compose.yaml
    after="$($SSH "$NAS" 'md5sum ~/docker/arcadia/nginx.conf ~/docker/arcadia/compose.yaml')"
    if [ "$before" != "$after" ]; then
        log "arcadia: nginx.conf/compose.yaml changed — restarting the origin"
        $SSH "$NAS" 'cd ~/docker/arcadia && docker compose up -d && docker compose restart arcadia' >/dev/null 2>&1
    else
        # Static assets are bind-mounted read-only: new files are served as they land.
        $SSH "$NAS" 'cd ~/docker/arcadia && docker compose up -d' >/dev/null 2>&1
    fi
    wait_for "$ORIGIN/" 200
    log "arcadia: smoke"
    quietly "arcadia smoke" sh -c 'cd "$1" && sh deploy/smoke.sh "$2"' _ "$ROOT/arcadia" "$ORIGIN"
    stamp arcadia arcadia
    log "arcadia: $SHORT is live"
}

# ----------------------------------------------------------------------------- townhall
# townhall/docs/deployment.md: build with the mount prefix, publish into arcadia's dir.
deploy_townhall() {
    require_clean townhall
    log "townhall: build --base=/observatory/ ($(node --version), pnpm $(pnpm --version))"
    quietly "townhall install" sh -c 'cd "$1" && pnpm install --frozen-lockfile' _ "$ROOT/townhall"
    if tests_enabled; then quietly "townhall tests" sh -c 'cd "$1" && pnpm test' _ "$ROOT/townhall"; fi
    quietly "townhall build" sh -c 'cd "$1" && pnpm build --base=/observatory/' _ "$ROOT/townhall"
    log "townhall: publishing dist/ to $NAS:~/docker/arcadia/observatory-dist"
    publish "$ROOT/townhall/dist" docker/arcadia/observatory-dist
    curl -fsS -m 10 "$ORIGIN/observatory/" | grep -q 'id="root"' || die "townhall: /observatory/ did not serve the app"
    stamp arcadia townhall
    log "townhall: $SHORT is live"
}

# checkout_sh <image tag> <shell script> — run a script on the burrow inside the control-
# plane image, against the residents checkout, with the deploy key. The NAS has no git of
# its own; the image has git, ssh, GitHub's host keys and a GIT_SSH_COMMAND naming the key
# mounted here, so this is the one way anything on the burrow talks to the repository. The
# script must not contain single quotes: it travels through two shells as one word.
checkout_sh() {
    case "$2" in *\'*) die "checkout_sh: the script must not contain a single quote: $2" ;; esac
    $SSH "$NAS" "docker run --rm -v ~/docker/steward/residents-repo:/checkout -v ~/docker/steward/residents-key:/run/steward/residents-key:ro steward-cp:$1 sh -c '$2'"
}

# ensure_checkout <image tag> — the residents checkout on the burrow (warren#351): made
# once, fetched on every deploy, never reset. Runs before anything is stopped, so a refusal
# here leaves the fleet exactly as it was.
ensure_checkout() {
    $SSH "$NAS" 'test -f ~/docker/steward/residents-key' \
        || die "~/docker/steward/residents-key is missing on $NAS — the deploy key that lets the burrow's residents checkout reach $CHECKOUT_URL; deploy/README.md \"The residents checkout\" says how to make one"
    $SSH "$NAS" 'chmod 600 ~/docker/steward/residents-key'
    if $SSH "$NAS" 'test -d ~/docker/steward/residents-repo/.git'; then
        # Dirty means a write landed on disk and its commit did not — the one state this
        # script must not paper over with a reset, because the bytes are somebody's edit.
        dirty="$(checkout_sh "$1" "git -C /checkout status --porcelain")" \
            || die "could not read the residents checkout on $NAS (is steward-cp:$1 on the burrow?)"
        if [ -n "$dirty" ]; then
            printf '%s\n' "$dirty" >&2
            die "the residents checkout on $NAS has uncommitted changes and this script will not reset them; look with: ssh $NAS docker exec steward-api git -C /checkout status"
        fi
        log "steward: residents checkout — fetching, never resetting"
        checkout_sh "$1" "git -C /checkout fetch --quiet origin" \
            || log "steward: WARNING — the burrow could not fetch from origin; continuing with what it has"
        # What the burrow holds that the branch does not is history that exists in one place.
        if checkout_sh "$1" "git -C /checkout rev-parse --verify --quiet origin/$CHECKOUT_BRANCH >/dev/null"; then
            unpushed="$(checkout_sh "$1" "git -C /checkout rev-list --count origin/$CHECKOUT_BRANCH..HEAD")"
        else
            unpushed="every"
        fi
        if [ "$unpushed" != "0" ]; then
            log "steward: pushing $unpushed commit(s) the burrow holds that origin/$CHECKOUT_BRANCH does not"
            checkout_sh "$1" "git -C /checkout push --quiet origin HEAD:refs/heads/$CHECKOUT_BRANCH" \
                || log "steward: WARNING — the push failed; those commits exist on $NAS alone until one succeeds"
        fi
    else
        log "steward: creating the residents checkout on $NAS from $CHECKOUT_URL ($CHECKOUT_BRANCH)"
        # A sparse, blobless clone: the two directories the control plane reads, nothing
        # else's blobs. On its own branch — an existing one continues this burrow's history,
        # a new one starts from the seed on the default branch — and pushed at once, so the
        # branch exists off the box from the first minute.
        $SSH "$NAS" 'mkdir -p ~/docker/steward/residents-repo'
        checkout_sh "$1" "git clone --quiet --filter=blob:none --sparse $CHECKOUT_URL /checkout && git -C /checkout sparse-checkout set steward/residents steward/skills && (git -C /checkout checkout --quiet -B $CHECKOUT_BRANCH origin/$CHECKOUT_BRANCH 2>/dev/null || git -C /checkout checkout --quiet -b $CHECKOUT_BRANCH) && git -C /checkout push --quiet origin HEAD:refs/heads/$CHECKOUT_BRANCH" \
            || die "could not create the residents checkout on $NAS (is residents-key's public half a deploy key with write access on the repository?)"
        $SSH "$NAS" 'test -d ~/docker/steward/residents-repo/steward/residents' \
            || die "the checkout on $NAS came up without steward/residents"
    fi
    # What the checkout holds going into this deploy. The smoke after `up` insists it is
    # still in the history — the issue's own acceptance line: a redeploy does not lose a
    # declaration written the day before.
    CHECKOUT_HEAD="$(checkout_sh "$1" "git -C /checkout rev-parse HEAD")"
    [ -n "$CHECKOUT_HEAD" ] || die "could not read the residents checkout's HEAD on $NAS"
}

# ------------------------------------------------------------------------------ steward
# steward/README.md "Deployment": the control-plane image travels as `docker save | ssh
# docker load` (no registry), the compose file is steward/deploy/compose.yaml, and the
# secrets are in a .env this script never writes. The residents checkout beside them is
# made here once and only ever fetched afterwards — see ensure_checkout.
deploy_steward() {
    require_clean steward
    if tests_enabled; then
        log "steward: make check"
        quietly "steward check" sh -c 'cd "$1" && make check' _ "$ROOT/steward"
    fi
    log "steward: building steward-cp:$SHORT (linux/amd64)"
    quietly "steward image build" sh -c 'cd "$1" && make image-cp CP_TAG="$2" REVISION="$3"' _ "$ROOT/steward" "$SHORT" "$REV"
    log "steward: shipping the image to $NAS"
    quietly "steward image ship" sh -c 'cd "$1" && make image-cp-ship CP_TAG="$2" NAS="$3"' _ "$ROOT/steward" "$SHORT" "$NAS"
    $SSH "$NAS" 'test -f ~/docker/steward/.env' \
        || die "~/docker/steward/.env is missing on $NAS — it must hold STEWARD_TOKEN=… (chmod 600); see steward/deploy/compose.yaml"
    ensure_checkout "$SHORT"
    log "steward: stopping, backing up data/, publishing compose.yaml"
    # down before the new compose file lands: the old file is the one that knows the old
    # service names, and --remove-orphans catches whatever it did not. Backups: keep three.
    $SSH "$NAS" 'cd ~/docker/steward && docker compose down --remove-orphans >/dev/null 2>&1; \
        cp -r data "data.bak-$(date -u +%Y%m%dT%H%M%SZ)" && ls -dt data.bak-* | tail -n +4 | xargs -r rm -rf'
    publish_files docker/steward "$ROOT/steward/deploy" compose.yaml
    $SSH "$NAS" "cd ~/docker/steward \
        && if grep -q '^STEWARD_IMAGE_TAG=' .env; then sed -i 's/^STEWARD_IMAGE_TAG=.*/STEWARD_IMAGE_TAG=$SHORT/' .env; else printf 'STEWARD_IMAGE_TAG=%s\n' '$SHORT' >> .env; fi \
        && chmod 600 .env && docker compose up -d" >/dev/null 2>&1
    # 401 is the API saying it is up and that it wants a credential — the smoke check
    # arcadia's origin already makes of the same route.
    wait_for "$STEWARD_URL/residents" 401
    log "steward: checkout smoke"
    # The API sees the checkout on its branch, and the daemons' links reach into it: the
    # two facts behind warren#351's three defects, checked on the running containers.
    branch="$($SSH "$NAS" 'cd ~/docker/steward && docker compose exec -T api git -C /checkout rev-parse --abbrev-ref HEAD' 2>/dev/null | tr -d '\r')"
    [ "$branch" = "$CHECKOUT_BRANCH" ] || die "steward-api does not see the residents checkout on $CHECKOUT_BRANCH (saw: ${branch:-nothing})"
    # Nothing written before this deploy is gone: the HEAD read before anything was stopped
    # is still an ancestor of (or is) the HEAD the new API serves. An ancestor rather than
    # an equality, because the old API may legitimately have committed a save between the
    # two reads — what must never be true is that history went backwards.
    $SSH "$NAS" "cd ~/docker/steward && docker compose exec -T api git -C /checkout merge-base --is-ancestor $CHECKOUT_HEAD HEAD" >/dev/null 2>&1 \
        || die "the residents checkout no longer contains $CHECKOUT_HEAD, which it held before this deploy: a declaration written earlier may be gone — stop and look before deploying again"
    $SSH "$NAS" 'cd ~/docker/steward && docker compose exec -T scheduler test -f /checkout/steward/residents/pip/manifest.yaml' >/dev/null 2>&1 \
        || die "the scheduler's read-only mount of the checkout does not reach pip's manifest"
    log "steward: doctor"
    # In the watchdog's container, against the tree the daemons actually run: that is the
    # process with the docker socket, so its topology line is the true one.
    $SSH "$NAS" 'cd ~/docker/steward && docker compose exec -T watchdog steward doctor /checkout/steward/residents' 2>&1 | head -40 || true
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
