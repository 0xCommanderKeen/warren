"""Validated resident manifests for Burrow.

The loader is intentionally dependency-free and returns a report instead of
raising: callers can serve every valid resident while reporting exactly why an
invalid file was not granted a home or advertised capabilities.
"""

import json
import os
import pathlib
import re


CHARACTERS = {"Villager", "Villager2", "Villager3", "Villager4", "Villager5",
              "Woman", "Boy", "OldMan", "Princess", "Hunter", "Noble", "Monk"}
TOP_LEVEL = {"manifest_version", "match", "home", "soul", "skills", "memory",
             "routes", "app_grants"}
SOUL_FIELDS = {"name", "char", "accent", "role", "description"}
REFERENCE_FIELDS = {"id", "status_ref"}
MEMORY_FIELDS = {"ref", "status_ref"}
UNKNOWN_PATH_SEGMENT = "<unknown>"
FORBIDDEN_KEY = re.compile(
    r"(^|_)(secret|token|password|credential|api_key|private_key|access_key)(_|$)", re.I)
FORBIDDEN_VALUE = re.compile(
    r"(?:\bbearer\s+|\b(?:secret|token|password|credential|api[_-]?key|private[_-]?key|access[_-]?key)\s*[:=]\s*|"
    r"\b[A-Za-z][A-Za-z0-9_.-]{1,31}\s*=\s*\S+)",
    re.I)
OPAQUE_VALUE = re.compile(r"(?=[A-Za-z0-9_-]{32,})(?=[A-Za-z0-9_-]*[A-Za-z])"
                          r"(?=[A-Za-z0-9_-]*[0-9])"
                          r"[A-Za-z0-9_-]{32,}")
RECOGNIZED_CREDENTIAL = re.compile(
    r"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}\b")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,255}")


def _diagnostic(filename, path, message):
    return {"file": filename, "path": path, "message": message}


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _trusted_child(path, key, fields):
    """Build a path without ever appending attacker-controlled key text."""
    trusted = next((field for field in fields or () if key == field), None)
    return path + "." + (trusted if trusted is not None else UNKNOWN_PATH_SEGMENT), trusted


def _child_fields(field):
    if field == "match":
        return {"agent_id", "project"}
    if field == "soul":
        return SOUL_FIELDS
    if field == "memory":
        return MEMORY_FIELDS
    if field in {"skills", "routes", "app_grants"}:
        return REFERENCE_FIELDS
    return None


def _find_sensitive_key(value, path="$", fields=TOP_LEVEL):
    if isinstance(value, dict):
        for key, child in value.items():
            if FORBIDDEN_KEY.search(str(key)):
                return path + "." + UNKNOWN_PATH_SEGMENT
            child_path, trusted = _trusted_child(path, key, fields)
            found = _find_sensitive_key(child, child_path, _child_fields(trusted))
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_sensitive_key(child, f"{path}[{index}]", fields)
            if found:
                return found
    return None


def _find_sensitive_value(value, path="$", fields=TOP_LEVEL):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path, trusted = _trusted_child(path, key, fields)
            found = _find_sensitive_value(child, child_path, _child_fields(trusted))
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_sensitive_value(child, f"{path}[{index}]", fields)
            if found:
                return found
    elif isinstance(value, str) and (FORBIDDEN_VALUE.search(value) or
                                     OPAQUE_VALUE.search(value) or
                                     RECOGNIZED_CREDENTIAL.search(value)):
        return path
    return None


def _validate_safe_string(value, filename, path, diagnostics, pattern):
    if not _nonempty_string(value):
        diagnostics.append(_diagnostic(filename, path,
                                       "is required and must be a non-empty string"))
    elif not pattern.fullmatch(value):
        diagnostics.append(_diagnostic(
            filename, path,
            "must be a credential-free identifier or status reference using only letters, numbers, . _ : / # -"))


def _validate_reference_list(value, filename, path, diagnostics):
    if not isinstance(value, list):
        diagnostics.append(_diagnostic(filename, path, "must be an array"))
        return
    for index, item in enumerate(value):
        at = f"{path}[{index}]"
        if not isinstance(item, dict):
            diagnostics.append(_diagnostic(filename, at, "must be an object"))
            continue
        extra = set(item) - REFERENCE_FIELDS
        if extra:
            diagnostics.append(_diagnostic(
                filename, at + "." + UNKNOWN_PATH_SEGMENT,
                "is not allowed; declarations contain identifiers and status references only"))
        for field in REFERENCE_FIELDS:
            _validate_safe_string(item.get(field), filename, at + "." + field,
                                  diagnostics, IDENTIFIER if field == "id" else REFERENCE)


def validate_manifest(value, filename="<manifest>"):
    diagnostics = []
    if not isinstance(value, dict):
        return [_diagnostic(filename, "$", "must be a JSON object")]
    sensitive = _find_sensitive_key(value)
    if sensitive:
        return [_diagnostic(filename, sensitive,
                            "credential and secret fields are forbidden; store only an identifier or status reference")]
    sensitive = _find_sensitive_value(value)
    if sensitive:
        return [_diagnostic(filename, sensitive,
                            "credential and secret material is forbidden; store only safe capability metadata")]
    extra = set(value) - TOP_LEVEL
    if extra:
        diagnostics.append(_diagnostic(filename, "$." + UNKNOWN_PATH_SEGMENT, "unknown field"))
    for field in TOP_LEVEL:
        if field not in value:
            diagnostics.append(_diagnostic(filename, "$." + field, "is required"))
    manifest_version = value.get("manifest_version")
    if type(manifest_version) is not int or manifest_version != 1:
        diagnostics.append(_diagnostic(
            filename, "$.manifest_version", "must equal integer 1"))

    match = value.get("match")
    if not isinstance(match, dict):
        diagnostics.append(_diagnostic(filename, "$.match", "must be an object"))
    else:
        if set(match) - {"agent_id", "project"}:
            diagnostics.append(_diagnostic(
                filename, "$.match." + UNKNOWN_PATH_SEGMENT,
                "allows only agent_id or project"))
        supplied = [key for key in ("agent_id", "project") if _nonempty_string(match.get(key))]
        if len(supplied) != 1 or len(match) != 1:
            diagnostics.append(_diagnostic(filename, "$.match",
                                           "must contain exactly one non-empty agent_id or project"))
        elif not IDENTIFIER.fullmatch(match[supplied[0]]):
            diagnostics.append(_diagnostic(filename, "$.match." + supplied[0],
                                           "must be a credential-free identifier"))

    home = value.get("home")
    if not isinstance(home, int) or isinstance(home, bool) or not 0 <= home < 8:
        diagnostics.append(_diagnostic(filename, "$.home", "must be an integer from 0 through 7"))

    soul = value.get("soul")
    if not isinstance(soul, dict):
        diagnostics.append(_diagnostic(filename, "$.soul", "must be an object"))
    else:
        extra_soul = set(soul) - SOUL_FIELDS
        if extra_soul:
            diagnostics.append(_diagnostic(
                filename, "$.soul." + UNKNOWN_PATH_SEGMENT, "unknown field"))
        for field in SOUL_FIELDS:
            if not _nonempty_string(soul.get(field)):
                diagnostics.append(_diagnostic(filename, "$.soul." + field,
                                               "is required and must be a non-empty string"))
        if soul.get("char") not in CHARACTERS:
            diagnostics.append(_diagnostic(filename, "$.soul.char", "must name a checked-in character sprite"))
        if _nonempty_string(soul.get("accent")) and not re.fullmatch(r"#[0-9A-Fa-f]{6}", soul["accent"]):
            diagnostics.append(_diagnostic(filename, "$.soul.accent", "must be a six-digit hex colour"))

    _validate_reference_list(value.get("skills"), filename, "$.skills", diagnostics)
    _validate_reference_list(value.get("routes"), filename, "$.routes", diagnostics)
    _validate_reference_list(value.get("app_grants"), filename, "$.app_grants", diagnostics)
    memory = value.get("memory")
    if not isinstance(memory, dict):
        diagnostics.append(_diagnostic(filename, "$.memory", "must be an object"))
    else:
        extra_memory = set(memory) - MEMORY_FIELDS
        if extra_memory:
            diagnostics.append(_diagnostic(
                filename, "$.memory." + UNKNOWN_PATH_SEGMENT, "unknown field"))
        for field in MEMORY_FIELDS:
            _validate_safe_string(memory.get(field), filename, "$.memory." + field,
                                  diagnostics, REFERENCE)
    return diagnostics


def _resident(filename, manifest):
    soul = manifest["soul"]
    match_field = next(iter(manifest["match"]))
    match = {match_field: manifest["match"][match_field]}
    public_soul = {field: soul[field] for field in
                   ("name", "char", "accent", "role", "description")}
    def public_references(items):
        return [{"id": item["id"], "status_ref": item["status_ref"]}
                for item in items]
    meta = dict(match)
    meta.update({key: soul[key] for key in ("name", "char", "accent", "role")})
    return {
        "file": filename,
        "valid": True,
        "manifest_version": manifest["manifest_version"],
        "match": match,
        "home": manifest["home"],
        "meta": meta,
        "body": soul["description"],
        "capabilities": {
            "soul": public_soul,
            "skills": public_references(manifest["skills"]),
            "memory": {field: manifest["memory"][field]
                       for field in ("ref", "status_ref")},
            "routes": public_references(manifest["routes"]),
            "app_grants": public_references(manifest["app_grants"]),
        },
    }


def _safe_public_string(value, pattern=None):
    """Return one independently safe public string, never a rejected value."""
    if not _nonempty_string(value):
        return None
    if pattern is not None and not pattern.fullmatch(value):
        return None
    if _find_sensitive_value(value) or _find_sensitive_key(value):
        return None
    return value


def _diagnostic_resident(filename, manifest, problems):
    """Project safe fragments of a malformed manifest for capability diagnosis.

    This record is deliberately separate from residents: it can explain a bad
    declaration in the UI, but can never reserve a home or become a soul.
    """
    if not isinstance(manifest, dict):
        return None
    match = manifest.get("match")
    public_match = {}
    if isinstance(match, dict) and len(match) == 1:
        field = next(iter(match))
        if field in {"agent_id", "project"}:
            safe = _safe_public_string(match[field], IDENTIFIER)
            if safe:
                public_match[field] = safe
    soul = manifest.get("soul")
    public_meta = {}
    body = None
    if isinstance(soul, dict):
        patterns = {"char": re.compile("|".join(sorted(CHARACTERS))),
                    "accent": re.compile(r"#[0-9A-Fa-f]{6}")}
        for field in ("name", "char", "accent", "role"):
            safe = _safe_public_string(soul.get(field), patterns.get(field))
            if safe:
                public_meta[field] = safe
        body = _safe_public_string(soul.get("description"))
    public_meta.update(public_match)

    problem_paths = [problem.get("path") for problem in problems]
    capabilities = {}

    def reference_items(kind, value, id_field="id"):
        items = []
        if isinstance(value, list):
            source = value
        elif kind == "memory" and isinstance(value, dict):
            source = [value]
        else:
            source = []
        for index, item in enumerate(source):
            if not isinstance(item, dict):
                continue
            public = {}
            identifier = _safe_public_string(item.get(id_field),
                                             REFERENCE if id_field == "ref" else IDENTIFIER)
            status_ref = _safe_public_string(item.get("status_ref"), REFERENCE)
            if identifier:
                public[id_field] = identifier
            if status_ref:
                public["status_ref"] = status_ref
            prefix = f"$.{kind}" if kind == "memory" else f"$.{kind}[{index}]"
            affected = next((path for path in problem_paths if path == f"$.{kind}" or
                             path.startswith(prefix)), None)
            if affected:
                public["invalid"] = True
                public["diagnostic_path"] = affected
            if public:
                items.append(public)
        kind_problem = next((path for path in problem_paths if path == f"$.{kind}" or
                             path.startswith(f"$.{kind}[")), None)
        if kind_problem and not any(item.get("invalid") for item in items):
            items.append({"invalid": True, "diagnostic_path": kind_problem})
        return items

    capabilities["skills"] = reference_items("skills", manifest.get("skills"))
    capabilities["memory"] = reference_items("memory", manifest.get("memory"), "ref")
    capabilities["routes"] = reference_items("routes", manifest.get("routes"))
    capabilities["app_grants"] = reference_items("app_grants", manifest.get("app_grants"))
    soul_problem = next((path for path in problem_paths if path == "$.soul" or
                         path.startswith("$.soul.")), None)
    public_soul = dict(public_meta)
    public_soul.pop("agent_id", None)
    public_soul.pop("project", None)
    if body:
        public_soul["description"] = body
    if soul_problem:
        public_soul.update({"invalid": True, "diagnostic_path": soul_problem})
    capabilities["soul"] = public_soul

    home = manifest.get("home")
    return {
        "file": filename, "valid": False, "diagnostic": True,
        "manifest_version": manifest.get("manifest_version")
        if type(manifest.get("manifest_version")) is int else None,
        "match": public_match, "declared_home": home
        if type(home) is int and 0 <= home < 8 else None,
        "meta": public_meta, "body": body, "capabilities": capabilities,
    }


def load_resident_manifests(directory):
    directory = pathlib.Path(directory)
    residents, diagnostic_residents, diagnostics = [], [], []
    if not directory.is_dir():
        return {"residents": residents, "diagnostic_residents": diagnostic_residents,
                "diagnostics": diagnostics}
    claimed_homes = {}
    claimed_matches = {}
    for path in sorted(directory.glob("*.resident.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            diagnostics.append(_diagnostic(path.name, "$", "invalid JSON: " + str(error).splitlines()[0]))
            continue
        problems = validate_manifest(manifest, path.name)
        if problems:
            diagnostics.extend(problems)
            partial = _diagnostic_resident(path.name, manifest, problems)
            if partial:
                diagnostic_residents.append(partial)
            continue
        home = manifest["home"]
        match_key = next(iter(manifest["match"].items()))
        if home in claimed_homes:
            diagnostics.append(_diagnostic(
                path.name, "$.home", f"home {home} is already reserved by {claimed_homes[home]}"))
            continue
        if match_key in claimed_matches:
            diagnostics.append(_diagnostic(
                path.name, "$.match", f"match is already declared by {claimed_matches[match_key]}"))
            continue
        claimed_homes[home] = path.name
        claimed_matches[match_key] = path.name
        residents.append(_resident(path.name, manifest))
    return {"residents": residents, "diagnostic_residents": diagnostic_residents,
            "diagnostics": diagnostics}
