"""Soul document parsing and the shared Markdown frontmatter boundary."""

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from steward.credential_policy import scan_text_for_secrets
from steward.diagnostics import Diagnostic
from steward.manifest_models import VOICE_HEADING, VOICE_MAX_CHARS, SoulDocument

_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<frontmatter>.*?)\r?\n---\r?\n?(?P<body>.*)\Z", re.DOTALL
)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split a markdown document into its raw ``---`` frontmatter block and its body.

    One splitter for every document in this repo that carries frontmatter — souls here,
    skills in :mod:`steward.skills` — so "what counts as frontmatter" has one answer.
    A document without a block returns ``(None, text)``.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None, text
    return match.group("frontmatter"), match.group("body")


def extract_voice(body: str) -> str | None:
    """Return the text of the ``## Voice`` section, if the soul has one.

    The one definition of what a voice is: the validator caps it here, and
    :mod:`steward.prompt` injects exactly what this returns.
    """
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == VOICE_HEADING.lower():
            collected: list[str] = []
            for following in lines[index + 1 :]:
                if following.startswith("## "):
                    break
                collected.append(following)
            return "\n".join(collected).strip()
    return None


def parse_soul(text: str, source: Path) -> tuple[SoulDocument, list[Diagnostic]]:
    """Parse a soul document, returning it alongside any diagnostics."""
    raw_frontmatter, raw_body = split_frontmatter(text)
    if raw_frontmatter is None:
        return SoulDocument(path=source, body=text), [
            Diagnostic(
                file=source,
                field_path="frontmatter",
                problem="soul file has no --- frontmatter block",
                example="---\nname: Hob\nchar: Monk\naccent: '#a68a4f'\nrole: life bot\n---",
            )
        ]

    diagnostics: list[Diagnostic] = []
    try:
        loaded = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        return SoulDocument(path=source, body=raw_body), [
            Diagnostic(
                file=source,
                field_path="frontmatter",
                problem=f"frontmatter is not valid YAML: {exc}",
                example="name: Hob",
            )
        ]

    if not isinstance(loaded, Mapping):
        return SoulDocument(path=source, body=raw_body), [
            Diagnostic(
                file=source,
                field_path="frontmatter",
                problem="frontmatter must be a mapping of keys to values",
                example="name: Hob",
            )
        ]

    body = raw_body
    voice = extract_voice(body)
    if voice is not None and len(voice) > VOICE_MAX_CHARS:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path=VOICE_HEADING,
                problem=(
                    f"voice section is {len(voice)} characters; the cap is {VOICE_MAX_CHARS} "
                    f"so it stays cheap to inject into every session"
                ),
                example=f"a voice section of at most {VOICE_MAX_CHARS} characters",
            )
        )
    diagnostics.extend(scan_text_for_secrets(text, source, "body"))
    return SoulDocument(path=source, frontmatter=dict(loaded), body=body, voice=voice), diagnostics
