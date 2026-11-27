#!/usr/bin/env python3
"""Tests for the filing-delta feature (exp-filing-delta-v1).

Fixtures (frozen in experiments/exp-filing-delta-v1-design.md):
(a) a genuinely new liquidity warning -> high delta;
(b) a paragraph moved without substantive edits -> low delta;
(c) a missing prior section -> unknown.
Plus: brute-force cross-check of the inverted-index nearest match,
same-form prior matching, the 0.50 Jaccard boundary, and the panel
tilt rule (unknown -> tilt 0, high delta penalized).
"""

import numpy as np
import pandas as pd
import pytest

import filing_delta as fd

PRIOR_RISK = (
    "Our business faces intense competition from larger rivals with greater "
    "financial resources and broader product offerings. "
    "Changes in consumer demand may adversely affect our net sales and "
    "operating results in future periods. "
    "We rely on third party suppliers for key components and any disruption "
    "could delay product shipments. "
    "Fluctuations in foreign currency exchange rates could reduce the value "
    "of our international earnings."
)

NEW_LIQUIDITY_WARNING = (
    "The company may be unable to refinance its two billion dollar "
    "revolving credit facility maturing in March next year. "
    "Management has engaged advisors to evaluate a potential debt "
    "restructuring if liquidity conditions continue to deteriorate. "
    "Covenant breaches under existing loan agreements could trigger "
    "accelerated repayment demands from lenders."
)


def test_new_liquidity_warning_high_delta():
    """(a) Three genuinely new sentences added to an unchanged base."""
    current = PRIOR_RISK + " " + NEW_LIQUIDITY_WARNING
    result = fd.compute_delta(current, PRIOR_RISK)
    assert result is not None
    assert result["n_sentences"] == 7
    assert result["n_new"] == 3
    assert result["delta"] == pytest.approx(3 / 7)
    # The strongest changed passage is one of the new warning sentences.
    assert result["strongest_jaccard"] < 0.20
    assert "credit facility" in result["strongest_passage"] or \
        "restructuring" in result["strongest_passage"] or \
        "Covenant" in result["strongest_passage"]


def test_paragraph_moved_without_edits_low_delta():
    """(b) Same sentences, reordered + reformatted -> no new language.

    Order-independence is the point: a moved paragraph is not a change.
    """
    sentences = [s.strip() for s in PRIOR_RISK.split(". ") if s.strip()]
    reordered = ". ".join(reversed(sentences))
    # Extra whitespace + a comma added (punctuation does not change tokens).
    current = "  " + reordered.replace(
        "intense competition", "intense, competition") + "  "
    result = fd.compute_delta(current, PRIOR_RISK)
    assert result is not None
    assert result["delta"] == 0.0
    assert result["n_new"] == 0
    assert result["n_sentences"] == 4


def test_missing_prior_section_unknown():
    """(c) Prior risk section missing -> unknown, not 'no change'."""
    assert fd.compute_delta(PRIOR_RISK, "") is None
    assert fd.compute_delta(PRIOR_RISK, "   ") is None
    assert fd.compute_delta("", PRIOR_RISK) is None


def test_jaccard_boundary_half_is_not_new():
    """Max Jaccard exactly 0.50 is NOT materially new (needs < 0.50)."""
    # A: 6 tokens; B: 6 tokens; intersection 4 -> 4/(6+6-4) = 0.50.
    current = "Alpha bravo charlie delta echo foxtrot."
    prior = "Alpha bravo charlie delta golf hotel."
    result = fd.compute_delta(current, prior)
    assert result is not None
    assert result["delta"] == 0.0
    # One more sentence at Jaccard 1/3 -> new: delta 1/2.
    current2 = current + " Indigo juliet kilo lima mike november."
    prior2 = prior + " Indigo juliet kilo oscar papa quebec."
    result2 = fd.compute_delta(current2, prior2)
    assert result2 is not None
    assert result2["n_new"] == 1
    assert result2["delta"] == pytest.approx(0.5)


def test_nearest_match_matches_brute_force():
    """Inverted-index max Jaccard equals brute-force pairwise Jaccard."""
    rng = np.random.default_rng(7)
    vocab = [f"word{i}" for i in range(40)]

    def random_text(n_sent):
        sents = []
        for _ in range(n_sent):
            k = int(rng.integers(5, 12))
            toks = rng.choice(vocab, size=k, replace=False)
            sents.append(" ".join(toks) + ".")
        return " ".join(sents)

    for _ in range(10):
        cur_text = random_text(8)
        pri_text = random_text(8)
        cur = fd.eligible_sentences(cur_text)
        pri = fd.eligible_sentences(pri_text)
        fast = fd._nearest_match_jaccards(cur, pri)
        for i, (_, ctoks) in enumerate(cur):
            brute = 0.0
            for _, ptoks in pri:
                inter = len(ctoks & ptoks)
                denom = len(ctoks) + len(ptoks) - inter
                brute = max(brute, inter / denom if denom else 0.0)
            assert fast[i] == pytest.approx(brute), (i, fast[i], brute)


def test_prior_matching_same_form_only():
    """Prior = most recent filing of the SAME form type, even if an
    intervening filing of the other form is more recent."""
    filings = pd.DataFrame([
        {"ticker": "AAPL", "form": "10-K", "filing_date": pd.Timestamp("2023-11-03"),
         "accession": "k23", "risk_text": "Alpha bravo charlie delta echo."},
        {"ticker": "AAPL", "form": "10-Q", "filing_date": pd.Timestamp("2024-02-02"),
         "accession": "q1", "risk_text": "Alpha bravo charlie delta echo."},
        {"ticker": "AAPL", "form": "10-K", "filing_date": pd.Timestamp("2024-11-01"),
         "accession": "k24", "risk_text": "Alpha bravo charlie delta echo."},
        {"ticker": "AAPL", "form": "10-Q", "filing_date": pd.Timestamp("2024-05-03"),
         "accession": "q2", "risk_text": "Alpha bravo charlie delta echo."},
    ])
    # The 2024-05 10-Q's prior is the 2024-02 10-Q, NOT the 2024-11 10-K
    # (wrong form) and not an older filing.
    prior = fd.find_prior_filing(filings, "AAPL", "10-Q", "2024-05-03")
    assert prior["accession"] == "q1"
    # The 2024 10-K's prior is the 2023 10-K.
    prior_k = fd.find_prior_filing(filings, "AAPL", "10-K", "2024-11-01")
    assert prior_k["accession"] == "k23"
    # No prior same-form filing -> None.
    assert fd.find_prior_filing(filings, "AAPL", "10-Q", "2024-02-02") is None


def test_build_delta_table_exclusion_reasons():
    """Unknown cases carry distinct, countable exclusion reasons."""
    filings = pd.DataFrame([
        {"ticker": "AAA", "form": "10-Q", "filing_date": pd.Timestamp("2024-02-01"),
         "accession": "a1", "risk_text": PRIOR_RISK},
        {"ticker": "AAA", "form": "10-Q", "filing_date": pd.Timestamp("2024-05-01"),
         "accession": "a2", "risk_text": PRIOR_RISK + " " + NEW_LIQUIDITY_WARNING},
        {"ticker": "BBB", "form": "10-Q", "filing_date": pd.Timestamp("2024-02-01"),
         "accession": "b1", "risk_text": ""},
        {"ticker": "BBB", "form": "10-Q", "filing_date": pd.Timestamp("2024-05-01"),
         "accession": "b2", "risk_text": PRIOR_RISK},
        {"ticker": "CCC", "form": "10-K", "filing_date": pd.Timestamp("2024-05-01"),
         "accession": "c1", "risk_text": PRIOR_RISK},
    ])
    in_scope = filings[filings["accession"].isin(["a2", "b2", "c1"])].copy()
    table = fd.build_delta_table(filings, in_scope)
    by_acc = table.set_index("accession")
    assert by_acc.loc["a2", "delta"] == pytest.approx(3 / 7)
    assert by_acc.loc["a2", "prior_accession"] == "a1"
    assert by_acc.loc["a2", "exclusion_reason"] is None
    # BBB's prior filing has no risk text -> prior_risk_missing.
    assert pd.isna(by_acc.loc["b2", "delta"])
    assert by_acc.loc["b2", "exclusion_reason"] == "prior_risk_missing"
    # CCC has no prior same-form filing at all.
    assert pd.isna(by_acc.loc["c1", "delta"])
    assert by_acc.loc["c1", "exclusion_reason"] == "no_prior_same_form"


def test_carry_forward_and_tilt():
    """Delta carries forward like the score; unknown -> tilt 0; high
    delta is penalized and low delta boosted by up to 0.25."""
    days = pd.DatetimeIndex(["2024-02-01", "2024-02-02", "2024-05-02"])
    table = pd.DataFrame([
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-02-01"), "delta": 0.8},
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-05-01"), "delta": 0.1},
        {"ticker": "BBB", "filing_date": pd.Timestamp("2024-02-01"), "delta": np.nan},
    ])
    panel = fd.carry_forward_delta(table, days, ["AAA", "BBB"])
    assert panel.loc["2024-02-01", "AAA"] == 0.8
    assert panel.loc["2024-02-02", "AAA"] == 0.8  # carried forward
    assert panel.loc["2024-05-02", "AAA"] == 0.1
    assert np.isnan(panel.loc["2024-02-01", "BBB"])  # unknown stays NaN
    score = pd.DataFrame(0.5, index=days, columns=["AAA", "BBB"])
    adj = fd.tilted_score_panel(score, panel, tilt=0.25)
    # 2024-02-01: AAA delta 0.8 is the only known value -> rank 1.0
    # -> penalized by the full 0.25. BBB unknown -> unchanged.
    assert adj.loc["2024-02-01", "AAA"] == pytest.approx(0.25)
    assert adj.loc["2024-02-01", "BBB"] == pytest.approx(0.5)
    # 2024-05-02: AAA delta 0.1 is again the only known value -> rank
    # 1.0 (a single known value ranks at the top). Tilt is by rank,
    # not by the raw fraction: documented behaviour of the frozen rule.
    assert adj.loc["2024-05-02", "AAA"] == pytest.approx(0.25)
