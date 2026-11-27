# exp-sma50-timing-v1 — stock-level 50-session SMA entry/exit timing

**Experiment ID:** `exp-sma50-timing-v1`
**Registered:** 2026-11-26 (BEFORE any result was computed — this file was
written first, then the harness, then the run).
**Question:** Does a simple 50-session SMA trend rule improve the timing of
decisions the existing model already makes?
**Status:** registered; run on the TRAIN window only (see §5).

## 1. Step 0 finding — read before building anything

The signal and regime code were inspected first
(`trade_signal.py`, `indicators.py`, `filters.py`, `strategy_runner.py`,
`backtest.py`, `research_runner.py`, `run_v2_backtest.py`).

### 1a. Where moving averages and the SPY filter already influence decisions

- **Stock level, signal:** `trade_signal.py` `composite_signal()` and
  `composite_score()` both require `trend = (mom_126_21 > 0) &
  (close > sma_50)` (trade_signal.py:27, :43). A buy label
  (`strong_up` / `mild_up`) is impossible on a day whose close is at or
  below its 50-session SMA. `trend_quality_score()` also awards a point
  for `close > sma_50` (trade_signal.py:78).
- **Stock level, runner:** `strategy_runner.py` `run_daily()` computes the
  label from the latest feature row (line ~697) and skips the symbol when
  the label is `no_trade` (line ~759), with further gates on momentum,
  63-day relative strength, trend quality, and decision score.
- **Market level, regime:** `filters.py` `market_regime()` blocks ALL new
  longs unless SPY is above its SMA50 AND above its SMA200 AND its
  SMA20 is above its SMA50, with positive momentum and drawdown / realized-
  vol caps (filters.py:130-145; config defaults
  `REGIME_REQUIRE_ABOVE_SMA_50=1`, `REGIME_REQUIRE_ABOVE_SMA_200=1`,
  `REGIME_REQUIRE_SMA_20_ABOVE_SMA_50=1`). The research replay uses the
  identical mask (`research_runner.py` `_regime_mask`, lines 232-247).
- **Registered baseline:** `research_runner.py` `build_v2_signal()` builds
  the v2 panel as `composite_score` where the label is `strong_up` AND
  trend-quality >= 4.0, else 0, then applies the regime mask
  (lines 306-340). `build_signal_variants()` gates v1/v3/v4 the same way
  on the labels (lines 249-290). The backtest engine
  (`backtest.py` `run_backtest`) lags target weights one bar, so a signal
  known at close t is acted on at the open of t+1.

### 1b. Entry timing is REDUNDANT by construction — not built as a variant

The proposed entry rule: permit entry at the next session's open only if
the latest completed close is above its 50-session SMA; if below, wait
while the buy signal remains valid.

For the registered baseline this gate can never bind:

1. The baseline selects a stock on day t only if its label is active on
   day t, which requires `close_t > sma_50_t` (see §1a).
2. The entry executes at the open of the next session, when the latest
   completed close IS `close_t` — above its SMA50 by (1).
3. "Wait while the signal remains valid" adds nothing: the signal is
   re-derived daily and is valid on a later day d only if
   `close_d > sma_50_d`, so the gate would pass on any later day too.
4. The v2 backtest only changes weights on rebalance days (every 5th
   session, `V2_CFG` in `run_v2_backtest.py:57`); an entry is decided by
   the rebalance day's signal, which again requires that day's close
   above its SMA50. The 5-day min-hold delays exits only, never entries.

Consequence, frozen: **version 2 (entry timing) is NOT built as a
research variant.** Running it would be a duplicate of the baseline.
Instead a redundancy-proof check is registered (§4.3): replay the entry
gate over the full train panel and assert it binds 0 times (0 waits,
0 missed trades). If that check ever finds a binding case, this finding
is wrong and version 2 must be built and run before any conclusion about
entry timing is drawn. The entry count breakdown (better prices, worse
prices, missed winners, avoided losers) is therefore 0/0/0/0 by
construction, with the proof check as the evidence.

**Quantified on the split-adjusted train panel (2026-11-26, Step 0
evidence — not a result):** 370 baseline entries checked; **binding 0**.
The gate never binds for a signal-driven entry. (An earlier count of
365 missed 5 day-1 entries through a NaN-handling bug in the check
itself — the held-weight matrix's first row is NaN from the 1-bar lag;
fixed by filling 0.0, re-counted at 370, binding still 0.) One quantified
exception: **20 of the 370 entries trace to the engine's all-zero
top-5 picks** — on 247 of 658 train sessions no symbol has a positive
v2 signal (strict filter + regime mask), and `signal_to_weights`
still selects the top-5 by value even when all values are 0
(verified by probe: an all-zero panel yields 0.2 weight to each of
the first 5 names). Those 20 entries have NO buy signal, so the entry
rule's "wait while the signal remains valid" does not apply to them as
specified; the rule's only effect would be to suppress them. That is
an observation about the ENGINE's zero-handling, not about timing, and
it is out of scope for this experiment (the exit comparison replays the
same baseline, all-zero entries included). If the parent wants the
zero-signal picks suppressed, that is a separate engine question, not a
timing question.

### 1c. Exit timing is GENUINELY DISTINCT — this is the experiment

The baseline has NO stock-level SMA-cross exit:

- Live runner default: `exit_on_signal_loss = False`
  (`config.py:99`, env `EXIT_ON_SIGNAL_LOSS` default 0). With the default
  config, a holding is exited only by its trailing stop
  (`strategy_runner.py` ~line 1004, `attach_trailing_stop`); the
  signal-loss exit loop (lines ~1027-1082) does not run.
- Backtest baseline: exits happen only when a symbol's weight goes to 0 —
  weekly top-N re-ranking, the 5-day min-hold, or the regime mask turning
  off. A symbol whose close crosses below its SMA50 loses the trend
  component (2 score points) and may keep a positive score from the other
  components (up to 3.5 without trend), so it can stay in the top-5 and
  stay held for up to 5 more sessions (rebalance + min-hold delay).
- The proposed rule exits at the next open after the cross — up to ~5
  sessions earlier than the baseline exit, and it exists at all in the
  live default config.

So the experiment is: **baseline (v2 replay) vs exit-timing-only**.

## 2. Versions (frozen)

All versions run on the SAME dated baseline signals (the registered v2
panel), with identical sizing, market filters, and cost assumptions.
Only the exit rule differs.

1. **Baseline:** replay the v2 panel unchanged. Entries/exits exactly as
   the v2 backtest defines them (top-5 equal weight, weekly rebalance,
   5-day min hold, regime mask, 1-bar lag). Fills at the next session's
   open (see §3).
2. **Entry timing only:** NOT BUILT — redundant by construction (§1b).
   Replaced by the redundancy-proof check (§4.3).
3. **Exit timing only:** keep baseline entries. For an existing holding,
   exit at the next session's open when the completed close crosses from
   at-or-above its SMA50 to below it (cross on session t iff
   `close_{t-1} >= sma_50_{t-1}` AND `close_t < sma_50_t`; exit at the
   open of the next session after t). Preserve any earlier baseline exit
   (weight going to 0, regime off). The SMA exit OVERRIDES the 5-day
   min-hold — it is an explicit earlier-exit rule — and each override
   inside the min-hold window is counted.

### Re-entry rule (frozen before results)

After an SMA exit, a stock can re-enter ONLY via a fresh baseline
selection (top-5 on a later rebalance day with an active v2 signal).
No re-entry on the exit day. The SMA rule never creates entries. A
stock that crossed below its SMA50 has signal 0 by construction, so it
cannot be selected until its close is back above its SMA50 and the
label returns.

**Clarification added 2026-11-26 (before the results were written,
during verification):** the baseline's own selections include all-zero
days — on 247 of 658 train sessions no symbol has a positive signal and
the engine still selects its top-5 by value (§1b). "A fresh baseline
selection" therefore has two readings, and the harness distinguishes
them, running BOTH and reporting both:
- **strict (PRIMARY, the doc-literal rule):** the block clears only on a
  fresh selection WITH a positive v2 signal for that symbol. An all-zero
  selection leaves the block in place (counted as refused).
- **engine-faithful (comparison):** any fresh engine top-5 selection
  clears the block, including all-zero picks. This is the faithful replay
  of the baseline's selections, but it can re-enter a stock on a day with
  no buy signal, including while it is still below its SMA50.
The strict variant is the headline result; the engine variant is the
comparison. The re-entry counters (`n_block_cleared_signal`,
`n_block_cleared_all_zero`, `n_block_refused_all_zero`,
`extra_reentries_by_clearing_kind`) make the difference auditable.

### Whipsaw definition (frozen before results)

A timing exit is a **whipsaw** if any close in the 5 sessions after the
cross session (sessions t+1 through t+5, each compared with that
session's own SMA50) is at or above its SMA50. N = 5, matching the
rebalance / min-hold cadence. Whipsaws are counted among timing exits;
their round-trip cost (exit cost + re-entry cost if re-entered) is
reported separately.

## 3. Execution, costs, data (frozen)

- **Fills:** at the next session's open, at the open price. A crossover
  known at Tuesday's close can first affect Wednesday's open (assuming
  Wednesday is a trading session). Never use the signal session's own
  close as a fill price. Sessions are the panel's own index (holidays
  are absent from it); `market_calendar.is_trading_day` is used to
  verify session existence in fixtures.
- **Costs:** 10bps and 20bps on traded notional, same model as
  `backtest.py` (cost = |weight change| x bps / 1e4). Headline at
  10bps, matching the registered v2 convention. The SMA exit pays cost
  on the exited notional at the exit open; a later re-entry pays again.
- **Sizing / market filter:** top-5 equal weight, weekly rebalance
  (every 5th session), 5-day min hold, regime mask — identical to
  `V2_CFG` (`run_v2_backtest.py:57`). No trailing stops (plain engine,
  same as v1/v2).
- **Data — split-adjusted (this is load-bearing):** the registered cache
  (`.cache/backtest_data`, `market_data_adjustment: raw`) contains
  unadjusted bars. Verified 2026-11-26: NVDA 10:1 split 2024-06-10 shows
  as a -89.9% day, AVGO 10:1, CMG 50:1, WMT 3:1, PANW 2:1, ORLY 15:1,
  NFLX 10:1 and further 2025-2026 level shifts (BKNG, CRWD, NOW) all
  show as permanent level shifts with volume scaling — i.e. splits, not
  market moves — plus DD's Qnity spinoff (-57.5%, 2025-11-03), which no
  split adjustment corrects. A raw SMA50 line would produce false
  crosses at every one of these. This experiment therefore uses a
  FRESH split-adjusted fetch via the existing Alpaca data path
  (`research_runner.fetch_alpaca_rest_history`, feed IEX,
  `adjustment='split'`), cached separately as
  `.cache/backtest_data_split/` and NEVER overwriting the registered raw
  cache. Verified on the probe: NVDA split-adjusted closes 2024-06-05..
  2024-06-12 run ~120-125 with no cliff. Warmup from 2023-01-01 so
  SMA200 / 126-21 momentum are formed on day one of the window.
- **Exclusion rule (frozen before the run):** after the split-adjusted
  fetch, scan every symbol for any remaining single-day |return| > 40%
  level shift. Any symbol with an unexplained remaining shift is
  EXCLUDED from the universe, with the date and magnitude recorded.
  DD is expected to trip this rule (spinoff, not a split). Exclusion
  counts are reported.
- **Applied (2026-11-26, Step 0 evidence):** the rule excluded exactly
  two symbols — DD (2025-11-03, -57.5%, Qnity spinoff) and HON
  (2026-06-29, -50.8%, Solstice spinoff). Universe for the run: 131
  symbols (133 minus DD, HON). Every other split (NVDA, AVGO, CMG,
  WMT, PANW, ORLY, NFLX, BKNG, CRWD, NOW) was removed by the split
  adjustment and produced no remaining shift.
- **Insufficient history:** fewer than 50 sessions of closes means no
  SMA50; the symbol is not eligible for a timing exit and the case is
  recorded as `insufficient` (the baseline signal is already NaN there
  by construction).

## 4. Metrics and benchmarks (frozen)

Per version, at 10bps (headline) and 20bps: net portfolio return after
costs, max drawdown, avg daily turnover (|weight change|), time
invested (fraction of sessions with gross exposure > 0), average gross
exposure, distinct issuers traded, number of trades (entries), number
of timing exits.

- **Entry counts** (better prices / worse prices / missed winners /
  avoided losers): 0/0/0/0 by the §1b finding, with the redundancy-proof
  check as evidence.
- **Exit counts:** losses avoided (timing exit price above the price the
  baseline eventually exited the same position at — or the window-end
  mark if the baseline never exited), gains forfeited (timing exit below
  the baseline's eventual exit price), whipsaws (per §2).
- **Exposure-matched benchmark (frozen construction):** daily return =
  the variant's gross exposure on day t x SPY's return on day t; no
  costs; same window. If the variant's drawdown is smaller than the
  exposure-matched benchmark's, timing added value beyond holding cash;
  if similar, the smaller drawdown came mainly from holding more cash.
  Also report SPY buy-and-hold and equal-weight universe (no cost), as
  in the registered v2 results.
- **Sensitivity (frozen):** ex-largest-contributor (remove the single
  timing exit with the largest absolute P&L contribution and recompute
  the net timing effect); entry delayed one additional session (all
  timing exits executed at the open one session later than specified).
- **Uncertainty (frozen):** trade-level bootstrap over timing-exit
  outcomes (1,000 resamples with replacement) for the net timing effect
  vs baseline; report the 5th/95th percentile interval WITH the caveat
  that trades overlap in calendar time, so the interval is indicative
  and overstates independence — it is not a p-value.

## 5. Window and non-contamination (frozen)

- **Run window:** TRAIN ONLY, 2024-01-02 .. 2026-08-17 (658 sessions,
  per `experiments/holdout_dates.json`). Warmup bars from 2023-01-01.
- **NEVER** the frozen holdout (2026-08-18 .. 2026-11-13) or the January
  validation window (2026-11-16 .. 2026-12-31). If the available baseline
  signals cannot support a pre-Aug-18 run, report that limitation
  instead of reaching into those windows.
- **Baseline reconciliation:** the harness baseline is an OPEN-FILL
  replay of the v2 signals, not the registered close-to-close engine.
  The registered v2 train numbers (total return -14.01% @10bps /
  -25.62% @20bps, max drawdown -49.79% / -55.21%, avg daily turnover
  0.2201, 364 trades, 658 days; SPY buy-and-hold +63.48%) are a
  reconciliation reference only. The baseline-vs-variant comparison
  inside this experiment is apples-to-apples (same open-fill execution,
  same costs), and any gap vs the registered numbers is reported as the
  overnight-gap effect of filling at the open instead of the close.

## 6. Queued hypotheses (document only — NOT built here)

- **20-session EMA:** recent prices weighted more heavily; tests faster
  exits. Distinct hypothesis: same structure as this experiment with
  EMA20 replacing SMA50 (entry redundancy must be re-checked — the
  signal does not reference EMA20, so the entry variant may NOT be
  redundant there).
- **Prior 20-session high/low channel:** tests breakouts and breakdowns.
  The channel MUST exclude the current session (prior 20 sessions only).
- **VWAP:** answers a different question — how good was a simulated fill
  relative to trading during the execution window. Reserved for an
  intraday execution study with suitable data; not a daily timing rule.

## 7. Decision rule (frozen before results)

Recommend ONE of: **entry timing** / **exit timing** / **neither**
deserves a future prospective test, tied to the evidence:

- Entry timing: "deserves a test" only if the §4.3 proof check fails
  (binds > 0). Otherwise the recommendation is that it is already
  enforced by the signal and a prospective test would duplicate it.
- Exit timing: "deserves a test" only if the net timing effect vs
  baseline is positive after costs AND survives both sensitivities
  (ex-largest-contributor, +1 session delay) AND the drawdown
  improvement is not explained away by the exposure-matched benchmark
  (i.e. it beats the exposure-matched benchmark, not just the raw
  baseline). Otherwise: neither.
- An inconclusive result (effect within the bootstrap interval, or
  sensitivities flip the sign) is a valid outcome: recommend neither,
  and say what data would decide it.
