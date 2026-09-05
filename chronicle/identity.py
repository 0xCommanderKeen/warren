"""Fleet resident/home allocation and stable fallback identities."""

import hashlib
from typing import NamedTuple


NAMES = (
    "Bramble",
    "Poppy",
    "Wren",
    "Sorrel",
    "Fern",
    "Alder",
    "Maple",
    "Rowan",
    "Thistle",
    "Clover",
    "Hazel",
    "Juniper",
    "Moss",
    "Reed",
    "Tansy",
    "Willow",
)
CHARS = (
    "Villager",
    "Villager2",
    "Villager3",
    "Villager4",
    "Villager5",
    "Woman",
    "Boy",
    "OldMan",
    "Princess",
    "Hunter",
    "Noble",
    "Monk",
)
ACCENTS = ("#7d5ba6", "#4f7d5b", "#a65b5b", "#5b7da6", "#a68a4f", "#5ba69b")


class FallbackIdentity(NamedTuple):
    name: str
    char: str
    accent: str


def fallback_identity(agent_id):
    """Return the deterministic name, character and accent for an unknown agent."""
    number = int.from_bytes(
        hashlib.sha256(agent_id.encode("utf-8")).digest()[:8], "big"
    )
    return FallbackIdentity(
        NAMES[number % len(NAMES)],
        CHARS[number % len(CHARS)],
        ACCENTS[number % len(ACCENTS)],
    )


def allocate_identities(candidates, manifests, declarations=None):
    """Allocate each identity/home once among eligible villagers.

    Declarations win, then exact identities, then unambiguous projects in agent-id
    order. Retained child lineage forbids project inheritance.
    Candidates map agent IDs to project and has_parent_lineage.
    """
    exact, projects = {}, {}
    for manifest in manifests or ():
        match = manifest.get("match") or manifest.get("meta") or {}
        for index, key in ((exact, match.get("agent_id")), (projects, match.get("project"))):
            if not key:
                continue
            options = index.setdefault(key, [])
            options.append(manifest)

    def select(options):
        return options[0] if len(options) == 1 else None

    assigned, used, homes = {}, set(), set()

    def reserve(agent_id, manifest):
        if manifest is None:
            return
        key = manifest.get("file") or id(manifest)
        home = manifest.get("home")
        if key in used or (home is not None and home in homes):
            return
        assigned[agent_id] = manifest
        used.add(key)
        if home is not None:
            homes.add(home)

    for agent_id in sorted(candidates):
        reserve(agent_id, (declarations or {}).get(agent_id))
    for agent_id in sorted(candidates):
        if agent_id not in assigned:
            reserve(agent_id, next(iter(exact.get(agent_id, [])), None))
    for agent_id, candidate in sorted(candidates.items()):
        if agent_id not in assigned and not candidate["has_parent_lineage"]:
            reserve(agent_id, select(projects.get(candidate["project"], [])))
    return assigned
