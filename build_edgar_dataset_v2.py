#!/usr/bin/env python3
"""Build dataset-edgar-point-in-time-v2 (fresh-validation extension).

Registered design: experiments/exp-fresh-validation-v1-design.md
(Step 0). This is the v1 extension covering filings with filing date
2026-11-13 through 2026-12-31 — the fresh window (2026-11-16..
2026-12-31). Same universe (config.DEFAULT_SEED_SYMBOLS,
133 symbols), same EDGAR extraction via edgartools 5.62.1, same trims
(MD&A 100,000 chars, Risk Factors 60,000 chars, excerpt max 160,000
chars), same point-in-time discipline, amendments excluded.

Writes to data/edgar_excerpts_v2/ (own shards + own manifest) so the
v1 dataset and its manifest are never touched. Reuses
build_edgar_dataset.py's fetch/extract/validate functions, passing the
v2 window and data dir explicitly — v1's globals are the v1 dataset's
constants and are never mutated (importing this module changes no v1
behaviour; the EDGAR suite fails in either import order if it does).

Daily refresh: scripts/run_daily.py calls this with --workers 4, so
the extension grows incrementally through Dec 31 (resumable — it
skips accessions already stored). The January completion run is then
just a final refresh + validate, not a fresh build.

Parallelism (v1 pattern): each worker is a fresh subprocess that
fetches one chunk of tickers and writes to its own temp dir; the
parent merges the worker shards (dedup by accession) and writes the
manifest once. Workers are chunked small because edgartools degrades
after ~15 tickers / ~250 requests in a long-lived process
(PROGRESS.md) — a fresh process per chunk stays well under that.

Usage:
    .venv/bin/python build_edgar_dataset_v2.py                  # build (resumable, serial)
    .venv/bin/python build_edgar_dataset_v2.py --workers 4     # build (parallel, 4 workers)
    .venv/bin/python build_edgar_dataset_v2.py --tickers AAPL,MSFT
    .venv/bin/python build_edgar_dataset_v2.py --validate       # validate only
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

import build_edgar_dataset as v1  # noqa: E402

# Fresh-validation window: filings dated 2026-11-13..2026-12-31.
# (The design window starts 2026-11-16; the builder keeps the Nov 13
# start from Step 0 — there were no filings Nov 13-16, so it is the
# same set. The end is FIXED at 2026-12-31 by the design.)
V2_WINDOW_START = date(2026, 11, 13)
V2_WINDOW_END = date(2026, 12, 31)
V2_DATA_DIR = REPO_ROOT / "data" / "edgar_excerpts_v2"

# The v2 window, passed explicitly to every v1 call below. v1's
# WINDOW_START / WINDOW_END / DATA_DIR are the v1 dataset's constants
# and are never mutated here — importing this module must not change
# any v1 behaviour.
V2_WINDOW: tuple[date, date] = (V2_WINDOW_START, V2_WINDOW_END)

# Parallelism: keep each worker process well under the edgartools
# degradation limit (~15 tickers / ~250 requests per long-lived
# process). 12 tickers per chunk, 4 concurrent workers (the v1 build
# used 4 parallel workers).
DEFAULT_CHUNK_SIZE = 12
DEFAULT_WORKERS = 4


def write_v2_manifest(failures: list[dict]) -> dict:
    records = v1.load_all_records(data_dir=V2_DATA_DIR)
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
                         "note": "refreshed daily by scripts/run_daily.py (parallel, resumable); "
                                 "coverage grows through 2026-12-31; January completion run is a "
                                 "final refresh + validate, not a fresh build"},
        "coverage": summary,
        "failures": failures,
        "n_failures": len(failures),
    }
    V2_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (V2_DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def _fetch_chunk(
    tickers: list[str], existing: set[str], data_dir: Path = V2_DATA_DIR
) -> tuple[int, list[dict]]:
    """Fetch one chunk of tickers serially; append as we go.

    Returns (n_new, failures). `existing` is updated in place so a
    retry inside the same process never re-fetches a stored filing.
    Writes go to `data_dir` (the main v2 dir, or a worker's own dir).
    """
    all_failures: list[dict] = []
    total_new = 0
    for i, ticker in enumerate(tickers, 1):
        records, failures = v1.fetch_ticker_records(ticker, existing, window=V2_WINDOW)
        if records:
            v1.append_records(records, data_dir=data_dir)
            total_new += len(records)
        all_failures.extend(failures)
        print(f"[{i}/{len(tickers)}] {ticker}: +{len(records)} filings, {len(failures)} failures (total new {total_new})", flush=True)
    return total_new, all_failures


def _run_worker(tickers: list[str], out_dir: Path) -> tuple[int, list[dict]]:
    """Worker entry: fetch `tickers` into `out_dir` (own shards).

    Existing accessions are read from the MAIN v2 dir first (so a
    worker never re-fetches stored filings); writes go to `out_dir`
    via the explicit data_dir parameter. Writes a worker_result.json
    next to the shards for the parent to aggregate. No manifest is
    written here — the parent writes it once, after merging.
    """
    existing = v1.load_existing_accessions(data_dir=V2_DATA_DIR)
    print(f"worker: universe chunk {len(tickers)} tickers; already stored: {len(existing)} filings", flush=True)
    total_new, failures = _fetch_chunk(tickers, existing, data_dir=out_dir)
    result = {"tickers": tickers, "n_new": total_new, "failures": failures}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "worker_result.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    return total_new, failures


def _merge_worker_dirs(
    worker_dirs: list[Path], data_dir: Path = V2_DATA_DIR
) -> tuple[int, list[dict]]:
    """Merge worker shards into the main v2 dir (dedup by accession).

    Chunks partition tickers disjointly, so overlap is not expected;
    dedup is a safety net. Returns (n_new, aggregated failures).
    `data_dir` is where the merged records are appended (the main v2
    dir in production; a temp dir in tests).
    """
    seen: set[str] = set()
    records: list[dict] = []
    all_failures: list[dict] = []
    for wdir in worker_dirs:
        result_path = wdir / "worker_result.json"
        if result_path.exists():
            all_failures.extend(json.loads(result_path.read_text())["failures"])
        for path in sorted(wdir.glob("excerpts_*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec["accession"] in seen:
                    continue
                seen.add(rec["accession"])
                records.append(rec)
    if records:
        v1.append_records(records, data_dir=data_dir)
    return len(records), all_failures


def _run_parallel(tickers: list[str], workers: int, chunk_size: int) -> tuple[int, list[dict]]:
    """Fetch `tickers` with `workers` concurrent fresh processes.

    Any worker failure aborts the run WITHOUT merging, so the dataset
    stays in its last good state (the next run retries the refresh).
    """
    chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]
    print(f"parallel: {len(tickers)} tickers in {len(chunks)} chunks of <={chunk_size}, "
          f"{workers} workers", flush=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="edgar_v2_"))
    worker_dirs = [tmp_root / f"w{i}" for i in range(len(chunks))]
    try:
        def _spawn(args: tuple[int, list[str]]) -> tuple[int, int]:
            idx, chunk = args
            cmd = [sys.executable, str(Path(__file__).resolve()),
                   "--tickers", ",".join(chunk),
                   "--out-dir", str(worker_dirs[idx])]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            sys.stdout.write(proc.stdout)
            if proc.returncode != 0:
                sys.stderr.write(proc.stderr)
                raise RuntimeError(f"worker {idx} ({chunk[0]}..{chunk[-1]}) "
                                   f"exited {proc.returncode}; not merging")
            result = json.loads((worker_dirs[idx] / "worker_result.json").read_text())
            return idx, result["n_new"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_spawn, enumerate(chunks)))
        print(f"parallel: all {len(chunks)} workers ok "
              f"({sum(n for _, n in results)} new filings)", flush=True)
        return _merge_worker_dirs(worker_dirs)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build / validate the EDGAR v2 extension.")
    parser.add_argument("--tickers", default=None, help="Comma-separated tickers (default: full universe)")
    parser.add_argument("--validate", action="store_true", help="Validate existing shards + manifest, no fetching")
    parser.add_argument("--out-dir", default=None,
                        help="Worker mode: write shards to this dir instead of the main v2 dir "
                             "(existing accessions are still read from the main dir)")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel worker processes (default 1 = serial; 4 = daily-refresh mode)")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help=f"Tickers per worker process (default {DEFAULT_CHUNK_SIZE}; keep <= 15)")
    args = parser.parse_args(argv)

    if args.validate:
        ok, report = v1.validate_dataset(window=V2_WINDOW, data_dir=V2_DATA_DIR)
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

    if args.out_dir:
        # Worker mode: fetch one chunk into its own dir. The parent
        # merges afterwards and writes the manifest.
        total_new, failures = _run_worker(tickers, Path(args.out_dir))
        print(f"worker done: {total_new} new filings; failures {len(failures)}", flush=True)
        return 0

    if args.workers > 1:
        total_new, all_failures = _run_parallel(tickers, args.workers, args.chunk_size)
    else:
        existing = v1.load_existing_accessions(data_dir=V2_DATA_DIR)
        print(f"universe: {len(tickers)} tickers; already stored: {len(existing)} filings", flush=True)
        total_new, all_failures = _fetch_chunk(tickers, existing)

    manifest = write_v2_manifest(all_failures)
    print(f"done: {total_new} new filings; total records {manifest['coverage']['n_records']}; "
          f"tickers {manifest['coverage']['n_tickers']}; failures {len(all_failures)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
