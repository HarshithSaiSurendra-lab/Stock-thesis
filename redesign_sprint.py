#!/usr/bin/env python3
"""Sprint redesign ($0, TRAIN ONLY) — Fable audit fix: remove the tie-break.

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.
No API calls ($0 spend). The frozen holdout is never touched here.

Why: Fable 5.2 (independent audit) found the C2 design's train result
(+53% to +182% across defensible tie-break rules) is driven by the
tie-break, not the score: ~23 symbols tie at the 5th-place Haiku score on
most rebalance days, so an arbitrary sort key chooses the 5-name book.
Verdict: "redesign first. Do not spend the holdout on this version."
Single most important change: REMOVE THE TIE-BREAK ENTIRELY — hold every
name tied at the cutoff score.

Redesign (exp-llm-long-horizon-v2, candidate):
  - Signal: same Haiku 5.5 score of each symbol's latest filing (existing
    Phase 2 scores, $0 reuse), carried forward until the next filing.
  - Selection: each day, cutoff = the 5th-highest score among score > 0.
    Hold EVERY name with score >= cutoff (equal weight). No tie-break:
    the book is fully determined by the score. Book size adapts
    (expected ~5-40 names).
  - Execution: weekly rebalance (every 5 days), min hold 10 days,
    1-bar lag (engine), long-only, max_positions set to cover the
    largest observed selection.
  - This script evaluates the redesign on TRAIN ONLY (walk-forward folds
    within train). If train is credible, it is registered and confirmed;
    if train is weak, the pattern does not survive tie-break removal
    and that is the finding.

Each (config x cost) evaluated here is a trial for the Deflated Sharpe
count. Prior n_trials = 72 (26 + 36 calibration + 2 C6-rank + 8
tie-break sensitivity, corrected for the TB1/TB2 double-count).

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python redesign_sprint.py
    (writes experiments/results/sprint-redesign.json)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import TradingConfig
from research_runner import build_signal_variants, evaluate_variants, slice_research_window
from run_real_backtest import _load_cached_bars
import run_llm_backtest as p2

REPO = Path(__file__).parent
OUT_PATH = REPO / "experiments" / "results" / "sprint-redesign.json"

RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63


def main() -> int:
    print("=== Redesign: hold all names tied at the cutoff (TRAIN ONLY, $0) ===",
          flush=True)
    data = _load_cached_bars()
    if data is None:
        raise RuntimeError("no cached bars; .cache/backtest_data is empty")
    cfg = TradingConfig.from_env()
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, RESEARCH_START, RESEARCH_END)
    window_days = prices.index
    train_days = window_days[window_days < window_days[-HOLDOUT_DAYS_N]]
    train_prices = prices.loc[train_days]
    print(f"train {train_days[0].date()}..{train_days[-1].date()} ({len(train_days)} days)",
          flush=True)

    filings = p2.load_edgar_filings()
    haiku = p2._load_scores(REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json")
    in_scope_train = p2.in_scope_filings(filings, variants, train_days)
    current = p2.current_filing_series(in_scope_train, train_days)
    score = pd.DataFrame(0.0, index=train_days, columns=sorted(current.columns))
    for sym in current.columns:
        for day in train_days:
            acc = current.at[day, sym]
            if isinstance(acc, str):
                h = haiku.get(acc)
                if h is not None and "error" not in h:
                    score.at[day, sym] = float(h["score"])

    # Selection panel: 1.0 for every name with score >= the day's 5th-highest
    # positive score, 0.0 otherwise. No tie-break anywhere.
    selection = pd.DataFrame(0.0, index=train_days, columns=score.columns)
    sizes = []
    for day in train_days:
        row = score.loc[day]
        pos = row[row > 0].sort_values(ascending=False)
        if len(pos) == 0:
            continue
        k = min(5, len(pos))
        cutoff = pos.iloc[k - 1]
        sel = row >= cutoff
        selection.loc[day, sel] = 1.0
        sizes.append(int(sel.sum()))
    sizes = np.array(sizes)
    print(f"selection size: mean {sizes.mean():.1f}  min {sizes.min()}  "
          f"max {sizes.max()}  median {int(np.median(sizes))}", flush=True)
    max_pos = int(max(60, sizes.max() + 5))
    print(f"max_positions set to {max_pos}", flush=True)

    results: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "spend_usd": 0.0,
        "train_window": f"{train_days[0].date()}..{train_days[-1].date()}",
        "design": ("hold every name with Haiku score >= the day's 5th-highest "
                   "positive score, equal weight; no tie-break"),
        "selection_size": {"mean": float(sizes.mean()), "min": int(sizes.min()),
                           "max": int(sizes.max()),
                           "median": float(np.median(sizes))},
        "max_positions": max_pos,
        "variants": {},
    }
    n_new = 0
    for cost in (0, 10, 20):
        res = evaluate_variants(
            train_prices, {"redesign_no_tiebreak": selection}, cost_bps=float(cost),
            max_positions=max_pos, rebalance_every=5, n_splits=4,
            initial_capital=1000.0, min_hold_days=10)
        row = res.iloc[0]
        results["variants"][f"cost_{cost}bps"] = {
            "total_return": float(row["total_return"]),
            "trades": int(row["trades"]),
            "avg_daily_turnover": float(row["avg_daily_turnover"]),
            "sharpe": float(row["sharpe"]) if pd.notna(row["sharpe"]) else None,
            "hit_rate": float(row["hit_rate"]) if pd.notna(row["hit_rate"]) else None,
            "max_drawdown": float(row["max_drawdown"]) if pd.notna(row["max_drawdown"]) else None,
            "wf_median_sharpe": float(row["wf_median_sharpe"]) if row["wf_median_sharpe"] is not None and pd.notna(row["wf_median_sharpe"]) else None,
            "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
        }
        n_new += 1
    for cost in (0, 10, 20):
        r = results["variants"][f"cost_{cost}bps"]
        print(f"  cost {cost:2d}bps: total {r['total_return']*100:+7.2f}%  "
              f"wf_med {r['wf_median_sharpe']}  wf+ {r['wf_positive_folds']}/4  "
              f"trades {r['trades']:5d}  turn {r['avg_daily_turnover']:.3f}", flush=True)

    results["n_trials_new"] = n_new
    results["n_trials_total_dsr"] = 72 + n_new
    OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nnew trials: {n_new}; DSR n_trials total: {72 + n_new}", flush=True)
    print(f"report written to {OUT_PATH}", flush=True)
    print("SPEND: $0.00 (no API calls)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
