# exp-filing-delta-10k-only-v1 — design (registered BEFORE looking at any return for THIS experiment)

Registered 2026-11-27 (research branch; train period only). Written
before any return, correlation, or backtest number was computed for
this experiment. No LLM calls: filing text is already scored; no new
scoring is needed. $0 new spend.

## Origin — why this is a separate registration

The parent experiment `exp-filing-delta-v1` (verdict DROP, Nov 26,
2026) tested a risk-factor language-change tilt on ALL filings
(10-K and 10-Q) and found the tilt HURT the ranking (20d
sector-relative IC change −0.0126, paired NW t −2.44). Its post-hoc
diagnostics (labeled as such in its results file, NOT part of its
frozen test) showed the headline's positive delta IC (+0.0407) is
driven by 10-Q comparisons (3-month gaps), while the YoY 10-K
comparison — the actual Lazy Prices design — points the other way:
delta IC, 10-K filings only (269 known filings, YoY comparison):
mean IC −0.0219, NW t −1.59 (n=611 days). Direction matches the
literature (changers underperform), but the evidence is weak. A
second cut (filings with >= 10 sentences on BOTH sides, 260 known
filings) stayed positive (+0.0351, t +1.78), so the stub pattern
alone does not explain the headline.

Those diagnostics are the MOTIVATION for this experiment, not its
evidence: they were computed after the parent run, on the same train
data, and they are weak (t −1.59, not significant). This experiment
registers its own frozen hypothesis and tests it on the same train
window with a pre-registered rule. If it passes, it is candidacy for
a prospective test, not proof — and a train-only pass on data whose
diagnostic already pointed this way carries a multiple-comparison
discount that must be stated in the results.

## Primary hypothesis

Year-over-year changes in 10-K risk-factor language predict
subsequent 20-trading-day sector-relative returns in the Lazy Prices
direction: issuers whose 10-K risk factors introduce materially new
language UNDERPERFORM over the next 20 sessions, so tilting the
ranking DOWN on high-delta 10-K filings improves the ranking.
Direction (frozen): the delta IC is NEGATIVE. A positive delta IC is
a failure of the hypothesis, not a reason to flip the tilt.

Research basis: Cohen, Malloy & Nguyen, *Lazy Prices* (NBER w25084).
The Lazy Prices design is annual: a firm's 10-K compared with its
own prior-year 10-K. The parent experiment's 10-Q comparisons (3-month
gaps, stub-dominated) are not the Lazy Prices design; this experiment
is.

## Feature definition (frozen)

Reuse `filing_delta.py` from exp-filing-delta-v1, with two
restrictions:

- FORM RESTRICTION: only Form 10-K filings are in scope, compared
  ONLY with the most recent prior Form 10-K for the same issuer
  (YoY). 10-Q filings are excluded entirely from both arms (they
  were the parent experiment's contamination).
- MINIMUM-SENTENCE RULE: a filing is `known` only if BOTH the
  current 10-K risk-factor section AND the prior 10-K risk-factor
  section have >= 10 sentences. Anything else is `unknown`
  (counted by reason: current risk missing / prior risk missing /
  <10 sentences either side / no prior same-form filing).

Feature: the fraction of sentences in the current 10-K's Risk
Factors section with no close match (max token-set Jaccard < 0.50)
in the prior 10-K's Risk Factors section. Deterministic; the
strongest changed passage + closest prior match are saved per
filing, as in the parent. Missing/incomparable → `unknown`.

## Comparison (frozen)

Identical to the parent experiment, restricted to the 10-K-only
feature:

- BASELINE: v2 Haiku ranking (the parent's baseline, unchanged).
- EXPERIMENT: the same ranking tilted — `score_adj = haiku −
  0.25·(2·delta_pct_rank − 1)`, unknown → tilt 0. Same tilt weight
  (0.25) as the parent, so the two experiments are comparable.
- Identical issuers, dates, and simulated entry times; cohort
  machinery H=5, min hold 10d, max positions 60, costs 0/10/20bps.
  Horizon 20d, sector-relative = peer-mean (as in the parent —
  no sector ETFs in cached bars, only SPY).
- Train period ONLY (2024-01-02..2026-08-17). No holdout or
  January-window data. No new LLM scoring ($0).

## Primary outcome (frozen)

PRIMARY (ranking diagnostic): the change in 20d sector-relative IC
(experiment minus baseline), with paired Newey-West t and an
issuer-cluster bootstrap 95% CI — the same primary as the parent.
SECONDARY (economic): net return after costs @0/10/20bps, max
drawdown, Sharpe, turnover, trades.

## Sensitivities (frozen, run regardless of the headline)

1. REMOVE THE LARGEST CONTRIBUTOR: drop the single filing/exit with
   the largest absolute portfolio attribution and recompute the IC
   change and the net return difference.
2. DELAY ENTRY ONE ADDITIONAL SESSION: shift every entry one extra
   session later and recompute the portfolio comparison.

## Uncertainty (frozen)

As in the parent: Newey-West SEs (lag 19) for the ICs; issuer-cluster
bootstrap (500 draws) for the IC change; time-block bootstrap (block
length 20, 500 draws) for the net-return difference. Report
uncertainty accounting for repeated issuers and overlapping return
windows. An inconclusive result is a valid outcome.

## Coverage floor (frozen)

Fewer than 10 known-delta 10-K filings → the experiment cannot
support a judgment: report the counts and the decision is COLLECT
MORE INDEPENDENT DATA (not a DROP — a thin sample is not evidence
against the signal). The parent's diagnostic counted 269 known 10-K
filings; the minimum-sentence rule will reduce that — the exact
known count is reported by the run.

## End decision (tied to the evidence)

Exactly one of:
- DROP the feature — if the IC change is <= 0 (no improvement) or
  of the wrong sign, or the net return difference is <= 0 after
  costs at 10bps with no robustness in the sensitivities.
- COLLECT MORE INDEPENDENT DATA — if the direction is right (IC
  change > 0 in the hypothesized direction — i.e. the tilt improves
  the ranking — and net return difference > 0) but the uncertainty
  is too wide to judge (either bootstrap CI includes 0 and the
  point estimate is economically meaningful), or a sensitivity flips
  the sign, or coverage is too thin (< 10 known filings).
- REGISTER FOR A FUTURE PROSPECTIVE TEST — only if the IC change is
  positive (the tilt improves the ranking) with the bootstrap CI
  excluding 0, AND the net return difference is positive after
  costs at 10bps, AND both sensitivities keep the sign, AND >= 10
  known filings. A prospective test is a NEW registration; nothing
  here changes the live ranking, the daily runner, or any registered
  design.

Note on the decision wording: the parent's primary was the IC
CHANGE (experiment minus baseline). The delta-alone IC (−0.0219 in
the diagnostic) is reported as a secondary diagnostic, not the
decision variable — the decision is whether the TILT improves the
ranking, as in the parent.

## Limits (read before interpreting any pass)

- This tests the TRAIN period only. A train-only pass is candidacy
  for a prospective test, not proof. The frozen holdout and January
  window are untouched by design.
- The motivation (the parent's 10-K-only diagnostic, IC −0.0219,
  t −1.59) was computed on this same train window AFTER the parent
  run. This experiment is therefore a second look at data that
  already pointed this way — a multiple-comparison discount applies
  to any pass, and it must be stated in the results. The frozen
  hypothesis and the minimum-sentence rule were fixed BEFORE this
  run, which is what makes the test meaningful at all.
- The 10-K-only sample is smaller than the parent's (269 known
  filings before the sentence rule, vs 1,035) and annual: each
  issuer contributes at most ~3 events in the train window. Power
  is lower; the coverage floor and the CIs matter more, not less.
- The minimum-sentence rule (≥10 sentences both sides) is a
  design choice, not a tuned threshold: it excludes stub artifacts
  (the parent's mechanism finding) on principle. A different
  threshold is a different registration, not a sensitivity of
  this one.

## Spend

$0.00. Filing text and scores are already on disk. No LLM calls.
