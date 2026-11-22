#!/usr/bin/env python3
"""Sprint step 1+2 ($0, train-only): cost/turnover decomposition + pattern recognition.

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.
No API calls are made anywhere in this script ($0 spend).

Part A — decomposition: reproduce base-variant gross/net/trades/turnover on
TRAIN (2024-01-02..2026-08-17) at 0/10/20bps, plus the v2 turnover-cut panel.
Holdout numbers are reproduced descriptively only (already published); NO
design decision in this sprint is made from holdout outcomes.

Part B — pattern recognition (TRAIN ONLY):
  B1. Technical features: cross-sectional rank IC + quintile spread of each
      feature vs engine-consistent forward returns (from day d+1) at
      horizons 1/5/10/20/60 trading days.
  B2. LLM scores: filing-date-anchored forward returns vs Haiku score,
      combined Haiku+Sonnet score, score-change vs the previous filing for
      the same symbol, and confidence buckets. Horizons 5/10/20/60/126.
  B3. Textual delta features ($0, from the excerpts themselves):
      consecutive-filing text overlap (Jaccard), length change, and a small
      tone-lexicon delta — vs forward returns at 5/10/20/60 days.

Selection discipline: everything in Part B is TRAIN ONLY. Walk-forward
folds (n_splits=4, within train) are used for any design selection in
step 3. The frozen holdout is never used to pick a design.

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python analyze_sprint_patterns.py
    (writes experiments/results/sprint-pattern-analysis.json)
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
from trade_signal import composite_score, trend_quality_score

REPO = Path(__file__).parent
OUT_PATH = REPO / "experiments" / "results" / "sprint-pattern-analysis.json"

RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63
TRAIN_END_DATE = "2026-08-17"
HORIZONS = [1, 5, 10, 20, 60]
LLM_HORIZONS = [5, 10, 20, 60, 126]

# Small tone lexicon for the $0 textual-delta features (B3). Deliberately
# tiny and generic: direction words any filing uses, not a tuned list.
POS_WORDS = {"growth", "strong", "record", "expand", "improve", "increase",
             "robust", "resilient", "opportunity", "momentum", "outperform"}
NEG_WORDS = {"decline", "risk", "uncertain", "challenge", "weak", "decrease",
             "pressure", "headwind", "adverse", "loss", "shortfall", "volatile"}


def rank_ic_and_spread(feature: pd.DataFrame, fwd: pd.DataFrame):
    """Cross-sectional rank IC (mean, t-stat) + top-minus-bottom quintile spread.

    feature/fwd share index (days) and columns (symbols). NaNs dropped
    pairwise per day. Returns dict with n_days used.
    """
    ics, spreads = [], []
    common = feature.index.intersection(fwd.index)
    for day in common:
        f = feature.loc[day].dropna()
        r = fwd.loc[day].dropna()
        both = f.index.intersection(r.index)
        if len(both) < 20:
            continue
        fr = f.loc[both].rank()
        rr = r.loc[both].rank()
        ic = float(fr.corr(rr, method="pearson"))
        if np.isnan(ic):
            continue
        ics.append(ic)
        # quintile spread: top-quintile mean fwd return minus bottom-quintile
        order = f.loc[both].sort_values()
        n = len(order)
        q = max(1, n // 5)
        top = order.index[-q:]
        bot = order.index[:q]
        spreads.append(float(r.loc[top].mean() - r.loc[bot].mean()))
    ics = np.array(ics)
    spreads = np.array(spreads)
    if len(ics) == 0:
        return {"n_days": 0, "mean_ic": None, "ic_tstat": None,
                "mean_qspread": None}
    return {
        "n_days": int(len(ics)),
        "mean_ic": round(float(ics.mean()), 4),
        "ic_tstat": round(float(ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))), 2) if len(ics) > 1 else None,
        "mean_qspread": round(float(spreads.mean()), 5),
        "qspread_tstat": round(float(spreads.mean() / (spreads.std(ddof=1) / np.sqrt(len(spreads)))), 2) if len(spreads) > 1 else None,
    }


def fwd_returns(prices: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Engine-consistent forward returns: feature at close of day d predicts
    the close-to-close return from d+1 to d+1+horizon (the engine's 1-bar
    lag means a signal at d first earns the move starting d+1)."""
    shifted = prices.shift(-(1 + horizon)) / prices.shift(-1) - 1.0
    return shifted


def main() -> int:
    print("=== Sprint steps 1+2: $0 decomposition + pattern recognition (TRAIN ONLY) ===",
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
    holdout_days = window_days[window_days >= holdout_start]
    train_prices = prices.loc[train_days]
    print(f"window {window_days[0].date()}..{window_days[-1].date()} "
          f"({len(window_days)} days); train {len(train_days)} days, "
          f"holdout {len(holdout_days)} days", flush=True)

    report: dict = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "spend_usd": 0.0,
        "train_window": f"{train_days[0].date()}..{train_days[-1].date()}",
        "holdout_window": f"{holdout_days[0].date()}..{holdout_days[-1].date()}",
    }

    # ------------------------------------------------------------------
    # Part A — decomposition (train; holdout reproduced descriptively)
    # ------------------------------------------------------------------
    print("\n--- Part A: cost/turnover decomposition ---", flush=True)
    v2_prices, v2_panel = build_v2_signal(data, cfg)
    v2_prices, v2_panel = slice_research_window(
        v2_prices, {"v2_turnover_cut": v2_panel}, RESEARCH_START, RESEARCH_END)
    v2_panel = v2_panel["v2_turnover_cut"]
    panels_all = {**variants, "v2_turnover_cut": v2_panel}
    decomp = {}
    for window_name, w_prices, w_panels in (
        ("train", train_prices, {k: v.loc[train_days] for k, v in panels_all.items()}),
        ("holdout", prices.loc[holdout_days], {k: v.loc[holdout_days] for k, v in panels_all.items()}),
    ):
        decomp[window_name] = {}
        for cost in (0, 10, 20):
            res = evaluate_variants(
                w_prices, w_panels, cost_bps=float(cost), max_positions=5,
                rebalance_every=1, n_splits=4, initial_capital=1000.0)
            decomp[window_name][f"cost_{cost}bps"] = {
                row["variant"]: {
                    "total_return": row["total_return"],
                    "trades": int(row["trades"]),
                    "avg_daily_turnover": row["avg_daily_turnover"],
                    "sharpe": row["sharpe"],
                    "hit_rate": row["hit_rate"],
                    "max_drawdown": row["max_drawdown"],
                    "wf_median_sharpe": row["wf_median_sharpe"],
                    "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
                } for _, row in res.iterrows()
            }
        # cost drag per variant on this window
        for var in panels_all:
            g = decomp[window_name]["cost_0bps"][var]["total_return"]
            n10 = decomp[window_name]["cost_10bps"][var]["total_return"]
            n20 = decomp[window_name]["cost_20bps"][var]["total_return"]
            decomp[window_name][var] = {
                "gross_0bps": g, "net_10bps": n10, "net_20bps": n20,
                "cost_drag_10bps_pp": round((g - n10) * 100, 2),
                "cost_drag_20bps_pp": round((g - n20) * 100, 2),
            }
    report["part_a_decomposition"] = decomp
    for window_name in ("train", "holdout"):
        print(f"\n[{window_name}] gross vs net (pp) and trades @10bps:", flush=True)
        for var in panels_all:
            d = decomp[window_name][var]
            t10 = decomp[window_name]["cost_10bps"][var]
            print(f"  {var:28s} gross {d['gross_0bps']*100:+7.2f}%  net10 {d['net_10bps']*100:+7.2f}%  "
                  f"drag {d['cost_drag_10bps_pp']:5.2f}pp  trades {t10['trades']:5d}  "
                  f"turnover {t10['avg_daily_turnover']:.3f}  wf_med_sharpe {t10['wf_median_sharpe']}",
                  flush=True)

    # ------------------------------------------------------------------
    # Part B1 — technical feature predictive power (TRAIN ONLY)
    # ------------------------------------------------------------------
    print("\n--- Part B1: technical feature rank IC (TRAIN ONLY) ---", flush=True)
    _, features = _feature_panels(data)
    # restrict feature frames to the train window
    feats_train = {s: f.loc[train_days] for s, f in features.items() if s in train_prices.columns}
    feat_names = ["mom_126_21", "ret_63", "ret_1d", "rsi_14", "mfi_14", "kvo_hist",
                  "obv_slope_20", "wad_slope_20", "rvol_20", "drawdown_63",
                  "sma_20", "sma_50"]
    # derived panels
    comp = pd.DataFrame({s: composite_score(feats_train[s]) for s in feats_train})
    tq = pd.DataFrame({s: trend_quality_score(feats_train[s]) for s in feats_train})
    feat_panels = {name: pd.DataFrame({s: feats_train[s][name] for s in feats_train})
                   for name in feat_names}
    feat_panels["composite_score"] = comp
    feat_panels["trend_quality"] = tq
    feat_panels["close_gt_sma50"] = pd.DataFrame(
        {s: (feats_train[s]["close"] > feats_train[s]["sma_50"]).astype(float)
         for s in feats_train})
    feat_panels["sma20_gt_sma50"] = pd.DataFrame(
        {s: (feats_train[s]["sma_20"] > feats_train[s]["sma_50"]).astype(float)
         for s in feats_train})

    b1 = {}
    for h in HORIZONS:
        fwd = fwd_returns(train_prices, h)
        b1[f"h{h}"] = {}
        for name, panel in feat_panels.items():
            b1[f"h{h}"][name] = rank_ic_and_spread(panel, fwd)
        # print the horizon table sorted by |mean IC|
        print(f"\n[h={h}d forward return] top features by |mean IC|:", flush=True)
        ranked = sorted(b1[f"h{h}"].items(),
                        key=lambda kv: abs(kv[1]["mean_ic"] or 0), reverse=True)
        for name, stats in ranked:
            print(f"  {name:18s} IC {stats['mean_ic']:+.4f} (t {stats['ic_tstat']})  "
                  f"Q-spread {stats['mean_qspread']:+.5f} (t {stats['qspread_tstat']})  "
                  f"n={stats['n_days']}", flush=True)
    report["part_b1_technical_ic"] = b1

    # ------------------------------------------------------------------
    # Part B2 — LLM score predictive power (TRAIN ONLY)
    # ------------------------------------------------------------------
    print("\n--- Part B2: LLM score vs filing-anchored forward returns (TRAIN ONLY) ---",
          flush=True)
    filings = p2.load_edgar_filings()
    haiku = p2._load_scores(REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json")
    sonnet = p2._load_scores(REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-sonnet.json")
    print(f"filings loaded: {len(filings)}; Haiku scores: {len(haiku)}; "
          f"Sonnet scores: {len(sonnet)}", flush=True)
    train_filings = filings[filings["filing_date"] <= pd.Timestamp(TRAIN_END_DATE)].copy()
    # map each filing to the next trading day on/after its filing date
    tdays = train_prices.index
    pos = np.searchsorted(tdays.to_numpy(), train_filings["filing_date"].to_numpy(), side="left")
    train_filings["trade_day_pos"] = pos
    train_filings = train_filings[train_filings["trade_day_pos"] < len(tdays)].copy()
    train_filings["trade_day"] = tdays[train_filings["trade_day_pos"].to_numpy()]
    train_filings["haiku_score"] = train_filings["accession"].map(
        lambda a: (haiku.get(a) or {}).get("score", np.nan))
    train_filings["haiku_conf"] = train_filings["accession"].map(
        lambda a: (haiku.get(a) or {}).get("confidence", np.nan))
    train_filings["sonnet_score"] = train_filings["accession"].map(
        lambda a: (sonnet.get(a) or {}).get("score", np.nan))
    train_filings["sonnet_conf"] = train_filings["accession"].map(
        lambda a: (sonnet.get(a) or {}).get("confidence", np.nan))
    scored = train_filings[train_filings["haiku_score"].notna()].copy()
    both = scored[scored["sonnet_score"].notna()].copy()
    both["combined"] = (both["haiku_score"] + both["sonnet_score"]) / 2.0
    # score change vs previous filing for the same symbol
    scored = scored.sort_values(["ticker", "filing_date"])
    scored["prev_haiku"] = scored.groupby("ticker")["haiku_score"].shift(1)
    scored["haiku_delta"] = scored["haiku_score"] - scored["prev_haiku"]
    print(f"train filings with Haiku score: {len(scored)}; with Sonnet too: {len(both)}; "
          f"with a previous filing (delta): {int(scored['haiku_delta'].notna().sum())}", flush=True)

    def filing_fwd(filings_df: pd.DataFrame, h: int) -> pd.Series:
        """Forward return from trade_day+1 to trade_day+1+h for each filing."""
        out = []
        for _, row in filings_df.iterrows():
            p = int(row["trade_day_pos"])
            i0 = p + 1
            i1 = p + 1 + h
            if i1 >= len(tdays):
                out.append(np.nan)
                continue
            sym = row["ticker"]
            if sym not in train_prices.columns:
                out.append(np.nan)
                continue
            c0 = train_prices.iloc[i0][sym]
            c1 = train_prices.iloc[i1][sym]
            out.append(c1 / c0 - 1.0 if pd.notna(c0) and pd.notna(c1) and c0 != 0 else np.nan)
        return pd.Series(out, index=filings_df.index)

    b2 = {}
    for h in LLM_HORIZONS:
        fwd = filing_fwd(scored, h)
        b2[f"h{h}"] = {}
        for col in ("haiku_score", "haiku_delta"):
            sub = scored[[col]].assign(fwd=fwd).dropna()
            if len(sub) >= 30:
                ic = float(sub[col].rank().corr(sub["fwd"].rank(), method="pearson"))
                top = sub.nlargest(max(1, len(sub) // 5), col)
                bot = sub.nsmallest(max(1, len(sub) // 5), col)
                b2[f"h{h}"][col] = {
                    "n": int(len(sub)),
                    "spearman_ic": round(ic, 4),
                    "top_quintile_mean_fwd": round(float(top["fwd"].mean()), 5),
                    "bot_quintile_mean_fwd": round(float(bot["fwd"].mean()), 5),
                    "qspread": round(float(top["fwd"].mean() - bot["fwd"].mean()), 5),
                }
            else:
                b2[f"h{h}"][col] = {"n": int(len(sub)), "spearman_ic": None}
        # combined (both models) — smaller n
        fwd_b = filing_fwd(both, h)
        sub = both[["combined"]].assign(fwd=fwd_b).dropna()
        if len(sub) >= 20:
            ic = float(sub["combined"].rank().corr(sub["fwd"].rank(), method="pearson"))
            top = sub.nlargest(max(1, len(sub) // 5), "combined")
            bot = sub.nsmallest(max(1, len(sub) // 5), "combined")
            b2[f"h{h}"]["combined_haiku_sonnet"] = {
                "n": int(len(sub)),
                "spearman_ic": round(ic, 4),
                "top_quintile_mean_fwd": round(float(top["fwd"].mean()), 5),
                "bot_quintile_mean_fwd": round(float(bot["fwd"].mean()), 5),
                "qspread": round(float(top["fwd"].mean() - bot["fwd"].mean()), 5),
            }
        else:
            b2[f"h{h}"]["combined_haiku_sonnet"] = {"n": int(len(sub)), "spearman_ic": None}
        # confidence buckets for Haiku
        sub = scored[["haiku_score", "haiku_conf"]].assign(fwd=fwd).dropna()
        buckets = {}
        for lo, hi, label in ((0.6, 0.8, "conf_0.6-0.8"), (0.8, 0.9, "conf_0.8-0.9"),
                              (0.9, 1.01, "conf_0.9-1.0")):
            s2 = sub[(sub["haiku_conf"] >= lo) & (sub["haiku_conf"] < hi)]
            if len(s2) >= 20:
                ic = float(s2["haiku_score"].rank().corr(s2["fwd"].rank(), method="pearson"))
                buckets[label] = {"n": int(len(s2)), "spearman_ic": round(ic, 4),
                                  "mean_fwd_pos_score": round(float(s2[s2["haiku_score"] > 0]["fwd"].mean()), 5) if (s2["haiku_score"] > 0).any() else None,
                                  "mean_fwd_neg_score": round(float(s2[s2["haiku_score"] < 0]["fwd"].mean()), 5) if (s2["haiku_score"] < 0).any() else None}
        b2[f"h{h}"]["confidence_buckets"] = buckets
        print(f"\n[h={h}d] Haiku IC {b2[f'h{h}']['haiku_score'].get('spearman_ic')} "
              f"(n={b2[f'h{h}']['haiku_score'].get('n')})  delta IC "
              f"{b2[f'h{h}']['haiku_delta'].get('spearman_ic')} "
              f"(n={b2[f'h{h}']['haiku_delta'].get('n')})  combined IC "
              f"{b2[f'h{h}']['combined_haiku_sonnet'].get('spearman_ic')} "
              f"(n={b2[f'h{h}']['combined_haiku_sonnet'].get('n')})", flush=True)
    report["part_b2_llm_predictive"] = b2

    # ------------------------------------------------------------------
    # Part B3 — $0 textual delta features (TRAIN ONLY)
    # ------------------------------------------------------------------
    print("\n--- Part B3: textual delta features vs forward returns (TRAIN ONLY) ---",
          flush=True)
    word_re = re.compile(r"[a-z]{3,}")
    def tokens(text: str) -> set:
        return set(word_re.findall(text.lower()))
    def tone(text: str) -> float:
        toks = word_re.findall(text.lower())
        if not toks:
            return 0.0
        pos = sum(1 for t in toks if t in POS_WORDS)
        neg = sum(1 for t in toks if t in NEG_WORDS)
        return (pos - neg) / max(len(toks), 1)

    delta_rows = []
    tf = train_filings.sort_values(["ticker", "filing_date"])
    for sym, group in tf.groupby("ticker"):
        prev_text = prev_tone = prev_len = None
        for _, row in group.iterrows():
            text = row["excerpt_text"]
            toks = tokens(text)
            tn = tone(text)
            if prev_text is not None:
                inter = len(toks & prev_text)
                union = len(toks | prev_text)
                delta_rows.append({
                    "ticker": sym,
                    "filing_date": row["filing_date"],
                    "trade_day_pos": int(row["trade_day_pos"]),
                    "jaccard": inter / union if union else np.nan,
                    "len_ratio": len(text) / max(prev_len, 1),
                    "tone_delta": tn - prev_tone,
                    "tone": tn,
                })
            prev_text, prev_tone, prev_len = toks, tn, len(text)
    delta = pd.DataFrame(delta_rows)
    print(f"consecutive-filing pairs on train: {len(delta)}", flush=True)
    b3 = {}
    for h in (5, 10, 20, 60):
        fwd = []
        for _, row in delta.iterrows():
            p = int(row["trade_day_pos"])
            i0, i1 = p + 1, p + 1 + h
            if i1 >= len(tdays) or row["ticker"] not in train_prices.columns:
                fwd.append(np.nan)
                continue
            c0 = train_prices.iloc[i0][row["ticker"]]
            c1 = train_prices.iloc[i1][row["ticker"]]
            fwd.append(c1 / c0 - 1.0 if pd.notna(c0) and pd.notna(c1) and c0 != 0 else np.nan)
        delta["fwd"] = fwd
        b3[f"h{h}"] = {}
        for col in ("jaccard", "len_ratio", "tone_delta", "tone"):
            sub = delta[[col, "fwd"]].dropna()
            if len(sub) >= 30:
                ic = float(sub[col].rank().corr(sub["fwd"].rank(), method="pearson"))
                top = sub.nlargest(max(1, len(sub) // 5), col)
                bot = sub.nsmallest(max(1, len(sub) // 5), col)
                b3[f"h{h}"][col] = {
                    "n": int(len(sub)), "spearman_ic": round(ic, 4),
                    "qspread": round(float(top["fwd"].mean() - bot["fwd"].mean()), 5),
                }
            else:
                b3[f"h{h}"][col] = {"n": int(len(sub)), "spearman_ic": None}
        print(f"[h={h}d] " + "  ".join(
            f"{c}: IC {b3[f'h{h}'][c].get('spearman_ic')} (n={b3[f'h{h}'][c].get('n')})"
            for c in ("jaccard", "len_ratio", "tone_delta", "tone")), flush=True)
    report["part_b3_textual_delta"] = b3

    OUT_PATH.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nreport written to {OUT_PATH}", flush=True)
    print("SPEND: $0.00 (no API calls)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
