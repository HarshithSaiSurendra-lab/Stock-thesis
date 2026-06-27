# Quant Trading System — Phase 1 Prototype

A bias-aware research and backtesting foundation for a retail quant strategy.
Built signal-first: the goal is a backtest you can *trust*, because the edge for
a retail trader is signal quality and discipline, not execution speed.

## Why it's built this way

The strategy work established that at retail scale you cannot win the speed race,
so the entire value is in finding a real signal and not fooling yourself about it.
That means the backtest engine's job is to be *honest*, even when honesty is
disappointing. Every component is designed around the three biases that produce
fake backtests:

| Bias | Where it's handled | How to verify |
|------|-------------------|---------------|
| Look-ahead | `backtest._lag_weights` — weights act one bar after the signal | `test_integrity.py` runs a perfect-foresight signal and proves it *can't* print money |
| Survivorship | engine treats NaN symbols as ineligible, never forward-fills across listing gaps | integrity test takes zero positions in a not-yet-listed name |
| Cost-free fantasy | per-rebalance transaction cost on weight changes | Sharpe visibly degrades as `cost_bps` rises |

## Files

- `indicators.py` — the six-indicator volume-flow system (KVO, OBV, Weighted A/D,
  MFI, RSI, moving averages) plus momentum and realized vol, as vectorized,
  no-look-ahead functions. `build_feature_frame()` assembles them for one symbol.
- `backtest.py` — cross-sectional long / long-short daily backtest engine with the
  bias guards, transaction costs, optional vol targeting, and walk-forward splits.
- `test_integrity.py` — adversarial tests that try to make the engine lie and
  prove it doesn't. **Run these after any change to `backtest.py`.**

## Run it

```bash
python3 indicators.py        # indicator smoke test on synthetic data
python3 backtest.py          # backtest across 3 cost levels
python3 test_integrity.py    # the tests that matter most
```

## What's REAL vs. STUBBED

Real and working:
- Indicator math (verified: NaN warmup periods are correct, nothing peeks ahead)
- The bias-guarded backtest loop, cost model, and stats
- Walk-forward split generator
- The integrity test suite

Still stubbed / next:
- **Real data.** Everything currently runs on synthetic prices. The injected
  momentum effect is why the demo Sharpe looks good — it is NOT a result.
  Next step is wiring a survivorship-free data source (Alpaca/Tiingo for prices,
  later Polygon/Databento for tick data; CRSP via WRDS once you have GT access).
- **The signal itself.** Right now the demo uses raw 6-1 momentum. The point of
  the indicator library is to combine your six indicators into a learned signal
  (XGBoost on the feature frame, predicting forward returns) — that's the next build.
- **Live execution.** No broker connection yet. When ready, this targets Alpaca
  (documented API, WebSocket, FIX on higher tiers), NOT Robinhood.

## The discipline rule

Before any strategy graduates from this engine to real money:
1. It must survive `cost_bps` set to a *realistic* level (start at 5–10 for liquid
   US equities, higher for anything illiquid).
2. It must hold up across the `walk_forward_splits` — not just one lucky period.
3. If it only works at zero cost or on one date range, it is not a strategy.

> "Backtesting while researching is like drink driving. Do not research under the
> influence of a backtest." — López de Prado. The integrity tests exist so you
> stay sober.

---

## Safety layer (`guardian.py`)

Built for the hands-off Robinhood-agent case, where the agent can trade while
you're not watching. The posture is **the broker connection is untrusted** — the
dangerous failure isn't a crash (that's safe, nothing trades) but the code
running on bad information. Five protections, checked before every order:

1. **Kill switch** — if the `KILL_SWITCH` file exists, nothing trades. Trip it
   from anywhere (phone, cron, script). Cannot be auto-cleared; you must remove
   the file. This is your hard stop.
2. **Circuit breaker** — auto-halts on: daily loss limit (3%), too many orders
   (count + rate), consecutive broker errors (the "API goes bad" case), or stale
   market data.
3. **Pre-trade checks** — every order validated for size, notional cap
   ($2,500/order), price sanity vs last quote, and position concentration (25%).
4. **Heartbeat/health** — `health_report()` proves the system is alive AND the
   data is fresh. A frozen feed halts trading — it's more dangerous than a crash.
5. **Alerting** — every trip/error pushes a message. Default logs to file +
   stdout; `TelegramAlerter` template included — add a bot token for phone alerts.

State persists to `guardian_state.json`, so a restart can't reset daily counters
(an agent can't dodge the loss limit by crashing and restarting).

### Wiring it up
```python
g = Guardian(cfg, get_account=broker.account, last_data_ts=feed.last_ts,
             alerter=TelegramAlerter(cfg.alert_log_path, TOKEN, CHAT_ID))
g.start_day()
if g.can_trade():
    ok, why = g.validate_order(order, last_price, equity, positions)
    if ok:
        g.submit(order, broker.place_order)   # tracks errors, trips breaker
```

### Honest limit
The guardian protects against bad *behavior* (wrong orders, runaway loops, dead
APIs, frozen feeds) but cannot make Robinhood's undocumented API *reliable*. It
makes you fail SAFE instead of failing SILENT. Alpaca's documented API remains
the structurally safer base for full automation — run the guardian regardless.

---

## Alpaca dual-account engine (`broker_alpaca.py`)

Runs ONE signal against TWO Alpaca accounts at once:
- **Paper** — idealized fills; the "what the strategy theoretically earned" baseline.
- **Live** — real fills with real slippage; the "what I actually captured" reality.

The gap between them is your live, continuous measurement of execution cost.

**Capital floor:** if the LIVE account equity drops to ≤80% of its $1,000 start
(a −20% drawdown), the engine liquidates all live positions and halts the live
leg PERMANENTLY (persisted across restarts). Paper keeps running as a benchmark.

**Single-position cap:** no position may exceed 50% of that account's equity.

**Market-vs-limit hybrid** (`decide_order_type`): strong signal + tight spread →
market (take the move); mild signal → limit at bid (let it come to you); wide
spread → limit regardless (never pay the spread).

**Paper/live switch & keys:** keys come from env vars only — never hardcoded,
never in chat. Set `ALPACA_PAPER_KEY/SECRET` and `ALPACA_LIVE_KEY/SECRET`.
A leg with missing keys disables itself gracefully instead of crashing.

## Memory / trade journal (`memory.py`)

Continuous memory, retrieval before action, deliberate (not real-time) learning.
- **Records** every decision: full signal snapshot, liquidity, intended vs actual
  fill, paper-vs-live slippage, and closed-trade outcome.
- **Retrieves** similar past setups BEFORE acting (`similar_setups`) and returns
  win rate + verdict — references history instead of guessing.
- **Flags** setups resembling past losers (`flag_if_repeating_loss`).
- **Tax**: per-lot cost basis, short/long-term classification, yearly summary.
- **Daily summary** feeds the end-of-day phone notification.

It deliberately does NOT retrain a model off recent trades in real time (that
chases noise). Learning is human-gated: export the journal, review on a real
sample, retrain on purpose.

## What's left to build
- **Strategy runner** — the daily loop tying it together: pull universe (with
  liquidity floor), compute signals via `indicators.py`, check `memory` for
  similar setups, size positions, route through `decide_order_type`, submit to
  both legs via `DualBroker`, attach trailing stops, log to `memory`.
- **Notifier** — wire the daily summary to a phone push (Telegram/Pushover).
- **Reconciliation** — daily check that intended state == Alpaca's actual state.
