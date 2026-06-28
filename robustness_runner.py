from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from itertools import product

import numpy as np
import pandas as pd

from config import RiskConfig, TradingConfig
from research_runner import (
    build_signal_variants,
    evaluate_variants,
    fetch_alpaca_history,
    format_results,
    slice_bars_window,
    slice_research_window,
    synthetic_history,
)

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


def summarize_grid(rows: list[dict]) -> pd.DataFrame:
    raw = pd.DataFrame(rows)
    grouped = []
    keys = [
        "variant",
        "max_entry_rvol",
        "target_position_rvol",
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
        grouped.append(
            {
                "variant": key[0],
                "max_entry_rvol": key[1],
                "target_position_rvol": key[2],
                "max_positions": key[3],
                "rebalance_every": key[4],
                "throttle_drawdown_pct": key[5],
                "halt_drawdown_pct": key[6],
                "stop_aware": key[7],
                "dynamic_trail": key[8],
                "trail_percent": key[9],
                "stop_only_exits": key[10],
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
    return pd.DataFrame(grouped).sort_values(
        ["avg_score", "avg_total_return", "worst_max_drawdown"],
        ascending=[False, False, False],
    )


def run_robustness(
    cfg: TradingConfig,
    start_dates: list[str],
    max_entry_rvols: list[float],
    target_position_rvols: list[float],
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
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    symbols = tuple(dict.fromkeys((*cfg.universe.seed_symbols, cfg.regime.benchmark_symbol)))
    earliest = min(start_dates)
    if synthetic:
        data = synthetic_history(symbols, years=6)
    else:
        data = fetch_alpaca_history(symbols, cfg, years=6, start_date=earliest)

    rows = []
    detail_rows = []
    for max_entry_rvol, target_position_rvol in product(max_entry_rvols, target_position_rvols):
        local_cfg = replace(
            cfg,
            risk=replace(
                cfg.risk,
                max_entry_rvol=max_entry_rvol,
                target_position_rvol=target_position_rvol,
            ),
        )
        prices, variants = build_signal_variants(data, local_cfg)
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
        "throttle_drawdown_pct": throttle_drawdown_pct,
        "halt_drawdown_pct": halt_drawdown_pct,
        "stop_aware": stop_aware,
        "dynamic_trail": dynamic_trail,
        "trail_percent": trail_percent,
        "stop_only_exits": stop_only_exits,
        "variants_filter": variants_filter,
        "n_splits": n_splits,
    }
    return summary, detail, meta


def format_summary(summary: pd.DataFrame, limit: int) -> str:
    cols = [
        "variant",
        "max_entry_rvol",
        "target_position_rvol",
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
        "avg_score",
    ]
    return summary[cols].head(limit).to_string(index=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run rolling-window robustness checks")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--start-dates", default="2021-01-01,2022-01-01,2023-01-01,2024-01-01,2025-01-01")
    parser.add_argument("--max-entry-rvols", default="0.45,0.55,0.65")
    parser.add_argument("--target-position-rvols", default="0.15,0.20,0.25")
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
