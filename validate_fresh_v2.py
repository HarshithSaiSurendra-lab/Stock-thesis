#!/usr/bin/env python3
"""Validate fresh-validation Step 0 (v2 dataset + Haiku scores).

Checks (same as v1 validation, plus the design's union check):
1. v2 shards + manifest validate (schema, window 2026-11-13..2026-11-24,
   no duplicate accessions, manifest n_records matches shards).
2. Union check: the current-filing series built from v1+v2 filings
   reproduces the v1-only series EXACTLY on every business day before
   2026-11-13 (v2 filings are all dated >= 2026-11-13, so the union
   must not change any pre-Nov-13 day).
3. No score is dated before its filing date (score as_of ==
   filing_date for every scored accession).
4. Every v2 filing has a Haiku score (or is listed as failed/unscored).

Writes data/edgar_excerpts_v2/haiku_validation.json (same shape as
v1's: {"results": [...], "total_cost_usd": ..., "checks": {...}}).

Usage:
    .venv/bin/python validate_fresh_v2.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).parent

import build_edgar_dataset as v1  # noqa: E402
import build_edgar_dataset_v2 as v2  # noqa: E402 — importing patches v1's
# WINDOW_START / WINDOW_END / DATA_DIR globals to the v2 window + dir.
assert v1.DATA_DIR == REPO / "data" / "edgar_excerpts_v2", v1.DATA_DIR
assert v1.WINDOW_START.isoformat() == "2026-11-13"
import run_llm_backtest as p2  # noqa: E402

V2_DIR = REPO / "data" / "edgar_excerpts_v2"
SCORES_PATH = REPO / "experiments" / "results" / "exp-fresh-validation-v1-scores-haiku.json"


def _current_series(filings: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Accession of the latest filing <= day, per symbol (NaN if none)."""
    scoped = filings.sort_values(["ticker", "filing_date"])
    out = pd.DataFrame(index=days, columns=sorted(scoped["ticker"].unique()), dtype=object)
    for symbol, group in scoped.groupby("ticker"):
        dates = group["filing_date"].to_numpy()
        accessions = group["accession"].to_numpy()
        pos = np.searchsorted(dates, days.to_numpy(), side="right") - 1
        col = np.where(pos >= 0, accessions[np.maximum(pos, 0)], np.nan)
        out[symbol] = col
    return out


def main() -> int:
    # 1. v2 shards + manifest.
    ok, report = v1.validate_dataset()
    print(f"[1] v2 dataset validate: ok={ok} "
          f"(records={report['coverage']['n_records']}, "
          f"tickers={report['coverage']['n_tickers']}, "
          f"errors={len(report['errors'])})", flush=True)
    for e in report["errors"][:10]:
        print(f"    ERROR: {e}", flush=True)

    v1_filings = p2.load_edgar_filings()
    v2_frames = []
    for shard in sorted(V2_DIR.glob("excerpts_*.jsonl")):
        recs = [json.loads(line) for line in shard.read_text().splitlines() if line.strip()]
        v2_frames.append(pd.DataFrame.from_records(recs))
    v2_filings = pd.concat(v2_frames, ignore_index=True)
    v2_filings["filing_date"] = pd.to_datetime(v2_filings["filing_date"])
    print(f"    v1 filings: {len(v1_filings)} (max date "
          f"{v1_filings['filing_date'].max().date()}), "
          f"v2 filings: {len(v2_filings)} "
          f"({v2_filings['filing_date'].min().date()}..{v2_filings['filing_date'].max().date()})",
          flush=True)

    # Overlap check: no v2 accession may already exist in v1.
    overlap = set(v1_filings["accession"]) & set(v2_filings["accession"])
    print(f"    accession overlap v1/v2: {len(overlap)}", flush=True)

    # 2. Union check on pre-Nov-13 business days.
    days = pd.bdate_range("2024-01-01", "2026-11-12")
    v1_filings = v1_filings.copy()
    union = pd.concat([v1_filings, v2_filings], ignore_index=True)
    s_v1 = _current_series(v1_filings, days)
    s_union = _current_series(union, days)
    same = (s_v1.fillna("") == s_union.fillna("")).all().all()
    n_diff = int((s_v1.fillna("") != s_union.fillna("")).sum().sum())
    print(f"[2] union == v1 series on {len(days)} pre-Nov-13 business days: {same} "
          f"(diff cells: {n_diff})", flush=True)

    # 3. Score-date check.
    scores = p2._load_scores(SCORES_PATH)
    v2_by_acc = v2_filings.set_index("accession")
    bad_dates = []
    missing_scores = []
    for acc, row in v2_filings.iterrows():
        s = scores.get(row["accession"])
        if s is None or "error" in s:
            missing_scores.append(row["accession"])
            continue
        as_of = s.get("as_of", "")
        if as_of < row["filing_date"].strftime("%Y-%m-%d"):
            bad_dates.append((row["accession"], as_of, row["filing_date"].strftime("%Y-%m-%d")))
    print(f"[3] scores dated before filing date: {len(bad_dates)}; "
          f"v2 filings without a score: {len(missing_scores)}", flush=True)
    for b in bad_dates[:5]:
        print(f"    BAD DATE: {b}", flush=True)

    # 4. Coverage: every v2 filing scored?
    n_scored = sum(1 for v in scores.values() if "error" not in v)
    print(f"[4] scored accessions in file: {len(scores)} ({n_scored} ok, "
          f"{len(scores) - n_scored} errored)", flush=True)

    total_cost = round(sum(float(v.get("cost_usd", 0.0)) for v in scores.values()
                           if "error" not in v and not v.get("cached")), 6)
    results = []
    for acc, s in sorted(scores.items()):
        if "error" in s:
            continue
        f = v2_by_acc.loc[acc]
        results.append({
            "accession": acc,
            "ticker": f["ticker"],
            "form": f["form"],
            "filing_date": f["filing_date"].strftime("%Y-%m-%d"),
            "score": s["score"],
            "confidence": s["confidence"],
            "rationale": s.get("rationale", ""),
            "input_tokens": s.get("tokens_in", 0),
            "output_tokens": s.get("tokens_out", 0),
            "cost_usd": s.get("cost_usd", 0.0),
            "excerpt_chars": int(f["excerpt_chars"]) if "excerpt_chars" in f else len(f["excerpt_text"]),
            "cached": bool(s.get("cached")),
        })
    validation = {
        "results": results,
        "total_cost_usd": total_cost,
        "checks": {
            "dataset_validate_ok": bool(ok),
            "dataset_errors": report["errors"][:20],
            "accession_overlap_v1_v2": sorted(overlap),
            "union_equals_v1_pre_nov13": bool(same),
            "union_diff_cells": n_diff,
            "scores_before_filing_date": bad_dates,
            "filings_without_score": missing_scores,
            "n_v2_filings": len(v2_filings),
            "n_scored": n_scored,
        },
    }
    (V2_DIR / "haiku_validation.json").write_text(json.dumps(validation, indent=2, sort_keys=True))
    print(f"wrote {V2_DIR / 'haiku_validation.json'} "
          f"({len(results)} results, total_cost_usd ${total_cost:.6f})", flush=True)

    all_ok = bool(ok) and not overlap and same and not bad_dates and not missing_scores
    print(f"VALIDATION {'PASSED' if all_ok else 'FAILED'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
