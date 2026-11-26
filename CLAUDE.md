# Contributor Guide

This file is the operating guide for AI coding agents and human contributors.
Read `README.md`, `PROJECT_STATUS.md`, and `HANDOFF_SPEC.md` before changing
strategy, risk, execution, or deployment behavior.

## Product definition

Stock Thesis is a daily US-equity research and paper-trading prototype. It is
not a high-frequency system, an options engine, or an autonomous LLM trader.
The immediate goal is to determine whether a simple stock-selection policy has
repeatable net edge and can be operated safely.

Current state:

- Operational prototype exists.
- Research and robustness tools exist.
- Paper execution path exists.
- Live and after-hours paths are locked by default.
- Strategy profitability is not established.
- Exact-once order lifecycle is implemented in `order_manager.py`
  (B3, Dec 2026, fake broker only, 16 new tests) — deterministic
  client_order_id, SQLite intent store, query-before-retry, startup
  reconcile, fill dedup. The live loop in `strategy_runner.py` is
  deliberately NOT rewired to it yet.

Never describe the project as profitable, production-ready, or fully autonomous.

## Source-of-truth documents

- `README.md`: orientation, setup, commands, and module map.
- `PROJECT_STATUS.md`: verified status, weaknesses, gates, and roadmap.
- `HANDOFF_SPEC.md`: technical contracts and load-bearing invariants.
- `ATS_LOOPS.md`: loop specs — spec, verification, and stop conditions
  for the daily run, B2 simulation, fresh validation, earnings re-run,
  B3 reconcile, and the Astra collaboration loop.
- `.env.example`: non-secret configuration surface.

If these documents disagree, update them in the same change. Do not preserve a
stale percentage-complete estimate.

## Module boundaries

### Offline research

- `indicators.py`: feature math only.
- `backtest.py`: portfolio replay, lagged weights, costs, statistics.
- `stop_backtest.py`: stop-aware replay.
- `research_runner.py`: candidate signal variants and chronological tests.
- `robustness_runner.py`: stress grid and benchmark comparisons.

### Decisions

- `universe.py`: current tradable universe and data retrieval.
- `trade_signal.py`: deterministic signal math.
- `filters.py`: regime, downside, and volatility gates.
- `sector_map.py`: sector benchmark lookup.
- `strategy_runner.py`: regular-session orchestration.
- `after_hours.py`: separately gated extended-hours orchestration.
- `session_router.py`: chooses the eligible session path.

### Safety and execution

- `config.py`: typed runtime policy loaded from environment variables.
- `guardian.py`: kill switch, breakers, stale-data and pre-trade checks.
- `broker_alpaca.py`: broker transport and account floor.
- `deploy_check.py`: startup safety validation.
- `execution_journal.py`: durable evidence about decisions and submissions.
- `order_manager.py`: B3 exact-once lifecycle (deterministic intent,
  claim, submit/reconcile, fill dedup) over a SQLite intent store.
- `fake_broker.py`: test-only broker modelling accept+drop, delayed
  lookup, duplicate client_order_id, and partial fills. No real network.
- `design_runner.py`: B2 design-selectable simulation runner (fake
  broker only, winner always None until January).
- `memory.py`: setup history, outcomes, and tax lots.
- `reconcile.py`: expected-versus-actual position comparison.
- `notifier.py`: numeric reports and alerts.
- `main.py`: CLI construction and dispatch.

## Load-bearing rules

1. Live trading remains disabled unless a human deliberately enables it.
2. After-hours real orders require their own independent enable flag.
3. Missing, stale, invalid, or crossed quotes block entries.
4. No secret belongs in source, logs, fixtures, or documentation.
5. A strategy signal never overrides Guardian, capital, liquidity, or deployment gates.
6. Signals formed on a close may not fill at that same close in research.
7. Synthetic data verifies code behavior, not investment performance.
8. Historical claims must disclose whether universe membership is point-in-time.
9. Do not tune risk upward because a strategy is losing.
10. An order acceptance is not a fill, and a fill is not protected until confirmed.
11. Existing position management must continue when new entries are halted.
12. AI can propose changes; deterministic tests and human approval control deployment.
13. Loops need a written spec, verification rules, and stop conditions
    before they run (see `ATS_LOOPS.md`). A loop without a stop
    condition is a runaway process, not a loop.
14. Every repeated mistake becomes memory: write the lesson into
    `CLAUDE.md`, a skill, `PROGRESS.md`, or the experiment log — the
    place the next run will actually read — instead of fixing it once
    and forgetting it.

## Development workflow

Supported runtime: Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

Before a pull request:

1. Inspect the complete diff and keep unrelated files out.
2. Run `python -m pytest -q`.
3. Run `python deploy_check.py` with safe paper-only configuration.
4. Add tests at the same layer as the behavior changed.
5. Update `PROJECT_STATUS.md` when completion or risk claims change.

Broker tests must use fakes or paper endpoints. Unit tests must never place live
orders or require secrets.

## Research acceptance

New signals start as hypotheses. They must be compared with simple baselines and
evaluated chronologically, net of costs, across folds and regimes. Record every
parameter family tried. Preserve a final holdout that is not inspected during
iteration.

Do not promote a strategy because it has:

- one attractive equity curve;
- a high in-sample Sharpe;
- a favorable synthetic demo;
- one strong market regime;
- performance only at zero cost;
- parameters selected after repeated holdout inspection.

The acceptance gates are defined in `PROJECT_STATUS.md`.

## Execution priorities

The crash-safe order lifecycle (B3) is implemented — see
`order_manager.py` and `ATS_LOOPS.md` Loop 5:

1. Persist intent before submit. — Done (SQLite, before any broker call).
2. Use a deterministic idempotency key. — Done (SHA-256 of the intent).
3. Query before retry after an ambiguous response. — Done (lookup,
   grace poll, resubmit SAME ID).
4. Track partial and terminal broker states. — Done (fills deduped by
   fill_id, monotonic cumulative).
5. Protect confirmed filled quantity. — Done (filled only moves forward).
6. Reconcile positions and open orders on boot. — Done for intents
   (`reconcile_all_unknowns`); position reconcile in `reconcile.py`
   is unchanged.
7. Halt and alert on unresolved exposure. — Guardian halt exists;
   UNKNOWN intents stay UNKNOWN for the next reconcile pass, never a
   new ID.

The live loop is deliberately NOT rewired to B3 yet, and B3 has only
been exercised against the fake broker. Do not call the system
exact-once in production until the live loop is rewired and soaked.
The next operational work is B5 (paper-API flakiness: account-call
timeouts, regime failing closed on one failed SPY fetch) and the
Phase 4 soak readiness, not more strategy complexity.

## Deliberately deferred

- Options, futures, leverage, and margin.
- Real-time retraining.
- LLM decisions in the order path.
- High-frequency or sub-minute trading.
- Broad live deployment.
- Adding indicators without a registered research question.

The clean next move is usually stronger evidence or stronger recovery behavior,
not more strategy complexity.
