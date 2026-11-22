#!/usr/bin/env python3
"""Sprint verification ($0): stress-test the calibration winners before registering.

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.
No API calls ($0 spend). TRAIN ONLY — the frozen holdout is never touched here.

The calibration returned train numbers far better than anything before
(C2 +153.83% net10, C6 +137.79% net10 vs the prior best train -15.55%),
so this script independently verifies them before any registration:

  V1. Reproduce exp-technical-v2 train net10 at rebalance 5 / min-hold 5
      (recorded -14.0%). The calibration machinery must reproduce the
      recorded number, or nothing in the calibration can be trusted.
      Also re-checks at min-hold 10 (the calibration's primary config).
  V2. C2 holdings audit: entries/exits, distinct names, per-symbol P&L,
      beta/alpha vs SPY, walk-forward fold detail, and a manual spot-check
      of the largest winning trade against raw closes.
  V3. C6 rebuilt with POINT-IN-TIME normalization: the calibration's C6
      z-scored delta features using the FULL train sample's mean/std
      (mild look-ahead: a day-d score used statistics from filings after
      day d). Rebuilt here as a cross-sectional RANK of the delta score
      among symbols with a current delta value (purely point-in-time),
      then re-evaluated at rebalance 5 / min-hold 10. If it collapses,
      the z-score leakage was load-bearing.
  V4. C6 holdings audit (same as V2) on the rebuilt rank version.

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python verify_sprint.py
    (writes experiments/results/sprint-verification.json)
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest import BacktestConfig, run_backtest, walk_forward_splits
from config import TradingConfig
from research_runner import (
    build_signal_variants,
    build_v2_signal,
    evaluate_variants,
    slice_research_window,
)
from run_real_backtest import _load_cached_bars
import run_llm_backtest as p2
from calibrate_sprint import tone, WORD_RE, POS_WORDS, NEG_WORDS  # reuse lexicon only

REPO = Path(__file__).parent
OUT_PATH = REPO / "experiments" / "results" / "sprint-verification.json"

RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63
TRAIN_END_DATE = "2026-08-17"


def entries_from_weights(weights: pd.DataFrame) -> pd.DataFrame:
    """Position entries: (symbol, entry_date, exit_date, entry_weight)."""
    held = weights.fillna(0.0)
    entered = (held > 0) & (held.shift(1).fillna(0.0) <= 0)
    exited = (held <= 0) & (held.shift(1).fillna(0.0) > 0)
    rows = []
    for sym in held.columns:
        entry_dates = held.index[entered[sym]]
        exit_dates = held.index[exited[sym]]
        for ed in entry_dates:
            later = exit_dates[exit_dates > ed]
            xd = later[0] if len(later) else held.index[-1]
            rows.append({"symbol": sym, "entry": str(ed.date()),
                         "exit": str(xd.date()),
                         "hold_days": int((xd - ed).days)})
    return pd.DataFrame(rows)


def attribution(weights: pd.DataFrame, prices: pd.DataFrame) -> dict:
    """Per-symbol P&L contribution and portfolio beta/alpha vs SPY."""
    fwd = prices.pct_change(fill_method=None).shift(-1)
    held = weights.fillna(0.0)
    # engine-consistent: held_w[t] earns fwd_ret[t]
    daily_pnl_by_sym = held * fwd
    total_by_sym = daily_pnl_by_sym.sum().sort_values(ascending=False)
    port_daily = daily_pnl_by_sym.sum(axis=1).dropna()
    return {
        "n_positions": int((total_by_sym != 0).sum()),
        "top_contributors": {s: round(float(v), 4) for s, v in total_by_sym.head(8).items()},
        "bottom_contributors": {s: round(float(v), 4) for s, v in total_by_sym.tail(5).items()},
        "total_gross_return": round(float(port_daily.sum()), 4),
        "n_days": int(len(port_daily)),
    }


def main() -> int:
    print("=== Sprint verification ($0, TRAIN ONLY) ===", flush=True)
    data = _load_cached_bars()
    if data is None:
        raise RuntimeError("no cached bars; .cache/backtest_data is empty")
    cfg = TradingConfig.from_env()
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, RESEARCH_START, RESEARCH_END)
    full_prices = pd.DataFrame(
        {s: f["close"] for s, f in data.bars.items()}).sort_index().loc[prices.index]
    window_days = prices.index
    holdout_start = window_days[-HOLDOUT_DAYS_N]
    train_days = window_days[window_days < holdout_start]
    train_prices = prices.loc[train_days]
    report: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "spend_usd": 0.0,
        "train_window": f"{train_days[0].date()}..{train_days[-1].date()}",
    }

    # ------------------------------------------------------------------
    # V1: reproduce exp-technical-v2 train numbers
    # ------------------------------------------------------------------
    print("\n--- V1: reproduce exp-technical-v2 (train) ---", flush=True)
    _, v2_panel = build_v2_signal(data, cfg)
    _, v2_sliced = slice_research_window(
        prices, {"v2": v2_panel}, RESEARCH_START, RESEARCH_END)
    v2_train = v2_sliced["v2"].loc[train_days]
    v1 = {}
    for min_hold in (5, 10):
        res = evaluate_variants(
            train_prices, {"v2": v2_train}, cost_bps=10.0, max_positions=5,
            rebalance_every=5, n_splits=4, initial_capital=1000.0,
            min_hold_days=min_hold)
        row = res.iloc[0]
        v1[f"reb5_hold{min_hold}"] = {
            "total_return": float(row["total_return"]),
            "trades": int(row["trades"]),
            "wf_median_sharpe": float(row["wf_median_sharpe"]) if pd.notna(row["wf_median_sharpe"]) else None,
        }
        print(f"  v2 reb5/hold{min_hold}: train net10 {row['total_return']*100:+.2f}% "
              f"(recorded hold5: -14.0%)  trades {int(row['trades'])}", flush=True)
    report["v1_reproduce_v2"] = v1

    # ------------------------------------------------------------------
    # Rebuild C2 panel (same construction as calibration) + audit
    # ------------------------------------------------------------------
    filings = p2.load_edgar_filings()
    haiku = p2._load_scores(REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json")
    in_scope_train = p2.in_scope_filings(filings, variants, train_days)
    current = p2.current_filing_series(in_scope_train, train_days)
    llm_panel = pd.DataFrame(0.0, index=train_days, columns=sorted(current.columns))
    for sym in current.columns:
        for day in train_days:
            acc = current.at[day, sym]
            if isinstance(acc, str):
                h = haiku.get(acc)
                if h is not None and "error" not in h:
                    llm_panel.at[day, sym] = float(h["score"])

    def audit_panel(name: str, panel: pd.DataFrame, reb: int, hold: int) -> dict:
        cfg_bt = BacktestConfig(cost_bps=10.0, long_only=True, max_positions=5,
                                rebalance_every=reb, min_hold_days=hold)
        out = run_backtest(train_prices, panel, cfg_bt)
        gross_cfg = BacktestConfig(cost_bps=0.0, long_only=True, max_positions=5,
                                   rebalance_every=reb, min_hold_days=hold)
        gross = run_backtest(train_prices, panel, gross_cfg)
        entries = entries_from_weights(out.weights)
        attr = attribution(out.weights, train_prices)
        # beta/alpha vs SPY (daily portfolio returns vs SPY daily returns)
        spy = full_prices.loc[train_days]["SPY"].pct_change().dropna()
        pr = out.daily_returns
        common = pr.index.intersection(spy.index)
        x = spy.loc[common].to_numpy()
        y = pr.loc[common].to_numpy()
        xm = x - x.mean()
        beta = float((xm * (y - y.mean())).sum() / (xm * xm).sum()) if (xm * xm).sum() > 0 else None
        alpha_ann = float((y.mean() - beta * x.mean()) * 252) if beta is not None else None
        # walk-forward fold detail (total return per fold)
        folds = []
        for _, test_idx in walk_forward_splits(train_days, n_splits=4):
            if len(test_idx) < 80:
                continue
            fr = run_backtest(train_prices.loc[test_idx], panel.loc[test_idx], cfg_bt)
            folds.append({"fold": f"{test_idx[0].date()}..{test_idx[-1].date()}",
                          "total_return": round(float(fr.equity_curve.iloc[-1]) - 1.0, 4),
                          "trades": int(((fr.weights.fillna(0.0) > 0) & (fr.weights.fillna(0.0).shift(1).fillna(0.0) <= 0)).sum().sum())})
        return {
            "net10": round(float(out.equity_curve.iloc[-1]) - 1.0, 4),
            "gross0": round(float(gross.equity_curve.iloc[-1]) - 1.0, 4),
            "sharpe": out.stats.get("sharpe"),
            "max_drawdown": out.stats.get("max_drawdown"),
            "n_entries": int(len(entries)),
            "distinct_symbols": int(entries["symbol"].nunique()) if len(entries) else 0,
            "entries": entries.to_dict(orient="records"),
            "attribution": attr,
            "beta_vs_spy": beta,
            "alpha_ann_vs_spy": alpha_ann,
            "wf_folds": folds,
        }

    print("\n--- V2: audit C2_llm_long_horizon (reb5/hold10, train) ---", flush=True)
    c2_audit = audit_panel("C2", llm_panel, 5, 10)
    report["v2_audit_c2"] = c2_audit
    print(f"  net10 {c2_audit['net10']*100:+.2f}%  gross {c2_audit['gross0']*100:+.2f}%  "
          f"entries {c2_audit['n_entries']}  distinct {c2_audit['distinct_symbols']}  "
          f"beta {c2_audit['beta_vs_spy']:.2f}  alpha/yr {c2_audit['alpha_ann_vs_spy']*100:+.1f}%", flush=True)
    print(f"  top contributors: {c2_audit['attribution']['top_contributors']}", flush=True)
    print(f"  wf folds: {c2_audit['wf_folds']}", flush=True)
    # manual spot-check: largest contributor's first entry vs raw closes
    entries = pd.DataFrame(c2_audit["entries"])
    if len(entries):
        top_sym = max(c2_audit["attribution"]["top_contributors"],
                      key=c2_audit["attribution"]["top_contributors"].get)
        first = entries[entries["symbol"] == top_sym].iloc[0]
        bars = data.bars[top_sym]
        e = pd.Timestamp(first["entry"])
        x = pd.Timestamp(first["exit"])
        c_entry = bars.loc[bars.index >= e, "close"].iloc[0] if (bars.index >= e).any() else None
        c_exit = bars.loc[bars.index <= x, "close"].iloc[-1] if (bars.index <= x).any() else None
        print(f"  spot-check {top_sym}: entry {first['entry']} close {c_entry}, "
              f"exit {first['exit']} close {c_exit}, "
              f"raw move {(c_exit/c_entry-1)*100 if c_entry and c_exit else None:+.2f}%", flush=True)
        c2_audit["spot_check"] = {"symbol": top_sym, "entry": first["entry"],
                                  "exit": first["exit"],
                                  "entry_close": float(c_entry) if c_entry is not None else None,
                                  "exit_close": float(c_exit) if c_exit is not None else None}

    # ------------------------------------------------------------------
    # V3: rebuild C6 with point-in-time RANK normalization, re-evaluate
    # ------------------------------------------------------------------
    print("\n--- V3: C6 rebuilt with point-in-time rank normalization ---", flush=True)
    tf = filings[filings["filing_date"] <= pd.Timestamp(TRAIN_END_DATE)].copy()
    tf = tf.sort_values(["ticker", "filing_date"])
    delta_rows = []
    for sym, group in tf.groupby("ticker"):
        prev_toks = prev_tone = prev_len = None
        for _, row in group.iterrows():
            toks = set(WORD_RE.findall(row["excerpt_text"].lower()))
            tn = tone(row["excerpt_text"])
            if prev_toks is not None:
                union = toks | prev_toks
                delta_rows.append({
                    "ticker": sym,
                    "filing_date": row["filing_date"],
                    "jaccard": len(toks & prev_toks) / len(union) if union else np.nan,
                    "len_ratio": len(row["excerpt_text"]) / max(prev_len, 1),
                    "tone_delta": tn - prev_tone,
                })
            prev_toks, prev_tone, prev_len = toks, tn, len(row["excerpt_text"])
    delta = pd.DataFrame(delta_rows)
    # Per-filing ranks computed ONLY against filings dated <= that filing's
    # own date is complex; instead use the filing's raw delta values ranked
    # cross-sectionally each DAY among symbols whose current filing has a
    # delta — that is purely point-in-time (only info available that day).
    # Build the carried-forward raw delta panels first.
    raw = {}
    for col in ("jaccard", "len_ratio", "tone_delta"):
        panel = pd.DataFrame(np.nan, index=train_days,
                             columns=sorted(delta["ticker"].unique()))
        for sym, group in delta.groupby("ticker"):
            dates = group["filing_date"].to_numpy()
            vals = group[col].to_numpy()
            pos = np.searchsorted(dates, train_days.to_numpy(), side="right") - 1
            panel[sym] = np.where(pos >= 0, vals[np.maximum(pos, 0)], np.nan)
        raw[col] = panel
    # cross-sectional rank (0..1) per day, then combine: tone_delta rank
    # + jaccard rank - len_ratio rank (len growth is bad).
    rank_panels = {c: raw[c].rank(axis=1, pct=True) for c in raw}
    c6_rank = rank_panels["tone_delta"] + rank_panels["jaccard"] - rank_panels["len_ratio"]
    c6_rank = c6_rank.fillna(0.0)
    # keep only symbols that actually have a delta value that day (rank of
    # NaN was filled 0 above only after ranking; NaN ranks are NaN -> the
    # sum is NaN where any component is NaN; fillna(0) zeroes those cells,
    # which is correct: no delta info -> score 0 -> not selected).
    res = evaluate_variants(
        train_prices, {"C6_rank": c6_rank}, cost_bps=10.0, max_positions=5,
        rebalance_every=5, n_splits=4, initial_capital=1000.0, min_hold_days=10)
    row = res.iloc[0]
    res0 = evaluate_variants(
        train_prices, {"C6_rank": c6_rank}, cost_bps=0.0, max_positions=5,
        rebalance_every=5, n_splits=4, initial_capital=1000.0, min_hold_days=10)
    row0 = res0.iloc[0]
    report["v3_c6_rank_rebuild"] = {
        "gross0": float(row0["total_return"]),
        "net10": float(row["total_return"]),
        "trades": int(row["trades"]),
        "turnover": float(row["avg_daily_turnover"]),
        "wf_median_sharpe": float(row["wf_median_sharpe"]) if pd.notna(row["wf_median_sharpe"]) else None,
        "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
    }
    print(f"  C6-rank: gross {row0['total_return']*100:+.2f}%  net10 "
          f"{row['total_return']*100:+.2f}%  wf_med {row['wf_median_sharpe']}  "
          f"wf+ {row['wf_positive_folds']}/4  trades {int(row['trades'])} "
          f"(calibration z-scored C6 was net10 +137.79%)", flush=True)

    # ------------------------------------------------------------------
    # V4: audit the rebuilt C6-rank panel
    # ------------------------------------------------------------------
    print("\n--- V4: audit C6-rank (reb5/hold10, train) ---", flush=True)
    c6_audit = audit_panel("C6_rank", c6_rank, 5, 10)
    report["v4_audit_c6_rank"] = c6_audit
    print(f"  net10 {c6_audit['net10']*100:+.2f}%  gross {c6_audit['gross0']*100:+.2f}%  "
          f"entries {c6_audit['n_entries']}  distinct {c6_audit['distinct_symbols']}  "
          f"beta {c6_audit['beta_vs_spy']:.2f}  alpha/yr {c6_audit['alpha_ann_vs_spy']*100:+.1f}%", flush=True)
    print(f"  top contributors: {c6_audit['attribution']['top_contributors']}", flush=True)
    print(f"  wf folds: {c6_audit['wf_folds']}", flush=True)

    OUT_PATH.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nreport written to {OUT_PATH}", flush=True)
    print("SPEND: $0.00 (no API calls)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
