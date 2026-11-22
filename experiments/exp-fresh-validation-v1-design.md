# exp-fresh-validation-v1 — design (registered BEFORE running)

Registered 2026-11-22. DRAFT ONLY — no EDGAR fetching, no backtests,
no new LLM scoring has been done under this design. Running it requires
the user's go-ahead (same as rounds 1/2) and the dataset refresh in
Step 0.

## Objective

Four designs have passed on the SAME frozen 63-day holdout
(2026-08-18..2026-11-13): exp-llm-long-horizon-v2, v3, H3
lazy-veto-v3, and H7 C1c reversion+RSI. Four passes on one window is
not four independent pieces of evidence — family-wise false-positive
risk grows with every holdout look, and the holdout is short enough
that a regime effect (e.g. H7's 9.22 Sharpe) can masquerade as an edge.
This experiment tests all four, with parameters FROZEN exactly as
registered in rounds 1/2, on a FRESH window of filings after Nov 13,
2026 that none of the four has ever been evaluated on. No retuning:
any parameter change, however small, invalidates the test and must be
registered as a new experiment instead.

## Designs under test (frozen rules — no changes)

Common execution for all four (as in rounds 1/2): weekly rebalance
(every 5 days), minimum hold 10 days, 1-bar lag (engine), long-only,
$1,000 initial capital. Costs: 0/10/20bps (10bps base, 20bps stress).
Prices: same bar panel construction as rounds 1/2 (`backtest.py` /
`r2_panels.py` inputs), extended to the fresh window.

### 1. exp-llm-long-horizon-v2
Source: `experiments/exp-llm-long-horizon-v2-design.md`,
results `experiments/results/exp-llm-long-horizon-v2.json`.
Signal: Haiku 5.5 score of each symbol's latest filing (filing date
<= day, carried forward until the next filing). Selection: each day,
cutoff = the 5th-highest score among score > 0; hold EVERY name with
score >= cutoff, equal weight. No tie-break anywhere. max_positions 60.
Frozen-holdout result: net10 +2.31%, net20 +2.10%, Sharpe 0.661,
DSR 0.731 (n_trials 75), alpha vs SPY +9.7%/yr, 43 trades,
turnover 0.033/day. Train net10 +57.77%.

### 2. exp-llm-long-horizon-v3
Source: `experiments/exp-llm-long-horizon-v3-design.md`,
results `experiments/results/exp-llm-long-horizon-v3.json`.
v2 selection PLUS the deterministic technical veto v1_composite > 0
(a name must pass BOTH the LLM cutoff AND the technical screen).
max_positions 60. Frozen-holdout result: net10 +4.74%, net20 +3.44%,
Sharpe 0.91, DSR 0.931 (n_trials 81), alpha +19.0%/yr, 55 trades,
turnover 0.200/day. Train net10 +85.48%.

### 3. H3 lazy-veto-v3 (exp-r2-h3-lazy-veto)
Source: `experiments/exp-llm-long-horizon-r2-design.md` (R2-H3),
results `experiments/results/exp-r2-h3-lazy-veto-v3-confirmation.json`.
v3 PLUS the Lazy-Prices veto: exclude YoY big changers of Item 1A
Risk Factors — bottom-quintile similarity cross-sectionally each day,
same form type (10-K vs prior 10-K, 10-Q vs prior 10-Q), computed
deterministically from the EDGAR excerpts; missing Risk Factors text
in either filing = no change (similarity 1.0), no veto. Frozen-holdout
result: net10 +4.25%, net20 +2.97%, Sharpe 0.788, DSR 0.857
(n_trials 126), alpha +17.1%/yr, 54 trades, turnover 0.198/day.
Train net10 +89.52%.
**Known contradiction (carried into the gate):** H3's own diagnostic
found Item 1A similarity IC @20d = −0.0177 (t −4.78) — changers
predict HIGHER returns, the opposite of the veto's premise. See SG4
below.

### 4. H7 C1c reversion+RSI (exp-r2-h7-runnerup)
Source: `experiments/exp-llm-long-horizon-r2-design.md` (R2-H7),
results `experiments/results/exp-r2-h7-c1c-reversion-rsi-confirmation.json`,
panel code `r2_panels.py::c1c_panel`.
Score = z(rvol_20) − z(ret_63) − z(drawdown_63) − z(mom_126_21) −
z(rsi_14), cross-sectional z-scores per day; engine rank-selects
top-5 (max_positions 5). No LLM input. Frozen-holdout result:
net10 +42.54%, net20 +41.09%, Sharpe 9.22, DSR 1.0 (n_trials 129),
alpha +148.3%/yr, 31 trades, turnover 0.162/day.
Train net10 +64.06%, WF median Sharpe 1.178 (3/4 folds).
**Sanity flag:** holdout Sharpe 9.22 vs train WF 1.178 on a 63-day
window is a short-window/regime effect, not proof of a tradeable
edge. See the audit rule below.

## Universe

Same 133-symbol config list (`config.DEFAULT_SEED_SYMBOLS`) as every
prior run. Not survivorship-free — same limitation as before, affects
all designs equally.

## Step 0 — dataset refresh (prerequisite, NOT done now)

The point-in-time EDGAR dataset ends Nov 12/13, 2026
(`dataset-edgar-point-in-time-v1`: 2,108 records, 133 tickers,
filing dates 2023-01-05..2026-11-12; 10-K 527, 10-Q 1,581; source SEC
EDGAR direct via edgartools 5.62.1; excerpt trims MD&A 100,000 chars,
Risk Factors 60,000 chars, excerpt max 160,000 chars). A fresh window
needs filings after that date, so the refresh build is step 0:

- Build `dataset-edgar-point-in-time-v2` (or a v1 extension with its
  own manifest): same EDGAR extraction, same universe, same trims,
  same point-in-time discipline (filing dates, no look-ahead),
  covering filings with filing date 2026-11-13 through the window
  end. Amendments excluded, as in v1.
- Score every new in-scope filing with Haiku 5.5, prompt version
  `llm-sentiment-v2`, same JSON score contract as Phase 2
  (`llm_sentiment.py`), disk-cached by document hash. No Sonnet
  adjudication (Haiku-only, as in v2/v3/H3).
- Budget: run cap $1.00 in code (repo convention); monthly cap set
  at run time from the user's current Anthropic balance/headroom.
  Expected cost ≈ $0.003 per filing at the measured Haiku screen
  rate; the exact filing count is determined by the refresh, not
  guessed here. November headroom was $4.33 after the pilot
  ($10.6702/$15.00); the refresh will most likely run in December,
  so the December budget must be confirmed before scoring.
- Validation (same as v1): manifest + `haiku_validation.json`;
  verify the union series reproduces the v1 series exactly on all
  pre-Nov-13 days (the same check the v2/v3 drivers ran), and that
  no score is dated before its filing date.
- The refresh does NOT touch the frozen holdout: holdout dates stay
  2026-08-18..2026-11-13, and no parameter is chosen from the fresh
  window before the gate is read.

## Window definition and minimum size

- Start: **2026-11-16** (first trading day after the frozen holdout
  ends 2026-11-13).
- End: fixed at registration time when the run is authorized — the
  default is **2026-12-31**. The end date must be written into the
  results file BEFORE the gate is read; it cannot be moved after
  seeing results.
- Minimums (both must hold, else the window is extended, not run):
  **≥ 30 trading days** AND **≥ 40 in-scope filings with fresh Haiku
  scores** (filing date inside the window). If either minimum is
  unmet, extend the window by 10 trading days at a time, up to 3
  extensions (latest end ≈ 2027-01-29). If the
  minimums are still unmet after the third extension, report the
  shortfall and do NOT run the gate.
- Per-design minimum: a design with **< 10 trades** in the window is
  reported as INCONCLUSIVE (too few observations), not PASS/FAIL.

## Baseline

The v4 technical base (exp-technical-v2 panel, long-hold) is
recomputed on the SAME fresh window with the same construction as
rounds 1/2. The frozen-holdout v4 numbers (−4.33% @10bps, −8.45%
@20bps) are reference only and are NOT the gate baseline here.

## Gates (same structure as rounds 1/2, per design)

Computed at costs 0/10/20bps on the fresh window. PASS iff ALL of:

- **SG1:** fresh-window net return @10bps > fresh-window v4 base @10bps.
- **SG2:** fresh-window net return @20bps > fresh-window v4 base @20bps.
- **SG3:** fresh-window net return @10bps > 0.

Any sub-gate failing = FAIL: variant dropped per the kill rule.
Supporting diagnostics (reported, not gated): Sharpe, max drawdown,
Deflated Sharpe, alpha/beta vs SPY, hit rate, trades, turnover.

- **SG4 (H3 only):** recompute the Item 1A similarity IC @20d on the
  fresh window's filings. If it is significantly negative (IC < 0
  with |t| ≥ 2), H3 FAILS regardless of SG1–SG3 — a pass built on a
  veto whose premise is contradicted twice is not trusted. If the IC
  is ≥ 0 or not significant, SG4 passes.
- **H7 audit rule (not a gate):** if H7 C1c's fresh-window Sharpe
  exceeds 5, the result triggers a data/construction audit (re-verify
  the panel, the price join, and the trade list) BEFORE any
  paper-trading decision. A second 9+ Sharpe would indicate an
  artifact, not an edge.
- **Deflated Sharpe trial count:** n_trials continues from 129 (end
  of round 2) and increases by 3 per design confirmed on the fresh
  window (one per cost level, the round-2 convention). The fresh
  window is a new evaluation set; the DSR still accounts for all
  prior trials because the designs were selected using them.

## Decision this informs (paper trading)

- If ≥ 1 design passes SG1–SG3 (and SG4 for H3): the passing design
  with the best fresh-window net10 becomes the paper-trading
  CANDIDATE. Candidacy is not authorization: paper trading still
  requires the user's explicit decision, with written risk limits
  and a kill switch, under the plan's Phase 4 (60-day paper soak).
  The DSR and the 20bps result must be cited in that decision; a
  pass with DSR < 0.95 or a net20 ≤ 0 is flagged "pass, weak" and
  needs a longer validation or reduced size.
- If NO design passes: all four are demoted to "holdout-only
  passes" — no paper trading. Research returns to pattern
  recognition on the larger combined sample (train + frozen holdout
  + fresh window as a new train set), with a NEW frozen holdout
  defined for any future test. The fresh window is then train data,
  never a test set again.
- If H7 C1c passes again: same candidacy rule, plus the audit rule
  above. A second pass does not by itself authorize paper trading.

## Limits (read before interpreting any pass)

- **This tests holdout-overfitting, NOT knowledge-cutoff
  contamination.** Fresh-window filings will be scored by Haiku 5.5
  after the window, with training knowledge that may postdate the
  filings — the same contamination caveat as rounds 1/2. A pass is
  evidence the designs generalize beyond the 63-day holdout, not
  proof of a contamination-free edge. A clean test would need a
  model whose cutoff predates the filings.
- The fresh window is short (≥ 30 trading days by design, default
  ~33 to Dec 31) and December is seasonally thin for 10-Q/10-K
  filings; the ≥ 40-filings minimum exists for exactly this reason,
  and the extension rule is the only permitted remedy.
- Universe is not survivorship-free; filing dates (not EDGAR
  acceptance timestamps) with a 1-bar lag; after-close filings are
  effectively 1 day late — same as all prior runs.
- Backtest only; no paper trading, no live money, ALLOW_LIVE stays 0.
