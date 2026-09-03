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
#                     start*; baking it is the whole point of moving.) Pinned by digest,
#                     not by that tag — see "The base pin" below.
# `@anthropic-ai/…`   Pinned by build arg. A resident whose brain silently changed version
#                     under it on a rebuild is a resident nobody can say anything true
#                     about, so the pin lives in the Makefile and moves deliberately.
# `python3`           The chronicle emitter is stdlib-only python: no pip, no venv, no
#                     import that is not in the standard library — asserted over the file
#                     copied in below and over the module embedded in it, by this repo's
#                     suite and by chronicle's. Its *source* is two files (hooks/emit.py
#                     and the durable outbox it grew in warren#221); what is copied in is
#                     the one-file bundle chronicle's hooks/build.py flattens them into,
#                     because a build context is a directory and an image should not have
#                     to carry one.
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
# Where sessions actually run is the manifest's choice (steward #58): the default
# `runner.placement: local` keeps them in the process running `steward scheduler run`,
# and `placement: container` runs them in here — `docker exec steward-<id> claude -p …`,
# built and killed by steward/runners.py, with the memory volume as the working
# directory and only the named session variables in the environment. The compose file
# steward renders runs this image under docker's own init (`init: true`), so what a
# killed session leaves behind is reaped rather than accumulating as zombies.
#
# ## Arch
#
# The NAS (dxp2800) is x86_64 and this repo is usually edited on an arm64 Mac, so
# `make image` builds `--platform linux/amd64` through buildx. Building without buildx on
# an Apple Silicon machine produces an arm64 image that will not start on the NAS.
#
# ## The base pin
#
# `node:22-slim` is a tag, and a tag moves: the same `docker build` a month apart is two
# different images, and nothing afterwards can say which one a resident got. The digest
# below is the *manifest list* for that tag — one digest that still resolves per-arch, so
# it works for both the amd64 build for the NAS and an arm64 build on the Mac. Pinning a
# per-arch digest instead would build on exactly one of those two machines.
#
# Resolved 2026-08-31 from `node:22-slim` = `22-bookworm-slim`, pushed 2026-08-25.
# To move it — deliberately, as its own commit, with the date above updated:
#
#   $ docker buildx imagetools inspect node:22-slim \
#       --format '{{.Manifest.Digest}} {{.Manifest.MediaType}}'
#
# The MediaType must say `index` or `manifest.list` — that is how you know you copied the
# multi-arch digest and not one architecture's. CI builds this image on every PR that
# touches it (.github/workflows/steward.yml), so a bad digest fails there, not on the NAS.

FROM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5

ARG CLAUDE_VERSION=2.1.243
ARG DEBIAN_FRONTEND=noninteractive

# What commit this image was built from. `make image` passes the real one; the default is
# `unknown` because a bare `docker build docker/resident` genuinely does not know, and a
# label that guessed would be worse than one that admits it.
ARG IMAGE_REVISION=unknown

LABEL org.opencontainers.image.title="steward-resident"
LABEL org.opencontainers.image.description="Runtime for a steward resident: claude CLI plus chronicle's hook emitter."
LABEL org.opencontainers.image.source="https://github.com/0xCommanderKeen/warren"
LABEL org.opencontainers.image.revision="${IMAGE_REVISION}"

# Not version-pinned, deliberately, and this is the one floating input left. Debian points
# a suite at whatever is current, so `curl=7.88.1-10+deb12u5` stops resolving the moment
# bookworm ships the next security update and the build starts failing for the one reason
# nobody wants: because a patch landed. Freezing it properly means snapshot.debian.org and
# a second thing to refresh; what these four packages actually give a resident (a CA
# bundle, an HTTP client, git, a python) is stable across bookworm point releases in a way
# the claude CLI and the node runtime are not — which is why those two are pinned and this
# is not. Revisit if a bookworm update ever breaks a resident.
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
    CHRONICLE_MIRROR="" \
    TZ=Europe/Ljubljana

# Baked under /opt/steward, not /root/.claude: that path is a bind mount from the host and
# a mount hides whatever the image put there. The entrypoint copies these into the volume
# at start — see entrypoint.sh for what it will and will not overwrite.
COPY chronicle-emit.py /opt/steward/chronicle-emit.py
COPY settings.json /opt/steward/settings.json
COPY entrypoint.sh /usr/local/bin/steward-entrypoint
COPY smoke.sh /usr/local/bin/steward-smoke
RUN chmod 0755 /usr/local/bin/steward-entrypoint /usr/local/bin/steward-smoke \
    && chmod 0644 /opt/steward/chronicle-emit.py /opt/steward/settings.json

# Root, deliberately: the compose fragment mounts the claude config at /root/.claude and
# the resident's memory at a path its manifest names, both bind mounts owned by the host
# user. One caveat worth knowing before it surprises somebody — claude refuses
# `--permission-mode bypassPermissions` when it is running as root, so a resident that
# needs unattended edits uses `acceptEdits`, exactly as life-agent does today.
WORKDIR /root
ENTRYPOINT ["/usr/local/bin/steward-entrypoint"]
CMD ["sleep", "infinity"]
