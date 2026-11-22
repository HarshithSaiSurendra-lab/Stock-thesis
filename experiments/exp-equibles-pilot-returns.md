# Equibles Pilot Returns Test — Results

Generated 2026-11-22. Machine-readable:
`exp-equibles-pilot-returns.json`. Design (registered BEFORE running):
`exp-equibles-pilot-returns-design.md`. Runner:
`run_pilot_returns.py`. Prices:
`experiments/equibles-pilot-raw/prices-adjusted-*.csv` (Alpaca daily
bars, adjustment = all, split + dividend).

## Verdict: NO EVIDENCE

The Q&A scores do not predict 20-day post-call returns in this
pilot. No signal clears the pre-registered bar (|IC| ≥ 0.3 with
p < 0.10 on the score). Per the design, there is no gate pass and no
DROP/SCALE verdict on the earnings-call leg from this test alone —
but the leg has no supporting evidence from its first direct test.

## What was tested

16 of the 21 scored calls (5 excluded — see below). Returns are
adjusted-close returns from the call date T to T+20 (20th trading
day after T); excess returns are vs SPY over the same window. T is
the call date even when the call was after market close, per the
registered design.

## Headline ICs (n = 16, except QoQ delta n = 10)

| Signal | vs raw return | vs excess return |
|---|---|---|
| Q&A score | Pearson +0.21 (t +0.82, p = 0.43); Spearman +0.14 (p = 0.60) | Pearson +0.16 (t +0.61, p = 0.55); Spearman +0.00 (p = 0.99) |
| QoQ score delta | Pearson +0.09 (p = 0.80); Spearman +0.18 (p = 0.63) | Pearson −0.11 (p = 0.77); Spearman −0.14 (p = 0.70) |
| Evasion | Pearson −0.29 (t −1.11, p = 0.28); Spearman −0.22 (p = 0.41) | Pearson −0.22 (p = 0.42); Spearman −0.12 (p = 0.67) |
| Analyst tone | Pearson −0.14 (p = 0.61); Spearman −0.02 (p = 0.93) | Pearson −0.12 (p = 0.65); Spearman −0.03 (p = 0.92) |

Nothing is significant. The largest |IC| is evasion vs raw returns
(−0.29, p = 0.28) — directionally sensible (more evasive Q&A, worse
returns) but far from significant, and it is one of 16 ICs computed
on the same small sample.

## Median split (score)

Median score 0.20. Above-median (n = 6): +10.56% raw, +6.89%
excess. Below-median (n = 5): +0.80% raw, −2.09% excess. Spread
**+9.76pp raw / +8.98pp excess**. Five calls sit exactly at the
median (0.20) and are dropped from the split per the design. The
spread looks large — read the next section before believing it.

## The spread is one call

Post-hoc sensitivity check (NOT a registered metric, reported for
honesty): the above-median group contains AMD's 2026-05-05 call,
+52.71% raw (+48.50% excess) over its 20 days — a single-call
outlier that dwarfs everything else in the sample (next best is
+17.25%). Excluding that one call:

- Score vs raw: Pearson IC **+0.01 (p = 0.96)**; vs excess **−0.06
  (p = 0.83)**.
- Median-split spread: **+1.33pp raw** (was +9.76pp).

The entire apparent signal is one AMD call. Without it there is
nothing at all — not even a directional hint.

## Excluded calls (5 of 21)

T+20 falls after the last available trading day (2026-11-20), so no
partial window was used, per the design: ABBV FY2026 Q3 (2026-10-30),
AMT FY2026 Q3 (2026-10-27), AMZN FY2026 Q3 (2026-10-29), AMD FY2026
Q3 (2026-11-03), AMGN FY2026 Q3 (2026-11-03). All five are the most
recent quarter per ticker — the pilot's freshest calls are exactly
the ones that cannot be tested yet. Re-running this test in December,
when their windows complete, adds 5 calls (n = 21) and is the cheap
follow-up. Note the selection effect this creates: the tested 16
exclude every Q3-2026 call.

## Per-call notes

- ABT 2026-04-16 (score −0.35, most negative): −11.07% raw,
  −17.70% excess — the score "worked" here. But ABT 2026-10-21
  (also −0.35): +3.95% raw, +9.50% excess — the same score "failed"
  there, in a window running into the Nov 16–20 selloff (SPY −5.55%).
- AMGN 2026-02-03 (score −0.30, the Tavneos quarter): +12.79% raw,
  +13.43% excess — negative score, strong positive return.
- AMGN 2026-04-08 (score +0.05): −5.35% raw, −13.90% excess.
- Mean 20-day raw return across the 16 tested calls: +6.27%; mean
  excess +3.52%. The window was mostly a rising market, so raw
  returns flatter the scores; the excess ICs are the honest read.

## Spend

$0.00. No LLM calls. Prices from the existing Alpaca paper account
(data reads only). No orders, no live trading.

## Honest caveats

- n = 16 is tiny; a single call moves any IC by ~0.2 — as the AMD
  outlier demonstrates directly.
- 16 ICs on one small sample with no multiple-testing correction;
  the pre-registered bar (|IC| ≥ 0.3, p < 0.10) was read against
  that multiplicity and still nothing cleared it.
- Single-model, single-pass scores; Haiku 5.5 may know these calls'
  outcomes (knowledge-cutoff contamination).
- The 5 excluded calls are the freshest quarter — re-test in
  December for n = 21 before drawing any firmer conclusion.
- This test does not touch the frozen holdout and says nothing about
  v2/v3/H3/C1c.

## Next step

Re-run this exact test in December once the five Q3-2026 windows
complete (n = 21). If the IC is still ~0 there, drop the
earnings-call leg. A wider follow-up (more tickers, more quarters)
is only justified if the December re-test shows something this pilot
did not. Do not trade on these scores.
