# exp-sma50-timing-v1 — 50-session SMA entry/exit timing (RESULTS)

**Experiment:** `exp-sma50-timing-v1` · **Design:**
`experiments/exp-sma50-timing-v1-design.md` (registered before any result was
computed) · **Harness:** `sma_timing.py` · **Tests:** `test_sma_timing.py`
(10 tests, all passing) · **Results JSON:**
`experiments/results/exp-sma50-timing-v1.json`

**Command run:**
```
.venv/bin/python sma_timing.py --costs 10 20
```
**Window (train only):** 2024-01-02 – 2026-08-17, 658 sessions.
**Universe:** 131 symbols (133 fetched, 2 excluded — see below).
**Data:** split-adjusted Alpaca REST bars (feed `iex`, adjustment `split`),
warmup from 2023-01-01, cached in `.cache/backtest_data_split/`; the
registered raw cache was never touched. **Costs:** 10 bps and 20 bps on
traded notional. **Sizing/market filter:** identical to the registered v2
backtest (top-5 equal weight, weekly rebalance, 5-day min hold, SPY regime
mask). **No trailing stops.** Fills at the next session's open, never the
signal session's close.

**Two re-entry modes.** The design doc's §2 states re-entry only via a
fresh baseline selection "with an active v2 signal". The baseline's own
selections include all-zero days (see §1), so the harness distinguishes
two modes and runs both: **strict** (primary — the doc-literal rule: an
SMA block clears only on a selection with a positive v2 signal) and
**engine-faithful** (comparison — any fresh engine top-5 selection clears
the block, including all-zero picks). The strict variant is the headline
result; the engine variant is reported alongside because it is the
faithful replay of the baseline's selections.

## 1. Entry timing — NOT built (redundant by construction)

The Step 0 finding held on the full train panel. The baseline selects a
stock only when `close_t > sma_50_t` (`trade_signal.py` `composite_signal`
/ `composite_score`), and the entry executes at the next session's open,
when the latest completed close IS `close_t`. The entry gate therefore
never binds.

**Redundancy proof (registered check, §4.3 of the design):**

| | |
|---|---|
| Baseline entries checked | 370 |
| Gate bindings (close ≤ SMA50 at entry) | **0** |
| Waits / missed trades | 0 / 0 |

One quantified exception: **20 of the 370 entries trace to the engine's
all-zero top-5 picks.** On 247 of 658 train sessions no symbol has a
positive v2 signal (strict filter + regime mask), and
`signal_to_weights` still selects the top-5 by value even when all values
are 0 (verified by probe). Those 20 entries have NO buy signal, so the
entry rule's "wait while the signal remains valid" does not apply to them
as specified; the rule's only effect would be to suppress them. That is an
observation about the ENGINE's zero-handling, not about timing, and it is
out of scope here (the exit comparison replays the same baseline,
all-zero entries included). If the zero-signal picks should be suppressed,
that is a separate engine question.

Entry counts are therefore 0 better / 0 worse / 0 missed winners /
0 avoided losers, by construction, with the proof check as the evidence.

## 2. Exclusions and coverage

| | |
|---|---|
| Symbols fetched (split-adjusted) | 134 (incl. SPY as benchmark) |
| Excluded by the frozen >40% level-shift rule | **2** |
| Universe for the run | 131 |
| Missing opens in the run | 0 |
| Positions with insufficient SMA50 history (<50 sessions) | 0 (warmup covers all) |

- **DD** — 2025-11-03, −57.5% (Qnity spinoff, not a split).
- **HON** — 2026-06-29, −50.8% (Solstice spinoff, not a split).

Every genuine split (NVDA, AVGO, CMG, WMT, PANW, ORLY, NFLX, BKNG,
CRWD, NOW) was removed by the split adjustment and produced no remaining
shift. The raw cache would have false-crossed at every one of those dates
— see the split fixture in `test_sma_timing.py`.

## 3. Baseline vs exit-timing variants

The baseline is an **open-fill replay** of the registered v2 engine: the
simulator's entries/exits match the engine's held-weight transitions
exactly (pinned by `test_baseline_sim_matches_engine_transitions`).
Headline cost is 10 bps. **Strict is the primary variant** (doc-literal
re-entry); engine-faithful is the comparison.

| Metric | Baseline @10bps | Strict @10bps | Engine @10bps | Baseline @20bps | Strict @20bps | Engine @20bps |
|---|---|---|---|---|---|---|
| Net return after costs | −7.82% | **−6.11%** | −6.38% | −20.50% | **−18.69%** | −20.29% |
| Max drawdown | −22.26% | −16.87% | −19.48% | −29.65% | −26.21% | −29.56% |
| Avg daily turnover | 0.2333 | 0.2241 | 0.2519 | 0.2332 | 0.2240 | 0.2518 |
| Avg gross exposure | 0.9955 | 0.6723 | 0.9044 | 0.9955 | 0.6724 | 0.9044 |
| Time invested | 0.9985 | 0.8343 | 0.9985 | 0.9985 | 0.8343 | 0.9985 |
| Entries | 370 | 360 | 402 | 370 | 360 | 402 |
| Baseline exits | 365 | 263 | 281 | 365 | 263 | 281 |
| SMA exits | — | 92 | 116 | — | 92 | 116 |
| Min-hold overrides | — | 64 | 66 | — | 64 | 66 |
| Whipsaws | — | 49 (53.3%) | 61 (52.6%) | — | 49 | 61 |
| **Net timing effect** | — | **+1.71 pp** | +1.44 pp | — | **+1.81 pp** | +0.21 pp |

**Exposure-matched benchmarks** (variant's daily exposure × SPY return,
no costs): strict +31.15% return, −7.34% max drawdown at 0.6723 avg
exposure; engine +49.22%, −18.52% at 0.9044. The baseline's own
exposure-matched benchmark is +65.09%, −18.98% at 0.9955 exposure.
SPY buy-and-hold over the same window is +63.48% (registered).

**Re-entry accounting (strict, @10bps):** 65 blocks cleared on a
positive-signal selection; **170 all-zero selections refused** (the block
stayed in place); 11 variant-only re-entries, all signal-cleared. The
strict variant made only 360 entries vs the baseline's 370 — it skipped
21 baseline entries of blocked symbols (all-zero-day selections with no
signal). (Engine: 59 signal + 31 all-zero clearings; 32 variant-only
re-entries, 22 of them all-zero — most of the engine variant's extra
entries were re-buys on days with no buy signal.)

**Reconciliation vs the registered v2 train numbers:** the registered
close-to-close engine is −14.01% @10bps / −25.62% @20bps. This experiment's
open-fill baseline is −7.82% / −20.50% — a gap of +6.19 pp @10bps and
+5.12 pp @20bps. That gap is the overnight-fill effect the design doc
anticipated (entries/exits at the next open catch overnight moves the
close-to-close engine does not). The baseline-vs-variant comparison inside
this experiment is apples-to-apples (both use open fills), so the gap does
not affect the timing conclusion.

## 4. Where the timing effect comes from (strict variant)

Per timing exit, timing exit price vs the baseline's eventual exit price
for the SAME holding (85 of 92 exits have a baseline counterfactual; the
other 7 were holdings the baseline still held at window end, so no
counterfactual price exists — they are excluded from this breakdown but
included in the total-return effect).

| | Strict @10bps | Strict @20bps | Engine @10bps |
|---|---|---|---|
| Losses avoided (n) | $159.25 (35) | $145.57 (35) | $225.09 (38) |
| Gains forfeited (n) | −$164.37 (36) | −$153.82 (36) | −$200.14 (41) |
| **Net direct price effect** | **−$5.12** | **−$8.25** | +$24.95 |
| (on a $1,000 book) | −0.01 pp | −0.02 pp | +0.02 pp |

For the doc-literal (strict) variant, the direct exit-price effect is
**negative**: 35 exits avoided a further loss, 36 forfeited a later gain,
net −$5 on a $1,000 book. The headline +1.71 pp total effect therefore
does NOT come from the exits being better prices — it comes from trading
less (turnover 0.2241 vs baseline 0.2333, so lower costs) and holding less
(avg exposure 0.6723 vs 0.9955, in a window where the strategy loses
money). The timing rule itself, priced exit-by-exit, is a wash-to-negative.

**Whipsaws (strict):** 49 of 92 timing exits (53.3%) saw the stock close
back above its SMA50 within 5 sessions. More than half the rule's exits
are reversed almost immediately. **Min-hold overrides:** 64 of 92 exits
cut a position inside the engine's 5-day minimum hold.

**Uncertainty (frozen trade-level bootstrap, 1,000 resamples, seed 7):**
mean −$4.25, 5th/95th percentile **[−$99.72, +$81.82]** @10bps
([−$97.18, +$72.61] @20bps). The interval includes zero comfortably;
trades overlap in calendar time, so this overstates independence — it is
not a p-value.

## 5. Sensitivities (frozen, §6 of the design) — both fail (strict variant)

| Sensitivity | Result @10bps | Result @20bps |
|---|---|---|
| Ex-largest-contributor (drop the single biggest exit, AMZN +$20.17) | Net price effect −$5.12 → **−$25.29** | −$8.25 → **−$26.17** |
| Entries delayed one additional session (variant re-run, same baseline) | Net timing effect +1.71 pp → **−5.16 pp** | +1.81 pp → **−4.29 pp** |

The direct price effect is already negative before removing anything;
dropping the largest positive exit makes it −$25.29. And the headline
total effect does not survive a one-session entry delay — it flips to
−5.16 pp, a 6.87 pp swing from a single session of execution timing.
The +1.71 pp is not robust. (The engine variant fails the same two
sensitivities: ex-largest +$24.95 → −$5.72; entry delay +1.44 pp →
−6.21 pp.)

## 6. Dated trade walkthroughs (strict variant)

### 6.1 Where the line helped — AMZN, Jan–Feb 2026 (+$20.17)

- **Entry:** 2026-01-30, open $239.95 (baseline selection; close above SMA50).
- **Cross:** 2026-02-03 close $238.63 vs SMA50 $233.26 (above); **2026-02-04
  close $232.97 vs SMA50 $233.57 (below)** — the cross is known at Wednesday's close.
- **Timing exit:** 2026-02-05 open **$225.00** (the next session's open, not
  Wednesday's close). Min-hold override: entry to exit was 4 sessions,
  inside the 5-day minimum hold.
- **What happened next:** AMZN fell further — 2026-02-05 close $222.46,
  2026-02-06 open $202.62 (gap down), close $210.25 — and stayed below
  its SMA50. Not a whipsaw.
- **Baseline exit:** 2026-02-06, $202.62.
- **Effect:** (225.00 − 202.62) × 0.90 shares ≈ **+$20.17** — the largest
  single timing effect in the strict run, and the trade whose removal
  takes the net price effect to −$25.29 (§5).

### 6.2 Where the line hurt — AAPL, Apr–May 2024 (−$25.75, whipsaw)

- **Entry:** 2024-04-22, open $165.53.
- **Cross:** 2024-04-29 close $173.48 vs SMA50 $173.10 (above); **2024-04-30
  close $170.05 vs SMA50 $172.85 (below)** — the cross is known at Tuesday's close.
- **Timing exit:** 2024-05-01 open **$169.82** (Wednesday's open).
- **Whipsaw:** 2024-05-02 close $173.13 vs SMA50 $172.42 — back above
  within **one** session; AAPL ran to $183.36 by May 3.
- **Baseline exit:** 2024-05-28, $191.53.
- **Effect:** (169.82 − 191.53) × 1.19 shares ≈ **−$25.75** — the largest
  single timing loss in both variants, and a textbook whipsaw: the rule
  sold the dip one day before the recovery.

## 7. Decision — NEITHER

Checked against the frozen decision rule (§7 of the design), for the
primary (strict) variant — the engine variant fails the same legs:

- **Entry timing:** does not deserve a prospective test. The gate never
  binds (0/370). There is no entry-timing effect to test.
- **Exit timing:** does not deserve a prospective test.
  1. Net timing effect positive after costs? **Yes, but thin and
     misleading:** +1.71 pp @10bps, +1.81 pp @20bps — and the direct
     exit-price effect is **negative** (−$5.12). The headline gain is
     lower turnover and lower exposure, not better exit prices.
  2. Survives both sensitivities? **No.** Ex-largest-contributor takes
     the price effect to −$25.29; a one-session entry delay flips the
     total effect to −5.16 pp.
  3. Beats the exposure-matched benchmark? **No.** Variant −6.11% vs
     benchmark +31.15%; drawdown −16.87% vs benchmark −7.34%. The
     variant's drawdown improvement vs the baseline is fully explained
     by lower exposure — the benchmark at the SAME exposure has a far
     smaller drawdown. The rule adds no timing value beyond holding less
     stock.

The direct evidence is negative-to-a-wash (−$5 on 85 matched exits, 35
avoided vs 36 forfeited), more than half the exits are whipsaws, and the
headline effect is lower turnover and lower exposure that does not survive
either sensitivity. **Recommendation: neither entry timing nor exit timing
deserves a future prospective test on this evidence.** What would decide
it: a longer independent window (the frozen holdout and the January
validation window are off-limits for this question), or a pre-registered
prospective test — but this result does not justify running one.

## 8. Queued hypotheses (document only — NOT built)

- **20-session EMA:** faster exits, recent prices weighted more. Entry
  redundancy must be re-checked there — the signal does not reference
  EMA20, so the entry variant may NOT be redundant.
- **Prior 20-session high/low channel:** breakouts/breakdowns; exclude the
  current session.
- **VWAP:** reserved for an intraday execution study with suitable data
  (how good was a simulated fill relative to trading during the execution
  window).

## 9. Audit trail

- Design (frozen before results): `experiments/exp-sma50-timing-v1-design.md`
  — §1b amended 2026-11-26 with the quantified redundancy proof (370
  entries, 0 bindings, 20 all-zero) and the two spinoff exclusions; §2
  clarified 2026-11-26 that two re-entry modes are distinguished — strict
  (doc-literal, primary) and engine-faithful (comparison) — because the
  baseline's own selections include all-zero days.
- Harness: `sma_timing.py` (new file). Tests: `test_sma_timing.py`
  (10 tests: overnight gap, holiday/Thanksgiving, insufficient history,
  stock split, the Tuesday→Wednesday clock, whipsaw definition,
  entry-gate redundancy on synthetic data, block-clear signal vs
  all-zero (both modes), fresh-selection replication on the real panel,
  baseline structural match vs the engine).
- Full suite after this work: **325 passed**. Of those, 10 are this
  experiment's new tests (286 pre-existing + 10 mine = 296); the other
  29 are the concurrent filing-delta experiment's tests (a separate
  workstream running in the same tree, not this experiment's).
- Files changed: `experiments/exp-sma50-timing-v1-design.md` (new),
  `sma_timing.py` (new), `test_sma_timing.py` (new),
  `experiments/results/exp-sma50-timing-v1.json` (new),
  `experiments/exp-sma50-timing-v1.md` (this file, new).
  `.cache/backtest_data_split/` is new cache data (not committed source).
  No existing source file was modified. No PROGRESS.md / ATS_LOOPS.md /
  registry / Sheet writes (parent's scope).
