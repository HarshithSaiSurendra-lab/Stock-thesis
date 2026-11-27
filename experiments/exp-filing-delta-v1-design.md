# exp-filing-delta-v1 — design (registered BEFORE looking at any return)

Registered 2026-11-26 (research branch; train period only). Written
before any return, correlation, or backtest number was computed for
this experiment. No new LLM calls: reuses the existing Haiku 5.5
scores (1,166 in-scope train filings). $0 new spend.

## Primary hypothesis

Newly changed language in a filing's Risk Factors (Item 1A)
predicts subsequent sector-relative returns — the Lazy Prices
premise (Cohen, Malloy & Nguyen, NBER w25084): firms that change
their disclosures a lot ("changers") subsequently underperform,
because managers rewrite risk language when conditions are
deteriorating, while unchanged ("lazy") disclosures accompany
steadier outcomes.

DIRECTION (frozen): the new-language fraction is NEGATIVELY
associated with subsequent 20-trading-day sector-relative returns.
A positive association is a failure of the hypothesis, not a reason
to flip the tilt — the tilt direction below is fixed by this
hypothesis, not by the data.

## What is new here vs existing work

- H3 (lazy-veto, registered, passed the frozen holdout +4.25%):
  document-level Jaccard similarity of Risk Factors token sets vs the
  prior same-form filing; missing risk text treated as similarity 1.0
  (no change); bottom-quintile changers VETOED from v2/v3 longs.
- C6 (textual-delta rank, train +48.97%): per-filing YoY deltas on
  the FULL excerpt text (Jaccard, length ratio, tone delta),
  combined as rank(tone_delta) + rank(jaccard) - rank(len_ratio).
- THIS experiment: a SENTENCE-level, DIRECTIONAL feature on the Risk
  Factors section only — the fraction of today's risk sentences with
  no close match in the prior comparable filing — used as a RANKING
  TILT on the Haiku score (not a veto, not a standalone rank), with
  passage-level evidence (strongest changed passage + its closest
  prior match) so a human can judge whether it detects meaningful
  change or formatting noise. Jaccard is a symmetric whole-document
  overlap; this feature is sentence-granular and answers "how much of
  what the firm says about risk today did it not say last time?".

## Feature definition (frozen)

For each filing F (ticker T, form type 10-K or 10-Q, filing_date d):

- PRIOR: the most recent prior filing for the same ticker with the
  SAME form type and filing_date strictly before d, drawn from the
  full v1 excerpt dataset (2,108 records, 2023-01-05..2026-11-12),
  regardless of whether the prior filing itself was Haiku-scored.
  No fallback: if the immediately prior same-form filing has a
  missing/empty risk section, the feature is `unknown` (do not skip
  to an earlier filing — that would silently change the comparison
  window from one period to two). Amendments are excluded at dataset
  build (manifest), so no amendment handling is needed.
- SENTENCES: the filing's `risk_text` split into sentences by a
  deterministic regex on sentence terminators (`.`, `!`, `?`
  followed by whitespace), lowercased, whitespace-collapsed,
  leading list numbering/bullets stripped. A sentence is ELIGIBLE
  if it contains >= 4 word tokens (words = `[a-z]{3,}`); shorter
  strings are headings/fragments and are excluded from numerator
  and denominator.
- NEAREST MATCH: for each eligible current sentence, the maximum
  token-set Jaccard similarity against ANY eligible sentence of the
  prior filing's `risk_text`. "Materially new" = max Jaccard < 0.50
  (frozen: the Jaccard midpoint — a majority of tokens differ).
  Order-independent: a paragraph moved without substantive edits
  still matches its prior counterpart (Jaccard ~1.0) and does NOT
  count as new language.
- FEATURE: delta(F) = (# eligible current sentences with max
  Jaccard < 0.50) / (# eligible current sentences). Range [0, 1];
  high = much new risk language.
- EVIDENCE: for each filing, save the strongest changed passage =
  the eligible sentence with the LOWEST max Jaccard (most novel),
  alongside its closest prior match (the prior sentence attaining
  that max Jaccard) and the Jaccard value, plus n_sentences, n_new,
  prior accession, prior filing_date.
- UNKNOWN: delta = `unknown` (None) if the current `risk_text` is
  missing/empty, OR no prior same-form filing exists in the dataset,
  OR the prior filing's `risk_text` is missing/empty. Unknown is
  excluded from the feature (tilt 0, see below) and counted in
  exclusions. This differs deliberately from H3, which treats
  missing risk text as similarity 1.0: for a ranking tilt, "no
  evidence of change" must be neutral, not "no change" — treating
  missing as unchanged would systematically boost names with
  missing risk sections.

## The comparison (frozen)

One comparison, on identical issuers, dates, and simulated entry
times:

- BASELINE arm: the registered v2 ranking — Haiku 5.5 score of the
  latest in-scope filing (filing date <= day, carried forward),
  selection = every name with score >= the day's 5th-highest
  positive score, equal weight.
- EXPERIMENT arm: the same ranking PLUS this single feature:
  score_adj = haiku_score - TILT * (2 * delta_pct_rank - 1),
  TILT = 0.25 score points (frozen). delta_pct_rank is the
  cross-sectional percentile rank of delta among names with a KNOWN
  delta that day (0..1); unknown delta -> tilt 0 (neutral). The
  minus sign implements the frozen directional hypothesis (high
  new-language fraction is penalized). Selection = the identical
  v2 rule applied to score_adj.
- TILT SCALE (recorded, not tuned): Haiku scores span [-0.7, +0.85]
  in 0.05 steps (35 distinct values; mode 0.6, n=240). A +/-0.25
  tilt is material — it can move a name across the day's cutoff —
  but cannot single-handedly promote a deeply negative score into
  the book (e.g. -0.5 + 0.25 = -0.25, still below any positive
  cutoff). The amplitude was chosen on this scale argument alone,
  before any return was computed.
- EXECUTION (both arms, identical): the repo's cohort machinery
  (`r2_panels.cohort_returns`): overlapping 1/H cohorts, H = 5
  (weekly rebalance), minimum hold 10 days, 1-bar lag (engine —
  signals known at close of day t act at day t+1), long-only,
  max_positions 60. Costs 0 / 10 / 20 bps. Entry times are identical
  by construction: both arms run the same machinery on the same day
  index; only the selection panel differs.

## Horizon, universe, costs (reused from the baseline)

- RETURN HORIZON (primary): 20 trading days — the baseline's IC
  horizon (v2 design: Haiku IC +0.060 @20d). Sector-relative: the
  stock's 20d forward return minus the equal-weighted mean 20d
  forward return of same-sector peers in the universe (excluding
  the stock itself), sectors from `sector_map.py`. (Sector ETFs are
  NOT in the cached bars — only SPY is — so the peer-mean
  definition is used; it needs no extra data.)
- ELIGIBLE UNIVERSE: the 133-symbol `config.DEFAULT_SEED_SYMBOLS`
  universe; filings = the 1,166 in-scope Haiku-scored train filings
  (same scope as v2). Symbols without a sector mapping are excluded
  from the IC sample only (reported in coverage); they remain in the
  portfolio backtest, which uses raw prices like v2.
- EVALUATION SET: train period ONLY — decision days 2024-01-01
  through 2026-08-17 (RESEARCH_START..TRAIN_END_DATE). The frozen
  holdout (2026-08-18..2026-11-13) and the January validation
  window (2026-11-16..2026-12-31) are NOT used as the evaluation
  set. BOUNDARY RULE (frozen): an IC event is included only if its
  full 20-session forward window completes on or before
  2026-08-17 — i.e. filing_date + 20 trading sessions <=
  2026-08-17. Events after that are excluded from the IC (counted
  in exclusions). The portfolio backtest slices prices and both
  selection panels to train days only, so no holdout price enters
  either arm.
- TIMING / POINT-IN-TIME: a filing is known at the close of its
  filing_date (score and delta panels carry the latest filing with
  filing_date <= day); the engine's 1-bar lag makes the first
  eligible simulated action the next session's open. Earliest
  eligible decision time = filing_date (close); first action = next
  trading session. Per observation the runner saves: source
  accession, filing_date (public timestamp), earliest eligible
  decision time, and exclusion reason where applicable.

## Primary outcome (frozen)

PRIMARY: the change in Spearman rank correlation (IC) at the 20d
sector-relative horizon — the DAILY cross-sectional Spearman IC
(the repo's established `rank_ic_and_spread` convention: per day,
rank the panel values against the forward returns across symbols,
require >= 20 symbols with both values that day, average across
days) of score_adj vs 20d sector-relative returns, MINUS the same
daily IC of the Haiku score vs the same returns, on the SAME days
and symbols. The panels are the carried-forward panels (Haiku 0.0
where a symbol has no scored filing yet; delta NaN where unknown,
so unknown-delta pairs drop out of the delta IC pairwise and carry
tilt 0 in score_adj). The forward window must complete by
2026-08-17 (masked in the runner: a day t is usable only if t+21
<= last train day, because `fwd_returns(20)` spans close(t+1) to
close(t+21)). A negative IC for delta itself (delta vs returns)
supports the hypothesis.
SECONDARY (economic): portfolio net return after costs (10bps
primary, 0/20bps shown), turnover, max drawdown, coverage — from
the cohort backtest on train days, baseline vs experiment.

## Sensitivities (frozen, run regardless of the headline)

1. REMOVE THE LARGEST CONTRIBUTOR: drop the single issuer with the
   largest absolute attribution to the experiment-minus-baseline
   daily return difference (attribution per issuer = sum over train
   days of (w_exp - w_base) * daily return), from BOTH arms'
   universes, and recompute the IC and the backtest.
2. DELAY ENTRY ONE ADDITIONAL SESSION: shift the experiment arm's
   selection by one extra day (signals act at t+2 instead of t+1)
   and recompute the backtest. (The IC is a ranking diagnostic
   anchored at the filing's first eligible session; it is reported
   unchanged, and this is documented, not hidden.)

## Uncertainty (frozen)

Repeated issuers and overlapping 20d windows make naive SEs too
small. Report:
- IC: mean daily cross-sectional Spearman IC with a Newey-West SE
  (lag 19, matching the 20-session overlap) and t-stat; PLUS an
  issuer-cluster bootstrap (resample tickers with replacement,
  500 draws, recompute the mean daily IC difference on the
  resampled (day, symbol) event set — the same pairs that enter
  the daily ICs) with a 95% CI for the IC change.
- Portfolio: a time-block bootstrap of the daily return-difference
  series (experiment minus baseline), block length 20 sessions,
  500 draws, 95% CI for the net-return difference at 10bps.

## Coverage and exclusions (reported)

Input dates, n filings in scope, n with known delta, n unknown
(by reason: current risk missing / no prior same-form filing /
prior risk missing), n excluded from the IC by the boundary rule,
n excluded for missing sector mapping, distinct issuers and event
dates in the IC sample, fraction of train days with >= 1 known
delta in the selection. Passage pairs: the results file shows the
top-5 highest-delta filings' strongest changed passage + closest
prior match so a human can judge meaningful change vs formatting
noise.

## End decision (tied to the evidence)

Exactly one of:
- DROP the feature — if the IC change is ~0 or of the wrong sign,
  or the portfolio difference is <= 0 after costs with no
  robustness in the sensitivities.
- COLLECT MORE INDEPENDENT DATA — if the direction is right but the
  uncertainty is too wide to judge (e.g. CI includes 0 and the
  point estimate is economically meaningful), or coverage is too
  thin (too few known-delta events).
- REGISTER FOR A FUTURE PROSPECTIVE TEST — only if the IC change
  is in the hypothesized direction AND the portfolio difference is
  positive after costs AND both sensitivities keep the sign. A
  prospective test is a NEW registration; nothing here changes the
  live ranking, the daily runner, or any registered design.

## Limits (read before interpreting any pass)

- This tests the TRAIN period only (2024-01-01..2026-08-17). A
  train-only pass is candidacy for a prospective test, not proof.
  The frozen holdout and January window are untouched by this
  experiment by design.
- Knowledge-cutoff contamination applies exactly as for the Haiku
  scores: Haiku 5.5 ran Nov 2026 and may know outcomes of the
  filings it scored. The delta feature itself is deterministic
  text arithmetic ($0, no model) and is NOT contaminated — but the
  baseline it tilts IS. A pass could reflect the Haiku level, not
  the delta. The IC of delta ALONE (reported separately) is the
  uncontaminated read on the feature.
- The delta feature is computed from the same filings the Haiku
  score read. If Haiku's score already encodes "this filing changed
  a lot" (its rationale often mentions new risks), the tilt may
  double-count what the score already captures. The delta-alone IC
  and the incremental IC change are reported separately so this is
  visible.
- 10-Q risk sections are often short ("no material changes" or a
  cross-reference to the 10-K). Short sections make the fraction
  noisy (few sentences). The n_sentences distribution is reported
  with the results.

## Spend

$0.00 new scoring. Deterministic text arithmetic only. No API
calls anywhere in this experiment.
