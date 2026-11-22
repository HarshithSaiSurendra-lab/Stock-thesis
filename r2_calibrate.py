#!/usr/bin/env python3
"""Round-2 train-only calibration for the $0 hypotheses (H2-H7).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE
stays 0. No API calls anywhere ($0 spend). All panels are built on the
TRAIN window only (2024-01-01..2026-08-17); the frozen holdout is never
touched here. Nothing from the 7 shared research repos is cloned,
installed, imported, or run — all ideas are implemented natively in
stock-thesis code only.

Trial accounting (registry is source of truth): n_trials starts at 81
(round-1 v3 DSR). Each (design x cost) backtest evaluation = 1 trial.
The v2/v3 anchor reproductions are NOT new trials (same designs already
counted in round 1). IC diagnostics are not trials. Run:
    python3 r2_calibrate.py
Writes experiments/results/sprint-r2-calibration.json and prints a
summary table with the pre-registered selection decision for each
design. Holdout confirmation (one per design that clears its train
bar) happens in a separate driver afterwards.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from analyze_sprint_patterns import fwd_returns, rank_ic_and_spread
from research_runner import evaluate_variants
import r2_panels as r2

REPO = Path(__file__).parent
RESULTS_PATH = REPO / "experiments" / "results" / "sprint-r2-calibration.json"

N_TRIALS_START = 81
V2_TRAIN_NET10 = 0.5777  # round-1 exp-llm-long-horizon-v2 train net10
V3_TRAIN_NET10 = 0.8548  # round-1 exp-llm-long-horizon-v3 train net10
COSTS = (0, 10, 20)

# Pre-registered train bars for holdout confirmation (decided before
# seeing any round-2 numbers; see the task brief).
# H2 bar is vs V3 (+85.48%) for ALL H2 designs, per the task brief
# (not vs each design's own base): turnover control must keep the
# design within 10pp of the best round-1 design.
BAR_H2_WITHIN_PP = 0.10       # H2: net10 within 10pp of v3 (+85.48%)
BAR_H2_TURNOVER = 0.10        # H2: turnover < 0.10/day
BAR_H3_BEAT_PP = 0.02         # H3: beats its base by >= 2pp
BAR_H4_H5_H6_BEAT_V2 = V2_TRAIN_NET10  # H4/H5/H6: train net10 > v2's
BAR_H7_MIN_NET10 = 0.40       # H7: pick clears +40% train net10
BAR_WF_FOLDS = 3              # all: walk-forward >= 3/4 positive folds


def eval_panel(prices, panel, max_positions, rebalance_every=5, min_hold_days=10):
    """Evaluate one signal panel at costs 0/10/20. Returns dict of
    cost -> {total_return, sharpe, trades, avg_daily_turnover,
    wf_median_sharpe, wf_positive_folds}. Counts as 3 trials."""
    out = {}
    for cost in COSTS:
        res = evaluate_variants(
            prices, {"x": panel}, cost_bps=float(cost),
            max_positions=max_positions, rebalance_every=rebalance_every,
            n_splits=4, initial_capital=1000.0, min_hold_days=min_hold_days)
        row = res.iloc[0]
        out[f"cost_{cost}bps"] = {
            "total_return": float(row["total_return"]),
            "sharpe": float(row["sharpe"]) if pd.notna(row["sharpe"]) else None,
            "trades": int(row["trades"]),
            "avg_daily_turnover": float(row["avg_daily_turnover"]),
            "max_drawdown": float(row["max_drawdown"]) if pd.notna(row["max_drawdown"]) else None,
            "wf_median_sharpe": float(row["wf_median_sharpe"]) if row["wf_median_sharpe"] is not None and pd.notna(row["wf_median_sharpe"]) else None,
            "wf_positive_folds": int(row["wf_positive_folds"]) if row["wf_positive_folds"] is not None else None,
            "wf_folds": int(row["wf_folds"]) if row["wf_folds"] is not None else None,
        }
    return out


def _persist(results):
    """Write the calibration JSON now, so a later crash does not lose
    completed sections (the first H5 crash lost all H2-H4 results)."""
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2))


def _net10(result):
    return result["cost_10bps"]["total_return"]


def _wf(result):
    return result["cost_10bps"]["wf_positive_folds"] or 0


def main() -> int:
    print("=== Round-2 calibration: $0 hypotheses H2-H7 (TRAIN ONLY) ===", flush=True)
    d = r2.load_r2_data()
    train_days = d["train_days"]
    prices = d["prices"].loc[train_days]
    score = d["score"].loc[train_days]
    composite = d["composite"].loc[train_days]
    results = {"n_trials_start": N_TRIALS_START, "designs": {}}
    n_trials = N_TRIALS_START

    # --- Anchors: reproduce round-1 v2 / v3 train numbers (not new trials)
    v2_sel = r2.v2_selection(score)
    v3_sel = r2.v3_selection(score, composite)
    anchor_v2 = eval_panel(prices, v2_sel, max_positions=60)
    anchor_v3 = eval_panel(prices, v3_sel, max_positions=60)
    for name, got, want in (("v2", _net10(anchor_v2), V2_TRAIN_NET10),
                            ("v3", _net10(anchor_v3), V3_TRAIN_NET10)):
        diff_pp = (got - want) * 100
        print(f"[anchor] {name} train net10 {got*100:+.2f}% "
              f"(round-1 {want*100:+.2f}%, diff {diff_pp:+.2f}pp)", flush=True)
        if abs(got - want) > 0.005:
            raise RuntimeError(f"anchor {name} reproduction failed: "
                               f"got {got:.4f}, want {want:.4f}")
    results["anchors"] = {"v2": anchor_v2, "v3": anchor_v3}
    results["n_trials_end"] = n_trials
    results["n_trials_new"] = 0
    _persist(results)

    def record(name, hypothesis, panel_result, base_net10, bar_text, confirm):
        nonlocal n_trials
        results["designs"][name] = {
            "hypothesis": hypothesis,
            "base_train_net10": base_net10,
            "bar": bar_text,
            "confirm_holdout": bool(confirm),
            "results": panel_result,
        }
        results["n_trials_end"] = n_trials
        results["n_trials_new"] = n_trials - N_TRIALS_START
        _persist(results)
        print(f"[{name}] net10 {_net10(panel_result)*100:+.2f}%  "
              f"wf {_wf(panel_result)}/4  "
              f"turn {panel_result['cost_10bps']['avg_daily_turnover']:.3f}  "
              f"-> {'CONFIRM' if confirm else 'drop'}", flush=True)

    # --- H2a: buy/hold banding on v3 (and v2 for comparison).
    # Bar is vs V3 (+85.48%) for ALL H2 designs, per the task brief.
    for base_name, base_sel, base_net in (("v3", v3_sel, V3_TRAIN_NET10),
                                          ("v2", v2_sel, V2_TRAIN_NET10)):
        for buffer_pct in (30, 50):
            name = f"h2-banding-{base_name}-buf{buffer_pct}"
            panel = r2.banded_selection(base_sel, score, buffer_pct)
            res = eval_panel(prices, panel, max_positions=60)
            n_trials += 3
            confirm = (abs(_net10(res) - V3_TRAIN_NET10) <= BAR_H2_WITHIN_PP
                       and res["cost_10bps"]["avg_daily_turnover"] < BAR_H2_TURNOVER
                       and _wf(res) >= BAR_WF_FOLDS)
            record(name, "R2-H2a buy/hold banding", res, base_net,
                   f"net10 within {BAR_H2_WITHIN_PP*100:.0f}pp of v3 "
                   f"({V3_TRAIN_NET10*100:.2f}%), turnover < {BAR_H2_TURNOVER}, "
                   f"WF >= {BAR_WF_FOLDS}/4", confirm)

    # --- H2b: overlapping 1/H cohorts on v3 (and v2 H=10).
    # Bar is vs V3 (+85.48%) for ALL H2 designs, per the task brief.
    for base_name, base_sel, base_net in (("v3", v3_sel, V3_TRAIN_NET10),
                                          ("v2", v2_sel, V2_TRAIN_NET10)):
        horizons = (10, 20) if base_name == "v3" else (10,)
        for horizon in horizons:
            name = f"h2-cohort-{base_name}-h{horizon}"
            res = r2.cohort_returns(prices, base_sel, horizon, costs=COSTS,
                                    min_hold_days=10, max_positions=60)
            n_trials += 3
            confirm = (abs(_net10(res) - V3_TRAIN_NET10) <= BAR_H2_WITHIN_PP
                       and res["cost_10bps"]["avg_daily_turnover"] < BAR_H2_TURNOVER
                       and _wf(res) >= BAR_WF_FOLDS)
            record(name, "R2-H2b overlapping 1/H cohorts", res, base_net,
                   f"net10 within {BAR_H2_WITHIN_PP*100:.0f}pp of v3 "
                   f"({V3_TRAIN_NET10*100:.2f}%), turnover < {BAR_H2_TURNOVER}, "
                   f"WF >= {BAR_WF_FOLDS}/4", confirm)

    # --- H3: Lazy-Prices Item 1A veto (diagnostic IC first, then portfolios)
    sim = r2.lazy_prices_similarity(d["filings"])
    sim_panel, changer = r2.lazy_changer_panel(sim, train_days,
                                               sorted(d["score"].columns))
    fwd20 = fwd_returns(prices, 20)
    h3_ic = rank_ic_and_spread(sim_panel.loc[train_days], fwd20)
    results["h3_diagnostics"] = {
        "similarity_ic_20d": h3_ic,
        "mean_changer_share_per_day": float(changer.mean(axis=1).mean()),
        "n_filings_compared": int(len(sim)),
    }
    print(f"[h3-diag] Item 1A similarity IC @20d: {h3_ic['mean_ic']} "
          f"(t {h3_ic['ic_tstat']}, n {h3_ic['n_days']} days); "
          f"mean changer share/day "
          f"{results['h3_diagnostics']['mean_changer_share_per_day']:.3f}", flush=True)
    _persist(results)
    for base_name, base_sel, base_net in (("v3", v3_sel, V3_TRAIN_NET10),
                                          ("v2", v2_sel, V2_TRAIN_NET10)):
        name = f"h3-lazy-veto-{base_name}"
        panel = r2.lazy_veto_selection(base_sel, changer)
        res = eval_panel(prices, panel, max_positions=60)
        n_trials += 3
        confirm = (_net10(res) >= base_net + BAR_H3_BEAT_PP
                   and _wf(res) >= BAR_WF_FOLDS)
        record(name, "R2-H3 Lazy-Prices Item 1A veto", res, base_net,
               f"beats base by >= {BAR_H3_BEAT_PP*100:.0f}pp, WF >= {BAR_WF_FOLDS}/4",
               confirm)

    # --- H4: decoupled mixture
    mix = r2.mixture_panel(score, composite)
    res = eval_panel(prices, mix, max_positions=60)
    n_trials += 3
    confirm = _net10(res) > BAR_H4_H5_H6_BEAT_V2 and _wf(res) >= BAR_WF_FOLDS
    record("h4-mixture", "R2-H4 decoupled rank mixture", res, V2_TRAIN_NET10,
           f"train net10 > v2 {V2_TRAIN_NET10*100:.2f}%, WF >= {BAR_WF_FOLDS}/4",
           confirm)

    # --- H5: cross-family ensemble (walk-forward Ridge + GBDT + Haiku)
    print("[h5] fitting walk-forward Ridge + native GBDT (4 folds)...", flush=True)
    panel_veto, panel_no_veto, diag = r2.ensemble_panels(
        d["features"], prices, score, train_days)
    results["h5_diagnostics"] = diag
    print(f"[h5-diag] ensemble rows {diag['n_rows']}, predicted "
          f"{diag['n_predicted']}, horizon {diag['horizon_days']}d", flush=True)
    _persist(results)
    for name, panel in (("h5-ensemble-veto", panel_veto),
                        ("h5-ensemble-noveto", panel_no_veto)):
        res = eval_panel(prices, panel, max_positions=60)
        n_trials += 3
        confirm = _net10(res) > BAR_H4_H5_H6_BEAT_V2 and _wf(res) >= BAR_WF_FOLDS
        record(name, "R2-H5 cross-family ensemble", res, V2_TRAIN_NET10,
               f"train net10 > v2 {V2_TRAIN_NET10*100:.2f}%, WF >= {BAR_WF_FOLDS}/4",
               confirm)

    # --- H6: high-volume shock + intraday/overnight (IC gate, then backtest)
    h6 = r2.h6_features(d["data"], train_days)
    fwd10 = fwd_returns(prices, 10)
    shock_flag = (h6["vol_pct"] >= 0.9).astype(float)
    h6_ics = {}
    for feat_name, panel in (("vol_pct", h6["vol_pct"]),
                             ("vol_ratio", h6["vol_ratio"]),
                             ("shock_flag", shock_flag),
                             ("intraday_20d", h6["intraday_20d"]),
                             ("overnight_20d", h6["overnight_20d"])):
        h6_ics[feat_name] = {
            "ic_10d": rank_ic_and_spread(panel, fwd10),
            "ic_20d": rank_ic_and_spread(panel, fwd20),
        }
    results["h6_diagnostics"] = h6_ics
    _persist(results)
    for feat_name, ics in h6_ics.items():
        ic20 = ics["ic_20d"]["mean_ic"]
        print(f"[h6-diag] {feat_name}: IC @10d {ics['ic_10d']['mean_ic']} "
              f"(t {ics['ic_10d']['ic_tstat']}), @20d {ic20} "
              f"(t {ics['ic_20d']['ic_tstat']})", flush=True)
    clears = [f for f, ics in h6_ics.items()
              if ics["ic_20d"]["mean_ic"] is not None
              and abs(ics["ic_20d"]["mean_ic"]) >= 0.03]
    if clears:
        print(f"[h6] IC gate cleared by: {clears}; building shock composite",
              flush=True)
        intra_rank = r2.pct_rank_xsec(h6["intraday_20d"]).fillna(0.0)
        over_rank = r2.pct_rank_xsec(h6["overnight_20d"]).fillna(0.0)
        shock_score = (intra_rank + over_rank) / 2.0
        shock_score = shock_score.where(shock_flag > 0, 0.0)
        res = eval_panel(prices, shock_score, max_positions=10)
        n_trials += 3
        confirm = _net10(res) > BAR_H4_H5_H6_BEAT_V2 and _wf(res) >= BAR_WF_FOLDS
        record("h6-shock-composite", "R2-H6 volume shock + intra/overnight",
               res, V2_TRAIN_NET10,
               f"train net10 > v2 {V2_TRAIN_NET10*100:.2f}%, WF >= {BAR_WF_FOLDS}/4",
               confirm)
    else:
        print("[h6] IC gate NOT cleared by any feature (|IC| < 0.03 @20d) "
              "-> H6 dropped at IC stage, no backtest", flush=True)
        results["designs"]["h6-shock-composite"] = {
            "hypothesis": "R2-H6 volume shock + intra/overnight",
            "confirm_holdout": False,
            "dropped_at": "IC gate: no feature |IC| >= 0.03 @20d",
            "results": None,
        }
        _persist(results)

    # --- H7: round-1 runner-ups (re-verified in R2, pick one)
    c6 = r2.c6_rank_panel(d["filings"], train_days)
    c1c = r2.c1c_panel(d["features"], train_days, sorted(d["score"].columns))
    h7 = {}
    for name, panel in (("h7-c6-textual-delta", c6), ("h7-c1c-reversion-rsi", c1c)):
        res = eval_panel(prices, panel, max_positions=5)
        n_trials += 3
        h7[name] = res
        print(f"[{name}] net10 {_net10(res)*100:+.2f}%  wf {_wf(res)}/4  "
              f"turn {res['cost_10bps']['avg_daily_turnover']:.3f}", flush=True)
    pick = max(h7, key=lambda n: (_net10(h7[n]), _wf(h7[n])))
    for name, res in h7.items():
        confirm = (name == pick and _net10(res) > BAR_H7_MIN_NET10
                   and _wf(res) >= BAR_WF_FOLDS)
        results["designs"][name] = {
            "hypothesis": "R2-H7 round-1 runner-up",
            "base_train_net10": 0.4897 if "c6" in name else 0.6406,
            "bar": f"picked by highest train net10; confirm if net10 > "
                   f"{BAR_H7_MIN_NET10*100:.0f}% and WF >= {BAR_WF_FOLDS}/4",
            "confirm_holdout": bool(confirm),
            "picked": name == pick,
            "results": res,
        }
    print(f"[h7] pick: {pick} -> "
          f"{'CONFIRM' if results['designs'][pick]['confirm_holdout'] else 'drop'}",
          flush=True)
    _persist(results)

    results["n_trials_end"] = n_trials
    results["n_trials_new"] = n_trials - N_TRIALS_START
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(results, indent=2))
    print(f"\n=== calibration done: {results['n_trials_new']} new trials, "
          f"n_trials {N_TRIALS_START} -> {n_trials} ===", flush=True)
    confirmed = [n for n, v in results["designs"].items() if v["confirm_holdout"]]
    print(f"CONFIRM: {confirmed if confirmed else 'none'}", flush=True)
    print(f"results -> {RESULTS_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
