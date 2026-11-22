#!/usr/bin/env python3
"""Confirmation for exp-llm-long-horizon-v2 (registered BEFORE scoring).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.
$0 new scoring: reuses Phase 2 Haiku scores (1,166 train filings) and the
freshness Haiku scores (75 holdout-window filings). No API calls.

Registered design (see experiments/exp-llm-long-horizon-v2-design.md):
  - Signal: Haiku 5.5 score of each symbol's latest filing (filing date
    <= day, carried forward until the next filing).
  - Selection: each day, cutoff = the 5th-highest score among score > 0.
    Hold EVERY name with score >= cutoff, equal weight. No tie-break.
  - Execution: weekly rebalance (every 5 days), min hold 10 days, 1-bar
    lag (engine), long-only, max_positions 60, $1,000 initial capital.
  - Current-filing series: union of in-scope train + in-scope holdout
    filings (same construction as the freshness run). The driver verifies
    the union series equals the stale series on train days, so the train
    reproduction is exact (must match the redesign's +57.77% net10).

Gate (same structure as Phase 2 / freshness):
  SG1: holdout net return @10bps > v4 base (-4.33%)
  SG2: holdout net return @20bps > v4 base (-8.45%)
  SG3: holdout net return @10bps > 0
  Any sub-gate failing = FAIL: variant dropped per the kill rule.
  Diagnostics (Sharpe, max DD, Deflated Sharpe n_trials=75, alpha/beta
  vs SPY) are reported, not gated.

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python run_llm_long_horizon_v2.py
    (writes experiments/results/exp-llm-long-horizon-v2.json)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest import BacktestConfig, run_backtest
from config import TradingConfig
from diagnostics import alpha_beta, deflated_sharpe
from research_runner import build_signal_variants, slice_research_window
from run_real_backtest import _load_cached_bars
import run_llm_backtest as p2
import run_freshness_backtest as rf

REPO = Path(__file__).parent
OUT_PATH = REPO / "experiments" / "results" / "exp-llm-long-horizon-v2.json"

RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63
N_TRIALS_DSR = 75
V4_BASE_HOLDOUT_NET10 = -0.0433
V4_BASE_HOLDOUT_NET20 = -0.0845
MAX_POSITIONS = 60
REBALANCE_EVERY = 5
MIN_HOLD_DAYS = 10


def build_selection_panel(score: pd.DataFrame) -> pd.DataFrame:
    """1.0 for every name with score >= the day's 5th-highest positive score."""
    selection = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    for day in score.index:
        row = score.loc[day]
        pos = row[row > 0].sort_values(ascending=False)
        if len(pos) == 0:
            continue
        cutoff = pos.iloc[min(5, len(pos)) - 1]
        selection.loc[day, row >= cutoff] = 1.0
    return selection


def main() -> int:
    print("=== Confirmation: exp-llm-long-horizon-v2 ($0, registered design) ===",
          flush=True)
    data = _load_cached_bars()
    if data is None:
        raise RuntimeError("no cached bars; .cache/backtest_data is empty")
    cfg = TradingConfig.from_env()
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, RESEARCH_START, RESEARCH_END)
    window_days = prices.index
    holdout_start = window_days[-HOLDOUT_DAYS_N]
    train_days = window_days[window_days < holdout_start]
    holdout_days = window_days[window_days >= holdout_start]
    print(f"train {train_days[0].date()}..{train_days[-1].date()} "
          f"({len(train_days)} days); holdout {holdout_days[0].date()}.."
          f"{holdout_days[-1].date()} ({len(holdout_days)} days)", flush=True)

    filings = p2.load_edgar_filings()
    haiku_train = p2._load_scores(
        REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json")
    haiku_fresh = p2._load_scores(
        REPO / "experiments" / "results" / "exp-technical-plus-llm-freshness-v1-scores-haiku.json")
    merged_haiku = {**haiku_train, **haiku_fresh}
    print(f"Haiku scores: train {len(haiku_train)}, fresh holdout {len(haiku_fresh)}, "
          f"merged {len(merged_haiku)}", flush=True)

    in_scope_train = p2.in_scope_filings(filings, variants, train_days)
    in_scope_hw = rf.in_scope_holdout_filings(filings, variants, holdout_days)
    filings_union = pd.concat([in_scope_train, in_scope_hw], ignore_index=True)
    current_union = rf.current_filing_series_union(filings_union, window_days)
    # Train days must be identical to the stale (train-only) series.
    current_stale = p2.current_filing_series(in_scope_train, window_days)
    if not (current_union.loc[train_days].fillna("")
            == current_stale.loc[train_days].fillna("")).all().all():
        raise RuntimeError("union current series differs from stale on TRAIN days; aborting")
    print("union series == stale series on train days: OK", flush=True)

    score = pd.DataFrame(0.0, index=window_days,
                         columns=sorted(current_union.columns))
    for sym in current_union.columns:
        for day in window_days:
            acc = current_union.at[day, sym]
            if isinstance(acc, str):
                h = merged_haiku.get(acc)
                if h is not None and "error" not in h:
                    score.at[day, sym] = float(h["score"])
    selection = build_selection_panel(score)
    sizes = (selection > 0).sum(axis=1)
    print(f"selection size (full window): mean {sizes.mean():.1f}  min {int(sizes.min())}  "
          f"max {int(sizes.max())}; holdout mean {sizes.loc[holdout_days].mean():.1f}",
          flush=True)

    results: dict = {
        "experiment_id": "exp-llm-long-horizon-v2",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "spend_usd": 0.0,
        "train_window": f"{train_days[0].date()}..{train_days[-1].date()}",
        "holdout_window": f"{holdout_days[0].date()}..{holdout_days[-1].date()}",
        "design": ("hold every name with Haiku score >= the day's 5th-highest "
                   "positive score, equal weight; no tie-break; rebalance 5, "
                   "min hold 10, max_positions 60"),
        "selection_size": {"train_mean": float(sizes.loc[train_days].mean()),
                           "holdout_mean": float(sizes.loc[holdout_days].mean()),
                           "max": int(sizes.max())},
    }

    # --- Train reproduction (must match the redesign's +57.77% net10) -----
    train_selection = selection.loc[train_days]
    train_prices = prices.loc[train_days]
    for cost in (0, 10, 20):
        out = run_backtest(train_prices, train_selection,
                           BacktestConfig(cost_bps=float(cost), long_only=True,
                                          max_positions=MAX_POSITIONS,
                                          rebalance_every=REBALANCE_EVERY,
                                          min_hold_days=MIN_HOLD_DAYS))
        key = f"train_cost_{cost}bps"
        results[key] = {
            "total_return": float(out.equity_curve.iloc[-1]) - 1.0,
            "trades": int(((out.weights.fillna(0.0) > 0) & (out.weights.fillna(0.0).shift(1).fillna(0.0) <= 0)).sum().sum()),
            "avg_daily_turnover": float(out.stats.get("avg_daily_turnover")) if out.stats.get("avg_daily_turnover") is not None else None,
        }
        print(f"  TRAIN cost {cost:2d}bps: total {results[key]['total_return']*100:+7.2f}%  "
              f"trades {results[key]['trades']}", flush=True)
    train_net10 = results["train_cost_10bps"]["total_return"]
    if abs(train_net10 - 0.5777) > 0.002:
        raise RuntimeError(
            f"train reproduction mismatch: net10 {train_net10:.4f} vs redesign 0.5777; aborting")

    # --- Holdout confirmation (ONE scoring) --------------------------------
    holdout_selection = selection.loc[holdout_days]
    holdout_prices = prices.loc[holdout_days]
    full_prices = pd.DataFrame(
        {s: f["close"] for s, f in data.bars.items()}).sort_index().loc[window_days]
    spy_holdout = full_prices.loc[holdout_days]["SPY"].pct_change().dropna()
    for cost in (0, 10, 20):
        out = run_backtest(holdout_prices, holdout_selection,
                           BacktestConfig(cost_bps=float(cost), long_only=True,
                                          max_positions=MAX_POSITIONS,
                                          rebalance_every=REBALANCE_EVERY,
                                          min_hold_days=MIN_HOLD_DAYS))
        key = f"holdout_cost_{cost}bps"
        dsr = deflated_sharpe(out.daily_returns, N_TRIALS_DSR)
        ab = alpha_beta(out.daily_returns, spy_holdout)
        results[key] = {
            "total_return": float(out.equity_curve.iloc[-1]) - 1.0,
            "sharpe": out.stats.get("sharpe"),
            "max_drawdown": out.stats.get("max_drawdown"),
            "hit_rate": out.stats.get("hit_rate"),
            "trades": int(((out.weights.fillna(0.0) > 0) & (out.weights.fillna(0.0).shift(1).fillna(0.0) <= 0)).sum().sum()),
            "avg_daily_turnover": float(out.stats.get("avg_daily_turnover")) if out.stats.get("avg_daily_turnover") is not None else None,
            "deflated_sharpe": dsr.get("deflated_sharpe"),
            "sharpe_null_max": dsr.get("sharpe_null_max"),
            "alpha_annual_vs_spy": ab.get("alpha_annual"),
            "beta_vs_spy": ab.get("beta"),
            "r_squared_vs_spy": ab.get("r_squared"),
        }
        print(f"  HOLDOUT cost {cost:2d}bps: total {results[key]['total_return']*100:+7.2f}%  "
              f"sharpe {out.stats.get('sharpe')}  maxDD {out.stats.get('max_drawdown')}  "
              f"DSR {dsr.get('deflated_sharpe')}  alpha/yr {ab.get('alpha_annual')}  "
              f"beta {ab.get('beta')}", flush=True)

    h10 = results["holdout_cost_10bps"]["total_return"]
    h20 = results["holdout_cost_20bps"]["total_return"]
    sg1 = h10 > V4_BASE_HOLDOUT_NET10
    sg2 = h20 > V4_BASE_HOLDOUT_NET20
    sg3 = h10 > 0.0
    verdict = "PASS" if (sg1 and sg2 and sg3) else "FAIL"
    results["gate"] = {
        "sg1_holdout_net10_gt_v4_base": sg1,
        "sg2_holdout_net20_gt_v4_base": sg2,
        "sg3_holdout_net10_positive": sg3,
        "verdict": verdict,
        "v4_base_holdout_net10": V4_BASE_HOLDOUT_NET10,
        "v4_base_holdout_net20": V4_BASE_HOLDOUT_NET20,
    }
    print(f"\nGATE: SG1 {sg1} (net10 {h10*100:+.2f}% vs base -4.33%)  "
          f"SG2 {sg2} (net20 {h20*100:+.2f}% vs base -8.45%)  "
          f"SG3 {sg3} (net10 > 0)  -> {verdict}", flush=True)
    if verdict == "FAIL":
        print("Variant DROPPED per the kill rule. No paper trading.", flush=True)

    OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"results written to {OUT_PATH}", flush=True)
    print("SPEND: $0.00 (no API calls)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
