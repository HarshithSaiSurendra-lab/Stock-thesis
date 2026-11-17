"""Tests for diagnostics.py (Phase 0 metrics). Zero API spend, synthetic data only."""
import numpy as np
import pandas as pd

from diagnostics import (
    alpha_beta,
    deflated_sharpe,
    exposure_stats,
    information_ratio,
    per_symbol_pnl,
    return_significance,
    total_costs,
    trade_stats,
)


def _rng_returns(n=500, mean=0.0005, vol=0.01, seed=3):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mean, vol, n))


def test_deflated_sharpe_bounds_and_null():
    r = _rng_returns()
    out = deflated_sharpe(r, n_trials=8)
    assert 0.0 <= out["deflated_sharpe"] <= 1.0
    assert out["sharpe_null_max"] > 0
    assert out["n_trials"] == 8
    # A strongly positive series should have a high DSR.
    strong = _rng_returns(mean=0.003, vol=0.01)
    assert deflated_sharpe(strong, n_trials=8)["deflated_sharpe"] > 0.9


def test_alpha_beta_perfect_tracking():
    bench = _rng_returns()
    out = alpha_beta(bench, bench)
    assert abs(out["beta"] - 1.0) < 1e-9
    assert abs(out["alpha_annual"]) < 1e-9
    assert abs(out["r_squared"] - 1.0) < 1e-9


def test_information_ratio_zero_for_identical():
    bench = _rng_returns()
    out = information_ratio(bench, bench)
    # No active return and no tracking error: the ratio is undefined.
    assert out["information_ratio"] is None
    assert out["tracking_error_annual"] is None


def test_return_significance_positive_series():
    r = _rng_returns(mean=0.003, vol=0.01)
    out = return_significance(r)
    assert out["t_stat_mean_return"] > 3.0
    assert out["mean_return_annual_ci95"][0] > 0


def test_total_costs_arithmetic():
    turnover = pd.Series([1.0, 0.5, 0.0, 2.0])
    out = total_costs(turnover, 10.0, initial_capital=1000.0)
    assert out["total_cost_frac_of_capital"] == round(3.5 * 0.001, 4)
    assert out["total_cost_usd_on_1000"] == round(3.5 * 0.001 * 1000, 2)


def test_trade_stats_single_winning_trade():
    idx = pd.bdate_range("2024-01-01", periods=5)
    prices = pd.DataFrame({"AAA": [100.0, 101.0, 102.0, 103.0, 104.0]}, index=idx)
    weights = pd.DataFrame({"AAA": [0.0, 1.0, 1.0, 1.0, 0.0]}, index=idx)
    out = trade_stats(weights, prices, cost_bps=0.0)
    assert out["n_trades"] == 1
    assert out["win_rate"] == 1.0
    assert out["avg_holding_days"] == 3.0
    assert out["profit_factor"] is None  # no losing trades
    assert out["expectancy_per_trade"] > 0


def test_trade_stats_costs_turn_winner_into_loser():
    idx = pd.bdate_range("2024-01-01", periods=3)
    # Entry at day-1 close, the 101 -> 102 move is earned while holding.
    prices = pd.DataFrame({"AAA": [100.0, 101.0, 102.0]}, index=idx)
    weights = pd.DataFrame({"AAA": [0.0, 1.0, 0.0]}, index=idx)
    cheap = trade_stats(weights, prices, cost_bps=0.0)
    dear = trade_stats(weights, prices, cost_bps=100.0)
    assert cheap["expectancy_per_trade"] > 0
    assert dear["expectancy_per_trade"] < 0


def test_per_symbol_pnl_and_exposure():
    idx = pd.bdate_range("2024-01-01", periods=4)
    prices = pd.DataFrame(
        {"AAA": [100.0, 102.0, 102.0, 102.0], "BBB": [50.0, 49.0, 49.0, 49.0]},
        index=idx,
    )
    weights = pd.DataFrame({"AAA": [0.5, 0.5, 0.0, 0.0], "BBB": [0.5, 0.5, 0.0, 0.0]}, index=idx)
    pnl = per_symbol_pnl(weights, prices)
    assert pnl.loc["AAA", "gross_pnl_frac"] > 0
    assert pnl.loc["BBB", "gross_pnl_frac"] < 0
    exp = exposure_stats(weights)
    assert exp["mean_gross_exposure"] == 0.5
    assert exp["frac_days_in_cash"] == 0.5
