"""Regression tests for the benchmark (SPY) fetch behind the regime gate.

Background (diagnosed 2026-11-25): the 2026-11-25 daily dry run came back
``regime_blocked`` with reason "benchmark data unavailable". Diagnosis:

1. API error (the Nov 25 failure itself). The SPY bar cache
   (``.cache/bars/SPY_320_iex_raw.csv``) was written 2026-11-16 and had
   expired under the 18h TTL, so a fresh fetch was required. During the
   run the sandbox egress proxy was dropping connections
   (``ProxyError`` / ``RemoteDisconnected`` on paper-api.alpaca.markets),
   the fresh fetch returned None, and ``market_regime`` correctly failed
   closed: no orders, decision log written. The response was right; the
   failure was invisible because the fetch logs at DEBUG level only.
2. Stale window (found while diagnosing #1, worse than #1). Alpaca's
   bars API truncates ``limit`` from the START of the requested window,
   so a "successful" fetch with limit=320 returned SPY bars ending
   2026-06-02 — six months stale — on both the SDK and REST paths
   (reproduced 2026-11-25: 320 rows, 2025-02-24..2026-06-02). Had the
   Nov 25 fetch succeeded, the regime check would have computed
   SMA-50/200, momentum and drawdown on June data and could have
   returned ok=True. The fix: request wide (limit=1000) and take the
   tail in code, reject any frame whose latest bar is older than
   ``cfg.run.max_bar_age_days`` (7 days, covers holiday weekends), in
   ``_daily_bars`` and, as defense in depth, in ``market_regime``.

These tests are offline: synthetic frames and mocked clients only.
No API calls, no broker, no real data dirs.

Run: .venv/bin/python -m pytest test_regime_benchmark.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import filters
import universe
from config import TradingConfig


# ---------------------------------------------------------------- helpers

def _synthetic_frame(end: pd.Timestamp, periods: int = 250) -> pd.DataFrame:
    """Rising OHLCV frame ending on `end` (business days).

    A steadily rising close clears every regime gate (above SMA-50/200,
    SMA-20 above SMA-50, positive momentum, ~zero drawdown, tiny
    realized vol), so a fresh frame must evaluate to ok=True.
    """
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


def _cfg() -> TradingConfig:
    cfg = TradingConfig()
    cfg.run.bar_cache_enabled = False  # never touch the real bar cache
    return cfg


class _FakeBarsClient:
    """Stands in for the Alpaca data client; returns a canned frame."""

    def __init__(self, frame: pd.DataFrame):
        self._frame = frame
        self.requests = []

    def get_stock_bars(self, req):
        self.requests.append(req)
        return SimpleNamespace(df=self._frame)


def _today() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc).date())


# ------------------------------------------------------- market_regime

def test_fetch_none_blocks_trading(monkeypatch):
    """Reproduces the 2026-11-25 failure: fetch returns None (API error).

    Expected response: fail closed — no longs, reason "benchmark data
    unavailable", so the decision log records regime_blocked, not a
    market verdict.
    """
    monkeypatch.setattr(filters, "fetch_symbol_frame", lambda broker, symbol, cfg: None)
    result = filters.market_regime(broker=None, cfg=_cfg())
    assert result.ok is False
    assert result.reason == "benchmark data unavailable"
    assert result.symbol == "SPY"


def test_stale_benchmark_frame_blocks_trading(monkeypatch):
    """A fetch that "succeeds" with a months-old window must also block.

    This is the stale-window bug: SPY bars ending 2026-06-02 would
    otherwise be evaluated as if they were current.
    """
    stale = _synthetic_frame(end=_today() - timedelta(days=180))
    monkeypatch.setattr(filters, "fetch_symbol_frame", lambda broker, symbol, cfg: stale)
    result = filters.market_regime(broker=None, cfg=_cfg())
    assert result.ok is False
    assert result.reason == "benchmark data stale"
    assert result.details["age_days"] >= 180
    assert result.details["last_bar"] == str(stale.index.max().date())


def test_fresh_benchmark_frame_is_evaluated(monkeypatch):
    """A fresh frame must reach the real verdict, not a data reason."""
    fresh = _synthetic_frame(end=_today())
    monkeypatch.setattr(filters, "fetch_symbol_frame", lambda broker, symbol, cfg: fresh)
    result = filters.market_regime(broker=None, cfg=_cfg())
    assert result.ok is True
    assert result.reason == "ok"
    assert result.details["above_sma_50"] is True
    assert result.details["above_sma_200"] is True


# ---------------------------------------------------------- _daily_bars

def test_daily_bars_rejects_stale_window():
    """_daily_bars must return None for a months-old window, not serve it."""
    stale = _synthetic_frame(end=_today() - timedelta(days=180), periods=320)
    got = universe._daily_bars(_FakeBarsClient(stale), "SPY", 320, _cfg())
    assert got is None


def test_daily_bars_returns_latest_window():
    """The limit fix: a full-span response must come back ending today.

    The API hands back the earliest N bars from the requested start, so
    the code requests wide (limit=1000) and takes the tail. A 440-bar
    response must yield the latest 320 bars, ending today.
    """
    wide = _synthetic_frame(end=_today(), periods=440)
    got = universe._daily_bars(_FakeBarsClient(wide), "SPY", 320, _cfg())
    assert got is not None
    assert len(got) == 320
    assert got.index.max().date() == _today().date()


def test_daily_bars_refetches_when_cache_is_stale(tmp_path, monkeypatch):
    """A recently written cache holding an old window must not be served."""
    cfg = _cfg()
    cfg.run.bar_cache_enabled = True
    cfg.run.bar_cache_dir = str(tmp_path)
    stale = _synthetic_frame(end=_today() - timedelta(days=180), periods=320)
    universe._write_cached_bars("SPY", 320, cfg, stale)
    fresh = _synthetic_frame(end=_today(), periods=320)
    got = universe._daily_bars(_FakeBarsClient(fresh), "SPY", 320, cfg)
    assert got is not None
    assert got.index.max().date() == _today().date()


def test_rest_bars_does_not_truncate_recent(monkeypatch):
    """The REST path must request wide and return bars ending today."""
    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy-secret")
    idx = pd.bdate_range(end=_today(), periods=440)
    bars = [
        {
            "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": 600.0, "h": 601.0, "l": 599.0, "c": 600.0, "v": 1000,
        }
        for ts in idx
    ]
    payload = {"bars": {"SPY": bars}}
    seen_urls = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def _fake_urlopen(req, timeout=None):
        seen_urls.append(req.full_url)
        return _FakeResponse()

    monkeypatch.setattr(universe.urllib.request, "urlopen", _fake_urlopen)
    got = universe._rest_daily_bars("SPY", 320, _cfg())
    assert got is not None
    assert len(got) == 440
    assert got.index.max().date() == _today().date()
    assert "limit=1000" in seen_urls[0]
