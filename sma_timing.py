"""sma_timing.py — 50-session SMA entry/exit timing experiment (exp-sma50-timing-v1).

Research-only. Replays the registered v2 baseline signals on the TRAIN
window (2024-01-02..2026-08-17) with open fills, and applies a
stock-level SMA50-cross exit. See
experiments/exp-sma50-timing-v1-design.md (registered BEFORE any result).

Data: split-adjusted Alpaca bars (feed IEX, adjustment='split'), cached
separately in .cache/backtest_data_split/ — NEVER the registered raw
cache. The raw cache contains unadjusted splits (NVDA 10:1, AVGO 10:1,
CMG 50:1, WMT 3:1, PANW 2:1, ORLY 15:1, NFLX 10:1, ...) which would put
false crosses in the SMA line. Any symbol with a remaining single-day
|return| > 40% level shift after split adjustment is EXCLUDED (the
frozen exclusion rule); DD's Qnity spinoff is expected to trip it.

Timing rule (the clock): a signal/cross known at the close of session t
can first be acted on at the open of the NEXT session. Fills are at the
open price. The signal session's own close is never a fill price.

Baseline fidelity: the baseline trajectory is the engine's own
held-weight matrix (backtest.signal_to_weights -> _lag_weights ->
_enforce_min_hold) on the registered v2 panel, so entries/exits,
weekly rebalance (every 5th session), 5-day min hold, and the SPY
regime mask are exactly the registered v2 mechanics. The ONLY change
the exit-timing variant makes is forcing a holding to 0 at the open
after an SMA50 cross, overriding the min-hold. Re-entry is only via a
fresh engine selection (see _fresh_selections).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import BacktestConfig, _enforce_min_hold, _lag_weights, signal_to_weights
from config import TradingConfig
from market_calendar import is_trading_day
from research_runner import (
    ResearchData,
    build_v2_signal,
    fetch_alpaca_rest_history,
)

REPO = Path(__file__).resolve().parent
SPLIT_CACHE_DIR = REPO / ".cache" / "backtest_data_split"
RAW_CACHE_DIR = REPO / ".cache" / "backtest_data"
HOLDOUT_DATES = json.loads((REPO / "experiments" / "holdout_dates.json").read_text())

TRAIN_START = HOLDOUT_DATES["train_start"]  # 2024-01-02
TRAIN_END = HOLDOUT_DATES["train_end"]      # 2026-08-17
WARMUP_START = "2023-01-01"

SMA_WINDOW = 50
V2_CFG = dict(max_positions=5, rebalance_every=5, min_hold_days=5)
COSTS_BPS = (10.0, 20.0)

# Frozen exclusion rule (design §3): any remaining single-day |return|
# above this after split adjustment is an unexplained level shift.
LEVEL_SHIFT_THRESHOLD = 0.40


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def _load_env() -> None:
    for line in open(REPO / ".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _tz_naive(frame: pd.DataFrame) -> pd.DataFrame:
    idx = frame.index
    if getattr(idx, "tz", None) is not None:
        frame = frame.copy()
        frame.index = idx.tz_convert(None)
    return frame


def universe_symbols() -> list[str]:
    """Universe = the registered raw-cache symbols (133 + SPY benchmark)."""
    return sorted(p.stem for p in RAW_CACHE_DIR.glob("*.csv"))


def load_split_adjusted_bars(
    symbols: list[str] | None = None,
    fetch_missing: bool = True,
) -> ResearchData:
    """Split-adjusted bars, warmup 2023-01-01 through TRAIN_END.

    Loads per-symbol CSVs from .cache/backtest_data_split/; fetches any
    missing symbol via the existing Alpaca REST path (feed IEX,
    adjustment='split') and saves it there. Never touches the raw cache.
    """
    symbols = symbols or universe_symbols()
    SPLIT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    bars: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for symbol in symbols:
        path = SPLIT_CACHE_DIR / f"{symbol}.csv"
        if path.exists():
            bars[symbol] = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            missing.append(symbol)
    if missing and fetch_missing:
        _load_env()
        key = os.environ.get("ALPACA_PAPER_KEY") or os.environ.get("ALPACA_LIVE_KEY")
        secret = os.environ.get("ALPACA_PAPER_SECRET") or os.environ.get("ALPACA_LIVE_SECRET")
        if not key or not secret:
            raise RuntimeError("missing Alpaca API keys in environment")
        fetched = fetch_alpaca_rest_history(
            missing,
            key,
            secret,
            datetime(2023, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 18, tzinfo=timezone.utc),
            "iex",
            "split",
        )
        for symbol, frame in fetched.bars.items():
            frame = _tz_naive(frame)
            frame.to_csv(SPLIT_CACHE_DIR / f"{symbol}.csv")
            bars[symbol] = frame
        still_missing = [s for s in missing if s not in fetched.bars]
        if still_missing:
            raise RuntimeError(f"split-adjusted fetch returned no bars for {still_missing}")
    if not bars:
        raise RuntimeError("no split-adjusted bars loaded")
    return ResearchData(bars, "alpaca-rest-iex-split")


def level_shift_exclusions(bars: dict[str, pd.DataFrame]) -> list[dict]:
    """Symbols with a remaining single-day |return| > 40% after split
    adjustment — an unexplained level shift (e.g. a spinoff, which split
    adjustment does not correct). Each is excluded, with the evidence."""
    out = []
    for symbol, frame in sorted(bars.items()):
        rets = frame["close"].pct_change()
        worst = rets.abs().max()
        if pd.notna(worst) and worst > LEVEL_SHIFT_THRESHOLD:
            day = rets.abs().idxmax()
            out.append({
                "symbol": symbol,
                "date": str(day.date()),
                "return": round(float(rets.loc[day]), 4),
                "close": round(float(frame.loc[day, "close"]), 2),
                "prev_close": round(float(frame["close"].shift(1).loc[day]), 2),
                "reason": "unexplained level shift after split adjustment",
            })
    return out


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------

@dataclass
class TrainPanels:
    sessions: pd.DatetimeIndex          # train sessions, 2024-01-02..2026-08-17
    symbols: list[str]                  # universe ex-benchmark, ex-exclusions
    opens: pd.DataFrame                 # split-adjusted open prices (train window)
    closes: pd.DataFrame                # split-adjusted closes (train window)
    sma50: pd.DataFrame                 # 50-session SMA of closes (warmup-aware)
    signal: pd.DataFrame                # registered v2 panel (train window)
    spy_closes: pd.Series               # split-adjusted SPY closes (train window)
    exclusions: list[dict]              # level-shift exclusions
    n_train_sessions: int


def build_train_panels(data: ResearchData, cfg: TradingConfig) -> TrainPanels:
    """Build the v2 signal panel + SMA50 + open/close panels on the TRAIN
    window only, from split-adjusted bars. Features are computed on the
    full warmup-inclusive frame, then sliced — so SMA50 / momentum are
    formed on day one of the window."""
    exclusions = level_shift_exclusions(data.bars)
    excluded = {e["symbol"] for e in exclusions}
    keep = [s for s in data.symbols if s != cfg.regime.benchmark_symbol and s not in excluded]
    kept = ResearchData({s: data.bars[s] for s in keep + [cfg.regime.benchmark_symbol]}, data.source)

    prices, signal = build_v2_signal(kept, cfg)
    # build_v2_signal returns the FULL frame; slice to the train window.
    # The rebalance grid (mask[::5] in signal_to_weights) starts at index 0
    # of the SLICED panel, exactly as in run_v2_backtest.py.
    prices = prices.loc[TRAIN_START:TRAIN_END]
    signal = signal.loc[TRAIN_START:TRAIN_END]
    assert str(prices.index.min().date()) == TRAIN_START
    assert str(prices.index.max().date()) == TRAIN_END

    opens = pd.DataFrame(
        {s: kept.bars[s]["open"] for s in keep}
    ).sort_index().loc[TRAIN_START:TRAIN_END]
    closes = prices[keep]
    # SMA50 is warmup-aware: computed from the full frame (from 2023),
    # then sliced — so it is formed on day one of the window.
    full_closes = pd.DataFrame(
        {s: kept.bars[s]["close"] for s in keep}
    ).sort_index()
    sma50 = full_closes.rolling(SMA_WINDOW, min_periods=SMA_WINDOW).mean().loc[TRAIN_START:TRAIN_END]
    spy_closes = pd.Series(
        kept.bars[cfg.regime.benchmark_symbol]["close"]
    ).sort_index().loc[TRAIN_START:TRAIN_END]
    return TrainPanels(
        sessions=prices.index,
        symbols=keep,
        opens=opens,
        closes=closes,
        sma50=sma50,
        signal=signal,
        spy_closes=spy_closes,
        exclusions=exclusions,
        n_train_sessions=len(prices),
    )


def engine_weights(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The registered engine's weight trajectory for a signal panel:
    target weights (signal_to_weights, weekly rebalance, ffill) and held
    weights (1-bar lag + 5-day min hold). Identical to run_v2_backtest."""
    cfg = BacktestConfig(
        cost_bps=10.0, long_only=True,
        max_positions=V2_CFG["max_positions"],
        rebalance_every=V2_CFG["rebalance_every"],
        min_hold_days=V2_CFG["min_hold_days"],
    )
    target = signal_to_weights(panel, cfg)
    held = _enforce_min_hold(_lag_weights(target), V2_CFG["min_hold_days"])
    return target, held


def _daily_selection(panel_row: pd.Series, max_positions: int) -> list[str]:
    """One day's selection, replicating backtest.signal_to_weights'
    long-only branch EXACTLY — including its all-zero case: when no
    signal is positive, the top-N by value are still selected (they
    carry weight 1/N each). A test asserts this matches the engine's
    target weights on every rebalance day."""
    valid = panel_row.dropna()
    if valid.empty:
        return []
    top = valid.nlargest(min(max_positions, len(valid)))
    if (top > 0).any():
        top = top[top > 0]
    if top.empty:
        return []
    return list(top.index)


def fresh_selections(panel: pd.DataFrame) -> dict[pd.Timestamp, set[str]]:
    """For each rebalance day (every 5th session, same grid as the
    engine), the freshly selected symbols. A re-entry after an SMA exit
    is only allowed via one of these."""
    out: dict[pd.Timestamp, set[str]] = {}
    for i, day in enumerate(panel.index):
        if i % V2_CFG["rebalance_every"] == 0:
            out[day] = set(_daily_selection(panel.loc[day], V2_CFG["max_positions"]))
    return out


def rebalance_days(sessions: pd.DatetimeIndex) -> set[pd.Timestamp]:
    return {day for i, day in enumerate(sessions) if i % V2_CFG["rebalance_every"] == 0}


# --------------------------------------------------------------------------
# Step 0 redundancy proof (entry gate) — §1b of the design doc
# --------------------------------------------------------------------------

def entry_gate_binding_count(panels: TrainPanels, held: pd.DataFrame) -> dict:
    """Count baseline entries where the entry-timing gate would bind:
    an entry at the open of session t whose latest completed close
    (close_{t-1}) is at or below that session's SMA50. The design doc
    claims this is 0 by construction (selection requires close > SMA50).
    Entries decided by the engine's all-zero top-N picks are counted
    separately — they are NOT buy-signal selections, so the gate's
    "wait while the signal remains valid" does not apply to them; they
    are reported, not hidden."""
    fresh = fresh_selections(panels.signal)
    # A selection is signal-driven if any selected symbol has a positive
    # panel value that day (i.e. a real buy signal existed).
    binding = 0
    all_zero_entries = 0
    checked = 0
    details: list[dict] = []
    prev_held = pd.Series(0.0, index=held.columns)
    for t in held.index:
        row = held.loc[t].fillna(0.0)
        entered = [s for s in held.columns if prev_held[s] == 0 and row[s] > 0]
        prev_held = row
        if not entered:
            continue
        prev_day = panels.sessions[panels.sessions.get_loc(t) - 1] if panels.sessions.get_loc(t) > 0 else None
        if prev_day is None:
            continue
        # Was the selection that produced this entry signal-driven?
        # The entry at t is decided by the last rebalance day <= t-1.
        sel_day = max(d for d in fresh if d <= prev_day)
        signal_driven = any(panels.signal.loc[sel_day, s] > 0 for s in fresh[sel_day])
        for s in entered:
            checked += 1
            close_prev = panels.closes.loc[prev_day, s]
            sma_prev = panels.sma50.loc[prev_day, s]
            if pd.isna(close_prev) or pd.isna(sma_prev):
                continue
            if close_prev <= sma_prev:
                if signal_driven:
                    binding += 1
                    details.append({"symbol": s, "entry": str(t.date()),
                                    "prev_close": round(float(close_prev), 2),
                                    "prev_sma50": round(float(sma_prev), 2)})
                else:
                    all_zero_entries += 1
    return {"entries_checked": checked, "binding": binding,
            "all_zero_engine_entries": all_zero_entries, "details": details}


# --------------------------------------------------------------------------
# Position-level simulation (open fills, same for baseline and variant)
# --------------------------------------------------------------------------

@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None       # baseline | sma_cross | window_end
    weight: float = 0.0                  # target weight at entry
    shares: float = 0.0                  # actual shares held (exact P&L)
    min_hold_override: bool = False      # SMA exit fired inside min-hold
    whipsaw: bool | None = None
    baseline_exit_price: float | None = None  # counterfactual (variant only)
    baseline_exit_date: str | None = None
    reentry_kind: str | None = None  # "signal" | "all_zero" if the entry followed an SMA-block clearing


@dataclass
class SimResult:
    equity_curve: pd.Series             # portfolio value, indexed by session
    daily_returns: pd.Series
    daily_exposure: pd.Series           # actual gross exposure (fraction of PV) per session
    trades: list[Trade] = field(default_factory=list)
    timing_exits: list[Trade] = field(default_factory=list)
    avg_daily_turnover: float = 0.0
    avg_gross_exposure: float = 0.0
    time_invested: float = 0.0
    n_entries: int = 0
    n_baseline_exits: int = 0
    n_sma_exits: int = 0
    n_min_hold_overrides: int = 0
    n_insufficient: int = 0             # symbols/days with no SMA50
    n_missing_open: int = 0             # trades skipped: no open price
    n_block_cleared_signal: int = 0     # SMA blocks cleared by a positive-signal selection
    n_block_cleared_all_zero: int = 0   # SMA blocks cleared by an all-zero engine selection
    n_block_refused_all_zero: int = 0   # strict mode: all-zero selections that did NOT clear a block
    n_reentries_signal: int = 0         # re-entries after a signal-cleared block
    n_reentries_all_zero: int = 0       # re-entries after an all-zero-cleared block
    total_return: float = 0.0
    max_drawdown: float = 0.0


def _cross_fired(closes: pd.Series, sma50: pd.Series, day: pd.Timestamp) -> bool:
    """Cross known at the close of `day`: close_{day-1} >= sma50_{day-1}
    AND close_day < sma50_day. Returns False on insufficient history."""
    loc = closes.index.get_loc(day)
    if loc < 1:
        return False
    prev, cur = closes.index[loc - 1], day
    c_prev, s_prev = closes.loc[prev], sma50.loc[prev]
    c_cur, s_cur = closes.loc[cur], sma50.loc[cur]
    if pd.isna(c_prev) or pd.isna(s_prev) or pd.isna(c_cur) or pd.isna(s_cur):
        return False
    return bool(c_prev >= s_prev and c_cur < s_cur)


def _is_whipsaw(closes: pd.Series, sma50: pd.Series, cross_day: pd.Timestamp,
                sessions: pd.DatetimeIndex, n: int = 5) -> bool:
    """Whipsaw (frozen): any close in the 5 sessions after the cross
    session is at or above that session's SMA50."""
    loc = sessions.get_loc(cross_day)
    for k in range(1, n + 1):
        if loc + k >= len(sessions):
            break
        day = sessions[loc + k]
        c, s = closes.loc[day], sma50.loc[day]
        if pd.notna(c) and pd.notna(s) and c >= s:
            return True
    return False


def simulate(
    panels: TrainPanels,
    held: pd.DataFrame,
    mode: str,
    cost_bps: float,
    start_capital: float = 1000.0,
    entry_delay_sessions: int = 0,
    reentry_mode: str = "engine",
) -> SimResult:
    """Replay the baseline held-weight trajectory with open fills.

    mode='baseline': exactly the engine's trajectory (entries/exits when
    held weight changes). mode='exit_timing': additionally force a
    holding to 0 at the open after an SMA50 cross, overriding min-hold,
    and block re-entry until a fresh engine selection.

    entry_delay_sessions (sensitivity only, default 0): when the baseline
    selects a symbol at session t, the entry executes at the open of
    t + delay instead — and only if the baseline still holds the symbol
    then (held[t+delay] > 0); otherwise the entry is dropped (the
    opportunity passed). Exits are unchanged.

    reentry_mode (exit_timing only): "engine" clears an SMA block on ANY
    fresh engine selection (the engine's top-5, including its all-zero
    picks on days with no positive signal); "strict_signal" clears it
    only on a fresh selection WITH a positive v2 signal for that symbol
    — the rule the design doc's §2 literally states. The engine mode is
    the faithful replay of the baseline's selections; the strict mode is
    the doc-literal rule. They are reported separately.
    """
    assert mode in ("baseline", "exit_timing")
    assert reentry_mode in ("engine", "strict_signal")
    sessions = panels.sessions
    fresh = fresh_selections(panels.signal)
    rebal = rebalance_days(sessions)
    n = len(sessions)

    cash = start_capital
    positions: dict[str, dict] = {}   # symbol -> {shares, entry_date, entry_price, weight, entry_idx}
    blocked: dict[str, pd.Timestamp] = {}  # symbol -> SMA-exit date (cleared by fresh selection)
    pending: dict[str, int] = {}  # symbol -> due session index (entry delay)
    trades: list[Trade] = []
    open_trades: dict[str, Trade] = {}
    equity = pd.Series(np.nan, index=sessions, dtype=float)
    exposure = pd.Series(0.0, index=sessions, dtype=float)
    prev_actual_w = pd.Series(0.0, index=held.columns)
    turnover_sum = 0.0
    exposure_sum = 0.0
    invested_days = 0
    n_insufficient = 0
    n_missing_open = 0
    n_block_cleared_signal = 0
    n_block_cleared_all_zero = 0
    n_block_refused_all_zero = 0  # strict mode: all-zero selections that did NOT clear a block
    n_reentries_signal = 0
    n_reentries_all_zero = 0
    clear_kind: dict[str, str] = {}  # symbol -> "signal" | "all_zero" (how its block cleared)

    def price_at(t: pd.Timestamp, s: str, col: str) -> float:
        """Open price, falling back to the last available close when the
        open is missing (a missing bar must not zero a position's value)."""
        frame = panels.opens if col == "open" else panels.closes
        v = frame.loc[t, s]
        if pd.notna(v):
            return float(v)
        prior = panels.closes[s].loc[:t].dropna()
        return float(prior.iloc[-1]) if not prior.empty else float("nan")

    def pv_at_open(t: pd.Timestamp) -> float:
        return cash + sum(
            pos["shares"] * price_at(t, s, "open") for s, pos in positions.items()
        )

    for i, t in enumerate(sessions):
        # --- information through the latest completed close (t-1) ---
        prev_day = sessions[i - 1] if i > 0 else None
        # (a) clear SMA blocks via fresh selections on the latest completed
        #     session: a selection on day d clears the block for entries
        #     at the open of d+1 (the lag). Only selections AFTER the exit.
        if prev_day is not None and prev_day in fresh:
            for s in list(blocked):
                if s in fresh[prev_day] and blocked[s] < prev_day:
                    # HOW the block clears depends on reentry_mode.
                    # "engine": any fresh engine selection clears it
                    # (including the all-zero top-5 pick). "strict_signal":
                    # only a selection with a positive v2 signal clears
                    # it (the design doc's §2 literal rule); an all-zero
                    # selection leaves the block in place (counted).
                    sig = float(panels.signal.loc[prev_day, s]) > 0
                    if not sig and reentry_mode == "strict_signal":
                        n_block_refused_all_zero += 1
                        continue
                    if sig:
                        n_block_cleared_signal += 1
                        clear_kind[s] = "signal"
                    else:
                        n_block_cleared_all_zero += 1
                        clear_kind[s] = "all_zero"
                    del blocked[s]
        # (b) SMA crosses known at the close of prev_day -> exit at open t.
        sma_exits: set[str] = set()
        if mode == "exit_timing" and prev_day is not None:
            for s in list(positions):
                if _cross_fired(panels.closes[s], panels.sma50[s], prev_day):
                    sma_exits.add(s)
                elif pd.isna(panels.sma50[s].loc[prev_day]):
                    n_insufficient += 1
        # --- target weights for today (baseline trajectory) ---
        target_w = held.loc[t].fillna(0.0)
        # --- execute exits at today's open ---
        for s in list(positions):
            baseline_exit = target_w[s] == 0
            sma_exit = s in sma_exits
            if not (baseline_exit or sma_exit):
                continue
            open_px = panels.opens.loc[t, s]
            if pd.isna(open_px):
                n_missing_open += 1
                continue  # cannot fill: hold (recorded, counted)
            pos = positions.pop(s)
            notional = pos["shares"] * open_px
            cost = notional * cost_bps / 1e4
            cash += notional - cost
            tr = open_trades.pop(s)
            tr.exit_date = str(t.date())
            tr.exit_price = round(float(open_px), 4)
            tr.exit_reason = "sma_cross" if sma_exit else "baseline"
            if sma_exit:
                tr.min_hold_override = (i - pos["entry_idx"]) < V2_CFG["min_hold_days"]
                tr.whipsaw = _is_whipsaw(panels.closes[s], panels.sma50[s], prev_day, sessions)
                blocked[s] = t
            trades.append(tr)
        # --- execute entries at today's open ---
        pv_open = pv_at_open(t)

        def _enter(s: str, w: float, day_idx: int) -> bool:
            nonlocal cash, n_missing_open
            open_px = panels.opens.loc[t, s]
            if pd.isna(open_px):
                n_missing_open += 1
                return False
            notional = w * pv_open
            cost = notional * cost_bps / 1e4
            if cash < notional + cost:
                # Cannot afford the full target: size to available cash.
                notional = max(cash - cost, 0.0)
                if notional <= 0:
                    return False
            shares = notional / open_px
            cash -= notional + cost
            positions[s] = {"shares": shares, "entry_date": t, "entry_price": float(open_px),
                            "weight": w, "entry_idx": day_idx}
            tr = Trade(symbol=s, entry_date=str(t.date()), entry_price=round(float(open_px), 4),
                       weight=w, shares=shares)
            open_trades[s] = tr
            return True

        def _count_reentry(s: str) -> None:
            """Attribute a re-entry to how the symbol's SMA block cleared.
            Tags the trade so the run can separate variant-only re-entries
            from entries the baseline makes on the same day."""
            nonlocal n_reentries_signal, n_reentries_all_zero
            kind = clear_kind.pop(s, None)
            if kind is None:
                return
            open_trades[s].reentry_kind = kind
            if kind == "signal":
                n_reentries_signal += 1
            elif kind == "all_zero":
                n_reentries_all_zero += 1

        # Due delayed entries first: execute only if the baseline still
        # holds the symbol today and it is not blocked; else drop.
        if entry_delay_sessions > 0:
            for s in [s for s, due in pending.items() if due <= i]:
                del pending[s]
                if s in positions or s in blocked or float(target_w[s]) <= 0:
                    clear_kind.pop(s, None)  # opportunity passed
                    continue
                if _enter(s, float(target_w[s]), i):
                    _count_reentry(s)
        for s in held.columns:
            w = float(target_w[s])
            if w <= 0 or s in positions or s in blocked:
                if w <= 0:
                    clear_kind.pop(s, None)  # no longer wanted
                continue
            if entry_delay_sessions > 0:
                if s not in pending:
                    pending[s] = i + entry_delay_sessions
                continue
            if _enter(s, w, i):
                _count_reentry(s)
        # --- actual weights after today's trades (open prices) ---
        actual_w = pd.Series(0.0, index=held.columns)
        for s, pos in positions.items():
            actual_w[s] = pos["shares"] * price_at(t, s, "open") / pv_open if pv_open > 0 else 0.0
        # Turnover is on ACTUAL weight changes (same definition as the
        # engine's, but measured on what was really traded — so the
        # variant's SMA exits count their turnover).
        turnover_sum += float((actual_w - prev_actual_w).abs().sum())
        prev_actual_w = actual_w
        exposure.loc[t] = float(actual_w.sum())
        # --- mark to market at today's close ---
        pv_close = cash + sum(
            pos["shares"] * price_at(t, s, "close") for s, pos in positions.items()
        )
        equity.loc[t] = pv_close
        exposure_sum += float(actual_w.sum())
        if float(actual_w.sum()) > 0:
            invested_days += 1

    # --- window end: mark open positions (no exit trade, no cost) ---
    last = sessions[-1]
    for s, tr in open_trades.items():
        tr.exit_date = str(last.date())
        tr.exit_price = round(float(panels.closes.loc[last, s]), 4) if pd.notna(panels.closes.loc[last, s]) else None
        tr.exit_reason = "window_end"
        trades.append(tr)

    equity = equity.ffill()
    daily_returns = equity.pct_change().fillna(0.0)
    # drop the first session's return: no prior close (matches the engine
    # dropping the NaN first fwd return).
    daily_returns.iloc[0] = 0.0
    total_return = float(equity.iloc[-1] / start_capital - 1)
    dd = float((equity / equity.cummax() - 1).min())
    timing = [t for t in trades if t.exit_reason == "sma_cross"]
    return SimResult(
        equity_curve=equity,
        daily_returns=daily_returns,
        daily_exposure=exposure,
        trades=trades,
        timing_exits=timing,
        avg_daily_turnover=round(turnover_sum / n, 4),
        avg_gross_exposure=round(exposure_sum / n, 4),
        time_invested=round(invested_days / n, 4),
        n_entries=sum(1 for t in trades),
        n_baseline_exits=sum(1 for t in trades if t.exit_reason == "baseline"),
        n_sma_exits=len(timing),
        n_min_hold_overrides=sum(1 for t in timing if t.min_hold_override),
        n_insufficient=n_insufficient,
        n_missing_open=n_missing_open,
        n_block_cleared_signal=n_block_cleared_signal,
        n_block_cleared_all_zero=n_block_cleared_all_zero,
        n_block_refused_all_zero=n_block_refused_all_zero,
        n_reentries_signal=n_reentries_signal,
        n_reentries_all_zero=n_reentries_all_zero,
        total_return=round(total_return, 4),
        max_drawdown=round(dd, 4),
    )


# --------------------------------------------------------------------------
# Benchmarks, sensitivities, uncertainty
# --------------------------------------------------------------------------

def exposure_matched_benchmark(panels: TrainPanels,
                               daily_exposure: pd.Series) -> dict:
    """SPY at the variant's own daily gross exposure (frozen construction):
    daily return = exposure_t x SPY return_t, no costs, same window."""
    spy_rets = panels.spy_closes.pct_change().fillna(0.0)
    bench_rets = daily_exposure.reindex(spy_rets.index).fillna(0.0) * spy_rets
    eq = (1 + bench_rets).cumprod()
    return {
        "total_return": round(float(eq.iloc[-1] - 1), 4),
        "max_drawdown": round(float((eq / eq.cummax() - 1).min()), 4),
        "avg_exposure": round(float(daily_exposure.mean()), 4),
    }


def _matched_exit_effect_records(baseline: SimResult, variant: SimResult) -> list[dict]:
    """One record per timing exit that has a baseline counterfactual price
    (same symbol + entry date; the baseline's eventual exit or window-end
    mark). Exits whose baseline holding is still open at window end have
    no counterfactual price and are excluded — count them separately."""
    base_by_key = {}
    for tr in baseline.trades:
        base_by_key[(tr.symbol, tr.entry_date)] = tr
    records = []
    for tr in variant.timing_exits:
        base = base_by_key.get((tr.symbol, tr.entry_date))
        if base is None or base.exit_price is None or tr.exit_price is None:
            continue
        tr.baseline_exit_price = base.exit_price
        tr.baseline_exit_date = base.exit_date
        effect = (tr.exit_price - base.exit_price) * tr.shares
        records.append({
            "symbol": tr.symbol,
            "entry_date": tr.entry_date,
            "entry_price": tr.entry_price,
            "timing_exit_date": tr.exit_date,
            "timing_exit_price": tr.exit_price,
            "baseline_exit_date": base.exit_date,
            "baseline_exit_price": base.exit_price,
            "baseline_exit_reason": base.exit_reason,
            "effect_usd": round(float(effect), 2),
            "whipsaw": bool(tr.whipsaw),
            "min_hold_override": bool(tr.min_hold_override),
        })
    return records


def _timing_effect_breakdown(baseline: SimResult, variant: SimResult) -> dict:
    """Per timing exit: timing exit price vs the baseline's eventual exit
    price for the SAME holding (same symbol + entry date). The baseline
    position is still open at the timing exit (the variant exits earlier),
    so its eventual exit (or window-end mark) is the counterfactual."""
    records = _matched_exit_effect_records(baseline, variant)
    avoided = sum(r["effect_usd"] for r in records if r["effect_usd"] > 0)
    forfeited = sum(r["effect_usd"] for r in records if r["effect_usd"] < 0)
    return {
        "losses_avoided_usd": round(avoided, 2),
        "gains_forfeited_usd": round(forfeited, 2),
        "n_avoided": sum(1 for r in records if r["effect_usd"] > 0),
        "n_forfeited": sum(1 for r in records if r["effect_usd"] < 0),
        "net_price_effect_usd": round(avoided + forfeited, 2),
        "n_unmatched": len(variant.timing_exits) - len(records),
    }


def sensitivity_analysis(panels: TrainPanels, held: pd.DataFrame,
                         baseline: SimResult, variant: SimResult,
                         cost_bps: float, reentry_mode: str = "strict_signal") -> dict:
    """Frozen sensitivities (design doc §6):
    (a) ex-largest-contributor — drop the single largest positive
        timing-exit price effect and recompute the net price effect.
    (b) entry delayed one additional session — re-run the exit-timing
        variant with every entry one session later (entries only if the
        baseline still holds the symbol then), SAME re-entry mode as the
        primary variant; report its net timing effect vs the same baseline.
    Also the baseline's own exposure-matched benchmark, so the variant's
    gap to SPY-at-the-same-exposure can be compared with the baseline's.
    """
    records = _matched_exit_effect_records(baseline, variant)
    effects = [r["effect_usd"] for r in records]
    net = sum(effects)
    largest = max(effects) if effects else 0.0
    delayed = simulate(panels, held, "exit_timing", cost_bps,
                       entry_delay_sessions=1, reentry_mode=reentry_mode)
    return {
        "ex_largest_contributor": {
            "largest_effect_usd": round(float(largest), 2),
            "net_price_effect_usd": round(float(net), 2),
            "net_ex_largest_usd": round(float(net - largest), 2),
            "n_effects": len(effects),
        },
        "entry_delayed_one_session": {
            "total_return": delayed.total_return,
            "max_drawdown": delayed.max_drawdown,
            "avg_daily_turnover": delayed.avg_daily_turnover,
            "n_entries": delayed.n_entries,
            "n_sma_exits": delayed.n_sma_exits,
            "net_timing_effect_pp": round(
                (delayed.total_return - baseline.total_return) * 100, 2),
        },
        "baseline_exposure_matched_benchmark":
            exposure_matched_benchmark(panels, baseline.daily_exposure),
    }


def bootstrap_timing_effect(variant: SimResult, baseline: SimResult,
                            n_draws: int = 1000, seed: int = 7) -> dict:
    """Trade-level bootstrap over timing-exit outcomes (frozen): resample
    the per-exit net price effects with replacement; the 5th/95th
    percentile interval is INDICATIVE — trades overlap in calendar time,
    so it overstates independence. Not a p-value."""
    rng = np.random.default_rng(seed)
    effects = []
    base_by_key = {(t.symbol, t.entry_date): t for t in baseline.trades}
    for tr in variant.timing_exits:
        base = base_by_key.get((tr.symbol, tr.entry_date))
        if base is None or base.exit_price is None or tr.exit_price is None:
            continue
        effects.append((tr.exit_price - base.exit_price) * tr.shares)
    if not effects:
        return {"n_effects": 0, "p5": None, "p95": None, "mean": None}
    effects = np.array(effects)
    draws = rng.choice(effects, size=(n_draws, len(effects)), replace=True).sum(axis=1)
    return {
        "n_effects": len(effects),
        "mean": round(float(draws.mean()), 2),
        "p5": round(float(np.percentile(draws, 5)), 2),
        "p95": round(float(np.percentile(draws, 95)), 2),
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _variant_summary(variant: SimResult, baseline: SimResult) -> dict:
    """Headline numbers for one exit-timing variant, including the
    variant-only re-entry split by clearing kind (see run_experiment)."""
    base_keys = {(t.symbol, t.entry_date) for t in baseline.trades}
    extra_re = {"signal": 0, "all_zero": 0, "untagged": 0}
    for t in variant.trades:
        if (t.symbol, t.entry_date) in base_keys:
            continue
        if t.reentry_kind == "signal":
            extra_re["signal"] += 1
        elif t.reentry_kind == "all_zero":
            extra_re["all_zero"] += 1
        else:
            extra_re["untagged"] += 1
    return {
        "total_return": variant.total_return,
        "max_drawdown": variant.max_drawdown,
        "avg_daily_turnover": variant.avg_daily_turnover,
        "avg_gross_exposure": variant.avg_gross_exposure,
        "time_invested": variant.time_invested,
        "n_entries": variant.n_entries,
        "n_baseline_exits": variant.n_baseline_exits,
        "n_sma_exits": variant.n_sma_exits,
        "n_min_hold_overrides": variant.n_min_hold_overrides,
        "n_whipsaws": sum(1 for t in variant.timing_exits if t.whipsaw),
        "n_insufficient": variant.n_insufficient,
        "n_missing_open": variant.n_missing_open,
        "n_block_cleared_signal": variant.n_block_cleared_signal,
        "n_block_cleared_all_zero": variant.n_block_cleared_all_zero,
        "n_block_refused_all_zero": variant.n_block_refused_all_zero,
        "n_reentries_signal": variant.n_reentries_signal,
        "n_reentries_all_zero": variant.n_reentries_all_zero,
        "extra_reentries_by_clearing_kind": extra_re,
        "net_timing_effect_pp": round((variant.total_return - baseline.total_return) * 100, 2),
    }


def run_experiment(cost_bps: float = 10.0) -> dict:
    cfg = TradingConfig.from_env()
    data = load_split_adjusted_bars()
    panels = build_train_panels(data, cfg)
    target, held = engine_weights(panels.signal)
    redundancy = entry_gate_binding_count(panels, held)
    baseline = simulate(panels, held, "baseline", cost_bps)
    # PRIMARY variant: strict_signal re-entry — the rule the design doc's
    # §2 literally states (block clears only on a positive-signal
    # selection). The engine-faithful variant (any fresh engine selection
    # clears the block, including all-zero picks) is reported alongside
    # for comparison; it is NOT the frozen rule.
    variant = simulate(panels, held, "exit_timing", cost_bps,
                       reentry_mode="strict_signal")
    variant_engine = simulate(panels, held, "exit_timing", cost_bps,
                              reentry_mode="engine")
    # Exposure-matched benchmark uses the PRIMARY variant's daily exposure.
    bench = exposure_matched_benchmark(panels, variant.daily_exposure)
    breakdown = _timing_effect_breakdown(baseline, variant)
    records = _matched_exit_effect_records(baseline, variant)
    boot = bootstrap_timing_effect(variant, baseline)
    sens = sensitivity_analysis(panels, held, baseline, variant, cost_bps)
    whipsaws = [t for t in variant.timing_exits if t.whipsaw]
    return {
        "experiment_id": "exp-sma50-timing-v1",
        "window": {"train_start": TRAIN_START, "train_end": TRAIN_END,
                   "n_sessions": panels.n_train_sessions},
        "universe": {"symbols": len(panels.symbols), "exclusions": panels.exclusions},
        "cost_bps": cost_bps,
        "redundancy_entry_gate": redundancy,
        "baseline": {
            "total_return": baseline.total_return,
            "max_drawdown": baseline.max_drawdown,
            "avg_daily_turnover": baseline.avg_daily_turnover,
            "avg_gross_exposure": baseline.avg_gross_exposure,
            "time_invested": baseline.time_invested,
            "n_entries": baseline.n_entries,
            "n_baseline_exits": baseline.n_baseline_exits,
            "n_missing_open": baseline.n_missing_open,
        },
        "exit_timing": _variant_summary(variant, baseline),
        "exit_timing_engine_faithful": _variant_summary(variant_engine, baseline),
        "net_timing_effect_pp": round((variant.total_return - baseline.total_return) * 100, 2),
        "exposure_matched_benchmark": bench,
        "timing_exit_breakdown": breakdown,
        "timing_exit_records": records,
        "bootstrap_timing_effect_usd": boot,
        "sensitivities": sens,
        "entry_counts": {"better": 0, "worse": 0, "missed_winners": 0,
                         "avoided_losers": 0, "note": "entry timing not built; redundant by §1b"},
    }


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="exp-sma50-timing-v1: 50-session SMA exit timing on the train window.")
    parser.add_argument("--costs", nargs="+", type=float, default=list(COSTS_BPS))
    parser.add_argument("--out-dir", default=str(REPO / "experiments" / "results"))
    args = parser.parse_args()
    results = {}
    for cost in args.costs:
        print(f"--- cost {cost} bps ---")
        res = run_experiment(cost)
        results[f"cost_{int(cost)}bps"] = res
        print(json.dumps({k: v for k, v in res.items()
                          if k in ("baseline", "exit_timing", "net_timing_effect_pp",
                                   "redundancy_entry_gate", "exposure_matched_benchmark")}, indent=1))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "exp-sma50-timing-v1.json", "w") as fh:
        json.dump(results, fh, indent=1, default=str)
    print(f"wrote {out_dir / 'exp-sma50-timing-v1.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
