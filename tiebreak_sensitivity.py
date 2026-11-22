#!/usr/bin/env python3
"""Sprint tie-break sensitivity ($0, TRAIN ONLY).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.
No API calls ($0 spend). The frozen holdout is never touched here.

Why: the C2 panel's Haiku scores are quantized (0.6/0.7/0.8/0.85...), so on
131 of 132 rebalance days many symbols tie at the 5th-place (cutoff) score
(mean 23.3 tied symbols). The engine's stable sort then breaks ties by
panel column order (alphabetical) — an arbitrary rule that shapes the
portfolio. This script measures how much the train result depends on the
tie-break, so the registered design can use a principled one.

Tie-break variants (all rebalance 5 / min-hold 10, train only):
  TB1 alphabetical (as calibrated): score as-is; stable sort -> alphabetical.
  TB2 reverse-alphabetical: score*10000 - alpha_rank.
  TB3 confidence: score*10000 + confidence*100 (higher confidence first).
  TB4 recency: score*10000 - days_since_filing (newer filing first).
  TB5 confidence then recency: score*10000 + confidence*100 - days_since_filing.

Pre-commitment: the REGISTERED design will use TB5 (most principled:
prefer the model's most confident, most recent readings), reported
alongside the full sensitivity table. TB5 is chosen on principle, not
on train performance.

Each variant x cost evaluated here is a trial for the Deflated Sharpe
count. Prior n_trials = 64 (26 + 36 calibration + 2 C6-rank rebuild).

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python tiebreak_sensitivity.py
    (writes experiments/results/sprint-tiebreak-sensitivity.json)
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
OUT_PATH = REPO / "experiments" / "results" / "sprint-tiebreak-sensitivity.json"

RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63


def main() -> int:
    print("=== Tie-break sensitivity (TRAIN ONLY, $0) ===", flush=True)
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
    filing_date = filings.set_index("accession")["filing_date"].to_dict()

    cols = sorted(current.columns)
    score = pd.DataFrame(0.0, index=train_days, columns=cols)
    conf = pd.DataFrame(0.0, index=train_days, columns=cols)
    recency = pd.DataFrame(999.0, index=train_days, columns=cols)
    for sym in cols:
        for day in train_days:
            acc = current.at[day, sym]
            if isinstance(acc, str):
                h = haiku.get(acc)
                if h is not None and "error" not in h:
                    score.at[day, sym] = float(h["score"])
                    conf.at[day, sym] = float(h["confidence"])
                    fd = filing_date.get(acc)
                    if fd is not None:
                        recency.at[day, sym] = float((day - fd).days)

    alpha_rank = pd.Series({s: i for i, s in enumerate(cols)})
    panels = {
        "TB1_alphabetical": score,
        "TB2_reverse_alpha": score * 10000 - alpha_rank,
        "TB3_confidence": score * 10000 + conf * 100,
        "TB4_recency": score * 10000 - recency,
        "TB5_conf_then_recency": score * 10000 + conf * 100 - recency,
    }

    results: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "spend_usd": 0.0,
        "train_window": f"{train_days[0].date()}..{train_days[-1].date()}",
        "pre_commitment": "Registered design uses TB5 (confidence then recency) on principle, not on train performance.",
        "variants": {},
    }
    n_new = 0
    for name, panel in panels.items():
        results["variants"][name] = {}
        for cost in (0, 10):
            res = evaluate_variants(
                train_prices, {name: panel}, cost_bps=float(cost), max_positions=5,
                rebalance_every=5, n_splits=4, initial_capital=1000.0,
                min_hold_days=10)
            row = res.iloc[0]
            results["variants"][name][f"cost_{cost}bps"] = {
                "total_return": float(row["total_return"]),
                "trades": int(row["trades"]),
                "avg_daily_turnover": float(row["avg_daily_turnover"]),
                "sharpe": float(row["sharpe"]) if pd.notna(row["sharpe"]) else None,
                "wf_median_sharpe": float(row["wf_median_sharpe"]) if row["wf_median_sharpe"] is not None and pd.notna(row["wf_median_sharpe"]) else None,
                "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
            }
            n_new += 1
        r0 = results["variants"][name]["cost_0bps"]
        r10 = results["variants"][name]["cost_10bps"]
        print(f"  {name:24s} gross {r0['total_return']*100:+7.2f}%  net10 "
              f"{r10['total_return']*100:+7.2f}%  wf_med {r10['wf_median_sharpe']}  "
              f"wf+ {r10['wf_positive_folds']}/4  trades {r10['trades']:4d}", flush=True)

    results["n_trials_new"] = n_new
    results["n_trials_total_dsr"] = 64 + n_new
    OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nnew trials: {n_new}; DSR n_trials total: {64 + n_new}", flush=True)
    print(f"report written to {OUT_PATH}", flush=True)
    print("SPEND: $0.00 (no API calls)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
