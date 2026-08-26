"""The committed JSON Schema, checked against the models that generate it.

Burrow reads resident manifests for display (burrow #35/#47) and validates them against
``schema/resident-manifest-v0.json`` rather than importing this package — so the artifact,
not this repo's pydantic models, is the contract. An artifact that lags the models is a
contract nobody is holding: a renamed or removed field would reach burrow as a manifest it
silently cannot read, discovered in the village rather than in the pull request.

So the artifact is committed and asserted off the file, the same way the vendored burrow
emitter is (``tests/test_resident_image.py``): regenerate in-process, compare bytes, and
name the regeneration command in the failure. ``make check`` runs pytest, and so does CI,
which is why this guard needs no CI step of its own.

Pydantic's schema emission is a hidden input here. It is locked at 2.13.4 (``uv.lock``;
CI installs ``--locked``), and a lockfile bump that changes what pydantic emits will
surface as a diff in this artifact. That is the feature, not a nuisance: the diff is
exactly what somebody has to read for burrow impact before the bump merges.
"""

import json

import jsonschema
import pytest
from pydantic import ValidationError

from conftest import REPO_ROOT, valid_manifest
from steward.manifest import (
    SCHEMA_ARTIFACT,
    ResidentManifest,
    SkillGrant,
    manifest_json_schema,
    manifest_schema_json,
)

SCHEMA_FILE = REPO_ROOT / SCHEMA_ARTIFACT

#: What answers a failure here. Named in the message, because a drift test whose failure
#: does not say how to fix it just gets the artifact deleted.
REGENERATE = "make schema-write"


def test_the_committed_schema_exists_where_its_own_id_promises() -> None:
    """The schema's `$id` names this path; a promise with no file behind it is a lie."""
    assert SCHEMA_FILE.is_file(), (
        f"{SCHEMA_ARTIFACT} is missing — burrow validates manifests against it. "
        f"Write it with:  {REGENERATE}"
    )
    assert manifest_json_schema()["$id"].endswith(SCHEMA_ARTIFACT)


def test_the_committed_schema_matches_the_models_byte_for_byte() -> None:
    """The artifact is only a contract while it is the same bytes the models generate.

    Byte-for-byte rather than a semantic compare: the committed file is what burrow
    fetches, so the thing worth asserting is the file, not a parsed idea of it.
    """
    generated = manifest_schema_json()
    committed = SCHEMA_FILE.read_text(encoding="utf-8")

    assert committed == generated, (
        f"{SCHEMA_ARTIFACT} no longer matches steward.manifest.manifest_json_schema().\n"
        "Regenerate it and review the diff for burrow impact — a removed or renamed "
        "field breaks burrow's reader (burrow #35/#47), and a pydantic bump can move "
        "the emission on its own:\n"
        f"  {REGENERATE}"
    )


def test_the_committed_schema_is_the_shape_burrow_reads() -> None:
    """Read off the file the way burrow does: the dimensions it renders, and the title."""
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))

    assert schema["title"] == "steward resident manifest v0"
    for dimension in ("skills", "memory", "routes", "app_grants", "soul"):
        assert dimension in schema["properties"], f"burrow renders {dimension}; it must be here"


@pytest.mark.parametrize(
    "grant",
    [
        "a",
        "0",
        "daily-summary",
        "daily-summary-",
        {"id": "a"},
        {"id": "0"},
        {"id": "daily-summary"},
        {"id": "daily-summary-"},
    ],
    ids=[
        "bare-letter-boundary",
        "bare-digit-boundary",
        "bare-slug",
        "bare-trailing-hyphen-boundary",
        "object-letter-boundary",
        "object-digit-boundary",
        "object-slug",
        "object-trailing-hyphen-boundary",
    ],
)
def test_skill_grant_inputs_have_schema_model_parity(grant: object) -> None:
    """Both documented spellings pass the artifact and normalize to SkillGrant."""
    document = valid_manifest()
    document["skills"] = [grant]

    jsonschema.Draft202012Validator(json.loads(SCHEMA_FILE.read_text())).validate(document)
    manifest = ResidentManifest.model_validate(document)
    expected = SkillGrant.model_validate(grant)

    assert manifest.skills == [expected]


@pytest.mark.parametrize(
    "grant",
    [
        42,
        {"source": "library"},
        "",
        "   ",
        "Daily-summary",
        "-daily-summary",
        "daily-summary_",
        {"id": ""},
        {"id": "   "},
        {"id": "Daily-summary"},
        {"id": "-daily-summary"},
        {"id": "daily-summary_"},
        {"id": " daily-summary "},
        {"id": "\tdaily-summary\t"},
        {"id": "\ndaily-summary\n"},
        " daily-summary ",
        "\tdaily-summary\t",
        "\ndaily-summary\n",
        {"id": "ok", "extra": True},
    ],
    ids=[
        "wrong-type",
        "missing-id",
        "bare-empty",
        "bare-spaces",
        "bare-uppercase",
        "bare-invalid-leading-character",
        "bare-invalid-trailing-character",
        "object-empty",
        "object-spaces",
        "object-uppercase",
        "object-invalid-leading-character",
        "object-invalid-trailing-character",
        "object-padded-with-spaces",
        "object-padded-with-tabs",
        "object-padded-with-newlines",
        "bare-padded-with-spaces",
        "bare-padded-with-tabs",
        "bare-padded-with-newlines",
        "extra-field",
    ],
)
def test_skill_grant_rejections_have_schema_model_parity(grant: object) -> None:
    """The artifact and model reject the same representative malformed grants."""
    document = valid_manifest()
    document["skills"] = [grant]
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_FILE.read_text()))

    assert not validator.is_valid(document)
    with pytest.raises(ValidationError):
        ResidentManifest.model_validate(document)
    with pytest.raises(ValidationError):
        SkillGrant.model_validate(grant)
