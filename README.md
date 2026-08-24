# Stock Thesis

A Python research and paper-trading prototype for daily US equity decisions.
The project is designed to answer two separate questions:

1. Does a repeatable, cost-adjusted signal exist?
2. Can that signal be operated without unsafe or duplicate orders?

Those questions are deliberately separated. The repository contains substantial
research and operational tooling, but it does not yet contain evidence that the
strategy is profitable on unseen, point-in-time market data. Live trading remains
disabled by default.

See [PROJECT_STATUS.md](PROJECT_STATUS.md) for the current completion assessment
and prioritized roadmap. See [HANDOFF_SPEC.md](HANDOFF_SPEC.md) for technical
contracts and safety invariants.

## Current status

**Phase:** operational prototype and research validation.

Implemented:

- Vectorized technical indicators and volume-flow features.
- Bias-aware cross-sectional backtesting with lagged weights and costs.
- Walk-forward, stop-aware, and parameter-robustness research runners.
- Market-regime, breadth, volatility, relative-strength, and sector filters.
- Dynamic universe selection with bar caching and liquidity constraints.
- Regular-session and restricted after-hours decision paths.
- Alpaca paper/live adapters with live trading locked by default.
- Kill switch, circuit breakers, quote checks, capital floor, and deployment checks.
- Reconciliation, notification, decision logging, and SQLite execution journaling.
- A 42-test offline suite validated on Python 3.12.

Not yet proven or complete:

- No validated alpha claim on a survivorship-free, point-in-time universe.
- No untouched final holdout or complete experiment registry.
- Order submission is not yet a complete crash-safe, exact-once lifecycle.
- Paper operation has not completed a long soak with alert and reconciliation evidence.
- Live capital is not approved. Options and additional asset classes are out of scope.

## Architecture

```text
market data
    |
    v
universe.py -> indicators.py -> trade_signal.py -> filters.py
                                              |
                                              v
                                     strategy_runner.py
                                              |
                              guardian + broker_alpaca
                                              |
                    execution journal + memory + reconcile
                                              |
                               notifier + decision reports

offline research:
backtest.py -> stop_backtest.py -> research_runner.py -> robustness_runner.py
```

### Research layer

- `indicators.py`: RSI, MFI, KVO, OBV, Williams A/D, moving averages,
  realized volatility, and medium-term momentum.
- `backtest.py`: lagged cross-sectional weights, transaction costs, exposure
  controls, and walk-forward splits.
- `stop_backtest.py`: trailing-stop-aware replay and drawdown throttling.
- `research_runner.py`: signal variants and chronological evaluation.
- `robustness_runner.py`: parameter grids, multiple start dates, benchmarks,
  and cost-aware robustness summaries.

### Decision layer

- `universe.py`: tradable-symbol discovery, liquidity filtering, daily bars,
  and local bar caching.
- `trade_signal.py`: deterministic composite score and signal.
- `filters.py`: regime, downside, and volatility gates.
- `sector_map.py`: sector benchmark mapping.
- `strategy_runner.py`: ranking, sizing, order previews, broker submission,
  reporting, and journaling for the regular session.
- `after_hours.py`: separately gated, limit-only extended-hours workflow.
- `session_router.py`: regular, after-hours, or closed-session selection.

### Safety and operations

- `guardian.py`: kill switch, daily-loss and rate breakers, stale-data checks,
  order validation, persistent halt state, and alerts.
- `broker_alpaca.py`: Alpaca account adapters, quote/order normalization,
  capital-floor enforcement, and market/limit routing.
- `deploy_check.py`: blocks unsafe live or after-hours configuration.
- `execution_journal.py`: SQLite records for runs, candidates, intended orders,
  skips, and broker results.
- `memory.py`: decision history, similar-setup retrieval, outcomes, and tax lots.
- `reconcile.py`: compares expected strategy state with broker positions.
- `notifier.py`: daily summaries and phone delivery.
- `main.py`: command-line entry point.

## Safety posture

- `ALLOW_LIVE=0` is the default.
- Real after-hours submission requires `ALLOW_AFTER_HOURS=1`.
- Live after-hours submission has a second independent lock.
- Missing, invalid, or crossed quotes fail closed.
- Secrets are read from environment variables and ignored by Git.
- A `KILL_SWITCH` file prevents new orders.
- A persisted live-account capital floor prevents a restart from silently
  restoring permission to trade.

These controls reduce operational risk. They do not create investment edge or
guarantee that exits fill at modeled prices.

## Setup

Python 3.12 is the supported runtime.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Place only paper credentials in `.env` while validating. Never commit `.env`.

## Validate

```bash
python -m pytest -q
python deploy_check.py
python main.py --dry-run --mode regular
python main.py --dry-run --mode after-hours
```

The test suite is fully offline. A strategy dry run needs market-data credentials
when it requests Alpaca data.

## Research commands

```bash
python research_runner.py --synthetic
python robustness_runner.py --synthetic
```

Synthetic results verify software behavior only. They are never trading evidence.
Real-data results must be chronological, net of costs, benchmark-relative, and
clearly labeled when historical universe membership is not point-in-time.

## Deployment rule

The project remains paper-only until the gates in `PROJECT_STATUS.md` pass.
Risk limits may not be increased to rescue a weak backtest or losing paper run.
An AI may help analyze results or propose code, but deterministic code and human
approval remain in control of orders and deployment.
