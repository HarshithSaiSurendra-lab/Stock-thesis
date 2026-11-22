#!/usr/bin/env python3
"""Round-2 holdout confirmation (ONE per design that cleared its train bar).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE
stays 0. No API calls ($0). Reads experiments/results/sprint-r2-calibration.json
and confirms each design with confirm_holdout == True on the frozen
holdout (2026-08-18..2026-11-13). Gate = beat v4 base -4.33% @10bps AND
net positive @10bps; DSR (running n_trials from the calibration end,
+3 per confirmation), Sharpe, turnover, alpha vs SPY reported for every
confirmation. The frozen holdout is never retuned against: panels are
built on the FULL window with the same fixed construction as
calibration (same thresholds, same buffers, same horizons), then sliced
to holdout days. No parameter is chosen from holdout outcomes.

H5 note: the confirmation model is refit on ALL train rows (Ridge +
GBDT) and predicts holdout rows — the standard final-model refit; the
train walk-forward numbers and holdout numbers are therefore not
methodologically identical, and this is recorded in the results.

Run: python3 r2_confirm.py
Writes experiments/results/exp-r2-<design>-confirmation.json per
confirmed design, appends registry results entries, and logs all R2
train + holdout rows to the experiment Sheet.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest import BacktestConfig, run_backtest
from diagnostics import alpha_beta, deflated_sharpe
import r2_panels as r2

REPO = Path(__file__).parent
CAL_PATH = REPO / "experiments" / "results" / "sprint-r2-calibration.json"
REGISTRY_PATH = REPO / "experiments" / "registry.jsonl"
SHEET_ID = "1bJdCrPm2licM-arLLdIck2zAq15gWfa6ZNjrk14h3G4"
SHEET_CLI = "/opt/hatch/bin/hatch_gws_cli"

V4_BASE_HOLDOUT_NET10 = -0.0433
V4_BASE_HOLDOUT_NET20 = -0.0845
COSTS = (0, 10, 20)
TODAY = "2026-11-22"

# design name -> (registry experiment_id, max_positions, rebalance_every)
PANEL_DESIGNS = {
    "h2-banding-v3-buf30": ("exp-r2-h2a-banding", 60, 5),
    "h2-banding-v3-buf50": ("exp-r2-h2a-banding", 60, 5),
    "h2-banding-v2-buf30": ("exp-r2-h2a-banding", 60, 5),
    "h2-banding-v2-buf50": ("exp-r2-h2a-banding", 60, 5),
    "h3-lazy-veto-v3": ("exp-r2-h3-lazy-veto", 60, 5),
    "h3-lazy-veto-v2": ("exp-r2-h3-lazy-veto", 60, 5),
    "h4-mixture": ("exp-r2-h4-mixture", 60, 5),
    "h5-ensemble-veto": ("exp-r2-h5-ensemble", 60, 5),
    "h5-ensemble-noveto": ("exp-r2-h5-ensemble", 60, 5),
    "h6-shock-composite": ("exp-r2-h6-shock", 10, 5),
    "h7-c6-textual-delta": ("exp-r2-h7-runnerup", 5, 5),
    "h7-c1c-reversion-rsi": ("exp-r2-h7-runnerup", 5, 5),
}
COHORT_DESIGNS = {
    "h2-cohort-v3-h10": ("exp-r2-h2b-cohorts", 10, 60),
    "h2-cohort-v3-h20": ("exp-r2-h2b-cohorts", 20, 60),
    "h2-cohort-v2-h10": ("exp-r2-h2b-cohorts", 10, 60),
}


def _trade_count(weights: pd.DataFrame) -> int:
    w = weights.fillna(0.0)
    return int(((w > 0) & (w.shift(1).fillna(0.0) <= 0)).sum().sum())


def confirm_panel(prices_hw, panel_hw, spy_hw, max_positions, n_trials):
    """Backtest one panel on holdout days at costs 0/10/20 + DSR/alpha."""
    out = {}
    for cost in COSTS:
        res = run_backtest(
            prices_hw, panel_hw,
            BacktestConfig(cost_bps=float(cost), long_only=True,
                           max_positions=max_positions, rebalance_every=5,
                           min_hold_days=10))
        dsr = deflated_sharpe(res.daily_returns, n_trials)
        ab = alpha_beta(res.daily_returns, spy_hw)
        out[f"cost_{cost}bps"] = {
            "total_return": float(res.equity_curve.iloc[-1]) - 1.0,
            "sharpe": res.stats.get("sharpe"),
            "max_drawdown": res.stats.get("max_drawdown"),
            "hit_rate": res.stats.get("hit_rate"),
            "trades": _trade_count(res.weights),
            "avg_daily_turnover": float(res.stats.get("avg_daily_turnover")) if res.stats.get("avg_daily_turnover") is not None else None,
            "deflated_sharpe": dsr.get("deflated_sharpe"),
            "sharpe_null_max": dsr.get("sharpe_null_max"),
            "alpha_annual_vs_spy": ab.get("alpha_annual"),
            "beta_vs_spy": ab.get("beta"),
            "r_squared_vs_spy": ab.get("r_squared"),
        }
    return out


def confirm_cohort(prices_full, selection_full, holdout_days, spy_hw,
                   horizon, max_positions, n_trials):
    """Cohort confirmation: run cohorts on the full window, blend, then
    slice the blended daily returns to holdout days."""
    out = {}
    for cost in COSTS:
        blended, turnover, trades = r2.cohort_daily_returns(
            prices_full, selection_full, horizon, cost,
            min_hold_days=10, max_positions=max_positions,
            trades_from=holdout_days[0])
        hw = blended.loc[holdout_days]
        dsr = deflated_sharpe(hw, n_trials)
        ab = alpha_beta(hw, spy_hw)
        stats = r2._series_stats(hw)
        equity = (1 + hw).cumprod()
        max_dd = float((equity / equity.cummax() - 1).min()) if len(hw) else None
        out[f"cost_{cost}bps"] = {
            "total_return": stats["total_return"],
            "sharpe": stats["sharpe"],
            "max_drawdown": max_dd,
            "hit_rate": float((hw > 0).mean()) if len(hw) else None,
            "trades": trades,
            "avg_daily_turnover": turnover,
            "wf_median_sharpe": stats["wf_median_sharpe"],
            "wf_positive_folds": stats["wf_positive_folds"],
            "deflated_sharpe": dsr.get("deflated_sharpe"),
            "sharpe_null_max": dsr.get("sharpe_null_max"),
            "alpha_annual_vs_spy": ab.get("alpha_annual"),
            "beta_vs_spy": ab.get("beta"),
            "r_squared_vs_spy": ab.get("r_squared"),
        }
    return out


def build_full_panels(d):
    """Build every R2 panel on the FULL window (same fixed construction
    as calibration). Returns dict name -> panel (full-window index)."""
    days = d["days"]
    score = d["score"]
    composite = d["composite"]
    v2_sel = r2.v2_selection(score)
    v3_sel = r2.v3_selection(score, composite)
    panels = {}
    for base_name, base_sel in (("v3", v3_sel), ("v2", v2_sel)):
        for buffer_pct in (30, 50):
            panels[f"h2-banding-{base_name}-buf{buffer_pct}"] = \
                r2.banded_selection(base_sel, score, buffer_pct)
    sim = r2.lazy_prices_similarity(d["filings"])
    _, changer = r2.lazy_changer_panel(sim, days, sorted(score.columns))
    panels["h3-lazy-veto-v3"] = r2.lazy_veto_selection(v3_sel, changer)
    panels["h3-lazy-veto-v2"] = r2.lazy_veto_selection(v2_sel, changer)
    panels["h4-mixture"] = r2.mixture_panel(score, composite)
    # H5: refit on ALL train rows, predict holdout rows (final-model refit)
    X_train, y_train, _ = r2.build_ensemble_xy(
        d["features"], d["prices"], d["train_days"], horizon=20)
    X_hold, _, idx_hold = r2.build_ensemble_xy(
        d["features"], d["prices"], d["holdout_days"], horizon=20,
        require_target=False)
    ridge_hold = r2.ridge_fit_predict(X_train, y_train, X_hold)
    gbdt = r2.NativeGBDT(seed=7).fit(X_train, y_train)
    gbdt_hold = gbdt.predict(X_hold)
    cols = sorted(set(idx_hold.get_level_values("symbol")))
    ridge_panel = pd.DataFrame(np.nan, index=d["holdout_days"], columns=cols)
    gbdt_panel = pd.DataFrame(np.nan, index=d["holdout_days"], columns=cols)
    for (day, sym), v in zip(idx_hold, ridge_hold):
        ridge_panel.at[day, sym] = v
    for (day, sym), v in zip(idx_hold, gbdt_hold):
        gbdt_panel.at[day, sym] = v
    r_ridge = r2.pct_rank_xsec(ridge_panel).fillna(0.0)
    r_gbdt = r2.pct_rank_xsec(gbdt_panel).fillna(0.0)
    r_haiku = r2.pct_rank_xsec(score.loc[d["holdout_days"]].where(
        score.loc[d["holdout_days"]] > 0)).fillna(0.0)
    ens = (r_ridge + r_gbdt + r_haiku) / 3.0
    stacked = np.stack([r_ridge.values, r_gbdt.values, r_haiku.values], axis=2)
    dis = pd.DataFrame(np.nanstd(stacked, axis=2), index=d["holdout_days"],
                       columns=r_ridge.columns)
    sel = ens >= 0.9
    veto_ok = dis.le(dis.quantile(0.80, axis=1), axis=0)
    panels["h5-ensemble-veto"] = ens.where(sel & veto_ok, 0.0)
    panels["h5-ensemble-noveto"] = ens.where(sel, 0.0)
    # H6: shock composite (only built if it cleared the IC gate)
    h6 = r2.h6_features(d["data"], days)
    shock_flag = (h6["vol_pct"] >= 0.9).astype(float)
    intra_rank = r2.pct_rank_xsec(h6["intraday_20d"]).fillna(0.0)
    over_rank = r2.pct_rank_xsec(h6["overnight_20d"]).fillna(0.0)
    panels["h6-shock-composite"] = ((intra_rank + over_rank) / 2.0).where(
        shock_flag > 0, 0.0)
    # H7: runner-ups on the full window
    panels["h7-c6-textual-delta"] = r2.c6_rank_panel(d["filings"], days)
    panels["h7-c1c-reversion-rsi"] = r2.c1c_panel(
        d["features"], days, sorted(score.columns))
    return panels, v2_sel, v3_sel


def main() -> int:
    print("=== Round-2 holdout confirmation (ONE per cleared design) ===", flush=True)
    calib = json.loads(CAL_PATH.read_text())
    confirmed = [n for n, v in calib["designs"].items() if v.get("confirm_holdout")]
    print(f"confirmed for holdout: {confirmed if confirmed else 'none'}", flush=True)
    if not confirmed:
        print("No designs cleared their train bar; nothing to confirm.", flush=True)
        return 0
    d = r2.load_r2_data()
    holdout_days = d["holdout_days"]
    prices_hw = d["prices"].loc[holdout_days]
    spy_hw = d["full_prices"].loc[holdout_days]["SPY"].pct_change().dropna()
    panels, v2_sel, v3_sel = build_full_panels(d)
    n_trials = calib["n_trials_end"]
    results = {}

    for name in confirmed:
        n_trials += 3  # this confirmation's (design x cost) trials
        if name in PANEL_DESIGNS:
            exp_id, max_pos, _ = PANEL_DESIGNS[name]
            panel_hw = panels[name].loc[holdout_days]
            res = confirm_panel(prices_hw, panel_hw, spy_hw, max_pos, n_trials)
            method_note = "panel backtest, rebalance 5, min hold 10"
        elif name in COHORT_DESIGNS:
            exp_id, horizon, max_pos = COHORT_DESIGNS[name]
            base_sel = v3_sel if "v3" in name else v2_sel
            res = confirm_cohort(d["prices"], base_sel, holdout_days, spy_hw,
                                 horizon, max_pos, n_trials)
            method_note = (f"overlapping 1/{horizon} cohorts on full window, "
                           "blended returns sliced to holdout")
        else:
            raise RuntimeError(f"unknown confirmed design {name!r}")
        h10 = res["cost_10bps"]["total_return"]
        h20 = res["cost_20bps"]["total_return"]
        sg1 = h10 > V4_BASE_HOLDOUT_NET10
        sg2 = h20 > V4_BASE_HOLDOUT_NET20
        sg3 = h10 > 0.0
        verdict = "PASS" if (sg1 and sg2 and sg3) else "FAIL"
        train = calib["designs"][name]["results"]
        entry = {
            "experiment_id": exp_id,
            "design": name,
            "train": train,
            "holdout": res,
            "n_trials_dsr": n_trials,
            "method": method_note,
            "gate": {"sg1_holdout_net10_gt_v4_base": sg1,
                     "sg2_holdout_net20_gt_v4_base": sg2,
                     "sg3_holdout_net10_positive": sg3,
                     "verdict": verdict,
                     "v4_base_holdout_net10": V4_BASE_HOLDOUT_NET10,
                     "v4_base_holdout_net20": V4_BASE_HOLDOUT_NET20},
        }
        if name.startswith("h5-"):
            entry["method"] += ("; H5 final-model refit on all train rows "
                                "(not the walk-forward folds used for train)")
        out_path = REPO / "experiments" / "results" / f"exp-r2-{name}-confirmation.json"
        out_path.write_text(json.dumps(entry, indent=2, default=str))
        results[name] = entry
        print(f"[{name}] holdout net10 {h10*100:+.2f}%  net20 {h20*100:+.2f}%  "
              f"DSR {res['cost_10bps']['deflated_sharpe']} (n_trials {n_trials})  "
              f"alpha/yr {res['cost_10bps']['alpha_annual_vs_spy']}  -> {verdict}",
              flush=True)
        # registry results entry
        reg = {
            "experiment_id": f"{exp_id}-results",
            "design": name,
            "strategy": f"RESULTS for {name} (round 2).",
            "holdout_rule": "final 63 trading days 2026-08-18..2026-11-13, frozen before any tuning",
            "results_file": str(out_path),
            "gate": (f"GATE VERDICT: {verdict}. Frozen holdout: net10 "
                     f"{h10*100:+.2f}% (SG1 vs v4 base -4.33%: "
                     f"{'pass' if sg1 else 'fail'}), net20 {h20*100:+.2f}% "
                     f"(SG2 vs base -8.45%: {'pass' if sg2 else 'fail'}), "
                     f"SG3 net10 > 0: {'pass' if sg3 else 'fail'}. Train "
                     f"net10 {train['cost_10bps']['total_return']*100:+.2f}%. "
                     f"Deflated Sharpe {res['cost_10bps']['deflated_sharpe']} "
                     f"(n_trials={n_trials}). Alpha vs SPY "
                     f"{res['cost_10bps']['alpha_annual_vs_spy']}/yr. "
                     f"{'Variant confirmed on holdout.' if verdict == 'PASS' else 'Variant DROPPED per the kill rule. No paper trading.'}"),
            "parameters": {"n_trials_dsr": n_trials,
                           "train_net10": train["cost_10bps"]["total_return"],
                           "holdout_net10": h10, "holdout_net20": h20},
            "spend": {"run_usd": 0.0},
            "status": "passed" if verdict == "PASS" else "failed",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        with REGISTRY_PATH.open("a") as fh:
            fh.write(json.dumps(reg, sort_keys=True) + "\n")

    # --- Log all R2 train rows + confirmed holdout rows to the Sheet.
    # NOTE: the Sheets CLI's --json-values parser accepts STRINGS ONLY:
    # any number or null makes it reject the whole payload (sending an
    # empty values array while still exiting 0). Stringify everything.
    def _s(v):
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.4f}"
        return str(v)

    rows = []
    exp_map = {**PANEL_DESIGNS, **COHORT_DESIGNS}
    for design_name, design in calib["designs"].items():
        if design.get("results") is None:
            continue
        exp_id = exp_map[design_name][0] if design_name in exp_map \
            else "exp-llm-long-horizon-r2"
        for cost in COSTS:
            r = design["results"][f"cost_{cost}bps"]
            rows.append([_s(exp_id), TODAY, "train", design_name, _s(cost),
                         _s(round(r["total_return"], 4)),
                         _s(r.get("sharpe")), "", _s(r.get("trades")),
                         _s(r.get("wf_median_sharpe")),
                         _s(r.get("wf_positive_folds")),
                         "round 2 calibration (train-only)"])
    for name, entry in results.items():
        exp_id = exp_map[name][0]
        for cost in COSTS:
            r = entry["holdout"][f"cost_{cost}bps"]
            rows.append([_s(exp_id), TODAY, "holdout", name, _s(cost),
                         _s(round(r["total_return"], 4)),
                         _s(r.get("sharpe")), _s(r.get("max_drawdown")),
                         _s(r.get("trades")),
                         _s(r.get("wf_median_sharpe")),
                         _s(r.get("wf_positive_folds")),
                         f"round 2 holdout confirmation: {entry['gate']['verdict']}; "
                         f"DSR {r.get('deflated_sharpe')} (n_trials {entry['n_trials_dsr']})"])
    print(f"[sheet] appending {len(rows)} rows (train + holdout)...", flush=True)
    proc = subprocess.run(
        [SHEET_CLI, "sheets", "+append", "--spreadsheet", SHEET_ID,
         "--json-values", json.dumps(rows)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(f"[sheet] append FAILED: {proc.stdout} {proc.stderr}", flush=True)
        raise RuntimeError("Sheet append failed; rows NOT logged")
    # Verify by reading back: the row count must have grown by len(rows).
    verify = subprocess.run(
        [SHEET_CLI, "sheets", "+read", "--spreadsheet", SHEET_ID,
         "--range", "Sheet1!A1:L500"],
        capture_output=True, text=True, timeout=120)
    try:
        n_rows = len(json.loads(verify.stdout).get("values", []))
    except (ValueError, AttributeError):
        raise RuntimeError("Sheet read-back failed; cannot verify append")
    print(f"[sheet] read-back: {n_rows} total rows; expected at least "
          f"{113 + len(rows)} (113 before + {len(rows)} appended)", flush=True)
    if n_rows < 113 + len(rows):
        raise RuntimeError("Sheet append did not land (row count unchanged); "
                           "rows NOT logged")
    print(f"n_trials after confirmations: {n_trials}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
