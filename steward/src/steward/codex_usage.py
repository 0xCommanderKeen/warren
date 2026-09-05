"""Codex's JSONL receipt: reported tokens and an explicitly priced cost estimate.

The CLI reports no dollar charge. Rates belong to the declaration, and travel with the
ledger entry so an estimate is reproducible after a model or price change.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from steward.manifest_models import CodexPricing
from steward.runners import _as_float, _as_int


@dataclass(frozen=True)
class CodexUsage:
    """A reply and its complete receipt; missing accounting stays absent."""

    output: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cost_estimate: dict[str, Any] | None = None
    failed: bool = False


def _counts(event: dict[str, Any]) -> tuple[int, int, int] | None:
    """Require all three counts, including zero cached tokens, and a possible cache hit."""
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    values = tuple(
        _as_int(usage.get(key)) for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    )
    inputs, cached, outputs = values
    if inputs is None or cached is None or outputs is None or cached > inputs:
        return None
    return inputs, cached, outputs


def read_usage(stdout: str, pricing: CodexPricing | None) -> CodexUsage:  # noqa: C901, PLR0912
    """Sum completed turns only when the entire stream has a complete terminal receipt."""
    output = ""
    inputs = cached = outputs = completed = 0
    pending = invalid = failed = False
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            invalid = True
            continue
        if not isinstance(event, dict):
            invalid = True
            continue
        kind = event.get("type")
        if kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    output = text
        elif kind == "turn.started":
            invalid |= pending
            pending = True
        elif kind == "turn.completed":
            counts = _counts(event)
            if not pending or counts is None:
                invalid = True
            else:
                inputs += counts[0]
                cached += counts[1]
                outputs += counts[2]
                completed += 1
            pending = False
        elif kind in {"turn.failed", "error"}:
            failed = invalid = True
    empty = CodexUsage(output=output, failed=failed)
    if invalid or pending or not completed:
        return empty
    if any(_as_int(value) is None for value in (inputs, cached, outputs)):
        return empty
    if pricing is None:
        return CodexUsage(output=output, input_tokens=inputs, output_tokens=outputs)
    rates = pricing.model_dump(mode="json")
    cost = (
        (inputs - cached) * Decimal(str(pricing.input_usd_per_million))
        + cached * Decimal(str(pricing.cached_input_usd_per_million))
        + outputs * Decimal(str(pricing.output_usd_per_million))
    ) / Decimal(1_000_000)
    believed_cost = _as_float(float(cost))
    if believed_cost is None:
        return empty
    return CodexUsage(
        output=output,
        input_tokens=inputs,
        output_tokens=outputs,
        cost_usd=believed_cost,
        cost_estimate={
            "basis": "api_equivalent_estimate",
            "pricing": rates,
            "input_tokens": inputs,
            "cached_input_tokens": cached,
            "output_tokens": outputs,
        },
    )
