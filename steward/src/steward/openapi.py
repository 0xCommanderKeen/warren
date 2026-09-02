"""The OpenAPI document, rendered offline so it can be committed and read elsewhere.

Every route steward serves is a write path, so it serves nothing unauthenticated —
including its own schema (``create_app`` sets ``docs_url``, ``redoc_url`` and
``openapi_url`` to ``None``). That leaves the console with a hand-written client and prose
for a contract, which is how a renamed body field reaches townhall as a refusal somebody
found by clicking.

So the document is exported rather than served: build the app in a throwaway directory,
ask it for its own schema, and commit the answer at :data:`OPENAPI_ARTIFACT`. Townhall
reads that file in-tree, ``tests/test_openapi_contract.py`` fails when it drifts from the
app, and no door opens on the running steward.
"""

import json
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from steward.api import ApiConfig, create_app
from steward.events import NullEmitter
from steward.store import Store

#: Where the committed document lives, relative to this repository's root. Townhall's
#: contract test reads it at `steward/docs/openapi.json` and the townhall workflow lists
#: it among the paths that run that suite.
OPENAPI_ARTIFACT = "docs/openapi.json"

#: A token that exists for the length of one export. ``create_app`` refuses to build
#: without one, and an app built for its schema never answers a request, so this is a
#: placeholder rather than a credential — nothing is served and nothing stores it.
EXPORT_TOKEN = "openapi-export"  # noqa: S105 — not a credential; see above


@contextmanager
def export_app() -> Iterator[FastAPI]:
    """Build the app the document is read from, against nothing that outlives the block.

    A throwaway residents tree, an in-memory database and an emitter that drops what it is
    given: the schema is a function of the route declarations alone, so the collaborators
    exist only because ``create_app`` needs them.

    The config is built here rather than read from the environment on purpose. A document
    that depended on whoever exported it would make the drift test red on one machine and
    green on the next, which is the fastest way to make a contract test hated.

    Everything opened here is closed here, through an :class:`~contextlib.ExitStack` so
    that one teardown raising cannot skip the others — and so that a ``create_app`` that
    refuses still closes the database it was handed.
    """
    with ExitStack() as stack:
        scratch = stack.enter_context(tempfile.TemporaryDirectory(prefix="steward-openapi-"))
        store = Store(":memory:")
        stack.callback(store.close)
        app = create_app(
            ApiConfig(residents_dir=Path(scratch) / "residents", token=EXPORT_TOKEN),
            store=store,
            emitter=NullEmitter(),
        )
        # The lifespan never runs, so nothing else will close these two.
        stack.callback(app.state.runs.shutdown)
        stack.callback(app.state.approval_outbox.close)
        yield app


def openapi_document() -> dict[str, Any]:
    """Return the app's own OpenAPI document."""
    with export_app() as app:
        return app.openapi()


def openapi_json() -> str:
    """Render the document exactly as the committed artifact holds it: sorted, indent 2.

    One function so the CLI, the make target and the drift test cannot disagree about
    whitespace — a contract test that failed over a missing trailing newline would teach
    everyone to stop reading it. Keys are sorted for the same reason chronicle's
    ``docs/openapi.json`` sorts them: the diff a reviewer reads should be the change, not
    a reordering.
    """
    return json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"
