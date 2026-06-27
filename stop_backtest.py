from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

import numpy as np
import pandas as pd

from backtest import BacktestConfig, BacktestResult, _drawdown_scale, compute_stats


@dataclass
class StopBacktestConfig:
    cost_bps: float = 5.0
    max_positions: int = 5
    gross_leverage: float = 1.0
    rebalance_every: int = 1
    trail_percent: float = 8.0
    dynamic_trail: bool = False
    dynamic_trail_vol_multiple: float = 3.0
    min_trail_percent: float = 6.0
    max_trail_percent: float = 15.0
    stop_only_exits: bool = True
    score_weighted: bool = True
    throttle_drawdown_pct: float | None = None
    throttle_scale: float = 0.50
    halt_drawdown_pct: float | None = None
    halt_scale: float = 0.0


@dataclass
class StopBacktestResult(BacktestResult):
    stop_events: int = 0
    entry_events: int = 0
    exit_events: int = 0
    stop_log: list[dict] = field(default_factory=list)


def _field_panel(bars: Dict[str, pd.DataFrame], field: str) -> pd.DataFrame:
    return pd.DataFrame(
        {symbol: frame[field] for symbol, frame in bars.items() if field in frame.columns}
    ).sort_index()


def _trail_fraction(value: float) -> float:
    return value / 100.0 if value > 1 else value


def _dynamic_trail_percent(rvol: float, cfg: StopBacktestConfig) -> float:
    if not cfg.dynamic_trail or not np.isfinite(rvol) or rvol <= 0:
        return cfg.trail_percent
    daily_vol = rvol / np.sqrt(252)
    pct = daily_vol * cfg.dynamic_trail_vol_multiple * 100.0
    return float(np.clip(pct, cfg.min_trail_percent, cfg.max_trail_percent))


def _target_weights(signal: pd.DataFrame, cfg: StopBacktestConfig) -> pd.DataFrame:
    weights = pd.DataFrame(0.0, index=signal.index, columns=signal.columns)
    for dt, row in signal.iterrows():
        top = row.dropna()
        top = top[top > 0].nlargest(min(cfg.max_positions, len(top)))
        if top.empty:
            continue
        if cfg.score_weighted and float(top.sum()) > 0:
            alloc = top / float(top.sum())
        else:
            alloc = pd.Series(1.0 / len(top), index=top.index)
        weights.loc[dt, alloc.index] = alloc.values * cfg.gross_leverage

    if cfg.rebalance_every > 1:
        mask = np.zeros(len(weights), dtype=bool)
        mask[:: cfg.rebalance_every] = True
        weights = weights.where(pd.Series(mask, index=weights.index), np.nan).ffill().fillna(0.0)
    return weights.shift(1).fillna(0.0)


def run_trailing_stop_backtest(
    bars: Dict[str, pd.DataFrame],
    signal: pd.DataFrame,
    cfg: StopBacktestConfig = StopBacktestConfig(),
) -> StopBacktestResult:
    """
    Daily OHLC trailing-stop simulation.

    Signals create entries on the next bar. By default, positions are not sold
    just because the signal cools; they exit through the trailing stop. With
    daily candles we do not know the intraday order of high and low, so stops
    use the trailing high known before the day's high is credited.
    """
    opens = _field_panel(bars, "open")
    highs = _field_panel(bars, "high")
    lows = _field_panel(bars, "low")
    closes = _field_panel(bars, "close")
    symbols = [symbol for symbol in signal.columns if symbol in closes.columns]
    if not symbols:
        raise ValueError("no overlap between signal symbols and OHLC bars")

    index = closes.index.intersection(signal.index).sort_values()
    opens = opens.reindex(index)[symbols]
    highs = highs.reindex(index)[symbols]
    lows = lows.reindex(index)[symbols]
    closes = closes.reindex(index)[symbols]
    signal = signal.reindex(index)[symbols]

    targets = _target_weights(signal, cfg)
    rvol = np.log(closes / closes.shift(1)).rolling(20, min_periods=20).std() * np.sqrt(252)
    cost_rate = cfg.cost_bps / 1e4
    scale_cfg = BacktestConfig(
        throttle_drawdown_pct=cfg.throttle_drawdown_pct,
        throttle_scale=cfg.throttle_scale,
        halt_drawdown_pct=cfg.halt_drawdown_pct,
        halt_scale=cfg.halt_scale,
    )

    cash = 1.0
    shares = pd.Series(0.0, index=symbols)
    trail_high = pd.Series(np.nan, index=symbols)
    trail_frac = pd.Series(np.nan, index=symbols)
    equity = 1.0
    peak = 1.0
    equity_rows = []
    return_rows = []
    weight_rows = []
    turnover_rows = []
    stop_log: list[dict] = []
    entry_events = 0
    exit_events = 0

    for dt in index:
        open_px = opens.loc[dt]
        high_px = highs.loc[dt]
        low_px = lows.loc[dt]
        close_px = closes.loc[dt]

        valid_open = open_px.dropna()
        equity_open = cash + float((shares[valid_open.index] * valid_open).sum())
        if equity_open <= 0:
            equity_open = equity
        throttle = _drawdown_scale(equity, peak, scale_cfg)
        desired = targets.loc[dt].fillna(0.0) * throttle
        traded_notional = 0.0

        def trade_to(symbol: str, target_shares: float, price: float) -> None:
            nonlocal cash, traded_notional
            current = float(shares[symbol])
            delta = target_shares - current
            if abs(delta) < 1e-12 or not np.isfinite(price) or price <= 0:
                return
            notional = abs(delta) * price
            cash -= delta * price
            cash -= notional * cost_rate
            traded_notional += notional
            shares[symbol] = target_shares

        if cfg.stop_only_exits:
            open_count = int((shares > 0).sum())
            candidates = desired[desired > 0].sort_values(ascending=False)
            for symbol, weight in candidates.items():
                if open_count >= cfg.max_positions:
                    break
                if shares[symbol] > 0:
                    continue
                price = float(open_px.get(symbol, np.nan))
                if not np.isfinite(price) or price <= 0 or cash <= 0:
                    continue
                target_notional = equity_open * float(weight)
                affordable = max(cash, 0.0) / (1.0 + cost_rate)
                buy_notional = min(target_notional, affordable)
                if buy_notional <= 0:
                    continue
                trade_to(symbol, buy_notional / price, price)
                trail_high[symbol] = price
                trail_pct = _dynamic_trail_percent(float(rvol.loc[dt, symbol]), cfg)
                trail_frac[symbol] = _trail_fraction(trail_pct)
                entry_events += 1
                open_count += 1
        else:
            for symbol in symbols:
                price = float(open_px.get(symbol, np.nan))
                if not np.isfinite(price) or price <= 0:
                    continue
                target_notional = equity_open * float(desired.get(symbol, 0.0))
                trade_to(symbol, target_notional / price, price)
                if shares[symbol] > 0 and not np.isfinite(trail_high[symbol]):
                    trail_high[symbol] = price
                    trail_pct = _dynamic_trail_percent(float(rvol.loc[dt, symbol]), cfg)
                    trail_frac[symbol] = _trail_fraction(trail_pct)
                    entry_events += 1
                if shares[symbol] <= 0:
                    trail_high[symbol] = np.nan
                    trail_frac[symbol] = np.nan

        for symbol in symbols:
            qty = float(shares[symbol])
            if qty <= 0:
                continue
            price_open = float(open_px.get(symbol, np.nan))
            price_low = float(low_px.get(symbol, np.nan))
            price_high = float(high_px.get(symbol, np.nan))
            known_high = trail_high[symbol]
            if not np.isfinite(known_high):
                known_high = price_open
            symbol_trail = trail_frac[symbol]
            if not np.isfinite(symbol_trail):
                symbol_trail = _trail_fraction(cfg.trail_percent)
            stop_price = float(known_high) * (1.0 - float(symbol_trail))
            if np.isfinite(price_low) and price_low <= stop_price:
                trade_to(symbol, 0.0, stop_price)
                trail_high[symbol] = np.nan
                trail_frac[symbol] = np.nan
                exit_events += 1
                stop_log.append(
                    {
                        "date": str(pd.Timestamp(dt).date()),
                        "symbol": symbol,
                        "stop_price": round(stop_price, 4),
                    }
                )
                continue
            if np.isfinite(price_high):
                trail_high[symbol] = max(float(known_high), price_high)

        valid_close = close_px.dropna()
        equity_close = cash + float((shares[valid_close.index] * valid_close).sum())
        daily_return = equity_close / equity - 1.0 if equity > 0 else 0.0
        equity = equity_close
        peak = max(peak, equity)

        held_value = shares * close_px.fillna(0.0)
        weight = held_value / equity if equity > 0 else held_value * 0.0
        equity_rows.append(equity)
        return_rows.append(daily_return)
        weight_rows.append(weight.fillna(0.0))
        turnover_rows.append(traded_notional / equity_open if equity_open > 0 else 0.0)

    equity_curve = pd.Series(equity_rows, index=index)
    returns = pd.Series(return_rows, index=index)
    weights = pd.DataFrame(weight_rows, index=index, columns=symbols).fillna(0.0)
    turnover = pd.Series(turnover_rows, index=index).fillna(0.0)
    stats = compute_stats(returns, turnover)
    stats.update(
        {
            "stop_events": len(stop_log),
            "entry_events": entry_events,
            "exit_events": exit_events,
        }
    )
    return StopBacktestResult(
        equity_curve=equity_curve,
        daily_returns=returns,
        weights=weights,
        turnover=turnover,
        stats=stats,
        stop_events=len(stop_log),
        entry_events=entry_events,
        exit_events=exit_events,
        stop_log=stop_log,
    )
