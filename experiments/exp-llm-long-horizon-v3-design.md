# exp-llm-long-horizon-v3 — design (registered BEFORE scoring)

Registered 2026-11-22. Follow-on to exp-llm-long-horizon-v2 (GATE PASS,
Nov 22, 2026). Authorized under the user's sprint approval (Nov 22,
~00:46 EST, "Yes, run the repo code", priority PATTERN RECOGNITION
FIRST) and the parent agent's follow-up task to test the three
repo-derived hypotheses natively (no repo code cloned, installed, or
run — all implementations are stock-thesis code only).

## Hypothesis

Adding a deterministic technical veto — require the technical composite
score > 0 (some technical support) — to the passing v2 LLM panel
improves the frozen-holdout return net of costs. The veto is a filter,
not a tilt: a name must pass BOTH the LLM score cutoff AND the
technical screen to be held.

## What the follow-on train tests found (train only, $0)

All three repo-derived hypotheses were tested natively on train:

1. **H1 — Haiku bull/bear debate as adjudication: BLOCKED on funding,
   not tested.** A debate needs 3 fresh Haiku calls per filing (bull +
   bear + adjudicate). Measured single-pass Haiku cost is $0.002831 /
   filing, so a debate costs ~$0.009908 / filing (3.5x planning
   estimate). A 100-filing subsample costs ~$0.99 vs $0.175 monthly
   cap headroom; at most ~17 filings fit. Needs the user's +$5
   Anthropic top-up verified (still UNVERIFIED) before any debate
   scoring. No calls were made.
2. **H2a — LLM as veto on the technical panel: WEAK (already tested
   in calibration as C3, cited not re-run).** v4 panel + LLM veto:
   train net10 +9.10%, WF -0.28, 1/4 folds, vs v4 long-hold control
   +7.02%. The veto adds only +2.08pp. Not registered.
3. **H2b — Technical veto on the passing v2 design: STRONG on train
   (this experiment).** v2 selection + composite > 0 veto: train
   gross +110.20%, net10 +85.48%, net20 +63.64%, WF median Sharpe
   0.767 @10bps, 3/4 folds @10bps (4/4 @0bps, 2/4 @20bps), 444
   trades, turnover 0.190/day. vs v2 no-veto: gross +59.50%, net10
   +57.77%, net20 +56.06%, WF 0.604, 4/4, 120 trades, turnover
   0.017/day. The veto roughly doubles the gross return but at 11x
   the turnover: cost drag 24.72pp @10bps (vs v2's 1.73pp), and the
   advantage over v2 shrinks fast with costs (net20 only +7.58pp
   over v2). The alternative veto (close > sma_50) was tested and
   NOT selected: train net10 +22.04%, worse than v2 no-veto.
4. **H3 — PEAD + MA alignment / pullback: NULL on train.** PEAD proxy
   (filing-date abnormal return vs forward returns, n=1,446 filings):
   IC +0.0078/+0.0077/+0.0035 @5/10/20d, Q-spread +0.50pp @20d —
   no drift, no reversal. MA alignment (MA5>MA10>MA20): spread IC
   -0.0162/-0.0103/-0.0028 @5/10/20d (t -1.94 at 5d, not
   significant), Q-spread -0.02pp @20d — slightly negative, consistent
   with the mean-reversion regime, but too weak to use. Pullback
   (aligned + dipped near MA20, still above): -0.33pp/-0.48pp/-0.62pp
   vs the rest @5/10/20d — pullbacks do WORSE, the opposite of the
   intended pattern. The pre-set bar (|IC| >= 0.03 @20d or 20d
   Q-spread >= 1%) was NOT cleared, so no alignment backtest was run.
   PEAD, alignment, and pullback are all dropped as patterns in this
   window.

## Design (registered)

- **Signal:** same as v2 — Haiku 5.5 score of each symbol's latest
  filing (filing date <= day, carried forward). $0 — reuses Phase 2
  scores (1,166 train filings) + freshness scores (75 holdout-window
  filings). NO new API calls, NO Sonnet adjudication (Haiku-only).
- **Selection:** each day, cutoff = the 5th-highest score among
  score > 0. Hold every name with score >= cutoff AND v1_composite
  > 0, equal weight. No tie-break (same as v2); the veto is a
  deterministic boolean screen on the existing composite panel.
- **Execution:** weekly rebalance (every 5 days), minimum hold 10
  days, 1-bar lag (engine), long-only, max_positions 60, $1,000
  initial capital.
- **Costs:** 10bps base, 20bps stress. **Holdout:** frozen final 63
  trading days 2026-08-18..2026-11-13. **Walk-forward:** n_splits 4
  (train diagnostics only).
- **Current-filing series:** union of in-scope train + in-scope
  holdout filings (same construction as v2 / freshness). The driver
  verifies the union series equals the stale series on train days
  and that the train reproduction matches H2b's +85.48% net10 within
  0.2pp before the gate is read.
- **Gate (same structure as v2):** PASS iff (SG1) holdout net return
  @10bps > v4 base (-4.33%); (SG2) holdout net return @20bps > v4
  base (-8.45%); (SG3) holdout net return @10bps > 0. Any sub-gate
  failing = FAIL: variant dropped per the kill rule. If v3 fails,
  v2 (already PASSED) remains the best confirmed design.

## Trial count (Deflated Sharpe)

n_trials = 81: 75 (v2) + 6 H2b veto evaluations (2 veto variants x 3
costs). The H3 IC diagnostics are not portfolio trials and are not
counted. H1 made no calls.

## Spend

- This experiment: $0.00 new scoring (all scores already paid).
- Cumulative Nov Anthropic: $9.824975 (ledger $9.806941 + off-ledger
  $0.018034). Balance vs $11.77: ~$1.95. Monthly cap ($10)
  headroom: $0.175. The user's +$5 Anthropic top-up is UNVERIFIED.
  OpenAI / GPT-6.1 Sol leg: no connector exists, adapter not built
  — pending, not run.

## Limits (read before interpreting any pass)

- **Second holdout evaluation on the same frozen window.** v2
  already passed on this holdout. A v3 pass is NOT independent
  evidence: the two designs share the same signal, the same scores,
  and the same 63 days. The family-wise false-positive risk grows
  with each holdout look; the Deflated Sharpe (n_trials=81) accounts
  for train trials, not for multiple holdout evaluations. Read v3's
  verdict alongside v2's, not as a fresh independent test.
- **Cost sensitivity.** The veto's edge depends on turnover
  (0.190/day vs v2's 0.017/day). At 20bps the train advantage over
  v2 is only +7.58pp and only 2/4 WF folds are positive. A pass at
  10bps with a weak 20bps result would mean the design is fragile
  to transaction costs.
- **LLM knowledge-cutoff contamination (same as v2):** Haiku 5.5 was
  run Nov 2026 and may know outcomes of the filings it scored; the
  gate cannot remove it.
- Universe is the current 133-symbol config list: not
  survivorship-free (affects all variants equally).
- Filing dates (not EDGAR acceptance timestamps) with a 1-bar lag.
- Holdout is 63 trading days; a pass is evidence, not proof.
- Backtest only; no paper trading, no live money, ALLOW_LIVE stays 0.
