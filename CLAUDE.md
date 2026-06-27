# CLAUDE.md — Trading System Project Guide

This file is for Claude (in any form: web, Codex, Code) working on this project.
It explains the structure, what's done, what's being built, and how to navigate.

## Quick context
**What:** An automated daily-decision equity trading system on Alpaca. Runs paper
and live accounts in sync. Uses volume-based indicators + trailing stops. Hard
capital floor at −20%. Trade journal with memory. Phone notifications.

**Why:** Retail can't compete on execution speed, so the edge is signal quality.
This system captures it safely: dual-account sync measures execution cost, memory
learns from mistakes, the floor prevents wipeout.

**Status:** 60% done. Core modules built and tested. Remaining work is the daily
loop logic and glue.

## Project structure
```
.
├── CLAUDE.md                    ← You are here
├── HANDOFF_SPEC.md              ← Read this first. Complete build spec.
├── README.md                    ← Module overview and safety architecture.
├── config.py                    ← TO BUILD: central config, loads secrets from env.
├── universe.py                  ← TO BUILD: dynamic stock screen + liquidity floor.
├── strategy_runner.py           ← TO BUILD: the daily loop spine.
├── notifier.py                  ← TO BUILD: phone push (Telegram/Pushover).
├── reconcile.py                 ← TO BUILD: catch state drift + silent fails.
├── main.py                      ← TO BUILD: entrypoint, cron-friendly.
│
├── indicators.py                ✓ DONE: six-indicator system + features.
├── backtest.py                  ✓ DONE: bias-aware backtester (integrity guards).
├── test_integrity.py            ✓ DONE: proves look-ahead/survivor/cost guards work.
├── guardian.py                  ✓ DONE: safety wrapper (kill switch, circuit breaker).
├── broker_alpaca.py             ✓ DONE: dual-account, floor, market/limit hybrid.
├── memory.py                    ✓ DONE: trade journal, retrieval, tax tracking.
│
├── trade_memory.db              (created at runtime, SQLite)
├── guardian_state.json          (created at runtime, persisted breaker state)
├── broker_state.json            (created at runtime, persisted floor state)
├── KILL_SWITCH                  (file: touch to halt immediately)
└── .env (you create this)       (env vars: API keys, secrets. DO NOT COMMIT.)
```

## What's done (don't break these)

### `indicators.py`
Six-indicator system: KVO, OBV, Weighted A/D, MFI, RSI, moving averages.
Entry point: `build_feature_frame(df)` where df has `open,high,low,close,volume`.
Returns a DataFrame with all features; every value at time t uses only data ≤ t
(no look-ahead). Vectorized, no loops.

### `backtest.py`
Bias-guarded cross-sectional backtester. Use this to validate any signal before
going live. Key functions:
- `run_backtest(prices, signal, cfg)` → equity curve + stats
- `walk_forward_splits(idx, n_splits)` → for out-of-sample testing
Passes integrity tests: look-ahead guard, survivorship guard, cost monotonicity.
Tests live in `test_integrity.py` — keep them passing.

### `guardian.py`
Broker-agnostic safety wrapper. Every order goes through this.
- Kill switch (file presence)
- Circuit breaker (daily loss, order rate, consecutive errors, stale data)
- Pre-trade sanity checks (size, notional, price vs last)
- Alerting (local log + pluggable Telegram/email)
Wire it like: `guardian.submit(order, broker.place_order_fn)` so broker errors
trip the breaker.

### `broker_alpaca.py`
Dual-account execution (paper + live) with the capital floor and market/limit.
- `DualBroker(cfg).paper` and `.live` each are an `AlpacaLeg`.
- `AlpacaLeg.submit(order)` places the order and may raise (Guardian catches).
- `AlpacaLeg.check_floor()` — live only; liquidates + halts if tripped.
- `AlpacaLeg.floor_tripped()` returns whether floor is dead.
- `decide_order_type(direction, quote)` → market vs limit (the hybrid logic).
Keys from env vars only: `ALPACA_PAPER_KEY/SECRET`, `ALPACA_LIVE_KEY/SECRET`.

### `memory.py`
SQLite trade journal. Logs every decision, retrieves similar past setups, flags
repeating losers, tracks tax cost-basis.
- `memory.log_decision(snap, order_type, intended_price, qty, paper_fill, live_fill)`
- `memory.similar_setups(snap)` → {wins, losses, verdict} from similar history
- `memory.flag_if_repeating_loss(snap)` → warning string or None
- `memory.open_tax_lot` / `memory.close_tax_lot` for cost basis
- `memory.daily_summary()` → used by notifier

## What's left to build (§4 of HANDOFF_SPEC)

### Priority 1: `strategy_runner.py` (the spine)
This is the core daily loop. Pseudocode in HANDOFF_SPEC §4.3.
1. Select universe (call universe.py).
2. Compute signals for each symbol (build_feature_frame + composite rule).
3. Check memory for repeating losers, skip them.
4. Rank and pick top N.
5. Size positions (equal-weight, capped).
6. Route through guardian + broker to BOTH paper and live.
7. Log to memory (both the decision and tax lots).
8. Attach trailing stops (live only).
9. Call reconcile to catch drift.
10. Notify via notifier.
Return summary dict for main.py.

### Priority 2: `config.py`
Central dataclass. Loads thresholds, paths, secrets from env. Must have:
- universe: min_dollar_volume, min_price, max_spread_pct
- sizing: max_position_pct=0.50, target_n_positions
- exits: trail_percent=8.0
- signal thresholds: how to map composite signal to strong_up/mild_up/no_trade
- accounts: paper & live starting capital, floor_pct=0.80
- notifier: provider, token/chat_id (from env)
- run: timezone, decision_time

### Priority 3: `universe.py`
Dynamic stock screen with liquidity floor. Function:
```python
select_universe(broker, cfg) -> list[str]
```
Start from Alpaca tradable US equities (or a static seed). Pull 20-day bars.
Keep symbols with avg_dollar_volume ≥ cfg threshold, price ≥ floor, spread ≤ cap.
Return surviving tickers (reasonable cap: ~200). Both paper and live use SAME
universe.

### Priority 4: `notifier.py`
End-of-day phone push. Telegram (free) or Pushover (paid but simpler).
```python
class Notifier: def send(title, body) -> None  # never raises
def format_daily(summary, status) -> (title, body)
```
The template already exists in guardian.py (TelegramAlerter) — follow that pattern.

### Priority 5: `reconcile.py`
Catch silent failures: intended holdings vs Alpaca actual.
```python
reconcile(broker, memory, cfg) -> {ok, discrepancies: [...]}
```
Compare what the system thinks it holds vs leg.positions() from both legs.
Mismatch = call guardian.manual_halt(...) + notify.

### Priority 6: `main.py`
Thin entrypoint. Load config, construct broker/guardian/memory, call
strategy_runner.run_daily(), notify, exit. Support `--dry-run` (signals/sizing
only, no submit). Cron-friendly.

## Build order
1. **config.py** (easiest, unlocks others)
2. **universe.py** (depends on config, broker)
3. **strategy_runner.py** (depends on all above + existing modules)
4. **notifier.py** (small, depends on config)
5. **reconcile.py** (depends on broker, guardian, memory, config)
6. **main.py** (final glue)

## How to navigate existing code

**When you see `broker_alpaca.DualBroker` or `broker_alpaca.AlpacaLeg`:**
- Both have a `.submit(order)` method. It raises on broker error.
- Always wrap submit calls with `guardian.submit(order, leg.submit)` so errors
  trip the breaker.
- Paper leg can always trade (no floor). Live leg checks floor.
- `order` dict shape: `{symbol, side, qty, order_type, limit_price?}`

**When you see `memory.TradeMemory`:**
- Before ANY entry, call `memory.flag_if_repeating_loss(snap)` and respect the
  result.
- Log the decision with `memory.log_decision(...)` capturing BOTH paper and live
  fills.
- When a position closes, call `memory.close_decision(...)` with exit price.
- Pull `memory.daily_summary()` at the end for the notification.

**When you see `guardian.Guardian`:**
- Every day starts with `guardian.start_day()`.
- Before any trade, check `guardian.can_trade()` (kills switch + breakers).
- Wrap every order: `guardian.submit(order, broker_call)`.
- Call `guardian.health_report()` once per run for logging.

## Testing
- Keep `test_integrity.py` passing. It proves the backtest guards work.
- Add tests for new modules (strategy_runner, config, universe, reconcile).
- **All broker interaction tests must use PAPER endpoint or mocks.** Never hit
  live in tests.
- Test the signal: backtest it with `backtest.run_backtest` and
  `walk_forward_splits` before deploying.

## Key invariants (must not break)
1. Live leg halts permanently at −20% floor (persisted state).
2. Every order through `guardian.submit`.
3. No order for symbols below liquidity floor.
4. No single position > 50% of equity.
5. Keys from env only, never logged.
6. Kill-switch file halts everything.
7. Rerun of daily loop does NOT double-submit.

## Secrets & env vars (you handle this, not Codex)
Create a `.env` file locally (do not commit):
```
ALPACA_PAPER_KEY=pk_xxx
ALPACA_PAPER_SECRET=xxx
ALPACA_LIVE_KEY=pk_yyy
ALPACA_LIVE_SECRET=yyy
NOTIFIER_PROVIDER=telegram
NOTIFIER_TOKEN=xxx
NOTIFIER_CHAT_ID=123
```
Load in main.py: `config.py` reads from `os.environ`. Codex won't see the `.env`
unless you pass it; the spec assumes you do.

## Signal definition
The signal rule is in HANDOFF_SPEC §5. It's a **stub** that Codex should
implement in a separate `signal.py` module:
```python
def composite_signal(features: pd.DataFrame) -> pd.Series:
    # returns 'strong_up', 'mild_up', 'no_trade' per row
```
Default v1: momentum > 0, volume confirming, RSI/MFI not overbought.
**Validate with backtest before trusting it live.** The current v1 is not
proven.

## Red flags (if Codex does any of these, stop and ask)
- Real-time model retraining (chases noise; learning is deliberate only)
- Intraday/hourly trading (breaks the whole thesis)
- Robinhood as broker (no API, agent rail is different)
- Trading below liquidity floor (spreads destroy small accounts)
- Hardcoded secrets
- Skipping the guardian wrapper on orders
- Skipping the floor check before live trades

## Run it (once you've built everything)
```bash
# Set env vars (secrets, API keys).
export ALPACA_PAPER_KEY=... ALPACA_LIVE_KEY=...

# Test the signal (offline, no trading).
python3 main.py --dry-run

# Run daily after market close (4:15pm ET).
python3 main.py

# Or via cron: 16 15 * * 1-5 cd /path && python3 main.py >> run.log 2>&1
```

Watch the phone notification. Compare paper equity to live equity every day to
measure execution cost. After a few weeks, review the trade journal to see what
patterns the memory learned.

## Questions for Codex
If Codex asks:
- "What signal should I use?" → Refer to HANDOFF_SPEC §5 and the stub. Ask to
  backtest it before deploying.
- "Should I add feature X?" → Refer to the non-goals in HANDOFF_SPEC §10. If
  it's not there, ask first (don't just add intraday or model retraining).
- "What if the floor gets tripped?" → It's permanent. Read the spec (§2, capital
  floor behavior). Manual intervention needed.

Codex is smart enough to spot gaps. If the spec is ambiguous, it will ask. Answer
clearly; the spec is complete enough to build from but intentionally leaves the
signal rule to be validated separately.
