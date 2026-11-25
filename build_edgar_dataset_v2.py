#!/usr/bin/env python3
"""Build dataset-edgar-point-in-time-v2 (fresh-validation Step 0, partial).

Registered design: experiments/exp-fresh-validation-v1-design.md
(Step 0). This is the v1 extension covering filings with filing date
2026-11-13 through 2026-11-24 — the first slice of the fresh window
(2026-11-16..2026-12-31). Same universe (config.DEFAULT_SEED_SYMBOLS,
133 symbols), same EDGAR extraction via edgartools 5.62.1, same trims
(MD&A 100,000 chars, Risk Factors 60,000 chars, excerpt max 160,000
chars), same point-in-time discipline, amendments excluded.

Writes to data/edgar_excerpts_v2/ (own shards + own manifest) so the
v1 dataset and its manifest are never touched. Reuses
build_edgar_dataset.py's fetch/extract/validate functions by patching
its module-level WINDOW_START / WINDOW_END / DATA_DIR globals.

Usage:
    .venv/bin/python build_edgar_dataset_v2.py            # build (resumable)
    .venv/bin/python build_edgar_dataset_v2.py --validate # validate only
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import build_edgar_dataset as v1  # noqa: E402

# Step 0 partial window: filings dated 2026-11-13..2026-11-24.
V2_WINDOW_START = date(2026, 11, 13)
V2_WINDOW_END = date(2026, 11, 24)
V2_DATA_DIR = REPO_ROOT / "data" / "edgar_excerpts_v2"

# Patch the v1 module's globals so its fetch/extract/validate
# functions operate on the v2 window and directory.
v1.WINDOW_START = V2_WINDOW_START
v1.WINDOW_END = V2_WINDOW_END
v1.DATA_DIR = V2_DATA_DIR


def write_v2_manifest(failures: list[dict]) -> dict:
    records = v1.load_all_records()
    summary = v1.coverage_summary(records)
    manifest = {
        "dataset_id": "dataset-edgar-point-in-time-v2",
        "registered_at": "2026-11-22",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source": "SEC EDGAR direct via edgartools",
        "extends": "dataset-edgar-point-in-time-v1 (filing dates 2023-01-05..2026-11-12)",
        "build_params": {
            "edgartools_version": __import__("edgar").__version__,
            "tickers_requested": 133,
            "universe": "config.DEFAULT_SEED_SYMBOLS",
            "trim_chars": {"mda_max": v1.MDA_MAX_CHARS, "risk_max": v1.RISK_MAX_CHARS,
                           "excerpt_max": v1.EXCERPT_MAX_CHARS},
        },
        "window": {"start": V2_WINDOW_START.isoformat(), "end": V2_WINDOW_END.isoformat()},
        "forms": list(v1.FORMS),
        "amendments": "excluded",
        "fresh_window": {"start": "2026-11-16", "end": "2026-12-31",
                         "note": "partial slice through 2026-11-24; completion run in Jan 2027 extends to 2026-12-31"},
        "coverage": summary,
        "failures": failures,
        "n_failures": len(failures),
    }
    V2_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (V2_DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build / validate the EDGAR v2 extension.")
    parser.add_argument("--tickers", default=None, help="Comma-separated tickers (default: full universe)")
    parser.add_argument("--validate", action="store_true", help="Validate existing shards + manifest, no fetching")
    args = parser.parse_args(argv)

    if args.validate:
        ok, report = v1.validate_dataset()
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if ok else 1

    from edgar import set_identity
    import edgar as edgar_pkg

    set_identity(v1.IDENTITY)
    from config import DEFAULT_SEED_SYMBOLS

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else list(DEFAULT_SEED_SYMBOLS)
    )
    existing = v1.load_existing_accessions()
    print(f"universe: {len(tickers)} tickers; already stored: {len(existing)} filings", flush=True)
    all_failures: list[dict] = []
    total_new = 0
    for i, ticker in enumerate(tickers, 1):
        records, failures = v1.fetch_ticker_records(ticker, existing)
        if records:
            v1.append_records(records)
            total_new += len(records)
        all_failures.extend(failures)
        print(f"[{i}/{len(tickers)}] {ticker}: +{len(records)} filings, {len(failures)} failures (total new {total_new})", flush=True)
    manifest = write_v2_manifest(all_failures)
    print(f"done: {total_new} new filings; total records {manifest['coverage']['n_records']}; "
          f"tickers {manifest['coverage']['n_tickers']}; failures {len(all_failures)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
