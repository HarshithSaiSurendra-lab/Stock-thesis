#!/usr/bin/env python3
"""Sprint step 3 ($0): calibrate pattern-based design variants on TRAIN ONLY.

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.
No API calls are made anywhere in this script ($0 spend).

Candidates (all derived from the step 1+2 train-only findings):
  C0  control: v2 turnover-cut panel, weekly rebalance + 5-day min hold
      (the registered exp-technical-v2 design, reproduced on train).
  C1a reversion: cross-sectional rank score z(rvol_20) - z(ret_63)
      - z(drawdown_63), top-5 long, weekly rebalance, min hold 10.
  C1b reversion + momentum-reversal: C1a - z(mom_126_21).
  C1c reversion + oversold: C1b - z(rsi_14).
  C2  LLM long-horizon: panel = current filing's Haiku score (existing
      Phase 2 scores, $0), top-5 among score > 0, weekly rebalance,
      min hold 10. Tests the LLM at its own 3-12mo horizon.
  C3  v4 + LLM veto + long hold: v4+LLM panel (existing scores, $0),
      entries blocked where the LLM contribution < 0, weekly rebalance,
      min hold 10.
  C4  v4 long-hold control: v4 panel, weekly rebalance, min hold 10.
  C6  textual delta: filing-anchored score z(tone_delta) + z(jaccard)
      - z(len_ratio), carried forward until the next filing, top-5,
      weekly rebalance, min hold 10. All $0 features from the excerpts.

Selection is TRAIN ONLY (walk-forward folds within train, n_splits=4).
The frozen holdout is NEVER evaluated in this script. The single best
design (by train net10 + wf_median_sharpe + wf_positive_folds) goes to
step 4 (registration + one holdout confirmation).

Trial accounting: each (candidate x config) evaluated here is a trial
for the Deflated Sharpe count. Prior n_trials = 26 (freshness run).

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python calibrate_sprint.py
    (writes experiments/results/sprint-calibration.json)
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import TradingConfig
from research_runner import (
    build_signal_variants,
    build_v2_signal,
    evaluate_variants,
    slice_research_window,
    _feature_panels,
)
from run_real_backtest import _load_cached_bars
import run_llm_backtest as p2

REPO = Path(__file__).parent
OUT_PATH = REPO / "experiments" / "results" / "sprint-calibration.json"

RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63
TRAIN_END_DATE = "2026-08-17"

WORD_RE = re.compile(r"[a-z]{3,}")
POS_WORDS = {"growth", "strong", "record", "expand", "improve", "increase",
             "robust", "resilient", "opportunity", "momentum", "outperform"}
NEG_WORDS = {"decline", "risk", "uncertain", "challenge", "weak", "decrease",
             "pressure", "headwind", "adverse", "loss", "shortfall", "volatile"}


def zscore_xsec(panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score per day (mean 0, sd 1 across symbols)."""
    m = panel.mean(axis=1)
    s = panel.std(axis=1).replace(0.0, np.nan)
    return panel.sub(m, axis=0).div(s, axis=0)


def tone(text: str) -> float:
    toks = WORD_RE.findall(text.lower())
    if not toks:
        return 0.0
    pos = sum(1 for t in toks if t in POS_WORDS)
    neg = sum(1 for t in toks if t in NEG_WORDS)
    return (pos - neg) / max(len(toks), 1)


def main() -> int:
    print("=== Sprint step 3: calibrate pattern-based designs (TRAIN ONLY, $0) ===",
          flush=True)
    data = _load_cached_bars()
    if data is None:
        raise RuntimeError("no cached bars; .cache/backtest_data is empty")
    cfg = TradingConfig.from_env()
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, RESEARCH_START, RESEARCH_END)
    window_days = prices.index
    holdout_start = window_days[-HOLDOUT_DAYS_N]
    train_days = window_days[window_days < holdout_start]
    train_prices = prices.loc[train_days]
    print(f"train {train_days[0].date()}..{train_days[-1].date()} ({len(train_days)} days)",
          flush=True)

    # --- feature frames (train only) -------------------------------------
    _, features = _feature_panels(data)
    feats = {s: f.loc[train_days] for s, f in features.items() if s in train_prices.columns}
    symbols = sorted(feats)
    panels = {}
    for name in ("rvol_20", "ret_63", "mom_126_21", "drawdown_63", "rsi_14"):
        panels[name] = pd.DataFrame({s: feats[s][name] for s in symbols})

    z = {name: zscore_xsec(panels[name]) for name in panels}
    # C1 reversion rank scores (higher = more mean-reversion-like)
    panels["C1a_reversion"] = z["rvol_20"] - z["ret_63"] - z["drawdown_63"]
    panels["C1b_reversion_mom"] = panels["C1a_reversion"] - z["mom_126_21"]
    panels["C1c_reversion_rsi"] = panels["C1b_reversion_mom"] - z["rsi_14"]

    # --- C2: LLM long-horizon panel from EXISTING Phase 2 Haiku scores ---
    filings = p2.load_edgar_filings()
    haiku = p2._load_scores(REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json")
    sonnet = p2._load_scores(REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-sonnet.json")
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
    panels["C2_llm_long_horizon"] = llm_panel
    print(f"C2 panel: {int((llm_panel > 0).sum().sum())} positive-score cells, "
          f"{int((llm_panel != 0).sum().sum())} scored cells", flush=True)

    # --- C3: v4+LLM with veto (block entries where LLM contribution < 0) ---
    contrib, applied = p2.build_contribution_panel(
        current, haiku, sonnet)
    adjusted = p2.build_adjusted_panels(variants, contrib)
    v4_llm = adjusted["v4_regime_quality_risk_llm"].loc[train_days]
    veto = v4_llm.copy()
    veto[contrib.loc[train_days] < 0] = 0.0
    panels["C3_v4_llm_veto"] = veto
    print(f"C3 veto blocks {int((contrib.loc[train_days] < 0).sum().sum())} "
          f"negative-contribution cells", flush=True)

    # --- C4: v4 long-hold control -----------------------------------------
    panels["C4_v4_long_hold"] = variants["v4_regime_quality_risk"].loc[train_days]

    # --- C0: v2 turnover-cut control --------------------------------------
    _, v2_panel = build_v2_signal(data, cfg)
    _, v2_sliced = slice_research_window(
        prices, {"v2": v2_panel}, RESEARCH_START, RESEARCH_END)
    panels["C0_v2_turnover_cut"] = v2_sliced["v2"].loc[train_days]

    # --- C6: textual delta panel (filing-anchored, $0) --------------------
    tf = filings[filings["filing_date"] <= pd.Timestamp(TRAIN_END_DATE)].copy()
    tf = tf.sort_values(["ticker", "filing_date"])
    tdays = train_days
    delta_by_symbol: dict[str, pd.DataFrame] = {}
    for sym, group in tf.groupby("ticker"):
        rows = []
        prev_toks = prev_tone = prev_len = None
        for _, row in group.iterrows():
            toks = set(WORD_RE.findall(row["excerpt_text"].lower()))
            tn = tone(row["excerpt_text"])
            if prev_toks is not None:
                union = toks | prev_toks
                jac = len(toks & prev_toks) / len(union) if union else np.nan
                rows.append({
                    "filing_date": row["filing_date"],
                    "jaccard": jac,
                    "len_ratio": len(row["excerpt_text"]) / max(prev_len, 1),
                    "tone_delta": tn - prev_tone,
                })
            prev_toks, prev_tone, prev_len = toks, tn, len(row["excerpt_text"])
        if rows:
            delta_by_symbol[sym] = pd.DataFrame(rows)
    # z-score each delta feature across all (filing) observations, then
    # carry the filing's combined score forward until the next filing.
    all_d = pd.concat(
        [d.assign(ticker=s) for s, d in delta_by_symbol.items()], ignore_index=True)
    for col in ("jaccard", "len_ratio", "tone_delta"):
        all_d[f"z_{col}"] = (all_d[col] - all_d[col].mean()) / all_d[col].std(ddof=1)
    all_d["delta_score"] = all_d["z_tone_delta"] + all_d["z_jaccard"] - all_d["z_len_ratio"]
    c6 = pd.DataFrame(0.0, index=train_days, columns=sorted(all_d["ticker"].unique()))
    for sym, group in all_d.groupby("ticker"):
        dates = group["filing_date"].to_numpy()
        vals = group["delta_score"].to_numpy()
        pos = np.searchsorted(dates, tdays.to_numpy(), side="right") - 1
        col = np.where(pos >= 0, vals[np.maximum(pos, 0)], 0.0)
        c6[sym] = col
    panels["C6_textual_delta"] = c6
    print(f"C6 panel: {int((c6 != 0).sum().sum())} nonzero cells "
          f"({len(all_d)} delta observations)", flush=True)

    # ------------------------------------------------------------------
    # Evaluate candidates on TRAIN (walk-forward within train).
    # Primary config: weekly rebalance (5), min hold 10. Top-2 also at
    # min hold 5 and 20.
    # ------------------------------------------------------------------
    candidates = ["C0_v2_turnover_cut", "C1a_reversion", "C1b_reversion_mom",
                  "C1c_reversion_rsi", "C2_llm_long_horizon", "C3_v4_llm_veto",
                  "C4_v4_long_hold", "C6_textual_delta"]
    results: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "spend_usd": 0.0,
        "train_window": f"{train_days[0].date()}..{train_days[-1].date()}",
        "selection_rule": "TRAIN ONLY: rank by train net10 + wf_median_sharpe + wf_positive_folds",
        "configs": {},
    }
    n_trials_new = 0
    for min_hold in (10,):
        for reb in (5,):
            key = f"reb{reb}_hold{min_hold}"
            results["configs"][key] = {}
            for name in candidates:
                panel = {name: panels[name]}
                for cost in (0, 10, 20):
                    res = evaluate_variants(
                        train_prices, panel, cost_bps=float(cost), max_positions=5,
                        rebalance_every=reb, n_splits=4, initial_capital=1000.0,
                        min_hold_days=min_hold)
                    row = res.iloc[0]
                    results["configs"][key].setdefault(name, {})[f"cost_{cost}bps"] = {
                        "total_return": float(row["total_return"]),
                        "trades": int(row["trades"]),
                        "avg_daily_turnover": float(row["avg_daily_turnover"]),
                        "sharpe": float(row["sharpe"]) if pd.notna(row["sharpe"]) else None,
                        "hit_rate": float(row["hit_rate"]) if pd.notna(row["hit_rate"]) else None,
                        "max_drawdown": float(row["max_drawdown"]) if pd.notna(row["max_drawdown"]) else None,
                        "wf_median_sharpe": float(row["wf_median_sharpe"]) if row["wf_median_sharpe"] is not None and pd.notna(row["wf_median_sharpe"]) else None,
                        "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
                    }
                    n_trials_new += 1
            print(f"\n[{key}] TRAIN net10 / wf_med_sharpe / wf+ folds / trades:", flush=True)
            ranked = sorted(
                candidates,
                key=lambda n: (results["configs"][key][n]["cost_10bps"]["total_return"]),
                reverse=True)
            for name in ranked:
                r10 = results["configs"][key][name]["cost_10bps"]
                r0 = results["configs"][key][name]["cost_0bps"]
                print(f"  {name:24s} gross {r0['total_return']*100:+7.2f}%  net10 "
                      f"{r10['total_return']*100:+7.2f}%  wf_med {r10['wf_median_sharpe']}  "
                      f"wf+ {r10['wf_positive_folds']}/4  trades {r10['trades']:5d}  "
                      f"turn {r10['avg_daily_turnover']:.3f}", flush=True)

    # Top-2 by train net10 at the primary config: also test min hold 5/20.
    primary = results["configs"]["reb5_hold10"]
    top2 = sorted(candidates, key=lambda n: primary[n]["cost_10bps"]["total_return"],
                  reverse=True)[:2]
    print(f"\ntop-2 for hold sensitivity: {top2}", flush=True)
    for min_hold in (5, 20):
        key = f"reb5_hold{min_hold}"
        results["configs"][key] = {}
        for name in top2:
            panel = {name: panels[name]}
            for cost in (0, 10, 20):
                res = evaluate_variants(
                    train_prices, panel, cost_bps=float(cost), max_positions=5,
                    rebalance_every=5, n_splits=4, initial_capital=1000.0,
                    min_hold_days=min_hold)
                row = res.iloc[0]
                results["configs"][key].setdefault(name, {})[f"cost_{cost}bps"] = {
                    "total_return": float(row["total_return"]),
                    "trades": int(row["trades"]),
                    "avg_daily_turnover": float(row["avg_daily_turnover"]),
                    "sharpe": float(row["sharpe"]) if pd.notna(row["sharpe"]) else None,
                    "hit_rate": float(row["hit_rate"]) if pd.notna(row["hit_rate"]) else None,
                    "max_drawdown": float(row["max_drawdown"]) if pd.notna(row["max_drawdown"]) else None,
                    "wf_median_sharpe": float(row["wf_median_sharpe"]) if row["wf_median_sharpe"] is not None and pd.notna(row["wf_median_sharpe"]) else None,
                    "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
                }
                n_trials_new += 1
        for name in top2:
            r10 = results["configs"][key][name]["cost_10bps"]
            r0 = results["configs"][key][name]["cost_0bps"]
            print(f"  [{key}] {name:24s} gross {r0['total_return']*100:+7.2f}%  net10 "
                  f"{r10['total_return']*100:+7.2f}%  wf_med {r10['wf_median_sharpe']}  "
                  f"wf+ {r10['wf_positive_folds']}/4  trades {r10['trades']:5d}",
                  flush=True)

    results["n_trials_new"] = n_trials_new
    results["n_trials_total_dsr"] = 26 + n_trials_new
    OUT_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nnew trials this calibration: {n_trials_new}; "
          f"DSR n_trials total: {26 + n_trials_new}", flush=True)
    print(f"report written to {OUT_PATH}", flush=True)
    print("SPEND: $0.00 (no API calls)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
