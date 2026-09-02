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
from pathlib import Path
from typing import Any

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


def openapi_document() -> dict[str, Any]:
    """Return the app's own OpenAPI document, built against nothing that outlives the call.

    A throwaway residents tree, an in-memory database and an emitter that drops what it is
    given: the schema is a function of the route declarations alone, so the collaborators
    exist only because ``create_app`` needs them. Everything opened here is closed here.
    """
    store = Store(":memory:")
    with tempfile.TemporaryDirectory(prefix="steward-openapi-") as scratch:
        app = create_app(
            ApiConfig(residents_dir=Path(scratch) / "residents", token=EXPORT_TOKEN),
            store=store,
            emitter=NullEmitter(),
        )
        try:
            return app.openapi()
        finally:
            # The lifespan never runs, so nothing else will close these.
            app.state.approval_outbox.close()
            app.state.runs.shutdown()
            store.close()


def openapi_json() -> str:
    """Render the document exactly as the committed artifact holds it: sorted, indent 2.

    One function so the CLI, the make target and the drift test cannot disagree about
    whitespace — a contract test that failed over a missing trailing newline would teach
    everyone to stop reading it. Keys are sorted for the same reason chronicle's
    ``docs/openapi.json`` sorts them: the diff a reviewer reads should be the change, not
    a reordering.
    """
    return json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"
