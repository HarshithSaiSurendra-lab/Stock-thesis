# Equibles Earnings-Call Coverage Pilot

Generated 2026-11-22. Source: Equibles REST API v1 (free tier, 100 requests/day shared across REST and MCP).
Machine-readable matrix: `equibles-coverage-pilot.json`. Raw API responses: `equibles-pilot-raw/`.

## Pilot set

Top 10 universe tickers by EDGAR record count (manifest `data/edgar_excerpts/manifest.json`).
113 tickers tie at 16 records and 20 at 15, so the ranking is a near-total tie; ties were
broken alphabetically by ticker. Pilot set (all 16 records): AAPL, ABBV, ABT, ADBE, AEP,
AMD, AMGN, AMT, AMZN, AXP.

## Method

- 1 `events` call per ticker (limit 8), then the 4 most recent earnings-call quarters per
  ticker from that page. Fiscal year/quarter are Equibles' own labels, taken as given;
  no inference from call dates was needed (every returned event carried fiscalYear and
  fiscalQuarter).
- 1 `call` call per (ticker, quarter) cell. A 404 would have meant no call for that
  quarter; none occurred (all 35 returned 200, no retries needed).
- Requests used: 45 of the 50 budget (10 events + 35 calls). One further Equibles request
  was made earlier today by the parent agent (NVDA FY2027 Q2, 200, transcript available),
  so the daily total is 46 of 100.

## Coverage totals

- Cells checked: 35 (target was 40; see gaps)
- With transcript: 31 of 35 (89%)
- With linked 8-K earnings release: 34 of 35 (97%)
- With transcript AND release (PEAD-testable): 31 of 35 (89%)
- Guidance status: approved 25, notExtracted 9, null 1 (future call)

## Per-ticker coverage

| Ticker | Cells | Transcripts | Releases | Transcript + release |
|---|---|---|---|---|
| AAPL | 4 | 2 | 4 | 2 |
| ABBV | 3 | 3 | 3 | 3 |
| ABT | 4 | 4 | 4 | 4 |
| ADBE | 4 | 3 | 3 | 3 |
| AEP | 4 | 3 | 4 | 3 |
| AMD | 3 | 3 | 3 | 3 |
| AMGN | 4 | 4 | 4 | 4 |
| AMT | 3 | 3 | 3 | 3 |
| AMZN | 4 | 4 | 4 | 4 |
| AXP | 2 | 2 | 2 | 2 |

## Cell detail (transcript / release)

- AAPL: FY26 Q4 2026-11-02 no / yes; Q3 2026-07-30 yes / yes; Q2 2026-04-30 no / yes;
  Q1 2026-01-29 yes / yes.
- ABBV: FY26 Q3 2026-10-30 yes / yes; Q2 2026-07-31 yes / yes; Q1 2026-04-29 yes / yes.
- ABT: FY26 Q3 2026-10-21 yes / yes; Q2 2026-07-16 yes / yes; Q1 2026-04-16 yes / yes;
  FY25 Q4 2026-01-22 yes / yes.
- ADBE: FY26 Q4 2026-12-09 no / no (scheduled, future); Q3 2026-09-10 yes / yes;
  Q2 2026-06-11 yes / yes; Q1 2026-03-12 yes / yes.
- AEP: FY26 Q3 2026-10-28 no / yes; Q2 2026-07-30 yes / yes; Q1 2026-05-05 yes / yes;
  FY25 Q4 2026-02-12 yes / yes.
- AMD: FY26 Q3 2026-11-03 yes / yes; Q2 2026-08-04 yes / yes; Q1 2026-05-05 yes / yes.
- AMGN: FY26 Q3 2026-11-03 yes / yes; Q2 2026-04-08 yes / yes; FY25 Q4 2026-02-03
  yes / yes; FY25 Q3 2025-11-04 yes / yes.
- AMT: FY26 Q3 2026-10-27 yes / yes; Q2 2026-07-28 yes / yes; Q1 2026-04-28 yes / yes.
- AMZN: FY26 Q3 2026-10-29 yes / yes; Q2 2026-07-30 yes / yes; Q1 2026-04-29 yes / yes;
  FY25 Q4 2026-02-05 yes / yes.
- AXP: FY26 Q3 2026-10-23 yes / yes; Q2 2026-07-24 yes / yes.

## Gaps

1. **Short pages.** ABBV, AMD, and AMT returned only 3 earnings calls in the first
   events page (limit 8, other event types fill the page); AXP returned 2. Their 4th
   most recent quarters sit on the next events page. Cost to complete: 4 more `events`
   requests (offset 8), plus 5 more `call` requests for the added cells.
2. **No transcript source.** AAPL FY26 Q4 (2026-11-02) and FY26 Q2 (2026-04-30), and
   AEP FY26 Q3 (2026-10-28), are concluded calls whose transcript material status is
   `notAttempted`, reason "No transcript source available". Their 8-K releases ARE
   linked, so the release text exists; only the call transcript is missing. AAPL is
   the weakest pilot name on transcript coverage (2 of 4).
3. **Future call.** ADBE FY26 Q4 is scheduled for 2026-12-09 and has not happened yet:
   no transcript, no release, guidance status null. It becomes usable after Dec 9.
4. **AMGN fiscal labelling.** No FY2026 Q1 earnings call appears in AMGN's events
   data at all; the Apr 8, 2026 call is labelled FY2026 Q2. Either the Q1 call was
   not captured or Equibles' fiscal mapping for AMGN differs from the calendar
   reading. Flagged, not assumed.
5. **Guidance not extracted.** 9 of 35 cells have guidanceStatus `notExtracted`
   (all 4 AAPL quarters, ABBV Q1, ABT Q2 and Q1, ADBE Q1, AEP FY25 Q4). Guidance
   extraction is separate from the transcript and release; it affects guidance-based
   tests, not the transcript PEAD blend (which needs transcript + 8-K release).

## Recommended pilot subset

**Full 4-quarter transcript coverage: ABT, AMGN, AMZN** (4 of 4 cells with transcript
and linked release each).

Next tier, every returned cell transcript+release but only 3 or 2 quarters checked:
ABBV, AMD, AMT (3 of 3), AXP (2 of 2). Completing their 4th quarters costs 9 more
requests (4 events pages + 5 calls) and would likely promote some of them.

**Suggested pilot for scoring: ABT, AMGN, AMZN plus ABBV, AMD, AMT (19 transcripted
calls with releases)**, optionally AAPL excluded until its transcript gaps are
understood, and ADBE's Q4 excluded until after Dec 9.

## Next step (not done here)

Fetch full transcripts (speakers endpoint, up to 200 turns per page) for the pilot
subset and score Q&A text only with Haiku, per the registered pilot design. No
transcripts were fetched and no LLM credits were spent in this step.
