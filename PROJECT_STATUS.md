# Project Status and Roadmap

**Status date:** 2026-08-24

## Executive assessment

Stock Thesis is a capable phase-1 prototype, not a validated money-making system.
The repository has progressed beyond a backtest notebook: it now contains signal,
risk, broker, session-routing, journaling, reconciliation, and reporting modules.
The strongest work is the separation of research, decisions, risk, and brokerage.

The binding limitation is evidence. The current deterministic signal and its
thresholds are hypotheses. Synthetic smoke tests, a good historical window, or a
clean paper fill do not establish a durable edge.

## Completion by area

| Area | Status | Meaning |
|---|---|---|
| Indicators and feature calculation | Implemented | Vectorized features exist and are testable. |
| Bias-aware portfolio backtest | Implemented | Signal lag, costs, and unavailable symbols are covered. |
| Stop-aware and robustness research | Implemented | Research runners support stress and parameter comparisons. |
| Signal policy and ranking | Implemented baseline | Deterministic, but not validated alpha. |
| Universe and market filters | Implemented prototype | Suitable for current/paper selection, not historical membership proof. |
| Portfolio planning and sizing | Implemented prototype | Includes quality, sector, breadth, volatility, and exposure controls. |
| Paper broker path | Implemented prototype | Requires configured Alpaca credentials and operational soak. |
| After-hours path | Implemented and locked | Limit-only with independent enable flags; still experimental. |
| Guardian and deployment locks | Implemented | Reduces unsafe operation; does not guarantee fills. |
| Journaling and reporting | Implemented prototype | Decision and execution evidence can be recorded locally. |
| Exact-once order lifecycle | Partial | Journal exists, but crash-safe intent reservation and fill recovery remain. |
| Research evidence | Incomplete | No accepted point-in-time, untouched-holdout result. |
| Live readiness | Not approved | Live remains disabled by default. |
| Options or multi-asset trading | Not started | Explicitly deferred. |

## Verified baseline

- Python 3.12 test suite: 42 tests passing as of this status update.
- Integrity tests cover signal lag, survivorship behavior for unavailable data,
  and monotonic transaction-cost effects.
- Operational tests cover filters, session routing, bar caching, after-hours
  restrictions, reconciliation, journaling, stop replay, and robustness runners.
- Order routing now rejects missing, invalid, and crossed quotes instead of
  converting them into market orders.
- The reproducible dependency manifest supports clean local and future CI runs.

## What the results do not prove

- The strategy will make money in future markets.
- The current symbol universe is survivorship-free historically.
- Paper fills represent achievable live fills.
- A trailing stop limits every realized loss to its configured percentage.
- The best parameter set was not selected through repeated backtest overfitting.
- Live execution can recover correctly from every timeout, partial fill, restart,
  or broker-state mismatch.

## Priority roadmap

### P0: Establish honest signal evidence

1. Acquire adjusted OHLCV and point-in-time universe membership with delistings.
2. Freeze a benchmark, cost model, forecast horizon, and final holdout period.
3. Register every strategy and parameter experiment before inspecting results.
4. Evaluate simple baselines first: buy-and-hold, equal weight, and momentum.
5. Run expanding walk-forward tests with purge gaps where labels overlap.
6. Stress spreads and slippage at 1x and 2x modeled costs.
7. Report fold-level expectancy, turnover, drawdown, hit rate, and benchmark excess.
8. Add multiple-testing diagnostics such as Deflated Sharpe or an equivalent
   documented selection-bias adjustment.

Exit gate: a fixed strategy shows positive net expectancy across folds, remains
viable at 2x costs, has enough independent observations, and passes the untouched
holdout without changing its rules.

### P1: Complete execution correctness

1. Persist deterministic order intents before broker submission.
2. Add idempotency keys and query-before-retry behavior.
3. Model submitted, partial, filled, canceled, rejected, and expired states.
4. Attach or replace protection only after confirmed fills and actual quantities.
5. Reconcile positions and open orders on every boot.
6. Adopt or close unexpected positions under a documented policy.
7. Halt and alert on unresolved or unprotected exposure.

Exit gate: fault-injection tests demonstrate no duplicate order after restart and
no filled position is silently treated as protected.

### P2: Paper-operation soak

1. Run on a reliable scheduler with persistent state and log rotation.
2. Deliver daily summaries and immediate critical alerts to a phone.
3. Add an external heartbeat/dead-man monitor.
4. Track intended price, paper fill, spread, slippage, rejection, and reconciliation.
5. Maintain an operator runbook for kill switch, stale data, broker outage, and
   unprotected positions.

Exit gate: at least 60 trading days with no duplicate submissions, unresolved
reconciliation mismatches, missed critical alerts, or unexplained state drift.

### P3: Small live canary

Live consideration begins only after P0-P2 pass. Start with one documented liquid
equity workflow, the smallest practical allocation, and unchanged paper decisions.
Measure live-minus-paper execution rather than assuming the difference.

Exit gate: human review of a predetermined number of reconciled live trades. Any
capital increase requires new written approval; it is never automatic.

### P4: Later research

- Compare regular-session and after-hours evidence independently.
- Add machine learning only after simple baselines and leakage controls pass.
- Consider options only after stock selection, sizing, exits, and execution have
  stable evidence. Options require separate pricing, volatility, liquidity, and
  assignment-risk infrastructure.

## Current weak points

1. Historical universe quality is insufficient for a strong survivorship claim.
2. Research outputs can be repeatedly inspected without a durable experiment log.
3. Execution journaling is more complete than crash recovery.
4. Some limits are configuration hypotheses rather than empirically calibrated rules.
5. The after-hours path increases operational surface before the core strategy has
   established edge; it should remain disabled outside deliberate paper experiments.
6. Documentation previously confused files that exist with work that is complete.

## What is unusually strong

- The project does not depend on speed or an LLM making live trade judgments.
- Research, strategy, risk, brokerage, and reporting have explicit boundaries.
- Live and after-hours modes require explicit unlocks.
- Safety state and trade evidence are designed to persist locally.
- The system records reasons for rejected trades, which is essential for studying
  whether risk gates protect performance or merely suppress opportunity.

## Decision mindset

Treat every attractive backtest as a bug report until it survives leakage,
selection-bias, cost, regime, and implementation checks. Risky trades may be
allowed only through bounded, precommitted sizing; conviction cannot override
operational safety. The product worth building is not a bot that always trades.
It is a system that knows when evidence is weak, abstains safely, and produces a
record that another person can audit.
