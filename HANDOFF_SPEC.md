# Stock Thesis Technical Handoff

**Status date:** 2026-08-24

This document describes the implemented system, its contracts, and the work that
must remain incomplete until evidence or fault-injection tests support it.

## 1. Objective and scope

Build a daily-decision US-equity system that:

- researches deterministic stock-ranking hypotheses honestly;
- operates the same decision policy in paper trading;
- fails closed on unsafe data or account state;
- records enough evidence to explain every decision and broker result;
- permits live consideration only after research and paper-operation gates pass.

The current repository is an operational prototype. It is not validated alpha,
not production-ready execution, and not an options platform.

## 2. Implemented flow

```text
main.py
  -> TradingConfig.from_env()
  -> DualBroker + Guardian + TradeMemory
  -> session_router.run_routed()
       -> strategy_runner.run_daily()
       -> after_hours.run_after_hours()
       -> closed/no-order response

regular decision path
  -> universe.select_universe()
  -> universe.fetch_symbol_frame()
  -> indicators.build_feature_frame()
  -> trade_signal composite score/signal
  -> regime, downside, volatility, relative-strength, breadth, sector gates
  -> ranking and position sizing
  -> Guardian validation
  -> Alpaca paper/live submission subject to account locks
  -> strategy state, execution journal, memory, reconciliation, notification

offline path
  -> backtest
  -> stop-aware replay
  -> research variants
  -> robustness grid
```

## 3. Configuration contract

`TradingConfig.from_env()` is the runtime entry point. Configuration is grouped
into universe, sizing, exits, signals, sector, after-hours, regime, risk, paper,
live, notifier, run, and path policies.

Required rules:

- Secrets come from environment variables only.
- `.env` is ignored and `.env.example` contains placeholders only.
- `ALLOW_LIVE=0` remains the default.
- `ALLOW_AFTER_HOURS=0` blocks real extended-hours submissions.
- `AFTER_HOURS_SUBMIT_LIVE=0` is a second live extended-hours lock.
- Market-data feed and adjustment semantics must be explicit in research output.
- Configuration values are starting hypotheses, not optimized constants.

`deploy_check.py` must reject a configuration that exposes live or real
after-hours trading without all corresponding safety conditions.

## 4. Research contracts

### 4.1 Features

`build_feature_frame(df)` receives one symbol's OHLCV DataFrame and returns
technical features. Values at timestamp t may use information through t only.

Core features include:

- RSI and MFI;
- KVO and signal histogram;
- OBV and Williams A/D slopes;
- moving averages;
- realized volatility;
- medium-term momentum.

### 4.2 Portfolio backtest

`run_backtest(prices, signal, cfg)` must preserve:

- one-bar lag between target and held weights;
- no position while price data is unavailable;
- transaction costs on turnover;
- exposure and drawdown policies from configuration;
- deterministic statistics for identical inputs.

`walk_forward_splits` is chronological. Randomly shuffled finance validation is
not an acceptable replacement.

### 4.3 Stop-aware replay

`run_trailing_stop_backtest` models holding and exit behavior separately from
the cross-sectional return engine. Gap-through behavior and ambiguous intrabar
ordering must remain conservative and documented.

### 4.4 Research and robustness runners

`research_runner.py` and `robustness_runner.py` compare fixed variants across
windows, costs, benchmarks, and selected parameter dimensions.

Current limitation: the runners do not by themselves prove point-in-time universe
membership or prevent repeated human selection against the same evaluation period.
The next research release must add an experiment registry and untouched holdout.

## 5. Decision contracts

### 5.1 Universe

The operational universe may come from a seed or current Alpaca asset inventory.
It applies price, average dollar-volume, spread, and candidate-count constraints.
Daily bars can be cached to reduce calls.

Current-universe selection is acceptable for paper operation. It must not be
presented as survivorship-free historical research.

### 5.2 Signals

`trade_signal.py` emits deterministic scores/signals from the feature frame.
`filters.py` and sector/breadth logic may reject an otherwise positive signal.
Every rejection should retain a machine-readable reason.

The signal may express intent. It has no authority to bypass:

- liquidity and quote validity;
- regime or portfolio gates;
- Guardian;
- account locks;
- capital floor;
- deployment checks.

### 5.3 Regular-session runner

```python
run_daily(broker, guardian, memory, cfg, dry_run=False) -> dict
```

The runner builds a ranked decision report, chooses eligible orders, writes
evidence, and submits only when the market and safety state allow it.

Dry-run mode may fetch data and produce intended orders, but must not submit.

### 5.4 After-hours runner

```python
run_after_hours(broker, guardian, memory, cfg, dry_run=False) -> dict
```

The after-hours path is a separate experiment with:

- a narrow liquid-symbol set;
- valid, fresh, tight quotes;
- limit orders only;
- explicit `extended_hours` behavior;
- reduced sizing;
- independent real-order and live-order locks.

It must not be evaluated as part of the regular strategy unless the research
design explicitly combines them.

## 6. Safety contracts

### 6.1 Guardian

Every submission path must pass through Guardian validation. Guardian owns:

- file-based kill switch;
- daily-loss, rate, order-count, and broker-error breakers;
- stale-data handling;
- notional, price-deviation, and concentration checks;
- persistent halt state;
- safety alerts.

Entry halts do not authorize abandoning existing positions.

### 6.2 Broker adapter

`broker_alpaca.py` owns account transport, order normalization, quote access,
positions, market state, trailing-stop requests, and the live capital floor.

Order routing requirements:

- missing or malformed quote -> skip;
- crossed or non-positive quote -> skip;
- strong urgent signal with tight spread -> market may be allowed;
- mild signal -> passive limit;
- wide spread -> passive limit;
- sell limits use the ask, buy limits use the bid.

A broker acceptance response is a submission state, not a fill.

### 6.3 Reconciliation

```python
reconcile(broker, memory, cfg, guardian=None) -> dict
```

Current reconciliation compares expected and actual positions and can halt on
drift. It does not yet reconcile every open order or implement complete adoption,
cancel-orphan, and partial-fill recovery policies.

### 6.4 Persistence

Runtime state and evidence live outside source control:

- strategy and Guardian JSON state;
- broker capital-floor state;
- trade-memory SQLite database;
- execution-journal SQLite database;
- decision logs and cached bars.

Anything required to survive a crash must ultimately be durable before the
corresponding broker side effect occurs.

## 7. Known execution gap

The repository records rich execution evidence, but the regular runner does not
yet implement the complete order state machine required for exact-once recovery.

Required lifecycle:

```text
INTENT_RECORDED
  -> SUBMITTING
  -> SUBMITTED
  -> PARTIAL | FILLED | CANCELED | REJECTED | EXPIRED
  -> PROTECTED
  -> CLOSING
  -> CLOSED
```

Each intent needs a deterministic client order ID. After an ambiguous response or
restart, query the broker by that ID before retrying. Protection must use actual
filled quantity and price. Boot reconciliation must inspect positions and open
orders before any new signal is submitted.

This is the highest-priority operational implementation after the research gate.

## 8. Reporting and evidence

`execution_journal.py` records run summaries, candidates, selections, skips,
quote context, sizing, order previews, and broker outcomes. `memory.py` stores
decision snapshots and closed outcomes. `notifier.py` formats daily reports.

Evidence requirements:

- log rejected signals as well as trades;
- preserve intended and actual prices separately;
- preserve paper and live accounts separately;
- never log credentials;
- alert delivery failure must not crash the trading loop;
- a future always-on deployment needs an external dead-man monitor.

## 9. Validation gates

### Research gate

Before long-running paper evaluation, a fixed strategy must:

1. use adjusted, reproducible data;
2. disclose point-in-time universe quality;
3. execute signals no earlier than the next tradable price;
4. include realistic round-trip costs and 2x cost stress;
5. show fold-level results across regimes;
6. beat simple baselines net of cost;
7. preserve an untouched final holdout;
8. account for the number of experiments tried.

### Execution gate

Before live consideration:

1. idempotent order retry is fault-tested;
2. partial-fill and timeout behavior is fault-tested;
3. filled positions cannot remain silently unprotected;
4. positions and open orders reconcile on restart;
5. unresolved drift halts new entries and alerts;
6. critical state survives process restart.

### Operational gate

Complete at least 60 trading days of paper operation with:

- no duplicate submissions;
- no unresolved state mismatch;
- no missed critical alert;
- no accidental live attempt;
- explainable daily decisions and broker outcomes.

## 10. Build order

1. Point-in-time data adapter and dataset manifest.
2. Experiment registry and untouched-holdout governance.
3. Fixed baseline evaluation with multiple-testing adjustment.
4. Exact-once intent and order lifecycle.
5. Open-order reconciliation and protection recovery.
6. Scheduler, external heartbeat, alert outbox, and runbook.
7. Paper soak and review.
8. Small live canary only after written approval.
9. Machine-learning comparison only after the simple baseline.
10. Options research only after stock operation is stable.

## 11. Tests

The supported command is:

```bash
python -m pytest -q
```

Tests must remain offline and deterministic. As of this handoff, 42 tests pass on
Python 3.12. A repository CI workflow is still pending.

Minimum coverage targets for future work:

- adversarial look-ahead and unavailable-symbol tests;
- quote and order-routing boundaries;
- every risk-gate boundary;
- crash injection at every order state;
- position and open-order reconciliation mismatches;
- dry-run proof of no broker mutation;
- secret scanning and safe-default startup.

## 12. Non-goals

- No guarantee of profit.
- No LLM in the order path.
- No automatic risk escalation.
- No live enablement by model decision.
- No high-frequency strategy.
- No options, futures, leverage, or margin in the current phase.
- No real-time self-retraining from a small trade sample.

The target product is an auditable decision and execution system. The ability to
abstain, halt, and explain failure is part of correctness.
