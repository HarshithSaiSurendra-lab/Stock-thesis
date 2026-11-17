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
  short TEST excerpt, a few cents) — NOT run yet: needs the Anthropic
  API key via the secure vault. 8 new provider tests; full suite
  60/60 passes. Still NOT imported by the order path.
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
