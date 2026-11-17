"""Phase 0 diagnosis driver (research-only, NOT imported by the order path).

Runs on the TRAIN window only (2024-01-02..2026-08-17) — the holdout
(2026-08-18..2026-11-13) is never touched during diagnosis, so the
fix hypotheses below are not tuned on it.

Questions answered, each with a number:
  1. Gross vs net — is the signal bad, or just expensive?
  2. Per-trade expectancy / profit factor / win rate — death by a
     thousand cuts, or a few big losers?
  3. Churn — average holding period, trades/month, total costs paid.
  4. Why v2 == v3 — how often does the trend-quality filter bind?
  5. 1-bar lag cost — what would the SAME signal earn with no lag?
     (LOOKAHEAD: signal shifted forward one bar. Diagnostic only, to
     size the lag cost. Never used as a result.)
  6. Regime filter — does it block good days or bad days?
  7. Per-symbol attribution — which names make/lose the money?
  8. Alpha/beta, information ratio, Deflated Sharpe, t-stat — is there
     anything here distinguishable from zero after costs?

Outputs: experiments/diagnosis/phase0-metrics.json (numbers) and
experiments/diagnosis/phase0-diagnosis.md (interpretation + ranked
fix hypotheses). The .md is written by this script from the numbers;
the analyst's ranked hypotheses are appended after reading it.
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
    _regime_mask,
    build_signal_variants,
    slice_research_window,
)
from run_real_backtest import _load_cached_bars  # noqa: E402
from trade_signal import composite_signal, trend_quality_score  # noqa: E402

REPO = Path(__file__).parent
OUT_DIR = REPO / "experiments" / "diagnosis"
TRAIN_START = "2024-01-02"
TRAIN_END = "2026-08-17"
N_TRIALS = 8  # 4 variants x 2 cost levels tried in exp-technical-only-v1


def _spy_returns(full_prices: pd.DataFrame) -> pd.Series:
    spy = full_prices["SPY"].dropna()
    return spy.pct_change().fillna(0.0)


def main() -> int:
    cfg = TradingConfig.from_env()
    data = _load_cached_bars()
    assert data is not None, "no cached bars; run run_real_backtest.py first"
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, TRAIN_START, TRAIN_END)
    full_prices = pd.DataFrame(
        {s: f["close"] for s, f in data.bars.items()}
    ).sort_index().loc[prices.index]
    spy_rets = _spy_returns(full_prices)

    # --- Q4: why v2 == v3. Count active signals with trend-quality < 3.0.
    _, features = _feature_panels(data)
    tq_below = 0
    active_days = 0
    mild_below = 0
    mild_days = 0
    for symbol in prices.columns:
        feats = features[symbol].reindex(prices.index)
        labels = composite_signal(feats)
        tq = trend_quality_score(feats)
        active = labels.isin(["strong_up", "mild_up"])
        active_days += int(active.sum())
        tq_below += int((active & (tq < cfg.signals.min_trend_quality)).sum())
        mild = labels == "mild_up"
        mild_days += int(mild.sum())
        mild_below += int((mild & (tq < cfg.signals.min_trend_quality)).sum())

    # --- Q6: regime mask on the benchmark.
    regime = _regime_mask(features["SPY"].reindex(prices.index), cfg)

    results: dict = {
        "window": {"train_start": TRAIN_START, "train_end": TRAIN_END, "days": len(prices)},
        "filter_binding": {
            "min_trend_quality": cfg.signals.min_trend_quality,
            "active_signal_days": active_days,
            "active_days_with_tq_below_threshold": tq_below,
            "frac_active_below_threshold": round(tq_below / active_days, 4) if active_days else None,
            "mild_up_days": mild_days,
            "mild_up_days_with_tq_below_threshold": mild_below,
        },
        "regime": {
            "frac_days_allowed": round(float(regime.mean()), 3),
            "frac_days_blocked": round(float((~regime).mean()), 3),
        },
        "variants": {},
    }

    for name, signal in variants.items():
        entry: dict = {}
        runs = {}
        for cost in (0.0, 10.0, 20.0):
            res = run_backtest(
                prices, signal,
                BacktestConfig(cost_bps=cost, long_only=True, max_positions=5, rebalance_every=1),
            )
            runs[cost] = res
        gross_ret = runs[0.0].daily_returns
        net10 = runs[10.0].daily_returns
        net20 = runs[20.0].daily_returns
        entry["total_return_gross_0bps"] = round(float((1 + gross_ret).prod() - 1), 4)
        entry["total_return_net_10bps"] = round(float((1 + net10).prod() - 1), 4)
        entry["total_return_net_20bps"] = round(float((1 + net20).prod() - 1), 4)
        entry["cost_drag_10bps"] = round(entry["total_return_gross_0bps"] - entry["total_return_net_10bps"], 4)
        entry["cost_drag_20bps"] = round(entry["total_return_gross_0bps"] - entry["total_return_net_20bps"], 4)
        entry["sharpe_gross"] = runs[0.0].stats.get("sharpe")
        entry["sharpe_net_10bps"] = runs[10.0].stats.get("sharpe")
        entry["max_drawdown_net_10bps"] = runs[10.0].stats.get("max_drawdown")
        entry.update(deflated_sharpe(net10, N_TRIALS))
        entry.update(alpha_beta(net10, spy_rets.reindex(net10.index)))
        entry.update(information_ratio(net10, spy_rets.reindex(net10.index)))
        entry.update(return_significance(net10))
        entry.update(total_costs(runs[10.0].turnover, 10.0))
        entry["trades"] = trade_stats(runs[10.0].weights, prices, 10.0)
        entry.update(exposure_stats(runs[10.0].weights))
        # per-symbol gross attribution (10bps weights, gross pnl)
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
        # regime split: gross daily return on allowed vs blocked days
        allowed = regime.reindex(gross_ret.index).fillna(False)
        entry["regime_split_gross_daily_mean"] = {
            "allowed_days": round(float(gross_ret[allowed].mean()), 5),
            "blocked_days": round(float(gross_ret[~allowed].mean()), 5),
            "n_allowed": int(allowed.sum()),
            "n_blocked": int((~allowed).sum()),
        }
        # Q5: lag-0 diagnostic (LOOKAHEAD — sizes the lag cost only).
        lag0 = run_backtest(
            prices, signal.shift(-1),
            BacktestConfig(cost_bps=0.0, long_only=True, max_positions=5, rebalance_every=1),
        )
        lag0_ret = float((1 + lag0.daily_returns).prod() - 1)
        entry["lag_diagnostic_lookahead_only"] = {
            "total_return_gross_no_lag": round(lag0_ret, 4),
            "total_return_gross_1bar_lag": entry["total_return_gross_0bps"],
            "lag_cost": round(lag0_ret - entry["total_return_gross_0bps"], 4),
            "warning": "NO-LAG RUN USES FUTURE SIGNALS. Diagnostic only — never a result.",
        }
        results["variants"][name] = entry
        print(f"{name}: gross={entry['total_return_gross_0bps']:+.3f} net10={entry['total_return_net_10bps']:+.3f} "
              f"sharpe_net={entry['sharpe_net_10bps']} dsr={entry.get('deflated_sharpe')} "
              f"alpha={entry.get('alpha_annual')} beta={entry.get('beta')} "
              f"expectancy={entry['trades'].get('expectancy_per_trade_bps')}bps/trade "
              f"avg_hold={entry['trades'].get('avg_holding_days')}d")

    print(f"filter: {tq_below}/{active_days} active days below tq threshold "
          f"({results['filter_binding']['frac_active_below_threshold']}); "
          f"mild below: {mild_below}/{mild_days}")
    print(f"regime: allowed {results['regime']['frac_days_allowed']:.1%} of days")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "phase0-metrics.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {OUT_DIR / 'phase0-metrics.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
