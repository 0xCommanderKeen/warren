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
- **A marker.** `~/docker/<dir>/DEPLOYED-<service>` — revision, service, time, who. The
  only thing on the NAS that can say what it is running; `status.sh` reads them.
- **A clean HEAD.** The deployed tree must be one git can name. `ALLOW_DIRTY=1` overrides
  that, out loud. `SKIP_TESTS=1` skips each service's own check first.
- **Steward's data backed up** beside itself before every rollout (three kept), and its
  image tag written into the burrow's `.env`, so a rollback is one line and `up -d`.

## What lives on the burrow that the repo does not carry

Secrets, in a `.env` beside each compose file, mode `0600`, never in git:

| Directory | `.env` holds | Compose file (in the repo) |
| --- | --- | --- |
| `~/docker/burrow` | `CHRONICLE_NOTIFY_URL` (private ntfy topic); `CHRONICLE_TOKEN` when ingest is closed | [`chronicle/deploy/compose.yaml`](../chronicle/deploy/compose.yaml) |
| `~/docker/arcadia` | — | [`arcadia/deploy/compose.yaml`](../arcadia/deploy/compose.yaml) + `nginx.conf` |
| `~/docker/steward` | `STEWARD_TOKEN`; `STEWARD_IMAGE_TAG` (written by the script); chat tokens per `steward/docs/chat.md` | [`steward/deploy/compose.yaml`](../steward/deploy/compose.yaml) |

`deploy.sh` refuses to roll out a service whose `.env` is missing and says what it must
contain. Data — chronicle's `/data`, steward's `data/` — is never written by the script.

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
