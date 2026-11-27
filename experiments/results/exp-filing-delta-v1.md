# exp-filing-delta-v1 — results (TRAIN ONLY)

Run at 2026-11-27T01:09:34.837618+00:00 UTC; $0 new spend; runtime 511.8s. Window 2024-01-02..2026-08-17 (658 train days). Frozen tilt 0.25, new-Jaccard threshold 0.5, IC horizon 20d, cohort H=5, min hold 10d, max positions 60.

## Coverage

- In-scope filings: 1166; known delta: 1035; unknown: 131 ({'current_risk_missing': 112, 'no_prior_same_form': 7, 'prior_risk_missing': 6, 'no_eligible_sentences': 6}).
- Known-delta issuers: 133; event dates: 309.
- IC events: 84721 pairs, 133 issuers, 637 days; excluded by boundary rule: 50; excluded (no sector mapping): 0.
- Train days with a known delta: 658/658; with a known delta in the selection: 658.
- Risk sentences per known-delta filing: median 9, mean 99.9; fraction with <10 sentences: 0.50.
- Selection differs between arms on 658 train days.

## Rank correlation (primary): 20d sector-relative IC

| series | n days | mean IC | NW t (lag 19) | mean symbols/day |
|---|---|---|---|---|
| haiku | 637 | 0.0350 | 2.0261565096535223 | 133.0 |
| score_adj | 637 | 0.0224 | 1.254181557661537 | 133.0 |
| delta | 637 | 0.0407 | 2.498822130647688 | 115.6 |

IC change (score_adj − Haiku): -0.0126 (paired daily NW SE 0.0052, t -2.4367879685779883).
Issuer-cluster bootstrap (500 draws): mean change -0.0124, 95% CI [-0.0235, -0.0006], fraction positive 0.02.

## Portfolio: baseline (v2 Haiku) vs experiment (Haiku + delta tilt)

| cost | arm | net return | Sharpe | max DD | turnover/day | trades |
|---|---|---|---|---|---|---|
| 0bps | baseline | 55.73% | 0.9319580352240308 | -23.55% | 0.0184 | 798 |
| 0bps | experiment | 50.59% | 0.9961151056393599 | -17.93% | 0.0301 | 664 |
| 10bps | baseline | 53.86% | 0.9094656241339992 | -23.65% | 0.0184 | 798 |
| 10bps | experiment | 47.64% | 0.952469506581709 | -18.05% | 0.0301 | 664 |
| 20bps | baseline | 52.01% | 0.8869540824753142 | -23.75% | 0.0184 | 798 |
| 20bps | experiment | 44.75% | 0.9087714428215415 | -18.16% | 0.0301 | 664 |

Net-return difference at 10bps: -6.22pp. Time-block bootstrap (20-session blocks, 500 draws): mean -6.77pp, 95% CI [-54.91pp, 39.83pp], fraction positive 0.39.

## Sensitivities

1. Remove the largest contributor (CMG, attribution -10.42pp): net10 baseline 56.95% vs experiment 70.06% (diff 13.11pp); IC change -0.0119 (Haiku 0.0357 vs score_adj 0.0238).
2. Delay experiment entry one extra session: net10 45.58% (vs experiment 47.64%), max DD -17.82%, turnover 0.0306, trades 717.

## Passage pairs (top-5 highest-delta filings)

Judge whether the feature detects meaningful change or formatting noise. `strongest_jaccard` is the max Jaccard of the most novel sentence vs any prior sentence; `closest_match_jaccard` is that sentence's best prior match.

### CB 10-Q 2023-11-01 (delta 1.00, 20/20 sentences)

Accession 0000896159-23-000016 (prior 0000896159-23-000013).

**Strongest changed passage** (Jaccard 0.00):

> subsidiaries operate in a manner so that none of these companies should be subject to U.S.

**Closest prior match** (Jaccard 0.00):

> 

### SPGI 10-Q 2024-04-25 (delta 1.00, 1/1 sentences)

Accession 0000064040-24-000125 (prior 0000064040-23-000193).

**Strongest changed passage** (Jaccard 0.23):

> Risk Factors For a discussion of our risk factors please see Item 1A, Risk Factors in our most recent Form 10-K.

**Closest prior match** (Jaccard 0.23):

> Risk Factors In addition to the other information set forth in this Quarterly Report on Form 10-Q, you should carefully consider the risk factors we have previously disclosed in Item 1A, Risk Factors, in our most recent Form 10-K, and the additional risk factor below.

### CB 10-Q 2024-04-26 (delta 1.00, 1/1 sentences)

Accession 0000896159-24-000006 (prior 0000896159-23-000016).

**Strongest changed passage** (Jaccard 0.48):

> Risk Factors There have been no material changes to the risk factors described under "Risk Factors" under Item 1A of Part I of our 2023 Form 10-K.

**Closest prior match** (Jaccard 0.48):

> Risk Factors The following supplements the risk factors that could have a material impact on our results of operations or financial condition as described under "Risk Factors" under Item 1A of Part I of our 2022 Form 10-K.

### NOW 10-Q 2024-05-06 (delta 1.00, 2/2 sentences)

Accession 0001373715-24-000169 (prior 0001373715-23-000419).

**Strongest changed passage** (Jaccard 0.17):

> There have been no material changes to the Company’s risk factors since our Annual Report on Form 10-K.

**Closest prior match** (Jaccard 0.17):

> RISK FACTORS Investing in our securities involves risks.

### NOW 10-Q 2024-07-25 (delta 1.00, 41/41 sentences)

Accession 0001373715-24-000272 (prior 0001373715-24-000169).

**Strongest changed passage** (Jaccard 0.02):

> Techniques used to sabotage or to obtain unauthorized access to systems are constantly evolving and may go undetected until a successful attack occurs.

**Closest prior match** (Jaccard 0.02):

> RISK FACTORS The Company’s business, financial condition, results of operations and stock price can be affected by a number of factors, whether currently known or unknown, including those described in Part I, Item 1A of our Annual Report on Form 10-K for the year ended December 31, 2023, under the section “Risk Factors.” When any one or more of these risks materialize from time to time, the Company’s business, financial condition, results of operations and stock price can be materially and adversely affected.

## Verification (baseline anchor)

The baseline selection panel reproduces the published v2 train
numbers EXACTLY under the single-backtest estimator
(`r2_calibrate.eval_panel`, rebalance_every=5, min hold 10):
net10 +57.77%, Sharpe 0.931, 120 trades, turnover 0.0166/day, WF
median Sharpe 0.604, 4/4 folds positive. The +53.86% in the table
above is the cohort-blend (H=5) estimator frozen in the design doc,
applied identically to both arms — the 3.9pp gap vs the published
number is the cohort-averaging effect, not an implementation error.
The comparison (experiment minus baseline) is unaffected: both arms
run the same machinery on the same days.

## Post-hoc diagnostics (NOT part of the frozen test)

Computed after the headline, to understand the mechanism. They do
not change the judgment above. Two cuts were tried; neither is
significant on its own.

- Delta IC, 10-K filings only (269 known filings, YoY comparison —
  the actual Lazy Prices design): mean IC -0.0219, NW t -1.59
  (n=611 days). Direction matches the literature (changers
  underperform), but the evidence is weak.
- Delta IC, filings with >= 10 sentences on BOTH sides (260 known
  filings, i.e. excluding stub priors and stub currents): mean IC
  +0.0351, NW t +1.78 (n=497 days). Still positive — the stub
  pattern alone does not explain the headline's positive delta IC.

Reading: the headline's positive delta IC (+0.0407) is driven by
10-Q comparisons (3-month gaps), while the YoY 10-K comparison
points the other way but weakly. A future, SEPARATELY registered
experiment could test the 10-K-only version (with its own frozen
hypothesis and a minimum-sentence rule); this experiment's verdict
stands on its frozen test.
