# exp-equibles-pilot-returns — design (registered BEFORE running)

Registered 2026-11-22. This design is written BEFORE any return is
computed. No post-call return has been read under this experiment yet.

## Objective

The pilot scoring (`exp-equibles-pilot-scoring`, 21 earnings calls,
Q&A text only, Haiku 5.5, prompt `equibles-pilot-qa-v1`) produced
scores but no return test: the scores are a diagnostic, not a signal.
This experiment pairs those frozen scores with 20-trading-day
post-call returns and asks the narrow question: do the Q&A scores
predict returns? It is a pilot diagnostic (n ≈ 16–21), NOT a gate
pass and NOT a backtest of a tradeable strategy.

## Inputs (frozen)

- Scores: `experiments/exp-equibles-pilot-scoring.json`, `per_call`
  entries (ticker, fiscal year/quarter, call date, score, confidence,
  evasion, analyst tone, precomputed `qoq_score_delta`). Scores are
  NOT recomputed and NOT re-scored under this experiment.
- Prices: Alpaca daily bars, adjustment = "all" (split + dividend
  adjusted closes), fetched fresh for ABBV, ABT, AMD, AMGN, AMT, AMZN
  and SPY. The repo's backtest cache (`.cache/backtest_data/`) is
  raw-adjusted and ends 2026-11-13, so it is NOT used for closes here;
  fresh adjusted bars are fetched with the repo's Alpaca path and saved
  under `experiments/equibles-pilot-raw/` for auditability. No other
  price source is used.

## Return definition (fixed before running)

For each call, let T be the call date (the calendar date of the
earnings call, as recorded in the scoring file).

- **R (raw 20-day return)** = close(T+20) / close(T) − 1, where T+20
  is the 20th trading day after T, using adjusted closes.
- **Excess return** = R − R_SPY over the same window, where R_SPY is
  SPY's adjusted-close return from T to T+20.
- **After-close choice:** if the call was held after market close, T
  is STILL the call date (not T+1). This is a deliberate, stated
  choice: it is the conservative convention for a pilot (it can only
  dilute, not inflate, the post-call signal, since part of the
  reaction may already be in close(T)).
- **No partial windows:** a call is included ONLY if its T+20 is a
  trading day present in the fetched price data (i.e. T+20 ≤ the last
  available trading day). If T+20 falls after the last available
  trading day, the call is EXCLUDED and reported as such. No
  truncated or estimated window is used.
- **Missing data:** if a ticker's price series is missing or
  incomplete for a call's window, that call is excluded and reported.
  No prices or returns are ever invented or interpolated.

## Metrics (fixed before running)

For each of the four signals — Q&A score, QoQ score delta
(`qoq_score_delta`, the bleak-tone-change proxy), evasion, analyst
tone — against BOTH the 20-day raw return and the 20-day excess
return:

- Pearson correlation (IC) with t-statistic and two-sided p-value
  (t = r·√((n−2)/(1−r²)), df = n−2).
- Spearman rank correlation (IC) with the same t-statistic and
  two-sided p-value on the ranks.
- The QoQ-delta IC uses only calls with a non-null
  `qoq_score_delta` (the first scored quarter per ticker has none),
  so its n is smaller; report its n separately.

Plus, for the Q&A score only:

- **Median-split long/short spread:** above-median-score calls vs
  below-median-score calls, equal weight, mean raw return and mean
  excess return for each side and the spread (above − below). With an
  even n the split is n/2 vs n/2; with an odd n the median call is
  dropped from the split and reported.

## Evidence bar (fixed before running)

With n ≈ 16 this is a pilot diagnostic, not a gate pass. There is no
PASS/FAIL gate and no DROP/SCALE verdict on the earnings-call leg
from this experiment alone.

- Call the Q&A score **"suggestive"** only if |IC| ≥ 0.3 with
  p < 0.10 on the score (Pearson or Spearman, raw or excess).
  Anything weaker is **"no evidence"**.
- Report every IC, t, and p honestly, including the ones that fail.
  Do not cherry-pick the best metric.
- No trading claim either way: even a "suggestive" result only
  justifies registering a wider follow-up experiment (more tickers,
  more quarters); it does not justify paper trading the scores.

## Honest limits (stated before running)

- n ≈ 16 after exclusions is tiny: a single outlier can move an IC
  by ~0.2. Confidence intervals on IC are wide by construction.
- Four signals × two return definitions × two correlation measures =
  16 ICs on one small sample: no formal multiple-testing correction
  is applied, but the evidence bar above is read against that
  multiplicity — a lone p < 0.10 among 16 tests is weak evidence.
- Scores are single-model (Haiku only), single-pass; model noise is
  in the signal (e.g. ABT's ±0.55 QoQ swings).
- Haiku 5.5 ran in November 2026 and may know these calls' outcomes
  (knowledge-cutoff contamination) — the same caveat as the filing
  scores. A positive IC here does NOT prove a live edge.
- The 20-day window is a single horizon chosen from the PEAD
  literature (PEAD.txt blend ~104 bps gross over 20 days), not tuned
  on this sample. No other horizon is tested under this design.
- This experiment does NOT touch the frozen holdout and does NOT
  evaluate any of v2/v3/H3/C1c.

## Cost

$0 expected. No LLM calls are needed: scores are frozen, prices come
from the existing Alpaca paper account (data reads only). No orders,
no live trading, ALLOW_LIVE stays 0.

## Outputs

- `experiments/exp-equibles-pilot-returns.json` — per-call returns +
  scores, the aggregate IC table, median-split spreads, n, exclusions.
- `experiments/exp-equibles-pilot-returns.md` — short summary.
- Registry entry in `experiments/registry.jsonl` and a row in the
  experiment log Google Sheet.
