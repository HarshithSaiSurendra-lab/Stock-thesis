"""Real-data backtest driver for exp-technical-only-v1 (P0).

Fetches Alpaca history ONCE, caches bars to .cache/backtest_data/ so
reruns are reproducible without re-fetching, then evaluates the four
technical variants (v1 composite, v2 regime, v3 regime+trend-quality,
v4 regime+quality+risk) on:

- the TRAIN window (everything before the frozen holdout), and
- the HOLDOUT window (the final 63 trading days of the acquired
  dataset, per the frozen holdout rule in experiments/holdout.json),

at 1x costs (10 bps) and stressed 2x costs (20 bps), with walk-forward
splits on the train window, against two baselines: SPY buy-and-hold
and an equal-weight universe portfolio.

Honesty limits, recorded in the output JSON:
- Alpaca data here is the IEX feed with RAW adjustment and the CURRENT
  universe (seed symbols as of Nov 2026). It is NOT survivorship-free
  and NOT point-in-time with delistings, so this is a first-pass
  backtest, not P0 evidence on its own. The holdout dates are recorded
  in experiments/holdout_dates.json and must never change.
- The LLM comparison (exp-technical-plus-llm-v1) is NOT run here: it
  needs historical, point-in-time 10-K/10-Q excerpts, which do not
  exist yet. Scoring filings that post-date the backtest window would
  be lookahead. That experiment stays registered, unrun.

Usage:
  set -a; . ./.env; set +a
  .venv/bin/python run_real_backtest.py [--start-date 2024-01-01] [--end-date 2026-11-16]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest import BacktestConfig, compute_stats, run_backtest, walk_forward_splits  # noqa: E402
from config import TradingConfig  # noqa: E402
from research_runner import (  # noqa: E402
    build_signal_variants,
    evaluate_variants,
    fetch_alpaca_history,
    slice_bars_window,
    slice_research_window,
)

REPO = Path(__file__).parent
CACHE_DIR = REPO / ".cache" / "backtest_data"
RESULTS_DIR = REPO / "experiments" / "results"
HOLDOUT_DAYS = 63


def _tz_naive(frame: pd.DataFrame) -> pd.DataFrame:
    # Alpaca returns tz-aware (UTC) bar timestamps; the research
    # slicers compare against tz-naive dates. Strip tz once, here.
    idx = frame.index
    if getattr(idx, "tz", None) is not None:
        frame = frame.copy()
        frame.index = idx.tz_convert(None)
    return frame


def _save_bars(data) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for symbol, frame in data.bars.items():
        _tz_naive(frame).to_csv(CACHE_DIR / f"{symbol}.csv")


def _load_cached_bars():
    from research_runner import ResearchData

    bars = {}
    for path in sorted(CACHE_DIR.glob("*.csv")):
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        bars[path.stem] = _tz_naive(frame)
    return ResearchData(bars, "alpaca-cached") if bars else None


def _baseline_stats(prices: pd.DataFrame, benchmark: str) -> dict:
    """Buy-and-hold SPY and equal-weight universe, no costs, 1-bar lag not applied."""
    out = {}
    spy = prices[benchmark].dropna()
    spy_rets = spy.pct_change().fillna(0.0)
    out["spy_buy_and_hold"] = {
        "total_return": round(float(spy_rets.add(1).prod() - 1), 4),
        **{k: round(float(v), 4) for k, v in compute_stats(spy_rets, pd.Series(0.0, index=spy_rets.index)).items()},
    }
    universe = prices.drop(columns=[benchmark])
    rets = universe.pct_change().fillna(0.0)
    ew_rets = rets.mean(axis=1)
    out["equal_weight_universe"] = {
        "total_return": round(float(ew_rets.add(1).prod() - 1), 4),
        **{k: round(float(v), 4) for k, v in compute_stats(ew_rets, pd.Series(0.0, index=ew_rets.index)).items()},
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default="2024-01-01")
    parser.add_argument("--end-date", default="2026-11-16")
    parser.add_argument("--initial-capital", type=float, default=1000.0)
    parser.add_argument("--use-cache", action="store_true", help="reuse .cache/backtest_data instead of fetching")
    args = parser.parse_args()

    cfg = TradingConfig.from_env()
    symbols = tuple(dict.fromkeys((*cfg.universe.seed_symbols, cfg.regime.benchmark_symbol)))

    if args.use_cache:
        data = _load_cached_bars()
        if data is None:
            raise RuntimeError("no cached bars; run without --use-cache first")
    else:
        data = fetch_alpaca_history(symbols, cfg, years=5, start_date=args.start_date, end_date=args.end_date)
        _save_bars(data)
    print(f"source={data.source} symbols={len(data.bars)}")

    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, args.start_date, args.end_date)
    bars = slice_bars_window(data.bars, prices.columns, args.start_date, args.end_date)
    if prices.empty:
        raise RuntimeError("no prices in requested window")
    print(f"window={prices.index.min().date()}..{prices.index.max().date()} trading_days={len(prices)}")

    # Frozen holdout: final 63 trading days of the acquired window.
    holdout_start = prices.index[-HOLDOUT_DAYS]
    train_prices = prices.loc[prices.index < holdout_start]
    holdout_prices = prices.loc[prices.index >= holdout_start]
    print(f"train={train_prices.index.min().date()}..{train_prices.index.max().date()} ({len(train_prices)} days)")
    print(f"holdout={holdout_prices.index.min().date()}..{holdout_prices.index.max().date()} ({len(holdout_prices)} days)")

    holdout_dates = {
        "definition": "final 63 trading days of the acquired Alpaca IEX/raw dataset",
        "train_start": str(train_prices.index.min().date()),
        "train_end": str(train_prices.index.max().date()),
        "holdout_start": str(holdout_prices.index.min().date()),
        "holdout_end": str(holdout_prices.index.max().date()),
        "holdout_trading_days": len(holdout_prices),
        "window_start": str(prices.index.min().date()),
        "window_end": str(prices.index.max().date()),
        "source": data.source,
        "market_data_feed": cfg.run.market_data_feed,
        "market_data_adjustment": cfg.run.market_data_adjustment,
        "note": "Recorded at first real-data backtest, 2026-11-17. Must never change.",
    }
    holdout_path = REPO / "experiments" / "holdout_dates.json"
    if holdout_path.exists():
        existing = json.loads(holdout_path.read_text())
        assert existing == holdout_dates, "holdout dates already recorded differently; refusing to change them"
    else:
        holdout_path.write_text(json.dumps(holdout_dates, indent=2))

    def _eval(window_prices, window_variants, window_bars, benchmark_prices, label):
        rows = {}
        for cost_bps in (10.0, 20.0):
            result = evaluate_variants(
                window_prices, window_variants, cost_bps=cost_bps,
                max_positions=5, rebalance_every=1, n_splits=4,
                initial_capital=args.initial_capital, bars=window_bars,
            )
            rows[f"cost_{int(cost_bps)}bps"] = result.to_dict(orient="records")
        rows["baselines_no_cost"] = _baseline_stats(benchmark_prices, cfg.regime.benchmark_symbol)
        return rows

    train_variants = {k: v.loc[train_prices.index] for k, v in variants.items()}
    holdout_variants = {k: v.loc[holdout_prices.index] for k, v in variants.items()}
    train_bars = slice_bars_window(data.bars, prices.columns, str(train_prices.index.min().date()), str(train_prices.index.max().date()))
    holdout_bars = slice_bars_window(data.bars, prices.columns, str(holdout_prices.index.min().date()), str(holdout_prices.index.max().date()))

    # Full close panel (including the SPY benchmark) for baseline stats;
    # build_signal_variants returns the universe-only panel.
    full_prices = pd.DataFrame(
        {s: f["close"] for s, f in data.bars.items()}
    ).sort_index().loc[prices.index]
    train_full = full_prices.loc[train_prices.index]
    holdout_full = full_prices.loc[holdout_prices.index]

    output = {
        "experiment_id": "exp-technical-only-v1",
        "run_date": "2026-11-17",
        "meta": {
            "source": data.source,
            "symbols": len(prices.columns),
            "symbols_with_data": sorted(data.bars.keys()),
            "market_data_feed": cfg.run.market_data_feed,
            "market_data_adjustment": cfg.run.market_data_adjustment,
            "initial_capital": args.initial_capital,
            "max_positions": 5,
            "rebalance_every": 1,
            "parameters": {"min_decision_score": 10.25, "target_n_positions": 5, "trail_percent": 8.0},
        },
        "holdout": holdout_dates,
        "limits": [
            "IEX feed, RAW adjustment, CURRENT universe (seed symbols as of Nov 2026).",
            "NOT survivorship-free, NOT point-in-time with delistings.",
            "First-pass backtest only; not P0 evidence on its own.",
            "LLM comparison not run: no historical point-in-time filing excerpts yet.",
        ],
        "train": _eval(train_prices, train_variants, train_bars, train_full, "train"),
        "holdout": _eval(holdout_prices, holdout_variants, holdout_bars, holdout_full, "holdout"),
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "exp-technical-only-v1.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"wrote {out_path}")

    for label in ("train", "holdout"):
        print(f"\n== {label} (10bps) ==")
        for row in output[label]["cost_10bps"]:
            print(
                f"{row['variant']:28s} return={row['total_return']:+.3f} "
                f"sharpe={row.get('sharpe')} maxdd={row.get('max_drawdown')} "
                f"trades={row['trades']} wf_med_sharpe={row.get('wf_median_sharpe')} "
                f"wf_pos={row.get('wf_positive_folds')}/{row.get('wf_folds')}"
            )
        print(f"  baselines: SPY buy-hold return={output[label]['baselines_no_cost']['spy_buy_and_hold']['total_return']:+.3f} "
              f"equal-weight return={output[label]['baselines_no_cost']['equal_weight_universe']['total_return']:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
