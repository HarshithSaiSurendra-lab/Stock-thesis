from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Optional

import pandas as pd

from config import TradingConfig

log = logging.getLogger("universe")

try:
    from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
    from alpaca.data.timeframe import TimeFrame
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False


def _bars_to_frame(response, symbol: str) -> Optional[pd.DataFrame]:
    if response is None:
        return None
    if hasattr(response, "df"):
        df = response.df
        if isinstance(df.index, pd.MultiIndex):
            if "symbol" in df.index.names:
                try:
                    df = df.xs(symbol, level="symbol")
                except Exception:
                    return None
        return df.sort_index()
    if isinstance(response, pd.DataFrame):
        return response.sort_index()
    if isinstance(response, dict) and symbol in response:
        frame = response[symbol]
        return frame.sort_index() if isinstance(frame, pd.DataFrame) else None
    return None


def _latest_quote(data_client, symbol: str) -> Optional[dict]:
    if data_client is None:
        return None
    try:
        if _ALPACA_AVAILABLE:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quotes = data_client.get_stock_latest_quote(req)
        else:
            quotes = data_client.get_stock_latest_quote(SimpleNamespace(symbol_or_symbols=symbol))
        quote = quotes[symbol]
        bid = float(getattr(quote, "bid_price", None) or quote["bid_price"])
        ask = float(getattr(quote, "ask_price", None) or quote["ask_price"])
        mid = (bid + ask) / 2
        spread = ask - bid
        return {"bid": bid, "ask": ask, "spread": spread, "spread_pct": spread / mid if mid else 1.0}
    except Exception as exc:
        log.debug("latest quote failed for %s: %s", symbol, exc)
        return None


def _candidate_symbols(broker, cfg: TradingConfig) -> list[str]:
    if cfg.universe.universe_source == "alpaca" and getattr(broker.paper, "client", None):
        try:
            assets = broker.paper.client.get_all_assets()
            symbols = [
                asset.symbol
                for asset in assets
                if getattr(asset, "tradable", False)
                and getattr(asset, "asset_class", "us_equity") == "us_equity"
                and getattr(asset, "status", "active") == "active"
            ]
            if symbols:
                return symbols
        except Exception as exc:
            log.warning("asset universe fetch failed, falling back to seed list: %s", exc)
    return list(cfg.universe.seed_symbols)


def _rest_daily_bars(symbol: str, lookback_days: int, cfg: TradingConfig) -> Optional[pd.DataFrame]:
    key = os.getenv("ALPACA_PAPER_KEY") or os.getenv("ALPACA_LIVE_KEY")
    secret = os.getenv("ALPACA_PAPER_SECRET") or os.getenv("ALPACA_LIVE_SECRET")
    if not key or not secret:
        return None
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(int(lookback_days * 2), lookback_days + 30))
    params = {
        "symbols": symbol,
        "timeframe": "1Day",
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "adjustment": cfg.run.market_data_adjustment,
        "feed": cfg.run.market_data_feed,
        "limit": str(max(lookback_days, 100)),
    }
    url = f"https://data.alpaca.markets/v2/stocks/bars?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log.debug("REST bar fetch failed for %s: %s", symbol, exc)
        return None
    rows = payload.get("bars", {}).get(symbol, [])
    if not rows:
        return None
    return pd.DataFrame(
        {
            "open": [bar["o"] for bar in rows],
            "high": [bar["h"] for bar in rows],
            "low": [bar["l"] for bar in rows],
            "close": [bar["c"] for bar in rows],
            "volume": [bar["v"] for bar in rows],
        },
        index=pd.to_datetime([bar["t"] for bar in rows]).tz_convert(None),
    ).sort_index()


def _cache_path(symbol: str, lookback_days: int, cfg: TradingConfig) -> Path:
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in ("-", "_"))
    name = (
        f"{safe_symbol}_{lookback_days}_"
        f"{cfg.run.market_data_feed}_{cfg.run.market_data_adjustment}.csv"
    )
    return Path(cfg.run.bar_cache_dir) / name


def _read_cached_bars(symbol: str, lookback_days: int, cfg: TradingConfig) -> Optional[pd.DataFrame]:
    if not cfg.run.bar_cache_enabled:
        return None
    path = _cache_path(symbol, lookback_days, cfg)
    if not path.exists():
        return None
    age_hours = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 3600
    if age_hours > cfg.run.bar_cache_max_age_hours:
        return None
    try:
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
    except Exception as exc:
        log.debug("bar cache read failed for %s: %s", symbol, exc)
        return None
    needed = {"open", "high", "low", "close", "volume"}
    if frame.empty or not needed.issubset(frame.columns):
        return None
    return frame.sort_index()


def _write_cached_bars(symbol: str, lookback_days: int, cfg: TradingConfig, frame: pd.DataFrame) -> None:
    if not cfg.run.bar_cache_enabled or frame is None or frame.empty:
        return
    path = _cache_path(symbol, lookback_days, cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.tail(max(lookback_days, 100)).to_csv(path)
    except Exception as exc:
        log.debug("bar cache write failed for %s: %s", symbol, exc)


def _daily_bars(data_client, symbol: str, lookback_days: int, cfg: Optional[TradingConfig] = None) -> Optional[pd.DataFrame]:
    cached = _read_cached_bars(symbol, lookback_days, cfg) if cfg is not None else None
    if cached is not None:
        return cached
    if data_client is None:
        frame = _rest_daily_bars(symbol, lookback_days, cfg) if cfg is not None else None
        if frame is not None and cfg is not None:
            _write_cached_bars(symbol, lookback_days, cfg, frame)
        return frame
    try:
        if _ALPACA_AVAILABLE:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, limit=lookback_days)
        else:
            req = SimpleNamespace(symbol_or_symbols=symbol, timeframe="1Day", limit=lookback_days)
        response = data_client.get_stock_bars(req)
        frame = _bars_to_frame(response, symbol)
        if frame is None:
            return None
        cols = {c.lower(): c for c in frame.columns}
        rename = {}
        for wanted in ("open", "high", "low", "close", "volume"):
            if wanted not in frame.columns and wanted in cols:
                rename[cols[wanted]] = wanted
        if rename:
            frame = frame.rename(columns=rename)
        if cfg is not None:
            _write_cached_bars(symbol, lookback_days, cfg, frame)
        return frame
    except Exception as exc:
        log.debug("bar fetch failed for %s: %s", symbol, exc)
        return None


def select_universe(broker, cfg: TradingConfig) -> list[str]:
    """
    Screen a candidate universe by dollar volume, price, and spread.
    """
    candidates = _candidate_symbols(broker, cfg)
    survivors: list[tuple[str, float]] = []
    data_client = getattr(broker.paper, "data", None) or getattr(broker.live, "data", None)

    for symbol in candidates:
        bars = _daily_bars(data_client, symbol, cfg.universe.lookback_days, cfg)
        if bars is None or bars.empty:
            continue
        needed = {"open", "high", "low", "close", "volume"}
        if not needed.issubset(set(bars.columns)):
            continue
        bars = bars.dropna(subset=["close", "volume"])
        if len(bars) < 20:
            continue
        last = bars.iloc[-1]
        price = float(last["close"])
        if price < cfg.universe.min_price:
            continue
        dollar_volume = float((bars["close"] * bars["volume"]).tail(20).mean())
        quote = _latest_quote(data_client, symbol)
        spread_pct = quote["spread_pct"] if quote else None
        if dollar_volume >= cfg.universe.min_dollar_volume and (
            spread_pct is None or spread_pct <= cfg.universe.max_spread_pct
        ):
            survivors.append((symbol, dollar_volume))

    survivors.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _ in survivors[: cfg.universe.max_candidates]]


def fetch_symbol_frame(broker, symbol: str, cfg: TradingConfig) -> Optional[pd.DataFrame]:
    data_client = getattr(broker.paper, "data", None) or getattr(broker.live, "data", None)
    bars = _daily_bars(data_client, symbol, cfg.run.data_lookback_days, cfg)
    if bars is None or bars.empty:
        return None
    return bars
