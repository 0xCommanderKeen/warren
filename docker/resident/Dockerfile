# syntax=docker/dockerfile:1
#
# The image a provisioned resident runs.
#
# Before this existed the nursery shipped `python:3.12-slim` running `sleep infinity`:
# the right name, the right volumes, the right environment — and no `claude` to run a
# session with and no emitter to tell the village about it. This image is that gap closed
# (steward #51). Build it with `make image`, put it on the NAS with `make image-ship`.
#
# ## What is in here, and why each piece
#
# `node:22-slim`      The claude CLI is an npm package, so the base is node. Slim rather
#                     than full because everything full adds that we actually need — git,
#                     python3 — is installed below by name, and a resident's image should
#                     be readable in one screen. (life-agent's hand-rolled container uses
#                     full `node:22` and apt-gets the rest *at runtime, on every cold
#                     start*; baking it is the whole point of moving.)
# `@anthropic-ai/…`   Pinned by build arg. A resident whose brain silently changed version
#                     under it on a rebuild is a resident nobody can say anything true
#                     about, so the pin lives in the Makefile and moves deliberately.
# `python3`           The burrow emitter is stdlib-only python. No pip, no venv: emit.py
#                     imports json, os, sys, time, urllib, fcntl, hashlib, datetime and
#                     nothing else, which is exactly why it can be vendored as one file.
# `git`               claude reads and writes repos; a resident without git is a resident
#                     that cannot do most of the work it would be asked to do.
# `curl`              For `steward-smoke`, which needs an HTTP status code it can assert on.
#
# ## What runs
#
# `sleep infinity`, under an entrypoint that seeds the claude config volume. That is the
# honest default and it is *not* a placeholder for a missing feature: a resident's
# container is a place for sessions to happen, and steward drives the brain from outside.
#
# Be precise about where that stands today: **steward's scheduler runs sessions locally**,
# in the process running `steward scheduler run` (see steward/runners.py — subprocess.Popen
# with cwd set to the resident's workdir). Nothing in steward execs into this container
# yet. This image is what makes that possible — `docker exec steward-<id> claude -p …`
# works the moment somebody wires it — but wiring it is the pilot's step, not something
# this image can claim on its own.
#
# ## Arch
#
# The NAS (dxp2800) is x86_64 and this repo is usually edited on an arm64 Mac, so
# `make image` builds `--platform linux/amd64` through buildx. Building without buildx on
# an Apple Silicon machine produces an arm64 image that will not start on the NAS.

ARG NODE_TAG=22-slim
FROM node:${NODE_TAG}

ARG CLAUDE_VERSION=2.1.243
ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="steward-resident"
LABEL org.opencontainers.image.description="Runtime for a steward resident: claude CLI plus burrow's hook emitter."
LABEL org.opencontainers.image.source="https://github.com/0xCommanderKeen/steward"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# Pinned, and asked for its version immediately: a build that produced an image whose
# claude does not run should fail here, not three days later inside a routine at 07:00.
RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
    && npm cache clean --force \
    && claude --version

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    # The pin above is the whole point; a CLI that updated itself in place would quietly
    # undo it and nobody would be able to say what version wrote what.
    DISABLE_AUTOUPDATER=1 \
    # emit.py mirrors every event to http://127.0.0.1:8737 by default, which is a
    # developer's laptop convenience: inside a container nothing is ever listening there,
    # and every hook would pay a refused connection and write a breaker file for it.
    BURROW_MIRROR="" \
    TZ=Europe/Ljubljana

# Baked under /opt/steward, not /root/.claude: that path is a bind mount from the host and
# a mount hides whatever the image put there. The entrypoint copies these into the volume
# at start — see entrypoint.sh for what it will and will not overwrite.
COPY burrow-emit.py /opt/steward/burrow-emit.py
COPY settings.json /opt/steward/settings.json
COPY entrypoint.sh /usr/local/bin/steward-entrypoint
COPY smoke.sh /usr/local/bin/steward-smoke
RUN chmod 0755 /usr/local/bin/steward-entrypoint /usr/local/bin/steward-smoke \
    && chmod 0644 /opt/steward/burrow-emit.py /opt/steward/settings.json

# Root, deliberately: the compose fragment mounts the claude config at /root/.claude and
# the resident's memory at a path its manifest names, both bind mounts owned by the host
# user. One caveat worth knowing before it surprises somebody — claude refuses
# `--permission-mode bypassPermissions` when it is running as root, so a resident that
# needs unattended edits uses `acceptEdits`, exactly as life-agent does today.
WORKDIR /root
ENTRYPOINT ["/usr/local/bin/steward-entrypoint"]
CMD ["sleep", "infinity"]
