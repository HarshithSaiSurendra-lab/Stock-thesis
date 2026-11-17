"""Tests for the P0 groundwork: experiment registry + holdout freeze
(experiments.py) and the offline LLM sentiment stub (llm_sentiment.py).

These tests use only the mock provider — zero API spend, no network,
no broker, no keys. They do not touch the order path.
"""
from __future__ import annotations

import pytest

import experiments
import llm_sentiment
from llm_sentiment import MockProvider, combine_with_technical, llm_score


DOCS = [{"doc": "10-K", "section": "Risk Factors", "date": "2026-02-01"}]


def _experiment(experiment_id="exp-technical-only-v1"):
    return {
        "experiment_id": experiment_id,
        "strategy": "technical-only composite (trade_signal.py)",
        "parameters": {"min_decision_score": 10.25, "trail_percent": 8.0},
        "cost_model": {"base": "1x", "stress": "2x"},
        "horizon": "daily, 1-bar lag",
        "benchmark": "SPY buy-and-hold and equal-weight",
        "holdout_rule": "final 63 trading days of acquired dataset",
    }


def test_register_and_list_experiment(tmp_path):
    record = experiments.register_experiment(_experiment(), directory=tmp_path)
    assert record["status"] == "registered"
    assert record["registered_at"]
    listed = experiments.list_experiments(directory=tmp_path)
    assert [e["experiment_id"] for e in listed] == ["exp-technical-only-v1"]


def test_register_rejects_duplicate_id(tmp_path):
    experiments.register_experiment(_experiment(), directory=tmp_path)
    with pytest.raises(ValueError, match="already registered"):
        experiments.register_experiment(_experiment(), directory=tmp_path)


def test_register_rejects_missing_fields(tmp_path):
    bad = _experiment()
    del bad["benchmark"]
    with pytest.raises(ValueError, match="missing required fields"):
        experiments.register_experiment(bad, directory=tmp_path)


def test_freeze_holdout_twice_identical_is_noop(tmp_path):
    first = experiments.freeze_holdout(directory=tmp_path)
    assert first["status"] == "frozen"
    assert first["definition"].startswith("final 63 trading days")
    second = experiments.freeze_holdout(directory=tmp_path)
    assert second["definition"] == first["definition"]
    assert experiments.load_holdout(directory=tmp_path)["status"] == "frozen"


def test_freeze_holdout_refuses_different_second_freeze(tmp_path):
    experiments.freeze_holdout(directory=tmp_path)
    changed = dict(experiments.DEFAULT_HOLDOUT)
    changed["definition"] = "a different holdout, tuned after looking"
    with pytest.raises(ValueError, match="already frozen"):
        experiments.freeze_holdout(changed, directory=tmp_path)


def test_llm_mock_agreeing_scores_combine():
    llm_sentiment.clear_cache()
    result = llm_score(
        "AAPL",
        "2026-02-02",
        DOCS,
        provider=MockProvider(fable_score=0.8, chatgpt_score=0.9),
    )
    assert result["combined_score"] == pytest.approx(0.85)
    assert result["agreement"] == pytest.approx(0.95)
    assert result["cost_usd"] == 0.0
    assert result["model_ids"]["fable"] == "claude-fable-5-2"
    combined = combine_with_technical(10.25, result)
    assert combined["applied"] is True
    assert combined["llm_contribution"] == pytest.approx(0.85)
    assert combined["adjusted_score"] == pytest.approx(11.10)


def test_llm_sign_disagreement_neutralises():
    llm_sentiment.clear_cache()
    result = llm_score(
        "AAPL",
        "2026-02-02",
        DOCS,
        provider=MockProvider(fable_score=0.8, chatgpt_score=-0.6),
    )
    assert result["disagree_in_sign"] is True
    assert result["combined_score"] == 0.0
    combined = combine_with_technical(10.25, result)
    assert combined["applied"] is False
    assert combined["adjusted_score"] == 10.25


def test_llm_low_confidence_or_agreement_not_applied():
    llm_sentiment.clear_cache()
    low_conf = llm_score(
        "AAPL",
        "2026-02-02",
        DOCS,
        provider=MockProvider(
            fable_score=0.8, fable_confidence=0.4, chatgpt_score=0.8
        ),
    )
    assert combine_with_technical(10.25, low_conf)["applied"] is False

    llm_sentiment.clear_cache()
    low_agree = llm_score(
        "MSFT",
        "2026-02-02",
        DOCS,
        provider=MockProvider(fable_score=1.0, chatgpt_score=0.0),
    )
    # agreement 0.5 < 0.7, so not applied even though signs do not differ
    assert low_agree["agreement"] == pytest.approx(0.5)
    assert combine_with_technical(10.25, low_agree)["applied"] is False


def test_llm_contribution_capped_at_one():
    llm_sentiment.clear_cache()
    result = llm_score(
        "NVDA",
        "2026-02-02",
        DOCS,
        provider=MockProvider(fable_score=1.0, chatgpt_score=1.0),
    )
    combined = combine_with_technical(10.25, result)
    assert combined["llm_contribution"] == pytest.approx(1.0)
    assert combined["adjusted_score"] == pytest.approx(11.25)


def test_llm_cache_second_call_is_cached():
    llm_sentiment.clear_cache()
    first = llm_score("AAPL", "2026-02-02", DOCS, provider=MockProvider(0.5, 0.8, 0.5, 0.8))
    second = llm_score("AAPL", "2026-02-02", DOCS, provider=MockProvider(0.5, 0.8, 0.5, 0.8))
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["combined_score"] == first["combined_score"]
