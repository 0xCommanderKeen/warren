# syntax=docker/dockerfile:1
#
# The image steward's control plane runs as on a burrow: the API (`steward serve`) and the
# daemons (`steward scheduler run`, `steward watchdog run`), one image, one process each —
# see deploy/compose.yaml beside this repo's deploy script for how they are wired.
#
# Before this existed the NAS ran a `steward-cp:0.2.0-amd64` built by hand on 2026-08-25
# from a Dockerfile that lived nowhere: `python:3.14`, a locally installed model CLI, and
# `pip install uv`. It answered for eight days
# and ninety merged PRs, and nothing in the repo could say what was in it. This file is
# that image with its inputs pinned and its recipe in git. Build it with `make image-cp`,
# put it on the NAS with `make image-cp-ship`, or let deploy/deploy.sh do both.
#
# ## What is in here, and why each piece
#
# `uv:python3.14-…`   The same base chronicle's own server runs on. Python 3.14 is what
#                     pyproject requires, uv is how the locked environment is installed,
#                     and bookworm-slim is the same Debian the node binary below was
#                     built against. Pinned by the index digest — see "The base pin".
# `docker`            A burrow's daemons reach containers through a *local* docker client
#                     (docs/topology.md): `docker inspect`/`restart` for the watchdog,
#                     `docker exec` for a container-placed session. The CLI is copied from
#                     the official `docker:<ver>-cli` image, pinned by digest; the socket
#                     is the compose file's decision, not this one's.
# `git`               Declaration and skill writes are git-backed (steward#292); a control
#                     plane without git cannot author anything and says so honestly. The
#                     tree it writes is the burrow's checkout, bind-mounted at /checkout
#                     (deploy/compose.yaml; warren#351) — not the seed COPYed below.
# `ssh`               What git pushes the checkout through. GitHub's host keys are baked in
#                     so a fresh burrow's first push is verified rather than trusted on
#                     first use, and GIT_SSH_COMMAND names the deploy key the compose file
#                     mounts.
# `curl`              Health checks from inside the container, and `steward doctor`'s probes.
#
# ## Arch
#
# The NAS (dxp2800) is x86_64 and this repo is usually edited on an arm64 Mac, so
# `make image-cp` builds `--platform linux/amd64` through buildx, exactly as `make image`
# does for the resident image.
#
# ## The base pin
#
# Tags move. `ghcr.io/astral-sh/uv:python3.14-bookworm-slim` is a tag, and the digest
# below is the *index* (multi-arch) digest for it, resolved 2026-09-02 with
#
#   $ docker buildx imagetools inspect ghcr.io/astral-sh/uv:python3.14-bookworm-slim
#
# The MediaType must say `index` — that is how you know you copied the multi-arch digest
# and not one architecture's. To move it: re-run the line above, paste the new digest,
# update this date, and make that its own commit. The node digest is the resident image's
# (docker/resident/Dockerfile carries its provenance); the docker CLI digest was resolved
# the same day from `docker:29.7.2-cli` — the NAS runs a 29.x daemon. The compose plugin
# digest was resolved 2026-09-03 from `docker/compose-bin:v5.1.3`, the version the NAS's
# own `docker compose version` reports.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1

ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="steward-cp"
LABEL org.opencontainers.image.description="Steward's control plane: the API and the scheduler/watchdog daemons."
LABEL org.opencontainers.image.source="https://github.com/0xCommanderKeen/warren"

# Not version-pinned, for the reason docker/resident/Dockerfile gives: a bookworm point
# release would otherwise break the build for the one reason nobody wants.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# GitHub's SSH host keys as it publishes them (`gh api meta -q .ssh_keys`, the same list
# as api.github.com/meta), copied 2026-09-02. A burrow's first push has to be able to
# verify the host it is handing the fleet's history to, and StrictHostKeyChecking=yes
# below refuses any other. Rotate them the way the base pin is moved: re-run the command,
# paste, update this date, own commit.
RUN printf '%s\n' \
    'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' \
    'github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=' \
    'github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=' \
    > /etc/ssh/ssh_known_hosts \
    # The checkout is a bind mount owned by whoever made it on the burrow — root, through
    # this image, or the operator by hand — and git refuses a repository owned by somebody
    # else unless told the path is safe. Told here, for that one path, so a checkout made
    # either way is one the API can commit into rather than a 409 not_a_git_checkout.
    && git config --system safe.directory /checkout

# The docker *client* only. Whether it can reach a daemon is the compose file's
# `/var/run/docker.sock` mount; without one, `steward doctor` says so by name.
COPY --from=docker:29.7.2-cli@sha256:3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e /usr/local/bin/docker /usr/local/bin/docker
# And the compose plugin beside it, where the CLI looks for plugins: the API brings a
# resident of its own burrow up with `docker compose up -d` in this process
# (steward.deploy.BurrowTransport), and `docker compose` without the plugin is
# "unknown command".
COPY --from=docker/compose-bin:v5.1.3@sha256:6d7c88928df9682427c7d5d0107be041e499c0f5a46760b2330274d1173faa48 /docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    # The pin above is the whole point; a CLI that updated itself in place would quietly
    # undo it and nobody would be able to say what version wrote what.
    DISABLE_AUTOUPDATER=1 \
    # How git reaches GitHub from a burrow: the deploy key the compose file mounts into the
    # API (and deploy/deploy.sh into its one-off `docker run`s), that key only, the host
    # keys above only, and never a prompt — a daemon has nobody to answer one.
    GIT_SSH_COMMAND="ssh -i /run/steward/residents-key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=10" \
    # The scheduler's state file; steward.db lands beside it. /data is the compose
    # file's volume, and the same path the hand-built image used, so an existing
    # burrow's database is found where it already is.
    STEWARD_STATE=/data/state.json \
    # uv's environment for the project below, and put its console script on PATH so
    # the compose commands read `steward serve …` rather than `uv run --no-dev …`.
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    TZ=Europe/Ljubljana

WORKDIR /app

# Dependencies first, so a source-only change does not reinstall them. `--no-install-project`
# leaves steward itself for the layer that has its source.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# The control plane's own tree: the package, the schema, and the residents and skills as a
# *seed*. On a burrow nothing reads /app/residents (warren#351): the compose file mounts
# the burrow's own checkout at /checkout and points every process there, and this copy is
# what a bare `docker run` of the image serves and what a new burrow is cloned from. What
# is NOT here is anything under local/, demo/, tests/ or .venv — see .dockerignore.
COPY src ./src
COPY residents ./residents
COPY skills ./skills
COPY schema ./schema
RUN uv sync --frozen --no-dev \
    && steward --help >/dev/null

# What commit this image was built from. `make image-cp` passes the real one; the default
# is `unknown` because a bare `docker build` genuinely does not know, and a label that
# guessed would be worse than one that admits it. Last, on purpose: a build arg
# invalidates every layer after its first use, and the revision changes on every commit
# while nothing above it does.
ARG IMAGE_REVISION=unknown
LABEL org.opencontainers.image.revision="${IMAGE_REVISION}"

EXPOSE 8801

# The API, on every interface of the container: the compose file decides which host port
# that becomes (8802 on the NAS) and the network it is reachable on (the tailnet).
CMD ["steward", "serve", "--host", "0.0.0.0", "--port", "8801", "--residents", "residents"]
