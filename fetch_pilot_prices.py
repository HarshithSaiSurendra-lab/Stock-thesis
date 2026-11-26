#!/usr/bin/env python3
"""Fetch split+dividend-adjusted daily bars for the pilot returns test.

Data reads only. Paper keys from .env (never printed, never committed).
Saves one CSV per symbol under experiments/equibles-pilot-raw/ so the
return computation is reproducible and auditable.

Standalone behaviour is unchanged (November run): END 2026-11-21,
files prices-adjusted-{sym}.csv. The December wrapper (B12,
scripts/run_equibles_rerun.py) calls main() with an explicit end date
and a dated filename template, so the November files are never
overwritten.
"""
import os
import sys
from pathlib import Path

REPO = Path("/home/hatch/workspace/stock-thesis")
sys.path.insert(0, str(REPO))

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment

SYMBOLS = ["ABBV", "ABT", "AMD", "AMGN", "AMT", "AMZN", "SPY"]
START = "2025-10-01"
END = "2026-11-21"
DEFAULT_OUT_DIR = REPO / "experiments" / "equibles-pilot-raw"


def _load_env():
    # Load .env the same way the repo does (keys from env only).
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _default_client():
    _load_env()
    key = os.getenv("ALPACA_PAPER_KEY")
    secret = os.getenv("ALPACA_PAPER_SECRET")
    assert key and secret, "missing Alpaca paper keys"
    return StockHistoricalDataClient(key, secret)


def main(end=END, out_dir=None, filename_template="prices-adjusted-{sym}.csv",
         client=None, start=START):
    """Fetch adjusted bars for SYMBOLS and write one CSV per symbol.

    `filename_template` is formatted with {sym} and {date} (date=end);
    the default reproduces the November filenames exactly. `client` is
    injectable for tests: a fake whose get_stock_bars(req) returns an
    object with a .df DataFrame. Returns the list of files written.
    """
    out_dir = Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    client = client if client is not None else _default_client()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for sym in SYMBOLS:
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.ALL,
        )
        resp = client.get_stock_bars(req)
        df = resp.df
        if hasattr(df.index, "names") and "symbol" in (df.index.names or []):
            df = df.xs(sym, level="symbol")
        df = df.rename(columns={c: str(c).lower() for c in df.columns})
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        path = out_dir / filename_template.format(sym=sym, date=end)
        df.to_csv(path)
        print(f"{sym}: {len(df)} bars, {df.index.min().date()} .. {df.index.max().date()} -> {path.name}")
        written.append(path)
    print("done")
    return written


if __name__ == "__main__":
    main()
