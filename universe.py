from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Optional

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


# ---------------------------------------------------------------- B5: fetch resilience

# Bounded retry budget for transient bar-fetch failures (B5). Three
# attempts with 1s / 2s backoff: enough to ride out the Nov 25-style
# egress-proxy outage, short enough that a genuinely dead feed fails
# closed within seconds instead of eating the decision timeout.
DEFAULT_FETCH_ATTEMPTS = 3
DEFAULT_FETCH_DELAYS = (1.0, 2.0, 4.0)

_TRANSIENT_TOKENS = (
    "timeout", "timed out", "connection", "temporarily", "unavailable",
    "502", "503", "504", "500", "429", "rate limit", "proxy",
    "remote disconnected", "reset by peer", "broken pipe",
)
# An explicit rejection means the broker understood the request and
# said no. Retrying it cannot help and (for a 409 duplicate) would be
# wrong — these are never retried.
_EXPLICIT_REJECTION_TOKENS = (
    "409", "duplicate", "400", "validation", "rejected", "invalid",
    "forbidden", "unauthorized", "not found",
)


def _is_explicit_rejection(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(token in text for token in _EXPLICIT_REJECTION_TOKENS)


def _is_transient_error(exc: BaseException) -> bool:
    """True when a failed call is worth retrying with the same arguments.

    Timeouts and connection errors are transient by type. Everything
    else is judged by its message: 5xx / 429 / proxy / disconnect
    wording is transient; an explicit rejection (400/409/validation)
    or a stale-data error is not — StaleBarsError is data, not a
    failed call, and is never retried (see fetch_with_retry).
    """
    if isinstance(exc, StaleBarsError):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    if _is_explicit_rejection(exc):
        return False
    return any(token in text for token in _TRANSIENT_TOKENS)


def fetch_with_retry(
    fn: Callable,
    max_attempts: int = DEFAULT_FETCH_ATTEMPTS,
    delays: tuple = DEFAULT_FETCH_DELAYS,
    sleep_fn: Optional[Callable[[float], None]] = None,
    is_transient: Optional[Callable[[BaseException], bool]] = None,
) -> tuple:
    """Call ``fn()``, retrying transient failures with bounded backoff.

    Returns ``(result, attempts)`` on success. Raises the last error
    after ``max_attempts`` transient failures, and raises immediately
    (1 attempt, no sleep) for non-transient errors and StaleBarsError.
    Callers that already convert exceptions to None — ``_daily_bars``
    does — therefore keep today's fail-closed behaviour exactly: a
    benchmark fetch that fails on every attempt still surfaces as
    "benchmark data unavailable", never as a crash. ``sleep_fn`` is
    injectable so tests can assert the delay sequence without waiting.
    """
    sleep = sleep_fn or time.sleep
    check = is_transient or _is_transient_error
    attempts = 0
    while True:
        attempts += 1
        try:
            return fn(), attempts
        except StaleBarsError:
            raise  # a stale frame is data, not a transient failure
        except Exception as exc:
            if not check(exc):
                raise
            if attempts >= max_attempts:
                raise
            delay = delays[min(attempts - 1, len(delays) - 1)]
            sleep(delay)


_FAILED = object()  # sentinel: a _parallel_map item whose fn() raised


def _parallel_map(items, fn, workers: int, timeout_seconds: float,
                  poll_interval: float = 0.02) -> dict:
    """Run ``fn(item)`` for each item on daemon worker threads.

    Returns ``{item: result}`` for items whose call returned without
    raising. Items that raised, or whose call was still running
    ``timeout_seconds`` after it started, are absent — the caller
    treats them as skipped, never as fatal. Items must be hashable
    (they key the results dict).

    Why a hand-rolled pool instead of ThreadPoolExecutor: a hung
    network call cannot be killed, only abandoned. ThreadPoolExecutor
    workers are non-daemon and CPython joins them at interpreter exit,
    so one hung Alpaca call would hang the whole daily-run process at
    exit. These workers are daemon threads, so abandoning a hung call
    cannot block process exit. The pool is bounded: at most ``workers``
    threads, and collection stops as soon as every pending item is a
    hung call, or every worker is stuck on a hung call (queued items
    can then never start). Total collection time is therefore bounded
    by the healthy work plus one timeout window — one hung call cannot
    eat the 60-minute decision timeout in scripts/run_daily.py.
    """
    items = list(items)
    if not items:
        return {}
    n_threads = max(1, min(int(workers), len(items)))
    work = queue.Queue()
    for item in items:
        work.put(item)
    results: dict = {}
    done: set = set()
    started: dict = {}
    lock = threading.Lock()

    def _worker() -> None:
        while True:
            try:
                item = work.get(timeout=0.05)
            except queue.Empty:
                return
            with lock:
                started[item] = time.monotonic()
            try:
                value = fn(item)
            except Exception:
                value = _FAILED
            with lock:
                if value is not _FAILED:
                    results[item] = value
                done.add(item)

    threads = [
        threading.Thread(target=_worker, daemon=True)
        for _ in range(n_threads)
    ]
    for thread in threads:
        thread.start()
    t0 = time.monotonic()
    # Last-resort guard against a threading pathology: never wait more
    # than this, even if the stop conditions below somehow never fire.
    absolute_deadline = t0 + timeout_seconds * 10 + 300.0
    while True:
        with lock:
            pending = [item for item in items if item not in done]
            now = time.monotonic()
            expired = [
                item for item in pending
                if item in started and now - started[item] > timeout_seconds
            ]
        if not pending:
            break
        if len(expired) == len(pending):
            break  # every pending item is a hung call; nothing can complete
        if len(expired) >= n_threads:
            break  # every worker is stuck on a hung call; queued items can never start
        if now >= absolute_deadline:
            log.warning("parallel map hit absolute deadline; %d items skipped",
                        len(pending))
            break
        time.sleep(poll_interval)
    return dict(results)


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
        def _do_fetch():
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()

        # B5: one dropped connection no longer kills the fetch — retry
        # transient failures, then fall through to None exactly as
        # before (the caller reports "data unavailable").
        payload_bytes, attempts = fetch_with_retry(_do_fetch)
        if attempts > 1:
            log.info("REST bar fetch for %s succeeded after %d attempts",
                     symbol, attempts)
        payload = json.loads(payload_bytes.decode("utf-8"))
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
            response, attempts = fetch_with_retry(
                lambda: data_client.get_stock_bars(req)
            )
            if attempts > 1:
                log.info("bar fetch for %s succeeded after %d attempts",
                         symbol, attempts)
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


def _screen_symbol(data_client, symbol: str, cfg: TradingConfig):
    """Screen one symbol: bars + price + dollar volume + spread.

    Returns ``(symbol, dollar_volume, quote_timestamp)`` when the
    symbol reached the quote step, where ``dollar_volume`` is None when
    it failed the final liquidity/spread filter (the quote timestamp is
    still returned — the serial code fed the DataClock for every symbol
    that reached the quote step, survivor or not). Returns None when
    the symbol was skipped earlier (no bars, stale bars, too few bars,
    price below the floor). Any unexpected error is also a skip: one
    bad symbol must never abort the whole screen (B5 per-symbol
    isolation — the serial code would have propagated such an error
    and killed the run).
    """
    try:
        bars = _daily_bars(data_client, symbol, cfg.universe.lookback_days, cfg)
    except StaleBarsError:
        # A stale window for one symbol skips that symbol; it must
        # not abort the whole screen.
        return None
    if bars is None or bars.empty:
        return None
    needed = {"open", "high", "low", "close", "volume"}
    if not needed.issubset(set(bars.columns)):
        return None
    try:
        bars = bars.dropna(subset=["close", "volume"])
        if len(bars) < 20:
            return None
        last = bars.iloc[-1]
        price = float(last["close"])
        if price < cfg.universe.min_price:
            return None
        dollar_volume = float((bars["close"] * bars["volume"]).tail(20).mean())
        quote = _latest_quote(data_client, symbol)
        spread_pct = quote["spread_pct"] if quote else None
        quote_ts = quote.get("timestamp") if quote else None
        if dollar_volume >= cfg.universe.min_dollar_volume and (
            spread_pct is None or spread_pct <= cfg.universe.max_spread_pct
        ):
            return (symbol, dollar_volume, quote_ts)
        return (symbol, None, quote_ts)
    except Exception as exc:
        log.debug("screen failed for %s: %s", symbol, exc)
        return None


def select_universe(broker, cfg: TradingConfig) -> list[str]:
    """
    Screen a candidate universe by dollar volume, price, and spread.

    B5: the per-symbol work (bars + quote) runs in parallel on
    ``cfg.universe.screen_workers`` daemon threads, each symbol bounded
    by ``cfg.universe.screen_symbol_timeout_seconds`` — a hung or
    failing symbol is skipped, never aborts the screen. The result is
    IDENTICAL to the serial screen: survivors are collected in
    candidate order, the DataClock is fed (single-threaded, in
    candidate order — DataClock.note is a read-then-write and is not
    thread-safe) for exactly the same symbols as before, and the final
    sort is by dollar volume descending with ties broken by candidate
    order, so a screen over the same data returns the same list as
    the serial code did.
    """
    candidates = _candidate_symbols(broker, cfg)
    data_client = getattr(broker.paper, "data", None) or getattr(broker.live, "data", None)
    indexed = list(enumerate(candidates))

    def _work(entry):
        idx, symbol = entry
        res = _screen_symbol(data_client, symbol, cfg)
        if res is None:
            return None
        sym, dollar_volume, quote_ts = res
        return (idx, sym, dollar_volume, quote_ts)

    results = _parallel_map(
        indexed,
        _work,
        workers=cfg.universe.screen_workers,
        timeout_seconds=cfg.universe.screen_symbol_timeout_seconds,
    )
    clock = getattr(broker, "data_clock", None)
    survivors: list[tuple[int, str, float]] = []
    for idx, symbol in indexed:
        res = results.get((idx, symbol))
        if res is None:
            continue
        _, sym, dollar_volume, quote_ts = res
        if clock is not None and quote_ts:
            # Feed the Guardian's DataClock with the quote's feed
            # timestamp (not the receipt time). Single-threaded here
            # by design: DataClock.note is not thread-safe.
            clock.note(quote_ts)
        if dollar_volume is not None:
            survivors.append((idx, sym, dollar_volume))

    survivors.sort(key=lambda item: (-item[2], item[0]))
    return [symbol for _, symbol, _ in survivors[: cfg.universe.max_candidates]]


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
