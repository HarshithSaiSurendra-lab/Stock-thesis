"""exp-technical-v2 driver (research-only, NOT imported by the order path).

Runs the registered v2 variant — strong_up only, trend-quality >= 4.0,
weekly rebalancing (every 5 days) + 5-day minimum holding period,
regime mask identical to v2_regime, 1-bar lag kept — on:
  - the TRAIN window first (2024-01-02..2026-08-17), then
  - the FROZEN HOLDOUT (2026-08-18..2026-11-13, 63 days).

The holdout dates are read from experiments/holdout_dates.json and
asserted, never changed. No parameter here was tuned on the holdout:
the design comes from the train-only Phase 0 diagnosis.

Costs: 10bps (1x) and 20bps (2x), plus a 0bps gross run for the
gross-vs-net split. Extended diagnostics mirror diagnose_phase0.py
(Deflated Sharpe with n_trials=10 — 8 from v1 + 2 from v2 — alpha/beta,
information ratio, t-stat, total costs, per-trade stats, exposure,
per-symbol attribution).

Outputs: experiments/results/exp-technical-v2.json.
Usage: .venv/bin/python run_v2_backtest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest import BacktestConfig, run_backtest  # noqa: E402
from config import TradingConfig  # noqa: E402
from diagnostics import (  # noqa: E402
    alpha_beta,
    deflated_sharpe,
    exposure_stats,
    information_ratio,
    per_symbol_pnl,
    return_significance,
    total_costs,
    trade_stats,
)
from research_runner import (  # noqa: E402
    _feature_panels,
    build_v2_signal,
    evaluate_variants,
    slice_research_window,
)
from run_real_backtest import _baseline_stats, _load_cached_bars  # noqa: E402
from trade_signal import composite_signal, trend_quality_score  # noqa: E402

REPO = Path(__file__).parent
RESULTS_DIR = REPO / "experiments" / "results"
N_TRIALS = 10  # 4 v1 variants x 2 costs + 1 v2 variant x 2 costs
V2_CFG = dict(max_positions=5, rebalance_every=5, min_hold_days=5)


def _v2_config(cost_bps: float) -> BacktestConfig:
    return BacktestConfig(
        cost_bps=cost_bps, long_only=True,
        max_positions=V2_CFG["max_positions"],
        rebalance_every=V2_CFG["rebalance_every"],
        min_hold_days=V2_CFG["min_hold_days"],
    )


def _extended(prices: pd.DataFrame, signal: pd.DataFrame, spy_rets: pd.Series) -> dict:
    """Extended diagnostics for one window, mirroring diagnose_phase0.py."""
    runs = {}
    for cost in (0.0, 10.0, 20.0):
        runs[cost] = run_backtest(prices, signal, _v2_config(cost))
    gross_ret = runs[0.0].daily_returns
    net10 = runs[10.0].daily_returns
    net20 = runs[20.0].daily_returns
    entry: dict = {
        "total_return_gross_0bps": round(float((1 + gross_ret).prod() - 1), 4),
        "total_return_net_10bps": round(float((1 + net10).prod() - 1), 4),
        "total_return_net_20bps": round(float((1 + net20).prod() - 1), 4),
        "cost_drag_10bps": round(float((1 + gross_ret).prod() - (1 + net10).prod()), 4),
        "cost_drag_20bps": round(float((1 + gross_ret).prod() - (1 + net20).prod()), 4),
        "sharpe_gross": runs[0.0].stats.get("sharpe"),
        "sharpe_net_10bps": runs[10.0].stats.get("sharpe"),
        "sharpe_net_20bps": runs[20.0].stats.get("sharpe"),
        "max_drawdown_net_10bps": runs[10.0].stats.get("max_drawdown"),
        "max_drawdown_net_20bps": runs[20.0].stats.get("max_drawdown"),
    }
    entry.update(deflated_sharpe(net10, N_TRIALS))
    entry.update(alpha_beta(net10, spy_rets.reindex(net10.index)))
    entry.update(information_ratio(net10, spy_rets.reindex(net10.index)))
    entry.update(return_significance(net10))
    entry.update(total_costs(runs[10.0].turnover, 10.0))
    entry["trades"] = trade_stats(runs[10.0].weights, prices, 10.0)
    entry.update(exposure_stats(runs[10.0].weights))
    entry["max_gross_exposure"] = round(
        float(runs[10.0].weights.fillna(0.0).abs().sum(axis=1).max()), 3
    )
    pnl = per_symbol_pnl(runs[10.0].weights, prices)
    entry["per_symbol"] = {
        "top_winners": [
            {"symbol": s, "gross_pnl_frac": round(float(v), 4), "days_held": int(pnl.loc[s, "days_held"])}
            for s, v in pnl["gross_pnl_frac"].tail(5).items()
        ],
        "top_losers": [
            {"symbol": s, "gross_pnl_frac": round(float(v), 4), "days_held": int(pnl.loc[s, "days_held"])}
            for s, v in pnl["gross_pnl_frac"].head(5).items()
        ],
        "symbols_with_positive_gross": int((pnl["gross_pnl_frac"] > 0).sum()),
        "symbols_with_negative_gross": int((pnl["gross_pnl_frac"] < 0).sum()),
        "symbols_ever_held": int((pnl["days_held"] > 0).sum()),
    }
    return entry


def main() -> int:
    cfg = TradingConfig.from_env()
    data = _load_cached_bars()
    assert data is not None, "no cached bars; run run_real_backtest.py first"

    holdout_dates = json.loads((REPO / "experiments" / "holdout_dates.json").read_text())
    train_start, train_end = holdout_dates["train_start"], holdout_dates["train_end"]
    holdout_start, holdout_end = holdout_dates["holdout_start"], holdout_dates["holdout_end"]

    prices, v2_signal = build_v2_signal(data, cfg)
    # Cached bars are timestamped 05:00 UTC, so slicing to holdout_end
    # at midnight would drop the final day's bars; slice to the next
    # day (no bars exist there — data ends 2026-11-13).
    from datetime import date as _date, timedelta as _timedelta

    slice_end = str(_date.fromisoformat(holdout_end) + _timedelta(days=1))
    prices, v2_signal = slice_research_window(prices, {"v2": v2_signal}, train_start, slice_end)
    v2_signal = v2_signal["v2"]
    assert str(prices.index.min().date()) == train_start
    assert str(prices.index.max().date()) == holdout_end
    train_prices = prices.loc[prices.index < holdout_start]
    holdout_prices = prices.loc[prices.index >= holdout_start]
    assert str(train_prices.index.max().date()) == train_end
    assert str(holdout_prices.index.min().date()) == holdout_start
    assert str(holdout_prices.index.max().date()) == holdout_end
    assert len(holdout_prices) == holdout_dates["holdout_trading_days"] == 63
    print(f"train={train_start}..{train_end} ({len(train_prices)} days)")
    print(f"holdout={holdout_start}..{holdout_end} ({len(holdout_prices)} days)")

    full_prices = pd.DataFrame(
        {s: f["close"] for s, f in data.bars.items()}
    ).sort_index().loc[prices.index]

    # --- Filter-binding diagnostic (train only): how much does tq >= 4.0 bite?
    _, features = _feature_panels(data)
    strong_days = tq_below = 0
    for symbol in prices.columns:
        feats = features[symbol].reindex(train_prices.index)
        labels = composite_signal(feats)
        tq = trend_quality_score(feats)
        strong = labels == "strong_up"
        strong_days += int(strong.sum())
        tq_below += int((strong & (tq < 4.0)).sum())
    filter_binding = {
        "min_trend_quality": 4.0,
        "strong_up_days_train": strong_days,
        "strong_up_days_with_tq_below_4": tq_below,
        "frac_strong_below_threshold": round(tq_below / strong_days, 4) if strong_days else None,
    }
    print(f"filter: {tq_below}/{strong_days} strong_up days below tq 4.0 "
          f"({filter_binding['frac_strong_below_threshold']})")

    def _eval(window_prices, window_signal, benchmark_prices):
        rows = {}
        for cost_bps in (10.0, 20.0):
            result = evaluate_variants(
                window_prices, {"v2": window_signal}, cost_bps=cost_bps,
                max_positions=V2_CFG["max_positions"],
                rebalance_every=V2_CFG["rebalance_every"],
                n_splits=4, initial_capital=1000.0,
                min_hold_days=V2_CFG["min_hold_days"],
            )
            rows[f"cost_{int(cost_bps)}bps"] = result.to_dict(orient="records")
        rows["baselines_no_cost"] = _baseline_stats(benchmark_prices, cfg.regime.benchmark_symbol)
        return rows

    # --- TRAIN first ---
    train_signal = v2_signal.loc[train_prices.index]
    train_full = full_prices.loc[train_prices.index]
    spy_train = train_full["SPY"].dropna().pct_change().fillna(0.0)
    train_rows = _eval(train_prices, train_signal, train_full)
    train_ext = _extended(train_prices, train_signal, spy_train)
    print(f"TRAIN 10bps: return={train_rows['cost_10bps'][0]['total_return']:+.3f} "
          f"sharpe={train_rows['cost_10bps'][0].get('sharpe')} "
          f"maxdd={train_rows['cost_10bps'][0].get('max_drawdown')} "
          f"trades={train_rows['cost_10bps'][0]['trades']} "
          f"wf_med={train_rows['cost_10bps'][0].get('wf_median_sharpe')} "
          f"wf_pos={train_rows['cost_10bps'][0].get('wf_positive_folds')}/{train_rows['cost_10bps'][0].get('wf_folds')}")
    print(f"TRAIN ext: gross={train_ext['total_return_gross_0bps']:+.3f} "
          f"dsr={train_ext.get('deflated_sharpe')} alpha={train_ext.get('alpha_annual')} "
          f"expectancy={train_ext['trades'].get('expectancy_per_trade_bps')}bps "
          f"med_hold={train_ext['trades'].get('median_holding_days')}d "
          f"costs=${train_ext.get('total_cost_usd_on_1000')}/$1000")

    # --- HOLDOUT second (only after train is fully computed) ---
    holdout_signal = v2_signal.loc[holdout_prices.index]
    holdout_full = full_prices.loc[holdout_prices.index]
    spy_hold = holdout_full["SPY"].dropna().pct_change().fillna(0.0)
    holdout_rows = _eval(holdout_prices, holdout_signal, holdout_full)
    holdout_ext = _extended(holdout_prices, holdout_signal, spy_hold)
    print(f"HOLDOUT 10bps: return={holdout_rows['cost_10bps'][0]['total_return']:+.3f} "
          f"sharpe={holdout_rows['cost_10bps'][0].get('sharpe')} "
          f"maxdd={holdout_rows['cost_10bps'][0].get('max_drawdown')} "
          f"trades={holdout_rows['cost_10bps'][0]['trades']}")
    print(f"HOLDOUT ext: gross={holdout_ext['total_return_gross_0bps']:+.3f} "
          f"dsr={holdout_ext.get('deflated_sharpe')} alpha={holdout_ext.get('alpha_annual')} "
          f"expectancy={holdout_ext['trades'].get('expectancy_per_trade_bps')}bps "
          f"costs=${holdout_ext.get('total_cost_usd_on_1000')}/$1000")

    # --- Gate check (from the registry entry) ---
    spy_hold_ret = holdout_rows["baselines_no_cost"]["spy_buy_and_hold"]["total_return"]
    gate = {
        "a_train_net10_positive": train_ext["total_return_net_10bps"] > 0,
        "b_holdout_net10_positive_and_beats_spy": (
            holdout_ext["total_return_net_10bps"] > 0
            and holdout_ext["total_return_net_10bps"] > spy_hold_ret
        ),
        "c_train_dsr_ge_0_95": (train_ext.get("deflated_sharpe") or 0) >= 0.95,
        "d_holdout_net20_positive": holdout_ext["total_return_net_20bps"] > 0,
        "spy_holdout_return": spy_hold_ret,
    }
    gate["pass"] = all(
        [gate["a_train_net10_positive"], gate["b_holdout_net10_positive_and_beats_spy"],
         gate["c_train_dsr_ge_0_95"], gate["d_holdout_net20_positive"]]
    )
    print(f"GATE: {'PASS' if gate['pass'] else 'FAIL'} {gate}")

    output = {
        "experiment_id": "exp-technical-v2",
        "run_date": "2026-11-18",
        "meta": {
            "source": data.source,
            "symbols": len(prices.columns),
            "symbols_with_data": sorted(data.bars.keys()),
            "market_data_feed": cfg.run.market_data_feed,
            "market_data_adjustment": cfg.run.market_data_adjustment,
            "initial_capital": 1000.0,
            "max_positions": 5,
            "rebalance_every": 5,
            "min_hold_days": 5,
            "parameters": {
                "min_decision_score": 10.25, "target_n_positions": 5, "trail_percent": 8.0,
                "min_hold_days": 5, "rebalance_every": 5, "min_trend_quality": 4.0,
                "strong_only": True, "regime_mask": "same as v2_regime (unchanged)",
            },
        },
        "holdout": holdout_dates,
        "limits": [
            "IEX feed, RAW adjustment, CURRENT universe (seed symbols as of Nov 2026).",
            "NOT survivorship-free, NOT point-in-time with delistings.",
            "First-pass backtest only; not P0 evidence on its own.",
            "LLM comparison not run: no historical point-in-time filing excerpts yet.",
            "Holdout dates were already observed for v1, so the v2 holdout check is a confirmation, not a pristine out-of-sample test.",
        ],
        "filter_binding": filter_binding,
        "train": {**train_rows, "extended": train_ext},
        "holdout": {**holdout_rows, "extended": holdout_ext},
        "gate": gate,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / "exp-technical-v2.json"
    out_path.write_text(json.dumps(output, indent=2, default=str))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
