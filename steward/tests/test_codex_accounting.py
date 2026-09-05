"""Codex token receipts must become reproducible estimates and real budget pauses."""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import ResidentWriter, valid_manifest
from steward.budgets import BudgetGuard
from steward.codex_usage import read_usage
from steward.manifest import ResidentManifest, Runner, ToolGrant, validate_manifest
from steward.manifest_models import CodexPricing
from steward.runners import CodexRunner, Outcome, RunRequest, RunResult, _ProcessRunner
from steward.store import Store

RATES = {
    "model": "test-model",
    "input_usd_per_million": 2.0,
    "cached_input_usd_per_million": 0.5,
    "output_usd_per_million": 10.0,
}
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def stream(inputs: object = 1000, cached: object = 800, outputs: object = 100) -> str:
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "aggregated_output": "x" * 21000},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Skill saved."}},
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": inputs,
                "cached_input_tokens": cached,
                "output_tokens": outputs,
            },
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


def spec() -> Runner:
    return Runner(
        kind="codex", model="test-model", codex_pricing=CodexPricing.model_validate(RATES)
    )


def manifest() -> ResidentManifest:
    data = valid_manifest()
    data["runner"] = spec().model_dump(mode="json")
    data["tools"] = "unrestricted"
    data["budgets"] = {"daily_cost_usd": 0.003}
    return ResidentManifest.model_validate(data)


def test_receipt_prices_uncached_cached_and_output_once_and_keeps_reply():
    runner = CodexRunner(spec())
    result = runner.parse(RunResult(outcome=Outcome.OK), stream())
    assert result.output == "Skill saved."
    assert result.input_tokens == 1000
    assert result.output_tokens == 100
    assert result.cost_usd == pytest.approx(0.0018)
    assert result.cost_estimate == {
        "basis": "api_equivalent_estimate",
        "pricing": RATES,
        "input_tokens": 1000,
        "cached_input_tokens": 800,
        "output_tokens": 100,
    }


def test_zero_usage_is_known_and_multiple_turns_are_summed():
    zero = read_usage(stream(0, 0, 0), spec().codex_pricing)
    assert zero.cost_usd == 0
    both = read_usage(stream() + "\n" + stream(), spec().codex_pricing)
    assert both.input_tokens == 2000
    assert both.cost_usd == pytest.approx(0.0036)


@pytest.mark.parametrize(
    ("inputs", "cached", "outputs"),
    [
        (True, 0, 1),
        (-1, 0, 1),
        (1.5, 0, 1),
        (1, 2, 1),
        (1, None, 1),
        (1, 0, float("nan")),
        (10**20, 0, 1),
    ],
)
def test_invalid_counts_discredit_whole_receipt(inputs, cached, outputs):
    result = read_usage(stream(inputs, cached, outputs), spec().codex_pricing)
    assert result.output == "Skill saved."
    assert result.cost_usd is result.input_tokens is result.output_tokens is None


@pytest.mark.parametrize(
    "tail",
    [
        "not json",
        '{"type":"turn.started"}',
        '{"type":"turn.failed"}',
        '{"type":"error","message":"secret child error"}',
        '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1}}',
    ],
)
def test_incomplete_or_corrupt_stream_does_not_claim_partial_spend(tail):
    result = read_usage(stream() + "\n" + tail, spec().codex_pricing)
    assert result.cost_usd is None


def test_failed_turn_is_failed_even_if_cli_exits_zero():
    result = CodexRunner(spec()).parse(RunResult(outcome=Outcome.OK), '{"type":"turn.failed"}')
    assert result.outcome is Outcome.FAILED
    assert result.cost_usd is None


def test_no_rate_card_reports_tokens_without_inventing_dollars():
    result = read_usage(stream(), None)
    assert result.input_tokens == 1000
    assert result.cost_usd is result.cost_estimate is None


def test_unpriced_model_override_is_refused_before_launch(tmp_path: Path):
    result = CodexRunner(spec()).run(
        RunRequest(
            prompt="hello",
            workdir=tmp_path,
            model="other",
            timeout_s=10,
            tools=ToolGrant("unrestricted"),
        )
    )
    assert result.outcome is Outcome.FAILED
    assert result.error == "Codex model override has no matching rates"


@pytest.mark.parametrize(
    "change",
    [
        {"input_usd_per_million": -1},
        {"output_usd_per_million": float("inf")},
        {"cached_input_usd_per_million": float("nan")},
    ],
)
def test_invalid_prices_are_rejected(change):
    with pytest.raises(ValidationError):
        CodexPricing.model_validate(RATES | change)


def test_prices_require_an_explicit_matching_codex_model():
    for kind, model in [("claude", "test-model"), ("codex", None), ("codex", "other")]:
        with pytest.raises(ValidationError, match="matching explicit model"):
            Runner.model_validate({"kind": kind, "model": model, "codex_pricing": RATES})


def test_manifest_accepts_priced_codex_cap_and_refuses_missing_rates(
    write_resident: ResidentWriter,
):
    data = manifest().model_dump(mode="json", exclude_none=True)
    path = write_resident(data)
    assert validate_manifest(path).ok
    data["runner"].pop("codex_pricing")
    path = write_resident(data)
    assert not validate_manifest(path).ok


def test_estimates_persist_and_crossing_cap_pauses_after_restart(tmp_path: Path):
    db = tmp_path / "state.db"
    m = manifest()
    result = CodexRunner(spec()).parse(RunResult(outcome=Outcome.OK), stream())
    with Store(db) as store:
        guard = BudgetGuard(store)
        assert guard.allow(m, NOW) is None
        first = guard.record(m, result=result, now=NOW)
        assert first.cost_estimate == result.cost_estimate
        assert guard.allow(m, NOW) is None
    with Store(db) as store:
        guard = BudgetGuard(store)
        assert store.ledger(m.id)[0].cost_estimate == result.cost_estimate
        guard.record(m, result=result, now=NOW)
        status = guard.status(m, NOW)
        assert status.spend.cost_usd == pytest.approx(0.0036)
        assert status.spend.to_dict()["estimated_cost_runs"] == 2
        assert status.paused
        assert guard.allow(m, NOW) is not None


@pytest.mark.parametrize("outcome", [Outcome.OK, Outcome.FAILED, Outcome.TIMEOUT])
def test_missing_usage_pauses_a_capped_codex_resident(outcome):
    with Store(":memory:") as store:
        guard = BudgetGuard(store)
        m = manifest()
        guard.record(m, result=RunResult(outcome=outcome), now=NOW)
        status = guard.status(m, NOW)
        assert status.spend.unreported == 1
        assert status.pause is not None
        assert status.pause.budget == "usage_accounting"
        assert "unknown" in status.pause.reason
        assert guard.allow(m, NOW) is not None


def test_uncapped_codex_is_not_paused_for_missing_usage():
    m = manifest().model_copy(
        update={"budgets": manifest().budgets.model_copy(update={"daily_cost_usd": None})}
    )
    with Store(":memory:") as store:
        guard = BudgetGuard(store)
        guard.record(m, result=RunResult(outcome=Outcome.FAILED), now=NOW)
        assert guard.allow(m, NOW) is None


def test_json_receipt_survives_real_process_and_output_truncation(tmp_path: Path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\nprint({stream()!r})\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    result = CodexRunner(spec()).run(
        RunRequest(
            prompt="account", workdir=tmp_path, timeout_s=10, tools=ToolGrant("unrestricted")
        )
    )
    assert result.output == "Skill saved."
    assert result.cost_usd == pytest.approx(0.0018)


def test_timeout_discards_protocol_and_does_not_price_truncated_receipt(
    tmp_path: Path, monkeypatch
):
    raw = stream().replace("x" * 21000, "tool output")
    monkeypatch.setattr(
        _ProcessRunner, "run", lambda *_: RunResult(outcome=Outcome.TIMEOUT, output=raw)
    )
    result = CodexRunner(spec()).run(
        RunRequest(
            prompt="account", workdir=tmp_path, timeout_s=10, tools=ToolGrant("unrestricted")
        )
    )
    assert result.output == "Skill saved."
    assert result.cost_usd is result.input_tokens is None
    assert result.outcome is Outcome.TIMEOUT


def test_pre_run_guard_recovers_an_unknown_usage_pause_and_allows_explicit_resume():
    with Store(":memory:") as store:
        m = manifest()
        store.record_run(
            resident=m.id,
            agent_id=m.chronicle_agent_id,
            kind="routine",
            run_id="lost",
            trigger="schedule",
            usage_known=False,
            now=NOW.isoformat(),
        )
        guard = BudgetGuard(store)
        assert guard.allow(m, NOW) is not None
        pause = store.budget_pause(m.id)
        assert pause is not None
        assert pause.budget == "usage_accounting"
        guard.resume(m.id)
        assert guard.allow(m, NOW) is None


def test_unpriced_tokens_still_report_unknown_cost():
    with Store(":memory:") as store:
        m = manifest().model_copy(
            update={
                "runner": Runner(kind="codex"),
                "budgets": manifest().budgets.model_copy(update={"daily_cost_usd": None}),
            }
        )
        result = CodexRunner(m.runner).parse(RunResult(outcome=Outcome.OK), stream())
        guard = BudgetGuard(store)
        guard.record(m, result=result, now=NOW)
        status = guard.status(m, NOW)
        assert status.spend.tokens == 1100
        assert status.spend.unreported == 1


def test_origin_rollup_preserves_estimated_cost_label():
    with Store(":memory:") as store:
        guard = BudgetGuard(store)
        result = CodexRunner(spec()).parse(RunResult(outcome=Outcome.OK), stream())
        guard.record(manifest(), result=result, origin="human:miha", now=NOW)
        origin = store.spend_by_origin()[0]
        assert origin.to_dict()["estimated_cost_runs"] == 1
        assert origin.cost_usd == pytest.approx(0.0018)
