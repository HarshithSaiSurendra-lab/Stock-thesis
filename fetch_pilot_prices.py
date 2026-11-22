#!/usr/bin/env python3
"""Fetch split+dividend-adjusted daily bars for the pilot returns test.

Data reads only. Paper keys from .env (never printed, never committed).
Saves one CSV per symbol under experiments/equibles-pilot-raw/ so the
return computation is reproducible and auditable.
"""
import os
import sys
from pathlib import Path

REPO = Path("/home/hatch/workspace/stock-thesis")
sys.path.insert(0, str(REPO))

# Load .env the same way the repo does (keys from env only).
for line in (REPO / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment

SYMBOLS = ["ABBV", "ABT", "AMD", "AMGN", "AMT", "AMZN", "SPY"]
START = "2025-10-01"
END = "2026-11-21"

key = os.getenv("ALPACA_PAPER_KEY")
secret = os.getenv("ALPACA_PAPER_SECRET")
assert key and secret, "missing Alpaca paper keys"

client = StockHistoricalDataClient(key, secret)
out_dir = REPO / "experiments" / "equibles-pilot-raw"
out_dir.mkdir(parents=True, exist_ok=True)

for sym in SYMBOLS:
    req = StockBarsRequest(
        symbol_or_symbols=sym,
        timeframe=TimeFrame.Day,
        start=START,
        end=END,
        adjustment=Adjustment.ALL,
    )
    resp = client.get_stock_bars(req)
    df = resp.df
    if hasattr(df.index, "names") and "symbol" in (df.index.names or []):
        df = df.xs(sym, level="symbol")
    df = df.rename(columns={c: str(c).lower() for c in df.columns})
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    path = out_dir / f"prices-adjusted-{sym}.csv"
    df.to_csv(path)
    print(f"{sym}: {len(df)} bars, {df.index.min().date()} .. {df.index.max().date()} -> {path.name}")
print("done")
