# exp-form4-insider-exit-v1 — design (registered BEFORE looking at any return)

Registered 2026-11-26 (research branch; train period only). Written
before any return, correlation, or backtest number was computed for
this experiment. No LLM calls: Form 4 data is public SEC bulk data;
no scoring is needed. $0 new spend.

## Primary hypothesis

Unusual insider sales identify holdings where an earlier exit would
have improved the outcome after costs. Direction (frozen): an unusual
sale predicts NEGATIVE subsequent 20-trading-day sector-relative
returns for the issuer — the sale signals deterioration the insider
is acting on — so exiting the holding at the next eligible open
after the filing becomes public avoids losses (or forfeits smaller
gains) relative to continuing to hold. A positive association
(unusual sales predict outperformance) is a failure of the
hypothesis, not a reason to flip the rule — the exit rule below is
fixed by this hypothesis, not by the data.

Research basis: Cohen, Malloy & Pomorski, "Decoding Inside
Information" (NBER w16454). Their finding is that insider trades
are informative only when they deviate from the insider's own
established pattern: "routine" trades (the same insider trading in
the same calendar month in each of the prior three years) carry no
signal, while "opportunistic" trades (deviations from the insider's
own pattern) predict returns. This experiment operationalizes
"deviation from the insider's own pattern" as the stake fraction
sold relative to that insider's own prior sales (see Feature), not
as the Cohen-Malloy-Pomorski calendar-month rule — the calendar
rule is a distinct, queued follow-up hypothesis (see §7), not built
here.

## What is new here vs existing work

- The SMA experiment (exp-sma50-timing-v1, NEITHER) tested an
  exit-timing rule on the SAME baseline (the v2 technical engine
  replay). Its structure is reused here: baseline holdings
  reconstructed from the registered v2 panel, an exit overlay that
  fires at the next eligible open, re-entry only via a fresh engine
  selection, losses avoided / gains forfeited per exit, the
  exposure-matched benchmark, and the same two sensitivities. The
  trigger differs: a price-rule cross (SMA) vs a disclosure event
  (an unusual insider sale becoming public).
- The filing-delta experiment (exp-filing-delta-v1, DROP) tested a
  filing-language tilt on the Haiku ranking. Its registration
  discipline is reused here (frozen hypothesis, horizon,
  comparison, decision rule; boundary rule; Newey-West + bootstrap
  uncertainty), but the baseline here is the v2 technical engine
  replay (research_runner.py / backtest.py), not the Haiku cohort
  backtest — because the question is about exiting holdings the
  engine already holds, and the engine replay is the holdings
  reconstruction the SMA experiment already solved and tested.

## Step 0 findings (read before building anything)

- **Baseline holdings ARE reconstructable.** The SMA harness
  (`sma_timing.py`, research branch) replays the registered v2
  panel (`research_runner.build_v2_signal`: composite_score where
  the label is `strong_up` AND trend-quality >= 4.0, else 0, plus
  the SPY regime mask) through the registered engine
  (`backtest.signal_to_weights` -> `_lag_weights` ->
  `_enforce_min_hold`: top-5 equal weight, weekly rebalance every
  5th session, 5-day min hold, 1-bar lag, open fills) to a
  held-weight matrix. This experiment imports that replay
  unchanged and asserts the baseline reproduces the SMA baseline
  numbers exactly (see Verification).
- **Form 4 data is public bulk data.** SEC Insider Transactions
  Data Sets (https://www.sec.gov/data-research/sec-markets-data/
  insider-transactions-data-sets): quarterly ZIPs, Forms 3/4/5,
  Jan 2006 – Sep 2026, updated quarterly. URL pattern
  `https://www.sec.gov/files/structureddata/data/
  insider-transactions-data-sets/{YYYY}q{N}_form345.zip`.
  Tables (tab-delimited, per the SEC readme
  `insider_transactions_readme.pdf`): SUBMISSION (accession,
  filing date, document type 4/4/A, issuer CIK/symbol, AFF10B5ONE
  10b5-1 flag), REPORTINGOWNER (owner CIK/name/relationship),
  NONDERIV_TRANS (transaction rows: security title, trans date,
  trans code, shares, price, acquired/disposed, shares owned
  following, direct/indirect ownership). The bulk files carry a
  FILING_DATE but NO acceptance timestamp.
- **Acceptance timestamps are retrievable per issuer.**
  `https://data.sec.gov/submissions/CIK{cik}.json` lists recent
  filings with `acceptanceDateTime`. Probed 2026-11-26 (AAPL,
  CIK 0000320193): 1,007 recent filings, 600 Form 4/4A, earliest
  2015-10-08 — the recent list reaches well before the train
  window, so one call per issuer covers 2023–2026 (older `files`
  entries are fetched if a recent list does not reach 2023).
- **Direct vs indirect ownership (Step 0 probe, 2024Q1, 10
  tickers):** of 1,975 code-S disposition rows, 1,996 are
  INDIRECT and 330 DIRECT — most insider sales are held through
  trusts/LLCs (e.g. "By CZI Holdings, LLC"). Restricting the
  stake fraction to direct ownership would discard most sales
  including the largest insiders'. The frozen rule therefore
  treats ownership CAPACITY as part of the insider identity (see
  Feature): the stake fraction is computed within one capacity
  (direct, or one named indirect holding), never by mixing
  capacities. A sale split across price bands inside one filing
  is aggregated within its capacity (see Feature).
- **Amendments exist and are resolvable.** 6 of 305 Form 4/4A
  submissions in the 2024Q1 probe are 4/A; the amendment's
  `DATE_OF_ORIG_SUB` is the original filing date. The frozen
  dedup resolves an amendment chain to the latest filing (see
  Feature). 159 of 305 accessions carry >1 transaction row
  (multiple dates/codes per filing), so aggregation must group by
  (accession, owner, date, code, security, capacity), not by
  accession alone.

## Feature definition (frozen)

For each sale observation O (issuer T, reporting owner R, capacity
C, transaction date d, filing F):

- SCOPE: non-derivative common-stock sales. NONDERIV_TRANS rows
  with TRANS_CODE = 'S', TRANS_ACQUIRED_DISP_CD = 'D',
  TRANS_FORM_TYPE = '4', and SECURITY_TITLE (lowercased)
  containing "common stock" or equal to "common". Purchases
  (TRANS_CODE = 'P') are ingested and counted separately but are
  NEVER mixed into the sale signal. Derivative transactions
  (Table II) are out of scope. Form 5 transactions and Form 5
  filings are out of scope (annual catch-ups, not timely).
- OBSERVATION UNIT (one sale = one observation): aggregate rows
  by (issuer CIK, owner CIK, trans_date, trans_code, normalized
  security title, capacity) where capacity = (DIRECT_INDIRECT_
  OWNERSHIP, NATURE_OF_OWNERSHIP). Price-band splits of one sale
  (same accession, same date/code/security/capacity, different
  prices) collapse into one observation: shares_sold = SUM of
  TRANS_SHARES; shares_owned_following = MIN over the rows (the
  final post-sale holding for a sale).
- CLARIFICATION (added before the run, 2026-11-26): an accession
  with MORE THAN ONE reporting owner is excluded from the feature
  (counted as `multiple_reporting_owners`) — the transaction rows
  in a filing do not say which owner they belong to, so attributing
  them to any one owner would be a guess. A single-owner accession
  attributes all its rows to that owner.
- CLARIFICATION (added before the run, 2026-11-26): the prior-sales
  reference set is keyed by the SAME identity as the observation —
  (issuer CIK, owner CIK, capacity, normalized security title) —
  because the stake is per security (an insider's Class A holding
  is a different stake from their Class B holding).
- AMENDMENTS: group observations by (issuer CIK, owner CIK,
  trans_date, trans_code, normalized security, capacity) across
  accessions; keep the rows from the accession with the LATEST
  filing date (the amendment supersedes the original); ties go to
  the larger accession number. The kept accession supplies the
  filing date and the acceptance timestamp. An amendment that
  corrects the share count therefore replaces, never duplicates,
  the original.
- STAKE FRACTION: stake(O) = shares_sold / (shares_sold +
  shares_owned_following). Range [0, 1]; high = the insider sold
  a large fraction of that holding. Computed ONLY where ownership
  and the denominator can be reconciled: TRANS_SHARES present and
  > 0, SHRS_OWND_FOLWNG_TRANS present and >= 0, capacity
  recorded (D or I with a nature). Otherwise stake(O) = `unknown`
  (counted by reason: shares missing / holding missing / capacity
  missing). Unknown is excluded from the feature and counted.
- PRIOR SALES (the "own prior sales" reference set): for the same
  (issuer CIK, owner CIK, capacity), all S observations with
  trans_date in [d - 365 days, d) AND filing_date strictly before
  F's filing_date — i.e. only history that was public by the time
  this filing was made. Their stake fractions are computed the
  same way; priors with unknown stake are excluded from the
  reference set (counted).
- UNUSUAL (frozen, before inspecting returns): stake(O) is
  UNUSUAL iff it is >= the 75th percentile (linear interpolation,
  numpy default) of the insider's own prior 12-month stake
  fractions AND the reference set has >= 3 known stake fractions.
  Fewer than 3 known priors -> `insufficient-history` (unknown,
  counted separately from unreconcilable denominators). The 75th
  percentile is chosen on principle (top quartile of the
  insider's own history), not tuned: no return was inspected
  before freezing it.
- DESCRIPTIVE ONLY (recorded, not part of the frozen feature):
  number of distinct insiders selling per issuer-day, AFF10B5ONE
  plan flag of the triggering filing (1/0), D/I split of trigger
  events, purchase (P) counts. Plan status does NOT filter the
  trigger — a 10b5-1 sale can still be unusual; whether plan
  sales should be excluded is a separately registered follow-up,
  not decided here.

## Timing (frozen)

- FILING BECOMES PUBLIC: the acceptance timestamp from the
  data.sec.gov submissions API when available for the kept
  accession; FALLBACK to the filing-date close when the timestamp
  is missing (reported as timestamp coverage). A filing dated d
  with no timestamp is treated as public at the close of d — the
  same convention as the filing-delta experiment, and conservative
  (never acts on information before it is public).
- FIRST ELIGIBLE ACTION: the first session open strictly after
  the filing became public. A filing accepted after the close on
  Wednesday is first actionable at Thursday's open (assuming
  Thursday is a trading session, per the exchange calendar); a
  filing accepted before the open on a session is first actionable
  at that session's open. Never use the filing session's own close
  as a fill price; fills are at the open price.
- Per observation the runner saves: source accession, issuer,
  owner, transaction date, filing date, acceptance timestamp (or
  fallback flag), earliest eligible decision time, first eligible
  action session, and exclusion reason where applicable.

## The comparison (frozen)

One comparison, on identical issuers, dates, and simulated entry
times, on the stocks the baseline would otherwise CONTINUE
holding:

- BASELINE arm: the v2 engine replay unchanged (held-weight
  matrix from the registered v2 panel; top-5 equal weight,
  weekly rebalance, 5-day min hold, regime mask, 1-bar lag, open
  fills; costs 10bps headline, 20bps shown). Holdings are the
  symbols with held weight > 0.
- EXPERIMENT arm: the same replay PLUS the Form 4 exit. For a
  holding, an UNUSUAL sale event whose first eligible action
  falls strictly after the holding's entry open and on or before
  the baseline's own exit (or the window end) triggers an exit
  of that holding at the trigger's first eligible open, paying
  cost on the exited notional. If the filing was public before
  the holding's entry, it is NOT a trigger for that holding
  (counted as "filing predates holding") — the baseline chose to
  enter with the information already public. The exit OVERRIDES
  the 5-day min-hold (an explicit earlier-exit rule); each
  override inside the min-hold window is counted. If several
  unusual events fire for the same holding, the earliest first
  eligible action governs (one exit per holding per event chain).
- RE-ENTRY (frozen, SMA-identical): after a Form 4 exit, the
  stock can re-enter ONLY via a fresh baseline selection (top-5
  on a later rebalance day with an active v2 signal for that
  symbol). No re-entry on the exit day. The Form 4 rule never
  creates entries. A block is held until such a selection clears
  it; clearings and refusals are counted (same counters as SMA).
- FREED CASH: stays in cash (uninvested) until the next
  rebalance selection. No redistribution to other holdings. The
  variant's gross exposure therefore falls when exits fire, which
  is why the exposure-matched benchmark is reported (see
  Metrics).
- EXECUTION (both arms, identical): the SMA harness's
  position-level simulator (open fills, same for baseline and
  variant); only the exit rule differs. Costs 10bps and 20bps on
  traded notional, same model as `backtest.py`.

## Horizon, universe, costs (reused from the baseline)

- RETURN HORIZON: 20 trading days — the baseline's IC horizon
  (v2 design: Haiku IC +0.060 @20d; filing-delta used the same).
  Sector-relative: the stock's 20d forward return minus the
  equal-weighted mean 20d forward return of same-sector peers in
  the universe (excluding the stock itself), sectors from
  `sector_map.py` (peer-mean definition, as in filing-delta —
  sector ETFs are NOT in the cached bars, only SPY).
- ELIGIBLE UNIVERSE: the SMA experiment's 131-symbol universe
  (133 seed symbols minus DD and HON, excluded by the frozen
  >40% level-shift rule) — the same panels, so the baseline is
  identical to SMA's by construction. Form 4 data is filtered to
  these 131 tickers.
- EVALUATION SET: train period ONLY — decision days 2024-01-01
  through 2026-08-17 (RESEARCH_START..TRAIN_END_DATE). The frozen
  holdout (2026-08-18..2026-11-13) and the January validation
  window (2026-11-16..2026-12-31) are NOT used as the evaluation
  set. BOUNDARY RULE (frozen): a trigger event is included only
  if its first eligible action is on or before 2026-08-17 AND its
  full 20-session forward window completes on or before
  2026-08-17. Events after that are excluded from the IC and the
  portfolio comparison (counted). The portfolio backtest slices
  prices and both arms' holdings to train days only.
- COSTS: 10bps headline, 20bps shown, on traded notional.

## Primary outcome (frozen)

PRIMARY (ranking diagnostic): the mean daily cross-sectional
Spearman IC of the trigger indicator vs 20-day sector-relative
forward returns. For each trigger day (a session on which >= 1
held symbol has an unusual sale first actionable that session),
rank the trigger indicator (1 = triggered held symbol, 0 = other
held symbols) against the 20d sector-relative forward returns
across the held symbols that day (require >= 4 held symbols with
both values); average across trigger days. Hypothesized direction
is NEGATIVE: triggered symbols rank LOW in forward returns
(they underperform), which is what makes exiting them valuable.
The comparison is against the no-signal null (IC = 0): there is
no "score" whose IC changes here, so the trigger IC itself is
the rank-correlation evidence, reported with its Newey-West SE
and issuer-cluster bootstrap CI (see Uncertainty). The trigger
is sparse and binary, so this IC has low power by construction;
the economic comparison below carries equal decision weight.

SECONDARY (economic, the decision variable): portfolio net
return after costs (10bps primary, 20bps shown), turnover, max
drawdown, coverage — baseline vs experiment on train days, plus
per-exit losses avoided / gains forfeited (see Metrics).

## Metrics and benchmarks (frozen)

Per version, at 10bps (headline) and 20bps: net portfolio return
after costs, max drawdown, avg daily turnover (|weight change|),
time invested (fraction of sessions with gross exposure > 0),
average gross exposure, distinct issuers traded, number of
trades (entries), number of timing exits, min-hold overrides.

- EXIT COUNTS: losses avoided (timing exit price above the price
  the baseline eventually exited the same holding at — or the
  window-end mark if the baseline never exited), gains forfeited
  (timing exit below the baseline's eventual exit price),
  whipsaws (SMA definition: any close in the 5 sessions after
  the trigger session at or above its SMA50 — reported for
  comparability, NOT a decision input here; the trigger is an
  event, not a price cross). Added trading costs: the experiment
  arm's extra turnover cost vs baseline (exit cost + re-entry
  cost), in $ and bps. Net portfolio difference: experiment minus
  baseline total return, in pp and $ on the $1,000 book.
- EXPOSURE-MATCHED BENCHMARK (frozen construction, SMA-identical):
  daily return = the variant's gross exposure on day t x SPY's
  return on day t; no costs; same window. If the variant's
  drawdown is smaller than the exposure-matched benchmark's,
  timing added value beyond holding cash; if similar, the smaller
  drawdown came mainly from holding more cash. Also report SPY
  buy-and-hold (+63.48% registered) and the baseline's own
  exposure-matched benchmark.
- COVERAGE: input dates; n Form 4/4A submissions in scope; n S
  observations (after aggregation/dedup); n with known stake
  fraction; n unknown by reason (shares missing / holding
  missing / capacity missing / insufficient history); n unusual;
  n trigger events (fired on a holding) and n events excluded by
  the boundary rule / filing-predates-holding / never-held;
  distinct issuers and event dates in the trigger set; distinct
  insiders selling; plan-status split of triggers; timestamp
  coverage (fraction of trigger filings with a real acceptance
  timestamp vs fallback).
- RE-ENTRY ACCOUNTING: blocks cleared on a positive-signal
  selection; all-zero selections refused; variant-only
  re-entries, split by clearing kind.

## Sensitivities (frozen, run regardless of the headline)

1. REMOVE THE LARGEST CONTRIBUTOR: drop the single trigger exit
   with the largest absolute P&L contribution (losses avoided /
   gains forfeited in $) and recompute the net portfolio
   difference and the trigger IC. (Attribution per exit is the
   timing-exit effect in $; the largest contributor is the exit
   whose removal moves the portfolio difference most.)
2. DELAY EXIT ONE ADDITIONAL SESSION: shift every trigger exit
   one extra session later (act at the open one session after the
   first eligible action) and recompute the portfolio comparison.
   (The IC is anchored at the trigger's first eligible session;
   it is reported unchanged, and this is documented, not hidden.)

## Uncertainty (frozen)

Repeated issuers and overlapping 20d windows make naive SEs too
small. Report:
- Trigger IC: mean daily Spearman IC with a Newey-West SE (lag
  19, matching the 20-session overlap) and t-stat; PLUS an
  issuer-cluster bootstrap (resample tickers with replacement,
  500 draws, recompute the mean trigger IC on the resampled
  (day, symbol) event set) with a 95% CI.
- Portfolio: a time-block bootstrap of the daily return-difference
  series (experiment minus baseline), block length 20 sessions,
  500 draws, 95% CI for the net-return difference at 10bps.
- Per-exit outcomes: a trade-level bootstrap over trigger-exit
  effects (1,000 resamples with replacement), 5th/95th
  percentile interval, WITH the caveat that trades overlap in
  calendar time, so the interval is indicative and overstates
  independence — it is not a p-value.

## Coverage floor (frozen)

Fewer than 10 trigger events -> the experiment cannot support a
judgment: report the counts and the event-level outcomes, and the
decision is COLLECT MORE INDEPENDENT DATA (not a DROP — a thin
sample is not evidence against the signal).

## End decision (tied to the evidence)

Exactly one of:
- DROP the feature — if the net portfolio difference is <= 0
  after costs at 10bps with no robustness in the sensitivities,
  OR the trigger IC is ~0 or of the wrong sign (positive —
  triggers predict outperformance, so exiting hurt).
- COLLECT MORE INDEPENDENT DATA — if the direction is right
  (portfolio difference > 0 and trigger IC negative) but the
  uncertainty is too wide to judge (either bootstrap CI includes
  0 and the point estimate is economically meaningful), or a
  sensitivity flips the sign, or coverage is too thin (< 10
  trigger events).
- REGISTER FOR A FUTURE PROSPECTIVE TEST — only if the net
  portfolio difference is positive after costs at 10bps AND both
  sensitivities keep the sign AND the trigger IC is in the
  hypothesized direction (negative point estimate) AND >= 10
  trigger events. A prospective test is a NEW registration;
  nothing here changes the live ranking, the daily runner, or
  any registered design.

## Limits (read before interpreting any pass)

- This tests the TRAIN period only (2024-01-02..2026-08-17). A
  train-only pass is candidacy for a prospective test, not proof.
  The frozen holdout and January window are untouched by this
  experiment by design.
- The trigger IC is sparse and binary by construction (a handful
  of trigger days, ~5 held symbols each); its power is low and
  it is reported alongside, not instead of, the economic
  comparison. A null IC with a positive portfolio difference is
  a COLLECT-MORE-DATA outcome, not a pass.
- Form 4 sales include routine RSU-vesting and 10b5-1 plan sales.
  The own-history percentile is the filter that is supposed to
  separate them from opportunistic sales; whether it does so is
  exactly what this test measures. Plan status is descriptive
  only — if plan sales dominate the triggers, that is reported
  and is a reason to register a plan-excluded follow-up.
- The v2 technical engine's train return is negative
  (registered -14.01% @10bps close-to-close; SMA open-fill
  baseline -7.82%). The exit test is about timing — whether
  exiting early improves the outcome relative to continuing to
  hold — not about the baseline's level. A positive portfolio
  difference on a losing baseline is still a timing effect, and
  the exposure-matched benchmark is what separates timing value
  from merely holding less stock.
- The bulk dataset is as-filed data; the SEC disclaimer applies
  (filers' errors and extraction errors are possible). The
  amendment resolution and the unknown/uncounted categories are
  the mitigation, not a cure.
- Acceptance timestamps come from a second source (the
  submissions API); where they are missing the fallback is the
  filing-date close, which is conservative (never acts before
  the filing is public) and can only delay an exit by at most
  one session.

## Queued hypotheses (document only — NOT built here)

- **Cohen-Malloy-Pomorski routine/opportunistic calendar rule:**
  a trade is routine if the same insider traded in the same
  calendar month in each of the prior three years, opportunistic
  otherwise (NBER w16454). A distinct classification from the
  stake-fraction percentile used here; a separately registered
  experiment could test it as the trigger (or compare the two
  classifications head-to-head on the same holdings).
- **10b5-1-excluded trigger:** same stake-fraction rule, but
  filings flagged AFF10B5ONE=1 do not trigger. Tests whether
  plan sales dilute the signal.
- **Purchase (P) signal:** unusual insider PURCHASES as an entry
  tilt (the mirror image of this exit test). Distinct hypothesis;
  the P rows are ingested and counted here but not used.
- **Stake-fraction threshold variants:** the 75th percentile is
  frozen here; a 90th-percentile or top-decile-of-own-history
  variant is a different registration, not a sensitivity of
  this one.

## Spend

$0.00. Public SEC bulk data + public submissions API. No LLM
calls anywhere in this experiment. No API keys needed (SEC
requests carry a research User-Agent and are rate-limited).
