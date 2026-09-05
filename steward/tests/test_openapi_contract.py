"""The committed OpenAPI document, checked against the app that generates it.

Townhall talks to this API through a hand-written client (``townhall/src/steward/client.js``)
whose only contract was the prose in ``docs/api.md``. Prose does not fail a build: a renamed
body field or a moved route reached the console as a refusal a human found by clicking, and
warren#242 pinned only the *route* half of that seam — which paths the origin proxies, not
what travels along them.

So the document is exported offline and committed, the same way the manifest JSON Schema is
(``tests/test_schema_contract.py``): regenerate in-process, compare bytes, and name the
regeneration command in the failure. ``townhall`` reads the committed file in-tree, so a
change here runs that suite too (``.github/workflows/townhall.yml``).

Offline is the constraint, not a preference. Every route is a write path, so steward serves
nothing unauthenticated — including its schema (``docs_url``/``redoc_url``/``openapi_url``
are ``None``, pinned by ``test_the_schema_is_not_served_unauthenticated``). The export builds
the app in a throwaway directory and asks it for the document; it opens no door.
"""

import json
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from steward.openapi import OPENAPI_ARTIFACT, export_app, openapi_document, openapi_json

DOCUMENT_FILE = REPO_ROOT / OPENAPI_ARTIFACT

#: What answers a failure here. Named in the message, because a drift test whose failure
#: does not say how to fix it just gets the artifact deleted.
REGENERATE = "make openapi-write"

#: The doors this API keeps shut. A route at any of these is the schema being served.
SCHEMA_PATHS = frozenset({"/openapi.json", "/docs", "/redoc"})


def test_the_committed_document_exists() -> None:
    assert DOCUMENT_FILE.is_file(), (
        f"{OPENAPI_ARTIFACT} is missing — townhall's contract test reads it in-tree. "
        f"Write it with:  {REGENERATE}"
    )


def test_the_committed_document_matches_the_running_app() -> None:
    """The artifact is only a contract while it is the same bytes the app generates."""
    assert DOCUMENT_FILE.read_text(encoding="utf-8") == openapi_json(), (
        f"{OPENAPI_ARTIFACT} no longer matches the app's own schema.\n"
        "Regenerate it and review the diff for townhall impact — a renamed body field or a "
        "moved route breaks the console's client (townhall/src/steward/client.js), and a "
        "fastapi or pydantic bump can move the emission on its own:\n"
        f"  {REGENERATE}"
    )


def test_the_export_is_deterministic() -> None:
    """Two exports of one unchanged app are the same bytes, or the drift test is noise."""
    assert openapi_json() == openapi_json()


def test_declaration_input_publishes_the_resident_mount_contract() -> None:
    schemas = openapi_document()["components"]["schemas"]

    assert schemas["Mount"]["properties"]["mode"]["enum"] == ["rw", "ro"]
    manifest = schemas["DeclarationPut"]["properties"]["manifest"]
    assert {branch.get("$ref") for branch in manifest["anyOf"]} >= {
        "#/components/schemas/ResidentManifest"
    }


def test_the_export_does_not_open_the_unauthenticated_door() -> None:
    """Exporting is offline: it renders the document, it does not start serving one.

    Asserted against the app rather than against the document it produces. FastAPI
    registers ``/docs`` and ``/openapi.json`` with ``include_in_schema=False``, so they
    are absent from ``paths`` whether or not they are being served — a test that looked
    for them there would stay green through exactly the regression it names.
    """
    with export_app() as app:
        assert app.openapi_url is None
        assert app.docs_url is None
        assert app.redoc_url is None
        assert not [route for route in app.routes if getattr(route, "path", "") in SCHEMA_PATHS]
        assert app.openapi()["info"]["title"] == "steward"


def test_the_export_writes_nothing_where_it_is_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `make openapi-write` in a checkout must not leave a database or a state file.

    steward's default state path is relative to the working directory, so an export that
    built the app the way `steward serve` does would scatter `.steward/` into whatever
    directory somebody ran it from.
    """
    monkeypatch.chdir(tmp_path)
    openapi_json()

    assert list(tmp_path.iterdir()) == []


def test_the_export_reads_no_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The document must be a function of the routes alone, not of whoever exported it.

    The export builds its own config rather than reading one, so a developer with
    ``STEWARD_TOKEN`` or ``STEWARD_RESIDENTS`` set exports the same bytes as CI. Without
    this the drift test above would be red on somebody's machine and green on everyone
    else's, which is the fastest way to make a contract test hated.
    """
    baseline = openapi_json()
    monkeypatch.setenv("STEWARD_TOKEN", "somebody-elses-token")
    monkeypatch.setenv("STEWARD_CORS_ORIGINS", "https://example.invalid")
    monkeypatch.setenv("STEWARD_RESIDENTS", "/does/not/exist")

    assert openapi_json() == baseline


def test_the_document_types_every_body_a_write_accepts() -> None:
    """Every mutating route with a body names a component, and every component has fields.

    Request models and the first response path name real components; remaining response
    debt is tracked separately in docs/response-migration.md.
    """
    document = openapi_document()
    components = document["components"]["schemas"]
    bodies = {
        f"{method.upper()} {path}": operation["requestBody"]["content"]["application/json"][
            "schema"
        ]
        for path, operations in document["paths"].items()
        for method, operation in operations.items()
        if "requestBody" in operation
    }

    assert bodies, "no route declares a body — the reader has gone stale"
    for path, schema in bodies.items():
        # An optional body (`ProvisionPost | None`) arrives as an `anyOf` with a null
        # branch, so the reference is read from either spelling.
        named = [
            branch["$ref"].rsplit("/", 1)[-1]
            for branch in [schema, *schema.get("anyOf", [])]
            if "$ref" in branch
        ]
        assert named, f"{path} inlines its body schema instead of naming a component"
        for name in named:
            assert components[name].get("properties"), f"{path} declares a body with no fields"


def test_success_responses_are_typed_or_explicitly_inventoried() -> None:
    """New routes must publish a contract or make their migration debt reviewable."""
    document = openapi_document()
    expected_typed = {
        "GET /requests 200": "RequestListResponse",
        "GET /requests/{request_id} 200": "RequestResponse",
        "GET /routines 200": "RoutineListResponse",
        "POST /residents/{resident_id}/routines/{routine_id}/run 202": "RoutineRunReceipt",
    }
    remaining = set()
    found_typed = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            for code, response in operation["responses"].items():
                if not code.startswith("2"):
                    continue
                key = f"{method.upper()} {path} {code}"
                schema = response.get("content", {}).get("application/json", {}).get("schema", {})
                if "$ref" in schema:
                    name = schema["$ref"].rsplit("/", 1)[-1]
                    component = document["components"]["schemas"][name]
                    assert component.get("properties"), key
                    assert component.get("additionalProperties") is False, key
                    found_typed[key] = name
                else:
                    remaining.add(key)
    assert found_typed == expected_typed
    inventory = (REPO_ROOT / "docs/response-migration.md").read_text(encoding="utf-8")
    listed = [
        line.removeprefix("- `").removesuffix("`")
        for line in inventory.splitlines()
        if line.startswith("- `")
    ]
    assert len(listed) == len(set(listed)), "duplicate migration entries"
    assert remaining == set(listed), "type the response or update the finite migration inventory"


def test_the_document_says_what_the_prose_says_about_the_credential() -> None:
    """Every route is bearer-gated and can answer 401, and the document has to say so.

    Declaring the scheme is all it is: `HTTPBearer(auto_error=False)` refuses nothing, so
    the request path is unchanged and the gate is still `_auth_dependency` — which is what
    `tests/test_api.py`'s auth suite goes on proving, unchanged by this. Without it a
    generated client would send no `Authorization` header and meet a blanket 401 — a
    machine-readable contract that contradicts `docs/api.md` on its first line.
    """
    document = openapi_document()

    assert document["components"]["securitySchemes"] == {
        "HTTPBearer": {"type": "http", "scheme": "bearer"}
    }
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            where = f"{method.upper()} {path}"
            assert operation.get("security") == [{"HTTPBearer": []}], where
            assert "401" in operation["responses"], where


def test_the_committed_document_is_the_shape_townhall_reads() -> None:
    """Read off the file the way townhall does: the paths its client calls by name."""
    document = json.loads(DOCUMENT_FILE.read_text(encoding="utf-8"))

    for path in ("/residents", "/skills", "/routines", "/jobs", "/approvals", "/requests"):
        assert path in document["paths"], f"townhall lists {path}; it must be here"
