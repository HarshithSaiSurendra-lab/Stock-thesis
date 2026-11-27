# exp-form4-insider-exit-v1 — results

Run at 2026-11-27T02:23:33.194600+00:00 (59.9s). Train only: 2024-01-02..2026-08-17. Horizon 20 trading days, sector-relative. Costs 10bps headline / 20bps shown.

## Decision: DROP

trigger IC is ~0 (mean -0.0423, Newey-West t -0.57) or of the wrong sign - no detectable ranking signal

## Baseline vs experiment

| metric | baseline @10bps | variant @10bps | baseline @20bps | variant @20bps |
|---|---:|---:|---:|---:|
| net return | -7.82% | -4.57% | -20.50% | -17.58% |
| max drawdown | -22.26% | -16.67% | -29.65% | -24.41% |
| avg daily turnover | 0.2333 | 0.2299 | 0.2332 | 0.2298 |
| avg gross exposure | 0.9955 | 0.8992 | 0.9955 | 0.8994 |
| time invested | 0.9985 | 0.9985 | 0.9985 | 0.9985 |
| entries | 370 | 366 | 370 | 366 |
| Form 4 exits | — | 29 | — | 29 |
| min-hold overrides | — | 21 | — | 21 |
| whipsaws | — | 26 | — | 26 |

Net portfolio difference: **+3.25pp** @10bps (+2.92pp @20bps).
Exposure-matched benchmark: variant +53.68% vs baseline +65.09% (SPY buy-and-hold registered +63.48%).

## What the gain is made of

The direct exit-price effect is $10.54 on the $1,000 book (1.05pp) — 14 losses avoided ($84.1) against 11 gains forfeited ($-73.56). The rest of the +3.25pp portfolio difference is exposure, not exit timing: the variant holds less (avg gross exposure 0.8992 vs 0.9955) of a negative-return book, and it underperforms its own exposure-matched benchmark by 58pp (variant -4.57% vs benchmark +53.68%; the baseline underperforms its own benchmark by 73pp) — the same pattern as the SMA experiment. The drawdown improvement is likewise explained by lower exposure (benchmark drawdowns -15.76% vs -18.98%).
26 of the 29 Form 4 exits are whipsaws by the SMA definition (close back above its SMA50 within 5 sessions). That definition was built for SMA-cross exits, not for Form 4 exits, so it is reported for comparability only and is not a decision input.

## Trigger IC (primary ranking diagnostic)

Mean daily Spearman IC: **-0.0423** over 35 trigger days (Newey-West t -0.57). Issuer-cluster bootstrap 95% CI [-0.3502, 0.298] (0.626 of draws negative). Hypothesized direction: negative.

## Exit economics (@10bps, $1,000 book)

Losses avoided: $84.1 (14 exits); gains forfeited: $-73.56 (11 exits); net price effect: $10.54. Added trading cost vs baseline (estimated from the turnover difference): $-2.24. Trade-level bootstrap 5th/95th: $-61.63 / $74.19 (n=28). Portfolio block bootstrap 95% CI (net diff): [-10.16, 20.84]pp (0.704 of draws positive).

## Sensitivities

- Ex-largest contributor (ABBV, $-24.12): net diff +4.99pp, trigger IC -0.0643.
- Exit delayed one additional session: net diff +5.53pp (26 exits).

## Coverage

- Bulk quarters: 2023q1, 2023q2, 2023q3, 2023q4, 2024q1, 2024q2, 2024q3, 2024q4, 2025q1, 2025q2, 2025q3, 2025q4, 2026q1, 2026q2, 2026q3; filing range 2023-01-03..2026-09-30.
- 2026q3 bulk file covers filings through 2026-09-30, past the train end; the window is fully covered. Filings after 2026-08-17 cannot become triggers.
- Submissions in scope (Form 4/4A, universe): 39543; sale observations: 11044 (11044 with a known stake; unknown by reason {}).
- Unusual sales: 2890 (insufficient history 4781, not unusual 3373).
- Acceptance timestamps: 9970 observations with a timestamp, 1074 on the filing-date fallback.
- Trigger events fired on a holding: 54 (18 issuers, 44 event dates, 28 insiders; plan 10b5-1: 41, not plan: 10, unknown: 3; direct/indirect: {'D': 47, 'I': 7}).
- Excluded: never held 835, filing predates holding 1915, action after window 0, no first action 86.

## Largest exits (by |effect|)

- ABBV entered 2024-07-11 @ 167.93, Form 4 exit 2024-07-22 @ 173.245 vs baseline exit 2024-09-06 @ 193.58: $-24.12 (whipsaw)
- AMAT entered 2025-12-02 @ 255.625, Form 4 exit 2025-12-04 @ 268.335 vs baseline exit 2026-01-08 @ 290.775: $-18.91 (whipsaw)
- EQIX entered 2024-02-08 @ 843.04, Form 4 exit 2024-02-14 @ 833.23 vs baseline exit 2024-02-23 @ 884.745: $-12.53 (whipsaw)
- META entered 2024-12-16 @ 628.85, Form 4 exit 2024-12-17 @ 626.17 vs baseline exit 2024-12-23 @ 589.6: $+12.45 (whipsaw)
- AMAT entered 2026-02-23 @ 373.4, Form 4 exit 2026-02-25 @ 386.035 vs baseline exit 2026-03-02 @ 366.15: $+11.93 (whipsaw)
- CVX entered 2026-05-19 @ 194.93, Form 4 exit 2026-05-26 @ 190.0 vs baseline exit 2026-05-27 @ 181.76: $+8.44
- GM entered 2026-05-27 @ 81.425, Form 4 exit 2026-05-28 @ 83.78 vs baseline exit 2026-06-03 @ 80.5: $+7.57 (whipsaw)
- CRM entered 2024-11-22 @ 338.755, Form 4 exit 2024-11-25 @ 344.5 vs baseline exit 2024-12-02 @ 332.555: $+7.40 (whipsaw)
- AVGO entered 2025-08-08 @ 303.97, Form 4 exit 2025-08-18 @ 302.38 vs baseline exit 2025-08-22 @ 291.38: $+7.40 (whipsaw)
- ADBE entered 2024-04-22 @ 470.215, Form 4 exit 2024-05-06 @ 488.15 vs baseline exit 2024-05-28 @ 471.74: $+6.88
