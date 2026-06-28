from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import TradingConfig
from indicators import build_feature_frame
from universe import fetch_symbol_frame


@dataclass
class RegimeResult:
    ok: bool
    symbol: str
    reason: str
    details: dict


def market_regime(broker, cfg: TradingConfig) -> RegimeResult:
    """
    Broad-market gate for a long-only strategy.
    If the benchmark trend is unhealthy or cannot be evaluated, new longs stop.
    """
    symbol = cfg.regime.benchmark_symbol
    if not cfg.regime.enabled:
        return RegimeResult(True, symbol, "disabled", {})

    frame = fetch_symbol_frame(broker, symbol, cfg)
    if frame is None or frame.empty:
        return RegimeResult(False, symbol, "benchmark data unavailable", {})

    features = build_feature_frame(frame).assign(close=frame["close"])
    latest = features.dropna().iloc[-1] if not features.dropna().empty else None
    if latest is None:
        return RegimeResult(False, symbol, "benchmark indicators unavailable", {})

    checks = {
        "above_sma_50": bool(latest["close"] > latest["sma_50"]),
        "above_sma_200": bool(latest["close"] > latest["sma_200"]),
        "sma_20_above_sma_50": bool(latest["sma_20"] > latest["sma_50"]),
        "positive_momentum": bool(latest["mom_126_21"] > 0),
        "benchmark_drawdown_63": float(latest["drawdown_63"]),
        "benchmark_rvol_20": float(latest["rvol_20"]),
        "drawdown_ok": bool(latest["drawdown_63"] >= -cfg.regime.max_benchmark_drawdown_63),
        "volatility_ok": bool(latest["rvol_20"] <= cfg.regime.max_benchmark_rvol_20),
    }
    failures = []
    if cfg.regime.require_above_sma_50 and not checks["above_sma_50"]:
        failures.append("benchmark below 50-day average")
    if cfg.regime.require_above_sma_200 and not checks["above_sma_200"]:
        failures.append("benchmark below 200-day average")
    if cfg.regime.require_sma_20_above_sma_50 and not checks["sma_20_above_sma_50"]:
        failures.append("short trend below medium trend")
    if cfg.regime.require_positive_momentum and not checks["positive_momentum"]:
        failures.append("benchmark momentum negative")
    if not checks["drawdown_ok"]:
        failures.append(
            f"benchmark 63-day drawdown {checks['benchmark_drawdown_63']:.1%} "
            f"below cap -{cfg.regime.max_benchmark_drawdown_63:.1%}"
        )
    if not checks["volatility_ok"]:
        failures.append(
            f"benchmark realized volatility {checks['benchmark_rvol_20']:.1%} "
            f"above cap {cfg.regime.max_benchmark_rvol_20:.1%}"
        )

    return RegimeResult(
        ok=not failures,
        symbol=symbol,
        reason="ok" if not failures else "; ".join(failures),
        details=checks,
    )


def volatility_scale(rvol: float, cfg: TradingConfig) -> float:
    """
    Scale position notional down when realized volatility is above target.
    """
    if pd.isna(rvol) or rvol <= 0:
        return cfg.risk.min_vol_scale
    raw = cfg.risk.target_position_rvol / rvol
    return max(cfg.risk.min_vol_scale, min(cfg.risk.max_vol_scale, raw))


def downside_ok(row: pd.Series, quote: dict | None, cfg: TradingConfig) -> tuple[bool, str]:
    rvol = float(row.get("rvol_20", float("nan")))
    if pd.isna(rvol):
        return False, "realized volatility unavailable"
    if rvol > cfg.risk.max_entry_rvol:
        return False, f"realized volatility {rvol:.1%} above cap {cfg.risk.max_entry_rvol:.1%}"

    if quote is not None:
        mid = (quote.get("bid", 0.0) + quote.get("ask", 0.0)) / 2
        spread_pct = quote.get("spread_pct")
        if spread_pct is None and mid > 0:
            spread_pct = quote.get("spread", 0.0) / mid
        if spread_pct is not None and spread_pct > cfg.risk.max_quote_spread_pct:
            return False, f"spread {spread_pct:.2%} above cap {cfg.risk.max_quote_spread_pct:.2%}"

    return True, "ok"
