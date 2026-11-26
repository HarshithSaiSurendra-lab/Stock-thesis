"""Data-freshness regression tests.

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
   tail in code, and judge freshness against the exchange calendar.

What changed 2026-11-25 (user instruction): the old 7-calendar-day age
heuristic is gone. Freshness is now "the latest complete bar IS the
latest completed NYSE session", computed from the exchange calendar at
the decision time — so a holiday weekend, a missing session, and a
partial bar are all distinguishable, and a stale frame rejected inside
``_daily_bars`` still reaches the decision log as "benchmark data
stale" (not "unavailable"). Quotes are judged by the timestamp the
feed supplied, and the Guardian's staleness check reads a DataClock
fed only by those feed timestamps.

These tests are offline: synthetic frames and mocked clients only.
No API calls, no broker, no real data dirs. Fixed 2026 dates are used
throughout so the tests do not depend on the wall clock.

Run: .venv/bin/python -m pytest test_data_freshness.py -v
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import filters
import universe
from config import TradingConfig
from data_clock import DataClock
from filters import downside_ok, market_regime
from guardian import Guardian, GuardianConfig
from strategy_runner import _freshness_report, _write_decision_log
from universe import StaleBarsError, check_frame_freshness

# Fixed evaluation times (Thanksgiving week 2026, America/New_York).
# Wed Nov 25 16:15 EST — after the close, expected session is Nov 25.
WED_CLOSE = datetime(2026, 11, 25, 21, 15, tzinfo=timezone.utc)
# Thu Nov 26 16:15 EST — Thanksgiving, market closed.
THU_THANKSGIVING = datetime(2026, 11, 26, 21, 15, tzinfo=timezone.utc)
# Fri Nov 27 16:15 EST — early close 13:00 ET, expected session is Nov 27.
FRI_EARLY_CLOSE = datetime(2026, 11, 27, 21, 15, tzinfo=timezone.utc)
# Sat Nov 28 10:00 EST — weekend, expected session is still Fri Nov 27.
SAT_AFTER = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)
# Mon Nov 30 10:00 EST — before the close, expected session is Fri Nov 27.
MON_BEFORE_CLOSE = datetime(2026, 11, 30, 15, 0, tzinfo=timezone.utc)
# Mon Nov 30 16:15 EST — after the close, expected session is Nov 30.
MON_AFTER_CLOSE = datetime(2026, 11, 30, 21, 15, tzinfo=timezone.utc)


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


def _frame_on(dates) -> pd.DataFrame:
    """Rising OHLCV frame on an explicit list of dates (no holidays)."""
    closes = np.linspace(500.0, 700.0, len(dates)) + 0.5 * np.sin(np.arange(len(dates)))
    return pd.DataFrame(
        {
            "open": closes - 0.5,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": 30_000_000.0,
        },
        index=pd.DatetimeIndex(dates),
    )


def _trading_days_through(end: date, periods: int = 60):
    """Business days ending on `end`, minus NYSE holidays in the window."""
    days = pd.bdate_range(end=end, periods=periods)
    return [d for d in days if d.date() not in {date(2026, 11, 26), date(2026, 12, 25)}]


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


# ------------------------------------------------------- exchange calendar

def test_latest_completed_session_thanksgiving_week():
    """The expected session follows the exchange calendar, not the clock.

    Wed Nov 25 after the close -> Nov 25. Thanksgiving (closed) ->
    still Nov 25. Fri Nov 27 (early close 13:00 ET) -> Nov 27 any time
    after 13:00. Saturday -> still Nov 27. Monday before the close ->
    still Nov 27; Monday after the close -> Nov 30.
    """
    from market_calendar import is_trading_day, latest_completed_session, session_close

    assert latest_completed_session(WED_CLOSE) == date(2026, 11, 25)
    assert latest_completed_session(THU_THANKSGIVING) == date(2026, 11, 25)
    assert latest_completed_session(FRI_EARLY_CLOSE) == date(2026, 11, 27)
    assert latest_completed_session(SAT_AFTER) == date(2026, 11, 27)
    assert latest_completed_session(MON_BEFORE_CLOSE) == date(2026, 11, 27)
    assert latest_completed_session(MON_AFTER_CLOSE) == date(2026, 11, 30)
    # Early close: the Nov 27 session ends at 13:00 ET, not 16:00.
    close = session_close(date(2026, 11, 27))
    assert close.hour == 13 and close.minute == 0
    assert session_close(date(2026, 11, 25)).hour == 16
    # Holidays are not trading days.
    assert is_trading_day(date(2026, 11, 26)) is False
    assert is_trading_day(date(2026, 11, 27)) is True
    assert is_trading_day(date(2026, 12, 25)) is False


# ------------------------------------------------------------- frame freshness

def test_frame_ending_on_expected_session_is_fresh():
    frame = _synthetic_frame(end=pd.Timestamp("2026-11-25"))
    res = check_frame_freshness(frame, _cfg(), WED_CLOSE)
    assert res.fresh is True
    assert res.expected_session == date(2026, 11, 25)
    assert res.observed_latest_bar == date(2026, 11, 25)
    assert res.missing_sessions == []


def test_holiday_weekend_frame_ending_friday_is_fresh():
    """A frame ending Fri Nov 27 is fresh on Saturday — no Nov 26 bar exists."""
    frame = _frame_on(_trading_days_through(date(2026, 11, 27)))
    res = check_frame_freshness(frame, _cfg(), SAT_AFTER)
    assert res.fresh is True
    assert res.expected_session == date(2026, 11, 27)
    assert res.observed_latest_bar == date(2026, 11, 27)


def test_frame_ending_wednesday_is_stale_on_saturday():
    """The holiday-weekend trap: a frame ending Wed Nov 25 is missing the
    Fri Nov 27 session, even though it is only three days old."""
    frame = _frame_on(_trading_days_through(date(2026, 11, 25)))
    res = check_frame_freshness(frame, _cfg(), SAT_AFTER)
    assert res.fresh is False
    assert res.expected_session == date(2026, 11, 27)
    assert res.observed_latest_bar == date(2026, 11, 25)
    assert res.missing_sessions == [date(2026, 11, 27)]


def test_missing_completed_session():
    """A frame ending yesterday, evaluated after today's close, is stale
    by exactly the one missing session."""
    frame = _synthetic_frame(end=pd.Timestamp("2026-11-24"))
    res = check_frame_freshness(frame, _cfg(), WED_CLOSE)
    assert res.fresh is False
    assert res.expected_session == date(2026, 11, 25)
    assert res.observed_latest_bar == date(2026, 11, 24)
    assert res.missing_sessions == [date(2026, 11, 25)]


def test_partial_bar_does_not_count():
    """A partial bar for the expected session (NaN volume) does not count
    as that session's data — the latest complete bar is yesterday."""
    frame = _synthetic_frame(end=pd.Timestamp("2026-11-25"))
    frame.loc[frame.index.max(), "volume"] = np.nan
    res = check_frame_freshness(frame, _cfg(), WED_CLOSE)
    assert res.fresh is False
    assert res.observed_latest_bar == date(2026, 11, 24)
    assert res.missing_sessions == [date(2026, 11, 25)]


def test_partial_bar_before_close_is_ignored():
    """At 10:00 ET the session has not completed, so a partial bar for
    today must not break freshness — the expected session is yesterday,
    and a complete-looking intraday bar for today is ignored too."""
    frame = _synthetic_frame(end=pd.Timestamp("2026-11-24"))
    # A complete-looking bar for today (intraday-updated daily bar).
    today_row = frame.iloc[-1].copy()
    today_row.name = pd.Timestamp("2026-11-25")
    frame = pd.concat([frame, today_row.to_frame().T])
    res = check_frame_freshness(frame, _cfg(), datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc))
    assert res.fresh is True
    assert res.expected_session == date(2026, 11, 24)
    assert res.observed_latest_bar == date(2026, 11, 24)


# ------------------------------------------------------- market_regime

def test_fetch_none_blocks_trading(monkeypatch):
    """Reproduces the 2026-11-25 failure: fetch returns None (API error).

    Expected response: fail closed — no longs, reason "benchmark data
    unavailable", so the decision log records regime_blocked, not a
    market verdict.
    """
    monkeypatch.setattr(filters, "fetch_symbol_frame", lambda broker, symbol, cfg, now=None: None)
    result = market_regime(broker=None, cfg=_cfg(), now=WED_CLOSE)
    assert result.ok is False
    assert result.reason == "benchmark data unavailable"
    assert result.symbol == "SPY"


def test_stale_frame_reaches_log_as_benchmark_data_stale(monkeypatch):
    """A stale frame rejected inside _daily_bars must reach the final log
    as "benchmark data stale" — not "unavailable".

    Simulates _daily_bars raising StaleBarsError (its real behaviour for
    a months-old window) and checks the regime result carries the
    expected / observed dates that diagnose the block.
    """
    stale = _synthetic_frame(end=pd.Timestamp("2026-06-02"), periods=320)

    def _raise(broker, symbol, cfg, now=None):
        res = check_frame_freshness(stale, cfg, now)
        raise StaleBarsError(symbol, res)

    monkeypatch.setattr(filters, "fetch_symbol_frame", _raise)
    result = market_regime(broker=None, cfg=_cfg(), now=WED_CLOSE)
    assert result.ok is False
    assert result.reason == "benchmark data stale"
    assert result.details["expected_latest_session"] == "2026-11-25"
    assert result.details["observed_latest_bar"] == "2026-06-02"
    assert result.details["missing_sessions"]  # many sessions, not just one


def test_fresh_benchmark_frame_is_evaluated(monkeypatch):
    """A fresh frame must reach the real verdict, not a data reason —
    and its details must carry the expected / observed session."""
    fresh = _synthetic_frame(end=pd.Timestamp("2026-11-25"))
    monkeypatch.setattr(
        filters, "fetch_symbol_frame", lambda broker, symbol, cfg, now=None: fresh
    )
    result = market_regime(broker=None, cfg=_cfg(), now=WED_CLOSE)
    assert result.ok is True
    assert result.reason == "ok"
    assert result.details["above_sma_50"] is True
    assert result.details["above_sma_200"] is True
    assert result.details["expected_latest_session"] == "2026-11-25"
    assert result.details["observed_latest_bar"] == "2026-11-25"


# ---------------------------------------------------------- _daily_bars

def test_daily_bars_raises_on_stale_window():
    """_daily_bars must RAISE for a months-old window, not return None —
    so the caller can report "stale" instead of "unavailable"."""
    stale = _synthetic_frame(end=pd.Timestamp("2026-06-02"), periods=320)
    with pytest.raises(StaleBarsError) as excinfo:
        universe._daily_bars(_FakeBarsClient(stale), "SPY", 320, _cfg(), WED_CLOSE)
    assert excinfo.value.details["expected_latest_session"] == "2026-11-25"
    assert excinfo.value.details["observed_latest_bar"] == "2026-06-02"


def test_daily_bars_returns_latest_window():
    """The limit fix: a full-span response must come back ending on the
    expected session. The API hands back the earliest N bars from the
    requested start, so the code requests wide (limit=1000) and takes
    the tail. A 440-bar response must yield the latest 320 bars."""
    wide = _synthetic_frame(end=pd.Timestamp("2026-11-25"), periods=440)
    got = universe._daily_bars(_FakeBarsClient(wide), "SPY", 320, _cfg(), WED_CLOSE)
    assert got is not None
    assert len(got) == 320
    assert got.index.max().date() == date(2026, 11, 25)


def test_daily_bars_refetches_when_cache_is_stale(tmp_path):
    """A recently written cache holding an old window must not be served."""
    cfg = _cfg()
    cfg.run.bar_cache_enabled = True
    cfg.run.bar_cache_dir = str(tmp_path)
    stale = _synthetic_frame(end=pd.Timestamp("2026-06-02"), periods=320)
    universe._write_cached_bars("SPY", 320, cfg, stale)
    fresh = _synthetic_frame(end=pd.Timestamp("2026-11-25"), periods=320)
    got = universe._daily_bars(_FakeBarsClient(fresh), "SPY", 320, cfg, WED_CLOSE)
    assert got is not None
    assert got.index.max().date() == date(2026, 11, 25)


def test_rest_bars_does_not_truncate_recent(monkeypatch):
    """The REST path must request wide and return bars ending on the
    expected session."""
    monkeypatch.setenv("ALPACA_PAPER_KEY", "dummy-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "dummy-secret")
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-25"), periods=440)
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
    assert got.index.max().date() == date(2026, 11, 25)
    assert "limit=1000" in seen_urls[0]


# ------------------------------------------------------------------ quotes

def test_old_quote_received_just_now_is_rejected():
    """An old quote received just now: the FEED timestamp is 30 minutes
    old, so the quote is stale even though it arrived this second."""
    cfg = _cfg()
    cfg.risk.max_quote_age_seconds = 120
    row = pd.Series({"rvol_20": 0.20, "close": 100.0})
    old_quote = {
        "bid": 99.9,
        "ask": 100.0,
        "spread": 0.1,
        "timestamp": (WED_CLOSE - timedelta(minutes=30)).isoformat(),
    }
    ok, reason = downside_ok(row, old_quote, cfg, WED_CLOSE)
    assert not ok
    assert "quote age 1800s" in reason


def test_fresh_quote_passes_age_check():
    cfg = _cfg()
    cfg.risk.max_quote_age_seconds = 120
    row = pd.Series({"rvol_20": 0.20, "close": 100.0})
    fresh_quote = {
        "bid": 99.9,
        "ask": 100.0,
        "spread": 0.1,
        "timestamp": (WED_CLOSE - timedelta(seconds=10)).isoformat(),
    }
    ok, reason = downside_ok(row, fresh_quote, cfg, WED_CLOSE)
    assert ok, reason


def test_quote_without_timestamp_is_rejected():
    """A quote with no feed timestamp cannot be verified fresh — fail
    closed instead of trading on an unverifiable quote."""
    cfg = _cfg()
    cfg.risk.max_quote_age_seconds = 120
    row = pd.Series({"rvol_20": 0.20, "close": 100.0})
    ok, reason = downside_ok(row, {"bid": 99.9, "ask": 100.0, "spread": 0.1}, cfg, WED_CLOSE)
    assert not ok
    assert reason == "quote timestamp unavailable"


def test_latest_quote_carries_feed_timestamp():
    """_latest_quote must surface the feed timestamp so the age check
    and the Guardian's DataClock have something to judge."""
    ts = datetime(2026, 11, 25, 21, 14, 30, tzinfo=timezone.utc)
    quote_obj = SimpleNamespace(bid_price=99.9, ask_price=100.0, timestamp=ts)

    class _FakeQuoteClient:
        def get_stock_latest_quote(self, req):
            return {"SPY": quote_obj}

    got = universe._latest_quote(_FakeQuoteClient(), "SPY")
    assert got is not None
    assert got["timestamp"] == ts.isoformat()
    assert got["bid"] == 99.9 and got["ask"] == 100.0


# ------------------------------------------------------------- data clock

def test_data_clock_uses_feed_timestamp():
    """The clock tracks the FEED timestamp, not the receipt time."""
    clock = DataClock()
    assert clock.last_ts() is None
    assert clock.age_seconds(WED_CLOSE) is None
    clock.note((WED_CLOSE - timedelta(minutes=30)).isoformat())
    assert clock.age_seconds(WED_CLOSE) == pytest.approx(1800.0)
    # A newer feed timestamp advances the clock; an older one does not.
    clock.note((WED_CLOSE - timedelta(seconds=5)).isoformat())
    assert clock.age_seconds(WED_CLOSE) == pytest.approx(5.0)
    clock.note(None)  # no timestamp -> ignored, clock unchanged
    assert clock.age_seconds(WED_CLOSE) == pytest.approx(5.0)


def _guardian(tmp_path, clock: DataClock) -> Guardian:
    cfg = GuardianConfig(
        kill_switch_path=str(tmp_path / "kill"),
        state_path=str(tmp_path / "guardian_state.json"),
        alert_log_path=str(tmp_path / "alerts.log"),
        max_position_pct=0.25,
    )
    g = Guardian(cfg, get_account=lambda: {"equity": 10_000.0}, last_data_ts=clock.last_ts)
    g.start_day()
    return g


def test_guardian_halts_on_stale_feed_timestamp(tmp_path):
    """A quote 30 minutes old (feed time) must halt the Guardian even
    though it was received just now."""
    clock = DataClock()
    clock.note(WED_CLOSE - timedelta(minutes=30))
    g = _guardian(tmp_path, clock)
    # Evaluate staleness against the fixed decision time, not wall time.
    import guardian as guardian_module

    real_time = guardian_module.time.time
    guardian_module.time.time = lambda: WED_CLOSE.timestamp()
    try:
        assert g.can_trade() is False
        assert g.state.halt_reason is not None
        assert g.state.halt_reason == "stale_market_data"
    finally:
        guardian_module.time.time = real_time


def test_guardian_skips_staleness_when_no_data_seen(tmp_path):
    """Before any feed data is observed, the Guardian must NOT halt on
    staleness — there is nothing to judge yet. The per-decision checks
    judge the data once it arrives."""
    clock = DataClock()
    g = _guardian(tmp_path, clock)
    assert g.can_trade() is True


# ------------------------------------------------------------ decision log

def test_decision_log_shows_expected_and_observed(tmp_path):
    """A regime-blocked run's log must show the expected session, the
    observed bar, and the missing sessions — enough to diagnose the
    block without re-running anything."""
    stale = _synthetic_frame(end=pd.Timestamp("2026-11-24"), periods=320)
    res = check_frame_freshness(stale, _cfg(), WED_CLOSE)
    assert res.fresh is False
    regime = filters.RegimeResult(False, "SPY", "benchmark data stale", {
        "expected_latest_session": res.expected_session.isoformat(),
        "expected_session_close": res.expected_close.isoformat(),
        "observed_latest_bar": res.observed_latest_bar.isoformat(),
        "missing_sessions": [d.isoformat() for d in res.missing_sessions],
    })
    cfg = _cfg()
    cfg.run.decision_log_enabled = True
    cfg.run.decision_log_dir = str(tmp_path)
    summary = {
        "date": "2026-11-25",
        "status": "regime_blocked",
        "reason": regime.reason,
        "regime": regime.__dict__,
        "data_freshness": _freshness_report(cfg, regime=regime, guardian=None, now=WED_CLOSE),
        "orders": [],
    }
    _write_decision_log(summary, cfg)
    written = list(Path(tmp_path).glob("*.json"))
    assert len(written) == 1
    logged = json.loads(written[0].read_text())
    assert logged["status"] == "regime_blocked"
    assert logged["reason"] == "benchmark data stale"
    assert logged["regime"]["details"]["expected_latest_session"] == "2026-11-25"
    assert logged["regime"]["details"]["observed_latest_bar"] == "2026-11-24"
    assert logged["regime"]["details"]["missing_sessions"] == ["2026-11-25"]
    assert logged["data_freshness"]["expected_latest_session"] == "2026-11-25"
    assert logged["data_freshness"]["observed_latest_bar"] == "2026-11-24"
    assert logged["data_freshness"]["decision_time"] == cfg.run.decision_time
