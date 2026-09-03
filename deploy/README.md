# Deploying the warren

Everything runs on one burrow — the NAS, `dxp2800` — and nothing there pulls. There is
no git on it and no registry: every deploy is *pushed* from a machine that has this repo
checked out. That machine is a laptop, or a GitHub Actions runner on every merge to
`main`. Both run the same script.

```sh
deploy/deploy.sh chronicle              # one service
deploy/deploy.sh arcadia townhall       # several
deploy/deploy.sh all                    # chronicle → arcadia → townhall → steward
deploy/status.sh                        # what is running, against origin/main
```

`deploy.sh` executes the per-service runbooks ([chronicle](../chronicle/README.md#running),
[arcadia](../arcadia/docs/deployment.md), [townhall](../townhall/docs/deployment.md),
[steward](../steward/README.md#deployment)) and cites each step it takes. What it adds
over running them by hand:

- **Convergence.** Every published directory is made *equal* to the staged copy — a file
  the repo removed is removed on the burrow too (warren#269). UGOS's `rsync` is sandboxed
  like its `scp`, so this is a tar over ssh into a staging directory plus a python3 pass
  on the burrow that replaces files one by one and never swaps the directory itself
  (docker bind-mounts an inode, not a path).
- **A marker.** `~/docker/warren/<dir>/DEPLOYED-<service>` — revision, service, time, who. The
  only thing on the NAS that can say what it is running; `status.sh` reads them.
- **A clean HEAD.** The deployed tree must be one git can name. `ALLOW_DIRTY=1` overrides
  that, out loud. `SKIP_TESTS=1` skips each service's own check first.
- **Steward's data backed up** beside itself before every rollout (three kept), and its
  image tag written into the burrow's `.env`, so a rollback is one line and `up -d`.

## Where things are on the burrow

Everything the warren puts on a burrow is under one directory, `~/docker/warren`
(warren#358), so that `ls ~/docker` on a machine that also runs other stacks does not
interleave the warren's directories with theirs:

| Directory | What | Written by |
| --- | --- | --- |
| `burrow/` | chronicle: `app/` (the tree its README's tar recipe ships), `data/`, `compose.yaml`, `.env` | `deploy.sh chronicle` |
| `arcadia/` | the origin: `dist/`, `observatory-dist/` (townhall's build), `nginx.conf`, `compose.yaml` | `deploy.sh arcadia`, `deploy.sh townhall` |
| `steward/` | the control plane: `compose.yaml`, `.env`, `data/`, `residents-key`, `residents-repo/` | `deploy.sh steward` |
| `residents/<id>/` | one resident: `docker-compose.yaml`, `.env`, `manifest.yaml`, `soul.md`, `memory/`, `claude/` | `steward provision <id>`, from a laptop — never this script |

The control plane's daemons bind-mount `residents/` and nothing wider: that is what lets
them journal for a container-placed resident without also seeing steward's own `.env`
and deploy key next door. A resident whose manifest sets `deploy.path` outside
`residents/` is one the daemons cannot see. The pre-steward bot in `~/docker/life-agent`
is not the warren's and stays where it is. A burrow still laid out the old way — the
same directories at the top of `~/docker` — is refused by `deploy.sh`, not moved; see
[Moving a burrow under `~/docker/warren`](#moving-a-burrow-under-dockerwarren).

## What lives on the burrow that the repo does not carry

Secrets, in a `.env` beside each compose file, mode `0600`, never in git:

| Directory | `.env` holds | Compose file (in the repo) |
| --- | --- | --- |
| `~/docker/warren/burrow` | `CHRONICLE_NOTIFY_URL` (private ntfy topic); `CHRONICLE_TOKEN` when ingest is closed | [`chronicle/deploy/compose.yaml`](../chronicle/deploy/compose.yaml) |
| `~/docker/warren/arcadia` | — | [`arcadia/deploy/compose.yaml`](../arcadia/deploy/compose.yaml) + `nginx.conf` |
| `~/docker/warren/steward` | `STEWARD_TOKEN`; `STEWARD_IMAGE_TAG` (written by the script); chat tokens per `steward/docs/chat.md`. Beside it: `residents-key`, the deploy key below, and `residents-repo/`, the residents checkout | [`steward/deploy/compose.yaml`](../steward/deploy/compose.yaml) |

`deploy.sh` refuses to roll out a service whose `.env` is missing and says what it must
contain. Data — chronicle's `/data`, steward's `data/` — is never written by the script.

## The residents checkout

The control plane does not serve the residents tree baked into its image. It serves —
and writes — a **git checkout on the burrow**: `~/docker/warren/steward/residents-repo`, a
sparse, blobless clone of this repository holding `steward/residents` and
`steward/skills`, on a branch of its own, `burrow/residents`. The compose file mounts it
at `/checkout`, read-write into the API and read-only into the scheduler and watchdog,
which read the same tree (partitioned by `deploy.host`, warren#344) rather than a copy
taken at container start. That is warren#351: without a checkout every
write from townhall was a `409`, and with the tree in the image a write would have died
on the next deploy, exactly as chronicle's event log did in warren#313.

What that makes true:

- **A charter edit is a change, not a proposal.** Saving in townhall commits into the
  checkout and the running daemons read the commit on their next wake-up, with no
  restart and no pull request. The commit is the record of what already happened.
- **The checkout is authoritative for this burrow's residents.** `steward/residents/`
  in the repository is the *seed* a new burrow is cloned from, and stops being a place
  charters are edited. There is no two-way sync, on purpose: that is where the conflict
  story lives. A change merged to `main` under `steward/residents/` or `steward/skills/`
  does **not** reach a burrow that already has a checkout — apply it through the API
  (`PUT /residents/{id}/declaration`, `PUT /skills/{name}`; townhall's editors), which
  validates the whole tree before it writes. Committing in the checkout by hand
  (`docker exec steward-api git -C /checkout …`) skips that gate and is break-glass:
  run `steward validate /checkout/steward/residents` in the container first.
- **Every commit the API makes is pushed to `burrow/residents`**, best effort. The save
  is durable on disk before the push starts, so a burrow that cannot reach GitHub
  answers `"committed, not pushed"` (`commit.pushed: false`) and carries on; the next
  write that commits, or the next deploy, pushes whatever the branch is missing (a save
  that changed nothing pushes nothing). Never `main`: nothing
  lands there without a pull request, and a push to any other branch deploys nothing,
  so a charter edit cannot loop into a rollout of the fleet it edited.
- **`deploy.sh` makes the checkout once and never resets it.** On every steward deploy
  it fetches, pushes anything the burrow holds that the branch does not, and **refuses
  to continue if the checkout is dirty** — a write that landed on disk without its
  commit is somebody's edit, and the script will not reset it. All of that runs before
  anything is stopped. Look with `ssh Miha@dxp2800 docker exec steward-api git -C
  /checkout status`; `deploy/status.sh` prints the branch, head, unpushed and dirty
  counts on every run.

The NAS has no git. Every git command above runs inside the control-plane image, which
carries git, an ssh client, GitHub's published host keys, and a `GIT_SSH_COMMAND` that
names the deploy key at `/run/steward/residents-key`.

### One-time setup — the deploy key

The checkout needs a key that can read and push this (private) repository. Generated
**on the NAS**, so the private half never travels; its public half becomes a deploy
key with write access. From the laptop:

```sh
ssh Miha@dxp2800 'ssh-keygen -t ed25519 -N "" -C "warren residents checkout (dxp2800)" -f ~/docker/warren/steward/residents-key && chmod 600 ~/docker/warren/steward/residents-key'
ssh Miha@dxp2800 'cat ~/docker/warren/steward/residents-key.pub' \
    | gh repo deploy-key add - -R 0xCommanderKeen/warren --allow-write --title 'dxp2800 residents checkout'
```

Both commands are quoted so the `~` reaches the NAS: unquoted, your own shell expands it
to your laptop home, and `cat` on the NAS answers `No such file or directory`.

Then deploy steward (`deploy/deploy.sh steward`, or merge anything under `steward/`):
the first run creates the checkout, pushes `burrow/residents`, and the smoke check
confirms the API sees the branch and the scheduler's links reach Pip's manifest.

Two things to know about that key. A deploy key added with `gh` is tied to the token
`gh` used and is removed if that token is revoked — add it in the repository's settings
(Deploy keys) instead if that bothers you. And GitHub deploy keys are per repository,
not per branch: the key *can* push `main`; steward never does, and the standing rule
that nothing lands on `main` without a pull request is what keeps it that way. To
revoke: delete the key on GitHub and `rm ~/docker/warren/steward/residents-key` on the NAS —
the API then answers `"committed, not pushed"` on every write until a new key exists.

## Deploy on merge

[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) runs on every push to
`main`, works out which services the push touched (the same path filters the CI
workflows use, plus `deploy/deploy.sh`; a change to the workflow itself deploys nothing —
prove one with a dispatch), and runs `deploy/deploy.sh` for them in order. Each job
first runs that service's own check — the same command CI runs — so a deploy is never
greener than the suite. The runner reaches the NAS by joining the tailnet as an
ephemeral node tagged `tag:ci`, then ssh with a key that only the tailnet may present.

The workflow is **off until `DEPLOY_ENABLED` is `true`**, so merging this is safe before
the steps below are done. `workflow_dispatch` runs it by hand for any subset of services.

### One-time setup — the parts only a person can do

Done already (2026-09-02, from the laptop): a deploy key `warren-deploy` generated and
installed in the NAS's `~/.ssh/authorized_keys` with
`restrict,from="127.0.0.1,100.64.0.0/10"` — no pty, no forwarding, and only from the
tailnet range or loopback. Loopback is not a loophole: the NAS's tailscale runs as a
container in userspace-networking mode, which proxies every inbound tailnet connection
to `127.0.0.1:22`, so sshd sees *all* tailnet clients as loopback (a `from=` with only the
100.64/10 range rejected the runner with `Permission denied`). LAN and WAN sources are
still excluded. The key's private half is the repo secret `NAS_SSH_KEY`; the NAS's host
key is the repo variable `NAS_KNOWN_HOSTS`. Neither is anywhere else.

Still to do, in the Tailscale admin console (<https://login.tailscale.com/admin>; the
menu names below are the new console's — Policies, Definitions, Trust credentials, Keys):

1. **Access controls → edit the policy file.** Give the tag an owner and let it reach
   the NAS on ssh and the two health-check ports:

   ```jsonc
   "tagOwners": {
     "tag:ci": ["autogroup:admin"],
   },
   "acls": [
     // … existing rules …
     {"action": "accept", "src": ["tag:ci"], "dst": ["dxp2800:22,8737,8802"]},
   ],
   ```

2. **Settings → Trust credentials → Generate OAuth client.** Description `warren deploy
   (GitHub Actions)`; scope **Auth Keys: Write**; under it, tag `tag:ci`. Copy the client
   id and the secret — the secret is shown once. The tag has to be *defined* first —
   **Access controls → Definitions → Tags** — not merely mentioned by a rule.

   The tag picker only lists tags the *saved* policy's `tagOwners` already declares, so
   step 1 has to be saved before this dialog is opened. A client generated without the
   tag cannot be given one afterwards — it answers `403: calling actor does not have
   enough permissions` when the workflow asks for a `tag:ci` key. Delete it and generate
   a new one.
   **Or, without tags at all:** **Settings → Keys → Generate auth key** — Reusable ✓,
   Ephemeral ✓, no tags, expiry up to 90 days. The runner then joins the tailnet as *you*,
   which the existing policy already lets reach the NAS. Simpler, but it expires: put a
   reminder where you will see it, because the first symptom of an expired key is a
   merge that silently stays undeployed. Store it as the secret `TS_AUTHKEY`; when it is
   set, the OAuth pair is ignored and steps 1–2 are unnecessary.

3. **GitHub → warren → Settings → Secrets and variables → Actions.** Add the secrets —
   `TS_OAUTH_CLIENT_ID` + `TS_OAUTH_SECRET`, or `TS_AUTHKEY` — then set the variable
   `DEPLOY_ENABLED` to `true`. From a terminal that is the same thing:

   ```sh
   gh secret set TS_OAUTH_CLIENT_ID -R 0xCommanderKeen/warren   # or:
   gh secret set TS_OAUTH_SECRET -R 0xCommanderKeen/warren      #   gh secret set TS_AUTHKEY -R 0xCommanderKeen/warren
   gh variable set DEPLOY_ENABLED -R 0xCommanderKeen/warren --body true
   ```

4. **Prove it:** Actions → deploy → Run workflow → services `townhall` (the cheapest —
   a build and a copy). `deploy/status.sh` afterwards should show townhall's marker
   signed `by=<you>@github-actions-run-<id>`.

To turn it off again: `DEPLOY_ENABLED` back to `false`. To revoke the runner's access
entirely: delete the OAuth client, and delete the `warren-deploy` line from the NAS's
`~/.ssh/authorized_keys`.

### What the runner can and cannot do

It holds a key that logs in as `Miha` on the NAS — the same account the laptop deploys
as — from tailnet addresses only. That account is in the `docker` group, so the key is
root-equivalent on that machine; this is the trust the pipeline needs to recreate
containers and it is not smaller than it looks. The repo is private, so only code merged
to `main` ever runs there, and the ephemeral node is gone when the job ends.

## Moving a burrow under `~/docker/warren`

One-time, for a burrow deployed before warren#358, when `burrow`, `arcadia`, `steward` and
`steward-<id>` sat at the top of `~/docker`. `deploy.sh` refuses such a burrow
(`require_layout`) rather than moving it: chronicle's `data/`, steward's `data/`, `.env`,
`residents-key` and `residents-repo/`, and every resident's `memory/` and `claude/` are not
things it created. The move is one operator's session from a laptop, in this order, and
everything the warren runs is down for the few minutes between steps 3 and 5. Everything
here is a rename, so the way back is the same moves in reverse and `up -d` from the old
places.

1. **Nothing deploying.** `gh run list -R 0xCommanderKeen/warren --workflow deploy.yml -L 1`
   shows no run in progress, and nothing is about to merge to `main`.

2. **Re-address the residents on the burrow's checkout, while the API is still up.** The
   checkout is authoritative and the daemons compute a resident's directory from its
   manifest, so `deploy.path` must say `~/docker/warren/residents/<id>` before the daemons
   come back in the new place. For each resident on the burrow, with `STEWARD_TOKEN` from
   the burrow's steward `.env`:

   ```sh
   api=http://dxp2800:8802; auth="Authorization: Bearer $STEWARD_TOKEN"
   for id in pip life-agent; do
     curl -fsS -H "$auth" "$api/residents/$id/declaration" \
       | python3 -c 'import json, sys
   d = json.load(sys.stdin)
   text = d["text"].replace("path: ~/docker/steward-" + d["id"], "path: ~/docker/warren/residents/" + d["id"])
   print(json.dumps({"text": text}))' \
       | curl -fsS -H "$auth" -H 'Content-Type: application/json' -X PUT "$api/residents/$id/declaration" -d @- \
       | python3 -c 'import json, sys; c = json.load(sys.stdin)["commit"]; print(c["sha"], "pushed" if c["pushed"] else "NOT pushed")'
   done
   ```

   Each is one commit on `burrow/residents`. A running scheduler now refuses to start —
   the directory it computes does not exist yet — which is the downtime beginning; a
   crash-looping one keeps crash-looping.

3. **Stop everything the warren runs**, residents first, then the control plane, the
   origin, chronicle. The resident projects are named by the nursery (`-p <id>`), so name
   them the same way, or `down` finds nothing:

   ```sh
   ssh Miha@dxp2800 'set -e
     for d in ~/docker/steward-*; do docker compose -f "$d/docker-compose.yaml" --project-directory "$d" -p "${d##*/steward-}" down; done
     cd ~/docker/steward && docker compose down --remove-orphans
     cd ~/docker/arcadia && docker compose down
     cd ~/docker/burrow && docker compose down'
   ```

4. **Move.** `~/docker/life-agent` does not match `steward-*` and stays.

   ```sh
   ssh Miha@dxp2800 'set -e
     mkdir -p ~/docker/warren/residents
     mv ~/docker/burrow ~/docker/warren/burrow
     mv ~/docker/arcadia ~/docker/warren/arcadia
     mv ~/docker/steward ~/docker/warren/steward
     for d in ~/docker/steward-*; do mv "$d" ~/docker/warren/residents/"${d##*/steward-}"; done
     ls ~/docker/warren ~/docker/warren/residents'
   ```

5. **Bring it back where it now is.** Every compose file is relative to its own
   directory, so nothing in them changes. Steward comes up on the compose file it already
   has, whose `~/docker`-wide mount still covers the new place, and its scheduler starts
   once it finds `residents/<id>/memory` there.

   ```sh
   ssh Miha@dxp2800 'set -e
     cd ~/docker/warren/burrow && docker compose up -d
     cd ~/docker/warren/arcadia && docker compose up -d
     for d in ~/docker/warren/residents/*; do docker compose -f "$d/docker-compose.yaml" --project-directory "$d" -p "${d##*/}" up -d; done
     cd ~/docker/warren/steward && docker compose up -d
     sleep 30; docker ps --format "{{.Names}}\t{{.Status}}\t{{.Label \"com.docker.compose.project.working_dir\"}}" | grep -E "^(burrow|arcadia|steward)"'
   ```

   Every warren container's working directory reads `/home/Miha/docker/warren/…`, and
   `steward-scheduler` is `Up`, not `Restarting`.

6. **Deploy the fleet** from a checkout that has this section — merging its pull request
   does it, or `deploy/deploy.sh all` from the branch. Steward's compose file with the
   `residents/`-only mount lands, the checkout is fetched where it now is, and the markers
   are written where `status.sh` reads them.

7. **Reconcile each provisioned resident's bundle** with its re-addressed manifest, from
   the laptop against the checkout's manifests. The bundle's copy of `manifest.yaml` is
   the one file that differs; `--dry-run` first shows exactly that.

   ```sh
   git worktree add ../burrow-residents origin/burrow/residents 2>/dev/null; git -C ../burrow-residents pull -q
   cd steward
   CHRONICLE_URL=http://192.168.1.222:8737 uv run steward provision pip --residents ../../burrow-residents/steward/residents --dry-run
   CHRONICLE_URL=http://192.168.1.222:8737 uv run steward provision pip --residents ../../burrow-residents/steward/residents
   ```

8. **Check.** `deploy/status.sh` prints four markers and the checkout line; on the burrow
   `ls ~/docker` shows `warren` and no `burrow`, `arcadia`, `steward` or `steward-*`.
