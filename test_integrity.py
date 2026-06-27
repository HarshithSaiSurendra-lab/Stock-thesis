"""
test_integrity.py — Adversarial tests for the backtest engine's honesty.

These don't test "does the code run" — they test "does the code lie." Each test
tries to PROVE a bias protection works by constructing a case where a broken
engine would give an obviously different (better) answer.
"""

import numpy as np
import pandas as pd
from backtest import run_backtest, BacktestConfig, signal_to_weights, _lag_weights


def test_lookahead_guard_costs_performance():
    """
    A perfect-foresight signal (tomorrow's actual return) must NOT produce
    infinite Sharpe through the proper engine, because the engine lags weights
    by one bar. If the guard were broken, this would print an absurd Sharpe.
    """
    rng = np.random.default_rng(1)
    dates = pd.bdate_range("2021-01-01", periods=500)
    syms = [f"S{i}" for i in range(10)]
    prices = pd.DataFrame(
        {s: 100 * np.exp(np.cumsum(rng.normal(0, 0.015, len(dates)))) for s in syms},
        index=dates,
    )

    # "Cheating" signal: literally next-day return, known only in hindsight.
    cheat_signal = prices.pct_change().shift(-1)

    cfg = BacktestConfig(cost_bps=0.0, max_positions=3)
    res = run_backtest(prices, cheat_signal, cfg)
    sharpe = res.stats["sharpe"]

    # With the one-bar lag, the "cheat" signal at t is applied to t+1->t+2 moves,
    # so it should NOT be a money printer. A broken (un-lagged) engine would show
    # an enormous Sharpe (the perfect-foresight return).
    print(f"  perfect-foresight signal Sharpe through guarded engine: {sharpe}")
    assert sharpe < 5.0, "LOOK-AHEAD GUARD BROKEN: foresight signal printed money"
    print("  PASS: look-ahead guard neutralizes a foresight signal")


def test_lag_shifts_by_one():
    """Direct unit test: held weights at t must equal target weights at t-1."""
    idx = pd.bdate_range("2021-01-01", periods=5)
    target = pd.DataFrame(
        {"A": [1.0, 0.0, 0.0, 0.0, 0.0], "B": [0.0, 1.0, 1.0, 1.0, 1.0]}, index=idx
    )
    held = _lag_weights(target)
    assert held.iloc[1]["A"] == 1.0, "weight not lagged correctly"
    assert pd.isna(held.iloc[0]["A"]), "first bar should have no position"
    print("  PASS: weights lag by exactly one bar")


def test_survivorship_no_phantom_positions():
    """
    A symbol that is NaN (not yet listed) must never receive weight on those
    dates. A broken engine might forward-fill or treat NaN as 0 signal and trade
    a stock that didn't exist.
    """
    idx = pd.bdate_range("2021-01-01", periods=200)
    rng = np.random.default_rng(3)
    prices = pd.DataFrame(
        {
            "OLD": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200))),
            "NEW": 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200))),
        },
        index=idx,
    )
    prices.loc[: idx[120], "NEW"] = np.nan  # NEW lists only after day 120

    signal = prices.shift(21) / prices.shift(63) - 1.0
    cfg = BacktestConfig(cost_bps=0.0, max_positions=2)
    res = run_backtest(prices, signal, cfg)

    # held weight in NEW before listing+lag must be zero/NaN
    pre_listing = res.weights.loc[: idx[120], "NEW"].fillna(0.0)
    assert (pre_listing == 0.0).all(), "SURVIVORSHIP LEAK: traded an unlisted name"
    print("  PASS: no positions taken in symbols before they exist")


def test_cost_monotonicity():
    """Higher costs must never improve net return. Sanity on the cost model."""
    rng = np.random.default_rng(5)
    dates = pd.bdate_range("2021-01-01", periods=400)
    syms = [f"S{i}" for i in range(8)]
    prices = pd.DataFrame(
        {s: 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, len(dates)))) for s in syms},
        index=dates,
    )
    signal = prices.shift(10) / prices.shift(40) - 1.0
    rets = []
    for c in (0.0, 10.0, 30.0):
        r = run_backtest(prices, signal, BacktestConfig(cost_bps=c)).stats["annual_return"]
        rets.append(r)
    assert rets[0] >= rets[1] >= rets[2], "cost model non-monotonic"
    print(f"  PASS: net return falls monotonically with cost {rets}")


if __name__ == "__main__":
    print("Running integrity tests (these prove the engine doesn't cheat):\n")
    test_lag_shifts_by_one()
    test_lookahead_guard_costs_performance()
    test_survivorship_no_phantom_positions()
    test_cost_monotonicity()
    print("\nAll integrity tests passed.")
