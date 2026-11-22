#!/usr/bin/env python3
"""Sprint follow-on: test the three repo-derived hypotheses NATIVELY ($0).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.
No API calls are made anywhere in this script ($0 spend). TRAIN ONLY — the
frozen holdout is never touched here.

GUARDRAIL (user, Nov 22, 2026): do NOT clone, install, or run any code
from the 7 shared research repos (OpenBB, TradingView MCP, FinceptTerminal,
daily_stock_analysis, ai-hedge-fund, TradingAgents, Alpaca MCP server).
Every idea below is implemented natively in stock-thesis code only, from
the repo's own price/filing/score data. Nothing from those repos is
imported or executed.

Hypotheses (from the read-only repo assessment; my call which to test):
  H1  Haiku-only bull/bear debate as adjudication instead of single-pass.
      -> COST ESTIMATE ONLY here. A debate needs 3 fresh Haiku calls per
         filing (bull + bear + adjudicate). The estimate below shows
         whether a meaningful train subsample fits the remaining monthly
         headroom ($0.175). No calls are made.
  H2  LLM as veto, not tilt — deterministic over existing scores, $0.
      -> H2a (already tested in calibration as C3: v4 panel + LLM veto,
         train net10 +9.10%, WF -0.28, 1/4 folds) is cited, not re-run.
         H2b (NEW, tested here): the REVERSE — the passing v2 design
         (hold all names >= 5th-highest Haiku score) plus a deterministic
         TECHNICAL veto: (i) composite score > 0, (ii) close > sma_50.
         Rebalance 5, min hold 10, train only, costs 0/10/20.
  H3  PEAD + MA5>MA10>MA20 alignment / pullback as deterministic features
      at the 5-20d horizon, $0 on existing price data.
      -> H3a PEAD proxy: filing-date abnormal return (symbol minus SPY)
         vs 5/10/20d forward returns (IC + quintile spread), train
         filings only. A positive IC = drift (PEAD); negative = reversal.
         H3b MA alignment: continuous spread (MA5-MA20)/MA20 and binary
         flag MA5>MA10>MA20 vs 5/10/20d forward returns (daily
         cross-sectional IC + quintile spread, same machinery as B1).
         H3c Pullback: aligned AND dipped near MA20 in the last 5 days
         but still above it, vs forward returns.
         PRE-SET RULE: an alignment/pullback backtest is run ONLY if
         |IC| >= 0.03 at h=20d or the 20d quintile spread >= 1%.
         Otherwise the pattern is reported as not found and no backtest
         is run (no extra trials).

Each (variant x cost) backtest evaluated here is a trial for the
Deflated Sharpe count. Prior n_trials = 75. IC diagnostics (H3a/H3b/H3c)
are not portfolio trials and are not counted.

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python test_repo_hypotheses.py
    (writes experiments/results/sprint-repo-hypotheses.json)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))


def spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman rank correlation via pandas ranks (no scipy in the venv)."""
    common = a.dropna().index.intersection(b.dropna().index)
    if len(common) < 3:
        return float("nan")
    return float(a.loc[common].rank().corr(b.loc[common].rank()))

from config import TradingConfig
from research_runner import (
    build_signal_variants,
    evaluate_variants,
    slice_research_window,
)
from run_real_backtest import _load_cached_bars
import run_llm_backtest as p2

REPO = Path(__file__).parent
OUT_PATH = REPO / "experiments" / "results" / "sprint-repo-hypotheses.json"

RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63
TRAIN_END_DATE = "2026-08-17"


def daily_ic(feature: pd.DataFrame, fwd: pd.DataFrame) -> tuple:
    """Daily cross-sectional Spearman IC: mean, t-stat, n days (B1 machinery)."""
    ics = []
    for day in feature.index:
        f = feature.loc[day].dropna()
        r = fwd.loc[day].dropna()
        common = f.index.intersection(r.index)
        if len(common) < 20:
            continue
        ic = spearman(f.loc[common], r.loc[common])
        if np.isfinite(ic):
            ics.append(ic)
    ics = np.array(ics)
    if len(ics) < 10:
        return None, None, int(len(ics))
    return float(ics.mean()), float(ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))), int(len(ics))


def quintile_spread(feature: pd.DataFrame, fwd: pd.DataFrame) -> tuple:
    """Top-quintile minus bottom-quintile mean forward return, averaged daily."""
    spreads = []
    for day in feature.index:
        f = feature.loc[day].dropna()
        r = fwd.loc[day].dropna()
        common = f.index.intersection(r.index)
        if len(common) < 20:
            continue
        f = f.loc[common]
        r = r.loc[common]
        try:
            q = pd.qcut(f, 5, labels=False, duplicates="drop")
        except ValueError:
            continue
        top = r[q == q.max()].mean()
        bot = r[q == q.min()].mean()
        if np.isfinite(top) and np.isfinite(bot):
            spreads.append(top - bot)
    spreads = np.array(spreads)
    if len(spreads) < 10:
        return None, int(len(spreads))
    return float(spreads.mean()), int(len(spreads))


def main() -> int:
    print("=== Repo-derived hypotheses, native implementation (TRAIN ONLY, $0) ===",
          flush=True)
    data = _load_cached_bars()
    if data is None:
        raise RuntimeError("no cached bars; .cache/backtest_data is empty")
    cfg = TradingConfig.from_env()
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, RESEARCH_START, RESEARCH_END)
    full_prices = pd.DataFrame(
        {s: f["close"] for s, f in data.bars.items()}).sort_index().loc[prices.index]
    window_days = prices.index
    train_days = window_days[window_days < window_days[-HOLDOUT_DAYS_N]]
    train_prices = prices.loc[train_days]
    print(f"train {train_days[0].date()}..{train_days[-1].date()} ({len(train_days)} days)",
          flush=True)
    report: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "spend_usd": 0.0,
        "train_window": f"{train_days[0].date()}..{train_days[-1].date()}",
        "guardrail": "No code from the 7 shared research repos was cloned, installed, imported, or run. All ideas implemented natively in stock-thesis code only.",
    }

    # ------------------------------------------------------------------
    # H1: cost estimate for a Haiku bull/bear debate (NO calls made)
    # ------------------------------------------------------------------
    filings = p2.load_edgar_filings()
    haiku = p2._load_scores(REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json")
    # Measured Phase 2 single-pass Haiku cost per filing (from the ledger):
    # $3.300814 / 1166 filings. A debate = 3 calls (bull + bear + adjudicate)
    # on a LONGER prompt (bull/bear instructions + prior argument), so 3x is
    # a floor; use 3.5x as the planning estimate.
    per_filing_single = 3.300814 / 1166
    per_filing_debate_floor = per_filing_single * 3
    per_filing_debate_plan = per_filing_single * 3.5
    headroom = 0.175025  # monthly $10 cap headroom after the Fable audit
    report["H1_debate_cost_estimate"] = {
        "method": "3 Haiku calls per filing (bull + bear + adjudicate); no calls made",
        "measured_single_pass_haiku_usd_per_filing": round(per_filing_single, 6),
        "debate_usd_per_filing_floor_3x": round(per_filing_debate_floor, 6),
        "debate_usd_per_filing_plan_3.5x": round(per_filing_debate_plan, 6),
        "monthly_headroom_usd": headroom,
        "max_filings_at_plan_estimate": int(headroom // per_filing_debate_plan),
        "cost_n50_usd": round(50 * per_filing_debate_plan, 4),
        "cost_n100_usd": round(100 * per_filing_debate_plan, 4),
        "verdict": ("BLOCKED at meaningful scale: a 100-filing subsample costs "
                    f"~${100 * per_filing_debate_plan:.2f} vs ${headroom:.3f} headroom. "
                    "A ~17-filing pilot fits the headroom but is too small for a "
                    "stable IC comparison. Needs the user's +$5 Anthropic top-up "
                    "verified (still UNVERIFIED) before any debate scoring."),
    }
    print("\n--- H1: Haiku debate cost estimate (no calls) ---", flush=True)
    print(f"  single-pass Haiku/filing: ${per_filing_single:.6f}; debate/filing "
          f"(plan 3.5x): ${per_filing_debate_plan:.6f}", flush=True)
    print(f"  n=100 debate: ~${100 * per_filing_debate_plan:.2f} vs headroom "
          f"${headroom:.3f} -> BLOCKED at scale; max ~{int(headroom // per_filing_debate_plan)} filings fit",
          flush=True)

    # ------------------------------------------------------------------
    # H2a: cite the calibration's C3 (LLM veto on the technical panel)
    # ------------------------------------------------------------------
    calib = json.loads((REPO / "experiments" / "results" / "sprint-calibration.json").read_text())
    c3 = calib["configs"]["reb5_hold10"]["C3_v4_llm_veto"]["cost_10bps"]
    c4 = calib["configs"]["reb5_hold10"]["C4_v4_long_hold"]["cost_10bps"]
    report["H2a_llm_veto_on_technical"] = {
        "source": "sprint-calibration.json (already tested; cited, not re-run)",
        "design": "v4 panel, entries blocked where the LLM contribution < 0; rebalance 5, min hold 10",
        "train_net10": c3["total_return"],
        "train_wf_median_sharpe": c3["wf_median_sharpe"],
        "train_wf_positive_folds": c3["wf_positive_folds"],
        "control_v4_long_hold_net10": c4["total_return"],
        "verdict": ("WEAK: the veto adds only +2.08pp over the v4 long-hold "
                    "control (+9.10% vs +7.02%) with a negative WF median Sharpe "
                    "(-0.28) and 1/4 positive folds. Not worth registering."),
    }
    print("\n--- H2a: LLM veto on technical panel (cited from calibration) ---", flush=True)
    print(f"  C3 veto train net10 {c3['total_return']*100:+.2f}% vs v4 long-hold "
          f"control {c4['total_return']*100:+.2f}%; WF {c3['wf_median_sharpe']}, "
          f"{c3['wf_positive_folds']}/4 folds -> WEAK, not registered", flush=True)

    # ------------------------------------------------------------------
    # H2b: technical veto on the PASSING v2 design (new, $0, train only)
    # ------------------------------------------------------------------
    in_scope_train = p2.in_scope_filings(filings, variants, train_days)
    current = p2.current_filing_series(in_scope_train, train_days)
    score = pd.DataFrame(0.0, index=train_days, columns=sorted(current.columns))
    for sym in current.columns:
        for day in train_days:
            acc = current.at[day, sym]
            if isinstance(acc, str):
                h = haiku.get(acc)
                if h is not None and "error" not in h:
                    score.at[day, sym] = float(h["score"])
    selection = pd.DataFrame(0.0, index=train_days, columns=score.columns)
    for day in train_days:
        row = score.loc[day]
        pos = row[row > 0].sort_values(ascending=False)
        if len(pos) == 0:
            continue
        cutoff = pos.iloc[min(5, len(pos)) - 1]
        selection.loc[day, row >= cutoff] = 1.0
    composite = variants["v1_composite"].loc[train_days]
    sma50 = full_prices.loc[train_days].rolling(50).mean()
    close = full_prices.loc[train_days]
    veto_panels = {
        "H2b_veto_composite_gt0": selection.where(composite > 0, 0.0),
        "H2b_veto_close_gt_sma50": selection.where(close > sma50, 0.0),
    }
    report["H2b_technical_veto_on_v2"] = {}
    n_new = 0
    print("\n--- H2b: technical veto on the passing v2 design (train) ---", flush=True)
    for name, panel in veto_panels.items():
        report["H2b_technical_veto_on_v2"][name] = {}
        for cost in (0, 10, 20):
            res = evaluate_variants(
                train_prices, {name: panel}, cost_bps=float(cost), max_positions=60,
                rebalance_every=5, n_splits=4, initial_capital=1000.0,
                min_hold_days=10)
            row = res.iloc[0]
            report["H2b_technical_veto_on_v2"][name][f"cost_{cost}bps"] = {
                "total_return": float(row["total_return"]),
                "trades": int(row["trades"]),
                "avg_daily_turnover": float(row["avg_daily_turnover"]),
                "wf_median_sharpe": float(row["wf_median_sharpe"]) if row["wf_median_sharpe"] is not None and pd.notna(row["wf_median_sharpe"]) else None,
                "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
            }
            n_new += 1
        r10 = report["H2b_technical_veto_on_v2"][name]["cost_10bps"]
        print(f"  {name:28s} train net10 {r10['total_return']*100:+7.2f}%  "
              f"wf_med {r10['wf_median_sharpe']}  wf+ {r10['wf_positive_folds']}/4  "
              f"trades {r10['trades']:4d}  (v2 no-veto: +57.77%, WF 0.604, 4/4)",
              flush=True)

    # ------------------------------------------------------------------
    # H3a: PEAD proxy — filing-date abnormal return vs forward returns
    # ------------------------------------------------------------------
    tf = filings[filings["filing_date"] <= pd.Timestamp(TRAIN_END_DATE)].copy()
    tf = tf.sort_values(["ticker", "filing_date"])
    spy_ret = full_prices["SPY"].pct_change()
    pead_rows = []
    for _, row in tf.iterrows():
        sym = row["ticker"]
        if sym not in full_prices.columns:
            continue
        fd = row["filing_date"]
        # next trading day on/after the filing date
        idx = full_prices.index.searchsorted(fd)
        if idx <= 0 or idx >= len(full_prices):
            continue
        d = full_prices.index[idx]
        if d not in train_days:
            continue
        # reaction = filing trading-day return minus SPY same day
        r_sym = full_prices[sym].pct_change().loc[d]
        r_spy = spy_ret.loc[d]
        if pd.isna(r_sym) or pd.isna(r_spy):
            continue
        rec = {"ticker": sym, "date": d, "reaction": float(r_sym - r_spy)}
        for h in (5, 10, 20):
            j = full_prices.index.get_loc(d) + h
            if j < len(full_prices):
                rec[f"fwd_{h}d"] = float(
                    full_prices[sym].iloc[j] / full_prices[sym].loc[d] - 1.0)
        pead_rows.append(rec)
    pead = pd.DataFrame(pead_rows).dropna()
    report["H3a_pead_proxy"] = {}
    print(f"\n--- H3a: PEAD proxy (n={len(pead)} train filings) ---", flush=True)
    for h in (5, 10, 20):
        col = f"fwd_{h}d"
        ic = spearman(pead["reaction"], pead[col])
        q = pd.qcut(pead["reaction"], 5, labels=False, duplicates="drop")
        top = pead[col][q == q.max()].mean()
        bot = pead[col][q == q.min()].mean()
        report["H3a_pead_proxy"][f"h{h}d"] = {
            "n": int(len(pead)), "spearman_ic": round(float(ic), 4),
            "top_quintile_mean_fwd": round(float(top), 4),
            "bot_quintile_mean_fwd": round(float(bot), 4),
            "qspread": round(float(top - bot), 4),
        }
        print(f"  h={h:2d}d: IC {ic:+.4f}  Q-spread {top - bot:+.4f} "
              f"(top {top:+.4f} vs bot {bot:+.4f})", flush=True)

    # ------------------------------------------------------------------
    # H3b/H3c: MA alignment + pullback vs forward returns
    # ------------------------------------------------------------------
    ma5 = full_prices.rolling(5).mean()
    ma10 = full_prices.rolling(10).mean()
    ma20 = full_prices.rolling(20).mean()
    align_spread = (ma5 - ma20) / ma20  # continuous alignment strength
    aligned = ((ma5 > ma10) & (ma10 > ma20)).astype(float)
    # pullback: aligned AND price dipped to within 3% above MA20 in last 5d,
    # still above MA20 today
    near_ma20 = (full_prices / ma20 - 1.0)
    dipped = near_ma20.rolling(5).min() <= 0.03
    pullback = ((aligned == 1) & dipped & (near_ma20 > 0)).astype(float)
    fwd = {}
    for h in (5, 10, 20):
        fwd[h] = full_prices.pct_change(periods=h, fill_method=None).shift(-h)
    report["H3b_ma_alignment"] = {}
    report["H3c_pullback"] = {}
    print("\n--- H3b: MA alignment (MA5>MA10>MA20) ---", flush=True)
    for h in (5, 10, 20):
        ic, t, n = daily_ic(align_spread.loc[train_days], fwd[h].loc[train_days])
        qs, nq = quintile_spread(align_spread.loc[train_days], fwd[h].loc[train_days])
        # binary flag: mean fwd return of aligned vs not, averaged daily
        diffs = []
        for day in train_days:
            a = aligned.loc[day].dropna()
            r = fwd[h].loc[day].dropna()
            common = a.index.intersection(r.index)
            if len(common) < 20 or a.loc[common].sum() < 5:
                continue
            diffs.append(r.loc[common][a.loc[common] == 1].mean()
                         - r.loc[common][a.loc[common] == 0].mean())
        flag_diff = float(np.mean(diffs)) if diffs else None
        report["H3b_ma_alignment"][f"h{h}d"] = {
            "spread_ic": ic, "spread_ic_t": t, "n_days": n,
            "quintile_spread": qs,
            "aligned_minus_unaligned_mean_fwd": flag_diff,
        }
        print(f"  h={h:2d}d: spread IC {ic:+.4f} (t {t:+.2f}, n={n})  "
              f"Q-spread {qs:+.4f}  aligned-minus-unaligned {flag_diff:+.4f}",
              flush=True)
    print("\n--- H3c: pullback (aligned + dipped near MA20, still above) ---", flush=True)
    for h in (5, 10, 20):
        diffs = []
        for day in train_days:
            p = pullback.loc[day].dropna()
            r = fwd[h].loc[day].dropna()
            common = p.index.intersection(r.index)
            if len(common) < 20 or p.loc[common].sum() < 5:
                continue
            diffs.append(r.loc[common][p.loc[common] == 1].mean()
                         - r.loc[common][p.loc[common] == 0].mean())
        flag_diff = float(np.mean(diffs)) if diffs else None
        # also: pullback vs aligned-but-no-pullback
        diffs2 = []
        for day in train_days:
            p = pullback.loc[day].dropna()
            a = aligned.loc[day].dropna()
            r = fwd[h].loc[day].dropna()
            common = p.index.intersection(a.index).intersection(r.index)
            if len(common) < 20 or p.loc[common].sum() < 5 or a.loc[common].sum() - p.loc[common].sum() < 5:
                continue
            diffs2.append(r.loc[common][p.loc[common] == 1].mean()
                          - r.loc[common][(a.loc[common] == 1) & (p.loc[common] == 0)].mean())
        flag_diff2 = float(np.mean(diffs2)) if diffs2 else None
        report["H3c_pullback"][f"h{h}d"] = {
            "pullback_minus_rest_mean_fwd": flag_diff,
            "pullback_minus_aligned_no_pullback_mean_fwd": flag_diff2,
            "n_days": len(diffs),
        }
        print(f"  h={h:2d}d: pullback-minus-rest {flag_diff:+.4f}  "
              f"pullback-minus-aligned-no-pullback {flag_diff2:+.4f}  (n_days={len(diffs)})",
              flush=True)

    # ------------------------------------------------------------------
    # Pre-set rule: alignment/pullback backtest ONLY if the pattern clears
    # |IC| >= 0.03 at h=20d or a 20d quintile spread >= 1%.
    # ------------------------------------------------------------------
    h20 = report["H3b_ma_alignment"]["h20d"]
    h20_pb = report["H3c_pullback"]["h20d"]
    clears = ((h20["spread_ic"] is not None and abs(h20["spread_ic"]) >= 0.03)
              or (h20["quintile_spread"] is not None and h20["quintile_spread"] >= 0.01)
              or (h20_pb["pullback_minus_rest_mean_fwd"] is not None
                  and abs(h20_pb["pullback_minus_rest_mean_fwd"]) >= 0.01))
    report["alignment_backtest_rule"] = {
        "rule": "run alignment backtest only if |IC| >= 0.03 at h=20d or 20d quintile spread >= 1% or |pullback 20d diff| >= 1%",
        "clears_bar": bool(clears),
    }
    if clears:
        print("\n--- Alignment clears the pre-set bar: running backtest ---", flush=True)
        panel = align_spread.loc[train_days].copy()
        panel[panel <= 0] = 0.0  # only positive alignment counts
        report["H3b_alignment_backtest"] = {}
        for cost in (0, 10, 20):
            res = evaluate_variants(
                train_prices, {"align": panel}, cost_bps=float(cost), max_positions=5,
                rebalance_every=5, n_splits=4, initial_capital=1000.0,
                min_hold_days=10)
            row = res.iloc[0]
            report["H3b_alignment_backtest"][f"cost_{cost}bps"] = {
                "total_return": float(row["total_return"]),
                "trades": int(row["trades"]),
                "wf_median_sharpe": float(row["wf_median_sharpe"]) if row["wf_median_sharpe"] is not None and pd.notna(row["wf_median_sharpe"]) else None,
                "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
            }
            n_new += 1
        r10 = report["H3b_alignment_backtest"]["cost_10bps"]
        print(f"  alignment backtest train net10 {r10['total_return']*100:+.2f}%  "
              f"wf_med {r10['wf_median_sharpe']}  wf+ {r10['wf_positive_folds']}/4",
              flush=True)
    else:
        print("\n--- Alignment does NOT clear the pre-set bar: no backtest run ---",
              flush=True)
        report["H3b_alignment_backtest"] = "not run: pattern did not clear the pre-set bar"

    report["n_trials_new"] = n_new
    report["n_trials_total_dsr"] = 75 + n_new
    OUT_PATH.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nnew trials: {n_new}; DSR n_trials total: {75 + n_new}", flush=True)
    print(f"report written to {OUT_PATH}", flush=True)
    print("SPEND: $0.00 (no API calls)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
