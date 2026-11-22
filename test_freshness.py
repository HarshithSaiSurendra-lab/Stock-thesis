"""Tests for the freshness test driver (exp-technical-plus-llm-freshness-v1).

Offline: synthetic panels/filings only. No API calls, no disk cache.
The 2.9 chars/token estimator is the point of the first test: Phase 2's
chars/4 estimate undercounted SEC text by ~36% and breached the Sonnet
sub-budget, so this run estimates at the measured 2.9 rate.
"""

import numpy as np
import pandas as pd
import pytest

import run_llm_backtest as p2
from run_freshness_backtest import (
    current_filing_series_union,
    estimate_call_cost_29,
    in_scope_holdout_filings,
)
from run_llm_backtest import current_filing_series


# ---------------------------------------------------------------------------
# estimate_call_cost_29
# ---------------------------------------------------------------------------
def test_estimate_call_cost_29_uses_measured_rate_not_chars_over_4():
    # 29,000-char excerpt, Sonnet: input tokens at 2.9 chars/token.
    from llm_sentiment import SYSTEM_PROMPT

    expected_in = int(len(SYSTEM_PROMPT) / 2.9) + int(29_000 / 2.9)
    expected = expected_in * 2.0 / 1_000_000 + 1000 * 10.0 / 1_000_000
    est29 = estimate_call_cost_29(29_000, "claude-sonnet-5-5")
    assert est29 == pytest.approx(expected, abs=1e-9)
    # The old chars/4 estimate for the same excerpt is lower — that is
    # exactly the undercount that breached Phase 2's Sonnet sub-budget.
    est4 = p2.estimate_call_cost(29_000, "claude-sonnet-5-5")
    assert est29 > est4
    assert est29 / est4 == pytest.approx(1.22, abs=0.02)


def test_estimate_call_cost_29_haiku_is_cheaper_than_sonnet():
    assert estimate_call_cost_29(70_000, "claude-haiku-5-5") < estimate_call_cost_29(
        70_000, "claude-sonnet-5-5"
    )


# ---------------------------------------------------------------------------
# in_scope_holdout_filings
# ---------------------------------------------------------------------------
def test_in_scope_holdout_filings_only_holdout_window_and_active_days():
    filings = pd.DataFrame([
        # Train filing for AAA, current on active holdout days — excluded:
        # it is dated before the holdout window.
        {"ticker": "AAA", "filing_date": pd.Timestamp("2026-08-10"), "form": "10-Q",
         "accession": "A0", "excerpt_text": "x", "excerpt_chars": 1},
        # Holdout filing for AAA, current on AAA's active holdout day.
        {"ticker": "AAA", "filing_date": pd.Timestamp("2026-08-20"), "form": "10-Q",
         "accession": "A1", "excerpt_text": "x", "excerpt_chars": 1},
        # Holdout filing for AAA, current only on inactive days.
        {"ticker": "AAA", "filing_date": pd.Timestamp("2026-09-15"), "form": "10-Q",
         "accession": "A2", "excerpt_text": "x", "excerpt_chars": 1},
        # Holdout filing for BBB, which is never active on holdout days.
        {"ticker": "BBB", "filing_date": pd.Timestamp("2026-08-21"), "form": "10-Q",
         "accession": "B1", "excerpt_text": "x", "excerpt_chars": 1},
    ])
    holdout_days = pd.DatetimeIndex(["2026-08-18", "2026-08-20", "2026-08-25",
                                     "2026-09-15", "2026-09-16"])
    # AAA active only on 2026-08-20; BBB never active.
    panel = pd.DataFrame(
        {"AAA": [0.0, 3.0, 0.0, 0.0, 0.0],
         "BBB": [0.0, 0.0, 0.0, 0.0, 0.0]},
        index=holdout_days,
    )
    scoped = in_scope_holdout_filings(filings, {"v1": panel}, holdout_days)
    # A1 is current 2026-08-20..2026-09-15 and covers AAA's active day.
    # A2 is current from 2026-09-15; AAA is inactive on 09-15 and 09-16.
    # B1's symbol is never active. A0 predates the holdout window.
    assert set(scoped["accession"]) == {"A1"}


# ---------------------------------------------------------------------------
# current_filing_series_union
# ---------------------------------------------------------------------------
def _union_filings():
    return pd.DataFrame([
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-01-02"), "form": "10-K",
         "accession": "A1", "excerpt_text": "x", "excerpt_chars": 1},
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-02-01"), "form": "10-Q",
         "accession": "A2", "excerpt_text": "x", "excerpt_chars": 1},
        # Holdout-window filing: becomes current on its filing date.
        {"ticker": "AAA", "filing_date": pd.Timestamp("2026-09-01"), "form": "10-Q",
         "accession": "A3", "excerpt_text": "x", "excerpt_chars": 1},
        {"ticker": "BBB", "filing_date": pd.Timestamp("2024-01-05"), "form": "10-K",
         "accession": "B1", "excerpt_text": "x", "excerpt_chars": 1},
    ])


def test_union_series_matches_stale_on_train_days_and_uses_fresh_on_holdout():
    days = pd.DatetimeIndex([
        "2024-01-01", "2024-01-02", "2024-01-15", "2024-02-01",
        "2024-03-01", "2026-08-31", "2026-09-01", "2026-10-01",
    ])
    filings = _union_filings()
    train_filings = filings[filings["filing_date"] <= "2026-08-17"]
    stale = current_filing_series(train_filings, days)
    union = current_filing_series_union(filings, days)
    train_days = days[days < "2026-08-18"]
    # Train days identical (no holdout filing predates a train day).
    assert (union.loc[train_days].fillna("") == stale.loc[train_days].fillna("")).all().all()
    # Holdout days: fresh filing takes over from its filing date.
    assert union.at[pd.Timestamp("2026-08-31"), "AAA"] == "A2"
    assert union.at[pd.Timestamp("2026-09-01"), "AAA"] == "A3"
    assert union.at[pd.Timestamp("2026-10-01"), "AAA"] == "A3"
    # Stale series still carries the pre-holdout filing forward.
    assert stale.at[pd.Timestamp("2026-09-01"), "AAA"] == "A2"


# ---------------------------------------------------------------------------
# Sonnet subset selection at the 2.9 rate (the Phase 2 failure mode)
# ---------------------------------------------------------------------------
def test_sonnet_subset_at_2_9_rate_admits_fewer_than_chars_over_4(monkeypatch):
    filings = pd.DataFrame([
        {"ticker": "A", "filing_date": pd.Timestamp("2026-08-20"), "form": "10-Q",
         "accession": "A1", "excerpt_text": "x" * 4000, "excerpt_chars": 4000},
        {"ticker": "B", "filing_date": pd.Timestamp("2026-08-21"), "form": "10-Q",
         "accession": "B1", "excerpt_text": "x" * 4000, "excerpt_chars": 4000},
        {"ticker": "C", "filing_date": pd.Timestamp("2026-08-22"), "form": "10-Q",
         "accession": "C1", "excerpt_text": "x" * 4000, "excerpt_chars": 4000},
    ])
    scored = {
        "A1": {"score": 0.5, "confidence": 0.9},
        "B1": {"score": 0.5, "confidence": 0.7},
        "C1": {"score": 0.5, "confidence": 0.8},
    }
    counts = pd.Series([10, 100, 10], index=filings.index)
    # Ranking: B1 (100 triples) first, then C1 (10, conf 0.8), then A1.
    # Budget $0.0245 admits TWO filings at the chars/4 estimate
    # ($0.012066 each -> $0.024132) but only ONE at the 2.9 rate
    # ($0.012848 each -> two would be $0.025696). This is the exact
    # undercount that breached Phase 2's $5.00 Sonnet sub-budget.
    chosen_old = p2.select_sonnet_subset(scored, counts, filings, budget_usd=0.0245)
    assert chosen_old == [1, 2]
    monkeypatch.setattr(p2, "estimate_call_cost", estimate_call_cost_29)
    chosen_new = p2.select_sonnet_subset(scored, counts, filings, budget_usd=0.0245)
    assert chosen_new == [1]
