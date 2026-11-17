# Phase 0 Diagnosis — Why the Technical Signal Loses

Date: 2026-11-17. Window: TRAIN only, 2024-01-02..2026-08-17 (658 trading days).
Holdout (2026-08-18..2026-11-13) was NOT touched. Driver: `diagnose_phase0.py`,
metrics in `diagnostics.py`. Numbers: `phase0-metrics.json` (same folder).
Sheet: "Stock-thesis experiment log" → "Phase 0 diagnosis" tab.

## Headline

**The signal is gross-positive but ruinously expensive.** v1 earns +39.8%
before costs (gross Sharpe 0.76) and loses −28.4% after 10bps costs.
Costs ate $668.67 per $1,000 of capital over 658 days — 66.9% of the
account paid away in turnover. The best variant (v4) is gross +13.9%,
net −25.3%, costs $420.80 per $1,000. Every variant is net-negative at
both 10bps and 20bps, on train and on the (already-known) holdout.

But do not over-read the gross number: gross Sharpe 0.35–0.76 over 658
days is a t-stat of only ~1.2, and the Deflated Sharpe (corrected for
the 8 trials run) is **0.0 for every variant** — after accounting for
how many variants were tried, there is no statistically significant
edge even before costs. The gross edge is real-looking but weak; the
cost problem is what turns it into a large loss.

## The eight questions, answered

### 1. Gross vs net — bad signal or expensive signal?
Both, in that order of size: expensive first, weak second.
| variant | gross (0bps) | net 10bps | net 20bps | cost drag 10bps |
|---|---|---|---|---|
| v1_composite | +39.8% | −28.4% | −63.3% | 68.2 pts |
| v2_regime | +17.9% | −27.1% | −55.0% | 45.0 pts |
| v4_regime_quality_risk | +13.9% | −25.3% | −51.0% | 39.1 pts |

### 2. Per-trade — death by a thousand cuts
Yes. Median holding period is **1 day** for every variant; mean 2.0–3.1
days; 34–53 trades/month. Expectancy is −1.78 to −2.62 bps per trade
(net); profit factor 0.85–0.91; win rate 46–47.5%; average win ≈
average loss in size (~0.39% vs ~0.36%). There is no per-trade edge
after costs — and only a ~+2 bps/trade gross edge before them (v1).
A 1-day median hold means the portfolio is rebuilt almost every day
into marginally-different names.

### 3. Churn / costs
Total costs at 10bps: v1 $668.67, v2 $480.80, v4 $420.80 per $1,000.
Average cost 6.4–10.2 bps *per day*. Exposure is 99.8% invested on
99.8% of days, ~5 positions always — the system is never in cash and
never rests.

### 4. Why v2 == v3 — the trend-quality filter never binds
Only **27 of 23,318 active signal-days (0.12%)** fall below the
trend-quality threshold of 3.0; only 27 of 14,301 mild_up days.
Root cause, in the code: a mild_up signal already requires trend
(mom>0, close>sma_50 = 2 of the 5 quality points) plus one volume
confirmation (+0.5) plus usually rvol ≤ 0.60 (+1) or sma_20>sma_50 (+1),
so it almost always scores ≥ 3.0 anyway. The v3 filter is redundant
by construction — it cannot discriminate. Fix: raise the threshold
(e.g. ≥ 4.0) so it actually bites, or delete the filter.

### 5. The 1-bar lag costs 7–24 points of gross return
Same signal, no lag (LOOKAHEAD — diagnostic only, uses future
signals, never a result): v1 gross +63.9% vs +39.8% lagged (lag cost
24.1 pts); v2 +25.1% vs +17.9% (7.2 pts); v4 +25.1% vs +13.9%
(11.2 pts). The lag is real but secondary: even with no lag, v1's
~68 pts of cost drag would still leave it roughly flat-to-negative.
The lag is the honesty guard and cannot be removed — holding longer
is how its cost is reduced.

### 6. The regime filter is mixed — it hurts v1, helps v2/v4 slightly
Mean gross daily return on regime-allowed vs regime-blocked days:
v1 allowed +0.048% vs blocked +0.073% (the filter blocks the *better*
days); v2 allowed +0.042% vs blocked +0.013%; v4 allowed +0.029% vs
blocked +0.016%. Regime is on 62.4% of days. Verdict: not a reliable
on/off switch as built; test it as a sizing dial, or drop it.

### 7. Per-symbol — broad losses, no single culprit
v1: 66 symbols positive gross, 52 negative, of 118 ever held. Top
winner AMAT +14.2% gross (98 days held); top loser FCX −6.4%. v2/v4
show the same spread, with ABT/ADBE/ABBV held 250–290 days each while
losing −4% to −5% — long, slow bleeds alongside the 1-day flips. The
loss is systemic (turnover + weak signal), not one bad name.

### 8. Alpha — negative, uncorrelated with the market
Annualised alpha vs SPY: v1 −11.9%, v2 −9.9%, v4 −9.4%. Beta ≈ 0.0
(−0.03 to +0.04), R² 0.001 — the strategy is essentially uncorrelated
with the market and loses ~10%/yr against it. Information ratio
−1.28 to −1.39. Mean net daily return t-stat ≈ −1.0; 95% CI for the
annualised mean return is roughly [−33%, +11%] — the loss is not
statistically distinguishable from zero, which is itself the point:
nothing here clears any significance bar.

## Ranked fix hypotheses (for the next registered experiment)

1. **Cut turnover first — highest leverage.** Minimum holding period
   (e.g. 5 days) and/or weekly instead of daily rebalancing. Halving
   turnover roughly halves the 39–68 pts of cost drag; on v1's gross
   that alone moves net from −28% toward roughly flat-to-positive.
   This is the fix to test first.
2. **Be selective: strong_up only, or a higher score bar.** The
   equal-weight top-5 rotates into marginal names daily. Fewer,
   higher-conviction positions should raise gross expectancy per
   trade and cut trades at the same time.
3. **Fix or delete the dead filter.** Raise trend-quality to ≥ 4.0 so
   it discriminates, or remove v3 as a distinct variant — keeping a
   filter that binds 0.12% of the time is noise in the experiment log.
4. **Regime as a dial, not a switch.** Test scaling exposure (e.g.
   0.5× when the regime is off) instead of a hard block, or drop it
   — it currently blocks v1's better days.
5. **Hold longer to amortise the lag.** Same lever as (1): the lag
   cost scales with turnover, so fewer, longer holds shrink it.
6. **Do not stop at execution fixes.** Even gross, the signal's
   Sharpe (0.35–0.76) and Deflated Sharpe (0.0) do not clear a
   significance bar. If the turnover-fixed variant still fails its
   holdout, the signal idea itself needs strengthening — that is
   where the LLM sentiment leg (exp-technical-plus-llm-v1) earns
   its place, not before.

## What Phase 0 does NOT do

- No parameter was tuned on the holdout; no holdout number above is
  new (holdout results are from exp-technical-only-v1, already logged).
- No fix has been applied to the strategy. The next step is to
  register a new experiment (exp-technical-v2) with fixes 1–3, run it
  on train, and only then look at the frozen holdout.
- Kill rule reminder: if the fixed variant also fails its holdout or
  2x costs, it is dropped — not rescued by raising risk.
