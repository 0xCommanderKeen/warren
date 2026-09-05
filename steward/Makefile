UV ?= uv
PYTHON ?= $(UV) run python

# --------------------------------------------------------------------------------------
# the resident runtime image (docker/resident/)
# --------------------------------------------------------------------------------------

#: What `make image` tags. The nursery's default image is `steward-resident:latest`, so a
#: resident that declares no `deploy.image` runs whatever this repo last built and shipped.
IMAGE ?= steward-resident
IMAGE_VERSION ?= $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)

#: The NAS is x86_64 and this repo is usually edited on an arm64 Mac, so the build is
#: cross-arch by default. buildx --load puts the result in the local docker images list;
#: `make image-ship` is what actually gets it onto the NAS.
PLATFORM ?= linux/amd64
NAS ?= Miha@dxp2800

#: The claude CLI is pinned, not floating: an image whose brain changes under it on a
#: rebuild is an image nobody can say anything true about. Bump this deliberately — and in
#: both places: the Dockerfile carries the same literal as its `ARG CLAUDE_VERSION`
#: default, for `docker build` without this Makefile, and tests/test_resident_image.py
#: fails when the two disagree.
CLAUDE_VERSION ?= 2.1.243

#: What the built image records as `org.opencontainers.image.revision`, so that "this
#: resident runs steward-resident:latest" can be turned back into a commit.
#:
#: `-dirty` when anything the build reads is uncommitted: an image built from bytes that
#: are in no commit must not name one, or the label is a lie that reads like provenance.
#: `git status --porcelain` rather than `git diff`, because docker's build context includes
#: *untracked* files and a diff cannot see those; over this Makefile as well as
#: docker/resident, because CLAUDE_VERSION — which decides which brain the image gets — is
#: passed from here. Outside a git checkout both halves are empty and the label reads
#: `unknown`, which is the honest answer.
REVISION ?= $(shell git rev-parse HEAD 2>/dev/null || echo unknown)$(shell test -z "$$(git status --porcelain -- docker/resident Makefile 2>/dev/null)" || echo -dirty)

#: Where the emitter's source lives, for `make vendor-emitter`. Since the warren monorepo
#: (2026-08-31) that is the sibling directory `../chronicle`, not a separate checkout.
#: The emitter is still *vendored* into docker/resident/chronicle-emit.py — a copy, not a
#: symlink into ../chronicle — because the image is built from this directory alone and
#: docker cannot COPY from outside its build context. What is copied is chronicle's
#: **built bundle** (hooks/emit.py + hooks/durable.py flattened into one stdlib-only file
#: by hooks/build.py), not a source file: the emitter grew a sibling module in warren#221
#: and a directory is not something the image can carry.
#: tests/test_resident_image.py rebuilds that bundle from ../chronicle at HEAD and compares
#: it to the copy byte for byte, and CI runs that test in its `check` job — long before the
#: `image` job builds anything. A recorded checksum is what used to be compared, and it
#: could only ever catch tampering with the copy: it stayed green for a week while the
#: source moved 1,200 lines away from it (warren#234).
#: Override for a checkout elsewhere: `make vendor-emitter CHRONICLE=/path/to/chronicle`.
#:
#: `BURROW=` is still honoured as a deprecated alias (warren#216) so an operator's muscle
#: memory and any script written before the rename keeps working. It goes away with the
#: rest of the compat layer.
BURROW ?=
CHRONICLE ?= $(if $(BURROW),$(BURROW),../chronicle)

#: The committed JSON Schema artifact, at the path the schema's own id promises. Burrow
#: validates manifests against this file rather than importing this package, so a manifest
#: change that would break its reader has to show up as a diff here.
SCHEMA ?= schema/resident-manifest-v0.json

#: The committed OpenAPI document. Steward serves no schema — every route is a write path
#: — so townhall reads this file in-tree instead, and the townhall workflow lists it among
#: the paths that run that suite.
OPENAPI ?= docs/openapi.json

.PHONY: dev lint format test check validate schema schema-write openapi openapi-write build clean image image-ship image-cp image-cp-ship vendor-emitter

dev:
	$(UV) sync --all-groups

lint:
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(UV) run ty check src/ tests/

format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

test:
	$(UV) run pytest

# What CI runs, and what should be green before a commit.
check: lint test validate

validate:
	$(UV) run steward validate

schema:
	$(UV) run steward schema

# Rewrite the committed schema artifact. tests/test_schema_contract.py fails when it
# drifts from what the models generate, so this is the command that answers that failure —
# and the diff it produces is the review burrow's reader needs.
schema-write:
	$(UV) run steward schema --output $(SCHEMA)

openapi:
	$(UV) run steward openapi

# Rewrite the committed OpenAPI document. tests/test_openapi_contract.py fails when it
# drifts from the routes, so this is the command that answers that failure — and the diff
# it produces is the review townhall's hand-written client needs (warren#321).
openapi-write:
	$(UV) run steward openapi --output $(OPENAPI)

build:
	$(UV) build

clean:
	rm -rf dist .pytest_cache .ruff_cache .coverage htmlcov

# Build the image a provisioned resident runs. Not part of `check` — `check` must stay
# runnable without docker — but CI runs exactly this target in its own `image` job
# (.github/workflows/steward.yml), which is what gives the apt and npm layers, the
# entrypoint and smoke.sh any execution coverage at all.
#
# No `--pull`: the base is pinned by digest, so there is nothing newer for a pull to find.
# A layer cache can still hand back an old apt layer, which is what `--no-cache` would be
# for — but a rebuild that reruns apt is also a rebuild that can differ from the last one,
# and the honest fix for that is a snapshot, not a flag. See the Dockerfile's apt layer.
image:
	docker buildx build \
	  --platform $(PLATFORM) \
	  --load \
	  --build-arg CLAUDE_VERSION=$(CLAUDE_VERSION) \
	  --build-arg IMAGE_REVISION=$(REVISION) \
	  -t $(IMAGE):$(IMAGE_VERSION) \
	  -t $(IMAGE):latest \
	  docker/resident

# Get it onto the NAS. There is no registry in this fleet, so the image travels the same
# way everything else does: a pipe over ssh. gzip because tailscale is not a LAN.
image-ship:
	docker save $(IMAGE):$(IMAGE_VERSION) $(IMAGE):latest | gzip -1 | ssh $(NAS) 'gunzip -c | docker load'

# The control-plane image — the API and the daemons the NAS runs (docker/control-plane/
# Dockerfile). Same shape as `image`: linux/amd64 through buildx, the commit stamped in,
# and it travels the way everything else does. CP_TAG defaults to the short sha so that
# the tag on the burrow names the commit; deploy/deploy.sh passes both explicitly.
CP_IMAGE ?= steward-cp
CP_TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

image-cp:
	docker buildx build \
	  --platform $(PLATFORM) \
	  --load \
	  --build-arg IMAGE_REVISION=$(REVISION) \
	  -f docker/control-plane/Dockerfile \
	  -t $(CP_IMAGE):$(CP_TAG) \
	  -t $(CP_IMAGE):latest \
	  .

image-cp-ship:
	docker save $(CP_IMAGE):$(CP_TAG) $(CP_IMAGE):latest | gzip -1 | ssh $(NAS) 'gunzip -c | docker load'

# Refresh the vendored copy of the hook emitter: chronicle's bundle build, run against
# ../chronicle, written where the Dockerfile COPYs from.
#
# Refuses when any input is uncommitted. The artifact is a pure function of three files —
# emit.py, durable.py and build.py itself — and a copy built from bytes that are in no
# commit is a copy nobody can trace back to anything. build.py is in that list because it
# decides the artifact's shape as much as the two sources do.
#
# The revspecs below are written `HEAD:./hooks/emit.py`, cwd-relative. Plain
# `HEAD:hooks/emit.py` is resolved from the *repository root*, which was the same thing
# when burrow was its own repo and is `warren/hooks/emit.py` — a path that does not
# exist — now that the source sits at `chronicle/hooks/emit.py` in this one.
#
# No provenance file is written any more. The bundle's own header records the sha256 of
# each source it was built from, which is what a byte comparison can check; a commit sha
# recorded beside it would go stale on the next rebase while the bytes stayed identical.
# The commit is echoed below for the human running this, and `git log -1 -- hooks/` in
# chronicle answers the same question at any later date.
BUNDLE_SOURCES = hooks/emit.py hooks/durable.py hooks/build.py hooks/presence.py hooks/delivery_worker.py

vendor-emitter:
	@git -C "$(CHRONICLE)" rev-parse --git-dir >/dev/null 2>&1 || { echo "no checkout at $(CHRONICLE); pass CHRONICLE=/path/to/chronicle"; exit 1; }
	@set -eu; \
	 for source in $(BUNDLE_SOURCES); do \
	   git -C "$(CHRONICLE)" cat-file -e "HEAD:./$$source" 2>/dev/null || { echo "$(CHRONICLE)/$$source is not committed at HEAD; commit it first"; exit 1; }; \
	   git -C "$(CHRONICLE)" diff --quiet HEAD -- "$$source" || { echo "$(CHRONICLE)/$$source has uncommitted changes; commit them first"; exit 1; }; \
	 done
	@set -eu; \
	 provenance=$$(git -C "$(CHRONICLE)" log -1 --format='%H (%cs)' -- $(BUNDLE_SOURCES) || true); \
	 $(PYTHON) "$(CHRONICLE)/hooks/build.py" --output docker/resident/chronicle-emit.py; \
	 echo "vendored chronicle's emitter bundle @ $${provenance:-a commit this shallow clone cannot name}"
