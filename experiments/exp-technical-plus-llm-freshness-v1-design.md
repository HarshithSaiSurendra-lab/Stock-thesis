# exp-technical-plus-llm-freshness-v1 — design (registered BEFORE scoring)

Registered 2026-11-21. Authorized by the user 2026-11-21 ("yes run the
freshness test"). New registered experiment, not an amendment to
exp-technical-plus-llm-v1.

## Hypothesis

Phase 2 (exp-technical-plus-llm-v1) FAILED all 3 sub-gates on the frozen
holdout (2026-08-18..2026-11-13): v4+LLM-stale net −4.71% @10bps vs v4
base −4.33%. Train improved markedly (v4+LLM −15.55% vs base −25.86%),
so the LLM leg carries real in-sample signal that did not carry
out-of-sample. Prime suspect, recorded in the Phase 2 results: STALENESS
— holdout decisions used the latest pre-holdout filing carried forward
up to ~3 months, because no holdout-window filing was scored.

Hypothesis: scoring the holdout-window filings fresh (same models, same
combination rule, same prompts/excerpts) restores the LLM edge on the
frozen holdout — the fresh-LLM variant beats the v4 technical base net
of costs where the stale-LLM variant did not.

## What changes vs Phase 2 (and what does NOT)

CHANGES (exactly these):
1. Scoring window: filings dated 2026-08-18..2026-11-13 (the 130
   holdout-window records in dataset-edgar-point-in-time-v1), restricted
   to the in-scope set (current on >= 1 active holdout day for their own
   symbol — 75 filings, same in-scope rule as Phase 2, applied to
   holdout days instead of train days).
2. Current-filing series: union of in-scope train filings (Phase 2's
   1,166, scores already paid) + in-scope holdout filings (75, scored
   fresh here). Train days are therefore IDENTICAL to Phase 2 (verified
   in the driver: union == stale on train days); only holdout days can
   change, where the current filing may now be a fresh holdout-window
   filing instead of the latest pre-holdout filing carried forward.
3. Cost estimation at ~2.9 chars/token (Phase 2 measured rate), NOT
   chars/4. The chars/4 estimate breached the Phase 2 Sonnet sub-budget
   by $0.5576. `llm_sentiment._estimate_tokens` is fixed to 2.9 in the
   same commit as this registration; the driver uses its own 2.9-based
   estimator for subset selection and budget guards.

DOES NOT CHANGE: models (Haiku 5.5 screen, Sonnet 5.5 adjudicate,
Fable 5.2 audit-only, no ChatGPT leg), prompts (PROMPT_VERSION
llm-sentiment-v2), excerpts (same dataset, same trim), combination rule
(LLM adds at most +/-1.0, only where technical score > 0, only when
agreement >= 0.7 and both confidences >= 0.6, sign disagreement -> 0),
backtest parameters (top-5, max 5 positions, daily rebalance, 1-bar lag,
$1,000 initial capital, 10/20bps costs), holdout dates (frozen
2026-08-18..2026-11-13), or the gate structure (SG1/SG2/SG3, same
definitions as Phase 2).

## Sonnet adjudication (same rule as Phase 2, holdout days)

Candidates: in-scope holdout filings with a Haiku score and Haiku
confidence >= 0.6. Ranked by decision-relevance triple count on HOLDOUT
days descending, tie-break Haiku confidence ascending, then filing_date
ascending. Taken in rank order while the cumulative worst-case estimated
Sonnet cost (2.9 chars/token, 1,000-token max output) stays within the
Sonnet sub-budget. Where Sonnet does not adjudicate, the LLM
contribution is 0 (no second opinion, no adjustment).

Measured scope: all 75 in-scope holdout filings are near-threshold
(868 relevance triples). Top-10 by relevance cover 216 active holdout
cells; at Phase 2's pass rate (58/104 = 56%) ~120 applied cells are
expected — comparable to the stale variant's 126 applied holdout cells.
The test therefore has comparable coverage to the stale leg it is
being compared against.

## Spend (hard caps — no new cash, no new funds)

- Already spent in Nov 2026 before this run: $8.888873 (ledger
  $8.870839 + $0.018034 off-ledger Haiku validation).
- Anthropic balance before this run: $11.77 − $8.888873 = $2.881127.
- Monthly cap $10.00 -> headroom $1.111127.
- Run budget: $1.00 of NEW spend (cumulative cap $9.888873). The
  provider instances get run_cap_usd=1.00, monthly_cap_usd=10.00.
- Haiku: all 75 in-scope holdout filings. Worst-case estimate (2.9
  chars/token, 1,000-token output): $0.2232. Expected actual ~$0.20.
- Sonnet sub-budget: $0.75 -> top-10 filings by relevance.
  Worst-case estimate: $0.7316. Expected actual ~$0.65.
- Total worst-case $0.9548 <= $1.00 run budget. Expected actual ~$0.84;
  expected monthly total $9.73 < $10.00. A top-11 subset would total
  $0.9977 worst-case (margin $0.0023 — too tight) and top-12 $1.0770
  (over budget); 10 is the largest subset with a real margin.
- If the design could not fit, the run would STOP, not add funds. It
  fits; proceed.

## Gate (same structure as Phase 2; headline pair v4 vs v4+LLM-fresh)

PASS iff ALL THREE hold on the frozen holdout:
- SG1: holdout net return @10bps, v4+LLM-fresh > v4 base (−4.33%).
- SG2: holdout net return @20bps, v4+LLM-fresh > v4 base (−8.45%).
- SG3: holdout net return @10bps, v4+LLM-fresh > 0.
Any sub-gate failing = FAIL: variant dropped per the kill rule, never
rescued by raising risk. Supporting pairs (v1/v2/v3 +/- LLM-fresh) are
diagnostics, not gates. Fresh-vs-stale deltas (net10/net20, applied
cells, alpha vs SPY) are reported prominently — the experiment's
question is whether freshness closes the stale gap, even if the
absolute gate fails.

Diagnostics (not gates): Sharpe, max drawdown, Deflated Sharpe with
n_trials=26 (18 from Phase 2 + 8 for the 4 fresh variants x 2 costs),
alpha/beta vs SPY, information ratio, cost drag, trade counts.

## Holdout discipline (read before scoring)

- The holdout stays frozen (2026-08-18..2026-11-13, 63 trading days).
  NO parameter is tuned on holdout outcomes. The combination rule,
  Sonnet subset rule, and budgets are fixed in this document BEFORE
  any holdout-window filing is scored.
- The Sonnet subset ranking uses holdout-day TECHNICAL relevance
  (which filings are near the top-5 cutoff on holdout days). That is
  the same budget-allocation mechanism Phase 2 used on train days; it
  selects WHERE the limited Sonnet budget is spent, using technical
  scores only — it does not look at holdout returns or tune any
  decision parameter. Recorded here so the choice is auditable.
- Train results must reproduce Phase 2 exactly (same panels on train
  days). The driver asserts this: train-window returns for the fresh
  variant must equal the stale variant's train returns; a mismatch
  aborts the run before the gate is read.

## Limitations (recorded before the run)

- Only the top-10 most decision-relevant holdout filings get Sonnet
  adjudication (budget). The other 65 in-scope holdout filings are
  Haiku-only and contribute 0 (no second opinion). Coverage is
  ~120/536 active holdout cells where the current filing is fresh.
- Agreement is within one model family (Haiku + Sonnet), same caveat
  as Phase 2 — not independent-model (Claude + ChatGPT) agreement.
- Holdout is 63 trading days; a single quarter of filings. A pass
  here is evidence freshness matters, not proof of a tradeable edge.
- Backtest only. No paper trading of any variant; ALLOW_LIVE stays 0;
  no live money.
