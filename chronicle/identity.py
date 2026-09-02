"""Stable fallback identity for agents without a resident declaration or manifest."""

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
