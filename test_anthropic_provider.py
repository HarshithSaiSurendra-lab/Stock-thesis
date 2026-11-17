"""Tests for the real Anthropic provider in llm_sentiment.py.

All tests use a fake client — zero API spend, no network, no keys.
The smoke test script (llm_smoke_test.py) is the only place that
touches the live API, and only with the user's key from env/.env.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from llm_sentiment import (
    HAIKU_MODEL_ID,
    SONNET_MODEL_ID,
    AnthropicProvider,
    SpendCap,
    SpendCapExceeded,
    _parse_score_json,
    cost_usd,
)

DOCS = [{"doc": "TEST", "section": "Test excerpt", "date": "2026-11-17"}]
EXCERPT = "TestCo revenue up 8%, margin expanded, guidance raised."


def _fake_client(reply_json='{"score": 0.6, "confidence": 0.8, "rationale": "Growth and margin expansion."}'):
    usage = SimpleNamespace(input_tokens=40000, output_tokens=750)
    block = SimpleNamespace(text=reply_json)
    message = SimpleNamespace(content=[block], usage=usage)
    messages = SimpleNamespace(create=lambda **kw: message)
    return SimpleNamespace(messages=messages)


def _provider(tmp_path, role="screen", run_cap=1.0, monthly_cap=10.0):
    return AnthropicProvider(
        "sk-ant-test-key",
        role=role,
        run_cap_usd=run_cap,
        monthly_cap_usd=monthly_cap,
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "spend.json",
        client=_fake_client(),
    )


def test_cost_math_matches_research_mid_case():
    # 40k in / 750 out, verified Nov 2026 prices.
    assert cost_usd(HAIKU_MODEL_ID, 40000, 750) == pytest.approx(0.004375, abs=1e-6)
    assert cost_usd(SONNET_MODEL_ID, 40000, 750) == pytest.approx(0.0875, abs=1e-6)
    assert cost_usd("claude-fable-5-2", 40000, 750) == pytest.approx(0.35, abs=1e-6)


def test_role_selects_model_per_methodology():
    assert AnthropicProvider.ROLE_MODELS["screen"] == HAIKU_MODEL_ID
    assert AnthropicProvider.ROLE_MODELS["adjudicate"] == SONNET_MODEL_ID
    assert AnthropicProvider.ROLE_MODELS["audit"] == "claude-fable-5-2"


def test_provider_scores_and_logs_cost(tmp_path):
    provider = _provider(tmp_path)
    result = provider.score("TEST", "2026-11-17", DOCS, EXCERPT)
    assert result["score"] == pytest.approx(0.6)
    assert result["confidence"] == pytest.approx(0.8)
    assert result["model_id"] == HAIKU_MODEL_ID
    assert result["tokens_in"] == 40000
    assert result["cost_usd"] == pytest.approx(0.004375, abs=1e-6)
    assert result["cached"] is False


def test_provider_disk_cache_second_call_is_free(tmp_path):
    provider = _provider(tmp_path)
    first = provider.score("TEST", "2026-11-17", DOCS, EXCERPT)
    # A provider with a client that would explode if called proves the
    # second call never hits the network.
    exploding = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(RuntimeError("called API")))
    )
    second_provider = AnthropicProvider(
        "sk-ant-test-key",
        role="screen",
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "spend.json",
        client=exploding,
    )
    second = second_provider.score("TEST", "2026-11-17", DOCS, EXCERPT)
    assert second["cached"] is True
    assert second["score"] == first["score"]
    assert second["cost_usd"] == first["cost_usd"]


def test_spend_cap_blocks_before_call(tmp_path):
    provider = _provider(tmp_path, run_cap=0.0001)  # far below worst-case
    with pytest.raises(SpendCapExceeded, match="run cap"):
        provider.score("TEST", "2026-11-17", DOCS, EXCERPT)


def test_spend_cap_ledger_records_month(tmp_path):
    cap = SpendCap(run_cap_usd=1.0, monthly_cap_usd=10.0, ledger_path=tmp_path / "spend.json", month="2026-11")
    cap.record(0.05)
    assert json.loads((tmp_path / "spend.json").read_text())["2026-11"] == pytest.approx(0.05)
    cap2 = SpendCap(run_cap_usd=1.0, monthly_cap_usd=10.0, ledger_path=tmp_path / "spend.json", month="2026-11")
    assert cap2.spent_month_usd == pytest.approx(0.05)


def test_parse_rejects_bad_json_and_out_of_range():
    with pytest.raises(ValueError):
        _parse_score_json("no json here")
    with pytest.raises(ValueError):
        _parse_score_json('{"score": 2.5, "confidence": 0.8}')
    parsed = _parse_score_json('prefix {"score": -0.4, "confidence": 0.7, "rationale": "Weak."} suffix')
    assert parsed["score"] == pytest.approx(-0.4)


def test_provider_rejects_bad_key():
    with pytest.raises(ValueError, match="sk-ant-"):
        AnthropicProvider("not-a-key", role="screen", client=_fake_client())
