from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from itertools import product
from typing import Iterable

import numpy as np
import pandas as pd

from config import RiskConfig, TradingConfig
from research_runner import (
    ResearchData,
    _regime_mask,
    build_signal_variants,
    evaluate_variants,
    fetch_alpaca_history,
    format_results,
    slice_bars_window,
    slice_research_window,
    synthetic_history,
)
from indicators import build_feature_frame

log = logging.getLogger("robustness")


def _parse_csv_floats(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def _parse_csv_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _parse_csv_dates(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_csv_strings(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def robustness_score(row: dict) -> float:
    annual = float(row.get("annual_return") or 0.0)
    drawdown = abs(float(row.get("max_drawdown") or 0.0))
    sharpe = float(row.get("sharpe") or 0.0)
    trade_penalty = 0.03 if int(row.get("trades") or 0) < 20 else 0.0
    return round(annual + 0.10 * sharpe - 0.75 * drawdown - trade_penalty, 4)


def _benchmark_stats(frame: pd.DataFrame, index: pd.Index, exposure: pd.Series | None = None) -> dict:
    close = frame["close"].reindex(index).dropna()
    if len(close) < 2:
        return {
            "total_return": np.nan,
            "annual_return": np.nan,
            "max_drawdown": np.nan,
        }
    returns = close.pct_change(fill_method=None)
    if exposure is not None:
        active = exposure.reindex(close.index).shift(1)
        active = active.where(active.notna(), False).astype(bool).astype(float)
        returns = returns.mul(active, fill_value=0.0)
    returns = returns.fillna(0.0)
    equity = (1.0 + returns).cumprod()
    total_multiple = float(equity.iloc[-1])
    annual_return = total_multiple ** (252 / len(returns)) - 1 if total_multiple > 0 else np.nan
    drawdown = (equity / equity.cummax() - 1.0).min()
    return {
        "total_return": round(total_multiple - 1.0, 4),
        "annual_return": round(float(annual_return), 4),
        "max_drawdown": round(float(drawdown), 4),
    }


def _attach_benchmark_rows(
    result: dict,
    bars: dict[str, pd.DataFrame],
    index: pd.Index,
    benchmarks: Iterable[str],
    regime_exposure: pd.Series | None = None,
) -> None:
    strategy_return = float(result.get("total_return") or 0.0)
    strategy_drawdown = float(result.get("max_drawdown") or 0.0)
    for symbol in benchmarks:
        frame = bars.get(symbol)
        if frame is None or frame.empty:
            continue
        stats = _benchmark_stats(frame, index)
        prefix = symbol.lower()
        result[f"{prefix}_total_return"] = stats["total_return"]
        result[f"{prefix}_annual_return"] = stats["annual_return"]
        result[f"{prefix}_max_drawdown"] = stats["max_drawdown"]
        if pd.notna(stats["total_return"]):
            result[f"excess_vs_{prefix}"] = round(strategy_return - float(stats["total_return"]), 4)
        if pd.notna(stats["max_drawdown"]):
            result[f"drawdown_delta_vs_{prefix}"] = round(strategy_drawdown - float(stats["max_drawdown"]), 4)
        if regime_exposure is not None:
            gated_stats = _benchmark_stats(frame, index, regime_exposure)
            gated_prefix = f"regime_{prefix}"
            result[f"{gated_prefix}_total_return"] = gated_stats["total_return"]
            result[f"{gated_prefix}_annual_return"] = gated_stats["annual_return"]
            result[f"{gated_prefix}_max_drawdown"] = gated_stats["max_drawdown"]
            if pd.notna(gated_stats["total_return"]):
                result[f"excess_vs_{gated_prefix}"] = round(
                    strategy_return - float(gated_stats["total_return"]), 4
                )
            if pd.notna(gated_stats["max_drawdown"]):
                result[f"drawdown_delta_vs_{gated_prefix}"] = round(
                    strategy_drawdown - float(gated_stats["max_drawdown"]), 4
                )


def summarize_grid(rows: list[dict]) -> pd.DataFrame:
    raw = pd.DataFrame(rows)
    grouped = []
    keys = [
        "variant",
        "max_entry_rvol",
        "target_position_rvol",
        "min_relative_strength_63",
        "max_positions",
        "rebalance_every",
        "throttle_drawdown_pct",
        "halt_drawdown_pct",
        "stop_aware",
        "dynamic_trail",
        "trail_percent",
        "stop_only_exits",
    ]
    for key, group in raw.groupby(keys, dropna=False):
        total_returns = group["total_return"].astype(float)
        drawdowns = group["max_drawdown"].astype(float)
        scores = group["robustness_score"].astype(float)
        summary_row = (
            {
                "variant": key[0],
                "max_entry_rvol": key[1],
                "target_position_rvol": key[2],
                "min_relative_strength_63": key[3],
                "max_positions": key[4],
                "rebalance_every": key[5],
                "throttle_drawdown_pct": key[6],
                "halt_drawdown_pct": key[7],
                "stop_aware": key[8],
                "dynamic_trail": key[9],
                "trail_percent": key[10],
                "stop_only_exits": key[11],
                "avg_total_return": round(float(total_returns.mean()), 4),
                "median_total_return": round(float(total_returns.median()), 4),
                "worst_total_return": round(float(total_returns.min()), 4),
                "avg_max_drawdown": round(float(drawdowns.mean()), 4),
                "worst_max_drawdown": round(float(drawdowns.min()), 4),
                "positive_windows": int((total_returns > 0).sum()),
                "windows": len(group),
                "avg_score": round(float(scores.mean()), 4),
            }
        )
        for col in group.columns:
            if col.startswith("excess_vs_"):
                values = group[col].astype(float)
                bench = col.removeprefix("excess_vs_")
                summary_row[f"avg_excess_vs_{bench}"] = round(float(values.mean()), 4)
                summary_row[f"worst_excess_vs_{bench}"] = round(float(values.min()), 4)
                summary_row[f"outperform_{bench}_windows"] = int((values > 0).sum())
            elif col.startswith("drawdown_delta_vs_"):
                values = group[col].astype(float)
                bench = col.removeprefix("drawdown_delta_vs_")
                summary_row[f"avg_drawdown_delta_vs_{bench}"] = round(float(values.mean()), 4)
        grouped.append(summary_row)
    return pd.DataFrame(grouped).sort_values(
        ["avg_score", "avg_total_return", "worst_max_drawdown"],
        ascending=[False, False, False],
    )


def run_robustness(
    cfg: TradingConfig,
    start_dates: list[str],
    max_entry_rvols: list[float],
    target_position_rvols: list[float],
    min_relative_strengths: list[float],
    max_positions_values: list[int],
    rebalance_values: list[int],
    initial_capital: float,
    cost_bps: float,
    synthetic: bool,
    throttle_drawdown_pct: float | None = None,
    throttle_scale: float = 0.50,
    halt_drawdown_pct: float | None = None,
    halt_scale: float = 0.0,
    stop_aware: bool = False,
    trail_percent: float = 8.0,
    stop_only_exits: bool = True,
    dynamic_trail: bool = False,
    dynamic_trail_vol_multiple: float = 3.0,
    min_trail_percent: float = 6.0,
    max_trail_percent: float = 15.0,
    variants_filter: list[str] | None = None,
    n_splits: int = 0,
    benchmarks: list[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    benchmark_symbols = benchmarks or [cfg.regime.benchmark_symbol]
    symbols = tuple(dict.fromkeys((*cfg.universe.seed_symbols, cfg.regime.benchmark_symbol, *benchmark_symbols)))
    signal_symbols = tuple(dict.fromkeys((*cfg.universe.seed_symbols, cfg.regime.benchmark_symbol)))
    earliest = min(start_dates)
    if synthetic:
        data = synthetic_history(symbols, years=6)
    else:
        data = fetch_alpaca_history(symbols, cfg, years=6, start_date=earliest)
    signal_data = ResearchData(
        {symbol: frame for symbol, frame in data.bars.items() if symbol in signal_symbols},
        data.source,
    )

    rows = []
    detail_rows = []
    for max_entry_rvol, target_position_rvol, min_relative_strength in product(
        max_entry_rvols, target_position_rvols, min_relative_strengths
    ):
        local_cfg = replace(
            cfg,
            risk=replace(
                cfg.risk,
                max_entry_rvol=max_entry_rvol,
                target_position_rvol=target_position_rvol,
            ),
            signals=replace(
                cfg.signals,
                min_relative_strength_63=min_relative_strength,
            ),
        )
        prices, variants = build_signal_variants(signal_data, local_cfg)
        benchmark_features = build_feature_frame(data.bars[cfg.regime.benchmark_symbol]).assign(
            close=data.bars[cfg.regime.benchmark_symbol]["close"]
        )
        regime_exposure = _regime_mask(benchmark_features, local_cfg)
        if variants_filter:
            wanted = set(variants_filter)
            variants = {name: sig for name, sig in variants.items() if name in wanted}
            if not variants:
                raise ValueError(f"no requested variants found: {sorted(wanted)}")
        for max_positions, rebalance_every, start_date in product(
            max_positions_values, rebalance_values, start_dates
        ):
            window_prices, window_variants = slice_research_window(prices, variants, start_date)
            window_bars = slice_bars_window(data.bars, window_prices.columns, start_date)
            if len(window_prices) < 80:
                continue
            results = evaluate_variants(
                window_prices,
                window_variants,
                cost_bps=cost_bps,
                max_positions=max_positions,
                rebalance_every=rebalance_every,
                n_splits=n_splits,
                initial_capital=initial_capital,
                throttle_drawdown_pct=throttle_drawdown_pct,
                throttle_scale=throttle_scale,
                halt_drawdown_pct=halt_drawdown_pct,
                halt_scale=halt_scale,
                bars=window_bars,
                stop_aware=stop_aware,
                trail_percent=trail_percent,
                stop_only_exits=stop_only_exits,
                dynamic_trail=dynamic_trail,
                dynamic_trail_vol_multiple=dynamic_trail_vol_multiple,
                min_trail_percent=min_trail_percent,
                max_trail_percent=max_trail_percent,
            )
            for result in results.to_dict(orient="records"):
                result.update(
                    {
                        "start_date": start_date,
                        "max_entry_rvol": max_entry_rvol,
                        "target_position_rvol": target_position_rvol,
                        "min_relative_strength_63": min_relative_strength,
                        "max_positions": max_positions,
                        "rebalance_every": rebalance_every,
                        "throttle_drawdown_pct": throttle_drawdown_pct,
                        "halt_drawdown_pct": halt_drawdown_pct,
                        "stop_aware": stop_aware,
                        "dynamic_trail": dynamic_trail,
                        "trail_percent": trail_percent,
                        "stop_only_exits": stop_only_exits,
                    }
                )
                _attach_benchmark_rows(
                    result,
                    data.bars,
                    window_prices.index,
                    benchmark_symbols,
                    regime_exposure=regime_exposure,
                )
                result["robustness_score"] = robustness_score(result)
                rows.append(result)
                detail_rows.append(result.copy())

    summary = summarize_grid(rows)
    detail = pd.DataFrame(detail_rows).sort_values(
        ["robustness_score", "total_return"],
        ascending=False,
    )
    meta = {
        "source": data.source,
        "symbols": len([s for s in symbols if s != cfg.regime.benchmark_symbol]),
        "start_dates": start_dates,
        "initial_capital": initial_capital,
        "cost_bps": cost_bps,
        "min_relative_strengths": min_relative_strengths,
        "throttle_drawdown_pct": throttle_drawdown_pct,
        "halt_drawdown_pct": halt_drawdown_pct,
        "stop_aware": stop_aware,
        "dynamic_trail": dynamic_trail,
        "trail_percent": trail_percent,
        "stop_only_exits": stop_only_exits,
        "variants_filter": variants_filter,
        "n_splits": n_splits,
        "benchmarks": benchmark_symbols,
    }
    return summary, detail, meta


def format_summary(summary: pd.DataFrame, limit: int) -> str:
    cols = [
        "variant",
        "max_entry_rvol",
        "target_position_rvol",
        "min_relative_strength_63",
        "max_positions",
        "rebalance_every",
        "throttle_drawdown_pct",
        "halt_drawdown_pct",
        "stop_aware",
        "dynamic_trail",
        "trail_percent",
        "avg_total_return",
        "worst_total_return",
        "avg_max_drawdown",
        "worst_max_drawdown",
        "positive_windows",
        "windows",
        "avg_excess_vs_spy",
        "worst_excess_vs_spy",
        "outperform_spy_windows",
        "avg_excess_vs_regime_spy",
        "worst_excess_vs_regime_spy",
        "outperform_regime_spy_windows",
        "avg_excess_vs_qqq",
        "worst_excess_vs_qqq",
        "outperform_qqq_windows",
        "avg_excess_vs_regime_qqq",
        "worst_excess_vs_regime_qqq",
        "outperform_regime_qqq_windows",
        "avg_score",
    ]
    available = [col for col in cols if col in summary.columns]
    return summary[available].head(limit).to_string(index=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run rolling-window robustness checks")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--start-dates", default="2021-01-01,2022-01-01,2023-01-01,2024-01-01,2025-01-01")
    parser.add_argument("--max-entry-rvols", default="0.45,0.55,0.65")
    parser.add_argument("--target-position-rvols", default="0.15,0.20,0.25")
    parser.add_argument("--min-relative-strengths", default="0.0")
    parser.add_argument("--max-positions", default="3,5,8")
    parser.add_argument("--rebalance-every", default="3,5,10")
    parser.add_argument("--initial-capital", type=float, default=1000.0)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--throttle-drawdown-pct", type=float, default=None)
    parser.add_argument("--throttle-scale", type=float, default=0.50)
    parser.add_argument("--halt-drawdown-pct", type=float, default=None)
    parser.add_argument("--halt-scale", type=float, default=0.0)
    parser.add_argument("--stop-aware", action="store_true")
    parser.add_argument("--trail-percent", type=float, default=8.0)
    parser.add_argument("--exit-on-signal-loss", action="store_true")
    parser.add_argument("--dynamic-trail", action="store_true")
    parser.add_argument("--dynamic-trail-vol-multiple", type=float, default=3.0)
    parser.add_argument("--min-trail-percent", type=float, default=6.0)
    parser.add_argument("--max-trail-percent", type=float, default=15.0)
    parser.add_argument("--variants", default="", help="comma-separated variant names to evaluate")
    parser.add_argument("--benchmarks", default="SPY,QQQ", help="comma-separated buy-and-hold benchmarks")
    parser.add_argument("--splits", type=int, default=0, help="walk-forward folds per grid cell")
    parser.add_argument("--summary-csv", default="", help="optional path for summary CSV")
    parser.add_argument("--detail-csv", default="", help="optional path for detail CSV")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    cfg = TradingConfig.from_env()
    summary, detail, meta = run_robustness(
        cfg=cfg,
        start_dates=_parse_csv_dates(args.start_dates),
        max_entry_rvols=_parse_csv_floats(args.max_entry_rvols),
        target_position_rvols=_parse_csv_floats(args.target_position_rvols),
        min_relative_strengths=_parse_csv_floats(args.min_relative_strengths),
        max_positions_values=_parse_csv_ints(args.max_positions),
        rebalance_values=_parse_csv_ints(args.rebalance_every),
        initial_capital=args.initial_capital,
        cost_bps=args.cost_bps,
        synthetic=args.synthetic,
        throttle_drawdown_pct=args.throttle_drawdown_pct,
        throttle_scale=args.throttle_scale,
        halt_drawdown_pct=args.halt_drawdown_pct,
        halt_scale=args.halt_scale,
        stop_aware=args.stop_aware,
        trail_percent=args.trail_percent,
        stop_only_exits=not args.exit_on_signal_loss,
        dynamic_trail=args.dynamic_trail,
        dynamic_trail_vol_multiple=args.dynamic_trail_vol_multiple,
        min_trail_percent=args.min_trail_percent,
        max_trail_percent=args.max_trail_percent,
        variants_filter=_parse_csv_strings(args.variants) if args.variants else None,
        n_splits=args.splits,
        benchmarks=_parse_csv_strings(args.benchmarks),
    )

    if args.summary_csv:
        summary.to_csv(args.summary_csv, index=False)
    if args.detail_csv:
        detail.to_csv(args.detail_csv, index=False)

    if args.json:
        print(
            json.dumps(
                {
                    "meta": meta,
                    "summary": summary.to_dict(orient="records"),
                    "detail": detail.to_dict(orient="records"),
                },
                indent=2,
            )
        )
    else:
        print(json.dumps(meta, indent=2))
        print(format_summary(summary, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
