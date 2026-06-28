from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd

from backtest import BacktestConfig, run_backtest, walk_forward_splits
from config import TradingConfig
from filters import volatility_scale
from indicators import build_feature_frame
from signal import composite_score, composite_signal, trend_quality_score
from stop_backtest import StopBacktestConfig, run_trailing_stop_backtest

log = logging.getLogger("research")

try:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False


@dataclass
class ResearchData:
    bars: Dict[str, pd.DataFrame]
    source: str

    @property
    def symbols(self) -> list[str]:
        return sorted(self.bars)


def _bars_to_frame(response, symbol: str) -> pd.DataFrame:
    df = response.df if hasattr(response, "df") else response
    if isinstance(df.index, pd.MultiIndex) and "symbol" in df.index.names:
        df = df.xs(symbol, level="symbol")
    rename = {col: str(col).lower() for col in df.columns}
    df = df.rename(columns=rename)
    required = ["open", "high", "low", "close", "volume"]
    return df[required].sort_index()


def _parse_date(value: Optional[str]) -> Optional[pd.Timestamp]:
    if value in (None, ""):
        return None
    return pd.Timestamp(value).tz_localize(None)


def fetch_alpaca_history(
    symbols: Iterable[str],
    cfg: TradingConfig,
    years: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> ResearchData:
    key = os.getenv("ALPACA_PAPER_KEY") or os.getenv("ALPACA_LIVE_KEY")
    secret = os.getenv("ALPACA_PAPER_SECRET") or os.getenv("ALPACA_LIVE_SECRET")
    if not key or not secret:
        raise RuntimeError("missing Alpaca API keys in environment")

    requested_start = _parse_date(start_date)
    requested_end = _parse_date(end_date)
    end = (
        requested_end.to_pydatetime().replace(tzinfo=timezone.utc)
        if requested_end is not None
        else datetime.now(timezone.utc)
    )
    if requested_start is not None:
        # Pull warmup history before the simulated paper-account start so
        # indicators like 126-21 momentum are already formed on day one.
        start = (requested_start - pd.Timedelta(days=365)).to_pydatetime().replace(tzinfo=timezone.utc)
    else:
        start = end - timedelta(days=int(years * 365.25) + 30)

    if not _ALPACA_AVAILABLE:
        return fetch_alpaca_rest_history(
            symbols,
            key,
            secret,
            start,
            end,
            cfg.run.market_data_feed,
            cfg.run.market_data_adjustment,
        )

    client = StockHistoricalDataClient(key, secret)
    out: Dict[str, pd.DataFrame] = {}

    for symbol in symbols:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        response = client.get_stock_bars(req)
        frame = _bars_to_frame(response, symbol)
        if not frame.empty:
            out[symbol] = frame

    return ResearchData(out, "alpaca")


def fetch_alpaca_rest_history(
    symbols: Iterable[str],
    key: str,
    secret: str,
    start: datetime,
    end: datetime,
    feed: str = "iex",
    adjustment: str = "raw",
) -> ResearchData:
    """
    Standard-library fallback for historical bars when alpaca-py is unavailable.
    """
    out: Dict[str, pd.DataFrame] = {}
    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }
    base_url = "https://data.alpaca.markets/v2/stocks/bars"

    for symbol in symbols:
        rows = []
        page_token = None
        while True:
            params = {
                "symbols": symbol,
                "timeframe": "1Day",
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
                "adjustment": adjustment,
                "feed": feed,
                "limit": "10000",
            }
            if page_token:
                params["page_token"] = page_token
            url = f"{base_url}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rows.extend(payload.get("bars", {}).get(symbol, []))
            page_token = payload.get("next_page_token")
            if not page_token:
                break

        if rows:
            out[symbol] = pd.DataFrame(
                {
                    "open": [bar["o"] for bar in rows],
                    "high": [bar["h"] for bar in rows],
                    "low": [bar["l"] for bar in rows],
                    "close": [bar["c"] for bar in rows],
                    "volume": [bar["v"] for bar in rows],
                },
                index=pd.to_datetime([bar["t"] for bar in rows]).tz_convert(None),
            ).sort_index()

    return ResearchData(out, f"alpaca-rest-{feed}-{adjustment}")


def synthetic_history(symbols: Iterable[str], years: int = 5, seed: int = 7) -> ResearchData:
    """
    Local smoke-test data. This is not evidence of edge; it proves the research
    harness runs end to end without broker credentials.
    """
    rng = np.random.default_rng(seed)
    periods = int(years * 252)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=periods)
    bars: Dict[str, pd.DataFrame] = {}

    for i, symbol in enumerate(symbols):
        drift = rng.normal(0.00025, 0.00015)
        vol = rng.uniform(0.010, 0.026)
        rets = rng.normal(drift, vol, periods)
        if i % 4 == 0:
            rets = pd.Series(rets).ewm(span=8).mean().to_numpy()
        close = 50 * np.exp(np.cumsum(rets))
        open_ = close * (1 + rng.normal(0, 0.002, periods))
        high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, periods))
        low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, periods))
        volume = rng.integers(800_000, 8_000_000, periods).astype(float)
        bars[symbol] = pd.DataFrame(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
            index=idx,
        )

    if "SPY" not in bars:
        spy_rets = rng.normal(0.00025, 0.011, periods)
        spy_close = 400 * np.exp(np.cumsum(pd.Series(spy_rets).ewm(span=5).mean()))
        bars["SPY"] = pd.DataFrame(
            {
                "open": spy_close,
                "high": spy_close * 1.005,
                "low": spy_close * 0.995,
                "close": spy_close,
                "volume": rng.integers(20_000_000, 90_000_000, periods).astype(float),
            },
            index=idx,
        )

    return ResearchData(bars, "synthetic")


def _feature_panels(data: ResearchData) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    prices = pd.DataFrame({symbol: frame["close"] for symbol, frame in data.bars.items()}).sort_index()
    features = {}
    for symbol, frame in data.bars.items():
        feats = build_feature_frame(frame).assign(close=frame["close"], volume=frame["volume"])
        features[symbol] = feats.reindex(prices.index)
    return prices, features


def _regime_mask(benchmark_features: pd.DataFrame, cfg: TradingConfig) -> pd.Series:
    if not cfg.regime.enabled:
        return pd.Series(True, index=benchmark_features.index)
    mask = pd.Series(True, index=benchmark_features.index)
    if cfg.regime.require_above_sma_50:
        mask &= benchmark_features["close"] > benchmark_features["sma_50"]
    if cfg.regime.require_above_sma_200:
        mask &= benchmark_features["close"] > benchmark_features["sma_200"]
    if cfg.regime.require_sma_20_above_sma_50:
        mask &= benchmark_features["sma_20"] > benchmark_features["sma_50"]
    if cfg.regime.require_positive_momentum:
        mask &= benchmark_features["mom_126_21"] > 0
    mask &= benchmark_features["drawdown_63"] >= -cfg.regime.max_benchmark_drawdown_63
    mask &= benchmark_features["rvol_20"] <= cfg.regime.max_benchmark_rvol_20
    return mask.fillna(False)


def build_signal_variants(data: ResearchData, cfg: TradingConfig) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    prices, features = _feature_panels(data)
    symbols = [symbol for symbol in prices.columns if symbol != cfg.regime.benchmark_symbol]
    base = pd.DataFrame(index=prices.index, columns=symbols, dtype=float)
    quality = pd.DataFrame(index=prices.index, columns=symbols, dtype=float)
    risk_adjusted = pd.DataFrame(index=prices.index, columns=symbols, dtype=float)

    for symbol in symbols:
        feats = features[symbol]
        labels = composite_signal(feats)
        scores = composite_score(feats)
        tq = trend_quality_score(feats)
        active = labels.isin(["strong_up", "mild_up"])
        base[symbol] = scores.where(active, 0.0)
        quality[symbol] = scores.where(active & (tq >= cfg.signals.min_trend_quality), 0.0)
        scales = feats["rvol_20"].map(lambda rvol: volatility_scale(float(rvol), cfg))
        risk_ok = feats["rvol_20"] <= cfg.risk.max_entry_rvol
        risk_adjusted[symbol] = (quality[symbol] * scales).where(risk_ok, 0.0)

    benchmark_symbol = cfg.regime.benchmark_symbol
    if benchmark_symbol in features:
        regime = _regime_mask(features[benchmark_symbol], cfg)
    else:
        regime = pd.Series(False, index=prices.index)

    variants = {
        "v1_composite": base,
        "v2_regime": base.where(regime, 0.0),
        "v3_regime_trend_quality": quality.where(regime, 0.0),
        "v4_regime_quality_risk": risk_adjusted.where(regime, 0.0),
    }
    return prices[symbols], variants


def slice_research_window(
    prices: pd.DataFrame,
    variants: dict[str, pd.DataFrame],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    sliced_prices = prices.copy()
    if start is not None:
        sliced_prices = sliced_prices.loc[sliced_prices.index >= start]
    if end is not None:
        sliced_prices = sliced_prices.loc[sliced_prices.index <= end]
    sliced_variants = {
        name: signal.reindex(prices.index).loc[sliced_prices.index]
        for name, signal in variants.items()
    }
    return sliced_prices, sliced_variants


def slice_bars_window(
    bars: dict[str, pd.DataFrame],
    symbols: Iterable[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, pd.DataFrame]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    out = {}
    for symbol in symbols:
        if symbol not in bars:
            continue
        frame = bars[symbol]
        sliced = frame.copy()
        if start is not None:
            sliced = sliced.loc[sliced.index >= start]
        if end is not None:
            sliced = sliced.loc[sliced.index <= end]
        out[symbol] = sliced
    return out


def _trade_count(weights: pd.DataFrame) -> int:
    held = weights.fillna(0.0)
    entries = (held > 0) & (held.shift(1).fillna(0.0) <= 0)
    return int(entries.sum().sum())


def evaluate_variants(
    prices: pd.DataFrame,
    variants: dict[str, pd.DataFrame],
    cost_bps: float,
    max_positions: int,
    rebalance_every: int,
    n_splits: int,
    initial_capital: float,
    throttle_drawdown_pct: float | None = None,
    throttle_scale: float = 0.50,
    halt_drawdown_pct: float | None = None,
    halt_scale: float = 0.0,
    bars: dict[str, pd.DataFrame] | None = None,
    stop_aware: bool = False,
    trail_percent: float = 8.0,
    stop_only_exits: bool = True,
    dynamic_trail: bool = False,
    dynamic_trail_vol_multiple: float = 3.0,
    min_trail_percent: float = 6.0,
    max_trail_percent: float = 15.0,
) -> pd.DataFrame:
    rows = []
    for name, signal in variants.items():
        if stop_aware:
            if bars is None:
                raise ValueError("stop-aware evaluation requires OHLC bars")
            cfg = StopBacktestConfig(
                cost_bps=cost_bps,
                max_positions=max_positions,
                rebalance_every=rebalance_every,
                trail_percent=trail_percent,
                dynamic_trail=dynamic_trail,
                dynamic_trail_vol_multiple=dynamic_trail_vol_multiple,
                min_trail_percent=min_trail_percent,
                max_trail_percent=max_trail_percent,
                stop_only_exits=stop_only_exits,
                throttle_drawdown_pct=throttle_drawdown_pct,
                throttle_scale=throttle_scale,
                halt_drawdown_pct=halt_drawdown_pct,
                halt_scale=halt_scale,
            )
            result = run_trailing_stop_backtest(bars, signal.reindex_like(prices), cfg)
        else:
            cfg = BacktestConfig(
                cost_bps=cost_bps,
                long_only=True,
                max_positions=max_positions,
                rebalance_every=rebalance_every,
                throttle_drawdown_pct=throttle_drawdown_pct,
                throttle_scale=throttle_scale,
                halt_drawdown_pct=halt_drawdown_pct,
                halt_scale=halt_scale,
            )
            result = run_backtest(prices, signal.reindex_like(prices), cfg)
        ending_multiple = float(result.equity_curve.iloc[-1]) if not result.equity_curve.empty else 1.0
        ending_equity = initial_capital * ending_multiple
        row = {
            "variant": name,
            "start_equity": round(initial_capital, 2),
            "ending_equity": round(ending_equity, 2),
            "pnl": round(ending_equity - initial_capital, 2),
            "total_return": round(ending_multiple - 1.0, 4),
            **result.stats,
            "trades": _trade_count(result.weights),
        }

        fold_sharpes = []
        for _, test_idx in walk_forward_splits(prices.index, n_splits=n_splits):
            if len(test_idx) < 80:
                continue
            if stop_aware:
                fold_bars = {
                    symbol: frame.reindex(test_idx).dropna(how="all")
                    for symbol, frame in (bars or {}).items()
                    if symbol in prices.columns
                }
                fold_result = run_trailing_stop_backtest(
                    fold_bars,
                    signal.reindex_like(prices).loc[test_idx],
                    cfg,
                )
            else:
                fold_result = run_backtest(
                    prices.loc[test_idx],
                    signal.reindex_like(prices).loc[test_idx],
                    cfg,
                )
            if "sharpe" in fold_result.stats:
                fold_sharpes.append(fold_result.stats["sharpe"])
        row["wf_median_sharpe"] = round(float(np.median(fold_sharpes)), 3) if fold_sharpes else None
        row["wf_positive_folds"] = int(sum(s > 0 for s in fold_sharpes))
        row["wf_folds"] = len(fold_sharpes)
        rows.append(row)

    return pd.DataFrame(rows).sort_values(["sharpe", "annual_return"], ascending=False)


def format_results(results: pd.DataFrame) -> str:
    cols = [
        "variant",
        "start_equity",
        "ending_equity",
        "pnl",
        "total_return",
        "annual_return",
        "annual_vol",
        "sharpe",
        "sortino",
        "max_drawdown",
        "avg_daily_turnover",
        "trades",
        "stop_events",
        "wf_median_sharpe",
        "wf_positive_folds",
        "wf_folds",
    ]
    available = [col for col in cols if col in results.columns]
    return results[available].to_string(index=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Research and compare signal variants")
    parser.add_argument("--synthetic", action="store_true", help="use generated data for a local smoke test")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--start-date", default=None, help="paper-account simulation start date, e.g. 2025-01-01")
    parser.add_argument("--end-date", default=None, help="paper-account simulation end date, defaults to latest data")
    parser.add_argument("--initial-capital", type=float, default=1000.0)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--rebalance-every", type=int, default=1)
    parser.add_argument("--throttle-drawdown-pct", type=float, default=None)
    parser.add_argument("--throttle-scale", type=float, default=0.50)
    parser.add_argument("--halt-drawdown-pct", type=float, default=None)
    parser.add_argument("--halt-scale", type=float, default=0.0)
    parser.add_argument("--stop-aware", action="store_true", help="use OHLC trailing-stop simulation")
    parser.add_argument("--trail-percent", type=float, default=None)
    parser.add_argument("--dynamic-trail", action="store_true", help="size trailing stops from recent realized volatility")
    parser.add_argument("--dynamic-trail-vol-multiple", type=float, default=None)
    parser.add_argument("--min-trail-percent", type=float, default=None)
    parser.add_argument("--max-trail-percent", type=float, default=None)
    parser.add_argument("--exit-on-signal-loss", action="store_true")
    parser.add_argument("--splits", type=int, default=4)
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    cfg = TradingConfig.from_env()
    symbols = tuple(dict.fromkeys((*cfg.universe.seed_symbols, cfg.regime.benchmark_symbol)))

    if args.synthetic:
        data = synthetic_history(symbols, years=args.years)
    else:
        data = fetch_alpaca_history(
            symbols,
            cfg,
            years=args.years,
            start_date=args.start_date,
            end_date=args.end_date,
        )

    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, args.start_date, args.end_date)
    bars = slice_bars_window(data.bars, prices.columns, args.start_date, args.end_date)
    if prices.empty:
        raise RuntimeError("no prices available in requested research window")
    results = evaluate_variants(
        prices,
        variants,
        cost_bps=args.cost_bps,
        max_positions=args.max_positions,
        rebalance_every=args.rebalance_every,
        n_splits=args.splits,
        initial_capital=args.initial_capital,
        throttle_drawdown_pct=args.throttle_drawdown_pct,
        throttle_scale=args.throttle_scale,
        halt_drawdown_pct=args.halt_drawdown_pct,
        halt_scale=args.halt_scale,
        bars=bars,
        stop_aware=args.stop_aware,
        trail_percent=args.trail_percent if args.trail_percent is not None else cfg.exits.trail_percent,
        stop_only_exits=not args.exit_on_signal_loss,
        dynamic_trail=args.dynamic_trail or cfg.exits.dynamic_trail_enabled,
        dynamic_trail_vol_multiple=(
            args.dynamic_trail_vol_multiple
            if args.dynamic_trail_vol_multiple is not None
            else cfg.exits.dynamic_trail_vol_multiple
        ),
        min_trail_percent=(
            args.min_trail_percent
            if args.min_trail_percent is not None
            else cfg.exits.min_trail_percent
        ),
        max_trail_percent=(
            args.max_trail_percent
            if args.max_trail_percent is not None
            else cfg.exits.max_trail_percent
        ),
    )

    meta = {
        "source": data.source,
        "symbols": len(prices.columns),
        "start": str(prices.index.min().date()),
        "end": str(prices.index.max().date()),
        "initial_capital": args.initial_capital,
        "cost_bps": args.cost_bps,
        "market_data_feed": cfg.run.market_data_feed,
        "market_data_adjustment": cfg.run.market_data_adjustment,
        "stop_aware": args.stop_aware,
        "trail_percent": args.trail_percent if args.trail_percent is not None else cfg.exits.trail_percent,
        "stop_only_exits": not args.exit_on_signal_loss,
        "dynamic_trail": args.dynamic_trail or cfg.exits.dynamic_trail_enabled,
        "throttle_drawdown_pct": args.throttle_drawdown_pct,
        "halt_drawdown_pct": args.halt_drawdown_pct,
    }
    if args.json:
        print(json.dumps({"meta": meta, "results": results.to_dict(orient="records")}, indent=2))
    else:
        print(json.dumps(meta, indent=2))
        print(format_results(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
