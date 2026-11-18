# Stock Thesis — Agentic Trading System Progress

Base: upstream Stock-thesis at commit `173535e`, tagged `base-upstream-173535e`
(2026-11-16). That tag is the revert point: `git checkout base-upstream-173535e`
returns to the untouched upstream code. All agentic work lives on branch
`agentic-p0` (later milestones get their own branches/tags).

Working branch: `agentic-p0`
Live trading: **disabled** (`ALLOW_LIVE=0`). Paper only. No API keys are ever
committed — `.env` is git-ignored. Keys live only in the local `.env`.

## Milestones (from PROJECT_STATUS.md)

| Milestone | Goal | Status |
|---|---|---|
| P0 | Honest signal evidence: experiment registry + frozen holdout, registered before results, real-data backtest vs baselines at 1x/2x costs | In progress — registry + holdout + LLM stub done; real-data backtest not yet run |
| P1 | Exact-once order lifecycle: persist intent, idempotency key, query-before-retry, partial-fill / protection recovery | Not started |
| P2 | Paper-operation soak: 60 trading days, no duplicates, no unresolved reconciliation, no missed critical alerts | Not started |
| P3 | Small live canary, only after P0–P2 pass + written approval | Not started |
| P4 | ML / LLM comparison only after simple baselines pass | Not started (LLM stub is mock-only) |

## Done — 2026-11-16

- Cloned HarshithSaiSurendra-lab/Stock-thesis (~7,886 lines, Python 3.12).
  Offline suite: 42 tests pass.
- Verified Claude Fable 5.2 is real (Anthropic, Nov 9, 2026,
  `claude-fable-5-2`, $8/$40 per million tokens). Lead agent for research
  and the LLM sentiment layer. ChatGPT is the independent second-opinion
  agent. Neither model is in the order path.
- P0 groundwork (`experiments.py`, `experiments/registry.jsonl`,
  `experiments/holdout.json`): append-only registry, holdout frozen as the
  final 63 trading days of the acquired point-in-time adjusted dataset
  (dates null until a dataset is acquired; re-freeze refused by code).
  Registered before any results: `exp-technical-only-v1`,
  `exp-technical-plus-llm-v1` (LLM adds at most ±1.0, only on agreement
  ≥0.7 and both confidences ≥0.6).
- `llm_sentiment.py`: mock-only stub (Fable + ChatGPT scores −1..+1,
  agreement, combined score, cost 0.0). Not imported by the order path.
  10 new tests; full suite 52/52 passes.
- Alpaca paper keys configured (local `.env` only). `deploy_check` passes.
  Paper account ACTIVE, $100,000 paper equity.
- IEX data fix (`universe.py`): bars requested with no start date returned
  only 1 SPY bar on the free IEX plan ("subscription does not permit
  querying recent SIP data" with an explicit recent end). Passing a start
  date with no end returns the full window (275 SPY bars).
- First real-data dry-run, 2026-11-17 (market closed, overnight): regime
  OK (SPY above SMA50/200), 53 symbols evaluated, 0 orders — system
  abstained, nothing placed.
- Synthetic checks only (NOT market evidence): research runner v1
  $1,000 → $602, Sharpe −0.79; v2/v3/v4 → $767, Sharpe −0.48. Full
  405-cell robustness grid was interrupted by a runtime restart; a
  single-cell synthetic rerun (v1, 2024-01-01) was +20.1%, −23.2% max
  drawdown — one synthetic window, not evidence.

## Done — 2026-11-17

- API funding research (verified live, Nov 2026): user has Claude Pro +
  ChatGPT Plus, neither includes API credits. Anthropic funded first:
  $10 added 2026-11-17, balance $11.77. OpenAI $5 in week 2, after the
  Claude pipeline works. $15 month 1. Methodology agreed: Haiku 5.5
  screens, Sonnet 5.5 adjudicates, Fable 5.2 audits a 5-10% sample only
  (NOT the daily model), GPT-6.1 Sol second opinion. Full page:
  "LLM API Funding and Models" artifact.
- Real Anthropic provider wired into `llm_sentiment.py`
  (`AnthropicProvider`, roles screen/adjudicate/audit, pinned model IDs
  + Nov 2026 prices, strict-JSON parsing, ~40k-token excerpt trim,
  disk cache by document hash, per-run $1.00 + monthly $10.00 spend cap
  in code with a JSON ledger). SDK `anthropic>=1,<3` (2.9.0 installed);
  client is built with an explicit proxy + trust_env=False because the
  VM's NO_PROXY ([::1] entries) makes httpx choke at construction.
  Smoke-test script `llm_smoke_test.py` (Haiku + Sonnet + Fable on one
  short TEST excerpt, a few cents). 8 new provider tests; full suite
  60/60 passes. Still NOT imported by the order path. Pushed to GitHub
  branch `agentic-p0` at remote commit 2fd4528 (tree verified identical
  to local), local branch reset to match; 60/60 tests pass after reset.
- Anthropic connected via Secure Vault 2026-11-17 (custom.anthropic,
  key in `x-api-key` header, host api.anthropic.com). Connector doc +
  helper at `~/workspace/custom-connectors/custom.anthropic/`
  (`bin/anthropic_api.py`, Messages API via surrogate, no key ever
  printed). Smoke test RUN 2026-11-17 on the $11.77 balance via the
  stored credential: Haiku +0.60 (conf 0.80, $0.0001), Sonnet +0.50
  (conf 0.60, $0.0017), Fable +0.50 (conf 0.65, $0.0107) — all three
  agree on the TEST excerpt (not a real filing, not a signal). Total
  $0.0124, ledger `.cache/llm_spend.json` Nov 2026 = $0.012397.
  First attempt timed out in the egress proxy tunnel (60s); immediate
  retry returned in ~1s — treat proxy timeouts as unconfirmed, check
  ledger/cache before retrying a scored call.
- Real-data backtest on Alpaca history: technical-only vs technical+LLM,
  walk-forward, 1x and 2x costs, vs SPY buy-and-hold / equal-weight.
  Record the holdout calendar dates at acquisition; never change after.
  (Blocked on the smoke test + a real filing excerpt source.)

## Next

1. Anthropic API key via secure vault, then run `llm_smoke_test.py`
   (expect a few cents) and verify cost logging + caps.
2. Real-data backtest on Alpaca history: technical-only vs technical+LLM,
   walk-forward, 1x and 2x costs, vs SPY buy-and-hold / equal-weight.
   Record the holdout calendar dates at acquisition; never change after.
3. Decide the point-in-time dataset source (Alpaca history is a first
   pass with disclosed limits — current universe, raw adjustment — not
   survivorship-free evidence).
4. OpenAI API key ($5, week 2) + GPT-6.1 Sol second-opinion adapter.
5. P1 exact-once order lifecycle, then P2 paper soak.

## How to revert

- To the untouched upstream base: `git checkout base-upstream-173535e`
- To the P0 checkpoint (this branch's first commits): see `git log agentic-p0`
- Never force-push to `main`. Milestones land on `main` only after their
  exit gate passes and the user approves.

## 2026-11-17 — First real-data backtest: exp-technical-only-v1 (FAILED P0 gate, honestly)

- Driver: `run_real_backtest.py` (research-only, not imported by order path).
  Fetched Alpaca IEX history for 134 symbols (133 seed + SPY), cached to
  `.cache/backtest_data/` (git-ignored), window 2024-01-02..2026-11-13
  (721 trading days). Fixed two driver bugs along the way: tz-aware Alpaca
  timestamps vs tz-naive slicers, and SPY being excluded from the
  universe-only price panel used for baselines.
- Holdout recorded in `experiments/holdout_dates.json` (never change):
  train 2024-01-02..2026-08-17 (658 days), holdout 2026-08-18..2026-11-13
  (63 days). Full results: `experiments/results/exp-technical-only-v1.json`.
- RESULT — the technical signal has NO edge on this data. Train, 10bps:
  v4 (best) -25.9% return, Sharpe -0.74, max DD -32.5%, 1,057 trades;
  v1 -28.5%, Sharpe -0.67, 1,673 trades. Walk-forward median Sharpe
  ~-0.94, 0/4 positive folds. Baselines over the same window: SPY
  buy-and-hold +63.5%, equal-weight universe +48.0%. At 20bps costs it is
  much worse (v4 -51.4%, Sharpe -1.64, max DD -53.8%).
- Holdout (untouched), 10bps: all variants -4.3% to -6.7% (SPY -1.7%,
  equal-weight +1.7%). At 20bps: -8.5% to -11.2%.
- Reading: high turnover (1,000+ trades) + negative Sharpe in every fold
  means the composite is not a tradeable signal as-is — costs eat it and
  the signal itself underperforms even before costs. This is exactly what
  P0 is for: do NOT paper-trade this signal, do NOT add LLM sentiment on
  top of it yet (the LLM leg would be dressing up a losing base).
- Next per PROJECT_STATUS P0: fix the signal FIRST (diagnose why — e.g.
  entry timing vs 1-bar lag, rebalance churn, regime filter behaviour;
  v2 and v3 were identical, so the trend-quality filter never binds),
  then re-run as a NEW registered experiment. exp-technical-plus-llm-v1
  still unrun: it needs historical point-in-time 10-K/10-Q excerpts,
  which do not exist yet — scoring later filings would be lookahead.
- Limits (unchanged): IEX feed, RAW adjustment, CURRENT universe —
  not survivorship-free, not point-in-time with delistings. First-pass
  only, not P0 evidence on its own.

## Experiment log (Google Sheet)
- Human-readable log: "Stock-thesis experiment log"
  https://docs.google.com/spreadsheets/d/1bJdCrPm2licM-arLLdIck2zAq15gWfa6ZNjrk14h3G4/edit
  (Sheet1 = results by variant/window/cost, Holdout = frozen dates,
  Notes = limits + next steps). Source of truth stays the repo JSON
  (experiments/results/) + this file; add one row-set per future experiment.

## 2026-11-17 — Phase 0 diagnosis DONE (train only, holdout untouched)

- New metrics module `diagnostics.py` (gross vs net, per-trade
  expectancy/profit factor, Deflated Sharpe, alpha/beta, information
  ratio, t-stat, per-symbol attribution, regime splits, exposure) +
  `test_diagnostics.py` (8 tests). Full suite now 68/68.
- Driver `diagnose_phase0.py`; numbers in
  `experiments/diagnosis/phase0-metrics.json`, write-up in
  `experiments/diagnosis/phase0-diagnosis.md`, Sheet tab
  "Phase 0 diagnosis".
- HEADLINE: signal is gross-positive (+39.8% v1, Sharpe 0.76) but
  costs destroy it (net −28.4% at 10bps; $668.67 per $1,000 paid in
  costs). Median hold 1 day; expectancy −1.78 to −2.62 bps/trade.
  Deflated Sharpe 0.0 for all variants; alpha −9.4% to −11.9%/yr,
  beta ~0. Trend-quality filter binds on 0.12% of active days
  (v2==v3 by construction). 1-bar lag costs 7–24 gross pts
  (lookahead diagnostic only). Regime filter blocks v1's better days.
  Losses broad across symbols, not one name.
- Ranked fixes: (1) cut turnover — min hold / weekly rebalance;
  (2) strong_up only / higher bar; (3) raise tq threshold to ≥4.0
  or delete the filter; (4) regime as sizing dial; (5) hold longer
  to amortise lag; (6) execution fixes may not suffice — gross edge
  itself is weak (DSR 0.0), LLM leg may be needed after.
- Next: register exp-technical-v2 with fixes 1–3, run on train,
  then check the frozen holdout. Kill rule stands.

## 2026-11-18 — exp-technical-v2: fixes worked mechanically, signal still has no edge — GATE FAILED, variant dropped

- User approved building v2 2026-11-18. Registered in
  `experiments/registry.jsonl` BEFORE running (hypothesis, params,
  exit gate: train net10 > 0, holdout net10 > 0 and > SPY, train
  Deflated Sharpe ≥ 0.95 with n_trials=10, holdout net20 > 0).
- Design (fixes 1–3 only): strong_up signals only, trend-quality
  threshold raised to ≥ 4.0, weekly rebalancing (every 5 days) +
  5-day minimum holding period. Regime mask kept identical to
  v2_regime (unchanged); 1-bar lag kept; plain engine, no trailing
  stops (same as v1). New code: `min_hold_days` on BacktestConfig
  (`backtest.py`, enforced on held weights), `build_v2_signal` +
  `_v2_active_labels` (`research_runner.py`, threaded through
  `evaluate_variants`), driver `run_v2_backtest.py`, 7 new tests in
  `test_v2.py`. Full suite 75/75. Results:
  `experiments/results/exp-technical-v2.json`.
- TRAIN (658 days), 10bps: return −14.0%, Sharpe −0.16, max DD
  −49.8%, 364 trades (v1: 1,673), walk-forward median Sharpe −0.23,
  1/4 positive folds. At 20bps: −25.6%, Sharpe −0.31, 0/4 folds.
  Baselines: SPY +63.5%, equal-weight +48.0%.
- HOLDOUT (63 days), 10bps: return −3.4%, Sharpe −0.73, max DD
  −7.8%, 46 trades. At 20bps: −5.0%. Baselines: SPY −1.7%,
  equal-weight +1.7%.
- What the fixes DID do: costs fell $668.67 → $144.80 per $1,000
  (−78%), median hold 1 day → 5.0 days, expectancy −1.78..−2.62 →
  −0.54 bps/trade, profit factor 0.85–0.91 → 0.99, cost drag 68.2 →
  13.4 pts, avg turnover 0.73 → 0.22/day. Max gross exposure stayed
  1.0 — the min-hold rule never pushed leverage over 1.0.
- What they did NOT do: the gross edge vanished. v1 gross +39.8% →
  v2 gross −0.6% on train (−1.8% holdout). The mild_up signals were
  carrying essentially all of v1's gross return; dropping them
  removed the edge, not just the churn. Only 88 of 9,031 strong_up
  days (0.97%) fall below tq 4.0, so the threshold barely bites —
  the selectivity change is almost entirely strong-only. Train
  Deflated Sharpe 0.044, alpha +2.9%/yr (vs v1 −11.9%) but
  t-stat −0.01, 95% CI [−42%, +42%] — statistically nothing.
  Holdout alpha −12.4%/yr, DSR 0.0. Max drawdown got WORSE on train
  (−49.8% vs v1 −40.1% / v4 −32.5%): fewer, longer-held names
  concentrate single-name swings (AVGO −26.6% gross over 90 days;
  one winner, DD +36.3% in 20 days).
- GATE: FAIL on all four sub-gates (train net10 negative, holdout
  net10 negative and below SPY, DSR 0.044 < 0.95, holdout net20
  negative). Per the kill rule, exp-technical-v2 is DROPPED — not
  rescued by raising risk. This confirms Phase 0 fix-hypothesis #6:
  execution fixes do not suffice; the gross edge itself was weak.
- Consequence: the technical composite as built has no tradeable
  edge in either form (churny v1 or selective v2). The edge, if
  there is one, has to come from the LLM sentiment leg
  (exp-technical-plus-llm-v1) — which still needs the point-in-time
  10-K/10-Q dataset decision before it can run. No paper trading
  of either technical variant; no live money; ALLOW_LIVE stays 0.
