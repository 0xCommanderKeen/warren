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
#: hooks/emit.py is still *vendored* into docker/resident/burrow-emit.py — a copy, not a
#: symlink into ../chronicle — because the image is built from this directory alone and
#: docker cannot COPY from outside its build context. tests/test_resident_image.py fails
#: when the copy drifts from the recorded checksum, and CI runs that test in its `check`
#: job — long before the `image` job builds anything.
#: Override for a checkout elsewhere: `make vendor-emitter BURROW=/path/to/chronicle`.
BURROW ?= ../chronicle

#: The committed JSON Schema artifact, at the path the schema's own id promises. Burrow
#: validates manifests against this file rather than importing this package, so a manifest
#: change that would break its reader has to show up as a diff here.
SCHEMA ?= schema/resident-manifest-v0.json

.PHONY: dev lint format test check validate schema schema-write build clean image image-ship vendor-emitter

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

# Refresh the vendored copy of the hook emitter, and record where it came from.
# Refuses when the source copy is uncommitted: a provenance line naming a commit that
# does not contain these bytes would be worse than no provenance line at all.
#
# Every revspec below is written `HEAD:./hooks/emit.py`, cwd-relative. Plain
# `HEAD:hooks/emit.py` is resolved from the *repository root*, which was the same thing
# when burrow was its own repo and is `warren/hooks/emit.py` — a path that does not
# exist — now that the source sits at `chronicle/hooks/emit.py` in this one.
vendor-emitter:
	@git -C "$(BURROW)" rev-parse --git-dir >/dev/null 2>&1 || { echo "no checkout at $(BURROW); pass BURROW=/path/to/chronicle"; exit 1; }
	@git -C "$(BURROW)" cat-file -e HEAD:./hooks/emit.py 2>/dev/null || { echo "$(BURROW)/hooks/emit.py is not committed at HEAD; commit it first"; exit 1; }
	@git -C "$(BURROW)" diff --quiet HEAD -- hooks/emit.py || { echo "$(BURROW)/hooks/emit.py has uncommitted changes; commit them first"; exit 1; }
	@set -eu; \
	 commit=$$(git -C "$(BURROW)" log -1 --format=%H -- hooks/emit.py); \
	 when=$$(git -C "$(BURROW)" log -1 --format=%cs -- hooks/emit.py); \
	 test -n "$$commit"; \
	 test -n "$$when"; \
	 sum=$$($(PYTHON) -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$(BURROW)/hooks/emit.py"); \
	 test -n "$$sum"; \
	 { \
	   echo "# Vendored from chronicle. DO NOT EDIT HERE — edit chronicle/hooks/emit.py and"; \
	   echo "# re-run \`make vendor-emitter\` in warren/steward/."; \
	   echo "#"; \
	   echo "# origin: https://github.com/0xCommanderKeen/warren  chronicle/hooks/emit.py"; \
	   echo "# commit: $$commit ($$when)"; \
	   echo "# sha256: $$sum (of every byte below the marker line)"; \
	   echo "#"; \
	   echo "# A copy rather than a submodule so this image builds with one repo checked"; \
	   echo "# out. tests/test_resident_image.py re-hashes the copy against"; \
	   echo "# docker/resident/burrow-emit.sha256 on every CI run, so drift is a failed"; \
	   echo "# test rather than a resident that emits a protocol nobody supports."; \
	   echo "# --- upstream copy begins below; every byte after this line is burrow's, verbatim ---"; \
	   cat "$(BURROW)/hooks/emit.py"; \
	 } > docker/resident/burrow-emit.py; \
	 { \
	   echo "# Where docker/resident/burrow-emit.py came from, and what it hashed to."; \
	   echo "# Written by \`make vendor-emitter\`; read by tests/test_resident_image.py."; \
	   echo "commit: $$commit"; \
	   echo "sha256: $$sum"; \
	 } > docker/resident/burrow-emit.sha256; \
	 echo "vendored chronicle/hooks/emit.py @ $$commit ($$sum)"
