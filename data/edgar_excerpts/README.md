# EDGAR Point-in-Time Dataset — `dataset-edgar-point-in-time-v1`

Registered in `experiments/registry.jsonl` **before** the build ran
(2026-11-20). Feeds `exp-technical-plus-llm-v1` (Phase 2, not yet run).

## Source

SEC EDGAR direct via `edgartools` 5.62.1 (MIT). $0 — no key, no signup.
Identity in the SEC User-Agent: the user's own email (required by SEC
fair-access rules). Rate limit 10 req/sec, handled by edgartools; raw
filing HTML is cached by edgartools under `~/.edgar` (outside the repo).

## Coverage (final, 2026-11-20)

- Universe: the 133 seed symbols in `config.DEFAULT_SEED_SYMBOLS`
  (SPY excluded — an ETF files no 10-K/10-Q).
- **2,108 records, 133/133 tickers** — 10-K 527, 10-Q 1,581; by year
  2023: 532, 2024: 532, 2025: 533, 2026: 511 (partial year, window
  ends Nov 13). Filing dates 2023-01-05..2026-11-12. Train window
  (2024-01-02..2026-08-17): 1,446 records. Frozen holdout
  (2026-08-18..2026-11-13): 130 records.
- Every ticker has 15–16 filings. 15-count tickers are fiscal-year
  offsets (e.g. NVDA Jan FY, AMAT Oct FY, COST Aug FY), not gaps —
  verified XOM (15) against SEC's own submissions listing.
- Forms: 10-K and 10-Q only. **Amendments (10-K/A, 10-Q/A) excluded** —
  a restatement is a separate filing event and must not silently
  overwrite the as-filed record.
- Filing-date window: **2023-01-01 .. 2026-11-13**. The 2023 warm-up
  year matters: the freshest filing at train start (2024-01-02) is a
  Q3-2023 10-Q / FY-2023 10-K filed in late 2023 / early 2024.
- Sections per filing: MD&A (10-K Item 7 / 10-Q Part I Item 2) +
  Risk Factors (Item 1A). A missing section is stored empty and listed
  in `missing_sections` — never filled from another filing.
  Missing MD&A: **0**. Missing Risk Factors: 185 (mostly 10-Qs, where
  Item 1A is legitimately omitted when unchanged; 4 in 10-Ks).
- **MD&A fallback**: edgartools' section splitter misses MD&A in
  some documents. When `obj.get()` returns nothing, the builder falls
  back to a regex extraction over the filing HTML (heading → next
  section heading, with TOC/glossary filters). 30 records recovered
  this way, marked `mda_source: "fallback"`: INTC 16, PNC 10 (its
  10-Q MD&A is headed "Financial Review"), USB 3, MCD 1. Every
  recovered head/tail was eyeballed.
- **Former CIK**: BlackRock filed under CIK 1364742 until late 2024,
  then CIK 2012383 (the current BLK mapping). The builder fetches both
  (`EXTRA_CIKS`); the 7 pre-change filings are stored under BLK.
- Build failures: the manifest lists 2 historical entries (UNP, LIN
  extraction failures during the chunked build); both filings were
  recovered on retry in the gap-fill and are in the dataset.

## Validation

- `build_edgar_dataset.py --validate`: **PASS, 0 errors**,
  manifest counts match the shards. Full test suite 95/95
  (incl. 6 MD&A-fallback tests).
- Haiku sample validation (train window only — holdout unscored):
  5 excerpts scored with claude-haiku-5-5, **$0.018034** total
  (172,870 in / 1,495 out tokens): AAPL +0.25, COF −0.40, GS −0.20,
  MS −0.10, SBUX +0.60, each with a rationale citing real filing
  numbers. Results in `haiku_validation.json`.

## Point-in-time guarantees

- Every record carries `filing_date`. A backtest may only use filings
  with `filing_date <=` the decision date. No future information.
- Holdout **2026-08-18..2026-11-13 is frozen**
  (`experiments/holdout_dates.json`). This dataset supports it; nothing
  here retunes against it.
- `excerpt_sha256` lets Phase 2 cache LLM scores by document hash
  (same keying as `llm_sentiment.py`).

## Schema (one JSON object per line)

| field | meaning |
|---|---|
| ticker, cik | symbol + SEC CIK |
| form | `10-K` or `10-Q` |
| accession | SEC accession number (unique across the dataset) |
| filing_date | ISO date the filing hit EDGAR — the point-in-time key |
| period_end_date | fiscal period end reported by the filing |
| fiscal_year | year of period_end_date |
| mda_text / risk_text | cleaned section text (whitespace collapsed) |
| mda_chars / risk_chars | lengths; caps 100,000 / 60,000 chars |
| excerpt_text | MD&A + Risk Factors combined, capped at 160,000 chars (~40k tokens, the `llm_sentiment.MAX_EXCERPT_CHARS` cap) |
| excerpt_chars / excerpt_sha256 | length + SHA-256 of excerpt_text |
| missing_sections | `[]`, or `["mda"]` / `["risk"]` when a section was absent |
| mda_source | `"parsed"` (edgartools section lookup) or `"fallback"` (regex extraction over the filing HTML — see Coverage) |
| trimmed | true if any cap truncated text |
| source_url | SEC filing index URL |
| edgartools_version, fetched_at | build provenance |

## Files

- `excerpts_YYYY_Qq.jsonl` — one shard per quarter (keeps every
  file well under GitHub's 100 MB limit AND under the Git Data API's
  practical per-blob ceiling of ~40 MB — the largest shard is 32 MB;
  a half-year shard ran ~48 MB and the API rejects it). Records are
  JSONL, sorted by (filing_date, ticker) within a shard.
- `manifest.json` — counts, per-year/per-form/per-ticker coverage,
  train/holdout-window record counts, failures, build parameters.
- `haiku_validation.json` — the 5-excerpt Haiku sample validation
  (train window only) with scores, token counts, and exact cost.
- `README.md` — this file.

## How Phase 2 consumes it

`exp-technical-plus-llm-v1` reads the shards, and for each decision date
takes the latest filing per ticker with `filing_date <=` that date, then
passes `excerpt_text` to `AnthropicProvider.score()` with
`documents=[{"doc": accession, "section": "MD&A+RiskFactors",
"date": filing_date}]`. Scores are cached by document hash, so a
backtest never re-bills for the same filing.

## Rebuilding

`.venv/bin/python build_edgar_dataset.py` is resumable: accessions
already in the shards are skipped. `.venv/bin/python
build_edgar_dataset.py --validate` re-checks every record against the
schema + point-in-time rules and verifies the manifest counts.
