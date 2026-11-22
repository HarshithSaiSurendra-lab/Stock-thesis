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

## Done — 2026-11-20

- **Point-in-time EDGAR dataset built — `dataset-edgar-point-in-time-v1`
  COMPLETE and validated** (`data/edgar_excerpts/`, results in
  `experiments/results/dataset-edgar-point-in-time-v1.json`).
  **2,108 records, 133/133 tickers** (10-K 527, 10-Q 1,581),
  filing dates 2023-01-05..2026-11-12; train window 1,446 records,
  frozen holdout 130 records. Build cost $0 new cash (SEC EDGAR direct
  via edgartools 5.62.1). `build_edgar_dataset.py --validate` passes
  with 0 errors. Full test suite 95/95 (incl. 6 new MD&A-fallback tests).
- Build notes: a runtime restart mid-build wiped /tmp and the first
  parallel build's shards; the edgartools cache survived, so the rebuild
  from cache finished in ~55 min (4 parallel workers, fresh processes
  of ≤8 tickers each — long-lived edgartools processes degrade after
  ~15 tickers / ~250 requests). Worker dirs/logs now live under
  `~/workspace/trading-system/` (persistent), not /tmp.
- Gap-fill (w4) fixed everything the first pass missed: BlackRock's
  pre-2024 filings live under former CIK 1364742 (7 filings added, BLK
  9 → 16); a regex MD&A fallback recovered 30 records edgartools'
  section splitter misses (INTC 16, PNC 10 — its 10-Q MD&A is headed
  "Financial Review", USB 3, MCD 1) — missing MD&A is now 0, and the
  one both-sections-empty record (MCD 2023 10-K) is valid; UNP and LIN
  extraction failures retried successfully. Missing Risk Factors: 185
  (mostly 10-Qs where Item 1A is legitimately omitted when unchanged).
- Haiku sample validation (train window only, holdout unscored):
  5 excerpts scored, $0.018034 total (172,870 input / 1,495 output
  tokens) — AAPL +0.25, COF −0.40, GS −0.20, MS −0.10, SBUX +0.60,
  all with sensible rationales citing real filing numbers
  (`data/edgar_excerpts/haiku_validation.json`).
- **Ready for Phase 2**: `exp-technical-plus-llm-v1` (the registered
  LLM test) can now run against this dataset. NOT run yet — dataset
  build only, per the task. No paper trading; no live money.

## Done — 2026-11-20

- **Phase 2 RUN — `exp-technical-plus-llm-v1` COMPLETE. GATE VERDICT: FAIL.
  Variant DROPPED per the kill rule. No paper trading of this variant.**
  Results in `experiments/results/exp-technical-plus-llm-v1.json`;
  registry record `exp-technical-plus-llm-v1-results` (status `failed`).
  - Headline pair v4 vs v4+LLM, holdout 2026-08-18..2026-11-13:
    net 10bps base −4.33% vs LLM −4.71% (SG1 fail), net 20bps base
    −8.45% vs LLM −8.85% (SG2 fail), net 10bps LLM −4.71% < 0 (SG3
    fail). All three sub-gates failed.
  - Train v4+LLM improved markedly (−15.55% vs −25.86% base;
    gross +27.0% vs +12.9%, expectancy −1.43 vs −2.62 bps/trade),
    but the effect did NOT carry to the holdout (holdout gross −0.39%
    vs −0.03% base; expectancy −4.14 vs −3.81 bps/trade). The holdout
    sentiment was stale by construction (latest pre-holdout filing
    carried forward up to ~3 months) — the recorded limitation is the
    likely cause: the LLM edge needs fresh filings to manifest.
  - Supporting pairs (not gates) all improved slightly on the holdout:
    v1 −4.46% → −3.60% (+0.86pp), v2/v3 −6.69% → −5.71% (+0.98pp) at
    10bps; same direction at 20bps. All remain deeply negative.
    On train the LLM hurt v1/v2/v3 (−28.51% → −37.55%; −27.25% →
    −35.13%). The effect is inconsistent across variants and windows.
  - Diagnostics (10bps): Deflated Sharpe 0.0 everywhere (n_trials=18);
    holdout v4+LLM alpha vs SPY −17.5%/yr, beta 0.15, IR −0.62;
    train v4+LLM cost drag $408.00 per $1,000 (gross +27.0% still
    nets −15.55%). Baselines: train SPY +63.48%, EW +48.02%;
    holdout SPY −1.70%, EW +1.65%. Nothing here beats SPY.
  - Scoring: 1,166 in-scope filings (Haiku screen; 795 new, 171
    cached, 0 failed); Sonnet adjudicated the top 104 by relevance
    (Haiku conf ≥ 0.6, worst-case est $4.9968 within the $5.00
    budget). Only 58 of the 104 actually contribute (both confidences
    ≥ 0.6, agreement ≥ 0.7, same sign). LLM adjustment applied on
    4,294 of 71,608 active (symbol, day) cells (6.0%); contribution
    mean +0.477 (models lean positive: Haiku mean +0.267, Sonnet
    +0.304). Haiku–Sonnet mean agreement 0.918; 6 sign disagreements.
- **Spend: $8.888873 total, under the $10 hard cap** (ledger $8.870839
  + $0.018034 off-ledger validation). Haiku $3.300814, Sonnet
  $5.557628. **Sonnet sub-budget BREACHED by $0.5576** ($5.5576 vs
  $5.00): the chars/4 token estimate undercounts SEC filing text by
  ~36% (mean actual Sonnet input 25,998 tokens vs 19,026 estimated;
  ~2.9 chars/token, not 4). The $10 cap held; the gate verdict is
  unaffected. Future runs must use a ~2.9 chars/token estimate (or a
  real tokenizer count) for the Sonnet budget.
- **Run notes (process)**: the run was interrupted once by a runtime
  restart mid-Haiku (~371 filings scored); resumed from the per-role
  score files + ledger-based already-spent accounting with $0
  double-billing (cached re-reads). Two driver bugs found and fixed
  during the run: (1) cache hits were re-adding their original cost to
  the run total; (2) the Haiku/Sonnet passes shared one incremental
  scores file and resume expected the final wrapped structure — now
  per-role files (`...-scores-haiku.json`, `...-scores-sonnet.json`)
  with a `_load_scores()` helper. (3) `score_pass` read
  `input_tokens`/`output_tokens` but the provider returns
  `tokens_in`/`tokens_out`, so the printed token totals were 0 —
  fixed in code; the results JSON token counts were corrected
  post-run from the scores files (Haiku 31,467,471 in / 308,101 out;
  Sonnet 2,703,744 in / 15,014 out) with a correction note in the
  JSON. Also: the first registry amendment recording was wiped by the
  dataset build agent's post-push `git reset --hard`; the identical
  amendment was re-recorded and then committed (141a214) before the
  resumed run scored anything. Full test suite 112/112 passes.
- **Amendment recorded before scoring**
  (`exp-technical-plus-llm-v1-amendment-1`): Haiku 5.5 screen / Sonnet
  5.5 adjudicate replaces the registered Fable/ChatGPT leg (OpenAI
  adapter not built, $5 top-up never approved; Fable audit-only).
  Agreement is within one model family, not independent models —
  recorded as a caveat on the verdict. No Fable audit run. No
  holdout-window filing scored. No paper trading; no live money;
  ALLOW_LIVE stays 0.

## Done — 2026-11-21

- **Freshness test RUN — `exp-technical-plus-llm-freshness-v1` COMPLETE.
  GATE VERDICT: FAIL. Variant DROPPED per the kill rule. No paper
  trading of this variant.** User authorized the run Nov 21, 2026.
  Registered BEFORE scoring (registry entry + design doc
  `experiments/exp-technical-plus-llm-freshness-v1-design.md`); driver
  `run_freshness_backtest.py`; results in
  `experiments/results/exp-technical-plus-llm-freshness-v1.json`;
  registry record `exp-technical-plus-llm-freshness-v1-results`
  (status `failed`); Sheet rows A66:L101 (36 rows, read back verified).
  - Headline pair v4 vs v4+LLM-fresh, holdout 2026-08-18..2026-11-13:
    net 10bps base −4.33% vs fresh −4.14% (SG1 pass, +0.19pp), net
    20bps base −8.45% vs fresh −8.38% (SG2 pass, +0.07pp), net 10bps
    fresh −4.14% < 0 (SG3 fail). Freshness HELPED — fresh beats the
    stale variant by +0.57pp @10bps (+0.47pp @20bps; stale −4.71% /
    −8.85%) and turns holdout GROSS positive (+0.29% vs stale −0.39%
    and base −0.03%), the first gross-positive holdout for any LLM
    variant — but costs (−4.43pp drag) still sink it to −4.14% net.
    The technical base itself is −4.33% on the holdout; freshness
    closes part of the gap but does not create an edge.
  - Supporting pairs (not gates): v1 fresh −2.18% (best LLM holdout
    yet: +2.28pp vs base, +1.42pp vs stale); v2/v3 fresh −6.24%
    (WORSE than stale −5.71% by 0.53pp). The freshness effect is
    inconsistent across variants, same as Phase 2's LLM effect.
  - Diagnostics (10bps): Deflated Sharpe 0.0 everywhere (n_trials=26);
    holdout fresh Sharpe −0.99 (base −1.02, stale −1.11), alpha vs SPY
    −14.9%/yr (base −15.9%, stale −17.5%), beta 0.17, IR −0.51.
    Train fresh == stale exactly (−15.55%, asserted by the driver:
    union current series is identical to Phase 2's on train days).
    Baselines: holdout SPY −1.70%, EW +1.65%. Nothing beats SPY.
  - Scoring: 75 in-scope holdout filings (Haiku screen; 75 new, 0
    cached, 0 failed); Sonnet adjudicated the top 10 by holdout
    relevance (Haiku conf ≥ 0.6, 2.9-char/token worst-case est $0.7316
    within the $0.75 sub-budget). Fresh LLM adjustment applied on 320
    holdout cells vs stale's 126 (4,488 total vs 4,294 stale).
- **Spend: $0.779766 this run, under the $1.00 run budget** (Haiku
  $0.19098, Sonnet $0.588786; worst-case estimates $0.2232 / $0.7316
  held — no sub-budget breach this run, the 2.9 chars/token fix
  works). Cumulative Nov total $9.668639 (ledger), under the $10.00
  monthly cap; Anthropic balance remaining $2.10 ($11.77 − $9.668639).
  `llm_sentiment._estimate_tokens` fixed to ~2.9 chars/token in the
  same commit as the registration, per the Phase 2 lesson.
- **Reproducibility**: the driver rebuilds the stale panels from
  Phase 2's scores in the same run and asserts base + stale train and
  holdout returns match Phase 2's recorded numbers exactly before the
  gate is read — the check PASSED. Full test suite 117/117 (5 new
  offline tests in `test_freshness.py`, incl. the estimator test that
  pins the 2.9 rate and the subset test that reproduces the Phase 2
  undercount failure mode).
- **Consequence**: the LLM leg's staleness was real but is not the
  whole story. Fresh scores improve the holdout, but no variant —
  technical, stale-LLM, or fresh-LLM — is net positive on the frozen
  holdout, and Deflated Sharpe is 0.0 throughout. The signal has no
  tradeable edge in any tested form. Next steps, if any, need a new
  registered hypothesis (different signal, different universe, or
  different cost structure) — not another rescoring of the same
  filings. No paper trading; no live money; ALLOW_LIVE stays 0.

## Done — 2026-11-22

- **Calibration sprint RUN — `exp-llm-long-horizon-v2` COMPLETE.
  GATE VERDICT: PASS — all 3 sub-gates pass. First net-positive
  holdout in the project.** User authorized running the repo's code
  Nov 22, 2026 ~00:46 EST ("Yes, run the repo code"), priority
  PATTERN RECOGNITION FIRST. Registered BEFORE scoring (registry
  entry + design doc
  `experiments/exp-llm-long-horizon-v2-design.md`); driver
  `run_llm_long_horizon_v2.py`; results in
  `experiments/results/exp-llm-long-horizon-v2.json`; registry record
  `exp-llm-long-horizon-v2-results` (status `passed`); Sheet rows
  A102:L107 (6 rows, read back verified).
  - Pattern recognition (train only, $0): the window is mean-reverting,
    not trending — momentum features are anti-predictive at 10-60d
    (composite IC -0.030 @60d, t -4.88); high realized vol is the
    strongest technical predictor (rvol_20 IC +0.083 @60d, Q-spread
    +5.59pp) and v4's risk filter excludes exactly those names; the
    Haiku score predicts at 20-126d (IC +0.060/+0.063), matching its
    3-12mo prompt, while the engine's 1-day holding wasted it;
    textual deltas predict at $0 (tone improvement IC +0.094 @20d,
    filing similarity +0.098 @20d, length growth -0.108 @60d).
  - Calibration: C2 LLM long-horizon (top-5 by score, rebalance 5,
    min hold 10) train net10 +153.83%, WF 1.92, 3/4 folds — but
    Fable 5.2 audit ($0.156336, 3 calls) found the result is driven
    by the tie-break: ~23 symbols tie at the 5th-place score on
    131/132 rebalance days, and train results run +53% to +182%
    across defensible tie-break rules. Verdict: "redesign first."
    Redesign (this experiment): hold EVERY name with score >= the
    day's 5th-highest positive score, equal weight — no tie-break.
    Train net10 +57.77% (gross +59.50%, net20 +56.06%), WF median
    Sharpe 0.604, 4/4 folds positive, 120 trades, turnover
    0.017/day. The pattern survives tie-break removal in diversified
    form (mean 25.7 names; holdout mean 37.4).
  - Headline, holdout 2026-08-18..2026-11-13: gross +2.53%, net
    10bps +2.31% (SG1 pass, +6.64pp vs v4 base -4.33%), net 20bps
    +2.10% (SG2 pass, +10.55pp vs base -8.45%), net 10bps +2.31% > 0
    (SG3 pass). Sharpe 0.661 @10bps, max DD -4.76%, hit rate 50.8%,
    43 trades, turnover 0.033/day, alpha vs SPY +9.7%/yr, beta -0.11.
  - **Honest limits on the PASS:** Deflated Sharpe 0.731 @10bps
    (n_trials=75) is BELOW the 0.95 significance threshold — the
    gate passes but the result is not proven once 75 trials are
    accounted for. LLM knowledge-cutoff contamination: Haiku 5.5 was
    run Nov 2026 and may know outcomes of the filings it scored;
    the gate cannot remove it. Holdout is 63 days; universe is not
    survivorship-free. No paper trading of this variant without a
    further decision; ALLOW_LIVE stays 0.
  - Verification done along the way: machinery reproduces
    exp-technical-v2 train net10 -14.01% (recorded -14.0%) exactly;
    MU entry verified point-in-time clean (10-Q filed 2025-12-18,
    Haiku +0.85, entry 2025-12-23, raw +251.71%); beta -0.10 vs SPY
    verified two ways; C6 textual-delta z-score leakage caught and
    corrected (full-sample z-scoring inflated +137.79% to a
    point-in-time +48.97%). Runner-ups recorded for future registered
    experiments, NOT evaluated on the holdout: C6-rank textual delta
    (+48.97% train net10, WF 1.87, 3/4), C1c reversion+RSI (+64.06%,
    WF 1.18, 3/4).
- **Spend: $0.00 new scoring this run** (all scores reused: Phase 2
  Haiku 1,166 + freshness Haiku 75). Fable 5.2 audit $0.156336.
  Cumulative Nov total $9.824975 (ledger $9.806941 + off-ledger
  $0.018034), under the $10.00 monthly cap; Anthropic balance
  remaining ~$1.95 ($11.77 − $9.824975); monthly cap headroom $0.175.
  The user's +$5 Anthropic top-up is UNVERIFIED (no balance
  endpoint). OpenAI / GPT-6.1 Sol leg: no connector exists, adapter
  not built — pending, not run.
- **Reproducibility**: the confirmation driver asserts the union
  current-filing series equals the stale series on train days and
  that the train reproduction matches the redesign's +57.77% net10
  within 0.2pp before the gate is read — both checks PASSED. Full
  test suite 117/117 passed.

- **Repo-derived hypotheses RUN (native only) + `exp-llm-long-horizon-v3`
  COMPLETE. GATE VERDICT: PASS — all 3 sub-gates pass, and v3 beats
  v2 on the holdout.** Parent follow-up Nov 22, 2026: repo assessment
  (read-only) ranked TradingAgents / ai-hedge-fund as study-for-ideas,
  daily_stock_analysis as mine-its-catalog, OpenBB data-layer only,
  Alpaca MCP adopt-narrowly-later, tradingview-mcp + FinceptTerminal
  skip. User guardrail, effective immediately: do NOT clone, install,
  or run any code from the 7 shared repos — every idea below was
  implemented natively in stock-thesis code only (`test_repo_hypotheses.py`,
  results in `experiments/results/sprint-repo-hypotheses.json`).
  - H1 Haiku bull/bear debate: BLOCKED on funding, not tested. Debate
    = 3 Haiku calls/filing, ~$0.009908/filing (3.5x the measured
    $0.002831 single-pass cost); n=100 costs ~$0.99 vs $0.175 monthly
    headroom; at most ~17 filings fit. Needs the +$5 Anthropic top-up
    verified (still UNVERIFIED). No calls were made.
  - H2a LLM as veto on the technical panel: WEAK (calibration C3,
    cited): train net10 +9.10%, WF -0.28, 1/4 folds vs v4 long-hold
    control +7.02%. Not registered.
  - H2b Technical veto on the passing v2 design: STRONG on train.
    v2 + composite > 0 veto: train gross +110.20%, net10 +85.48%,
    net20 +63.64%, WF 0.767 @10bps, 3/4 folds @10bps, 444 trades,
    turnover 0.190/day (v2: +57.77% net10, WF 0.604, 4/4, 120 trades,
    0.017/day). The sma_50 veto was tested and NOT selected (+22.04%,
    worse than v2). Registered as `exp-llm-long-horizon-v3` BEFORE
    scoring (registry entry + design doc
    `experiments/exp-llm-long-horizon-v3-design.md`); driver
    `run_llm_long_horizon_v3.py`; results in
    `experiments/results/exp-llm-long-horizon-v3.json`; registry
    record `exp-llm-long-horizon-v3-results` (status `passed`);
    Sheet rows A108:L113 (6 rows, read back verified).
  - H3 PEAD + MA alignment / pullback: ALL NULL on train. PEAD proxy
    (filing-date abnormal return, n=1,446): IC +0.0078/+0.0077/+0.0035
    @5/10/20d — no drift, no reversal. MA alignment: IC -0.0162/
    -0.0103/-0.0028 @5/10/20d, Q-spread -0.02pp @20d — slightly
    negative (mean-reversion regime) but too weak to use. Pullback:
    -0.33pp/-0.48pp/-0.62pp vs the rest @5/10/20d — pullbacks do
    WORSE. The pre-set bar (|IC| >= 0.03 @20d or 20d Q-spread >= 1%)
    was NOT cleared, so no alignment backtest was run. All three
    patterns are dropped in this window.
  - Headline v3, holdout 2026-08-18..2026-11-13: gross +6.07%, net
    10bps +4.74% (SG1 pass, +9.07pp vs v4 base -4.33%), net 20bps
    +3.44% (SG2 pass, +11.89pp vs base -8.45%), net 10bps +4.74% > 0
    (SG3 pass). vs v2: net10 +2.43pp better, net20 +1.34pp better.
    Sharpe 0.91 @10bps, max DD -9.52%, hit rate 50.8%, 43 trades,
    turnover 0.033/day, selection mean 8.2 names (min 0 — some days
    no name passes both filters), alpha vs SPY +19.0%/yr, beta -0.41.
  - **Honest limits on the PASS:** this is the SECOND holdout
    evaluation on the same frozen window — NOT independent evidence
    vs v2's pass; the family-wise false-positive risk grows with
    each holdout look. Deflated Sharpe 0.931 @10bps (n_trials=81) is
    still just below the 0.95 threshold (0.966 at 0bps). The veto's
    edge depends on turnover (0.190/day vs v2's 0.017/day); at 20bps
    the train advantage over v2 is only +7.58pp and only 2/4 WF folds
    are positive. LLM knowledge-cutoff contamination (same as v2).
    No paper trading of this variant without a further decision;
    ALLOW_LIVE stays 0.
  - **Spend: $0.00 new scoring this run** (all scores reused).
    Cumulative Nov total $9.824975, balance ~$1.95, monthly cap
    headroom $0.175. The +$5 Anthropic top-up is UNVERIFIED. OpenAI /
    GPT-6.1 Sol leg: no connector exists, adapter not built — pending.
  - **Reproducibility**: the confirmation driver asserts the union
    current-filing series equals the stale series on train days and
    that the train reproduction matches H2b's +85.48% net10 within
    0.2pp before the gate is read — both checks PASSED. Full test
    suite 117/117 passed.
