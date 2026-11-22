# Round 2 — calibration sprint design (exp-llm-long-horizon-r2)

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.

## Context

Round 1 (Nov 22, 2026) produced the first two gate passes on the frozen
holdout (2026-08-18..2026-11-13):
- **v2** (standalone LLM long-horizon panel): holdout net **+2.31%** @10bps.
- **v3** (v2 + deterministic technical veto v1_composite > 0): holdout net
  **+4.74%** @10bps.
Both have Deflated Sharpe below 0.95 (0.731 / 0.931, n_trials 75 / 81).
Every new holdout look raises family-wise false-positive risk; this
round tracks n_trials from **81** and reports DSR for everything.

Round 1 also established, from train-only pattern analysis ($0):
- the window is mean-reverting, not trending (momentum anti-predictive
  at 10–60d; nothing predicts at 1 day);
- high realized vol is the strongest technical predictor
  (rvol_20 IC +0.083 @60d) and v4's risk filter excluded exactly those
  names;
- Haiku score LEVEL predicts at 20–126d (IC +0.060/+0.063), score
  changes don't;
- textual deltas predict (tone improvement +0.094 @20d, filing
  similarity +0.098, length growth −0.108 @60d).

Two Gemini Deep Research briefs (Nov 22, 2026, PDFs in
~/workspace/user/files/) add the literature basis for this round:
- additive daily LLM tilts fail; vetoes / decoupled mixtures work;
- 10-K/10-Q language changes accrue over 3–18 months and are worth
  only ~4–23 bps prorated over 5–20 days — a slow exclusion veto only,
  never a short-horizon signal;
- earnings calls beat filings at short horizons (PEAD.txt blend
  5.2 bps/day over days 1–32) — **not testable: no call transcripts in
  the dataset** (future-data item, do not acquire data without asking);
- cross-family ensembles beat single models; high cross-family
  disagreement predicts underperformance (veto top-quintile
  disagreement);
- conditioned reversals with buy/hold banding and high-volume shocks
  survive costs;
- turnover control is structural — overlapping 1/H cohorts or
  15/30–20/50 banding.

## Funding (Nov 22, 2026)

User added **$5 to Anthropic** (balance was ~$1.95, expect ~$6.95) and
**$5 to OpenAI**. November budget is now **$15 total** ($11.77 original
+ $5 top-up); the $1/run cap is kept. November spend so far $9.824975.
Anthropic has NO balance endpoint — verify via the spend ledger and
successful calls, not a balance check. The repo's monthly spend cap is
$10 in code; the cap accounting is updated to $15 for November in the
ledger (see PROGRESS.md). The GPT-6.1 Sol second-opinion leg is
**pending**: no OpenAI connector exists yet
(~/workspace/custom-connectors/custom.openai/ absent, verified Nov 22);
it is skipped in this round and reported as pending — the user is never
asked for the key in chat.

## Hypotheses (priority order)

All designs are selected on TRAIN data only, one holdout confirmation
per design that clears its train bar. Gate (holdout) = beat v4 base
−4.33% @10bps AND net positive @10bps; report DSR, Sharpe, turnover,
alpha vs SPY for every confirmation.

### R2-H1 — Haiku bull/bear debate (funded, ~$0.99)

3 Haiku calls per filing (bull advocate, bear advocate, adjudicator),
n=100 train subsample, vs the single-pass score. Decision rule
(pre-registered): the debate is worth scaling IF the debate IC @20d
or @60d exceeds the single-pass IC by ≥ 0.02 consistently at both
horizons; otherwise DROP. Only proceeds if the Anthropic budget fits
(pre-budget at ~2.9 chars/token; $1/run cap). Design: the adjudicator
sees both advocates' one-sentence cases and returns the final
−1..+1 score; bull/bear prompts are role-specific system prompts,
cached separately (cache_tag) from single-pass scores. Same excerpt,
same model (Haiku 5.5), same point-in-time rules.

### R2-H2 — v3 + turnover control ($0)

v3's train turnover is 0.190/day — the cost drag is the binding
constraint. Two structural controls, tested separately:
- **H2a buy/hold banding** (De Groot/Huij/Zhou 2012; Blitz et al.
  2023): enter on the base selection; exit only when the Haiku score
  falls below a buffer percentile (30th / 50th = median) of that day's
  positive scores. Held set updates only on the rebalance grid.
- **H2b overlapping 1/H cohorts** (Chen & Wang 2026): H = 10 or 20
  daily cohorts, each rebalancing every H days from a different
  offset; the blended portfolio is the equal-capital mean of cohort
  daily returns. Caps daily one-way turnover structurally at 100%/H.
Train bar (per the task brief): net10 within 10pp of **v3 (+85.48%)**
for ALL H2 designs AND turnover < 0.10/day AND walk-forward ≥ 3/4
positive folds. (An earlier draft of this doc said "its base"; the
brief's bar is vs v3 and governs.)

### R2-H3 — Lazy-Prices veto ($0)

YoY Jaccard/cosine diff on Item 1A Risk Factors (Cohen, Malloy &
Nguyen 2020), same form type (10-K vs prior 10-K, 10-Q vs prior
10-Q), computed deterministically from the existing EDGAR excerpts.
Missing Risk Factors text in either filing = no change (similarity
1.0): no evidence of an edit, no veto. Changer = bottom-quintile
similarity cross-sectionally each day; the veto excludes changers
from v2/v3 longs. The literature's spread is short-leg driven, so
the long-only exclusion veto is the only usable leg. Train bar:
beats its base by ≥ 2pp AND walk-forward ≥ 3/4.

### R2-H4 — decoupled mixture ($0)

Rank-average of Haiku rank + technical rank with separate thresholds
(Guo & Hauptmann 2025): mixture ≥ 0.9 AND each rank ≥ 0.5. No
additive tilt. Train bar: train net10 > v2's +57.77% AND
walk-forward ≥ 3/4.

### R2-H5 — cross-family ensemble ($0)

Ridge + GBDT + Haiku rank averaged, plus a veto on top-quintile
cross-family forecast disagreement (Foley et al. 2026; Bali, Kelly,
Mörke & Rahman 2026). Ridge is closed-form numpy; GBDT is a native
binned gradient-boosting implementation (numpy only). **sklearn is
never imported by repo code**: a pip install of scikit-learn into the
venv was flagged by an execution-risk check, so the packages sit
installed but unused. Walk-forward (4 expanding folds) over train;
target = 20-day forward return; rows whose forward window runs past
the last train day are excluded (no holdout-price labels leak into
training). Train bar: train net10 > v2's +57.77% AND walk-forward
≥ 3/4. Both the veto and no-veto panels are evaluated.

### R2-H6 — high-volume shock + intraday/overnight ($0)

High-volume shock (Gervais, Kaniel & Mingelgrin 2001: volume in the
top decile of its own 49-day distribution) plus intraday/overnight
20-day return decomposition (Lou, Polk & Skouras 2019), from existing
Alpaca bars. IC gate first: backtest only if a feature clears
|IC| ≥ 0.03 @20d. The backtest design (pre-set): score =
mean percentile rank of intraday_20d and overnight_20d, masked to
top-decile volume-shock names, engine top-10. Train bar: train
net10 > v2's +57.77% AND walk-forward ≥ 3/4.

### R2-H7 — round-1 runner-ups ($0)

C6-rank textual delta (+48.97% train net10) and C1c reversion+RSI
(+64.06%), rebuilt exactly as in round 1 and re-verified in R2.
Register and confirm at most one: pick by highest train net10,
tie-break walk-forward. Train bar: pick clears +40% train net10
AND walk-forward ≥ 3/4.

## GPT leg — gpt-6.1-sol second opinion (funded, $5 OpenAI credit)

OpenAI VERIFIED live Nov 22, 2026 ~02:55 EST: GET /v1/models 200
(99 models, gpt-6.1-sol present — the pinned second-opinion model);
smoke chat call on gpt-6.1-sol 200 (73 tokens). Connector
custom.openai, helper bin/openai_api.py (sends max_completion_tokens;
gpt-6.1-sol rejects max_tokens with a 400). Credit-grants endpoint is
403 for secret keys (browser session key only), so the $5 balance is
not directly readable — a successful chat call is the practical proof
credits exist, and every call's token usage is logged in the spend
ledger (.cache/openai_spend.json).

Adapter: `gpt_sentiment.py` (GPTProvider, same JSON score contract as
the Haiku screen, disk cache, SpendCap run $4.50 / monthly $5.00).
Driver: `r2_gpt.py`. Scope: the Phase-2 Sonnet adjudication subset
(104 train filings, most decision-relevant first) ONLY, sized before
scoring by worst-case accounting (input ~2.9 chars/token, 300 max
output tokens, repo-recorded prices $2.00/$10.00 per MTok — pricing
NOT verified from a price list; token counts are ground truth).
Diagnostic only (no portfolio gate, no Sheet rows): cross-family
agreement (Haiku vs GPT, Sonnet vs GPT), pooled rank IC @20d/@60d
for GPT vs Haiku on the same filings, and which side GPT takes where
Haiku and Sonnet disagree in sign.

## Not testable (future-data item)

Earnings-call hypotheses from the Gemini briefs — PEAD.txt blend
(SUE.txt 3.4 bps/day, mix 5.2 bps/day over days 1–32), bleak tone
changes, Q&A evasion / on-topic alignment — need call transcripts.
The dataset has filings only. Do not acquire data without asking.

## Guardrails

- Nothing from the 7 shared repos (OpenBB, TradingView MCP,
  FinceptTerminal, daily_stock_analysis, ai-hedge-fund,
  TradingAgents, Alpaca MCP) is ever cloned, installed, imported, or
  run — ideas are implemented natively in stock-thesis code only.
- Frozen holdout is never retuned against. One holdout confirmation
  per design.
- Costs estimated at ~2.9 chars/token; STOP and report if a design
  cannot fit the budget.
- Every experiment is logged to the Sheet
  (ID 1bJdCrPm2licM-arLLdIck2zAq15gWfa6ZNjrk14h3G4), PROGRESS.md,
  and the registry; the full test suite must pass; commit + push via
  the Git Data API helper.

## Files

- `r2_panels.py` — shared point-in-time panel builders (H2–H7) +
  native Ridge/GBDT.
- `r2_calibrate.py` — train-only $0 calibration (H2–H7); writes
  `experiments/results/sprint-r2-calibration.json`.
- `r2_debate.py` — H1 debate scoring + IC comparison (funded).
- `gpt_sentiment.py` — GPT-6.1 Sol second-opinion adapter
  (GPTProvider: same JSON score contract, disk cache, SpendCap run
  $4.50 / monthly $5.00, OpenAI spend ledger).
- `r2_gpt.py` — GPT leg driver (adjudication subset only).
- `r2_confirm.py` — one holdout confirmation per design that clears
  its train bar (written after calibration).
- `test_r2.py` — unit tests for the panel builders and the
  llm_sentiment debate extension.
- `llm_sentiment.py` — extended `AnthropicProvider.score()` with
  `system_prompt` / `cache_tag` (debate roles), default behaviour
  unchanged.
