#!/usr/bin/env python3
"""Sentence-level filing-change feature (exp-filing-delta-v1).

Deterministic text arithmetic only — no LLM calls, $0 spend. For each
filing, the fraction of its Risk Factors (Item 1A) sentences that
introduce materially new language vs the most recent COMPARABLE
filing (same ticker, same form type, most recent filing_date strictly
before), following the Lazy Prices premise (Cohen, Malloy & Nguyen,
NBER w25084).

Definitions (frozen in experiments/exp-filing-delta-v1-design.md):
- A sentence is ELIGIBLE if it has >= 4 word tokens ([a-z]{3,}).
- For each eligible current sentence, the nearest match is the maximum
  token-set Jaccard similarity against ANY eligible sentence of the
  prior filing's risk text. "Materially new" = max Jaccard < 0.50.
- delta = (# eligible current sentences materially new) / (# eligible
  current sentences). Missing/incomparable section -> None (unknown).
- Order-independent: a paragraph moved without edits still matches
  (Jaccard ~1.0) and does not count as new language.

Efficiency: nearest-match Jaccard is computed exactly from an inverted
index (token -> prior sentence ids). The shared-token count between
two sentences IS their token-set intersection size, so Jaccard =
shared / (A + B - shared) needs no set operations per pair — only
integer increments over the (small) candidate set of prior sentences
sharing at least one token. Sentences sharing no token have max
Jaccard 0.0 by definition.
"""

from __future__ import annotations

import re

WORD_RE = re.compile(r"[a-z]{3,}")
# Split on a sentence terminator followed by whitespace. Numbers like
# "2.0" do not split (the "." is not followed by whitespace);
# abbreviations like "U.S." DO split, leaving a short fragment that is
# excluded by the >= 4 token eligibility rule.
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
MIN_SENTENCE_TOKENS = 4
NEW_JACCARD_THRESHOLD = 0.50


def split_sentences(text: str) -> list[str]:
    """Split risk-factor text into sentences, deterministically.

    Lowercases, collapses whitespace, strips leading list numbering /
    bullets (e.g. "1." / "-" / "•") from each sentence. Returns the
    raw sentence strings (before token filtering).
    """
    if not text:
        return []
    cleaned = re.sub(r"\s+", " ", str(text)).strip()
    if not cleaned:
        return []
    parts = SENTENCE_SPLIT_RE.split(cleaned)
    out = []
    for part in parts:
        s = part.strip()
        # Strip leading list markers: "1.", "a)", "-", "•", "(i)".
        s = re.sub(r"^(\(\w+\)|\w+[.)]|[-•])\s+", "", s)
        if s:
            out.append(s)
    return out


def sentence_tokens(sentence: str) -> set[str]:
    """Word-token set of a sentence ([a-z]{3,}, lowercased)."""
    return set(WORD_RE.findall(sentence.lower()))


def eligible_sentences(text: str) -> list[tuple[str, set[str]]]:
    """(sentence, token set) pairs with >= MIN_SENTENCE_TOKENS tokens."""
    out = []
    for sentence in split_sentences(text):
        toks = sentence_tokens(sentence)
        if len(toks) >= MIN_SENTENCE_TOKENS:
            out.append((sentence, toks))
    return out


def _nearest_match_jaccards(
    current: list[tuple[str, set[str]]],
    prior: list[tuple[str, set[str]]],
) -> list[float]:
    """Max token-set Jaccard of each current sentence vs any prior sentence.

    Exact: the inverted index counts distinct shared tokens per prior
    sentence, and Jaccard = shared / (A + B - shared). A current
    sentence sharing no token with any prior sentence scores 0.0.
    """
    prior_lens = [len(toks) for _, toks in prior]
    index: dict[str, list[int]] = {}
    for j, (_, toks) in enumerate(prior):
        for tok in toks:
            index.setdefault(tok, []).append(j)
    out: list[float] = []
    for _, ctoks in current:
        shared: dict[int, int] = {}
        for tok in ctoks:
            for j in index.get(tok, ()):
                shared[j] = shared.get(j, 0) + 1
        best = 0.0
        a = len(ctoks)
        for j, inter in shared.items():
            b = prior_lens[j]
            jac = inter / (a + b - inter) if (a + b - inter) > 0 else 0.0
            if jac > best:
                best = jac
                if best >= 1.0:
                    break
        out.append(best)
    return out


def compute_delta(current_risk: str, prior_risk: str) -> dict | None:
    """Delta feature for one filing vs its prior comparable filing.

    Returns None (unknown) when the comparison is impossible: either
    side has no eligible risk sentences. Otherwise returns a dict
    with delta, n_sentences, n_new, and the strongest changed passage
    (lowest max-Jaccard eligible sentence) with its closest prior
    match and that match's Jaccard.
    """
    current = eligible_sentences(current_risk or "")
    prior = eligible_sentences(prior_risk or "")
    if not current or not prior:
        return None
    jaccards = _nearest_match_jaccards(current, prior)
    n_new = sum(1 for j in jaccards if j < NEW_JACCARD_THRESHOLD)
    # Strongest changed passage: the most novel sentence (lowest max
    # Jaccard); ties break by document order (deterministic).
    worst_i = min(range(len(jaccards)), key=lambda i: (jaccards[i], i))
    worst_sentence, worst_toks = current[worst_i]
    # Closest prior match for the strongest passage (recomputed
    # directly; the inverted-index pass only kept the max value).
    best_j, best_prior = 0.0, ""
    for prior_sentence, ptoks in prior:
        inter = len(worst_toks & ptoks)
        denom = len(worst_toks) + len(ptoks) - inter
        jac = inter / denom if denom > 0 else 0.0
        if jac > best_j:
            best_j, best_prior = jac, prior_sentence
    return {
        "delta": n_new / len(current),
        "n_sentences": len(current),
        "n_new": n_new,
        "strongest_passage": worst_sentence,
        "strongest_jaccard": float(jaccards[worst_i]),
        "closest_prior_match": best_prior,
        "closest_match_jaccard": float(best_j),
    }


def find_prior_filing(filings, ticker: str, form: str, filing_date):
    """Most recent prior filing, same ticker + same form, date < filing_date.

    `filings` is a DataFrame with ticker/form/filing_date columns (the
    full v1 excerpt set, not just the scored subset). Returns the row
    (as a Series) or None. No fallback: the immediately prior
    same-form filing is THE comparable filing, even if its risk
    section is missing (that case yields unknown downstream).
    """
    import pandas as pd

    sub = filings[
        (filings["ticker"] == ticker)
        & (filings["form"] == form)
        & (filings["filing_date"] < pd.Timestamp(filing_date))
    ]
    if sub.empty:
        return None
    return sub.sort_values("filing_date").iloc[-1]


def carry_forward_delta(delta_table, days, symbols) -> "pd.DataFrame":
    """Delta panel: latest in-scope filing's delta per symbol/day.

    Same carried-forward construction as the Haiku score panel: for
    each symbol and day, the delta of the latest filing with
    filing_date <= day. NaN where the symbol has no in-scope filing
    on or before that day, or where the latest filing's delta is
    unknown. Point-in-time: only filings dated <= the day contribute.
    """
    import numpy as np
    import pandas as pd

    panel = pd.DataFrame(np.nan, index=days, columns=list(symbols))
    for sym, group in delta_table.groupby("ticker"):
        if sym not in panel.columns:
            continue
        group = group.sort_values("filing_date")
        dates = group["filing_date"].to_numpy()
        vals = group["delta"].to_numpy(dtype=float)
        pos = np.searchsorted(dates, days.to_numpy(), side="right") - 1
        panel[sym] = np.where(pos >= 0, vals[np.maximum(pos, 0)], np.nan)
    return panel


def delta_pct_rank_panel(delta_panel) -> "pd.DataFrame":
    """Cross-sectional percentile rank of delta per day (0..1; NaN stays NaN)."""
    return delta_panel.rank(axis=1, pct=True)


def tilted_score_panel(score_panel, delta_panel, tilt: float = 0.25):
    """Haiku score tilted by the delta feature (frozen rule).

    score_adj = haiku - tilt * (2 * delta_pct_rank - 1). High
    new-language fraction (rank near 1) is penalized by up to `tilt`;
    low fraction (rank near 0) is boosted by up to `tilt`. Unknown
    delta (NaN rank) -> tilt 0: the Haiku score stands unchanged.
    """
    rank = delta_pct_rank_panel(delta_panel)
    adjustment = -tilt * (2.0 * rank - 1.0)
    return score_panel + adjustment.fillna(0.0)


def build_delta_table(filings, in_scope_filings) -> "pd.DataFrame":
    """Delta feature for every in-scope filing.

    `filings`: full v1 excerpt DataFrame (prior lookup source).
    `in_scope_filings`: the filings to score (must carry ticker, form,
    filing_date, accession, risk_text). Returns one row per in-scope
    filing with delta (float or None), prior accession/date,
    n_sentences, n_new, strongest passage + closest match, and an
    exclusion_reason (None when delta is known).
    """
    import pandas as pd

    rows = []
    for _, row in in_scope_filings.iterrows():
        rec = {
            "ticker": row["ticker"],
            "filing_date": row["filing_date"],
            "form": row["form"],
            "accession": row["accession"],
            "prior_accession": None,
            "prior_filing_date": None,
            "delta": None,
            "n_sentences": 0,
            "n_new": 0,
            "strongest_passage": "",
            "strongest_jaccard": None,
            "closest_prior_match": "",
            "closest_match_jaccard": None,
            "exclusion_reason": None,
        }
        current_risk = row.get("risk_text") or ""
        if not str(current_risk).strip():
            rec["exclusion_reason"] = "current_risk_missing"
            rows.append(rec)
            continue
        prior = find_prior_filing(
            filings, row["ticker"], row["form"], row["filing_date"])
        if prior is None:
            rec["exclusion_reason"] = "no_prior_same_form"
            rows.append(rec)
            continue
        rec["prior_accession"] = prior["accession"]
        rec["prior_filing_date"] = prior["filing_date"]
        prior_risk = prior.get("risk_text") or ""
        if not str(prior_risk).strip():
            rec["exclusion_reason"] = "prior_risk_missing"
            rows.append(rec)
            continue
        result = compute_delta(current_risk, prior_risk)
        if result is None:
            rec["exclusion_reason"] = "no_eligible_sentences"
            rows.append(rec)
            continue
        rec.update(result)
        rows.append(rec)
    return pd.DataFrame(rows)
