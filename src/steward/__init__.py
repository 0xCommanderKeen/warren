"""steward — the control plane for the agent fleet that burrow watches.

Souls and resident manifests are versioned here; burrow only ever reads and renders
them. The public surface of this package is the manifest load-and-validate path.
"""

from steward.manifest import (
    Diagnostic,
    ManifestError,
    Resident,
    ResidentManifest,
    Severity,
    ValidationResult,
    load_manifest,
    manifest_json_schema,
    validate_manifest,
    validate_path,
    validate_paths,
    validate_tree,
)

__all__ = [
    "Diagnostic",
    "ManifestError",
    "Resident",
    "ResidentManifest",
    "Severity",
    "ValidationResult",
    "load_manifest",
    "manifest_json_schema",
    "validate_manifest",
    "validate_path",
    "validate_paths",
    "validate_tree",
]
