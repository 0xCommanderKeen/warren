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


def _find_sensitive_key(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + "." + str(key)
            if FORBIDDEN_KEY.search(str(key)):
                return child_path
            found = _find_sensitive_key(child, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_sensitive_key(child, f"{path}[{index}]")
            if found:
                return found
    return None


def _find_sensitive_value(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            found = _find_sensitive_value(child, path + "." + str(key))
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_sensitive_value(child, f"{path}[{index}]")
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
                filename, at + "." + sorted(extra)[0],
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
        diagnostics.append(_diagnostic(filename, "$." + sorted(extra)[0], "unknown field"))
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
            diagnostics.append(_diagnostic(filename, "$.match", "allows only agent_id or project"))
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
            diagnostics.append(_diagnostic(filename, "$.soul." + sorted(extra_soul)[0], "unknown field"))
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
            diagnostics.append(_diagnostic(filename, "$.memory." + sorted(extra_memory)[0], "unknown field"))
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


def load_resident_manifests(directory):
    directory = pathlib.Path(directory)
    residents, diagnostics = [], []
    if not directory.is_dir():
        return {"residents": residents, "diagnostics": diagnostics}
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
    return {"residents": residents, "diagnostics": diagnostics}
