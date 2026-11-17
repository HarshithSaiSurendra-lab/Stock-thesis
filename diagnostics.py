"""Phase 0 diagnosis metrics (research-only, NOT imported by the order path).

Extends the standard backtest stats with the metrics the 2026-11-17
plan + investment thesis calls for:

- gross vs net return (costs separated, so we can tell a bad signal
  from an expensive one)
- per-trade expectancy, profit factor, win rate, avg win / avg loss,
  average holding period (churn diagnosis)
- total costs paid, in dollars and in bps of capital
- Deflated Sharpe ratio (Bailey & Lopez de Prado, 2014): corrects the
  Sharpe ratio for the number of trials run, so testing 4 variants x 2
  costs does not inflate the result
- alpha / beta vs a benchmark, information ratio, t-stat of daily
  returns (is the result distinguishable from zero?)
- per-symbol gross P&L attribution, regime-conditioned returns,
  exposure / time in market

All functions are pure pandas/numpy + stdlib (statistics.NormalDist
for the normal quantile — no scipy dependency).
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

_NORM = NormalDist()


def _round(value: float, ndigits: int = 4):
    if value is None:
        return None
    try:
        if math.isnan(value) or math.isinf(value):
            return None
    except TypeError:
        return value
    return round(float(value), ndigits)


def deflated_sharpe(returns: pd.Series, n_trials: int) -> dict:
    """Bailey & Lopez de Prado Deflated Sharpe Ratio.

    SR_hat is the annualised Sharpe of the observed returns. Under the
    null of no skill, the expected maximum Sharpe across n_trials is
    SR0; the DSR is the probability that the true Sharpe exceeds SR0.
    A DSR below 0.95 means the result is not significant once you
    account for how many variants were tried.
    """
    r = returns.dropna()
    n = len(r)
    if n < 30 or r.std() == 0:
        return {"deflated_sharpe": None, "sharpe": None, "sharpe_null_max": None, "n_trials": n_trials}
    sr = (r.mean() / r.std()) * math.sqrt(252)
    skew = float(r.skew())
    kurt = float(r.kurtosis())  # excess kurtosis
    var_sr = (1 - skew * sr + (kurt - 1) / 4 * sr * sr) / (n - 1)
    var_sr = max(var_sr, 1e-12)
    gamma = 0.5772156649  # Euler-Mascheroni
    z_n = _NORM.inv_cdf(1 - 1 / n_trials)
    z_ne = _NORM.inv_cdf(1 - 1 / (n_trials * math.e))
    sr0 = math.sqrt(var_sr) * ((1 - gamma) * z_n + gamma * z_ne)
    dsr = _NORM.cdf((sr - sr0) / math.sqrt(var_sr))
    return {
        "sharpe": _round(sr, 3),
        "deflated_sharpe": _round(dsr, 3),
        "sharpe_null_max": _round(sr0, 3),
        "n_trials": n_trials,
    }


def alpha_beta(returns: pd.Series, benchmark_returns: pd.Series) -> dict:
    """OLS alpha (annualised) / beta of strategy vs benchmark, same index."""
    aligned = pd.DataFrame({"s": returns, "b": benchmark_returns}).dropna()
    if len(aligned) < 30 or aligned["b"].std() == 0:
        return {"alpha_annual": None, "beta": None, "r_squared": None}
    x = aligned["b"].to_numpy()
    y = aligned["s"].to_numpy()
    beta, alpha_daily = np.polyfit(x, y, 1)
    y_hat = alpha_daily + beta * x
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return {
        "alpha_annual": _round(alpha_daily * 252, 4),
        "beta": _round(beta, 3),
        "r_squared": _round(r2, 3),
    }


def information_ratio(returns: pd.Series, benchmark_returns: pd.Series) -> dict:
    active = (returns - benchmark_returns.reindex(returns.index)).dropna()
    if len(active) < 30 or active.std() == 0:
        return {"information_ratio": None, "tracking_error_annual": None}
    ir = (active.mean() / active.std()) * math.sqrt(252)
    return {
        "information_ratio": _round(ir, 3),
        "tracking_error_annual": _round(active.std() * math.sqrt(252), 4),
    }


def return_significance(returns: pd.Series) -> dict:
    """t-stat and 95% CI for the mean daily return (annualised)."""
    r = returns.dropna()
    n = len(r)
    if n < 30 or r.std() == 0:
        return {"t_stat_mean_return": None, "mean_return_annual_ci95": None}
    se = r.std() / math.sqrt(n)
    t = r.mean() / se
    lo = (r.mean() - 1.96 * se) * 252
    hi = (r.mean() + 1.96 * se) * 252
    return {
        "t_stat_mean_return": _round(t, 2),
        "mean_return_annual_ci95": [_round(lo, 4), _round(hi, 4)],
    }


def trade_stats(weights: pd.DataFrame, prices: pd.DataFrame, cost_bps: float) -> dict:
    """Round-trip trade statistics from held weights and forward returns.

    A trade opens when a symbol's held weight goes 0 -> >0 and closes
    when it returns to 0. P&L per trade = sum of (weight x next-day
    return) over the holding days, minus entry + exit costs (each leg
    pays cost_bps on the traded notional). Weights are equal-weight
    top-5, so a trade's notional is ~0.2 of capital.
    """
    fwd_ret = prices.pct_change(fill_method=None).shift(-1)
    cost_rate = cost_bps / 1e4
    trades = []
    for symbol in weights.columns:
        w = weights[symbol].fillna(0.0)
        in_trade = False
        entry_idx = None
        entry_w = 0.0
        pnl = 0.0
        for i, dt in enumerate(w.index):
            wi = float(w.iloc[i])
            if not in_trade and wi > 0:
                in_trade = True
                entry_idx = i
                entry_w = wi
                pnl = -entry_w * cost_rate  # entry cost
            if in_trade:
                ret = fwd_ret[symbol].iloc[i] if i < len(fwd_ret) else np.nan
                if pd.notna(ret):
                    pnl += float(w.iloc[i]) * float(ret)
                if wi == 0 and i > entry_idx:
                    pnl -= entry_w * cost_rate  # exit cost
                    trades.append({
                        "symbol": symbol,
                        "entry": w.index[entry_idx],
                        "exit": dt,
                        "holding_days": i - entry_idx,
                        "pnl_frac_of_capital": pnl,
                    })
                    in_trade = False
        # open positions at the end of the window are not closed trades
    if not trades:
        return {"n_trades": 0}
    pnls = pd.Series([t["pnl_frac_of_capital"] for t in trades])
    holds = pd.Series([t["holding_days"] for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_wins = float(wins.sum()) if len(wins) else 0.0
    gross_losses = float(-losses.sum()) if len(losses) else 0.0
    return {
        "n_trades": len(trades),
        "win_rate": _round(float((pnls > 0).mean()), 3),
        "expectancy_per_trade": _round(float(pnls.mean()), 5),
        "expectancy_per_trade_bps": _round(float(pnls.mean()) * 1e4, 2),
        "avg_win": _round(float(wins.mean()), 5) if len(wins) else None,
        "avg_loss": _round(float(losses.mean()), 5) if len(losses) else None,
        "profit_factor": _round(gross_wins / gross_losses, 3) if gross_losses > 0 else None,
        "avg_holding_days": _round(float(holds.mean()), 2),
        "median_holding_days": _round(float(holds.median()), 1),
        "max_holding_days": int(holds.max()),
        "trades_per_month": _round(len(trades) / (len(weights) / 21), 2),
    }


def total_costs(turnover: pd.Series, cost_bps: float, initial_capital: float = 1000.0) -> dict:
    """Total transaction costs paid over the run, in $ and as % of capital."""
    cost_frac = float(turnover.sum() * (cost_bps / 1e4))
    return {
        "total_cost_frac_of_capital": _round(cost_frac, 4),
        "total_cost_usd_on_1000": _round(cost_frac * initial_capital, 2),
        "avg_cost_bps_per_day": _round(float(turnover.mean() * cost_bps), 2),
    }


def per_symbol_pnl(weights: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Gross P&L contribution per symbol (before costs), as fraction of capital."""
    fwd_ret = prices.pct_change(fill_method=None).shift(-1)
    contrib = (weights.fillna(0.0) * fwd_ret).sum(axis=0)
    out = pd.DataFrame({
        "gross_pnl_frac": contrib,
        "days_held": (weights.fillna(0.0) > 0).sum(),
        "trades": [
            int(((weights[s].fillna(0.0) > 0) & (weights[s].fillna(0.0).shift(1, fill_value=0.0) == 0)).sum())
            for s in weights.columns
        ],
    })
    return out.sort_values("gross_pnl_frac")


def exposure_stats(weights: pd.DataFrame) -> dict:
    gross = weights.fillna(0.0).abs().sum(axis=1)
    return {
        "mean_gross_exposure": _round(float(gross.mean()), 3),
        "frac_days_fully_invested": _round(float((gross > 0.99).mean()), 3),
        "frac_days_in_cash": _round(float((gross == 0).mean()), 3),
        "mean_positions_held": _round(float((weights.fillna(0.0) > 0).sum(axis=1).mean()), 2),
    }
