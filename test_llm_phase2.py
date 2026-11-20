"""Tests for the amended Phase 2 pipeline (exp-technical-plus-llm-v1).

Offline: synthetic panels/filings only. No API calls, no disk cache.
"""

import numpy as np
import pandas as pd
import pytest

from llm_sentiment import combine_two_scores
from run_llm_backtest import (
    build_adjusted_panels,
    current_filing_series,
    in_scope_filings,
    relevance_counts,
    score_pass,
    select_sonnet_subset,
)


# ---------------------------------------------------------------------------
# combine_two_scores (amended rule: any two model scores)
# ---------------------------------------------------------------------------
def test_combine_two_scores_agree_and_confident_applies():
    out = combine_two_scores(4.0, 0.6, 0.9, 0.4, 0.8)
    assert out["applied"] is True
    assert out["llm_contribution"] == pytest.approx(0.5)
    assert out["adjusted_score"] == pytest.approx(4.5)


def test_combine_two_scores_capped_at_plus_minus_one():
    out = combine_two_scores(4.0, 1.0, 0.9, 1.0, 0.9)
    assert out["applied"] is True
    assert out["llm_contribution"] == 1.0
    out = combine_two_scores(4.0, -1.0, 0.9, -1.0, 0.9)
    assert out["llm_contribution"] == -1.0


def test_combine_two_scores_sign_disagreement_neutralises():
    out = combine_two_scores(4.0, 0.5, 0.9, -0.5, 0.9)
    assert out["applied"] is False
    assert out["llm_contribution"] == 0.0
    assert out["adjusted_score"] == 4.0


def test_combine_two_scores_low_agreement_neutralises():
    # |0.8 - (-0.1)| = 0.9 -> agreement 0.55 < 0.7 (same sign, far apart).
    out = combine_two_scores(4.0, 0.8, 0.9, -0.1, 0.9)
    assert out["applied"] is False
    assert out["llm_contribution"] == 0.0


def test_combine_two_scores_low_confidence_neutralises():
    out = combine_two_scores(4.0, 0.6, 0.9, 0.4, 0.5)
    assert out["applied"] is False
    assert out["llm_contribution"] == 0.0


# ---------------------------------------------------------------------------
# current_filing_series
# ---------------------------------------------------------------------------
def _filings():
    return pd.DataFrame([
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-01-02"), "form": "10-K",
         "accession": "A1", "excerpt_text": "x", "excerpt_chars": 1},
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-02-01"), "form": "10-Q",
         "accession": "A2", "excerpt_text": "x", "excerpt_chars": 1},
        # Holdout-window filing: must never be the current filing.
        {"ticker": "AAA", "filing_date": pd.Timestamp("2026-09-01"), "form": "10-Q",
         "accession": "A3", "excerpt_text": "x", "excerpt_chars": 1},
        {"ticker": "BBB", "filing_date": pd.Timestamp("2024-01-05"), "form": "10-K",
         "accession": "B1", "excerpt_text": "x", "excerpt_chars": 1},
    ])


def test_current_filing_series_effective_from_filing_date_and_carries_forward():
    days = pd.DatetimeIndex([
        "2024-01-01", "2024-01-02", "2024-01-15", "2024-02-01",
        "2024-03-01", "2026-09-01", "2026-10-01",
    ])
    current = current_filing_series(_filings(), days)
    assert pd.isna(current.at[pd.Timestamp("2024-01-01"), "AAA"])
    assert current.at[pd.Timestamp("2024-01-02"), "AAA"] == "A1"
    assert current.at[pd.Timestamp("2024-01-15"), "AAA"] == "A1"
    assert current.at[pd.Timestamp("2024-02-01"), "AAA"] == "A2"
    assert current.at[pd.Timestamp("2024-03-01"), "AAA"] == "A2"
    # Holdout days carry forward the latest pre-holdout filing (A2, not A3).
    assert current.at[pd.Timestamp("2026-09-01"), "AAA"] == "A2"
    assert current.at[pd.Timestamp("2026-10-01"), "AAA"] == "A2"
    assert current.at[pd.Timestamp("2024-01-15"), "BBB"] == "B1"


# ---------------------------------------------------------------------------
# build_adjusted_panels
# ---------------------------------------------------------------------------
def test_build_adjusted_panels_llm_never_creates_a_trade_alone():
    days = pd.date_range("2024-01-02", periods=3, freq="D")
    panel = pd.DataFrame({"AAA": [0.0, 3.0, np.nan]}, index=days)
    contribution = pd.DataFrame({"AAA": [0.5, -0.3, 0.9]}, index=days)
    adjusted = build_adjusted_panels({"v1": panel}, contribution)
    adj = adjusted["v1_llm"]["AAA"]
    assert adj.iloc[0] == 0.0  # inactive stays 0 even with a +0.5 contribution
    assert adj.iloc[1] == pytest.approx(2.7)
    assert pd.isna(adj.iloc[2])  # NaN technical score stays NaN


# ---------------------------------------------------------------------------
# in_scope_filings / relevance_counts / select_sonnet_subset
# ---------------------------------------------------------------------------
def test_in_scope_filings_date_cutoff_currency_and_per_symbol_activity():
    filings = pd.DataFrame([
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-01-02"), "form": "10-K",
         "accession": "A1", "excerpt_text": "x", "excerpt_chars": 1},
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-01-10"), "form": "10-Q",
         "accession": "A2", "excerpt_text": "x", "excerpt_chars": 1},
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-01-20"), "form": "10-Q",
         "accession": "A3", "excerpt_text": "x", "excerpt_chars": 1},
        {"ticker": "BBB", "filing_date": pd.Timestamp("2024-01-05"), "form": "10-K",
         "accession": "B1", "excerpt_text": "x", "excerpt_chars": 1},
        {"ticker": "CCC", "filing_date": pd.Timestamp("2026-09-01"), "form": "10-Q",
         "accession": "C1", "excerpt_text": "x", "excerpt_chars": 1},
    ])
    train_days = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-06",
                                   "2024-01-10", "2024-01-15", "2024-01-25"])
    # AAA is active only on 2024-01-03; BBB only on 2024-01-06; CCC never.
    panel = pd.DataFrame(
        {"AAA": [0.0, 3.0, 0.0, 0.0, 0.0, 0.0],
         "BBB": [0.0, 0.0, 2.0, 0.0, 0.0, 0.0],
         "CCC": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]},
        index=train_days,
    )
    scoped = in_scope_filings(filings, {"v1": panel}, train_days)
    # A1 current 2024-01-02..2024-01-10, covers AAA's active day 01-03.
    # A2 current 2024-01-10..2024-01-20, no AAA active day inside.
    # A3 current from 2024-01-20, no AAA active day after it.
    # B1 current from 2024-01-05, covers BBB's active day 01-06.
    # C1 is dated after the train cutoff (and CCC is never active).
    assert set(scoped["accession"]) == {"A1", "B1"}


def test_relevance_counts_near_threshold_only():
    days = pd.DatetimeIndex(["2024-01-02", "2024-01-03"])
    symbols = ["A", "B", "C", "D", "E", "F"]
    panel = pd.DataFrame(
        [[5.5, 5.0, 4.5, 4.0, 3.5, 0.5],
         [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
        index=days, columns=symbols,
    )
    filings = pd.DataFrame([
        {"ticker": s, "filing_date": pd.Timestamp("2024-01-01"), "form": "10-K",
         "accession": f"{s}1", "excerpt_text": "x", "excerpt_chars": 1}
        for s in symbols
    ])
    counts = relevance_counts(filings, {"v1": panel}, days)
    # Day 1 cutoff = 5th largest = 3.5; near (|score - 3.5| <= 1.0, > 0):
    # C (4.5), D (4.0), E (3.5). Day 2 has no active scores.
    by_accession = dict(zip(filings["accession"], counts))
    assert by_accession == {"A1": 0, "B1": 0, "C1": 1, "D1": 1, "E1": 1, "F1": 0}


def test_select_sonnet_subset_ranking_budget_and_confidence_gate():
    filings = pd.DataFrame([
        {"ticker": "A", "filing_date": pd.Timestamp("2024-01-01"), "form": "10-K",
         "accession": "A1", "excerpt_text": "x" * 4000, "excerpt_chars": 4000},
        {"ticker": "B", "filing_date": pd.Timestamp("2024-01-02"), "form": "10-K",
         "accession": "B1", "excerpt_text": "x" * 4000, "excerpt_chars": 4000},
        {"ticker": "C", "filing_date": pd.Timestamp("2024-01-03"), "form": "10-K",
         "accession": "C1", "excerpt_text": "x" * 4000, "excerpt_chars": 4000},
    ])
    scored = {
        "A1": {"score": 0.5, "confidence": 0.9},
        "B1": {"score": 0.5, "confidence": 0.5},  # below the 0.6 gate
        "C1": {"score": 0.5, "confidence": 0.7},
    }
    counts = pd.Series([10, 100, 10], index=filings.index)
    # Each filing's worst-case Sonnet cost is ~$0.012224 (4,000 chars in
    # + 1,000 max out tokens at $2/$10 per MTok); budget $0.013 admits
    # only the highest-ranked candidate (C1: 10 triples, conf 0.7 ranks
    # before A1: 10 triples, conf 0.9, because the tie-break is
    # confidence ascending). B1 is excluded by the confidence gate.
    chosen = select_sonnet_subset(scored, counts, filings, budget_usd=0.013)
    assert chosen == [2]  # C1 only


# ---------------------------------------------------------------------------
# score_pass (fake providers; no API)
# ---------------------------------------------------------------------------
class _FakeProvider:
    model_id = "claude-haiku-5-5"

    def __init__(self, cost=0.01, fail_accessions=()):
        self.cost = cost
        self.fail_accessions = set(fail_accessions)

    def score(self, symbol, documents, excerpt, as_of):
        if symbol in self.fail_accessions:
            raise RuntimeError("boom")
        return {"score": 0.5, "confidence": 0.9, "cost_usd": self.cost,
                "input_tokens": 100, "output_tokens": 10}


def _fake_filings(n):
    return pd.DataFrame([
        {"ticker": f"S{i}", "filing_date": pd.Timestamp("2024-01-02"), "form": "10-K",
         "accession": f"ACC{i}", "excerpt_text": "x" * 4000, "excerpt_chars": 4000}
        for i in range(n)
    ])


def test_score_pass_budget_guard_stops_before_exceeding(tmp_path):
    filings = _fake_filings(3)
    scores = {}
    summary = score_pass(
        _FakeProvider(cost=0.01), filings, "haiku",
        spent_before_usd=0.0, budget_usd=0.0205,
        save_path=tmp_path / "scores.json", scores=scores,
    )
    assert summary["n_scored_new"] == 2
    assert summary["cost_usd"] == pytest.approx(0.02)
    assert "ACC2" not in scores  # third filing never attempted


def test_score_pass_records_failures_and_continues(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    filings = _fake_filings(3)
    scores = {}
    summary = score_pass(
        _FakeProvider(cost=0.01, fail_accessions={"S1"}), filings, "haiku",
        spent_before_usd=0.0, budget_usd=10.0,
        save_path=tmp_path / "scores.json", scores=scores,
    )
    assert summary["n_failed"] == 1
    assert summary["n_scored_new"] == 2
    assert "error" in scores["ACC1"]


def test_score_pass_circuit_breaker_after_ten_consecutive_failures(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    filings = _fake_filings(12)
    scores = {}
    with pytest.raises(RuntimeError, match="circuit breaker"):
        score_pass(
            _FakeProvider(fail_accessions={f"S{i}" for i in range(12)}),
            filings, "haiku", spent_before_usd=0.0, budget_usd=10.0,
            save_path=tmp_path / "scores.json", scores=scores,
        )
