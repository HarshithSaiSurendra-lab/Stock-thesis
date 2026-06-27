# HANDOFF SPEC — Automated Trading System (Alpaca, dual-account)

This document is a complete build spec. It assumes no prior conversation. It
describes a partly-built Python trading system, the design decisions behind it,
the interfaces of the existing modules, and exactly what remains to build.

Build target: a daily-decision, days-to-weeks-holding automated equity trading
system on Alpaca, running paper and live accounts in parallel, with a hard
capital floor, a trade-memory layer, and end-of-day phone notifications.

---

## 1. CORE DESIGN DECISIONS (do not silently change these)

1. **Broker: Alpaca.** Not Robinhood. Reason: Alpaca has a documented order API
   and a real paper-trading environment; Robinhood has neither (its only
   programmatic equity path is an agent/MCP rail, unsuitable for an unattended
   script). Use the `alpaca-py` SDK (installed, v0.43.4).

2. **Holding period: days to weeks.** NOT intraday/HFT. The entire thesis is
   that retail cannot win on execution speed, so the edge must come from signal
   quality. Do not add intraday/scalping logic. Fast exits are handled by a
   trailing stop, not by shortening the decision loop.

3. **Decision cadence: once daily**, after market close, to place orders for the
   next session. Low call volume — stay well within Alpaca rate limits.

4. **Dual-account sync:** one signal engine drives TWO Alpaca accounts at once —
   paper (idealized fills, the theoretical baseline) and live (real fills). The
   difference between them is the live measurement of execution cost (slippage).
   Same decisions, same universe, both accounts, every run.

5. **Capital floor (hard):** if the LIVE account equity falls to ≤80% of its
   $1,000 starting capital (i.e. a −20% drawdown, equity ≤ $800), liquidate all
   live positions and halt the live leg PERMANENTLY (state persisted across
   restarts). Paper keeps running as a benchmark. This is the single most
   important safety rule.

6. **Position sizing:** single-position cap = 50% of that account's equity. No
   other cap (a position may be up to 50% of equity). Default sizing when not
   specified: split target allocation across selected names, each capped at 50%.

7. **Exit logic: trailing stop-loss** on every open long. This is the primary
   risk control alongside the floor. Default trail = 8% (make it configurable).

8. **Order type: market/limit hybrid** (already implemented, see §3). Strong
   signal + tight spread → market. Mild signal or wide spread → limit at bid.

9. **Universe selection: dynamic across the whole market, BUT with a liquidity
   floor.** The system may pick any stock that clears: minimum average daily
   dollar volume (default ≥ $5M), minimum price (default ≥ $5), and maximum
   spread (default ≤ 0.5%). Do NOT trade below the liquidity floor — thin names
   destroy small accounts via spread and unreliable volume-based signals.

10. **Memory: continuous journal + retrieval-before-action + pattern-flagging.**
    Do NOT implement real-time self-retraining (it chases noise). Learning is
    deliberate and human-gated: the journal records everything; retraining is a
    separate offline step the user runs on a large enough sample.

11. **Notifications: end-of-day phone ping** summarizing the day's activity.

12. **Secrets: environment variables only.** Never hardcode keys. Never log them.
    `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`, `ALPACA_LIVE_KEY`,
    `ALPACA_LIVE_SECRET`, plus the notifier token/chat id.

---

## 2. ARCHITECTURE OVERVIEW

```
                 ┌─────────────────────────────────────────┐
                 │            strategy_runner.py            │   <-- TO BUILD
                 │   (daily loop: the spine of everything)  │
                 └───────────────┬─────────────────────────┘
        ┌───────────────┬────────┼────────────┬───────────────┐
        ▼               ▼        ▼             ▼               ▼
  universe.py      indicators  memory.py   broker_alpaca   guardian.py
  (TO BUILD)        .py [done]  [done]       .py [done]      [done]
   liquidity        signals     retrieval    dual-account    circuit
   screen           features    + journal    + floor         breakers
                                              + hybrid
        │                                        │
        ▼                                        ▼
   notifier.py  (TO BUILD)              reconcile.py (TO BUILD)
   EOD phone ping                       intended vs actual holdings
```

Existing/done: `indicators.py`, `backtest.py`, `test_integrity.py`,
`guardian.py`, `broker_alpaca.py`, `memory.py`.
To build: `universe.py`, `strategy_runner.py`, `notifier.py`, `reconcile.py`,
plus a `config.py` and a thin `main.py` entrypoint.

---

## 3. EXISTING MODULES — INTERFACES TO BUILD AGAINST

### `indicators.py` (done)
Per-symbol OHLCV → features. Every value at time t uses only data ≤ t (no
look-ahead). Input: DataFrame with columns `open,high,low,close,volume`,
DatetimeIndex, single symbol.

Key function:
```python
build_feature_frame(df) -> pd.DataFrame
# columns: ret_1d, sma_20, sma_50, ema_12, rsi_14, obv, mfi_14, wad, kvo,
#          kvo_signal, kvo_hist, rvol_20, mom_126_21, obv_slope_20, wad_slope_20
```
Also individual fns: `rsi, obv, mfi, williams_ad, kvo, realized_vol, momentum,
sma, ema`. The six-indicator system = KVO, OBV, Williams A/D, MFI, RSI, MAs.

### `backtest.py` (done)
Bias-aware cross-sectional backtester. Use for offline validation of any signal
BEFORE it goes live.
```python
BacktestConfig(cost_bps, long_only, max_positions, gross_leverage,
               vol_target, rebalance_every)
run_backtest(prices: DataFrame, signal: DataFrame, cfg) -> BacktestResult
# BacktestResult: equity_curve, daily_returns, weights, turnover, stats
walk_forward_splits(index, n_splits) -> yields (train_idx, test_idx)
```
Guards: weights are lagged one bar (look-ahead), NaN symbols are ineligible
(survivorship), transaction costs on every rebalance. `test_integrity.py` proves
these work — keep it passing.

### `guardian.py` (done)
Broker-agnostic safety wrapper. Wrap EVERY order path with this.
```python
GuardianConfig(kill_switch_path, max_daily_loss_pct, max_orders_per_day,
               max_orders_per_minute, max_consecutive_errors,
               max_data_staleness_sec, max_position_pct, max_order_notional,
               max_price_deviation_pct, state_path, alert_log_path)
Guardian(cfg, get_account: ()->dict, last_data_ts: ()->float, alerter)
  .start_day()
  .can_trade() -> bool                      # kill switch + all breakers
  .validate_order(order, last_price, equity, positions) -> (ok, reason)
  .submit(order, place_order_fn) -> result|None   # tracks errors, trips breaker
  .health_report() -> dict
  .manual_halt(note); .reset_halt()
Alerter(log_path).send(level, message)      # subclass TelegramAlerter included
HaltReason enum: KILL_SWITCH, DAILY_LOSS, ORDER_COUNT, RATE_LIMIT, ERRORS,
                 STALE_DATA, MANUAL
```
Kill switch = presence of a file (default `./KILL_SWITCH`). Daily loss limit,
order count/rate limits, consecutive-error breaker, stale-data halt.

### `broker_alpaca.py` (done)
Dual-account execution + capital floor + order-type hybrid.
```python
AccountConfig(mode: 'paper'|'live', starting_capital, floor_pct=0.80, enabled)
BrokerConfig(paper, live, max_position_pct=0.50, state_path)
DualBroker(cfg)
  .paper -> AlpacaLeg ;  .live -> AlpacaLeg
  .status() -> dict ;  .save_state()
AlpacaLeg:
  .equity() -> float|None
  .positions() -> {symbol: {qty, notional, avg_entry, unrealized_pl}}
  .latest_quote(symbol) -> {bid, ask, spread}|None
  .can_trade() -> bool                 # False if disabled or (live) floor tripped
  .check_floor() -> bool               # live: liquidates + halts if breached
  .floor_tripped() -> bool
  .submit(order) -> dict|None          # enforces 50% position cap; may raise
  .attach_trailing_stop(symbol, qty, trail_pct) -> dict|None
# order dict shape:
#   {symbol, side: 'buy'|'sell', qty, order_type: 'market'|'limit', limit_price?}
decide_order_type(signal_direction, quote, max_spread_pct=0.005) -> dict
#   signal_direction in {'strong_up','mild_up',...}; returns
#   {'order_type':'market'} or {'order_type':'limit','limit_price':x}
```
Floor behavior: live leg, equity ≤ starting*floor_pct → `close_all_positions`
then permanent halt (persisted). `submit` raises on broker error so the Guardian
can count it — wrap submit calls accordingly.

### `memory.py` (done)
SQLite journal + retrieval + tax. DB default `./trade_memory.db`.
```python
SignalSnapshot(symbol, direction, rsi, mfi, kvo_hist, obv_slope, wad_slope,
               momentum, rvol, spread_pct, dollar_volume)
TradeMemory(db_path)
  .log_decision(snap, order_type, intended_price, qty,
                paper_fill=None, live_fill=None) -> decision_id
  .close_decision(decision_id, exit_price, realized_pl, holding_days)
  .similar_setups(snap, k=20) -> {n, wins, losses, win_rate, avg_realized_pl, verdict}
  .flag_if_repeating_loss(snap, threshold=0.4) -> warning str | None
  .open_tax_lot(symbol, qty, cost_basis) -> lot_id
  .close_tax_lot(lot_id, proceeds) -> {symbol, realized_gain, term}
  .tax_summary(year=None) -> {short_term_gain, long_term_gain, total_realized, closed_lots}
  .daily_summary() -> {date, trades_opened, trades_closed, realized_pl_today, avg_slippage_bps}
```
Retrieval is by normalized distance over (rsi, mfi, momentum, rvol). Call
`flag_if_repeating_loss` BEFORE every entry; respect/escalate the flag.

---

## 4. MODULES TO BUILD (exact contracts)

### 4.1 `config.py`
Central config dataclass loaded once. Reads thresholds and paths; pulls secrets
from env. Fields at minimum:
- universe: `min_dollar_volume=5e6`, `min_price=5.0`, `max_spread_pct=0.005`,
  `universe_source` (e.g. a static seed list of candidate tickers to screen, or
  Alpaca's `get_all_assets` filtered to tradable US equities).
- sizing: `max_position_pct=0.50`, `target_n_positions` (e.g. 5).
- exits: `trail_percent=8.0`.
- signal thresholds: how to map the composite signal to
  `strong_up`/`mild_up`/`no_trade` (see 4.3).
- accounts: paper & live starting capital ($1000 each), `live_floor_pct=0.80`.
- notifier: provider, token, chat id (from env).
- run: timezone (US/Eastern), decision time (after close).

### 4.2 `universe.py`
Dynamic universe selection with the liquidity floor.
```python
select_universe(broker, cfg) -> list[str]
# 1. Start from candidate set (Alpaca tradable US equities, or a seed list).
# 2. Pull recent daily bars; compute 20-day avg dollar volume.
# 3. Keep symbols with avg_dollar_volume >= cfg.min_dollar_volume
#    AND price >= cfg.min_price AND latest spread_pct <= cfg.max_spread_pct.
# 4. Return the surviving tickers (cap the count to keep API usage sane, e.g. 200).
```
Both paper and live use the SAME surviving universe (decision #4). Pull bars via
Alpaca `StockHistoricalDataClient` (already imported in broker module) or
`StockBarsRequest` with `TimeFrame.Day`.

### 4.3 `strategy_runner.py` — THE SPINE (most important)
The once-daily loop. Pseudocode contract:
```python
def run_daily(broker: DualBroker, guardian: Guardian, memory: TradeMemory,
              cfg) -> dict:
    guardian.start_day()
    if not guardian.can_trade():            # kill switch / breaker / floor
        return notify_and_exit("trading halted")

    universe = select_universe(broker, cfg)

    # 1. SIGNALS: for each symbol pull daily bars, build_feature_frame,
    #    compute a COMPOSITE signal from the six indicators (define a simple,
    #    documented scoring rule — e.g. momentum + volume-trend confirmation +
    #    RSI/MFI not overbought). Output per symbol: score and a direction label
    #    in {'strong_up','mild_up','no_trade'}.

    # 2. RANK & SELECT: take top cfg.target_n_positions names with direction
    #    != 'no_trade'. These are entry candidates.

    # 3. For each candidate, BEFORE acting:
    #      snap = SignalSnapshot(...)            # fill all fields from features+quote
    #      flag = memory.flag_if_repeating_loss(snap)
    #      if flag: log it; skip or down-weight per policy.
    #      sim  = memory.similar_setups(snap)    # attach to the decision record

    # 4. SIZE: equal-weight target across selected names, each capped at
    #    cfg.max_position_pct of equity. Convert to share qty using latest price.

    # 5. ORDER TYPE: quote = leg.latest_quote(symbol);
    #    decide_order_type(direction, quote) -> market/limit + limit_price.

    # 6. SUBMIT TO BOTH LEGS via guardian.submit(order, leg.submit):
    #      - validate with guardian.validate_order first
    #      - submit to paper leg AND live leg (live may be floor-halted -> skip live)
    #      - capture paper_fill and live_fill prices for slippage
    #      - memory.log_decision(snap, order_type, intended_price, qty,
    #                            paper_fill, live_fill)
    #      - memory.open_tax_lot(symbol, qty, cost_basis)  # live fills only

    # 7. ATTACH TRAILING STOP on each new LIVE long:
    #      leg.attach_trailing_stop(symbol, qty, cfg.trail_percent)

    # 8. MANAGE EXITS: for existing positions, if a trailing stop filled or the
    #    signal flipped to no_trade/down, the position closes -> when detected,
    #    memory.close_decision(...) and memory.close_tax_lot(...).

    # 9. RECONCILE (call reconcile.py): intended state == Alpaca actual state.

    # 10. NOTIFY: build memory.daily_summary() + floor/halt status + slippage,
    #     send via notifier. Return the summary dict.
```
Important correctness rules:
- Wrap EVERY live/paper order through `guardian.submit` so broker errors trip the
  circuit breaker. `broker_alpaca.AlpacaLeg.submit` raises on error by design.
- Never place an order for a symbol below the liquidity floor (universe already
  filtered, but re-check the live spread before a market order).
- If `broker.live` floor is tripped, paper still runs (benchmark).
- Idempotency: if the runner is re-invoked the same day, do not double-submit.
  Track per-day submitted symbols in state.

### 4.4 `notifier.py`
End-of-day phone push. Provider: Telegram (free) or Pushover. Contract:
```python
class Notifier:
    def __init__(self, cfg): ...
    def send(self, title: str, body: str) -> None  # never raises into caller
def format_daily(summary: dict, status: dict) -> (title, body)
# summary from memory.daily_summary(); status from broker.status()
# body includes: trades opened/closed, realized P&L today, avg slippage bps,
#   paper vs live equity, floor status (and a BIG warning if tripped).
```
Reuse the `TelegramAlerter` pattern in `guardian.py` (it already does a safe
outbound call that never raises into trading logic).

### 4.5 `reconcile.py`
Catch silent failures (rejected/partial fills, state drift).
```python
reconcile(broker, memory, cfg) -> {ok: bool, discrepancies: [...]}
# Compare: positions the system THINKS it holds (from memory/state) vs
# leg.positions() ACTUAL from Alpaca, for BOTH legs. Report any symbol/qty
# mismatch. If discrepancy and severe, call guardian.manual_halt(...) and notify.
```

### 4.6 `main.py`
Thin entrypoint: load `config`, construct `DualBroker`, `Guardian`,
`TradeMemory`, call `strategy_runner.run_daily`, exit. Intended to be invoked by
cron / a scheduler once daily after close (e.g. 4:15pm ET). Also support a
`--dry-run` that runs steps 1–5 (signals/sizing) and prints intended orders
WITHOUT submitting.

---

## 5. SIGNAL DEFINITION (fill this in deliberately)
The composite signal is intentionally left as a documented stub for the user to
tune. A reasonable v1 (state it explicitly in code so it's testable):
- Trend/momentum: `mom_126_21 > 0` and `close > sma_50`.
- Volume confirmation: `obv_slope_20 > 0` and `kvo_hist > 0`.
- Not overbought: `rsi_14 < 70` and `mfi_14 < 80`.
- Direction: all three groups agree → `strong_up`; trend + one confirm → `mild_up`;
  else `no_trade`.
VALIDATE any change to this with `backtest.run_backtest` + `walk_forward_splits`
before trusting it live. Do not ship a signal that only works at zero cost or on
one date range.

## 6. SAFETY INVARIANTS (must always hold)
1. Live leg never trades after the −20% floor trips (persisted).
2. Every order passes `guardian.validate_order` and goes through `guardian.submit`.
3. No order for a symbol failing the liquidity floor.
4. No single position > 50% of equity (enforced in `broker_alpaca` AND re-checked
   in sizing).
5. Keys only from env; never logged.
6. Kill-switch file halts everything immediately.
7. Re-running the daily loop must not double-submit.

## 7. TEST REQUIREMENTS
- Keep `test_integrity.py` passing (look-ahead, survivorship, cost monotonicity).
- Add tests: floor trips at exactly ≤80% and halts persistently; position cap
  rejects >50%; universe screen excludes sub-threshold names; order-type hybrid
  picks market/limit per the matrix; reconcile detects an injected mismatch;
  idempotent re-run does not double-submit.
- All broker interaction tests use the PAPER endpoint or mocks. Never hit live in
  tests.

## 8. RUN BOOK (intended operation)
1. Set env vars (paper + live keys, notifier token).
2. Fund Alpaca live with $1,000. Keep paper at $1,000 to match.
3. Run `main.py --dry-run` after close; inspect intended orders.
4. Run live daily via cron at ~4:15pm ET.
5. Watch the EOD phone summary. Compare paper vs live equity (slippage).
6. If anything looks wrong: `touch KILL_SWITCH` to stop everything.
7. Weekly: review `trade_memory.db`. Retrain the signal only on a real sample,
   deliberately — never auto-retrain in the loop.

## 9. DEPENDENCIES
- Python 3.13. `alpaca-py==0.43.4`, `pandas`, `numpy`. SQLite (stdlib).
- Notifier: `requests` or stdlib `urllib` (Telegram via urllib already shown).

## 10. EXPLICIT NON-GOALS
- No intraday/HFT/scalping. No real-time self-retraining. No trading below the
  liquidity floor. No hardcoded secrets. No Robinhood. No options/crypto/futures
  in v1 (equities only).
