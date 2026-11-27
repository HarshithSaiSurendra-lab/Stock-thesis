"""Tests for exp-sma50-timing-v1 (sma_timing.py).

Fixtures (frozen in the design doc):
  (a) overnight gap — the exit decision follows the CLOSE rule, the fill
      is at the (gapped) open;
  (b) holiday — a cross at Wednesday's close, Thursday a holiday, the
      first eligible session is Friday's open;
  (c) insufficient history — fewer than 50 sessions, no SMA50, no timing
      exit, baseline behaviour, recorded as insufficient;
  (d) stock split — the RAW line produces a false cross at the split,
      the SPLIT-ADJUSTED line produces none;
  (e) the clock — a cross known at Tuesday's close first affects
      Wednesday's open (never Tuesday itself).
Plus: the entry-gate redundancy proof on synthetic data, the
fresh-selection replication check against the engine, and the baseline
structural check (sim entries/exits == engine held-weight transitions).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest import BacktestConfig, signal_to_weights
from market_calendar import is_trading_day
from sma_timing import (
    TrainPanels,
    _cross_fired,
    _daily_selection,
    _is_whipsaw,
    build_train_panels,
    engine_weights,
    entry_gate_binding_count,
    fresh_selections,
    load_split_adjusted_bars,
    simulate,
)
from config import TradingConfig


def _tiny_panels(sessions, closes, opens=None, signal=None, spy_close=100.0):
    """Minimal TrainPanels for fixtures. `closes`: {symbol: [close,...]}.
    Opens default to prior close (no gap) unless given. SMA50 is the
    rolling 50-session mean, NaN for the first 49 sessions."""
    symbols = sorted(closes)
    closes_df = pd.DataFrame({s: closes[s] for s in symbols}, index=sessions)
    if opens is None:
        opens_df = closes_df.shift(1)
        opens_df.iloc[0] = closes_df.iloc[0]
    else:
        opens_df = pd.DataFrame({s: opens[s] for s in symbols}, index=sessions)
    sma50 = closes_df.rolling(50, min_periods=50).mean()
    if signal is None:
        signal_df = pd.DataFrame(0.0, index=sessions, columns=symbols)
    else:
        signal_df = pd.DataFrame({s: signal.get(s, [0.0] * len(sessions)) for s in symbols},
                                 index=sessions)
    return TrainPanels(
        sessions=sessions, symbols=symbols, opens=opens_df, closes=closes_df,
        sma50=sma50, signal=signal_df,
        spy_closes=pd.Series(spy_close, index=sessions),
        exclusions=[], n_train_sessions=len(sessions),
    )


def _held_from(panels, symbol, first_day_idx, last_day_idx, weight=0.2):
    held = pd.DataFrame(0.0, index=panels.sessions, columns=panels.symbols)
    held.iloc[first_day_idx:last_day_idx + 1, held.columns.get_loc(symbol)] = weight
    return held


def _tuesday_index(sessions, min_idx=50):
    """First Tuesday at or after min_idx (so the SMA50 exists)."""
    for i, d in enumerate(sessions):
        if i >= min_idx and d.weekday() == 1:  # Tuesday
            return i
    raise AssertionError("no Tuesday in fixture sessions")


# ---------------------------------------------------------------------------
# (e) the clock + (a) the overnight gap
# ---------------------------------------------------------------------------

def test_clock_cross_tuesday_close_exits_wednesday_open():
    sessions = pd.bdate_range("2024-03-01", periods=60)
    tue = _tuesday_index(sessions)
    assert sessions[tue + 1].weekday() == 2  # Wednesday
    closes = [100.0] * 60
    closes[tue] = 90.0  # cross at Tuesday's close (see below)
    opens = [100.0] * 60
    opens[tue + 1] = 95.0  # (a) Wednesday's open gaps UP above Tuesday's close
    panels = _tiny_panels(sessions, {"AAA": closes}, {"AAA": opens})
    # Entry well before the cross; held through the window end (so the
    # baseline's counterfactual is the window-end mark). Note the variant
    # may re-enter after the SMA exit via a fresh engine selection — the
    # assertions below are on the FIRST timing exit only.
    held = _held_from(panels, "AAA", 5, 59)
    res = simulate(panels, held, "exit_timing", 10.0)
    assert res.n_sma_exits == 1
    tr = res.timing_exits[0]
    # The cross is known at Tuesday's close; the first eligible action is
    # Wednesday's open — never Tuesday itself.
    assert tr.exit_date == str(sessions[tue + 1].date())
    # The fill is at the (gapped) open, not at Tuesday's close.
    assert tr.exit_price == 95.0
    # Baseline holds to window end: the timing exit is earlier.
    base = simulate(panels, held, "baseline", 10.0)
    assert base.n_sma_exits == 0
    assert base.trades[0].exit_reason == "window_end"


def test_no_cross_no_exit():
    sessions = pd.bdate_range("2024-03-01", periods=60)
    closes = [100.0] * 60
    closes[30] = 99.0  # dips below the flat SMA50=100? SMA50_30 is still
    # forming (NaN until day 49) — no cross can fire before day 49.
    panels = _tiny_panels(sessions, {"AAA": closes})
    held = _held_from(panels, "AAA", 5, 59)
    res = simulate(panels, held, "exit_timing", 10.0)
    assert res.n_sma_exits == 0


# ---------------------------------------------------------------------------
# (b) holiday
# ---------------------------------------------------------------------------

def test_holiday_cross_wednesday_exits_friday_open():
    # Thanksgiving week 2026: Wed Nov 25 is a session, Thu Nov 26 is the
    # holiday, Fri Nov 27 is the next session.
    assert is_trading_day(__import__("datetime").date(2026, 11, 25))
    assert not is_trading_day(__import__("datetime").date(2026, 11, 26))
    assert is_trading_day(__import__("datetime").date(2026, 11, 27))
    sessions = pd.DatetimeIndex([
        "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08",
        "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15",
        "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22",
        "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29",
        "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
        "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13",
        "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
        "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27",
        "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03",
        "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10",
        "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17",
        "2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24",
        "2026-11-25", "2026-11-27", "2026-11-30", "2026-12-01", "2026-12-02",
    ])
    assert sessions[-5] == pd.Timestamp("2026-11-25")  # Wednesday
    assert sessions[-4] == pd.Timestamp("2026-11-27")  # Friday (Thu is the holiday)
    n = len(sessions)
    closes = [100.0] * n
    closes[-5] = 90.0  # cross at Wednesday's close
    opens = [100.0] * n  # explicit opens (default would carry the prior close)
    panels = _tiny_panels(sessions, {"AAA": closes}, {"AAA": opens})
    held = _held_from(panels, "AAA", 5, n - 1)
    res = simulate(panels, held, "exit_timing", 10.0)
    assert res.n_sma_exits == 1
    tr = res.timing_exits[0]
    # First eligible session after the cross is Friday's open (Thursday
    # does not exist as a session).
    assert tr.exit_date == "2026-11-27"
    assert tr.exit_price == 100.0


# ---------------------------------------------------------------------------
# (c) insufficient history
# ---------------------------------------------------------------------------

def test_insufficient_history_no_sma_no_timing_exit():
    sessions = pd.bdate_range("2024-03-01", periods=30)
    closes = [100.0] * 30
    closes[-1] = 90.0  # a drop, but SMA50 does not exist yet (< 50 sessions)
    panels = _tiny_panels(sessions, {"BBB": closes})
    assert panels.sma50["BBB"].isna().all()
    held = _held_from(panels, "BBB", 5, 29)
    res = simulate(panels, held, "exit_timing", 10.0)
    assert res.n_sma_exits == 0
    assert res.n_insufficient > 0  # recorded as insufficient
    # Baseline behaviour is unchanged: the position is held to window end.
    assert res.trades[0].exit_reason == "window_end"


# ---------------------------------------------------------------------------
# (d) stock split — raw line false-crosses, adjusted line does not
# ---------------------------------------------------------------------------

def test_split_raw_line_false_cross_adjusted_line_clean():
    sessions = pd.bdate_range("2024-03-01", periods=60)
    # Raw prices: flat 1000, then a 10:1 split — the price drops to 100
    # and stays there. No market move, just the split.
    raw = [1000.0] * 50 + [100.0] * 10
    raw_s = pd.Series(raw, index=sessions)
    raw_sma = raw_s.rolling(50, min_periods=50).mean()
    # The raw SMA collapses after the split and the close crosses below it.
    assert _cross_fired(raw_s, raw_sma, sessions[50])
    # Split-adjusted: pre-split prices divided by 10 — a flat 100 line.
    adj = [100.0] * 60
    adj_s = pd.Series(adj, index=sessions)
    adj_sma = adj_s.rolling(50, min_periods=50).mean()
    for day in sessions[50:]:
        assert not _cross_fired(adj_s, adj_sma, day)
    # And a genuine cross on the adjusted line still fires.
    adj2 = [100.0] * 55 + [90.0] + [100.0] * 4
    adj2_s = pd.Series(adj2, index=sessions)
    adj2_sma = adj2_s.rolling(50, min_periods=50).mean()
    assert _cross_fired(adj2_s, adj2_sma, sessions[55])


def test_whipsaw_definition():
    sessions = pd.bdate_range("2024-03-01", periods=60)
    closes = [100.0] * 51 + [90.0] + [100.0] * 8  # cross at 51, back above at 52
    s = pd.Series(closes, index=sessions)
    sma = s.rolling(50, min_periods=50).mean()
    assert _cross_fired(s, sma, sessions[51])
    assert _is_whipsaw(s, sma, sessions[51], sessions, n=5)
    closes2 = [100.0] * 51 + [90.0] * 9  # stays below for 5+ sessions
    s2 = pd.Series(closes2, index=sessions)
    sma2 = s2.rolling(50, min_periods=50).mean()
    assert _cross_fired(s2, sma2, sessions[51])
    assert not _is_whipsaw(s2, sma2, sessions[51], sessions, n=5)


# ---------------------------------------------------------------------------
# Entry-gate redundancy proof (synthetic) + fresh-selection replication
# ---------------------------------------------------------------------------

def test_block_clear_counts_signal_vs_all_zero():
    # After an SMA exit, the block clears on a fresh engine selection.
    # Count HOW it cleared: a positive-signal selection vs the engine's
    # all-zero top-5 pick (the quirk). The design doc's §2 wording says
    # "with an active v2 signal" — the all-zero path is the deviation.
    sessions = pd.bdate_range("2024-03-01", periods=60)
    tue = _tuesday_index(sessions)
    closes = [100.0] * 60
    closes[tue] = 90.0
    opens = [100.0] * 60
    # Case 1: the clearing rebalance day (first rebalance day after the
    # exit) carries a positive signal for AAA.
    signal = {"AAA": [0.0] * 60}
    rebal_after = next(i for i in range(tue + 1, 60) if i % 5 == 0)
    signal["AAA"][rebal_after] = 1.0
    panels = _tiny_panels(sessions, {"AAA": closes}, {"AAA": opens}, signal)
    held = _held_from(panels, "AAA", 5, 59)
    res = simulate(panels, held, "exit_timing", 10.0)
    assert res.n_sma_exits == 1
    assert res.n_block_cleared_signal == 1
    assert res.n_block_cleared_all_zero == 0
    # The block cleared on a positive-signal selection and AAA was
    # re-entered at the next open after the clearing day.
    assert res.n_reentries_signal == 1
    assert res.n_reentries_all_zero == 0
    assert [t.reentry_kind for t in res.trades if t.reentry_kind] == ["signal"]
    # Case 2: the signal is all zeros — the clearing selection is the
    # engine's all-zero top-5 pick (engine mode).
    panels2 = _tiny_panels(sessions, {"AAA": closes}, {"AAA": opens})
    res2 = simulate(panels2, held, "exit_timing", 10.0)
    assert res2.n_sma_exits == 1
    assert res2.n_block_cleared_signal == 0
    assert res2.n_block_cleared_all_zero == 1
    assert res2.n_reentries_signal == 0
    assert res2.n_reentries_all_zero == 1
    assert [t.reentry_kind for t in res2.trades if t.reentry_kind] == ["all_zero"]
    # Case 3: strict_signal mode — the all-zero selection does NOT clear
    # the block (the design doc's §2 literal rule), so there is no
    # re-entry and the position stays out for the rest of the window.
    res3 = simulate(panels2, held, "exit_timing", 10.0, reentry_mode="strict_signal")
    assert res3.n_sma_exits == 1
    assert res3.n_block_cleared_all_zero == 0
    assert res3.n_block_refused_all_zero >= 1
    assert res3.n_reentries_signal == 0
    assert res3.n_reentries_all_zero == 0
    assert [t.reentry_kind for t in res3.trades if t.reentry_kind] == []


def test_entry_gate_never_binds_for_signal_driven_selection():
    # A symbol selected while close > SMA50: the entry gate (latest
    # completed close above SMA50 at the entry open) always passes.
    # The entry is placed late enough that the SMA50 exists (day 55),
    # and the signal is positive on the deciding rebalance day (day 50),
    # so the gate is genuinely evaluated on a signal-driven selection.
    sessions = pd.bdate_range("2024-03-01", periods=60)
    closes = [100.0 + i * 0.5 for i in range(60)]  # rising: close > SMA50
    signal = {"AAA": [0.0] * 50 + [1.0] * 10}
    panels = _tiny_panels(sessions, {"AAA": closes}, signal=signal)
    held = _held_from(panels, "AAA", 55, 59)
    red = entry_gate_binding_count(panels, held)
    assert red["binding"] == 0
    assert red["entries_checked"] == 1


def test_daily_selection_matches_engine_on_real_panel():
    cfg = TradingConfig.from_env()
    data = load_split_adjusted_bars(fetch_missing=False)
    panels = build_train_panels(data, cfg)
    target, _ = engine_weights(panels.signal)
    # On every rebalance day, my replication of the engine's daily rule
    # must equal the engine's own target weights for that day.
    for i, day in enumerate(panels.sessions):
        if i % 5 != 0:
            continue
        mine = set(_daily_selection(panels.signal.loc[day], 5))
        engine = set(target.loc[day][target.loc[day] > 0].index)
        assert mine == engine, f"selection mismatch on {day.date()}: {mine} vs {engine}"


def test_baseline_sim_matches_engine_transitions():
    """Structural fidelity: the baseline sim's entries/exits must equal
    the engine's held-weight transitions exactly (same symbol, same day).
    Any divergence means the simulator does not replay the baseline."""
    cfg = TradingConfig.from_env()
    data = load_split_adjusted_bars(fetch_missing=False)
    panels = build_train_panels(data, cfg)
    _, held = engine_weights(panels.signal)
    res = simulate(panels, held, "baseline", 10.0)
    assert res.n_missing_open == 0
    # Engine-implied entries / exits from the held-weight matrix.
    eng_entries, eng_exits = set(), set()
    prev = pd.Series(0.0, index=held.columns)
    for t in held.index:
        row = held.loc[t].fillna(0.0)
        for s in held.columns:
            if prev[s] == 0 and row[s] > 0:
                eng_entries.add((s, str(t.date())))
            elif prev[s] > 0 and row[s] == 0:
                eng_exits.add((s, str(t.date())))
        prev = row
    sim_entries = {(t.symbol, t.entry_date) for t in res.trades}
    sim_exits = {(t.symbol, t.exit_date) for t in res.trades if t.exit_reason == "baseline"}
    # Window-end marks are not engine exits; exclude trades still open.
    assert sim_entries == eng_entries, (
        f"entry mismatch: sim-only {sorted(sim_entries - eng_entries)[:5]}, "
        f"engine-only {sorted(eng_entries - sim_entries)[:5]}")
    assert sim_exits == eng_exits, (
        f"exit mismatch: sim-only {sorted(sim_exits - eng_exits)[:5]}, "
        f"engine-only {sorted(eng_exits - sim_exits)[:5]}")
