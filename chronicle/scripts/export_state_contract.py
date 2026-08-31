#!/usr/bin/env python3
"""Write the deterministic binding between snapshot shape and schema version."""

import hashlib
import json
import pathlib
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from serve import VillageState  # noqa: E402
from village_state import SCHEMA_VERSION  # noqa: E402


ANNOTATION_KEYS = frozenset({"description", "examples", "title"})
NAMED_SCHEMA_MAP_KEYS = frozenset(
    {
        "$defs",
        "definitions",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "properties",
    }
)


def _wire_shape(value: Any, *, named_schema_map: bool = False) -> Any:
    """Remove documentation annotations and canonicalize a JSON Schema value."""
    if isinstance(value, dict):
        return {
            key: _wire_shape(child, named_schema_map=key in NAMED_SCHEMA_MAP_KEYS)
            for key, child in sorted(value.items())
            if named_schema_map or key not in ANNOTATION_KEYS
        }
    if isinstance(value, list):
        return [_wire_shape(child) for child in value]
    return value


def snapshot_shape_fingerprint() -> str:
    shape = _wire_shape(VillageState.model_json_schema())
    canonical = json.dumps(shape, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def binding_error(
    binding: dict[str, Any],
    *,
    current_version: int = SCHEMA_VERSION,
    current_fingerprint: str | None = None,
) -> str | None:
    fingerprint = current_fingerprint or snapshot_shape_fingerprint()
    if (
        binding.get("schema_version") == current_version
        and binding.get("fingerprint") == fingerprint
    ):
        return None
    return (
        "Snapshot wire shape changed: either bump SCHEMA_VERSION and re-record the binding "
        "for a breaking change, or run `uv run python scripts/export_state_contract.py` "
        "and review the re-recorded fingerprint at the same version for an additive change"
    )


def rendered_binding() -> str:
    binding = {
        "fingerprint": snapshot_shape_fingerprint(),
        "schema_version": SCHEMA_VERSION,
    }
    return json.dumps(binding, indent=2, sort_keys=True) + "\n"


def main():
    destination = ROOT / "docs" / "state-shape.json"
    destination.write_text(rendered_binding(), encoding="utf-8")
    print(destination.relative_to(ROOT))


if __name__ == "__main__":
    main()
