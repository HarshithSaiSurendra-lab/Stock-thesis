"""B5 — paper-API flakiness resilience.

What B5 fixes (diagnosed 2026-11-25/26):

1. The benchmark fetch was single-shot: one egress-proxy outage made
   the SPY fetch fail and the whole run went regime_blocked with
   "benchmark data unavailable". The fetch now retries transient
   failures (3 attempts, 1s/2s backoff) and still fails closed.
2. The universe screen was serial: ~133 symbols x (bars + quote) =
   ~266 sequential API calls, no per-call timeout on the SDK path.
   One hung call could eat the 60-minute decision timeout. The screen
   now runs per-symbol work in parallel on daemon threads, each symbol
   bounded by a per-symbol timeout, with identical selection/order.

These tests are offline: fake data clients only. No network, no real
Alpaca, no .env/keys. Fixed 2026 dates are used where the freshness
check needs a pinned evaluation time; the select_universe tests use
frames ending today (the same pattern as test_new_modules.py), so they
do not depend on the wall clock beyond the exchange calendar.

Run: .venv/bin/python -m pytest test_b5_data_resilience.py -v
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import universe
from config import TradingConfig
from data_clock import DataClock
from filters import market_regime
from universe import StaleBarsError, fetch_with_retry

# Fixed evaluation time (Thanksgiving week 2026, America/New_York).
# Wed Nov 25 16:15 EST — after the close, expected session is Nov 25.
WED_CLOSE = datetime(2026, 11, 25, 21, 15, tzinfo=timezone.utc)


# ---------------------------------------------------------------- helpers

def _cfg() -> TradingConfig:
    cfg = TradingConfig()
    cfg.run.bar_cache_enabled = False  # never touch the real bar cache
    return cfg


def _synthetic_frame(end: pd.Timestamp, periods: int = 250) -> pd.DataFrame:
    idx = pd.bdate_range(end=end, periods=periods)
    closes = np.linspace(500.0, 700.0, periods) + 0.5 * np.sin(np.arange(periods))
    return pd.DataFrame(
        {
            "open": closes - 0.5,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": 30_000_000.0,
        },
        index=idx,
    )


class _FakeBarResponse:
    def __init__(self, df):
        self.df = df


class _FlakyBarsClient:
    """Raises ConnectionError `fail_times` times, then returns `frame`."""

    def __init__(self, frame, fail_times=0, exc=None):
        self._frame = frame
        self._fail_times = fail_times
        self._exc = exc or ConnectionError("proxy dropped the connection")
        self.calls = 0

    def get_stock_bars(self, req):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return _FakeBarResponse(self._frame)

    def get_stock_latest_quote(self, req):
        return {req.symbol_or_symbols: _quote_obj(100.0, 100.1)}


def _quote_obj(bid, ask, ts="2026-11-25T21:00:00+00:00"):
    return SimpleNamespace(bid_price=bid, ask_price=ask, timestamp=ts)


# ------------------------------------------------------- fetch_with_retry

def test_retry_succeeds_after_transient_failures():
    """ConnectionError twice, then success: 3 attempts, sleeps 1s, 2s."""
    calls = []
    sleeps = []

    def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise ConnectionError("proxy dropped the connection")
        return "ok"

    result, attempts = fetch_with_retry(flaky, sleep_fn=sleeps.append)
    assert (result, attempts) == ("ok", 3)
    assert sleeps == [1.0, 2.0]


def test_retry_exhaustion_raises_last_error():
    """All attempts transient-fail: the last error propagates, 3 attempts."""
    calls = []
    sleeps = []

    def always_down():
        calls.append(1)
        raise ConnectionError("proxy dropped the connection")

    with pytest.raises(ConnectionError):
        fetch_with_retry(always_down, sleep_fn=sleeps.append)
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]  # no sleep after the final attempt


def test_retry_uses_message_classification_for_429():
    """A 429 in the message is transient even on a plain RuntimeError."""
    calls = []
    sleeps = []

    def rate_limited():
        calls.append(1)
        if len(calls) <= 1:
            raise RuntimeError("429 too many requests")
        return "ok"

    result, attempts = fetch_with_retry(rate_limited, sleep_fn=sleeps.append)
    assert (result, attempts) == ("ok", 2)
    assert sleeps == [1.0]


def test_retry_never_retries_explicit_rejection():
    """A 400/validation error is final: 1 attempt, no sleep."""
    calls = []
    sleeps = []

    def bad_request():
        calls.append(1)
        raise ValueError("400 validation failed: bad symbol")

    with pytest.raises(ValueError):
        fetch_with_retry(bad_request, sleep_fn=sleeps.append)
    assert len(calls) == 1
    assert sleeps == []


def test_retry_never_retries_stale_bars():
    """StaleBarsError is data, not a transient failure: 1 attempt."""
    calls = []
    sleeps = []
    stale = StaleBarsError("SPY", universe.check_frame_freshness(
        _synthetic_frame(pd.Timestamp("2026-06-02")), _cfg(), WED_CLOSE))

    def raises_stale():
        calls.append(1)
        raise stale

    with pytest.raises(StaleBarsError):
        fetch_with_retry(raises_stale, sleep_fn=sleeps.append)
    assert len(calls) == 1
    assert sleeps == []


# ------------------------------------------------------------ _daily_bars

def test_benchmark_fetch_retries_then_succeeds():
    """The Nov 25 outage, replayed: SPY fetch fails twice, then works."""
    frame = _synthetic_frame(pd.Timestamp("2026-11-25"))
    client = _FlakyBarsClient(frame, fail_times=2)
    got = universe._daily_bars(client, "SPY", 320, _cfg(), WED_CLOSE)
    assert got is not None and not got.empty
    assert client.calls == 3
    assert got.index[-1].date() == WED_CLOSE.date()


def test_benchmark_fetch_fails_closed_after_retries(monkeypatch):
    """All 3 attempts fail: _daily_bars returns None (fail-closed),
    exactly as before B5 — the caller still reports "benchmark data
    unavailable". The fake sleep records the delay sequence instead
    of waiting 3 real seconds."""
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    frame = _synthetic_frame(pd.Timestamp("2026-11-25"))
    client = _FlakyBarsClient(frame, fail_times=99)
    got = universe._daily_bars(client, "SPY", 320, _cfg(), WED_CLOSE)
    assert got is None
    assert client.calls == 3
    assert sleeps == [1.0, 2.0]


def test_stale_bars_not_retried_by_daily_bars():
    """A stale frame raises StaleBarsError after exactly 1 fetch —
    the retry never fires, because the fetch itself succeeded."""
    stale = _synthetic_frame(pd.Timestamp("2026-06-02"))
    client = _FlakyBarsClient(stale, fail_times=0)
    with pytest.raises(StaleBarsError):
        universe._daily_bars(client, "SPY", 320, _cfg(), WED_CLOSE)
    assert client.calls == 1


# ------------------------------------------- refined brief: decision budget
# Decision step timeout in scripts/run_daily.py: 3600s (decision_dry_run).
DECISION_TIMEOUT_SECONDS = 3600


def test_benchmark_recovery_fits_inside_decision_timeout(monkeypatch):
    """Refined brief (1): a transient SPY failure that recovers must do
    so WITHIN the decision step's time budget. The fake client fails
    twice then succeeds; the retry delays are asserted on the fake
    clock (the recorded sleep calls), not wall time.

    Worst case for this configuration is the sum of the delays before
    the final attempt — 1s + 2s = 3s — and even the sum of ALL
    configured delays (1+2+4 = 7s) is three orders of magnitude under
    the 3600s decision timeout. A recovery that only works by blowing
    the decision timeout does not count; this one cannot."""
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    frame = _synthetic_frame(pd.Timestamp("2026-11-25"))
    client = _FlakyBarsClient(frame, fail_times=2)
    got = universe._daily_bars(client, "SPY", 320, _cfg(), WED_CLOSE)
    assert got is not None and not got.empty
    assert client.calls == 3
    assert sleeps == [1.0, 2.0]
    total_retry_delay = sum(sleeps)
    worst_case_budget = sum(universe.DEFAULT_FETCH_DELAYS[: universe.DEFAULT_FETCH_ATTEMPTS - 1])
    assert total_retry_delay == worst_case_budget == 3.0
    assert total_retry_delay < DECISION_TIMEOUT_SECONDS
    assert sum(universe.DEFAULT_FETCH_DELAYS) < DECISION_TIMEOUT_SECONDS


def test_exhausted_retries_block_clearly_unavailable(monkeypatch):
    """Refined brief (2): after all attempts fail, the benchmark fetch
    returns None and the caller reports "benchmark data unavailable"
    (fail-closed) — no crash, no order, and the reason is
    distinguishable in the log from "benchmark data stale". The
    stale case below proves the distinction: same caller, same
    fixed evaluation time, different reason."""
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    # Case 1: every attempt fails -> "benchmark data unavailable".
    frame = _synthetic_frame(pd.Timestamp("2026-11-25"))
    down_client = _FlakyBarsClient(frame, fail_times=99)
    down_broker = _FakeBroker(down_client)
    result = market_regime(down_broker, _cfg(), WED_CLOSE)
    assert result.ok is False
    assert result.reason == "benchmark data unavailable"
    assert down_client.calls == 3
    assert sleeps == [1.0, 2.0]

    # Case 2: the fetch succeeds but the frame is stale -> "stale".
    stale = _synthetic_frame(pd.Timestamp("2026-06-02"))
    stale_client = _FlakyBarsClient(stale, fail_times=0)
    stale_broker = _FakeBroker(stale_client)
    stale_result = market_regime(stale_broker, _cfg(), WED_CLOSE)
    assert stale_result.ok is False
    assert stale_result.reason == "benchmark data stale"
    assert stale_client.calls == 1  # never retried: the fetch worked

    assert result.reason != stale_result.reason
    # Both carry enough detail for the decision log to tell them apart.
    assert stale_result.details["expected_latest_session"] == "2026-11-25"
    assert stale_result.details["observed_latest_bar"] == "2026-06-02"


# --------------------------------------------------------- select_universe

def _screen_cfg(symbols, workers=4, timeout=60.0):
    cfg = _cfg()
    cfg.universe.seed_symbols = tuple(symbols)
    cfg.universe.universe_source = "seed"
    cfg.universe.max_candidates = 200
    cfg.universe.screen_workers = workers
    cfg.universe.screen_symbol_timeout_seconds = timeout
    return cfg


class _ScreenFakeData:
    """Per-symbol bars + quotes. `sleep` simulates network latency.

    close/volume vary by symbol so dollar volume (and therefore the
    expected survivor order) is deterministic. `behaviour` overrides
    per symbol: 'raise' | 'stale' | 'hang' | 'noquote' | 'lows' |
    'wide' (spread over the cap) | 'cheap' (price below the floor).
    """

    def __init__(self, symbols, sleep=0.0, behaviour=None):
        self.symbols = list(symbols)
        self.sleep = sleep
        self.behaviour = behaviour or {}
        self.bar_calls = []

    def _frame(self, symbol):
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=30)
        i = self.symbols.index(symbol)
        close = 100.0 + i  # distinct price per symbol
        volume = 1_000_000.0 + i * 100_000.0  # distinct dollar volume
        if self.behaviour.get(symbol) == "cheap":
            close = 2.0
        if self.behaviour.get(symbol) == "lows":
            volume = 1_000.0
        return pd.DataFrame(
            {"open": close, "high": close + 0.5, "low": close - 0.5,
             "close": close, "volume": volume},
            index=idx,
        )

    def get_stock_bars(self, req):
        symbol = req.symbol_or_symbols
        self.bar_calls.append(symbol)
        mode = self.behaviour.get(symbol)
        if mode == "raise":
            raise RuntimeError("boom")
        if mode == "hang":
            time.sleep(10.0)
        elif self.sleep:
            time.sleep(self.sleep)
        if mode == "stale":
            return _FakeBarResponse(
                _synthetic_frame(pd.Timestamp("2026-06-02"), periods=30))
        return _FakeBarResponse(self._frame(symbol))

    def get_stock_latest_quote(self, req):
        symbol = req.symbol_or_symbols
        if self.behaviour.get(symbol) == "noquote":
            raise RuntimeError("quote feed down")
        i = self.symbols.index(symbol)
        if self.behaviour.get(symbol) == "wide":
            return {symbol: _quote_obj(100.0, 102.0)}  # ~2% spread
        close = 100.0 + i
        return {symbol: _quote_obj(close - 0.02, close)}


class _FakeLeg:
    def __init__(self, data):
        self.client = object()
        self.data = data


class _FakeBroker:
    def __init__(self, data):
        self.paper = _FakeLeg(data)
        self.live = _FakeLeg(data)
        self.data_clock = DataClock()


def _expected_survivors(symbols, behaviour=None):
    """The serial answer: run the same per-symbol logic one by one."""
    behaviour = behaviour or {}
    cfg = _screen_cfg(symbols)
    data = _ScreenFakeData(symbols, behaviour=behaviour)
    survivors = []
    for idx, symbol in enumerate(symbols):
        res = universe._screen_symbol(data, symbol, cfg)
        if res is not None and res[1] is not None:
            survivors.append((idx, symbol, res[1]))
    survivors.sort(key=lambda item: (-item[2], item[0]))
    return [symbol for _, symbol, _ in survivors]


def test_screen_parallel_matches_serial_and_is_faster():
    """12 symbols, 0.08s per fetch: parallel returns the SAME ordered
    survivor list as the serial per-symbol logic, in well under 60%
    of the serial time."""
    symbols = [f"S{i:02d}" for i in range(12)]
    cfg = _screen_cfg(symbols, workers=4)
    data = _ScreenFakeData(symbols, sleep=0.08)
    broker = _FakeBroker(data)
    serial_estimate = 12 * 0.08  # one fetch per symbol, no parallelism
    # Warm the exchange-calendar cache first: _nyse_schedule is cached
    # by date range, and this test's real-now range differs from the
    # fixed-date ranges earlier tests used. The ~1s cold build is a
    # one-off per process, not per symbol, and does not belong in a
    # parallelism measurement.
    universe.check_frame_freshness(
        _synthetic_frame(pd.Timestamp.today().normalize()), cfg)

    start = time.monotonic()
    got = universe.select_universe(broker, cfg)
    elapsed = time.monotonic() - start

    assert got == _expected_survivors(symbols)
    assert elapsed < 0.6 * serial_estimate
    # The DataClock was fed (single-threaded, candidate order).
    assert broker.data_clock.last_ts() is not None


def test_screen_skips_raise_stale_and_hang():
    """One symbol raises, one is stale, one hangs past the 0.5s
    per-symbol timeout: the screen still returns the other survivors,
    finishes within budget, and no exception escapes."""
    symbols = ["GOOD1", "GOOD2", "RAISE", "STALE", "HANG", "GOOD3"]
    behaviour = {"RAISE": "raise", "STALE": "stale", "HANG": "hang"}
    cfg = _screen_cfg(symbols, workers=4, timeout=0.5)
    data = _ScreenFakeData(symbols, behaviour=behaviour)
    broker = _FakeBroker(data)

    start = time.monotonic()
    got = universe.select_universe(broker, cfg)
    elapsed = time.monotonic() - start

    assert got == ["GOOD3", "GOOD2", "GOOD1"]  # dollar volume desc
    assert elapsed < 3.0  # timeout budget, not the 10s hang
    assert broker.data_clock.last_ts() is not None


def test_screen_quote_failure_keeps_symbol():
    """A quote failure is today's behaviour, not a drop: the symbol
    is still evaluated with spread_pct None."""
    symbols = ["AAA", "NOQ", "BBB"]
    behaviour = {"NOQ": "noquote"}
    cfg = _screen_cfg(symbols, workers=4)
    data = _ScreenFakeData(symbols, behaviour=behaviour)
    broker = _FakeBroker(data)
    got = universe.select_universe(broker, cfg)
    assert got == _expected_survivors(symbols, behaviour)
    assert "NOQ" in got  # dollar volume passes; spread is None, not fatal


def test_screen_drops_cheap_wide_and_illiquid():
    """Price floor, spread cap, and dollar-volume floor still apply
    under the parallel screen."""
    symbols = ["KEEP", "CHEAP", "WIDE", "LOWS"]
    behaviour = {"CHEAP": "cheap", "WIDE": "wide", "LOWS": "lows"}
    cfg = _screen_cfg(symbols, workers=4)
    data = _ScreenFakeData(symbols, behaviour=behaviour)
    broker = _FakeBroker(data)
    assert universe.select_universe(broker, cfg) == ["KEEP"]


def test_parallel_map_timeout_abandons_hung_item():
    """_parallel_map directly: one item hangs 5s, the rest finish —
    the map returns the finished items within the 0.3s timeout."""

    def work(item):
        if item == "hang":
            time.sleep(5.0)
        return item.upper()

    start = time.monotonic()
    got = universe._parallel_map(
        ["a", "hang", "b"], work, workers=2, timeout_seconds=0.3)
    elapsed = time.monotonic() - start
    assert got == {"a": "A", "b": "B"}
    assert elapsed < 2.0


# ----------------------------------------------------------------- config

def test_screen_config_env_overrides(monkeypatch):
    """UNIVERSE_SCREEN_WORKERS / UNIVERSE_SCREEN_SYMBOL_TIMEOUT_SECONDS
    flow through TradingConfig.from_env; defaults are 4 and 60s."""
    assert TradingConfig().universe.screen_workers == 4
    assert TradingConfig().universe.screen_symbol_timeout_seconds == 60.0
    monkeypatch.setenv("UNIVERSE_SCREEN_WORKERS", "2")
    monkeypatch.setenv("UNIVERSE_SCREEN_SYMBOL_TIMEOUT_SECONDS", "15")
    cfg = TradingConfig.from_env()
    assert cfg.universe.screen_workers == 2
    assert cfg.universe.screen_symbol_timeout_seconds == 15.0
