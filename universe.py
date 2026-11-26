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
from market_calendar import (
    latest_completed_session,
    missing_sessions,
    session_close,
)

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
        # The feed timestamp is the freshness signal: quote_age_seconds
        # judges the quote by it, and the Guardian's DataClock notes it.
        # A quote without it cannot be verified fresh (see downside_ok).
        ts = getattr(quote, "timestamp", None)
        if ts is None and isinstance(quote, dict):
            ts = quote.get("timestamp") or quote.get("t")
        if hasattr(ts, "isoformat"):
            ts = ts.isoformat()
        return {
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "spread_pct": spread / mid if mid else 1.0,
            "timestamp": ts,
        }
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
    # NOTE: do NOT pass a tight `limit`. Alpaca truncates `limit` from the
    # START of the requested window, so limit=lookback_days once returned
    # SPY bars ending 2026-06-02 (verified 2026-11-25). The span below
    # holds at most ~440 trading days, so limit=1000 never truncates and
    # the caller takes the tail in code.
    params = {
        "symbols": symbol,
        "timeframe": "1Day",
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "adjustment": cfg.run.market_data_adjustment,
        "feed": cfg.run.market_data_feed,
        "limit": "1000",
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


@dataclass
class FreshnessResult:
    """Outcome of judging a bar frame against the exchange calendar."""

    fresh: bool
    expected_session: date  # latest completed NYSE session
    expected_close: datetime  # that session's market-close timestamp
    observed_latest_bar: Optional[date]  # latest COMPLETE bar on/before it
    missing_sessions: list  # trading days in (observed, expected]
    reason: str


class StaleBarsError(Exception):
    """A bar fetch returned a frame, but its latest complete bar is older
    than the latest completed trading session.

    Raised (not None) deliberately: the caller must be able to report
    "benchmark data stale" instead of "benchmark data unavailable" —
    the distinction matters when diagnosing a blocked run. Carries the
    expected / observed dates.
    """

    def __init__(self, symbol: str, result: FreshnessResult):
        self.symbol = symbol
        self.result = result
        super().__init__(f"{symbol}: {result.reason}")

    @property
    def details(self) -> dict:
        r = self.result
        return {
            "expected_latest_session": r.expected_session.isoformat(),
            "expected_session_close": r.expected_close.isoformat(),
            "observed_latest_bar": (
                r.observed_latest_bar.isoformat() if r.observed_latest_bar else None
            ),
            "missing_sessions": [d.isoformat() for d in r.missing_sessions],
        }


def _bar_date(ts) -> date:
    """Session date of a bar timestamp (bars are stamped midnight ET)."""
    stamp = pd.Timestamp(ts)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("America/New_York")
    return stamp.date()


def _latest_complete_bar(frame: pd.DataFrame, expected: date) -> Optional[date]:
    """Latest bar date that is both complete (all OHLCV present) and on or
    before the expected session.

    A partial bar for an incomplete session — today's bar fetched at
    10:00, or a row with a NaN field — does not count as that session's
    data.
    """
    needed = ["open", "high", "low", "close", "volume"]
    if frame is None or frame.empty or not set(needed).issubset(frame.columns):
        return None
    complete = frame.dropna(subset=needed)
    if complete.empty:
        return None
    eligible = [_bar_date(ts) for ts in complete.index if _bar_date(ts) <= expected]
    return max(eligible) if eligible else None


def check_frame_freshness(
    frame: pd.DataFrame,
    cfg: Optional[TradingConfig] = None,
    now: Optional[datetime] = None,
) -> FreshnessResult:
    """Judge a bar frame against the latest completed trading session.

    Fresh means the latest complete bar IS the expected session — not
    merely recent. A frame ending a session early is stale even if it
    is only a day old (a missing session), and a frame ending months
    ago is stale even though its bars are internally consistent.
    """
    tz = cfg.run.timezone if cfg is not None else "America/New_York"
    expected = latest_completed_session(now, tz)
    close = session_close(expected, tz)
    observed = _latest_complete_bar(frame, expected)
    if observed is None:
        return FreshnessResult(
            False, expected, close, None, [],
            "no complete bar on or before the expected session",
        )
    if observed < expected:
        return FreshnessResult(
            False, expected, close, observed,
            missing_sessions(observed, expected),
            f"latest complete bar {observed.isoformat()} is before "
            f"the expected session {expected.isoformat()}",
        )
    return FreshnessResult(True, expected, close, observed, [], "ok")


def _daily_bars(data_client, symbol: str, lookback_days: int, cfg: Optional[TradingConfig] = None, now: Optional[datetime] = None) -> Optional[pd.DataFrame]:
    cached = _read_cached_bars(symbol, lookback_days, cfg) if cfg is not None else None
    if cached is not None:
        res = check_frame_freshness(cached, cfg, now)
        if res.fresh:
            return cached
        # A recently written cache can still hold an old window (the
        # SPY cache written 2026-11-16 ended 2026-05-27). Never serve
        # it — refetch instead.
        log.debug("cached bars for %s are stale (%s); refetching",
                  symbol, res.reason)
    frame = None
    if data_client is None:
        frame = _rest_daily_bars(symbol, lookback_days, cfg) if cfg is not None else None
    else:
        try:
            if _ALPACA_AVAILABLE:
                # IEX free plan: an explicit `end` at/near now is treated as a
                # recent-SIP query and rejected ("subscription does not permit
                # querying recent SIP data"), and a request with no start at all
                # returns only the latest bar. Passing a start date with no end
                # returns the full window. Do NOT pass a tight `limit` here:
                # Alpaca truncates `limit` from the START of the window, so
                # limit=lookback_days once returned SPY bars ending
                # 2026-06-02 (verified 2026-11-25). Request wide and take
                # the tail in code instead.
                start = datetime.now(timezone.utc) - timedelta(
                    days=max(int(lookback_days * 2), lookback_days + 30)
                )
                req = StockBarsRequest(
                    symbol_or_symbols=symbol,
                    timeframe=TimeFrame.Day,
                    start=start,
                    limit=1000,
                )
            else:
                req = SimpleNamespace(symbol_or_symbols=symbol, timeframe="1Day", limit=1000)
            response = data_client.get_stock_bars(req)
            frame = _bars_to_frame(response, symbol)
            if frame is not None:
                cols = {c.lower(): c for c in frame.columns}
                rename = {}
                for wanted in ("open", "high", "low", "close", "volume"):
                    if wanted not in frame.columns and wanted in cols:
                        rename[cols[wanted]] = wanted
                if rename:
                    frame = frame.rename(columns=rename)
        except Exception as exc:
            log.debug("bar fetch failed for %s: %s", symbol, exc)
            frame = None
    if frame is None or frame.empty:
        return None
    frame = frame.sort_index().tail(lookback_days)
    res = check_frame_freshness(frame, cfg, now)
    if not res.fresh:
        # A stale window is worse than no data: it would silently drive
        # decisions on old prices. Raise (do not return None) so the
        # caller reports "benchmark data stale" instead of "benchmark
        # data unavailable" — the distinction matters when diagnosing a
        # blocked run.
        raise StaleBarsError(symbol, res)
    if cfg is not None:
        _write_cached_bars(symbol, lookback_days, cfg, frame)
    return frame


def select_universe(broker, cfg: TradingConfig) -> list[str]:
    """
    Screen a candidate universe by dollar volume, price, and spread.
    """
    candidates = _candidate_symbols(broker, cfg)
    survivors: list[tuple[str, float]] = []
    data_client = getattr(broker.paper, "data", None) or getattr(broker.live, "data", None)

    for symbol in candidates:
        try:
            bars = _daily_bars(data_client, symbol, cfg.universe.lookback_days, cfg)
        except StaleBarsError:
            # A stale window for one symbol skips that symbol; it must
            # not abort the whole screen.
            continue
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
        clock = getattr(broker, "data_clock", None)
        if clock is not None and quote:
            # Feed the Guardian's DataClock with the quote's feed
            # timestamp (not the receipt time).
            clock.note(quote.get("timestamp"))
        spread_pct = quote["spread_pct"] if quote else None
        if dollar_volume >= cfg.universe.min_dollar_volume and (
            spread_pct is None or spread_pct <= cfg.universe.max_spread_pct
        ):
            survivors.append((symbol, dollar_volume))

    survivors.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _ in survivors[: cfg.universe.max_candidates]]


def fetch_symbol_frame(broker, symbol: str, cfg: TradingConfig, now: Optional[datetime] = None) -> Optional[pd.DataFrame]:
    """Daily bars for one symbol, ending on the latest completed session.

    Returns None when the fetch fails outright (API error — the caller
    reports "data unavailable"). Raises StaleBarsError when the fetch
    returns a frame whose latest complete bar is older than the latest
    completed session — the caller must report "data stale" instead, so
    a blocked run can be diagnosed from its log.
    """
    data_client = getattr(broker.paper, "data", None) or getattr(broker.live, "data", None)
    bars = _daily_bars(data_client, symbol, cfg.run.data_lookback_days, cfg, now)
    if bars is None or bars.empty:
        return None
    return bars
