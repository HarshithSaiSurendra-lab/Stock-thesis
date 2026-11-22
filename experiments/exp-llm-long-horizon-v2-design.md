# exp-llm-long-horizon-v2 — design (registered BEFORE scoring)

Registered 2026-11-22. Authorized by the user 2026-11-22 ~00:46 EST
("Yes, run the repo code") with priority PATTERN RECOGNITION FIRST, and
the sprint plan (calibrate cheap, register, confirm once). New registered
experiment, not an amendment to exp-technical-plus-llm-v1 or
exp-technical-plus-llm-freshness-v1.

## Hypothesis

The Haiku 5.5 score of a filing predicts forward returns at 20-126 day
horizons (train IC +0.060 @20d, +0.063 @126d, positive at every horizon
5/10/20/60/126d), matching the prompt's 3-12 month view. The engine's
1-day holding wasted that signal. Holding every name whose latest filing
scored at or above the day's 5th-highest positive score, equal-weighted,
weekly rebalanced with a 10-day minimum hold, beats the v4 technical
base on the frozen holdout net of costs.

## What the sprint found (train only — never the holdout)

1. **Cost/turnover decomposition (Part A):** v4 base train gross +12.93%,
   net10 -25.86%, cost drag 38.79pp, turnover 0.639/day, WF median Sharpe
   -0.936, 0/4 folds positive. Every prior variant loses on train too:
   the problem is weak gross edge AND turnover, not just costs.
2. **Technical features are anti-predictive in this window (Part B1):**
   at 1 day NOTHING predicts (composite IC -0.0025). At 60 days the
   composite itself is anti-predictive (IC -0.030, t -4.88); momentum
   (ret_63 IC -0.068, mom_126_21 -0.025 @20d) reverses. The window is
   mean-reverting, not trending. Strongest single feature: realized
   volatility rvol_20 (IC +0.047/+0.064/+0.083 @10/20/60d, Q-spread
   +5.59pp @60d) — and v4's risk filter EXCLUDES exactly those names.
3. **LLM Haiku score predicts (Part B2):** IC +0.049/+0.024/+0.060/
   +0.036/+0.063 @5/10/20/60/126d; Q-spread +0.92pp @20d, +3.26pp @126d.
   Score CHANGES do not predict (delta IC ~0); the LEVEL does. The
   combined Haiku+Sonnet subset (n=104, decision-relevance selected) is
   negative at all horizons — a biased subset, not evidence Sonnet hurts.
4. **Textual deltas predict at $0 (Part B3):** filing similarity
   (Jaccard IC +0.098 @20d), filing length growth (IC -0.108 @60d),
   tone improvement (IC +0.094 @20d, +0.089 @60d).
5. **Calibration (train, $0):** C2 LLM long-horizon (top-5 by score,
   rebalance 5, min hold 10) train net10 +153.83%, WF 1.92, 3/4 folds.
   Runner-ups: C6 textual-delta rank +48.97% (point-in-time rebuilt;
   the z-scored version's +137.79% was inflated by full-sample
   z-score leakage — caught and corrected in verification), C1c
   reversion+RSI +64.06%.
6. **Verification:** machinery reproduces exp-technical-v2 train
   net10 -14.01% (recorded -14.0%) exactly. MU entry verified
   point-in-time clean (10-Q filed 2025-12-18, Haiku +0.85, entry
   2025-12-23; raw +251.71% to train end). Beta -0.10 vs SPY verified
   two ways (engine returns and weights x forward returns agree).
7. **Fable 5.2 audit (independent logic review, $0.156336):** flagged
   (a) LLM knowledge-cutoff contamination — Haiku 5.5 was run Nov 2026
   and may know outcomes of the filings it scored; point-in-time filing
   dates do not fix this; the gate cannot fully remove it (disclosed
   as a limit below); (b) the tie-break problem — Haiku scores are
   quantized, so ~23 symbols tie at the 5th-place score on 131/132
   rebalance days and an arbitrary sort key chooses the 5-name book;
   train results run +53% to +182% across defensible tie-break rules;
   verdict: "redesign first. Do not spend the holdout on this version."
   Single most important change: remove the tie-break entirely — hold
   every name tied at the cutoff score.
8. **Redesign (this experiment, train, $0):** hold every name with
   score >= the day's 5th-highest positive score, equal weight.
   Selection size mean 25.7 (min 5, max 40, median 27). Train net10
   +57.77% (gross +59.50%, net20 +56.06%), WF median Sharpe 0.604,
   4/4 folds positive, 120 trades, turnover 0.017/day (cost drag only
   1.73pp @10bps). The pattern survives tie-break removal in
   diversified form; the concentrated +153.83% was inflated by the
   tie-break and by MU concentration.

## Design (registered)

- **Signal:** Haiku 5.5 score of each symbol's latest filing (filing
  date <= day, carried forward until the next filing). $0 — reuses
  Phase 2 scores (1,166 train filings) + freshness scores (75
  holdout-window filings). NO new API calls, NO Sonnet adjudication
  (Haiku-only by design: the combined subset is decision-relevance
  biased, and the pattern is in the Haiku level).
- **Selection:** each day, cutoff = the 5th-highest score among
  score > 0. Hold EVERY name with score >= cutoff, equal weight.
  No tie-break anywhere: the book is fully determined by the score.
- **Execution:** weekly rebalance (every 5 days), minimum hold 10
  days, 1-bar lag (engine), long-only, max_positions 60 (covers the
  max observed selection of 40), $1,000 initial capital.
- **Costs:** 10bps base, 20bps stress. **Holdout:** frozen final 63
  trading days 2026-08-18..2026-11-13. **Walk-forward:** n_splits 4
  (train diagnostics only).
- **Current-filing series:** union of in-scope train + in-scope
  holdout filings (same construction as the freshness run). The driver
  verifies the union series equals the stale series on train days, so
  the train reproduction is exact; only holdout days can change, where
  the current filing may now be a fresh holdout-window filing.
- **Gate (same structure as Phase 2 / freshness):** PASS iff (SG1)
  holdout net return @10bps > v4 base (-4.33%); (SG2) holdout net
  return @20bps > v4 base (-8.45%); (SG3) holdout net return @10bps
  > 0. Any sub-gate failing = FAIL: variant dropped per the kill
  rule. Supporting diagnostics (Sharpe, max DD, Deflated Sharpe with
  n_trials=75, alpha/beta vs SPY) are reported, not gated.

## Trial count (Deflated Sharpe)

n_trials = 75: 26 prior (freshness) + 36 calibration + 2 C6-rank
rebuild + 8 tie-break sensitivity (TB2-TB5 x 2 costs; TB1 duplicates
the calibration's C2 and is not double-counted) + 3 redesign
(0/10/20bps). The confirmation itself is the 76th evaluation of a
design on the holdout only in the sense that it scores the frozen
holdout once; the DSR uses n_trials=75 (all train trials before it).

## Spend

- This experiment: $0.00 new scoring (all scores already paid).
  Fable 5.2 audit: $0.156336 (3 calls, 4,042 input / 3,300 output
  tokens at $8/$40 per MTok).
- Cumulative Nov Anthropic: $9.824975 (ledger $9.806941 + off-ledger
  $0.018034 Nov 17 validation). Balance vs $11.77: ~$1.95.
  Monthly cap ($10) headroom: $0.175. The user's +$5 Anthropic
  top-up is UNVERIFIED (no balance endpoint; no confirmation it
  landed). OpenAI / GPT-6.1 Sol leg: no connector exists, adapter
  not built — pending, not run.

## Limits (read before interpreting any pass)

- **LLM knowledge-cutoff contamination (most serious):** Haiku 5.5
  was run in Nov 2026; its training knowledge may postdate the
  filings it scored, so a high score on a 2024-25 filing may reflect
  knowledge of the outcome, not reading skill. The frozen-holdout
  gate does NOT remove this: holdout filings were scored by the same
  model, whose knowledge likely extends into the holdout window. A
  pass is evidence the scores carry signal beyond the technical base,
  not proof of a tradeable, contamination-free edge. A clean test
  would need a model whose cutoff predates the filings.
- Universe is the current 133-symbol config list: not
  survivorship-free (affects all variants equally).
- Filing dates (not EDGAR acceptance timestamps) with a 1-bar lag;
  after-close filings are effectively 1 day late.
- Holdout is 63 trading days; a pass is evidence, not proof.
- Train WF Sharpe 0.604 is modest; the diversified book dilutes the
  concentrated MU/AMD effect that drove the +153.83% figure.
- Backtest only; no paper trading, no live money, ALLOW_LIVE stays 0.
