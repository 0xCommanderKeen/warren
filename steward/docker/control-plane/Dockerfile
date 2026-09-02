# syntax=docker/dockerfile:1
#
# The image steward's control plane runs as on a burrow: the API (`steward serve`) and the
# daemons (`steward scheduler run`, `steward watchdog run`), one image, one process each —
# see deploy/compose.yaml beside this repo's deploy script for how they are wired.
#
# Before this existed the NAS ran a `steward-cp:0.2.0-amd64` built by hand on 2026-08-25
# from a Dockerfile that lived nowhere: `python:3.14`, node from nodesource, an unpinned
# `npm install -g @anthropic-ai/claude-code`, `pip install uv`. It answered for eight days
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
# `node` + `claude`   `steward scheduler run` fires locally-placed sessions as `claude -p`
#                     in *this* process's container (Pip runs that way today), so the CLI
#                     has to be here. node is copied out of the digest-pinned `node:22-slim`
#                     the resident image is built FROM (docker/resident/Dockerfile) rather
#                     than installed from a third repository, so the two images cannot
#                     disagree about which node a claude runs on. claude itself is pinned by
#                     the same CLAUDE_VERSION build arg the Makefile passes to both images.
# `docker`            A burrow's daemons reach containers through a *local* docker client
#                     (docs/topology.md): `docker inspect`/`restart` for the watchdog,
#                     `docker exec` for a container-placed session. The CLI is copied from
#                     the official `docker:<ver>-cli` image, pinned by digest; the socket
#                     is the compose file's decision, not this one's.
# `git`               Declaration and skill writes are git-backed (steward#292); a control
#                     plane without git cannot author anything and says so honestly.
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
# the same day from `docker:29.7.2-cli` — the NAS runs a 29.x daemon.

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1

ARG CLAUDE_VERSION=2.1.243
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
    && rm -rf /var/lib/apt/lists/*

# node and npm, out of the resident image's pinned base rather than a third source.
COPY --from=node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version

# Pinned, and asked for its version immediately: a build whose claude does not run should
# fail here, not inside a routine at 07:00.
RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && npm cache clean --force \
    && claude --version

# The docker *client* only. Whether it can reach a daemon is the compose file's
# `/var/run/docker.sock` mount; without one, `steward doctor` says so by name.
COPY --from=docker:29.7.2-cli@sha256:3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e /usr/local/bin/docker /usr/local/bin/docker

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    # The pin above is the whole point; a CLI that updated itself in place would quietly
    # undo it and nobody would be able to say what version wrote what.
    DISABLE_AUTOUPDATER=1 \
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

# The control plane's own tree: the package, and the residents, skills and schema the API
# validates against and the scheduler reads its routines from. What is NOT here is anything
# under local/, demo/, tests/ or .venv — see .dockerignore.
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
