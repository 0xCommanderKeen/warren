"""Bounds shared by durable delegated-task replies and their prompt rendering."""

from steward.manifest import redact_secrets

ANSWER_MESSAGE_MAX_CHARS = 4_000
ANSWER_BATCH_MAX_CHARS = 12_000


def bounded_message(message: str) -> str:
    """Return the redacted final message steward is willing to retain and deliver."""
    text = redact_secrets(message).strip()
    if len(text) > ANSWER_MESSAGE_MAX_CHARS:
        return text[: ANSWER_MESSAGE_MAX_CHARS - 1].rstrip() + "…"
    return text


def render_answer(*, title: str, receiver: str, status: str, message: str) -> str:
    """Render one bounded terminal letter for its sender."""
    return f"{title} — {receiver} — {status}\n{message or '(no final message)'}"
