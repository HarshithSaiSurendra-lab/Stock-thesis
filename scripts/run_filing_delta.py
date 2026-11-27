#!/usr/bin/env python3
"""Run exp-filing-delta-v1: Haiku ranking vs Haiku + filing-delta tilt.

TRAIN PERIOD ONLY (decision days 2024-01-01..2026-08-17). No new LLM
calls ($0). Reuses the registered v2 baseline machinery exactly:
r2_panels.cohort_returns (H=5 weekly rebalance, min hold 10 days,
1-bar lag, long-only, max_positions 60), costs 0/10/20bps, and the
established fwd_returns / rank_ic_and_spread IC machinery. The only
difference between the arms is the selection panel.

Outputs (experiments/results/):
  exp-filing-delta-v1.json      — full results + per-event IC table
  exp-filing-delta-v1.md        — human-readable results + passage pairs
  exp-filing-delta-v1-delta-table.json — per-filing delta + evidence

Usage:
    .venv/bin/python scripts/run_filing_delta.py [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import filing_delta as fd
import r2_panels as r2
import run_llm_backtest as p2
from analyze_sprint_patterns import fwd_returns
from backtest import BacktestConfig, run_backtest
from sector_map import sector_benchmark_for

REPO = Path(__file__).resolve().parent.parent
HORIZON = 5            # cohort rebalance cycle (weekly), v2 design
MIN_HOLD_DAYS = 10     # v2 design
MAX_POSITIONS = 60     # v2 design
COSTS = (0, 10, 20)    # bps, v2 design
IC_HORIZON = 20        # trading days, baseline IC horizon
TILT = 0.25            # frozen, see design doc
MIN_DAY_EVENTS = 20    # rank_ic_and_spread threshold
NW_LAG = IC_HORIZON - 1  # 19, matching the 20-session overlap
N_BOOT = 500
BLOCK_LEN = 20


# ----------------------------------------------------------------------
# Cohort loop with weights (mirrors r2.cohort_daily_returns exactly,
# and is verified against it before use)
# ----------------------------------------------------------------------
def cohort_run_with_weights(prices, selection, horizon, cost,
                            min_hold_days=10, max_positions=60):
    """Blended daily returns + mean turnover + trades + blended weights.

    Cohort o rebalances every `horizon` days from offset o; the
    blended portfolio is the equal-capital mean of cohort daily
    returns, and the blended weights are the mean of cohort weights
    (the lagged, min-hold-enforced weights run_backtest returns).
    """
    n = len(prices)
    cohort_rets, turnovers, trades = [], [], 0
    weight_sum = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for o in range(horizon):
        grid_idx = [i for i in range(n) if i % horizon == o]
        keep = pd.Series(False, index=selection.index)
        keep.iloc[grid_idx] = True
        cohort_panel = selection.copy().where(keep, np.nan).ffill().fillna(0.0)
        res = run_backtest(
            prices, cohort_panel,
            BacktestConfig(cost_bps=float(cost), long_only=True,
                           max_positions=max_positions, rebalance_every=1,
                           min_hold_days=min_hold_days))
        cohort_rets.append(res.daily_returns)
        turnovers.append(float(res.stats.get("avg_daily_turnover") or 0.0))
        w = res.weights.fillna(0.0)
        weight_sum = weight_sum.add(w, fill_value=0.0)
        entries = (w > 0) & (w.shift(1).fillna(0.0) <= 0)
        trades += int(entries.sum().sum())
    blended = pd.concat(cohort_rets, axis=1).mean(axis=1)
    return {
        "daily_returns": blended,
        "turnover": float(np.mean(turnovers)) if turnovers else None,
        "trades": trades,
        "weights": weight_sum / horizon,
    }


def max_drawdown(returns: pd.Series) -> float:
    eq = (1 + returns).cumprod()
    return float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0


def total_return(returns: pd.Series) -> float:
    return float((1 + returns).prod() - 1.0) if len(returns) else 0.0


# ----------------------------------------------------------------------
# IC helpers (Spearman = Pearson on ranks)
# ----------------------------------------------------------------------
def daily_ics(feature: pd.DataFrame, fwd: pd.DataFrame,
              min_n: int = MIN_DAY_EVENTS) -> pd.Series:
    """Per-day cross-sectional Spearman IC, days with >= min_n pairs."""
    out = {}
    for day in feature.index.intersection(fwd.index):
        f = feature.loc[day].dropna()
        r = fwd.loc[day].dropna()
        both = f.index.intersection(r.index)
        if len(both) < min_n:
            continue
        ic = float(f.loc[both].rank().corr(r.loc[both].rank(), method="pearson"))
        if not np.isnan(ic):
            out[day] = ic
    return pd.Series(out)


def newey_west_se(x: pd.Series, lag: int) -> float:
    """Newey-West SE of the mean (Bartlett weights, given lag)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return float("nan")
    mean = x.mean()
    resid = x - mean
    var = float((resid ** 2).sum()) / n
    for k in range(1, lag + 1):
        w = 1.0 - k / (lag + 1)
        var += 2 * w * float((resid[k:] * resid[:-k]).sum()) / n
    return float(np.sqrt(var / n))


def sector_relative_fwd(fwd: pd.DataFrame) -> pd.DataFrame:
    """Stock 20d forward return minus same-sector peer mean (excl. self).

    Sectors from sector_map.py. Symbols without a mapping, or with no
    usable peer that day, are NaN (excluded from the IC, counted).
    """
    symbols = list(fwd.columns)
    sector_of = {s: sector_benchmark_for(s) for s in symbols}
    by_sector: dict[str, list[str]] = {}
    for s, b in sector_of.items():
        if b:
            by_sector.setdefault(b, []).append(s)
    out = pd.DataFrame(np.nan, index=fwd.index, columns=symbols)
    for s in symbols:
        b = sector_of[s]
        if not b:
            continue
        peers = [p for p in by_sector[b] if p != s]
        if not peers:
            continue
        out[s] = fwd[s] - fwd[peers].mean(axis=1)
    return out


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Run exp-filing-delta-v1 (train only).")
    ap.add_argument("--out-dir", default=str(REPO / "experiments" / "results"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    print("loading r2 data (cached bars + scores, $0) ...", flush=True)
    d = r2.load_r2_data()
    train_days = d["train_days"]
    days = d["days"]
    prices = d["prices"]            # full research window
    prices_train = prices.loc[train_days]
    score = d["score"].loc[train_days]
    variants = d["variants"]
    filings = d["filings"]
    print(f"train days {train_days[0].date()}..{train_days[-1].date()} "
          f"({len(train_days)}); window {days[0].date()}..{days[-1].date()}",
          flush=True)

    # --- in-scope filings + delta table -------------------------------
    print("scoping in-scope filings ...", flush=True)
    in_scope = p2.in_scope_filings(filings, variants, train_days)
    print(f"in-scope filings: {len(in_scope)}", flush=True)
    print("computing filing deltas (deterministic, $0) ...", flush=True)
    delta_table = fd.build_delta_table(filings, in_scope)
    known = delta_table["delta"].notna()
    print(f"known delta: {int(known.sum())}/{len(delta_table)}; "
          f"unknown by reason: "
          f"{delta_table.loc[~known, 'exclusion_reason'].value_counts().to_dict()}",
          flush=True)
    delta_table_path = out_dir / "exp-filing-delta-v1-delta-table.json"
    delta_table.to_json(delta_table_path, orient="records", date_format="iso",
                        indent=1)
    print(f"wrote {delta_table_path}", flush=True)

    # --- panels + selections ------------------------------------------
    delta_panel = fd.carry_forward_delta(
        delta_table, train_days, list(score.columns))
    score_adj = fd.tilted_score_panel(score, delta_panel, tilt=TILT)
    base_sel = r2.v2_selection(score)
    exp_sel = r2.v2_selection(score_adj)
    sel_diff_days = int((base_sel != exp_sel).any(axis=1).sum())
    print(f"selection differs on {sel_diff_days}/{len(train_days)} train days",
          flush=True)

    # --- portfolio backtests (both arms, identical machinery) ---------
    arms = {}
    for name, sel in (("baseline", base_sel), ("experiment", exp_sel)):
        arms[name] = {}
        for cost in COSTS:
            # Headline via the established function; weights via the
            # mirrored loop, verified to reproduce it exactly.
            ref_blended, ref_turn, ref_trades = r2.cohort_daily_returns(
                prices_train, sel, HORIZON, cost,
                min_hold_days=MIN_HOLD_DAYS, max_positions=MAX_POSITIONS)
            run = cohort_run_with_weights(
                prices_train, sel, HORIZON, cost,
                min_hold_days=MIN_HOLD_DAYS, max_positions=MAX_POSITIONS)
            if not np.allclose(run["daily_returns"].reindex(ref_blended.index).fillna(0.0),
                               ref_blended.fillna(0.0), atol=1e-12):
                raise RuntimeError(f"cohort loop mismatch vs cohort_daily_returns ({name}, {cost}bps)")
            stats = r2._series_stats(ref_blended)
            arms[name][f"cost_{cost}bps"] = {
                "total_return": stats["total_return"],
                "sharpe": stats["sharpe"],
                "wf_median_sharpe": stats["wf_median_sharpe"],
                "wf_positive_folds": stats["wf_positive_folds"],
                "avg_daily_turnover": ref_turn,
                "trades": ref_trades,
                "max_drawdown": max_drawdown(ref_blended),
            }
            arms[name][f"cost_{cost}bps"]["daily_returns"] = run["daily_returns"]
            arms[name][f"cost_{cost}bps"]["weights"] = run["weights"]
        print(f"{name}: net10 {arms[name]['cost_10bps']['total_return']*100:.2f}% "
              f"turnover {arms[name]['cost_10bps']['avg_daily_turnover']:.4f} "
              f"trades {arms[name]['cost_10bps']['trades']}", flush=True)

    # --- IC: 20d sector-relative --------------------------------------
    fwd20 = fwd_returns(prices, IC_HORIZON)
    fwd20_rel = sector_relative_fwd(fwd20)
    # Boundary rule (frozen): the full forward window must complete by
    # the last train day. fwd_returns(20) at day t spans close(t+1) to
    # close(t+21), so day t is usable iff t+21 <= last train day.
    last_train_pos = int(days.get_indexer([train_days[-1]])[0])
    usable = pd.Series(True, index=days)
    usable.iloc[last_train_pos - IC_HORIZON:] = False  # t+21 > train end
    fwd20_rel = fwd20_rel.where(usable, np.nan)
    fwd_train = fwd20_rel.loc[train_days]

    ic = {}
    daily = {}
    for label, panel in (("haiku", score), ("score_adj", score_adj),
                         ("delta", delta_panel)):
        s = daily_ics(panel, fwd_train)
        daily[label] = s
        nw_se = newey_west_se(s, NW_LAG)
        ic[label] = {
            "n_days": int(len(s)),
            "mean_ic": float(s.mean()) if len(s) else None,
            "ic_tstat_naive": (float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s))))
                               if len(s) > 1 else None),
            "nw_se": nw_se,
            "nw_tstat": float(s.mean() / nw_se) if len(s) and nw_se and nw_se > 0 else None,
            "mean_symbols_per_day": None,
        }
        # Coverage: mean symbols/day entering the daily IC.
        counts = []
        for day in s.index:
            f = panel.loc[day].dropna()
            r = fwd_train.loc[day].dropna()
            counts.append(len(f.index.intersection(r.index)))
        ic[label]["mean_symbols_per_day"] = float(np.mean(counts)) if counts else None
        print(f"IC {label}: n_days={len(s)} mean={ic[label]['mean_ic']} "
              f"nw_t={ic[label]['nw_tstat']}", flush=True)
    ic_change = ic["score_adj"]["mean_ic"] - ic["haiku"]["mean_ic"]
    # Cross-check my daily-IC helper against the established function.
    from analyze_sprint_patterns import rank_ic_and_spread as _est_ic
    _est = _est_ic(score, fwd_train)
    assert _est["mean_ic"] is not None and abs(_est["mean_ic"] - ic["haiku"]["mean_ic"]) < 1e-3, \
        f"daily_ics disagrees with rank_ic_and_spread: {_est['mean_ic']} vs {ic['haiku']['mean_ic']}"
    print(f"verified daily_ics vs rank_ic_and_spread: {_est['mean_ic']} "
          f"(n_days={_est['n_days']})", flush=True)
    # Paired daily difference (same days in both series).
    common_days = daily["score_adj"].index.intersection(daily["haiku"].index)
    diff_daily = daily["score_adj"].loc[common_days] - daily["haiku"].loc[common_days]
    diff_nw_se = newey_west_se(diff_daily, NW_LAG)

    # --- issuer-cluster bootstrap for the IC change --------------------
    # Event set: (day, symbol) with score, score_adj, delta, fwd all known.
    events = []
    fwd_stack = fwd_train.stack()
    for (day, sym), fval in fwd_stack.items():
        h = score.at[day, sym] if sym in score.columns else np.nan
        a = score_adj.at[day, sym] if sym in score_adj.columns else np.nan
        dl = delta_panel.at[day, sym] if sym in delta_panel.columns else np.nan
        if pd.isna(h) or pd.isna(a) or pd.isna(fval):
            continue
        events.append({"day": day, "symbol": sym, "haiku": float(h),
                       "score_adj": float(a), "delta": (None if pd.isna(dl) else float(dl)),
                       "fwd_rel": float(fval)})
    events_df = pd.DataFrame(events)
    print(f"IC event set: {len(events_df)} (day, symbol) pairs, "
          f"{events_df['symbol'].nunique()} issuers, "
          f"{events_df['day'].nunique()} days", flush=True)

    def mean_daily_ic(df, col, min_n=MIN_DAY_EVENTS):
        """Mean daily cross-sectional Spearman IC on an event frame."""
        ics = []
        for _, g in df.groupby("day"):
            if len(g) < min_n:
                continue
            ic = float(g[col].rank().corr(g["fwd_rel"].rank(), method="pearson"))
            if not np.isnan(ic):
                ics.append(ic)
        return float(np.mean(ics)) if ics else float("nan")

    rng = np.random.default_rng(20261126)
    tickers = sorted(events_df["symbol"].unique())
    by_ticker = {t: events_df[events_df["symbol"] == t] for t in tickers}
    boot_diffs = []
    for _ in range(N_BOOT):
        sampled = rng.choice(tickers, size=len(tickers), replace=True)
        parts = [by_ticker[t] for t in sampled]
        boot = pd.concat(parts, ignore_index=True)
        boot_diffs.append(mean_daily_ic(boot, "score_adj")
                          - mean_daily_ic(boot, "haiku"))
    boot_diffs = np.array(boot_diffs)
    ic_boot = {
        "n_draws": N_BOOT,
        "mean_diff": float(np.nanmean(boot_diffs)),
        "ci_95": [float(np.nanpercentile(boot_diffs, 2.5)),
                  float(np.nanpercentile(boot_diffs, 97.5))],
        "frac_positive": float(np.nanmean(boot_diffs > 0)),
    }
    print(f"issuer-cluster bootstrap IC change: mean {ic_boot['mean_diff']:.4f} "
          f"95% CI [{ic_boot['ci_95'][0]:.4f}, {ic_boot['ci_95'][1]:.4f}]",
          flush=True)

    # --- time-block bootstrap for the portfolio return difference -----
    r_base = arms["baseline"]["cost_10bps"]["daily_returns"]
    r_exp = arms["experiment"]["cost_10bps"]["daily_returns"]
    paired = pd.DataFrame({"base": r_base, "exp": r_exp}).dropna()
    n = len(paired)
    boot_port = []
    for _ in range(N_BOOT):
        idx = []
        while len(idx) < n:
            start = int(rng.integers(0, n - BLOCK_LEN + 1))
            idx.extend(range(start, start + BLOCK_LEN))
        idx = idx[:n]
        sample = paired.iloc[idx]
        boot_port.append(total_return(sample["exp"]) - total_return(sample["base"]))
    boot_port = np.array(boot_port)
    port_boot = {
        "n_draws": N_BOOT, "block_len": BLOCK_LEN,
        "mean_diff": float(np.mean(boot_port)),
        "ci_95": [float(np.percentile(boot_port, 2.5)),
                  float(np.percentile(boot_port, 97.5))],
        "frac_positive": float(np.mean(boot_port > 0)),
    }
    net10_diff = (arms["experiment"]["cost_10bps"]["total_return"]
                  - arms["baseline"]["cost_10bps"]["total_return"])
    print(f"portfolio net10 diff: {net10_diff*100:.2f}pp; block bootstrap "
          f"95% CI [{port_boot['ci_95'][0]*100:.2f}pp, {port_boot['ci_95'][1]*100:.2f}pp]",
          flush=True)

    # --- sensitivity 1: remove the largest contributor -----------------
    fwd_ret_daily = prices_train.pct_change(fill_method=None).shift(-1)
    w_exp = arms["experiment"]["cost_10bps"]["weights"]
    w_base = arms["baseline"]["cost_10bps"]["weights"]
    attr = ((w_exp - w_base) * fwd_ret_daily).sum(axis=0).fillna(0.0)
    top_issuer = str(attr.abs().idxmax())
    print(f"largest contributor: {top_issuer} "
          f"(attribution {attr[top_issuer]*100:.2f}pp)", flush=True)
    loo = {}
    for arm_name, sel in (("baseline", base_sel), ("experiment", exp_sel)):
        sel_loo = sel.copy()
        sel_loo[top_issuer] = 0.0
        loo[arm_name] = {}
        for cost in (10,):
            blended, turn, trades = r2.cohort_daily_returns(
                prices_train, sel_loo, HORIZON, cost,
                min_hold_days=MIN_HOLD_DAYS, max_positions=MAX_POSITIONS)
            stats = r2._series_stats(blended)
            loo[arm_name][f"cost_{cost}bps"] = {
                "total_return": stats["total_return"],
                "avg_daily_turnover": turn, "trades": trades,
                "max_drawdown": max_drawdown(blended),
            }
    loo_ic = {}
    for label, panel in (("haiku", score), ("score_adj", score_adj)):
        p = panel.drop(columns=[top_issuer])
        s = daily_ics(p, fwd_train)
        loo_ic[label] = {"n_days": int(len(s)),
                         "mean_ic": float(s.mean()) if len(s) else None}
    loo_ic_change = loo_ic["score_adj"]["mean_ic"] - loo_ic["haiku"]["mean_ic"]

    # --- sensitivity 2: delay experiment entry one extra session -------
    exp_sel_delayed = exp_sel.shift(1).fillna(0.0)
    delayed = {}
    for cost in COSTS:
        blended, turn, trades = r2.cohort_daily_returns(
            prices_train, exp_sel_delayed, HORIZON, cost,
            min_hold_days=MIN_HOLD_DAYS, max_positions=MAX_POSITIONS)
        stats = r2._series_stats(blended)
        delayed[f"cost_{cost}bps"] = {
            "total_return": stats["total_return"], "sharpe": stats["sharpe"],
            "avg_daily_turnover": turn, "trades": trades,
            "max_drawdown": max_drawdown(blended),
        }
    print(f"delayed entry net10: {delayed['cost_10bps']['total_return']*100:.2f}% "
          f"(vs experiment {arms['experiment']['cost_10bps']['total_return']*100:.2f}%)",
          flush=True)

    # --- coverage ------------------------------------------------------
    delta_known_days = int((delta_panel.notna().any(axis=1)).sum())
    sel_known_days = int(((base_sel > 0) & delta_panel.notna()).any(axis=1).sum())
    n_sent = delta_table.loc[known, "n_sentences"]
    coverage = {
        "input_dates": f"{train_days[0].date()}..{train_days[-1].date()}",
        "n_in_scope_filings": int(len(delta_table)),
        "n_known_delta": int(known.sum()),
        "n_unknown": int((~known).sum()),
        "unknown_by_reason": delta_table.loc[~known, "exclusion_reason"].value_counts().to_dict(),
        "distinct_issuers_known_delta": int(delta_table.loc[known, "ticker"].nunique()),
        "distinct_event_dates_known_delta": int(delta_table.loc[known, "filing_date"].nunique()),
        "n_ic_events": int(len(events_df)),
        "n_ic_issuers": int(events_df["symbol"].nunique()),
        "n_ic_days": int(events_df["day"].nunique()),
        "n_ic_excluded_boundary": None,  # filled below
        "n_ic_excluded_no_sector": None,
        "train_days_with_known_delta": delta_known_days,
        "train_days_with_known_delta_in_selection": sel_known_days,
        "n_sentences_median": float(n_sent.median()) if len(n_sent) else None,
        "n_sentences_mean": float(n_sent.mean()) if len(n_sent) else None,
        "frac_known_delta_lt_10_sentences": float((n_sent < 10).mean()) if len(n_sent) else None,
        "selection_differs_days": sel_diff_days,
    }
    # Boundary + sector exclusions, counted from the in-scope known-delta set.
    boundary_cutoff_pos = last_train_pos - IC_HORIZON  # decision day t needs t+21 <= train end
    n_boundary = 0
    n_no_sector = 0
    for _, row in delta_table.loc[known].iterrows():
        if pd.isna(row["filing_date"]):
            continue
        # First trading day >= filing_date.
        pos = int(days.get_indexer([pd.Timestamp(row["filing_date"])], method="bfill")[0])
        if pos + IC_HORIZON + 1 > last_train_pos:
            n_boundary += 1
        elif sector_benchmark_for(row["ticker"]) is None:
            n_no_sector += 1
    coverage["n_ic_excluded_boundary"] = int(n_boundary)
    coverage["n_ic_excluded_no_sector"] = int(n_no_sector)

    # --- passage pairs: top-5 highest-delta filings --------------------
    top5 = (delta_table.loc[known]
            .sort_values(["delta", "filing_date"], ascending=[False, True])
            .head(5))
    passages = []
    for _, row in top5.iterrows():
        passages.append({
            "ticker": row["ticker"], "filing_date": str(row["filing_date"].date()),
            "form": row["form"], "accession": row["accession"],
            "prior_accession": row["prior_accession"],
            "delta": float(row["delta"]), "n_sentences": int(row["n_sentences"]),
            "n_new": int(row["n_new"]),
            "strongest_passage": row["strongest_passage"],
            "strongest_jaccard": float(row["strongest_jaccard"]),
            "closest_prior_match": row["closest_prior_match"],
            "closest_match_jaccard": float(row["closest_match_jaccard"]),
        })

    # --- assemble results ----------------------------------------------
    def arm_table(arm):
        return {k: {kk: vv for kk, vv in v.items()
                    if kk not in ("daily_returns", "weights")}
                for k, v in arm.items()}

    results = {
        "experiment_id": "exp-filing-delta-v1",
        "registered_design": "experiments/exp-filing-delta-v1-design.md",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "spend_usd": 0.0,
        "window": {"train_start": str(train_days[0].date()),
                   "train_end": str(train_days[-1].date()),
                   "n_train_days": int(len(train_days))},
        "frozen": {"tilt": TILT, "new_jaccard_threshold": fd.NEW_JACCARD_THRESHOLD,
                   "ic_horizon": IC_HORIZON, "cohort_horizon": HORIZON,
                   "min_hold_days": MIN_HOLD_DAYS, "max_positions": MAX_POSITIONS,
                   "costs_bps": list(COSTS)},
        "coverage": coverage,
        "ic": {**ic, "change_score_adj_minus_haiku": float(ic_change),
               "diff_daily_nw_se": float(diff_nw_se),
               "diff_daily_nw_tstat": float(diff_daily.mean() / diff_nw_se)
               if diff_nw_se and diff_nw_se > 0 else None,
               "issuer_cluster_bootstrap": ic_boot},
        "portfolio": {"baseline": arm_table(arms["baseline"]),
                      "experiment": arm_table(arms["experiment"]),
                      "net_return_diff_pp": {f"cost_{c}bps":
                          (arms["experiment"][f"cost_{c}bps"]["total_return"]
                           - arms["baseline"][f"cost_{c}bps"]["total_return"]) * 100
                          for c in COSTS},
                      "time_block_bootstrap_10bps": port_boot},
        "sensitivity_leave_one_out": {
            "removed_issuer": top_issuer,
            "attribution_pp": float(attr[top_issuer] * 100),
            "baseline_net10": loo["baseline"]["cost_10bps"]["total_return"],
            "experiment_net10": loo["experiment"]["cost_10bps"]["total_return"],
            "net10_diff_pp": (loo["experiment"]["cost_10bps"]["total_return"]
                              - loo["baseline"]["cost_10bps"]["total_return"]) * 100,
            "ic_haiku": loo_ic["haiku"], "ic_score_adj": loo_ic["score_adj"],
            "ic_change": float(loo_ic_change),
        },
        "sensitivity_delay_entry_1d": {
            f"cost_{c}bps": delayed[f"cost_{c}bps"] for c in COSTS},
        "passages_top5_delta": passages,
        "runtime_seconds": round(time.time() - t0, 1),
    }
    json_path = out_dir / "exp-filing-delta-v1.json"
    json_path.write_text(json.dumps(results, indent=1, default=str))
    print(f"wrote {json_path} ({time.time() - t0:.0f}s)", flush=True)

    # Per-event IC table (auditability) — appended to the same JSON file
    # as a second document is not valid JSON; write a sidecar instead.
    events_path = out_dir / "exp-filing-delta-v1-events.json"
    events_df.to_json(events_path, orient="records", date_format="iso", indent=1)
    print(f"wrote {events_path} ({len(events_df)} events)", flush=True)
    write_markdown(results, out_dir / "exp-filing-delta-v1.md")
    print(f"wrote {out_dir / 'exp-filing-delta-v1.md'}", flush=True)
    return 0


def write_markdown(res: dict, path: Path) -> None:
    ic = res["ic"]
    port = res["portfolio"]
    cov = res["coverage"]
    loo = res["sensitivity_leave_one_out"]
    delay = res["sensitivity_delay_entry_1d"]
    lines = []
    lines.append("# exp-filing-delta-v1 — results (TRAIN ONLY)")
    lines.append("")
    lines.append(f"Run at {res['run_at_utc']} UTC; $0 new spend; "
                 f"runtime {res['runtime_seconds']}s. Window "
                 f"{res['window']['train_start']}..{res['window']['train_end']} "
                 f"({res['window']['n_train_days']} train days). Frozen tilt "
                 f"{res['frozen']['tilt']}, new-Jaccard threshold "
                 f"{res['frozen']['new_jaccard_threshold']}, IC horizon "
                 f"{res['frozen']['ic_horizon']}d, cohort H="
                 f"{res['frozen']['cohort_horizon']}, min hold "
                 f"{res['frozen']['min_hold_days']}d, max positions "
                 f"{res['frozen']['max_positions']}.")
    lines.append("")
    lines.append("## Coverage")
    lines.append("")
    lines.append(f"- In-scope filings: {cov['n_in_scope_filings']}; known delta: "
                 f"{cov['n_known_delta']}; unknown: {cov['n_unknown']} "
                 f"({cov['unknown_by_reason']}).")
    lines.append(f"- Known-delta issuers: {cov['distinct_issuers_known_delta']}; "
                 f"event dates: {cov['distinct_event_dates_known_delta']}.")
    lines.append(f"- IC events: {cov['n_ic_events']} pairs, "
                 f"{cov['n_ic_issuers']} issuers, {cov['n_ic_days']} days; "
                 f"excluded by boundary rule: {cov['n_ic_excluded_boundary']}; "
                 f"excluded (no sector mapping): {cov['n_ic_excluded_no_sector']}.")
    lines.append(f"- Train days with a known delta: "
                 f"{cov['train_days_with_known_delta']}/{res['window']['n_train_days']}; "
                 f"with a known delta in the selection: "
                 f"{cov['train_days_with_known_delta_in_selection']}.")
    lines.append(f"- Risk sentences per known-delta filing: median "
                 f"{cov['n_sentences_median']:.0f}, mean {cov['n_sentences_mean']:.1f}; "
                 f"fraction with <10 sentences: "
                 f"{cov['frac_known_delta_lt_10_sentences']:.2f}.")
    lines.append(f"- Selection differs between arms on "
                 f"{cov['selection_differs_days']} train days.")
    lines.append("")
    lines.append("## Rank correlation (primary): 20d sector-relative IC")
    lines.append("")
    lines.append("| series | n days | mean IC | NW t (lag 19) | mean symbols/day |")
    lines.append("|---|---|---|---|---|")
    for label in ("haiku", "score_adj", "delta"):
        r = ic[label]
        lines.append(f"| {label} | {r['n_days']} | {r['mean_ic']:.4f} | "
                     f"{r['nw_tstat'] if r['nw_tstat'] is not None else 'n/a'} | "
                     f"{r['mean_symbols_per_day']:.1f} |")
    lines.append("")
    lines.append(f"IC change (score_adj − Haiku): {ic['change_score_adj_minus_haiku']:.4f} "
                 f"(paired daily NW SE {ic['diff_daily_nw_se']:.4f}, "
                 f"t {ic['diff_daily_nw_tstat']}).")
    boot = ic["issuer_cluster_bootstrap"]
    lines.append(f"Issuer-cluster bootstrap (500 draws): mean change "
                 f"{boot['mean_diff']:.4f}, 95% CI [{boot['ci_95'][0]:.4f}, "
                 f"{boot['ci_95'][1]:.4f}], fraction positive "
                 f"{boot['frac_positive']:.2f}.")
    lines.append("")
    lines.append("## Portfolio: baseline (v2 Haiku) vs experiment (Haiku + delta tilt)")
    lines.append("")
    lines.append("| cost | arm | net return | Sharpe | max DD | turnover/day | trades |")
    lines.append("|---|---|---|---|---|---|---|")
    for cost in (0, 10, 20):
        for arm in ("baseline", "experiment"):
            s = port[arm][f"cost_{cost}bps"]
            lines.append(f"| {cost}bps | {arm} | {s['total_return']*100:.2f}% | "
                         f"{s['sharpe'] if s['sharpe'] is not None else 'n/a'} | "
                         f"{s['max_drawdown']*100:.2f}% | {s['avg_daily_turnover']:.4f} | "
                         f"{s['trades']} |")
    lines.append("")
    d10 = port["net_return_diff_pp"]["cost_10bps"]
    pb = port["time_block_bootstrap_10bps"]
    lines.append(f"Net-return difference at 10bps: {d10:.2f}pp. Time-block "
                 f"bootstrap (20-session blocks, 500 draws): mean "
                 f"{pb['mean_diff']*100:.2f}pp, 95% CI "
                 f"[{pb['ci_95'][0]*100:.2f}pp, {pb['ci_95'][1]*100:.2f}pp], "
                 f"fraction positive {pb['frac_positive']:.2f}.")
    lines.append("")
    lines.append("## Sensitivities")
    lines.append("")
    lines.append(f"1. Remove the largest contributor ({loo['removed_issuer']}, "
                 f"attribution {loo['attribution_pp']:.2f}pp): net10 "
                 f"baseline {loo['baseline_net10']*100:.2f}% vs experiment "
                 f"{loo['experiment_net10']*100:.2f}% (diff "
                 f"{loo['net10_diff_pp']:.2f}pp); IC change "
                 f"{loo['ic_change']:.4f} (Haiku {loo['ic_haiku']['mean_ic']:.4f} "
                 f"vs score_adj {loo['ic_score_adj']['mean_ic']:.4f}).")
    lines.append(f"2. Delay experiment entry one extra session: net10 "
                 f"{delay['cost_10bps']['total_return']*100:.2f}% (vs experiment "
                 f"{port['experiment']['cost_10bps']['total_return']*100:.2f}%), "
                 f"max DD {delay['cost_10bps']['max_drawdown']*100:.2f}%, "
                 f"turnover {delay['cost_10bps']['avg_daily_turnover']:.4f}, "
                 f"trades {delay['cost_10bps']['trades']}.")
    lines.append("")
    lines.append("## Passage pairs (top-5 highest-delta filings)")
    lines.append("")
    lines.append("Judge whether the feature detects meaningful change or "
                 "formatting noise. `strongest_jaccard` is the max Jaccard of "
                 "the most novel sentence vs any prior sentence; "
                 "`closest_match_jaccard` is that sentence's best prior match.")
    lines.append("")
    for p in res["passages_top5_delta"]:
        lines.append(f"### {p['ticker']} {p['form']} {p['filing_date']} "
                     f"(delta {p['delta']:.2f}, {p['n_new']}/{p['n_sentences']} sentences)")
        lines.append("")
        lines.append(f"Accession {p['accession']} (prior {p['prior_accession']}).")
        lines.append("")
        lines.append(f"**Strongest changed passage** (Jaccard "
                     f"{p['strongest_jaccard']:.2f}):")
        lines.append("")
        lines.append(f"> {p['strongest_passage']}")
        lines.append("")
        lines.append(f"**Closest prior match** (Jaccard "
                     f"{p['closest_match_jaccard']:.2f}):")
        lines.append("")
        lines.append(f"> {p['closest_prior_match']}")
        lines.append("")
    path.write_text("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
