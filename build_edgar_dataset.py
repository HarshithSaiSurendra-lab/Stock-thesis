"""Build the free point-in-time EDGAR dataset (Phase 1).

Registered as ``dataset-edgar-point-in-time-v1`` in experiments/registry.jsonl
BEFORE the build ran. Research-only: NOT imported by the order path.

Source: SEC EDGAR direct via ``edgartools`` (MIT, pip install edgartools).
$0 new cash, no key, no signup. SEC fair-access limit is 10 req/sec;
edgartools throttles and caches downloads under ~/.edgar.

What it builds
-------------
One JSON record per 10-K / 10-Q filing for the 133-symbol backtest
universe (``config.DEFAULT_SEED_SYMBOLS``; SPY excluded — an ETF files
no 10-K/10-Q), filing_date in 2023-01-01..2026-11-13, amendments
excluded. Each record carries MD&A (10-K Item 7 / 10-Q Part I Item 2)
and Risk Factors (Item 1A) as clean text, plus a combined excerpt
trimmed to 160,000 chars (~40k tokens, the same cap
``llm_sentiment.MAX_EXCERPT_CHARS`` enforces at scoring time).

Point-in-time rules (the whole point of this dataset)
-----------------------------------------------------
- Every record carries its filing_date. A Phase 2 backtest may only use
  filings with filing_date <= the decision date. No future information.
- The holdout window 2026-08-18..2026-11-13 is FROZEN
  (experiments/holdout_dates.json). This dataset supports it; nothing
  here retunes against it.
- Amendments (10-K/A, 10-Q/A) are excluded, not merged: a restatement
  is a separate filing event and must not silently overwrite the
  as-filed record.
- A missing section is recorded as empty + listed in
  ``missing_sections`` — never silently filled from another filing.

Storage
-------
``data/edgar_excerpts/excerpts_YYYY_Qq.jsonl`` — one shard per filing
quarter (keeps every file well under GitHub's 100 MB limit AND under
the Git Data API's practical per-blob ceiling of ~40 MB; a half-year
shard runs ~48 MB and the API rejects it). ``manifest.json``
in the same directory records counts, coverage, failures and build
parameters. Raw filing HTML stays in edgartools' own cache (~/.edgar),
outside the repo and git-ignored.

Usage
-----
    .venv/bin/python build_edgar_dataset.py            # full build (resumable)
    .venv/bin/python build_edgar_dataset.py --tickers AAPL,MSFT
    .venv/bin/python build_edgar_dataset.py --validate # validate shards + manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
# Overridable so parallel shard workers can write to separate dirs
# (EDGAR_DATA_DIR=/tmp/edgar_w0); merged into the repo dir afterwards.
DATA_DIR = Path(os.environ.get(
    "EDGAR_DATA_DIR", REPO_ROOT / "data" / "edgar_excerpts"))

WINDOW_START = date(2023, 1, 1)
WINDOW_END = date(2026, 11, 13)
FORMS = ("10-K", "10-Q")
IDENTITY = "Harshith Sai Surendra Harshithsai.surendra@gmail.com"

MDA_MAX_CHARS = 100_000
RISK_MAX_CHARS = 60_000
EXCERPT_MAX_CHARS = 160_000  # == llm_sentiment.MAX_EXCERPT_CHARS (~40k tokens)

# Section accessors per form (edgartools Filing.obj().get(name)).
SECTIONS = {
    "10-K": {"mda": "Item 7", "risk": "Item 1A"},
    "10-Q": {"mda": "Part I, Item 2", "risk": "Part II, Item 1A"},
}

REQUIRED_FIELDS = (
    "ticker", "cik", "form", "accession", "filing_date",
    "period_end_date", "fiscal_year", "mda_chars", "risk_chars",
    "excerpt_chars", "excerpt_sha256", "mda_text", "risk_text",
    "excerpt_text", "missing_sections", "source_url",
    "edgartools_version", "fetched_at",
)

# Tickers whose pre-2024 filings live under a former CIK (ticker->CIK
# mapping only gives the current one). BlackRock, Inc. filed under
# CIK 1364742 until late 2024, then under CIK 2012383 (current BLK
# mapping); the 7 filings under 1364742 (2023-02..2024-08) are the
# same economic company and belong in the dataset under BLK.
EXTRA_CIKS: dict[str, list[int]] = {"BLK": [1364742]}

# Fallback MD&A extraction (only when edgartools obj.get() returns None).
# Some large filers' documents defeat edgartools' section splitter:
# INTC (all 16 filings), PNC 10-Qs (heading is "Financial Review", not
# "Management's Discussion and Analysis"), USB 2023 10-Qs, MCD 2023 10-K.
# The fallback strips the filing HTML to text and takes the span from the
# MD&A heading to the next section heading. It never fires when the
# parsed section exists, and a record notes mda_source="fallback" so the
# provenance is auditable. Verified by hand on the affected filings
# (see probe in ~/workspace/trading-system/probe_mda_fallback.py).
_MDA_PHRASE = r"MANAGEMENT.?S DISCUSSION AND ANALYSIS"
# Alternate MD&A headings, keyed by ticker (same section, company heading).
ALT_MDA_STARTS: dict[str, list[str]] = {"PNC": [r"FINANCIAL REVIEW"]}
_MDA_END_10K = [
    r"QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK",
    r"ITEM\s*7\s*A\b",
    r"ITEM\s*8\b",
    r"FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA",
    r"CONSOLIDATED STATEMENTS OF (INCOME|EARNINGS|OPERATIONS|COMPREHENSIVE INCOME)",
]
_MDA_END_10Q = [
    r"QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK",
    r"PART\s+II\b",
    r"ITEM\s*1A\b",
    r"OTHER INFORMATION",
    r"CONTROLS AND PROCEDURES",
    r"CONSOLIDATED STATEMENTS OF (INCOME|EARNINGS|OPERATIONS|COMPREHENSIVE INCOME)",
]
_MDA_FALLBACK_MIN_CHARS = 5_000
_MDA_FALLBACK_MAX_CHARS = 400_000

_BLOCK_TAGS = r"p|div|table|tr|td|th|li|ul|ol|h[1-6]|br|hr|section|article|header|footer|thead|tbody|tfoot"


def html_to_text(raw: str) -> str:
    """Filing HTML -> plain text. Block tags become spaces; inline tags
    are removed without a space so words split by <span> boundaries
    don't gain a space mid-word (e.g. 'MANAGEM ENT'S')."""
    text = re.sub(r"</?(?:" + _BLOCK_TAGS + r")[^>]*>", " ", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    import html as _html

    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_toc(snippet: str, after_match: str = "") -> bool:
    """True when a candidate MD&A heading is really a TOC line, a glossary
    entry, or a cross-reference — not the section body. Signals:
    (a) 'Table of Contents' nearby; (b) many bare page numbers;
    (c) another 'Item N' reference right after (TOC lists items);
    (d) a standalone page number in the 120 chars after the heading
    ('Financial Review 1 Executive Summary' is a TOC entry, while the
    real heading 'Financial Review Executive Summary' has none);
    (e) a glossary/acronym list: >=3 separate all-caps runs in the next
    300 chars and no sentence boundary there (a heading phrase such as
    "MANAGEMENT'S VIEW OF THE BUSINESS" is a single run, so it passes)."""
    if "Table of Contents" in snippet:
        return True
    if len(re.findall(r"\b\d{1,3}\b", snippet[:200])) >= 5:
        return True
    if re.search(r"ITEM\s*\d+[A-Z]?\b", snippet[:400], re.I):
        return True
    if re.search(r"\s\d{1,3}\s", after_match[:120]):
        return True
    head = snippet[:300]
    runs = re.findall(r"(?:\b[A-Z]{2,}(?:['\u2019][A-Z]+)?\b(?:\s+|$))+",
                      head)
    if len(runs) >= 3 and not re.search(r"\.\s+[A-Z]", head):
        return True
    return False


def extract_mda_fallback(text: str, form: str, ticker: str = "") -> str | None:
    """Regex MD&A extraction from plain filing text. Returns None when no
    candidate start yields a plausible span (5k..400k chars to the next
    section heading). Only used when the parsed lookup returned nothing."""
    end_pats = _MDA_END_10K if form == "10-K" else _MDA_END_10Q
    starts = [_MDA_PHRASE] + ALT_MDA_STARTS.get(ticker, [])
    for pat in starts:
        for m in re.finditer(pat, text, re.I):
            s = m.start()
            if _looks_like_toc(text[s:s + 500], text[m.end():m.end() + 120]):
                continue
            end = None
            for epat in end_pats:
                em = re.search(epat, text[s:], re.I)
                if em:
                    end = s + em.start()
                    break
            if end is None:
                continue
            span = text[s:end].strip()
            if _MDA_FALLBACK_MIN_CHARS <= len(span) <= _MDA_FALLBACK_MAX_CHARS:
                return span
    return None


def clean_text(text: str) -> str:
    """Collapse HTML-layout whitespace runs to single spaces."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def trim_section(text: str, cap: int) -> tuple[str, bool]:
    """Cap one section; returns (text, was_trimmed)."""
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def build_excerpt(mda_text: str, risk_text: str) -> tuple[str, bool]:
    """Combined excerpt, MD&A first, capped at EXCERPT_MAX_CHARS."""
    combined = (mda_text + "\n\n" + risk_text).strip()
    if len(combined) <= EXCERPT_MAX_CHARS:
        return combined, False
    return combined[:EXCERPT_MAX_CHARS], True


def excerpt_sha256(excerpt_text: str) -> str:
    return hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest()


def build_record(
    ticker: str,
    cik: int,
    form: str,
    accession: str,
    filing_date: date,
    period_end_date: date | None,
    mda_raw: str | None,
    risk_raw: str | None,
    source_url: str,
    edgartools_version: str,
    mda_source: str = "parsed",
) -> dict:
    """Assemble one dataset record from extracted sections."""
    missing: list[str] = []
    mda_text, mda_trimmed = trim_section(clean_text(mda_raw or ""), MDA_MAX_CHARS)
    risk_text, risk_trimmed = trim_section(clean_text(risk_raw or ""), RISK_MAX_CHARS)
    if not mda_text:
        missing.append("mda")
    if not risk_text:
        missing.append("risk")
    excerpt_text, excerpt_trimmed = build_excerpt(mda_text, risk_text)
    fiscal_year = period_end_date.year if period_end_date else filing_date.year
    return {
        "ticker": ticker,
        "cik": cik,
        "form": form,
        "accession": accession,
        "filing_date": filing_date.isoformat(),
        "period_end_date": period_end_date.isoformat() if period_end_date else None,
        "fiscal_year": fiscal_year,
        "mda_chars": len(mda_text),
        "risk_chars": len(risk_text),
        "excerpt_chars": len(excerpt_text),
        "excerpt_sha256": excerpt_sha256(excerpt_text),
        "mda_text": mda_text,
        "risk_text": risk_text,
        "excerpt_text": excerpt_text,
        "missing_sections": missing,
        "trimmed": bool(mda_trimmed or risk_trimmed or excerpt_trimmed),
        "mda_source": mda_source,
        "source_url": source_url,
        "edgartools_version": edgartools_version,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def validate_record(record: dict, errors: list[str]) -> bool:
    """Check one record against the schema + point-in-time rules."""
    ok = True
    for field in REQUIRED_FIELDS:
        if field not in record:
            errors.append(f"{record.get('accession', '?')}: missing field {field}")
            ok = False
    if not ok:
        return False
    accession = record["accession"]
    if record["form"] not in FORMS:
        errors.append(f"{accession}: form {record['form']!r} not in {FORMS}")
        ok = False
    try:
        fdate = date.fromisoformat(record["filing_date"])
    except (TypeError, ValueError):
        errors.append(f"{accession}: bad filing_date {record['filing_date']!r}")
        return False
    if not (WINDOW_START <= fdate <= WINDOW_END):
        errors.append(f"{accession}: filing_date {fdate} outside window")
        ok = False
    if record["period_end_date"]:
        try:
            pend = date.fromisoformat(record["period_end_date"])
            if pend > fdate:
                errors.append(f"{accession}: period_end {pend} after filing_date {fdate}")
                ok = False
        except ValueError:
            errors.append(f"{accession}: bad period_end_date {record['period_end_date']!r}")
            ok = False
    if record["excerpt_chars"] != len(record["excerpt_text"]):
        errors.append(f"{accession}: excerpt_chars mismatch")
        ok = False
    if record["mda_chars"] != len(record["mda_text"]):
        errors.append(f"{accession}: mda_chars mismatch")
        ok = False
    if record["risk_chars"] != len(record["risk_text"]):
        errors.append(f"{accession}: risk_chars mismatch")
        ok = False
    if record["excerpt_chars"] > EXCERPT_MAX_CHARS:
        errors.append(f"{accession}: excerpt over {EXCERPT_MAX_CHARS} chars")
        ok = False
    if record["excerpt_sha256"] != excerpt_sha256(record["excerpt_text"]):
        errors.append(f"{accession}: excerpt_sha256 mismatch")
        ok = False
    if not record["mda_text"] and not record["risk_text"]:
        errors.append(f"{accession}: both sections empty")
        ok = False
    return ok


def shard_path(year: int, quarter: int) -> Path:
    return DATA_DIR / f"excerpts_{year}_Q{quarter}.jsonl"


def _shard_key(filing_date: str) -> tuple[int, int]:
    year = int(filing_date[:4])
    month = int(filing_date[5:7])
    return year, (month - 1) // 3 + 1


def load_existing_accessions() -> set[str]:
    """Accessions already stored, for resumable builds."""
    found: set[str] = set()
    if not DATA_DIR.exists():
        return found
    for path in sorted(DATA_DIR.glob("excerpts_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                found.add(json.loads(line)["accession"])
    return found


def load_all_records() -> list[dict]:
    records: list[dict] = []
    if not DATA_DIR.exists():
        return records
    for path in sorted(DATA_DIR.glob("excerpts_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def coverage_summary(records: list[dict]) -> dict:
    """Coverage stats over a list of records."""
    tickers = sorted({r["ticker"] for r in records})
    per_year: dict[str, int] = {}
    per_form: dict[str, int] = {}
    per_ticker: dict[str, int] = {}
    holdout = train = 0
    for r in records:
        fdate = date.fromisoformat(r["filing_date"])
        per_year[str(fdate.year)] = per_year.get(str(fdate.year), 0) + 1
        per_form[r["form"]] = per_form.get(r["form"], 0) + 1
        per_ticker[r["ticker"]] = per_ticker.get(r["ticker"], 0) + 1
        if date(2026, 8, 18) <= fdate <= date(2026, 11, 13):
            holdout += 1
        elif date(2024, 1, 2) <= fdate <= date(2026, 8, 17):
            train += 1
    dates = [r["filing_date"] for r in records]
    return {
        "n_records": len(records),
        "n_tickers": len(tickers),
        "tickers": tickers,
        "per_year": per_year,
        "per_form": per_form,
        "per_ticker": per_ticker,
        "train_window_records": train,
        "holdout_window_records": holdout,
        "filing_date_min": min(dates) if dates else None,
        "filing_date_max": max(dates) if dates else None,
        "both_sections_empty": sum(
            1 for r in records if not r["mda_text"] and not r["risk_text"]
        ),
        "missing_mda": sum(1 for r in records if "mda" in r["missing_sections"]),
        "missing_risk": sum(1 for r in records if "risk" in r["missing_sections"]),
    }


def validate_dataset() -> tuple[bool, dict]:
    """Validate all shards + manifest. Returns (ok, report)."""
    errors: list[str] = []
    records = load_all_records()
    seen: set[str] = set()
    for r in records:
        if r["accession"] in seen:
            errors.append(f"{r['accession']}: duplicate accession")
        seen.add(r["accession"])
        validate_record(r, errors)
    summary = coverage_summary(records)
    manifest_path = DATA_DIR / "manifest.json"
    manifest_ok = True
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("coverage", {}).get("n_records") != len(records):
            errors.append(
                f"manifest n_records {manifest.get('coverage', {}).get('n_records')} "
                f"!= shard count {len(records)}"
            )
            manifest_ok = False
    else:
        errors.append("manifest.json missing")
        manifest_ok = False
    report = {"errors": errors, "coverage": summary, "manifest_ok": manifest_ok}
    return (not errors, report)


def _parse_period(period: str | None) -> date | None:
    if not period:
        return None
    try:
        return date.fromisoformat(str(period)[:10])
    except ValueError:
        return None


def fetch_ticker_records(ticker: str, existing: set[str]) -> tuple[list[dict], list[dict]]:
    """Fetch + extract all in-window 10-K/10-Q for one ticker.

    Returns (records, failures). Failures are recorded, never raised.
    Tickers in EXTRA_CIKS are also fetched under their former CIK(s).
    """
    from edgar import Company
    import edgar as edgar_pkg

    records: list[dict] = []
    failures: list[dict] = []
    try:
        company = Company(ticker)
        cik = int(company.cik)
    except Exception as exc:  # ticker unresolvable, delisted, renamed
        failures.append({"ticker": ticker, "stage": "resolve_cik", "error": f"{type(exc).__name__}: {exc}"})
        return records, failures
    companies = [(cik, company)]
    for extra_cik in EXTRA_CIKS.get(ticker, []):
        try:
            companies.append((extra_cik, Company(extra_cik)))
        except Exception as exc:
            failures.append({"ticker": ticker, "stage": f"resolve_cik_{extra_cik}",
                             "error": f"{type(exc).__name__}: {exc}"})
    for cik, company in companies:
        recs, fails = _fetch_company_records(ticker, cik, company, existing)
        records.extend(recs)
        failures.extend(fails)
    return records, failures


def _fetch_company_records(
    ticker: str, cik: int, company, existing: set[str]
) -> tuple[list[dict], list[dict]]:
    """Fetch + extract all in-window 10-K/10-Q for one Company object."""
    import edgar as edgar_pkg

    records: list[dict] = []
    failures: list[dict] = []
    for form in FORMS:
        try:
            filings = company.get_filings(
                form=form,
                filing_date=(WINDOW_START.isoformat(), WINDOW_END.isoformat()),
                amendments=False,
            )
        except Exception as exc:
            failures.append({"ticker": ticker, "stage": f"list_{form}", "error": f"{type(exc).__name__}: {exc}"})
            continue
        for filing in filings:
            accession = filing.accession_number
            if accession in existing:
                continue
            try:
                fdate = filing.filing_date
                if isinstance(fdate, str):
                    fdate = date.fromisoformat(fdate[:10])
                if not (WINDOW_START <= fdate <= WINDOW_END):
                    continue
                obj = filing.obj()
                names = SECTIONS[form]
                mda = obj.get(names["mda"])
                risk = obj.get(names["risk"])
                mda_source = "parsed"
                if not (mda or "").strip():
                    # Parsed section lookup failed (edgartools' splitter
                    # misses some documents) — try the regex fallback on
                    # the raw filing HTML. A fallback miss just leaves
                    # the section missing; it never raises.
                    try:
                        recovered = extract_mda_fallback(
                            html_to_text(filing.html()), form, ticker)
                    except Exception:
                        recovered = None
                    if recovered:
                        mda = recovered
                        mda_source = "fallback"
                record = build_record(
                    ticker=ticker,
                    cik=cik,
                    form=form,
                    accession=accession,
                    filing_date=fdate,
                    period_end_date=_parse_period(getattr(filing, "period_of_report", None)),
                    mda_raw=mda,
                    risk_raw=risk,
                    source_url=getattr(filing, "filing_url", "") or getattr(filing, "url", ""),
                    edgartools_version=getattr(edgar_pkg, "__version__", "unknown"),
                    mda_source=mda_source,
                )
                records.append(record)
                existing.add(accession)
            except Exception as exc:
                failures.append({
                    "ticker": ticker, "form": form, "accession": accession,
                    "stage": "extract", "error": f"{type(exc).__name__}: {exc}",
                })
    return records, failures


def append_records(records: list[dict]) -> None:
    """Append records to their per-half-year shards."""
    by_shard: dict[tuple[int, int], list[dict]] = {}
    for r in records:
        by_shard.setdefault(_shard_key(r["filing_date"]), []).append(r)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for (year, quarter) in sorted(by_shard):
        path = shard_path(year, quarter)
        with path.open("a", encoding="utf-8") as fh:
            for r in sorted(by_shard[(year, quarter)], key=lambda x: (x["filing_date"], x["ticker"])):
                fh.write(json.dumps(r, sort_keys=True) + "\n")


def write_manifest(failures: list[dict], build_params: dict) -> dict:
    records = load_all_records()
    summary = coverage_summary(records)
    manifest = {
        "dataset_id": "dataset-edgar-point-in-time-v1",
        "registered_at": "2026-11-20T00:28:08.518307+00:00",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": "SEC EDGAR direct via edgartools",
        "build_params": build_params,
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "forms": list(FORMS),
        "amendments": "excluded",
        "holdout": {"start": "2026-08-18", "end": "2026-11-13", "frozen_in": "experiments/holdout_dates.json"},
        "coverage": summary,
        "failures": failures,
        "n_failures": len(failures),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build / validate the EDGAR point-in-time dataset.")
    parser.add_argument("--tickers", default=None, help="Comma-separated tickers (default: full universe)")
    parser.add_argument("--validate", action="store_true", help="Validate existing shards + manifest, no fetching")
    args = parser.parse_args(argv)

    if args.validate:
        ok, report = validate_dataset()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if ok else 1

    from edgar import set_identity
    import edgar as edgar_pkg

    set_identity(IDENTITY)
    sys.path.insert(0, str(REPO_ROOT))
    from config import DEFAULT_SEED_SYMBOLS

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else list(DEFAULT_SEED_SYMBOLS)
    )
    existing = load_existing_accessions()
    print(f"universe: {len(tickers)} tickers; already stored: {len(existing)} filings", flush=True)
    all_failures: list[dict] = []
    total_new = 0
    for i, ticker in enumerate(tickers, 1):
        records, failures = fetch_ticker_records(ticker, existing)
        if records:
            append_records(records)
            total_new += len(records)
        all_failures.extend(failures)
        print(f"[{i}/{len(tickers)}] {ticker}: +{len(records)} filings, {len(failures)} failures (total new {total_new})", flush=True)
    manifest = write_manifest(all_failures, {
        "edgartools_version": getattr(edgar_pkg, "__version__", "unknown"),
        "tickers_requested": len(tickers),
        "universe": "config.DEFAULT_SEED_SYMBOLS",
        "trim_chars": {"mda_max": MDA_MAX_CHARS, "risk_max": RISK_MAX_CHARS, "excerpt_max": EXCERPT_MAX_CHARS},
    })
    print(f"done: {total_new} new filings; total records {manifest['coverage']['n_records']}; "
          f"tickers {manifest['coverage']['n_tickers']}; failures {len(all_failures)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
