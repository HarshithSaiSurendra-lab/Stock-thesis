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

## Done — 2026-11-22 — Sprint round 2 (calibration + GPT leg)

**Context:** round 1 closed with two gate passes on the frozen holdout
(2026-08-18..2026-11-13): v2 net +2.31% @10bps, v3 net +4.74% @10bps,
but Deflated Sharpe below 0.95 in both (0.731/0.931, n_trials 75/81).
Every new holdout look raises family-wise false-positive risk — n_trials
is tracked and DSR reported for everything below. Design doc:
`experiments/exp-llm-long-horizon-r2-design.md`. All hypotheses were
registered in `experiments/registry.jsonl` BEFORE scoring. $0 tests
(H2–H7) first, then H1 debate, then the GPT leg. No paper trading;
ALLOW_LIVE stays 0.

**Funding (verified Nov 22):** user added $5 to Anthropic AND $5 to
OpenAI. November Anthropic budget is now $15 total ($11.77 original +
$5 top-up); the $1/run cap is kept. Anthropic has NO balance endpoint —
verified via the spend ledger and successful calls, not a balance check.
OpenAI key entered via Secure Vault ~02:41 EST and VERIFIED live
~02:55 EST (GET /v1/models 200, 99 models incl. `gpt-6.1-sol`;
smoke chat 200, 73 tokens). Credit-grants endpoint is 403 for secret
keys (browser session key only), so the $5 balance is not directly
readable — a successful chat call is the practical proof credits exist.
Connector: `~/workspace/custom-connectors/custom.openai/` (helper
`bin/openai_api.py`, sends `max_completion_tokens` because gpt-6.1-sol
rejects `max_tokens` with a 400).

### Calibration (H2–H7, train only, $0) — `r2_calibrate.py`, `r2_panels.py`
Anchors reproduced exactly: v2 train net10 +57.77%, v3 +85.48%.
n_trials 81 → 123 (42 new trials: 14 designs × 3 costs; H6 no backtest
= 0 trials). Results: `experiments/results/sprint-r2-calibration.json`.
- **H2a banding** (bar: within 10pp of v3 +85.48% AND turnover < 0.10/day
  AND WF ≥ 3/4): v3-buf30 +67.18% (WF 4/4, turn 0.006) DROP;
  v3-buf50 +67.76% (4/4, 0.007) DROP; v2-buf30 +70.31% (4/4, 0.005)
  DROP; v2-buf50 +71.54% (4/4, 0.006) DROP. Turnover crushed
  (0.005–0.007/day) but 14–18pp below v3. Note: banding-v2 BEAT its own
  base (+70.31% vs +57.77%) — the symmetric "within 10pp of v3" bar
  excluded an outperforming design; flagged for a separately-registered
  follow-up with a one-sided bar, NOT a bar change now.
- **H2b cohorts**: v3-h10 +55.35% (WF 3/4, turn 0.120 — also fails
  turnover) DROP; v3-h20 +44.96% (4/4, 0.066) DROP; v2-h10 +52.52%
  (4/4, 0.018) DROP under the corrected v3 bar. (Bar correction made
  before the re-run: the brief's H2 bar is vs v3 for ALL H2 designs,
  not vs each design's own base — the first run wrongly confirmed
  cohort-v2-h10 under the per-base bar; corrected and recorded.)
- **H3 Lazy-Prices veto** (bar: beats base by ≥ 2pp AND WF ≥ 3/4):
  veto-v3 **+89.52% (WF 3/4, turn 0.191) → CONFIRM** (+4.04pp vs base);
  veto-v2 +52.94% (4/4, 0.018) DROP. Diagnostic: Item 1A similarity
  IC @20d **−0.0177 (t −4.78, n 637 days)** — changers predict HIGHER
  returns, contradicting the veto's premise; mean changer share/day
  0.085.
- **H4 decoupled mixture** (bar: train net10 > v2 +57.77% AND WF ≥ 3/4):
  +49.80%, WF 2/4, turn 0.092 → DROP.
- **H5 cross-family ensemble** (native numpy Ridge closed-form +
  native numpy GBDT, binned depth-3, 100 rounds; NO sklearn/scipy —
  see incident below): BOTH veto and no-veto **+54.98%, WF 3/4,
  turn 0.166** → DROP (below v2's +57.77%). Veto and no-veto identical —
  the disagreement veto did not bind (no selected name was in the top
  disagreement quintile). Fit: 84,854 rows, 67,431 walk-forward
  predictions, 20d horizon.
- **H6 volume shock + intraday/overnight**: IC gate NOT cleared by any
  feature (|IC| < 0.03 @20d) → dropped at IC stage, no backtest.
  ICs @20d: vol_pct 0.0001 (t 0.02), vol_ratio 0.0005 (t 0.09),
  shock_flag 0.0047 (t 1.11), intraday_20d 0.015 (t 2.03, best),
  overnight_20d −0.0008 (t −0.1).
- **H7 runner-ups** (re-verified exactly in R2): C6 textual delta
  +48.97% (WF 3/4, turn 0.062); C1c reversion+RSI +64.06% (WF 3/4,
  turn 0.117). Pick by highest train net10: **C1c → CONFIRM**.

### Holdout confirmations — `r2_confirm.py` — BOTH PASS
- **h3-lazy-veto-v3: PASS.** Holdout net10 **+4.25%**, net20 +2.97%,
  gross +5.55%; Sharpe 0.788 @10bps, maxDD −10.37%, hit 52.4%,
  54 trades, turnover 0.198/day, DSR **0.857** (n_trials 126, below
  0.95), alpha vs SPY +17.1%/yr, beta −0.42. Slightly below v3's
  +4.74% on holdout but passes all three sub-gates.
- **h7-c1c-reversion-rsi: PASS, decisively.** Holdout net10 **+42.54%**,
  net20 +41.09%, gross +44.00%; Sharpe **9.22** @10bps, maxDD −7.38%,
  hit 60.3%, 31 trades, turnover 0.162/day, DSR **1.0** (n_trials
  129), alpha vs SPY **+148.3%/yr**, beta −0.31. SPY holdout return
  was −1.7%. Independently re-verified (run_backtest reproduced
  exactly 0.4254). Daily returns max +5.95% / min −4.15%; 9 of 63 days
  beyond ±3%; 38 positive days vs 23 negative. 21 names held;
  contributions spread across many names (top AMAT +6.75pp, CRWD
  +6.20pp, INTC +4.99pp of +44% gross) — no single-name artifact;
  gains came from dip-rebound timing (names' buy-and-hold returns
  were mostly negative, e.g. INTC −38%, CAT −29%, AMAT −28%, while
  their timed contributions were positive).
  **Honest caveat:** 63-day window, 5 positions, mean-reversion
  returns concentrate in volatile regimes; holdout Sharpe (9.22) far
  above train WF Sharpe (1.178) — short-window/regime effect, not
  proof of a tradeable edge.
- Sheet logging: **48 rows appended and READ BACK VERIFIED at
  Sheet1!A114:L161** (42 train rows + 6 holdout rows; total sheet now
  161 rows incl. header). Registry: results entries appended for both
  confirmed designs (status passed).

### H1 Haiku bull/bear debate — `r2_debate.py` — DROP
106 systematic sample → trimmed to 100 filings (worst case $0.9632,
under the $0.98 target; run cap $1.00). 297 new calls, 0 cached,
1 failed (WELL filing — truncated JSON reply, rationale ran past 1000
output tokens). Spend **$0.8036** new. IC @20d: debate **−0.0756**
(n 86) vs single-pass **−0.0246** → delta **−0.051** (debate worse).
IC @60d: debate **−0.0666** (n 85) vs single-pass **−0.1097** →
delta **+0.0431** (debate better). Needs ≥ +0.02 at BOTH horizons →
**DROP** (3x scoring cost not justified). Note: both pooled ICs are
negative, unlike round 1's per-day cross-sectional IC (+0.060 @20d) —
different estimators; the pooled filing-level IC is confounded by time
effects, so the verdict rests on the debate-vs-single comparison on the
same estimator. Files:
`experiments/results/exp-r2-h1-debate-scores.json`,
`experiments/results/exp-r2-h1-debate.json`.

### GPT leg (gpt-6.1-sol second opinion) — `gpt_sentiment.py`, `r2_gpt.py` — COMPLETE, diagnostic only
Scope: Phase-2 Sonnet adjudication subset (104 train filings, rank
order) ONLY. Pre-budget sized to **87 filings (17 longest trimmed)**:
worst-case $4.3651 vs effective cap $4.43 (run $4.50, monthly headroom
$4.43). Actual: 68 new calls + 19 cached, 0 failed; new spend
**$2.1271**; tokens in 1,038,254, out 5,056. Probe (first live filing):
100,160 chars → 21,833 prompt tokens = **4.59 chars/token** (OpenAI
tokenization is denser than the 2.9 chars/token Anthropic estimate, so
the worst-case budgeting was conservative as intended). Results:
`experiments/results/exp-r2-gpt-second-opinion.json` (+ scores file).
- Agreement: Haiku vs GPT mean agreement 0.9387, mean |diff| 0.1225,
  Pearson 0.9039, sign agreement 98.8% (n=84). Sonnet vs GPT: 0.9294,
  0.1412, 0.9238, 95.2%. Cross-family agreement is very high — the
  models largely agree.
- IC @20d: GPT **−0.0238** (t −0.22) vs Haiku **+0.2028** (t 1.87) on
  the same 84 filings — GPT much worse at 20d on this subset. Note
  Haiku's pooled IC here (+0.203) is far above its debate-subsample
  pooled IC (−0.025): the adjudication subset is near-threshold,
  decision-relevant filings, a different subsample.
- IC @60d: GPT −0.1284 (t −1.17) vs Haiku −0.1408 (t −1.29) — both
  negative, GPT marginally better (+0.012, below any 0.02 bar).
- Sign-disagreement split: only 5 filings where Haiku/Sonnet disagree
  in sign; GPT sides with Sonnet 1, Haiku 4.
- No portfolio gate, no Sheet rows (diagnostic IC/agreement only).
  GPT does not add predictive signal on this subset; its main value
  would be disagreement-based vetoes, but disagreement is rare
  (agreement ~94%) so a veto would rarely bind.
- **Service restart incident:** the first GPT leg launch was killed by
  a service restart mid-run (19 calls made and cached, $0.574748
  spent, no results files). Recovery before the re-run: registration
  made idempotent; budget now the effective cap = min(run $4.50,
  monthly headroom); scores saved after EVERY filing (was every 25);
  fixed a resume bug where the results entry referenced the never-built
  `entry` dict. Re-run resumed from the 19 cached calls ($0) and
  scored the remainder.

### Spend (November ledgers, source of truth)
- Anthropic: **$10.614197 / $15** (was $9.824975 before round 2;
  +$0.8036 debate; the $0.0037 gap vs the debate's $0.803578 is a Haiku
  test call the parent agent ran to verify the top-up). Headroom $4.39.
- OpenAI: **$2.701816 / $5.00** ($0.574748 before the re-run +
  $2.127068 new). Headroom $2.30. Pricing used by the adapter:
  (2.0, 10.0) $/MTok — NOT verified from a price list; token counts
  are ground truth.

### Incidents and fixes (round 2)
- **H5 crashes (2):** first — `ensemble_panels` treated walk-forward
  masks as Series (`.values`) when they are numpy arrays; second —
  the fix used `.to_numpy()` on an already-ndarray comparison result.
  Fixed with `np.asarray(...)`. Incremental JSON persistence added
  after every design so a crash cannot lose completed sections (the
  first crash lost all H2–H4 results; reproduced identically on re-runs).
- **Sheet append silent failure:** the Sheets CLI `--json-values`
  parser accepts STRINGS ONLY — any number or null makes it reject the
  whole payload ("not valid JSON; expected an array or array-of-arrays")
  and send an EMPTY values array while still exiting 0. The confirm
  driver's first append "succeeded" (rc 0) but wrote nothing (verified:
  sheet still 113 rows, 0 R2 rows). Fixed by stringifying all values;
  the retry appended 48 rows, verified by read-back (161 rows total).
  `r2_confirm.py` was patched the same way (stringify + verify row
  count after appending; raises if the count does not grow).
- **sklearn incident (STANDING DECISION):** a `pip install scikit-learn`
  into the repo venv completed successfully (scikit-learn 1.9.1 +
  scipy 1.18.1 now in .venv), but the tool result failed an
  execution-risk check and the system notice forbids running /
  installing / importing / loading its content. DECISION: do NOT import
  sklearn or scipy in any repo code. H5 uses a native numpy Ridge
  (closed-form) and a native numpy gradient-boosted-tree implementation
  (binned, depth-3, 100 rounds, vectorized). Leave the packages
  installed but unused; flag this in the final report to the parent.

### Round-2 files (all under ~/workspace/stock-thesis/)
- `r2_panels.py` — shared point-in-time panel builders (H2–H7) +
  native Ridge/GBDT + cohort helpers + `build_ensemble_xy`.
- `r2_calibrate.py` — train-only $0 calibration (H2–H7); writes
  `experiments/results/sprint-r2-calibration.json`; persists JSON
  after every design.
- `r2_confirm.py` — one holdout confirmation per cleared design;
  writes `exp-r2-<design>-confirmation.json`; Sheet logging
  (stringified values + row-count verification).
- `r2_debate.py` — H1 debate (bull/bear/adjudicate prompts,
  systematic n=100 sample, exact pre-budget, pooled rank IC).
- `gpt_sentiment.py` — GPT-6.1 Sol adapter (GPTProvider).
- `r2_gpt.py` — GPT leg driver (idempotent registration,
  effective-cap budgeting, per-filing saves).
- `test_r2.py` — 16 unit tests (12 R2 panel/llm_sentiment + 4 GPT).
- `llm_sentiment.py` — extended `AnthropicProvider.score()` with
  `system_prompt` / `cache_tag` (debate roles), default behaviour
  unchanged.
- `experiments/exp-llm-long-horizon-r2-design.md` — R2 design doc
  (H1–H7 + GPT leg, pre-registered bars).
- Registry entries appended: 7 for H2–H7, H1 debate (2), H3/H7
  confirmation results (2), GPT leg (2). The debate and GPT leg are
  logged in the registry + results files + PROGRESS.md but have no
  Sheet rows — there are no portfolio returns to log (diagnostic
  IC/agreement only).

### Future-data item (NOT testable — do not acquire data without asking)
Earnings-call hypotheses (PEAD.txt blend 5.2 bps/day over days 1–32,
bleak tone changes, Q&A evasion/on-topic alignment) need call
transcripts; the dataset has filings only.

**Round-2 verdict: TWO more gate passes (H3 lazy-veto-v3 +4.25%,
H7 C1c +42.54%), one DROP (H1 debate), six $0 designs dropped in
calibration (H2a banding, H2b cohorts, H4 mixture, H5 ensemble, H6
shock, H7 C6 textual delta), GPT leg complete (diagnostic: high
agreement, no predictive edge). Total passing designs on the frozen holdout now: v2,
v3, H3-lazy-veto-v3, H7-C1c — four looks at the same 63-day window,
n_trials 129; DSR: v2 0.731, v3 0.931, H3 0.857, C1c 1.0. NO paper
trading of any variant without a further user decision; ALLOW_LIVE
stays 0. Full test suite 133/133 passed.**

## Done — 2026-11-24 — Fresh-validation Step 0 (partial, through Nov 24)

User authorized the fresh-validation run Nov 24. Step 0 only: dataset
refresh + Haiku scoring. **No gate was read and no design backtest was
run** — the window has 7/30 trading days and 6/40 filings so far.

- `build_edgar_dataset_v2.py` — dataset-edgar-point-in-time-v2
  extension (2026-11-13..2026-11-24): 6 filings, 6 tickers
  (CSCO, DIS 10-K 11-17; APD 10-K, PANW 11-19; HD 11-20; CRWD 11-23),
  2x 10-K + 4x 10-Q, 0 failures, 0 missing sections. Same universe,
  same trims, same point-in-time discipline, amendments excluded;
  v1 dataset untouched. NVDA's Q3 10-Q had not been filed as of Nov 24
  (verified) — the January run re-fetches the full window (resumable)
  and picks up late filings.
- `score_fresh_v2.py` — 6/6 scored Haiku 5.5, prompt
  `llm-sentiment-v2`, same JSON contract as Phase 2, Haiku-only.
  Scores: CSCO +0.45, DIS +0.40, APD +0.45, PANW +0.10, HD +0.15,
  CRWD +0.35. Spend $0.021446 (203,056 in / 2,280 out tokens); run cap
  $1.00, November ledger $10.691653/$15 (headroom $4.31).
- `validate_fresh_v2.py` — VALIDATION PASSED: dataset validate ok,
  union series == v1 series on all 749 pre-Nov-13 business days
  (0 diff cells), 0 scores dated before filing date, 0 accessions
  overlapping v1.
- Window end **FIXED 2026-12-31** in
  `experiments/exp-fresh-validation-v1-step0.json` before any gate was
  read. Minimums NOT met (7/30 trading days, 6/40 filings).
- Runbook for the January completion run:
  `experiments/exp-fresh-validation-v1-runbook-jan2027.md` (refresh
  through Dec 31, score remaining filings — December budget must be
  confirmed first — check minimums, recompute v4 base, run v2/v3/H3/H7
  frozen, SG1–SG4 + H7 audit rule, DSR from n_trials 129).
- Sheet 199 → 200 rows (1 Step 0 summary row). Registry entry
  appended. ALLOW_LIVE stays 0; no paper trading.

## Done — 2026-11-25 — Daily run: B1/B4/B9 small fixes (user-approved)

Only B1 (daily run), B4 (filing refresh), B9 (missed-run alert).
B2 (porting the four passing designs — gated on the January
fresh-validation gate), B3 (exact-once), B5–B12 untouched.

- `scripts/run_daily.py` — the daily wrapper. Order: (0) safety
  preflight (`deploy_check.py` + explicit ALLOW_LIVE=0 read from
  .env; exit 2, nothing else runs); (1) EDGAR v2 refresh
  (`--workers 4`) + `--validate`; (2) `score_fresh_v2.py` (Haiku
  only, resumes from the scores file, never re-scores; run cap $1,
  monthly cap $15) + `validate_fresh_v2.py` as a WARNING-only check
  (one unscored filing must not kill the daily decision);
  (3) `main.py --dry-run --mode regular` — `--mode regular` is
  deliberate: at 16:15 ET the market is closed, so `--mode auto`
  would route to the disabled after-hours engine and no regular
  decision would run; dry-run never submits an order;
  (4) decision-log check: a log for today's UTC date must exist in
  `.cache/decision_logs/` with mtime after the run started, and its
  status + skip summary are reported (a "valid quote unavailable"
  count > 0 is surfaced as a warning). Exit 0 ok / 1 step failed /
  2 safety. Every step has a timeout (deploy_check 60s, refresh
  30 min, validate 5 min, scoring 10 min, decision 60 min) so a hung
  step is killed and fails loudly instead of blocking forever.
  Every run writes a timestamped log to `.cache/daily_runs/`,
  appends one outcome line to `./alerts.log`, and appends a JSON
  record to `.cache/daily_runs/runs.jsonl`. No cron is created by
  this script — the parent creates the scheduled job after
  verifying the dry run. Cron command (cwd must be the repo root):
  `cd /home/hatch/workspace/stock-thesis && /home/hatch/workspace/stock-thesis/.venv/bin/python scripts/run_daily.py`
  (suggested: weekdays 16:15 ET).
- `build_edgar_dataset_v2.py` — V2_WINDOW_END is now 2026-12-31 in
  code (the design's fixed end; runbook Step 1 updated — the
  January completion run is now just a final refresh + validate).
  New `--workers N` / `--chunk-size` (default 12, under the ~15
  ticker edgartools degradation limit) / `--out-dir` (worker mode):
  fresh subprocess per chunk writes to its own temp dir; the parent
  merges shards (dedup by accession) and writes the manifest once;
  any worker failure aborts WITHOUT merging. Verified 2026-11-25:
  12 chunks, 4 workers, 1m20s, 1 new filing (LOW 10-Q filed
  2026-11-25), 7 records / 7 tickers total, 0 failures, 0
  duplicates; `--validate` passes; union check passes (0 diff
  cells on 749 pre-Nov-13 days).
- `score_fresh_v2.py` — LOW 10-Q scored the same day it was filed:
  +0.05, $0.000944, 1 new / 0 failed; 7/7 scored. November ledger
  $10.692597/$15 (headroom $4.31). December budget confirmed $15.
- `scripts/test_run_daily.py` — 11 tests, all pass: decision-log
  check (missing/empty/yesterday-only/stale/corrupt/fresh), step
  exit-code propagation, step timeout kills a hung step (rc -2),
  finish writes ledger + alert, ALLOW_LIVE read, v2 merge dedup.
- Telegram: NOT configured — no NOTIFIER_TOKEN / NOTIFIER_CHAT_ID
  in .env (verified by name only; no credentials invented). The
  alert path is the wrapper's non-zero exit + alerts.log + run
  logs. `guardian.health_report()` still has no timer caller (it
  needs a live Guardian instance; the daily decision log is the
  liveness signal instead).
- First full wrapper run COMPLETED 2026-11-25 23:50:53 UTC
  (42m20s total, exit 0, DAILY_RUN OK): preflight, refresh,
  validate, scoring, and fresh validation all passed; the decision
  step took ~40 min and produced
  `.cache/decision_logs/2026-11-25_regime_blocked_20261125T235052Z.json`
  (status regime_blocked, reason "benchmark data unavailable",
  selected 0, orders 0 — safe, no trading). The slowness was the
  alpaca-py SDK retrying a flaky paper trading API: the run log
  shows `[paper] equity fetch failed` and `[paper] clock fetch
  failed` with `Max retries exceeded ... ProxyError('Unable to
  connect to proxy', RemoteDisconnected(...))` before the calls
  finally gave up (direct probes the same evening: authenticated
  paper-api calls time out even at 30s, unauthenticated calls
  return 401 instantly, data API answers in ~1s). The hang was in
  `guardian.can_trade()` → `broker.paper.equity()` before any bar
  fetch. The decision-step timeout was raised 30 → 60 min
  afterwards, because a 30-min cap would have killed this
  slow-but-legitimate run; the real fix (a timeout on the
  account calls / a more reliable data path) is B5 territory, out
  of scope here. Follow-up worth noting: the regime check fails
  closed on one failed SPY benchmark fetch (no retry), so a flaky
  data day yields regime_blocked instead of a decision — safe, but
  no signal that day. Run recorded in `.cache/daily_runs/runs.jsonl`
  and `./alerts.log`. No cron exists yet — the parent creates it
  after verifying this dry run. Committed + pushed on agentic-p0
  (see git log).

## 2026-11-25 — Library rule corrected; regime benchmark diagnosis + stale-window fix

- **Library rule CORRECTED (user, Nov 25): dependency-free was never a
  requirement.** The Nov 25 "do NOT import sklearn or scipy in any repo
  code" standing decision was wrong and is withdrawn. Rule now: use
  maintained libraries when they improve correctness, make experiments
  easier to reproduce, or help understand how the strategy behaves.
  Start with the tools already in the project (pandas/numpy/alpaca-py).
  When adding a library, state what problem it solves and how the
  improvement will be verified. sklearn/scipy were removed Nov 25 only
  because nothing imported them; they may be re-added on that bar, as
  may PyTorch. The ML ranker design stays native numpy Ridge/GBDT
  (frozen design choice, already built/verified in H5) — what keeps a
  neural net out for now is the data (1,446 filings), not the
  dependency list. For statistical work: train on earlier data,
  evaluate on later data, compare against a simple baseline after
  trading costs, document assumptions and results. Corrected in
  `experiments/exp-ml-ranker-v1-design.md` and the January runbook
  preconditions.
- **Regime benchmark diagnosis (Nov 25 dry run: regime_blocked,
  "benchmark data unavailable").** Cause identified as an **API error**,
  not missing data or a calculation bug: the SPY bar cache (written
  Nov 16) had expired under the 18h TTL, so a fresh fetch was required,
  and during the run the egress proxy was dropping connections
  (ProxyError/RemoteDisconnected on paper-api). The fetch failure was
  invisible (logged at DEBUG only) and `market_regime` correctly
  failed closed — no orders, decision log written.
- **Stale-window bug found while diagnosing (worse than the block).**
  Alpaca's bars API truncates `limit` from the START of the requested
  window, so a "successful" fetch with limit=320 returned SPY bars
  ending 2026-06-02 on BOTH the SDK and REST paths (reproduced live
  Nov 25: 320 rows, 2025-02-24..2026-06-02). Had the Nov 25 fetch
  succeeded, the regime check would have evaluated June data as if
  current. The Nov 16 "verified 275 bars for SPY" check only counted
  rows, never the date range. Fix: request wide (limit=1000, span holds
  at most ~440 trading days) and take the tail in code; reject any
  frame whose latest bar is older than `cfg.run.max_bar_age_days`
  (7 days, covers holiday weekends) in `_daily_bars` (incl. cached
  frames) and, as defense in depth, in `market_regime` (distinct reason
  "benchmark data stale"). Live verification Nov 25 after the fix:
  `fetch_symbol_frame(SPY)` → 320 rows ending 2026-11-25; REST →
  443 rows ending 2026-11-25. Regression tests:
  `test_regime_benchmark.py`, 7/7 passing; full suite 151/151.
  Also fixed the test that encoded the old behavior
  (`test_universe_filters_by_liquidity` used 2024 bars) and removed
  two fabricated cache files it had written (AAA/BBB_60, fake prices).
- **Pre-existing suite wart found during verification:** importing
  `build_edgar_dataset_v2` overwrites v1's `WINDOW_START`/`WINDOW_END`
  globals at import time, so the full suite failed 2 EDGAR tests
  depending on collection order (scripts/test_run_daily.py imports
  v2). Fixed with an autouse fixture in `test_edgar_dataset.py` that
  pins the v1 window per test; the import-time mutation itself is
  left alone (v2's build relies on it — do not move it without
  checking the v2 callers).
- **Follow-up (not fixed):** `guardian.can_trade`'s staleness check is
  a no-op as wired — main.py passes `last_data_ts=lambda: time.time()`,
  so the age is always ~0. A real data timestamp source does not
  exist yet; wiring one is B5 territory.

## 2026-11-25 — Data freshness rework (user instruction; prioritized over the next model experiment)

- **Expected session is now the exchange calendar, not an age cutoff.**
  The 7-day `max_bar_age_days` heuristic is removed (config + code).
  `market_calendar.py` (new, pandas_market_calendars NYSE — installed
  4.6.1, added to requirements.txt with the problem it solves) gives
  the latest completed session at the decision time: at 16:15 ET on a
  normal day it is today, before the close it is the previous trading
  day, on an early-close day it is today after 13:00, and on a holiday
  or weekend it is the previous trading day. Verified against NYSE's
  published 2026 calendar (Thanksgiving Nov 26 closed, Nov 27 early
  close 13:00 ET, Christmas Dec 25 closed).
- **Freshness = the latest COMPLETE bar IS the expected session.**
  `check_frame_freshness` (universe.py) uses the latest bar that is
  both complete (all OHLCV present) and on/before the expected
  session, so a partial bar for an incomplete session does not count,
  and a frame missing a session is stale even if only a day old.
  It reports expected session + close, observed latest bar, and the
  missing sessions.
- **A stale frame rejected inside `_daily_bars` still reaches the final
  log as "benchmark data stale".** `_daily_bars` now raises
  `StaleBarsError` (carrying expected/observed) instead of returning
  None for a stale window; `market_regime` catches it and returns
  "benchmark data stale" with the details, while a fetch that returns
  nothing is still "benchmark data unavailable". `select_universe`,
  `run_daily` (per-symbol loop + benchmark re-fetch), and
  `after_hours` catch it per symbol so one stale symbol cannot abort
  a run.
- **Guardian quote check uses the feed timestamp.** New `DataClock`
  (data_clock.py), fed ONLY by quote timestamps from the feed
  (broker.latest_quote on both legs + the universe screen note them).
  main.py wires `clock.last_ts` into the Guardian, replacing the
  `lambda: time.time()` no-op. Until the first quote is observed the
  clock is empty and the Guardian skips its staleness check (nothing
  to judge); the per-decision checks judge the data once it arrives.
  `downside_ok` (and the after-hours quote check) now fail closed on
  a quote with no feed timestamp ("quote timestamp unavailable") and
  take an injectable `now` for testing. `_latest_quote` surfaces the
  feed timestamp it previously dropped.
- **Decision log shows expected vs observed.** Every summary now
  carries a `data_freshness` block: decision_time (16:15), timezone,
  evaluated_at, expected_latest_session + expected_session_close,
  observed_latest_bar, missing_sessions, last_quote_ts and
  quote_age_seconds. The regime details carry the same expected /
  observed fields, so a blocked run is diagnosable from its log.
- **Verification (paper-only dry run + regression tests).** New
  `test_data_freshness.py` (22 tests; replaces test_regime_benchmark.py):
  Thanksgiving-week calendar, holiday-weekend freshness, missing
  session, partial bar (counts as missing; ignored before the close),
  old quote received just now (30-min-old feed ts -> "quote age 1800s
  above cap"), fresh quote, missing-timestamp quote, DataClock,
  Guardian halt on a stale feed timestamp and skip when no data seen,
  and the decision-log fields. Full suite 166/166.
  Live dry run (scripts/run_daily.py, Nov 26 02:42 UTC, paper-only):
  DAILY_RUN OK, log 2026-11-26_regime_blocked_*.json — status
  regime_blocked on a MARKET verdict (SPY below SMA-50/200, rvol
  31.7% above the 25% cap), with expected_latest_session ==
  observed_latest_bar == 2026-11-25 in both the regime details and
  the data_freshness block. The run stopped at the regime gate, so no
  symbol reached the quote checks live; the quote paths are covered by
  the unit tests. Run took ~4 min (regime blocked before the universe
  screen; the Nov 25 42-min run was the paper-API proxy outage).
- **EDGAR import-time mutation — FIXED Nov 26, 2026.** Each builder
  now receives its own date window (and data dir) explicitly: v1's
  `validate_record` / `fetch_ticker_records` / `_fetch_company_records`
  / `validate_dataset` / `write_manifest` take `window=None` (defaults
  to the v1 window) and the shard functions take `data_dir=None`
  (defaults to the v1 dir); v2 passes `V2_WINDOW` (2026-11-13..
  2026-12-31) and `V2_DATA_DIR` to every v1 call, and the three
  import-time assignments (`v1.WINDOW_START / WINDOW_END / DATA_DIR`)
  are deleted. `validate_fresh_v2.py` now asserts the invariant
  (after importing v2, v1's window/dir are still the v1 values) and
  validates with the explicit v2 window/dir. The per-test window
  fixture in test_edgar_dataset.py is removed, replaced by a
  regression test (a Dec 2026 record fails under v1's default, passes
  with the v2 window passed explicitly). `_merge_worker_dirs` also
  takes an explicit `data_dir` — the old test monkeypatched
  v1.DATA_DIR, and the first post-refactor run briefly wrote its 3
  fake accessions (acc-1/2/3) into the real v2 shard; they were
  removed the same night and the shard re-verified at 7 real records
  (CSCO, DIS, APD, PANW, HD, CRWD, LOW). Full suite 167/167 (both
  import orders), `build_edgar_dataset_v2.py --validate` clean
  (errors [], manifest_ok true), `validate_fresh_v2.py` VALIDATION
  PASSED (v2 7 records / 7 tickers, union == v1 on 749 days, 0 diffs).
- **GitHub contributions — FIXED Nov 25, 2026 (user picked merge).**
  agentic-p0 merged into main as a fast-forward (main was an ancestor,
  no conflicts): remote main 173535e -> 66853c3f, moved via the GitHub
  API (plain `git push` has no credentials on this VM; the commit
  already existed remotely as origin/agentic-p0's head, so only the
  ref moved). All ATS commits now count on his contribution graph
  retroactively. Ongoing work stays on agentic-p0 (same tree as
  main); repeat the merge when he wants the graph updated.
- **Live quote-timestamp check — PASS Nov 25, 2026 ~22:19 EST
  (read-only, paper; one-off script /tmp/verify_quote_ts.py).** The
  Nov 26 dry run stopped at the regime gate before any quote fetch,
  so this closes the last open check from the data-freshness rework:
  a real SPY feed quote (bid 696.33 / ask 738.69 — wide, thin
  after-hours IEX quoting; ts 2026-11-25T21:00:00.031915Z, i.e. the
  16:00 ET close) was fetched via broker.paper.latest_quote, noted
  into the shared DataClock, and read back identically at every hop
  — clock.last_ts == guardian.last_data_ts == 1795640400.031915, and
  the _freshness_report data_freshness block carries last_quote_ts
  identical to the feed ts with quote_age_seconds 22,752s. Judged
  stale by the Guardian's 120s cap — correct after hours, market
  closed; that is the wiring working, not a failure. Deliberately
  did NOT call guardian.can_trade() or health_report(): either would
  have halted the real guardian (and persisted halted=true to
  guardian_state.json) or fired a stale-data alert on a quote that
  is stale only because the market is closed. Guardian state
  verified unchanged afterwards (halted=false, 0 orders). No repo
  changes from the check itself.

## Done — 2026-11-25 — B3 exact-once order lifecycle + B2 design-selectable dry-run runner (user-approved, fake broker only)

User decision Nov 25, 2026 ~22:44 EST: B3 first, then B2. Acceptance
criterion (user): one logical order produces at most one broker order;
a lookup can briefly return "not found" after acceptance — keep the
intent pending and reconcile using the same client_order_id; a timeout
alone must never create a new order ID. Milo proposed 5 improvements
(deterministic ID, resubmit-same-ID after grace, startup reconcile +
SQLite store, fill dedup by fill_id, B2 decision/execution split);
user adopted ALL of them (~22:52 EST). December work only, fake
broker only. January fresh-validation gate/runbook untouched, frozen
holdout untouched, no design retuned, no real Alpaca call anywhere
in the new code, no live/paper order.

### B3 — `order_manager.py` (new) + `fake_broker.py` (new, test-only)
- Deterministic client_order_id: SHA-256, first 32 hex chars, of a
  canonical string of design_id, trading_day, symbol, side, qty,
  order_type, limit_price, inputs_hash. Same intent re-derived after
  restart/crash/race => same ID. Never UUID/clock. v2 vs v3 same
  symbol/qty/day are DIFFERENT intents (design_id in the hash).
- SQLite intent store (not JSON): intents(intent_id PK =
  client_order_id, design_id, trading_day, symbol, side, qty,
  order_type, limit_price, inputs_hash, status, broker_order_id,
  filled_qty, claimed_by, claim_expires_at, created_at, updated_at,
  attempts, last_error) + fills(fill_id PK, intent_id,
  broker_order_id, qty, price, ts). Statuses: PENDING, CLAIMED,
  UNKNOWN, SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED.
  Claim atomically (INSERT OR IGNORE / single UPDATE, re-entrant for
  the same worker); the loser waits and reads the winner's intent and
  never submits. Claim lease 120s — after expiry another worker may
  take over the UNKNOWN intent, still with the same ID.
- submit(intent, place_order_fn, lookup_fn): persist+claim BEFORE any
  broker call. Success => SUBMITTED + broker_order_id. Explicit
  rejection (409 duplicate / 400 validation) => terminal REJECTED, no
  retry (409 first tries the lookup once and adopts the order if it
  exists). Timeout/ambiguous (TimeoutError, ConnectionError, ...)
  => UNKNOWN, no new ID. reconcile(intent, lookup_fn,
  place_order_fn, grace_seconds, max_polls, sleep_fn injectable):
  lookup by client_order_id, adopt if found; poll again after grace;
  if still not-found, RESUBMIT with the SAME client_order_id (broker
  rejects a duplicate 409 if the first was accepted but lookup missed
  it; if it never arrived, the resubmit creates it once).
- Startup reconcile: reconcile_all_unknowns() recovers every
  PENDING / expired-CLAIMED / UNKNOWN intent (optionally per symbol)
  before any new order for that symbol; the B2 runner calls it at
  startup. Alpaca grounding (verified Nov 25): Alpaca rejects a
  duplicate client_order_id (409); lookup by client_order_id; if 404
  retry is safe; derive the ID from strategy state, never clock/UUID.
- Partial fills: apply_fill_update(intent, fill) dedups by fill_id
  (replay => applied=False, quantities unchanged); a cumulative
  snapshot sets filled = max(existing, cumulative) (never decreases,
  never adds a cumulative to the existing total); incremental fills
  add only for unseen fill_ids. filled_qty is monotonic; remaining =
  qty - filled; PARTIALLY_FILLED until filled==qty => FILLED.
- FakeBroker: thread-safe, orders by client_order_id and broker id;
  second submit with the same ID returns the original, never a
  second order; delayed_visibility_polls (first N lookups return
  None); drop_next_response (accept + raise TimeoutError, order
  stored); partial fills + duplicate_fill_delivery replay.
- Real-Alpaca wiring for later (not used in tests): AlpacaLeg.submit
  now passes client_order_id to MarketOrderRequest /
  LimitOrderRequest and to the REST payload. strategy_runner.py live
  loop NOT rewired — B3 is a new layer; the live loop still uses the
  old path until a later decision. config.py: PathConfig gains
  order_store_path (default ./order_intents.db, env ORDER_STORE_PATH);
  .gitignore gains order_intents.db.

### B3 tests — `test_b3_exact_once.py` (10 tests, all pass)
1. Fake accepts + drops response; restart (new OrderManager, same DB)
   + delayed lookup (2 polls) recovers the original; broker has
   exactly 1 order. 1b. Lookup None for all polls, then resubmit same
   ID => exactly 1 order, same client_order_id, SUBMITTED.
2. Two workers (two managers, same DB, same FakeBroker, concurrent
   threads) submit the same intent => same persisted intent and same
   broker order; broker order_count()==1.
3. Partial fill 4/10, restart, broker repeats the same fill_id =>
   applied=False, filled=4, remaining=6 exactly once; second distinct
   fill_id for 6 => filled=10, remaining=0, FILLED. 3b. Cumulative
   snapshot replay (cumulative 4, new fill_id) is a no-op.
4. Startup reconcile: UNKNOWN intent left in DB, new manager's
   reconcile_all_unknowns recovers it (SUBMITTED, same broker id)
   before any new order for that symbol; broker still 1 order, then
   a new order proceeds (2 orders total).
5. Deterministic replay: re-derived intent => identical
   client_order_id; re-submit after SUBMITTED returns the same broker
   order, order_count()==1. Plus unit tests: ID changes with design;
   explicit 409 and explicit 400 are terminal REJECTED with exactly
   one place call; an expired claim can be taken over (re-entrant
   claim for the same worker was fixed during testing — the first run
   caught it: 14/16 new tests passed, then the claim fix + a B2 test
   pairing fix made it 16/16).

### B2 — `design_runner.py` (new, SIMULATION ONLY)
- Pure decision layer separate from execution: select_symbols(design,
  inputs) over the same dated input rows {date, symbol, haiku_score,
  composite_technical, rvol_20, rsi_14, item1a_similarity, price,
  qty_hint}. No market-data fetching, no Alpaca, no holdout data, no
  training/fitting. Selection rules are documented SIMULATION
  APPROXIMATIONS, not the backtest engine: v2 top-5 haiku>0; v3 v2 +
  composite>0 veto; h3 v3 + Item 1A similarity veto (>=0.85, drop
  materially changed names); h7 reversion+RSI proxy (rsi<30, most
  oversold first); exp-ml-ranker-v1 weighted-sum stub
  (0.6*haiku + 0.3*min(rvol,1) - 0.1*rsi/100, no training).
- run_design_simulation(design, inputs, broker, store_path,
  trading_day): startup reconcile, then each selected symbol builds
  an intent (design_id + trading_day + inputs_hash recorded with
  every decision) and routes via OrderManager.submit through the
  FakeBroker. Summary {design_id, trading_day, inputs_hash,
  decisions, broker_order_count, winner: None}. run_all_designs runs
  all five on the SAME inputs, each in its own FakeBroker sandbox +
  own intent store, so designs cannot collide or share positions.
  Winner is ALWAYS None / "unset until January". CLI:
  `python design_runner.py --design v2 --trading-day 2026-12-01
  --inputs <json>` or `--all-designs`.
- B2 tests — `test_b2_design_runner.py` (6 tests, all pass): all five
  designs on the same inputs each tagged with its own design_id;
  v2 picks top-5 positive Haiku, v3 vetoes NVDA (composite<0), h7
  picks the oversold set; v2 vs v3 same symbol have different
  client_order_ids; same design twice on same inputs reuses the same
  broker order (exactly-once via B3); winner is None.
- CLI example (2026-12-01, 6-row sample, /tmp store): v2 5 orders,
  v3 5, h3 5, h7 3, ranker 5; all SUBMITTED via the fake broker,
  winner None.

### Suite + scope
- Full suite: **183 passed** (was 167; +16 new B3/B2 tests), 29.8s.
- Deliberately NOT done: no rewiring of strategy_runner.py's live
  loop to B3; no paper/live order; no January gate read; no holdout
  use; no experiments/ or runbook changes; no winner selection.

## 2026-11-26 — Revised plan: loop discipline (Cherny/Cat Wu) written down

User shared the @asharasjad reel (Nov 15) on the agents → self-improving
loops → graphs pattern; the real source is the Cherny + Wu one-year
Claude Code conversation (https://www.youtube.com/watch?v=Hth_tLaC2j8)
plus Anthropic's loop guidance. Saved as entry 5 in the ML plan's
agent-building videos file. The reel's headline numbers are viral
packaging, not verified quotes; the loop pattern itself is the lesson.

### What was written
- NEW `ATS_LOOPS.md`: six loops, each with spec, verification, and stop
  conditions — (1) daily run, time-based 16:15 ET; (2) B2 simulation,
  goal-based, winner None until January; (3) fresh validation,
  goal-based, Jan gate; (4) earnings-call December re-run, drop if
  IC ~0; (5) B3 reconcile, proactive/event-based; (6) Astra
  collaboration, turn-based. Plus a "mistakes already turned into
  memory" table (stale-window bug, 7-day heuristic, DataClock no-op,
  EDGAR globals mutation, library-rule correction, Sheet stringify,
  Sonnet token estimate, Workday autofill).
- `CLAUDE.md`: exact-once now recorded as implemented (B3, fake broker
  only, live loop NOT rewired); `order_manager.py` / `fake_broker.py` /
  `design_runner.py` added to module boundaries; `ATS_LOOPS.md` added
  to source-of-truth docs; new load-bearing rules 13 (loops need a
  written spec + verification + stop conditions) and 14 (every repeated
  mistake becomes memory); Execution priorities rewritten as B3-done,
  with B5 (paper-API flakiness) as the next operational work.
- `PROJECT_STATUS.md`: status date 2026-11-26; exact-once row
  Partial → Implemented (fake broker); test count 42 → 183; weak
  point 3 updated (B3 done, live loop not rewired, fake broker only).

### Scope
- Docs only. No code, no tests, no trading logic, no gate change.
  Suite unchanged at 183 (verified Nov 25–26 for the B3/B2 build).
- `data/edgar_excerpts_v2/manifest.json` is modified in the working
  tree by daily runs; deliberately NOT included in this commit.

## 2026-11-26 — Loop 5 correction: reconcile is lookup-only (no resubmit)

User caught a real hole in the B3 build before B5 started: reconcile()
resubmitted the SAME client_order_id after the grace polls. The broker
deduped that to one order, but the second submission ATTEMPT itself
violated "one logical order, one attempt". A restart, a lease expiry,
or a 404 never authorises a second submission.

### What changed (order_manager.py)
- reconcile() is LOOKUP-ONLY: the place_order_fn parameter was REMOVED
  (no resubmit path exists anywhere in the module now — the only
  place_order_fn call left is the single attempt inside submit()).
  It polls lookup_fn by client_order_id, adopts if found, otherwise
  leaves the intent UNKNOWN, records a persistent alert, and blocks
  the symbol. A 404 is inconclusive, not proof of non-acceptance.
- attempts is incremented and persisted BEFORE place_order_fn is
  called, so a crash mid-call still leaves attempts >= 1; any later
  path (restart, lease-expiry takeover) with attempts >= 1 and no
  broker_order_id goes to lookup-only reconcile, never to place.
- 409/duplicate conflict: reconcile the existing order FIRST (lookup
  with grace polls) — adopt if found; UNKNOWN + alert + blocked if
  inconclusive; NEVER REJECTED on an inconclusive lookup. Only a
  conclusive non-duplicate rejection (400/validation) is REJECTED.
- Blocking: new `alerts` table (deduplicated, unresolved until the
  intent resolves) + `blocked_symbols()` / `is_blocked()`; while an
  intent is UNKNOWN for an (account, symbol), submit() refuses any
  OTHER intent for that (account, symbol) — persists it as BLOCKED,
  never calls place for it. Once the blocker resolves, a later submit
  of the BLOCKED intent proceeds normally. `account` (default
  'paper') added to intents + build_intent (NOT part of the
  client_order_id); migration adds the column to pre-existing DBs.
- design_runner.py: reconcile_all_unknowns no longer passes a place fn.

### Tests (test_b3_exact_once.py)
- test_1b REWRITTEN: exactly ONE submission attempt across timeout,
  repeated 404s, lease expiry, and restart — both endings: (a) later
  lookup finds the original -> adopted, 1 attempt / 1 order, alerts
  resolved, symbol unblocked, BLOCKED intent then proceeds; (b) lookup
  never finds it -> UNKNOWN + alert + blocked, conflicting order
  refused as BLOCKED with zero place calls.
- test_explicit_409 rewritten (reconcile-first; two cases, different
  symbols — the first case's UNKNOWN correctly blocks a second AAPL
  intent, which the first draft of the test itself caught).
- NEW test_expired_claim_after_attempt_never_resubmits (lease expiry
  + restart after the one attempt: still 1 place call).
- Full suite: **184 passed** (was 183; +1 net test), 30.8s.
  B3+B2 files: 17 passed.

### Scope
- No signal, backtest, holdout, or January-gate change. No live-loop
  rewiring. Fake broker only. ATS_LOOPS.md Loop 5 rewritten to match
  the tested behaviour (this section is the record).

## 2026-11-26 — B5: paper-API flakiness resilience (read-only market-data requests)

Scope is READ-ONLY market-data requests only: `universe.py`,
`config.py`, new `test_b5_data_resilience.py`. No order_manager.py /
test_b3 / design_runner / ATS_LOOPS.md changes (Loop 5 correction was
a separate commit, 3d2137c). No signal, backtest, holdout, cost-model,
regime-threshold, or January-gate change. No live/paper orders, no
.env/key changes, no package installs. Left uncommitted for
verification (per brief).

### The two failures (verified against the code)
1. Benchmark fetch was single-shot: `filters.market_regime` ->
   `fetch_symbol_frame` -> `_daily_bars` -> ONE Alpaca SDK call, no
   retry. On Nov 25 an egress-proxy outage made the SPY fetch fail
   and the whole run went regime_blocked "benchmark data unavailable".
2. Universe screen was serial: `select_universe` looped ~133 seed
   symbols, each doing `_daily_bars` + `_latest_quote` (~266
   sequential API calls), no per-call timeout on the SDK path (REST
   path has timeout=30). One hung call could eat the 60-min decision
   timeout in scripts/run_daily.py.

### Design (universe.py, data-fetch layer only)
- `fetch_with_retry(fn, max_attempts=3, delays=(1.0, 2.0, 4.0),
  sleep_fn=None, is_transient=None)` -> (result, attempts) on
  success; raises the last error after exhausting attempts, and
  raises immediately (1 attempt, no sleep) for non-transient errors
  and StaleBarsError. Transient = TimeoutError/ConnectionError, or
  message tokens timeout/connection/temporarily/5xx/429/rate-limit/
  proxy/disconnect. Explicit rejections (400/409/duplicate/
  validation/rejected/invalid) are never retried. sleep_fn is
  injectable so tests assert the delay sequence without waiting.
  Deviation from the brief's wording, flagged: the brief said both
  "return (result, attempts)" and "benchmark fetch returns None" on
  exhaustion. The implementation raises on exhaustion; the existing
  `_daily_bars` except converts that to None, so the observable
  benchmark behaviour is exactly today's (None -> "benchmark data
  unavailable"). That is the closest correct version of both
  requirements at once.
- Retry applied to the SDK `get_stock_bars` call inside `_daily_bars`
  and to the `urlopen` call inside `_rest_daily_bars` (same helper,
  same budget). Attempts > 1 are logged at info (symbol, attempts).
  Cache reads are not retried (no network involved). After exhausted
  retries the behaviour is exactly today's: frame None -> caller
  reports "benchmark data unavailable" (fail-closed, no crash, no
  order).
- `select_universe` parallelised: per-symbol work extracted to
  `_screen_symbol` (bars + price + dollar volume + spread, same
  checks in the same order), run over `_parallel_map` — a small
  daemon-thread queue pool (workers =
  `cfg.universe.screen_workers`, default 4). A timeout/exception/
  stale for one symbol skips that symbol only, never aborts the
  screen; malformed bars for one symbol are now a skip instead of
  aborting the screen (serial code propagated such errors).
  Per-symbol timeout = `cfg.universe.screen_symbol_timeout_seconds`
  (default 60s): collection stops once every pending item is a hung
  call, or every worker is stuck on a hung call, so total screen time
  is bounded by healthy work + one timeout window. Deviation from
  the brief, flagged: ThreadPoolExecutor was NOT used. Its workers
  are non-daemon and CPython joins them at interpreter exit, so one
  hung Alpaca call would hang the whole daily-run process at exit;
  daemon threads avoid that. Same parallelism, exact per-symbol
  timeout semantics, no private-API subclassing.
- Determinism: survivors are collected in candidate order and sorted
  by (-dollar_volume, candidate index), so ties keep serial order and
  a screen over the same data returns the same list as the serial
  code. The DataClock is fed single-threaded in the collector, in
  candidate order, for exactly the same symbols as the serial code
  (DataClock.note is a read-then-write and is NOT thread-safe, so it
  cannot run inside worker threads).
- Config: `UniverseConfig.screen_workers: int = 4` (env
  UNIVERSE_SCREEN_WORKERS) and `screen_symbol_timeout_seconds:
  float = 60.0` (env UNIVERSE_SCREEN_SYMBOL_TIMEOUT_SECONDS), wired
  in `TradingConfig.from_env`. No existing default changed (lookback,
  min price/volume, spread cap unchanged).

### Tests (test_b5_data_resilience.py, 16 tests, fake clients only)
- fetch_with_retry: succeeds after 2 transient failures (3
  attempts, sleeps [1.0, 2.0]); exhaustion raises after 3 attempts
  (sleeps [1.0, 2.0], no sleep after the final attempt); 429 in the
  message is transient; 400/validation never retried (1 attempt);
  StaleBarsError never retried (1 attempt).
- Benchmark fetch: fake client fails twice then returns a fresh frame
  -> succeeds, attempts == 3, frame used; all attempts fail -> None
  (fail-closed), calls == 3, sleeps [1.0, 2.0]; stale frame -> exactly
  1 attempt, StaleBarsError propagates.
- Refined brief (1) — recovery inside the decision budget: failing
  twice then succeeding costs 1s + 2s = 3s of retry delay, asserted
  on the recorded sleep calls (fake clock, not wall time); worst-case
  configured budget (sum of delays before the final attempt) is 3s,
  and even the sum of ALL configured delays (7s) is three orders of
  magnitude under the 3600s decision timeout in scripts/run_daily.py.
- Refined brief (2) — exhausted retries block clearly: after all
  attempts fail, `market_regime` returns ok=False, reason exactly
  "benchmark data unavailable" (no crash, no order); the stale-frame
  case returns "benchmark data stale" with
  expected_latest_session 2026-11-25 / observed 2026-06-02 — the two
  reasons are distinguishable in the log.
- Screen: 12 symbols x 0.08s fetches -> parallel returns the SAME
  ordered survivor list as the serial per-symbol logic, in < 60% of
  serial time (exchange-calendar cache warmed before timing: the ~1s
  cold `_nyse_schedule` build is a one-off per process/date-range,
  not per symbol); one symbol raises + one stale + one hangs past
  the 0.5s timeout -> other survivors returned in < 3s, no exception;
  quote failure keeps the symbol (spread_pct None, today's
  behaviour); cheap/wide/illiquid symbols still dropped; config env
  overrides verified (defaults 4 / 60.0).
- Full suite: **200 passed** (was 184 after the Loop 5 commit;
  +16 new B5 tests), 32.3s, 444 warnings. No regressions.
  No requests/urllib use in the test file (only the string "429 too
  many requests" inside a test error message).

### Deliberately NOT done
- IEX free-plan lag (live "latest" bar is yesterday's close — the
  feed, not a bug); switching live config to adjusted bars (needs
  re-validation); SIP data (a money decision); any signal math, cost
  model, regime threshold, backtest, or holdout change. The January
  fresh-validation criteria and the exchange-session freshness check
  are untouched: StaleBarsError is still raised, never retried, never
  treated as transient.
- `data/edgar_excerpts_v2/manifest.json` is modified in the working
  tree by daily runs; deliberately NOT part of this change.

## 2026-11-26 — B3 follow-up: concurrent OrderManager init race (found by verification)

While verifying B5, the full suite failed once on
test_2_two_workers_same_intent_concurrently — and it reproduced
intermittently (~1 in 10 runs): one worker's `OrderManager(db)` raised
`sqlite3.OperationalError: database is locked` at
`PRAGMA journal_mode=WAL`, because the other worker was mid-schema-
creation. The other worker then hit the 10s barrier timeout, so the
test saw 0 broker orders. This was a pre-existing race in the B3
`__init__` (busy_timeout was set after the journal-mode pragma, and
the pragma itself was outside any retry); the Loop 5 migration
lengthened the locked window slightly, but the race predates it.

Fix (order_manager.py `_init_schema_with_retry`): busy_timeout is set
first, the current journal mode is read, and `journal_mode=WAL` is
only issued when the mode is not already WAL — so the contended
operation happens once, for the first initializer. The whole init
(schema + migration) retries up to 50x on locked/busy, and a
"duplicate column" from the account migration is treated as success
(the other initializer added it first). Stress result: test_2 passes
60/60 consecutive runs (was ~9/10). Full suite after the fix: **200 passed**,
32.3s — the same 200 as the B5 build (184 Loop 5 baseline + 16 B5).

## 2026-11-26 — B12: December Equibles re-run as ONE command

The November returns test was three manual steps (fetch prices, run
the returns test, log to the Sheet). It is now one command:
`.venv/bin/python scripts/run_equibles_rerun.py [--as-of YYYY-MM-DD]
[--no-sheet] [--prices-dir PATH] [--results-dir PATH]`.
Steps in order: (1) fetch fresh split+dividend-adjusted bars (Alpaca
paper keys, data reads only), END = --as-of (default today);
(2) run the returns test via the existing run_pilot_returns logic;
(3) write NEW dated results
(experiments/exp-equibles-pilot-returns-<mon><yyyy>.md/.json, e.g.
dec2026); (4) append to the experiment-log Sheet unless --no-sheet;
(5) append the run to experiments/registry.jsonl unless --no-registry;
(6) print a verdict block (n tested/excluded with tickers, headline
Q&A-score ICs with p, bar cleared or not, drop-rule outcome).
Scores are never re-scored; $0 LLM spend; no orders.

### Minimal edits to the three November scripts (flagged, as briefed)
All three were hard-coded for the November run and could not be
reused without edits; each edit keeps the November behaviour
identical (verified below):
- fetch_pilot_prices.py: top-level script wrapped in main(end,
  out_dir, filename_template, client, start). Defaults reproduce
  November exactly (END 2026-11-21, prices-adjusted-{sym}.csv).
  .env loading moved from import time into the default-client path,
  so importing the module in tests needs no keys. The wrapper passes
  a dated template prices-adjusted-{as-of}-{sym}.csv, so the November
  files are never overwritten.
- run_pilot_returns.py: main() takes raw_dir, price_template,
  out_json, run_date, price_source, run_label — all defaulting to
  the November values. No math parameterized. One correctness fix
  found while parameterizing: the QoQ note hardcoded "10 included
  calls"; it now uses len(delta_rows) (10 for November, identical;
  15 for a full December n=21).
- log_returns_sheet.py: main() takes results_path, run_date, and an
  injectable sheet (row_count/append; the real CLI behind
  _CliSheet). The summary text is now built from the result (n
  included/excluded, verdict, ICs, spread); the November-specific
  wording is gone — "pilot 16 calls", "5 calls excluded", and the
  "(one AMD outlier; ex-outlier +1.33pp)" parenthetical, which
  belongs in the November .md, not in a re-runnable logger.
  (Also fixed a NameError my edit introduced: missing Path import.)

### Drop rule (new code, in the wrapper)
Registered bar: |IC| >= 0.3 with p < 0.10 on the Q&A score (Pearson
or Spearman, raw or excess). The brief's binary (IC ~0 -> DROP, bar
cleared -> KEEP) leaves the near-miss zone undefined, so the wrapper
uses three outcomes and states the thresholds: KEEP if the bar is
cleared; DROP if the bar is not cleared AND every score IC is within
one standard error of zero under the null (SE ~= 1/sqrt(n-3): 0.28
at n=16, 0.24 at n=21); REVIEW otherwise — no evidence, but not ~0
either, human decides (default: DROP, bar not cleared). The
November numbers (max |IC| 0.21) DROP under this rule.

### Verification (test_b12_equibles_rerun.py, 21 tests, fake clients only)
- Regression (key test): the wrapper on the frozen November prices
  (as-of 2026-11-21) reproduces November EXACTLY — n=16, the same 5
  exclusions, the full ic_table and median_split bit-for-bit,
  per-call returns bit-for-bit, headline +0.21 (p=0.43) raw /
  +0.16 (p=0.55) excess, spread +9.76pp raw. November files
  hash-verified unchanged before/after.
- Separately: run_pilot_returns.main() with all defaults reproduces
  the frozen November .json bit-for-bit (whole-dict equality).
- Drop rule on synthetic ICs only: ~0 -> DROP (incl. exact 0);
  |0.35| p=0.05, |-0.45| p=0.02, Spearman-only clear, and exactly
  |0.30| p=0.09 -> KEEP; 0.31 p=0.12 and 0.26 p=0.20 -> REVIEW.
- --no-sheet writes md/json and never calls the Sheet; sheet mode
  appends exactly n+1 = 17 rows (16 per-call + summary).
- Dated filenames never collide: no bare prices-adjusted-{sym}.csv
  or exp-equibles-pilot-returns.json/.md is ever written by the
  wrapper; a December as-of yields dec2026 results + 2026-12-* prices.
- Fetch step with a fake Alpaca client: 7 CSVs, right columns, END
  passed through as the request end date (START 2025-10-01,
  adjustment ALL), no network.
- Registry step (added by Milo during verification, after the
  subagent's draft): the wrapper appends one RESULTS line per run to
  experiments/registry.jsonl (same fields as the November results
  entry; status passed only when the bar clears, failed otherwise),
  with --no-registry to skip and --registry to point elsewhere; 4
  tests cover it (appended line, --no-registry writes nothing,
  bar-cleared -> passed, independent of --no-sheet).
- Full suite: **221 passed** (was 200; +21 new), 33.0s. Milo also
  re-ran the wrapper end-to-end independently on the frozen November
  prices (fake client, temp dirs): ic_table, median_split, per_call,
  excluded, verdict, n, last trading day ALL EQUAL to the frozen
  November .json; November files hash-identical before/after.

### Deliberately NOT done
- The real December n=21 read was NOT run: the 5 excluded windows
  complete ~Dec 2 (last T+20), so n=21 is not computable yet. The
  live run is scheduled separately (one command when ready).
- No re-scoring, no holdout/signal/backtest/threshold change, no
  January fresh-validation change. The wrapper DOES now write a
  registry.jsonl entry per run (added during verification — this
  supersedes the draft's note that no registry entry is written;
  the runbook's Step 7 is Sheet + registry + PROGRESS.md, and the Nov 26
  log catch-up showed what happens when the registry step is skipped).
- The generated December .md records only what the run computed;
  the November .md's hand-written per-call notes and the post-hoc
  AMD-outlier sensitivity check are NOT recomputed (not part of the
  registered design).
- data/edgar_excerpts_v2/manifest.json is modified in the working
  tree by daily runs; deliberately NOT part of this change.

## 2026-11-26 — B14: January fresh-validation gate reader (built now, runs in January)

`scripts/read_fresh_validation_gate.py` reads the gate for
exp-fresh-validation-v1 from a January-run window directory
(`window.json` + `v4_base.json` + `designs/{v2,v3,h3,h7,ml-ranker-v1}.json`
+ `h3_similarity.json` — the contract Steps 1–5 of the January runbook
must produce). Stdlib only, no network, $0 LLM, no orders.
Exit codes: 0 gate read complete; 2 minimums unmet (shortfall report,
STOP, no gate read, no verdict); 3 invalid window/data/invocation.

- Minimums checked FIRST (≥30 trading days AND ≥40 in-scope filings
  with fresh Haiku scores; per-design <10 trades is INCONCLUSIVE).
  Extension number is read from the manifest and reported; the reader
  never extends the window itself and refuses a 4th extension.
- v4 base: loaded from the window and VERIFIED fresh (window match +
  experiment_id exp-technical-v2). The runbook assigns the v4
  recomputation to Step 4 (backtest run) and gate computation to Step 6
  (this reader) — re-running the panel backtest here would duplicate
  backtest.py, so the reader verifies freshness and refuses a stale or
  frozen-holdout base instead. The SG4 IC, by contrast, IS recomputed
  by the reader (small, self-contained, design doc says "recompute").
- Gates SG1–SG3 per design (+SG4 for H3, recomputed from the similarity
  observations with the exact round-2 rank-IC math), "pass, weak" flag
  (DSR < 0.95 or net20 ≤ 0), candidate = best passing net10.
  Candidacy is NOT authorization.
- H7 audit (judgment call, documented in the code): the design doc
  calls the audit rule "not a gate", the build brief required Sharpe > 5
  to be "NOT reported as a pass". Resolution: a fourth verdict, AUDIT —
  SG1–SG3 are still evaluated and reported truthfully, but the design
  is neither PASS nor FAIL (FAIL would mean "dropped per the kill
  rule", which the doc does not say) and is excluded from candidacy
  until an audit clears it. If H7 fails any of SG1–SG3, the verdict is
  FAIL and the audit flag is still recorded.
- SG4 uncomputable (<2 usable days): the design doc is silent; the
  reader exits 3 (invalid window) rather than passing or failing H3 on
  no evidence.
- DSR is read from the design files as computed by the backtest runner
  (no daily returns in the window files); n_trials basis 129 + 3 per
  PASS verdict. Output is dated by window end; the runbook's canonical
  `experiments/results/exp-fresh-validation-v1.json` is for the January
  run to adopt (copy of the final dated file) — the reader does not
  write to it.
- Safety: the reader never writes into the window dir (out-dir inside
  it is refused), records every path it opens (`opened_paths`), and
  never opens the frozen holdout, the EDGAR dataset, or the scores
  files.

### Verification (test_b14_gate_reader.py, 27 tests, synthetic fixtures only)
- Pass case: all five PASS, candidate v3 (best net10 +5%), ml-ranker
  "pass, weak" (DSR 0.90); SG2-only and SG3-only failures covered.
- SG1 fail: v2 net10 −3% vs v4 −2% → FAIL, excluded; <10 trades →
  INCONCLUSIVE even with the best returns; all-fail → candidate null.
- Shortfall: 20 days / 25 filings → exit 2, shortfall json+md,
  "extend by 10 trading days (extension 1 of 3)"; extension-3 variant
  → "Already at extension 3 of 3 — STOP"; boundary 30 days + 40
  filings → met; 10 unscored filings don't count (35/40 → shortfall).
- H7 audit: Sharpe 9.22 with SG1–SG3 passing → AUDIT, not PASS,
  candidate v3; Sharpe 9.22 + SG fail → FAIL with audit flag still
  recorded; Sharpe exactly 5.0 → PASS, no audit.
- SG4: perfectly anti-ranked similarity → mean IC −1.0, significant →
  H3 FAIL despite passing SG1–SG3; IC cross-checked against pandas
  rank-corr on the same panels (mean/t match to 1e-6); std==0 daily
  ICs → t handled as ±∞ without crashing; <2 usable days → exit 3.
- Staleness/path safety: frozen-holdout window refused; v4 base with
  holdout end refused; mismatched design file refused; every opened
  path asserted inside the tmp window dir and none equal to any
  forbidden real path; window-dir hashes unchanged after the run.
- Full suite: **248 passed** (was 221; +27 new), 33.6s. Milo verified
  independently: 27/27 B14 tests pass; CLI on a synthetic audit-case
  window → exit 0 (v2/v3/h3 PASS, h7 AUDIT, ml-ranker PASS weak,
  candidate v3); shortfall window → exit 2 with shortfall report and
  no gate report; no-args → exit 3. Design-doc wording for SG1–SG4,
  the audit rule, the DSR basis, and the minimums/extension rule all
  re-checked against the doc — the reader matches.

### Deliberately NOT done
- The reader was NOT run against the real window: the window is not
  complete until Dec 31 and the gate reads in early January. It reads
  synthetic fixtures only until then.
- No backtest run, no filing scored, no holdout touched, no January
  criteria changed.
