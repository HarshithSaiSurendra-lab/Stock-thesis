"""Tests for exp-technical-v2 (registered 2026-11-18).

Covers the three Phase 0 fixes:
  1. minimum holding period (backtest._enforce_min_hold),
  2. strong_up-only selection with trend-quality >= 4.0
     (research_runner._v2_active_labels / build_v2_signal),
  3. weekly rebalancing cutting turnover (integration, synthetic data).

Research-only code; nothing here touches the order path.
"""
import pandas as pd

from backtest import BacktestConfig, _enforce_min_hold, run_backtest
from config import TradingConfig
from diagnostics import trade_stats
from research_runner import (
    _v2_active_labels,
    build_signal_variants,
    build_v2_signal,
    evaluate_variants,
    synthetic_history,
)
from trade_signal import composite_signal, trend_quality_score


def _weights_frame():
    idx = pd.bdate_range("2026-01-01", periods=10)
    w = pd.DataFrame(0.0, index=idx, columns=["AAA", "BBB"])
    w.loc[idx[0:2], "AAA"] = 0.5  # held days 0-1, then target drops it
    w.loc[idx[0:6], "BBB"] = 0.5  # held days 0-5, then target drops it
    return idx, w


def test_min_hold_blocks_early_exit():
    idx, w = _weights_frame()
    out = _enforce_min_hold(w, 5)
    # AAA entered day 0, may not exit before day 5.
    assert (out.loc[idx[2:5], "AAA"] == 0.5).all()
    assert out.loc[idx[5], "AAA"] == 0.0
    # BBB held 6 days already, may exit on day 6.
    assert out.loc[idx[6], "BBB"] == 0.0
    # Everything else unchanged.
    assert (out.loc[idx[0:2], "AAA"] == 0.5).all()
    assert (out.loc[idx[0:6], "BBB"] == 0.5).all()


def test_min_hold_default_is_noop():
    idx, w = _weights_frame()
    assert _enforce_min_hold(w, 1).equals(w)
    assert _enforce_min_hold(w, 0).equals(w)


def test_min_hold_reentry_resets_clock():
    idx = pd.bdate_range("2026-01-01", periods=14)
    w = pd.DataFrame(0.0, index=idx, columns=["AAA"])
    w.loc[idx[0:5], "AAA"] = 0.5   # first stint: 5 days, clean exit day 5
    w.loc[idx[7:8], "AAA"] = 0.5   # re-enters day 7, target drops it day 8
    out = _enforce_min_hold(w, 5)
    assert out.loc[idx[5], "AAA"] == 0.0       # first exit allowed
    assert (out.loc[idx[8:12], "AAA"] == 0.5).all()  # second exit blocked to day 12
    assert out.loc[idx[12], "AAA"] == 0.0      # released on day 12


def _feature_frame(sma_20, rvol, kvo):
    return pd.DataFrame(
        {
            "close": [110.0, 110.0, 110.0],
            "sma_20": [sma_20, sma_20, sma_20],
            "sma_50": [100.0, 100.0, 100.0],
            "mom_126_21": [0.05, 0.05, 0.05],
            "obv_slope_20": [1.0, 1.0, 1.0],
            "kvo_hist": [kvo, kvo, kvo],
            "rsi_14": [50.0, 50.0, 50.0],
            "mfi_14": [50.0, 50.0, 50.0],
            "rvol_20": [rvol, rvol, rvol],
        }
    )


def test_v2_active_labels_boundary():
    # strong_up with trend-quality exactly 3.0: excluded (the old
    # threshold). tq = close>sma50 (1) + mom>0 (1) + obv (0.5) + kvo (0.5).
    feats3 = _feature_frame(sma_20=95.0, rvol=0.8, kvo=1.0)
    assert (composite_signal(feats3) == "strong_up").all()
    assert (trend_quality_score(feats3) == 3.0).all()
    assert not _v2_active_labels(feats3).any()

    # strong_up with trend-quality 4.0 (sma_20 > sma_50 adds 1.0): included.
    feats4 = _feature_frame(sma_20=105.0, rvol=0.8, kvo=1.0)
    assert (trend_quality_score(feats4) == 4.0).all()
    assert _v2_active_labels(feats4).all()

    # strong_up with trend-quality 5.0: included.
    feats5 = _feature_frame(sma_20=105.0, rvol=0.5, kvo=1.0)
    assert (trend_quality_score(feats5) == 5.0).all()
    assert _v2_active_labels(feats5).all()

    # mild_up (one confirmation only): never included, whatever its tq.
    mild = _feature_frame(sma_20=105.0, rvol=0.5, kvo=-1.0)
    assert (composite_signal(mild) == "mild_up").all()
    assert not _v2_active_labels(mild).any()


def test_v2_signal_panel_only_strong_high_quality():
    cfg = TradingConfig.from_env()
    data = synthetic_history(("AAA", "BBB", "CCC", "SPY"), years=2)
    prices, panel = build_v2_signal(data, cfg)
    assert "SPY" not in panel.columns
    assert panel.index.equals(prices.index)
    assert set(panel.columns) == {"AAA", "BBB", "CCC"}
    # Property: every nonzero v2 cell is a strong_up with tq >= 4.0 on a
    # regime-allowed day (the panel also carries the v2_regime mask).
    from research_runner import _feature_panels, _regime_mask

    _, features = _feature_panels(data)
    regime = _regime_mask(features["SPY"].reindex(prices.index), cfg)
    for symbol in panel.columns:
        feats = features[symbol].reindex(prices.index)
        active = _v2_active_labels(feats)
        expected = active.fillna(False) & regime.fillna(False).reindex(prices.index)
        assert ((panel[symbol] > 0) == expected).all()
        assert ((panel[symbol] == 0) == (~expected)).all()


def test_weekly_rebalance_and_min_hold_cut_turnover():
    cfg = TradingConfig.from_env()
    data = synthetic_history((*cfg.universe.seed_symbols, "SPY"), years=2)
    prices, variants = build_signal_variants(data, cfg)
    signal = variants["v1_composite"]

    daily = run_backtest(
        prices, signal,
        BacktestConfig(cost_bps=10.0, long_only=True, max_positions=5, rebalance_every=1, min_hold_days=1),
    )
    weekly = run_backtest(
        prices, signal,
        BacktestConfig(cost_bps=10.0, long_only=True, max_positions=5, rebalance_every=5, min_hold_days=5),
    )
    assert float(weekly.turnover.sum()) < float(daily.turnover.sum())
    daily_trades = trade_stats(daily.weights, prices, 10.0)
    weekly_trades = trade_stats(weekly.weights, prices, 10.0)
    assert weekly_trades["median_holding_days"] >= 5
    assert weekly_trades["median_holding_days"] > daily_trades["median_holding_days"]


def test_evaluate_variants_threads_min_hold():
    cfg = TradingConfig.from_env()
    data = synthetic_history(("AAA", "BBB", "CCC", "DDD", "EEE", "SPY"), years=2)
    prices, variants = build_signal_variants(data, cfg)
    base = evaluate_variants(
        prices, {"v2": variants["v1_composite"]}, cost_bps=10.0,
        max_positions=5, rebalance_every=1, n_splits=4,
        initial_capital=1000.0, min_hold_days=1,
    )
    fixed = evaluate_variants(
        prices, {"v2": variants["v1_composite"]}, cost_bps=10.0,
        max_positions=5, rebalance_every=5, n_splits=4,
        initial_capital=1000.0, min_hold_days=5,
    )
    assert len(base) == 1 and len(fixed) == 1
    assert int(fixed.iloc[0]["trades"]) < int(base.iloc[0]["trades"])
