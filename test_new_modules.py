from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import tempfile

import pandas as pd

from backtest import BacktestConfig, run_backtest
from config import TradingConfig
from filters import downside_ok, volatility_scale
from reconcile import reconcile
from research_runner import (
    build_signal_variants,
    evaluate_variants,
    _regime_mask,
    slice_bars_window,
    slice_research_window,
    synthetic_history,
)
from robustness_runner import run_robustness, summarize_grid
from signal import composite_signal, trend_quality_score
from stop_backtest import StopBacktestConfig, run_trailing_stop_backtest
from strategy_runner import (
    StrategyState,
    _decision_rank,
    _position_payload_for_validation,
    _positions_notional,
    _quote_with_spread_pct,
    _strategy_equity,
    _strategy_owned_symbols,
)
from universe import _read_cached_bars, _write_cached_bars, select_universe


def test_signal_prefers_trend_and_volume():
    idx = pd.bdate_range("2024-01-01", periods=5)
    features = pd.DataFrame(
        {
            "close": [10, 11, 12, 13, 14],
            "sma_20": [8, 8, 8, 8, 8],
            "sma_50": [9, 9, 9, 9, 9],
            "mom_126_21": [0.1, 0.2, 0.3, 0.4, 0.5],
            "obv_slope_20": [1, 1, 1, 1, 1],
            "kvo_hist": [1, 1, 1, 1, 1],
            "rsi_14": [50, 50, 50, 50, 50],
            "mfi_14": [50, 50, 50, 50, 50],
            "rvol_20": [0.2, 0.2, 0.2, 0.2, 0.2],
        },
        index=idx,
    )
    sig = composite_signal(features)
    assert sig.iloc[-1] == "strong_up"
    assert trend_quality_score(features).iloc[-1] >= 4.0


def test_downside_filter_and_volatility_scale():
    cfg = TradingConfig.from_env()
    cfg.risk.max_entry_rvol = 0.60
    cfg.risk.target_position_rvol = 0.25
    row = pd.Series({"rvol_20": 0.50})
    ok, reason = downside_ok(row, {"bid": 99.9, "ask": 100.0, "spread": 0.1}, cfg)
    assert ok, reason
    assert volatility_scale(0.50, cfg) == 0.5

    too_hot = pd.Series({"rvol_20": 0.80})
    ok, _ = downside_ok(too_hot, {"bid": 99.9, "ask": 100.0, "spread": 0.1}, cfg)
    assert not ok


def test_universe_filters_by_liquidity(tmp_path=None):
    cfg = TradingConfig.from_env()
    cfg.universe.seed_symbols = ("AAA", "BBB")
    cfg.universe.universe_source = "seed"
    cfg.universe.max_candidates = 10

    class FakeBarResponse:
        def __init__(self, df):
            self.df = df

    class FakeData:
        def get_stock_bars(self, req):
            sym = req.symbol_or_symbols
            idx = pd.bdate_range("2024-01-01", periods=30)
            if sym == "AAA":
                df = pd.DataFrame(
                    {
                        "open": 10,
                        "high": 10.5,
                        "low": 9.5,
                        "close": 10,
                        "volume": 1_000_000,
                    },
                    index=idx,
                )
            else:
                df = pd.DataFrame(
                    {
                        "open": 2,
                        "high": 2.1,
                        "low": 1.9,
                        "close": 2,
                        "volume": 1_000,
                    },
                    index=idx,
                )
            return FakeBarResponse(df)

        def get_stock_latest_quote(self, req):
            sym = req.symbol_or_symbols
            if sym == "AAA":
                return {"AAA": type("Q", (), {"bid_price": 9.98, "ask_price": 10.0})()}
            return {"BBB": type("Q", (), {"bid_price": 1.98, "ask_price": 2.1})()}

    class FakeLeg:
        def __init__(self):
            self.client = object()
            self.data = FakeData()

    class FakeBroker:
        def __init__(self):
            self.paper = FakeLeg()
            self.live = FakeLeg()

    broker = FakeBroker()
    universe = select_universe(broker, cfg)
    assert universe == ["AAA"]


def test_reconcile_detects_mismatch(tmp_path=None):
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    cfg = TradingConfig.from_env()
    cfg.paths.strategy_state_path = str(tmp_path / "strategy_state.json")
    Path(cfg.paths.strategy_state_path).write_text(
        json.dumps({"expected_positions": {"paper": {"AAPL": {"qty": 10}}, "live": {"AAPL": {"qty": 10}}}})
    )

    class FakeLeg:
        def __init__(self, qty):
            self._qty = qty

        def positions(self):
            return {"AAPL": {"qty": self._qty}} if self._qty else {}

    class FakeBroker:
        paper = FakeLeg(10)
        live = FakeLeg(9)

    class FakeMemory:
        pass

    res = reconcile(FakeBroker(), FakeMemory(), cfg)
    assert not res["ok"]
    assert any("live:AAPL" in msg for msg in res["discrepancies"])


def test_strategy_state_preserves_positions_across_days():
    state = StrategyState(
        trading_day="2024-01-01",
        submitted_today=["AAPL"],
        expected_positions={"paper": {"AAPL": {"qty": 1}}, "live": {"AAPL": {"qty": 1}}},
        open_entries={"AAPL": {"qty": 1}},
    )
    state.start_day("2024-01-02")
    assert state.submitted_today == []
    assert state.expected_positions["paper"]["AAPL"]["qty"] == 1
    assert state.open_entries["AAPL"]["qty"] == 1


def test_strategy_equity_caps_to_strategy_capital():
    cfg = TradingConfig.from_env()
    cfg.sizing.strategy_capital = 1_000
    assert _strategy_equity(cfg, 100_000) == 1_000
    assert _strategy_equity(cfg, 500) == 500
    assert _strategy_equity(cfg, None) == 1_000


def test_strategy_notional_counts_only_managed_symbols():
    state = StrategyState(
        open_entries={"AAPL": {"qty": 1}},
        expected_positions={"paper": {"MSFT": {"qty": 1}}, "live": {}},
    )
    owned = _strategy_owned_symbols(state)
    positions = {
        "AAPL": {"notional": 300},
        "MSFT": {"notional": 200},
        "TSLA": {"notional": 5_000},
    }
    assert owned == {"AAPL", "MSFT"}
    assert _positions_notional(positions, owned) == 500
    payload = _position_payload_for_validation(positions, owned)
    assert set(payload) == {"AAPL", "MSFT"}
    assert payload["AAPL"]["notional"] == 300


def test_invalid_quote_is_rejected_before_entry():
    assert _quote_with_spread_pct({"bid": 0.0, "ask": 100.0, "spread": 100.0}) is None
    assert _quote_with_spread_pct({"bid": 101.0, "ask": 100.0, "spread": -1.0}) is None
    quote = _quote_with_spread_pct({"bid": 99.9, "ask": 100.0, "spread": 0.1})
    assert quote is not None
    assert quote["spread_pct"] < 0.01


def test_decision_rank_rewards_stronger_cleaner_setup():
    cfg = TradingConfig.from_env()
    strong = pd.Series(
        {
            "close": 100.0,
            "volume": 2_000_000,
            "mom_126_21": 0.20,
            "rvol_20": 0.20,
        }
    )
    weak = pd.Series(
        {
            "close": 100.0,
            "volume": 80_000,
            "mom_126_21": 0.02,
            "rvol_20": 0.50,
        }
    )
    strong_rank = _decision_rank(
        strong,
        {"bid": 99.99, "ask": 100.0, "spread": 0.01, "spread_pct": 0.0001},
        score=5.0,
        quality=5.0,
        relative_strength=0.08,
        cfg=cfg,
    )
    weak_rank = _decision_rank(
        weak,
        {"bid": 99.5, "ask": 100.0, "spread": 0.5, "spread_pct": 0.005},
        score=4.0,
        quality=3.0,
        relative_strength=-0.02,
        cfg=cfg,
    )
    assert strong_rank["decision_score"] > weak_rank["decision_score"]
    assert strong_rank["components"]["spread"] > weak_rank["components"]["spread"]


def test_bar_cache_round_trips(tmp_path=None):
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    cfg = TradingConfig.from_env()
    cfg.run.bar_cache_dir = str(tmp_path / "bars")
    cfg.run.bar_cache_enabled = True
    idx = pd.bdate_range("2024-01-01", periods=3)
    frame = pd.DataFrame(
        {
            "open": [10, 11, 12],
            "high": [11, 12, 13],
            "low": [9, 10, 11],
            "close": [10.5, 11.5, 12.5],
            "volume": [1000, 1100, 1200],
        },
        index=idx,
    )
    _write_cached_bars("AAA", 320, cfg, frame)
    cached = _read_cached_bars("AAA", 320, cfg)
    assert cached is not None
    assert cached["close"].tolist() == frame["close"].tolist()


def test_research_runner_builds_variants():
    cfg = TradingConfig.from_env()
    cfg.universe.seed_symbols = ("AAA", "BBB", "CCC")
    cfg.regime.benchmark_symbol = "SPY"
    data = synthetic_history((*cfg.universe.seed_symbols, "SPY"), years=2)
    prices, variants = build_signal_variants(data, cfg)
    assert set(variants) == {
        "v1_composite",
        "v2_regime",
        "v3_regime_trend_quality",
        "v4_regime_quality_risk",
    }
    assert list(prices.columns) == ["AAA", "BBB", "CCC"]
    for signal in variants.values():
        assert signal.index.equals(prices.index)
        assert signal.columns.equals(prices.columns)

    results = evaluate_variants(
        prices,
        variants,
        cost_bps=10,
        max_positions=2,
        rebalance_every=5,
        n_splits=2,
        initial_capital=1000,
    )
    assert len(results) == 4
    assert "sharpe" in results.columns
    assert "ending_equity" in results.columns
    assert "pnl" in results.columns


def test_backtest_drawdown_throttle_cuts_exposure():
    idx = pd.bdate_range("2024-01-01", periods=6)
    prices = pd.DataFrame({"AAA": [100, 100, 80, 80, 80, 80]}, index=idx)
    signal = pd.DataFrame({"AAA": [1, 1, 1, 1, 1, 1]}, index=idx)
    throttled = run_backtest(
        prices,
        signal,
        BacktestConfig(
            cost_bps=0,
            max_positions=1,
            throttle_drawdown_pct=0.05,
            throttle_scale=0.5,
        ),
    )
    assert throttled.weights.iloc[3]["AAA"] == 0.5


def test_research_runner_slices_paper_account_window():
    cfg = TradingConfig.from_env()
    cfg.universe.seed_symbols = ("AAA", "BBB")
    cfg.regime.benchmark_symbol = "SPY"
    data = synthetic_history((*cfg.universe.seed_symbols, "SPY"), years=3)
    prices, variants = build_signal_variants(data, cfg)
    start = str(prices.index[-120].date())
    end = str(prices.index[-20].date())
    sliced_prices, sliced_variants = slice_research_window(prices, variants, start, end)
    assert sliced_prices.index.min() >= pd.Timestamp(start)
    assert sliced_prices.index.max() <= pd.Timestamp(end)
    for signal in sliced_variants.values():
        assert signal.index.equals(sliced_prices.index)
    sliced_bars = slice_bars_window(data.bars, sliced_prices.columns, start, end)
    assert set(sliced_bars) == set(sliced_prices.columns)
    assert sliced_bars["AAA"].index.min() >= pd.Timestamp(start)


def test_regime_mask_blocks_market_stress():
    idx = pd.bdate_range("2024-01-01", periods=3)
    features = pd.DataFrame(
        {
            "close": [100, 98, 97],
            "sma_20": [101, 100, 99],
            "sma_50": [100, 100, 100],
            "sma_200": [95, 95, 95],
            "mom_126_21": [0.1, 0.1, 0.1],
            "drawdown_63": [-0.02, -0.08, -0.12],
            "rvol_20": [0.15, 0.20, 0.30],
        },
        index=idx,
    )
    cfg = TradingConfig.from_env()
    cfg.regime.require_above_sma_50 = False
    cfg.regime.require_sma_20_above_sma_50 = False
    cfg.regime.max_benchmark_drawdown_63 = 0.10
    cfg.regime.max_benchmark_rvol_20 = 0.25
    mask = _regime_mask(features, cfg)
    assert mask.tolist() == [True, True, False]


def test_trailing_stop_backtest_holds_until_stop():
    idx = pd.bdate_range("2024-01-01", periods=5)
    bars = {
        "AAA": pd.DataFrame(
            {
                "open": [100, 110, 115, 120, 112],
                "high": [110, 120, 125, 121, 113],
                "low": [99, 109, 114, 108, 100],
                "close": [109, 119, 124, 110, 101],
                "volume": 1_000_000,
            },
            index=idx,
        )
    }
    signal = pd.DataFrame({"AAA": [1, 0, 0, 0, 0]}, index=idx)
    result = run_trailing_stop_backtest(
        bars,
        signal,
        StopBacktestConfig(cost_bps=0, max_positions=1, trail_percent=8.0),
    )
    assert result.entry_events == 1
    assert result.stop_events == 1
    assert result.weights.iloc[2]["AAA"] > 0
    assert result.weights.iloc[-1]["AAA"] == 0


def test_dynamic_trailing_stop_uses_realized_volatility():
    idx = pd.bdate_range("2024-01-01", periods=30)
    close = pd.Series([100 + i * 0.2 for i in range(30)], index=idx)
    bars = {
        "AAA": pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000_000,
            },
            index=idx,
        )
    }
    signal = pd.DataFrame({"AAA": [0] * 24 + [1] * 6}, index=idx)
    result = run_trailing_stop_backtest(
        bars,
        signal,
        StopBacktestConfig(
            cost_bps=0,
            max_positions=1,
            trail_percent=8.0,
            dynamic_trail=True,
            dynamic_trail_vol_multiple=3.0,
            min_trail_percent=6.0,
            max_trail_percent=15.0,
        ),
    )
    assert result.entry_events == 1
    assert result.stop_events == 0


def test_stop_backtest_throttle_trims_existing_positions():
    idx = pd.bdate_range("2024-01-01", periods=6)
    bars = {
        "AAA": pd.DataFrame(
            {
                "open": [100, 100, 80, 80, 80, 80],
                "high": [100, 100, 80, 80, 80, 80],
                "low": [100, 100, 80, 80, 80, 80],
                "close": [100, 100, 80, 80, 80, 80],
                "volume": 1_000_000,
            },
            index=idx,
        )
    }
    signal = pd.DataFrame({"AAA": [1, 1, 1, 1, 1, 1]}, index=idx)
    result = run_trailing_stop_backtest(
        bars,
        signal,
        StopBacktestConfig(
            cost_bps=0,
            max_positions=1,
            trail_percent=50.0,
            throttle_drawdown_pct=0.05,
            throttle_scale=0.5,
        ),
    )
    assert result.weights.iloc[3]["AAA"] <= 0.51


def test_research_runner_stop_aware_mode():
    cfg = TradingConfig.from_env()
    cfg.universe.seed_symbols = ("AAA", "BBB")
    cfg.regime.benchmark_symbol = "SPY"
    data = synthetic_history((*cfg.universe.seed_symbols, "SPY"), years=2)
    prices, variants = build_signal_variants(data, cfg)
    bars = slice_bars_window(data.bars, prices.columns)
    results = evaluate_variants(
        prices,
        variants,
        cost_bps=10,
        max_positions=2,
        rebalance_every=5,
        n_splits=2,
        initial_capital=1000,
        bars=bars,
        stop_aware=True,
        trail_percent=8.0,
    )
    assert len(results) == 4
    assert "stop_events" in results.columns


def test_robustness_runner_summarizes_grid():
    rows = [
        {
            "variant": "v4",
            "max_entry_rvol": 0.55,
            "target_position_rvol": 0.20,
            "min_relative_strength_63": 0.0,
            "max_positions": 5,
            "rebalance_every": 5,
            "throttle_drawdown_pct": 0.05,
            "halt_drawdown_pct": 0.10,
            "stop_aware": True,
            "dynamic_trail": True,
            "trail_percent": 8.0,
            "stop_only_exits": True,
            "total_return": 0.2,
            "max_drawdown": -0.1,
            "robustness_score": 0.1,
        },
        {
            "variant": "v4",
            "max_entry_rvol": 0.55,
            "target_position_rvol": 0.20,
            "min_relative_strength_63": 0.0,
            "max_positions": 5,
            "rebalance_every": 5,
            "throttle_drawdown_pct": 0.05,
            "halt_drawdown_pct": 0.10,
            "stop_aware": True,
            "dynamic_trail": True,
            "trail_percent": 8.0,
            "stop_only_exits": True,
            "total_return": -0.1,
            "max_drawdown": -0.2,
            "robustness_score": -0.1,
        },
    ]
    summary = summarize_grid(rows)
    assert summary.iloc[0]["windows"] == 2
    assert summary.iloc[0]["positive_windows"] == 1


def test_robustness_runner_smoke_synthetic():
    cfg = TradingConfig.from_env()
    cfg.universe.seed_symbols = ("AAA", "BBB", "CCC")
    cfg.regime.benchmark_symbol = "SPY"
    summary, detail, meta = run_robustness(
        cfg=cfg,
        start_dates=["2025-01-01"],
        max_entry_rvols=[0.55],
        target_position_rvols=[0.20],
        min_relative_strengths=[0.0],
        max_positions_values=[2],
        rebalance_values=[5],
        initial_capital=1000,
        cost_bps=10,
        synthetic=True,
        stop_aware=True,
        dynamic_trail=True,
        variants_filter=["v4_regime_quality_risk"],
        benchmarks=["SPY", "QQQ"],
    )
    assert not summary.empty
    assert not detail.empty
    assert meta["source"] == "synthetic"
    assert set(detail["variant"]) == {"v4_regime_quality_risk"}
    assert "excess_vs_spy" in detail.columns
    assert "excess_vs_qqq" in detail.columns
    assert "avg_excess_vs_spy" in summary.columns
