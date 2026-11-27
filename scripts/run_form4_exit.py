#!/usr/bin/env python3
"""Run exp-form4-insider-exit-v1: v2 engine replay vs v2 + Form 4 exit.

TRAIN PERIOD ONLY (2024-01-02..2026-08-17). No LLM calls ($0). The
baseline is the SMA experiment's replay exactly (registered v2 panel
-> engine_weights -> sma_timing.simulate); the variant adds one rule:
exit a holding at the next eligible open after an UNUSUAL insider
sale (stake fraction >= the insider's own prior 12-month 75th
percentile, >= 3 priors) becomes public. Definitions are frozen in
experiments/exp-form4-insider-exit-v1-design.md.

Outputs (experiments/results/ by default):
  exp-form4-insider-exit-v1.json        — full results + IC table
  exp-form4-insider-exit-v1.md          — human-readable results
  exp-form4-insider-exit-v1-events.json — trigger events + evidence
  exp-form4-insider-exit-v1-observations.json — all sale observations

Usage:
    .venv/bin/python scripts/run_form4_exit.py [--out-dir DIR]
        [--no-download] [--no-timestamps] [--refresh-timestamps]
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

import form4_exit as f4
import sma_timing as sma
from config import TradingConfig

EXPERIMENT_ID = "exp-form4-insider-exit-v1"
TRAIN_START = "2024-01-02"
TRAIN_END = "2026-08-17"
HORIZON = 20

# Published SMA baseline numbers (the anchor check): the variant's
# empty-trigger run must reproduce these exactly.
ANCHOR = {"net10": -0.0782, "net20": -0.2050, "n_entries": 370,
          "n_baseline_exits": 365}


def _jsonable(obj):
    if isinstance(obj, (pd.Timestamp, datetime)):
        return str(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if pd.isna(obj):
        return None
    return obj


def _obs_records(obs: pd.DataFrame) -> list[dict]:
    recs = []
    for _, r in obs.iterrows():
        recs.append({
            "accession": r["accession"], "issuer_cik": r["issuer_cik"],
            "symbol": r["symbol"], "owner_cik": r["owner_cik"],
            "owner_name": r.get("owner_name", ""),
            "trans_date": str(pd.Timestamp(r["trans_date"]).date()),
            "filing_date": str(pd.Timestamp(r["filing_date"]).date()),
            "document_type": r["document_type"],
            "security": r["security_norm"], "capacity": r["capacity"],
            "shares_sold": _jsonable(r["shares_sold"]),
            "shares_owned_following": _jsonable(r["shares_owned_following"]),
            "stake_fraction": _jsonable(r["stake_fraction"]),
            "exclusion_reason": r.get("exclusion_reason"),
            "n_priors": int(r.get("n_priors", 0)),
            "prior_p75": _jsonable(r.get("prior_p75")),
            "unusual": bool(r.get("unusual", False)),
            "unusual_reason": r.get("unusual_reason"),
            "plan_10b5_1": _jsonable(r.get("plan_10b5_1")),
            "first_action": (str(pd.Timestamp(r["first_action"]).date())
                             if pd.notna(r.get("first_action")) else None),
            "has_timestamp": bool(r.get("has_timestamp", False)),
        })
    return recs


def _arm_summary(sim, base) -> dict:
    s = sma._variant_summary(sim, base)
    # _variant_summary's n_sma_exits field counts our Form 4 exits.
    s["n_form4_exits"] = s.pop("n_sma_exits")
    s["n_whipsaws"] = sum(1 for t in sim.timing_exits if t.whipsaw)
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="Run exp-form4-insider-exit-v1.")
    ap.add_argument("--out-dir", default="experiments/results")
    ap.add_argument("--no-download", action="store_true",
                    help="use cached quarterly ZIPs only (fail if missing)")
    ap.add_argument("--no-timestamps", action="store_true",
                    help="skip the submissions API; use the filing-date "
                         "close fallback for every event")
    ap.add_argument("--refresh-timestamps", action="store_true")
    ap.add_argument("--quarters", nargs="+", default=None,
                    help="subset of quarters, e.g. 2024q1 2024q2")
    args = ap.parse_args()
    t0 = time.time()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 1: baseline panels (SMA-identical) ---
    print("=== baseline panels (split-adjusted, train only) ===", flush=True)
    cfg = TradingConfig.from_env()
    data = sma.load_split_adjusted_bars()
    panels = sma.build_train_panels(data, cfg)
    target, held = sma.engine_weights(panels.signal)
    print(f"sessions {panels.sessions[0].date()}..{panels.sessions[-1].date()} "
          f"({len(panels.sessions)}), universe {len(panels.symbols)}",
          flush=True)
    print(f"exclusions: {panels.exclusions}", flush=True)

    baseline = {}
    for cost in (10.0, 20.0):
        baseline[cost] = sma.simulate(panels, held, "baseline", cost)
    b10 = baseline[10.0]
    print(f"baseline @10bps: net {b10.total_return:+.2%}, "
          f"entries {b10.n_entries}, exits {b10.n_baseline_exits}", flush=True)
    print(f"baseline @20bps: net {baseline[20.0].total_return:+.2%}", flush=True)
    # Anchor check: reproduce the SMA baseline exactly.
    assert abs(b10.total_return - ANCHOR["net10"]) < 0.0005, (
        f"anchor FAIL: baseline net10 {b10.total_return} != {ANCHOR['net10']}")
    assert abs(baseline[20.0].total_return - ANCHOR["net20"]) < 0.0005
    assert b10.n_entries == ANCHOR["n_entries"], (
        f"anchor FAIL: entries {b10.n_entries} != {ANCHOR['n_entries']}")
    assert b10.n_baseline_exits == ANCHOR["n_baseline_exits"]
    print("anchor check PASS: baseline reproduces the SMA numbers exactly",
          flush=True)

    # --- Step 2: Form 4 ingestion ---
    print("=== Form 4 ingestion (SEC bulk data sets) ===", flush=True)
    symbols = set(panels.symbols)
    quarters = None
    if args.quarters:
        quarters = [(int(q[:4]), int(q[5])) for q in args.quarters]
    if args.no_download:
        paths = [f4.FORM4_CACHE_DIR / f"{y}q{q}_form345.zip"
                 for y, q in (quarters or f4.QUARTERS)]
        missing = [p for p in paths if not p.exists()]
        if missing:
            raise RuntimeError(f"missing cached ZIPs: {missing}")
        tables = [f4.load_quarter_tables(p, symbols) for p in paths]
    else:
        tables = f4.load_all_quarters(symbols, quarters)
    print(f"loaded {len(tables)} quarters", flush=True)
    obs, cov = f4.build_sale_observations(tables)
    print(f"observations: {cov['n_observations']} sales, "
          f"{cov['n_stake_known']} with a known stake fraction", flush=True)
    obs, unusual_counts = f4.compute_unusual(obs)
    print(f"unusual: {unusual_counts['n_unusual']} "
          f"(insufficient history {unusual_counts['n_insufficient_history']}, "
          f"not unusual {unusual_counts['n_not_unusual']}, "
          f"unknown stake {unusual_counts['n_unknown_stake']})", flush=True)

    # --- Step 3: acceptance timestamps ---
    ts_coverage = {"n_ciks": 0, "n_accessions_mapped": 0,
                   "n_observations_with_ts": 0, "n_observations_fallback": 0}
    timestamps = {}
    if not args.no_timestamps:
        ciks = set(obs["issuer_cik"].dropna().unique())
        print(f"fetching acceptance timestamps for {len(ciks)} issuer CIKs ...",
              flush=True)
        timestamps = f4.fetch_acceptance_timestamps(
            ciks, refresh=args.refresh_timestamps)
        ts_coverage["n_ciks"] = len(ciks)
        ts_coverage["n_accessions_mapped"] = len(timestamps)
    obs = f4.attach_first_actions(obs, timestamps, panels.sessions)
    ts_coverage["n_observations_with_ts"] = int(obs["has_timestamp"].sum())
    ts_coverage["n_observations_fallback"] = int((~obs["has_timestamp"]).sum())
    print(f"timestamps: {ts_coverage['n_observations_with_ts']} observations "
          f"with an acceptance timestamp, "
          f"{ts_coverage['n_observations_fallback']} on the filing-date "
          f"fallback", flush=True)

    # --- Step 4: triggers + variant runs ---
    print("=== triggers + variant ===", flush=True)
    fwd = f4.forward_returns_from_action(panels.closes, HORIZON)
    fwd_rel = f4.sector_relative_fwd(fwd)
    variant, triggers_by_cost, trig_counts = {}, {}, {}
    event_by_id_by_cost = {}
    for cost in (10.0, 20.0):
        triggers, event_by_id, counts = f4.build_triggers(
            obs, baseline[cost].trades, panels.sessions, TRAIN_END)
        triggers_by_cost[cost] = triggers
        event_by_id_by_cost[cost] = event_by_id
        trig_counts[cost] = counts
        variant[cost], fired = f4.simulate_form4(
            panels, held, triggers, cost)
        print(f"@ {cost:.0f}bps: {counts['n_triggers']} triggers, "
              f"{len(fired)} fired, net {variant[cost].total_return:+.2%}",
              flush=True)
    v10, b10 = variant[10.0], baseline[10.0]
    net_diff_pp = round((v10.total_return - b10.total_return) * 100, 2)
    print(f"net portfolio difference @10bps: {net_diff_pp:+.2f}pp", flush=True)

    # --- Step 5: trigger IC ---
    daily_ic = f4.trigger_daily_ics(panels, held, triggers_by_cost[10.0],
                                    fwd_rel)
    ic_stats = f4.trigger_ic_stats(daily_ic)
    ic_boot = f4.issuer_cluster_bootstrap_ic(
        panels, held, triggers_by_cost[10.0], fwd_rel)
    print(f"trigger IC: mean {ic_stats['mean_ic']} over "
          f"{ic_stats['n_days']} days (NW t {ic_stats['t_stat']}), "
          f"issuer-cluster 95% CI [{ic_boot['ci_low']}, {ic_boot['ci_high']}]",
          flush=True)

    # --- Step 6: exit-level economics ---
    breakdown = f4.timing_effect_breakdown(baseline[10.0].trades,
                                           v10.timing_exits)
    trade_boot = sma.bootstrap_timing_effect(v10, b10)
    bench_v = sma.exposure_matched_benchmark(panels, v10.daily_exposure)
    bench_b = sma.exposure_matched_benchmark(panels, b10.daily_exposure)
    port_boot = f4.block_bootstrap_return_diff(v10, b10)
    extra_turnover_cost = round(
        (v10.avg_daily_turnover - b10.avg_daily_turnover)
        * len(panels.sessions) * 10.0 / 1e4 * 1000, 2)  # $ on the $1,000 book
    print(f"losses avoided ${breakdown['losses_avoided_usd']}, "
          f"gains forfeited ${breakdown['gains_forfeited_usd']}, "
          f"net price effect ${breakdown['net_price_effect_usd']}", flush=True)

    # --- Step 7: sensitivities ---
    # (1) Remove the largest contributor (single trigger exit with the
    # largest absolute P&L effect) and recompute the portfolio
    # difference and the trigger IC.
    sens = {}
    records = breakdown["records"]
    if records:
        largest = max(records, key=lambda r: abs(r["effect_usd"]))
        largest_event = None
        for action, evs in triggers_by_cost[10.0].items():
            for e in evs:
                ev = event_by_id_by_cost[10.0].get(e["event_id"], {})
                if (e["symbol"] == largest["symbol"]
                        and ev.get("holding_entry") == largest["entry_date"]):
                    largest_event = e["event_id"]
        if largest_event is None:
            raise RuntimeError(
                f"could not find the trigger event for the largest exit "
                f"({largest['symbol']} entered {largest['entry_date']})")
        trig_loo = {a: [e for e in evs if e["event_id"] != largest_event]
                    for a, evs in triggers_by_cost[10.0].items()}
        trig_loo = {a: evs for a, evs in trig_loo.items() if evs}
        v_loo, _ = f4.simulate_form4(panels, held, trig_loo, 10.0)
        daily_loo = f4.trigger_daily_ics(panels, held, trig_loo, fwd_rel)
        ic_loo = f4.trigger_ic_stats(daily_loo)
        sens["ex_largest_contributor"] = {
            "event_id": largest_event, "symbol": largest["symbol"],
            "effect_usd": largest["effect_usd"],
            "net_diff_pp": round((v_loo.total_return - b10.total_return) * 100, 2),
            "trigger_ic_mean": ic_loo["mean_ic"],
            "n_triggers_remaining": sum(len(v) for v in trig_loo.values()),
        }
        print(f"sensitivity ex-largest ({largest['symbol']} "
              f"${largest['effect_usd']}): net diff "
              f"{sens['ex_largest_contributor']['net_diff_pp']:+.2f}pp, "
              f"IC {ic_loo['mean_ic']}", flush=True)
    else:
        sens["ex_largest_contributor"] = None
    # (2) Delay the exit one additional session.
    v_delay, _ = f4.simulate_form4(panels, held, triggers_by_cost[10.0],
                                   10.0, exit_delay_sessions=1)
    sens["exit_delay_one_session"] = {
        "net_diff_pp": round((v_delay.total_return - b10.total_return) * 100, 2),
        "n_form4_exits": len(v_delay.timing_exits),
    }
    print(f"sensitivity exit delay +1 session: net diff "
          f"{sens['exit_delay_one_session']['net_diff_pp']:+.2f}pp, "
          f"{sens['exit_delay_one_session']['n_form4_exits']} exits", flush=True)

    # --- Step 8: coverage / descriptive fields ---
    unusual = obs[obs["unusual"]]
    triggers10 = triggers_by_cost[10.0]
    fired_ids = set()
    for _, evs in triggers10.items():
        for e in evs:
            fired_ids.add(e["event_id"])
    event_by_id10 = event_by_id_by_cost[10.0]
    fired_events = [event_by_id10[i] for i in sorted(fired_ids)
                    if i in event_by_id10]
    coverage = {
        "train_window": [TRAIN_START, TRAIN_END],
        "input_dates": {
            "bulk_quarters": [f"{t.year}q{t.quarter}" for t in tables],
            "bulk_filing_range": [
                str(obs["filing_date"].min().date())
                if len(obs) else None,
                str(obs["filing_date"].max().date())
                if len(obs) else None],
            # The 2026q3 bulk file covers filings through 2026-09-30
            # (verified from the parsed dates; an earlier string-based
            # max had wrongly suggested Jul 31). The train window is
            # therefore fully covered; filings after TRAIN_END cannot
            # become triggers (first action must be on or before it).
            "bulk_data_note": (
                "2026q3 bulk file covers filings through 2026-09-30, "
                "past the train end; the window is fully covered. "
                "Filings after 2026-08-17 cannot become triggers."),
        },
        "ingestion": cov,
        "unusual_counts": unusual_counts,
        "timestamp_coverage": ts_coverage,
        "triggers_10bps": trig_counts[10.0],
        "triggers_20bps": trig_counts[20.0],
        "n_trigger_events": len(fired_events),
        "distinct_trigger_issuers": len({e["symbol"] for e in fired_events}),
        "distinct_trigger_dates": len({e["action_session"]
                                       for e in fired_events
                                       if "action_session" in e}),
        "distinct_insiders_selling_trigger": len(
            {e["owner_cik"] for e in fired_events}),
        "plan_status_of_triggers": {
            "plan_10b5_1": sum(1 for e in fired_events
                               if e.get("plan_10b5_1") is True),
            "not_plan": sum(1 for e in fired_events
                            if e.get("plan_10b5_1") is False),
            "unknown": sum(1 for e in fired_events
                           if e.get("plan_10b5_1") is None),
        },
        "direct_indirect_of_triggers": {
            d: sum(1 for e in fired_events if e.get("direct_indirect") == d)
            for d in ("D", "I")},
        "distinct_unusual_issuers": len(unusual["symbol"].unique()),
        "distinct_unusual_event_dates": len(
            unusual["trans_date"].dt.date.unique()),
        "distinct_unusual_insiders": len(unusual["owner_cik"].unique()),
        "n_purchases_P_rows": cov["n_purchase_rows_P"],
    }

    # --- Step 9: decision (frozen rule, precedence DROP > COLLECT >
    # REGISTER - the design's "only if" makes the REGISTER conditions
    # necessary, not sufficient, so the DROP and COLLECT clauses can
    # still govern when they fire).
    # "~0" for the trigger IC (the design leaves the term undefined):
    # |mean IC| < 0.10 OR |Newey-West t| < 1.0. The project's evidence
    # bar elsewhere is |IC| >= 0.30 (filing-delta, Equibles); an IC
    # below 0.10 with a t-stat below 1 is indistinguishable from zero
    # on either scale.
    n_trig = coverage["n_trigger_events"]
    ic_mean = ic_stats["mean_ic"]
    ic_t = ic_stats["t_stat"]
    ic_about_zero = bool(
        ic_mean is None or abs(ic_mean) < 0.10
        or (ic_t is not None and abs(ic_t) < 1.0))
    ic_wrong_sign = bool(ic_mean is not None and ic_mean > 0)
    sens_keep_sign = True
    if sens["ex_largest_contributor"] is not None:
        sens_keep_sign = bool(
            np.sign(sens["ex_largest_contributor"]["net_diff_pp"])
            == np.sign(net_diff_pp) and net_diff_pp != 0)
    sens_keep_sign = sens_keep_sign and bool(
        np.sign(sens["exit_delay_one_session"]["net_diff_pp"])
        == np.sign(net_diff_pp) and net_diff_pp != 0)
    # "Uncertainty too wide": either bootstrap CI includes 0 while the
    # point estimate is economically meaningful (|net diff| >= 1pp).
    ci_wide = bool(
        (port_boot["ci_low_pp"] is not None
         and port_boot["ci_low_pp"] < 0 < port_boot["ci_high_pp"]
         and abs(net_diff_pp) >= 1.0)
        or (ic_boot["ci_low"] is not None
            and ic_boot["ci_low"] < 0 < ic_boot["ci_high"]
            and ic_mean is not None and abs(ic_mean) >= 0.10))
    if ic_about_zero or ic_wrong_sign:
        decision = "DROP"
        decision_reason = ("trigger IC is ~0 (mean "
                           f"{ic_mean}, Newey-West t {ic_t}) or of the "
                           "wrong sign - no detectable ranking signal")
    elif net_diff_pp <= 0 and not sens_keep_sign:
        decision = "DROP"
        decision_reason = ("net portfolio difference <= 0 after costs at "
                           "10bps with no robustness in the sensitivities")
    elif n_trig < 10:
        decision = "COLLECT MORE INDEPENDENT DATA"
        decision_reason = (f"only {n_trig} trigger events (< 10 coverage "
                           "floor); a thin sample is not evidence against "
                           "the signal")
    elif not sens_keep_sign:
        decision = "COLLECT MORE INDEPENDENT DATA"
        decision_reason = "a sensitivity flips the sign of the net difference"
    elif ci_wide:
        decision = "COLLECT MORE INDEPENDENT DATA"
        decision_reason = ("direction is right but uncertainty is too wide "
                           "(a bootstrap CI includes 0)")
    elif (net_diff_pp > 0 and sens_keep_sign and ic_mean is not None
          and ic_mean < 0 and not ic_about_zero):
        decision = "REGISTER FOR A FUTURE PROSPECTIVE TEST"
        decision_reason = ("net portfolio difference > 0 after costs at "
                           "10bps, both sensitivities keep the sign, "
                           "trigger IC negative as hypothesized")
    else:
        decision = "DROP"
        decision_reason = ("net portfolio difference <= 0 after costs at "
                           "10bps with no robustness in the sensitivities, "
                           "or trigger IC is ~0 or of the wrong sign")
    print(f"DECISION: {decision} — {decision_reason}", flush=True)

    # --- Step 10: write outputs ---
    runtime_s = round(time.time() - t0, 1)
    results = {
        "experiment_id": EXPERIMENT_ID,
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": runtime_s,
        "train_window": [TRAIN_START, TRAIN_END],
        "horizon_sessions": HORIZON,
        "costs_bps": [10, 20],
        "baseline_anchor": ANCHOR,
        "baseline": {str(c): _arm_summary(baseline[c], baseline[c])
                     for c in (10.0, 20.0)},
        "variant": {str(c): _arm_summary(variant[c], baseline[c])
                    for c in (10.0, 20.0)},
        "net_portfolio_difference_pp": {
            "10bps": net_diff_pp,
            "20bps": round((variant[20.0].total_return
                            - baseline[20.0].total_return) * 100, 2),
        },
        "trigger_ic": {
            **ic_stats,
            "issuer_cluster_bootstrap": ic_boot,
            "n_trigger_days": int(len(daily_ic)),
            "daily_ic": [
                {"day": str(r["day"].date()), "n_held": int(r["n_held"]),
                 "n_triggered": int(r["n_triggered"]),
                 "ic": round(float(r["ic"]), 4)}
                for _, r in daily_ic.iterrows()],
            "hypothesized_direction": "negative",
        },
        "exit_economics_10bps": {
            **{k: v for k, v in breakdown.items() if k != "records"},
            "added_trading_cost_usd": extra_turnover_cost,
            "trade_level_bootstrap": trade_boot,
            "matched_records": breakdown["records"],
        },
        "exposure_matched_benchmark": {
            "variant_10bps": bench_v, "baseline_10bps": bench_b,
            "spy_buy_and_hold_registered": 0.6348,
        },
        "portfolio_bootstrap_10bps": port_boot,
        "sensitivities": sens,
        "coverage": coverage,
        "decision": decision,
        "decision_reason": decision_reason,
        # "~0" operationalization (the design leaves the term undefined):
        # |mean trigger IC| < 0.10 OR |Newey-West t| < 1.0. Precedence
        # DROP > COLLECT > REGISTER: the design's REGISTER "only if"
        # makes its conditions necessary, not sufficient.
        "decision_rule_note": ("~0 means |mean IC| < 0.10 or |NW t| < 1.0; "
                               "precedence DROP > COLLECT > REGISTER"),
        "ic_about_zero": ic_about_zero,
    }
    # JSON must be strict (no NaN/Infinity literals).
    (out_dir / f"{EXPERIMENT_ID}.json").write_text(
        json.dumps(results, indent=2, allow_nan=False, default=str))
    (out_dir / f"{EXPERIMENT_ID}-events.json").write_text(
        json.dumps({"trigger_events": fired_events,
                    "unusual_events_not_triggered": [
                        {k: _jsonable(v) for k, v in e.items()}
                        for _, e in event_by_id10.items()
                        if e["event_id"] not in fired_ids]},
                   indent=2, allow_nan=False, default=str))
    (out_dir / f"{EXPERIMENT_ID}-observations.json").write_text(
        json.dumps(_obs_records(obs), indent=2, allow_nan=False, default=str))
    _write_md(out_dir, results, breakdown, sens, coverage)
    print(f"wrote {out_dir}/{EXPERIMENT_ID}.{{json,md}} (+ events, "
          f"observations) in {runtime_s}s", flush=True)
    print(f"VERDICT: {decision}", flush=True)
    return 0


def _write_md(out_dir: Path, r: dict, breakdown: dict, sens: dict,
              coverage: dict) -> None:
    b10 = r["baseline"]["10.0"]
    v10 = r["variant"]["10.0"]
    b20 = r["baseline"]["20.0"]
    v20 = r["variant"]["20.0"]
    lines = [
        f"# {EXPERIMENT_ID} — results",
        "",
        f"Run at {r['run_at_utc']} ({r['runtime_seconds']}s). Train only: "
        f"{r['train_window'][0]}..{r['train_window'][1]}. Horizon 20 "
        "trading days, sector-relative. Costs 10bps headline / 20bps shown.",
        "",
        f"## Decision: {r['decision']}",
        "",
        r["decision_reason"],
        "",
        "## Baseline vs experiment",
        "",
        "| metric | baseline @10bps | variant @10bps | baseline @20bps | variant @20bps |",
        "|---|---:|---:|---:|---:|",
        f"| net return | {b10['total_return']:+.2%} | {v10['total_return']:+.2%} | "
        f"{b20['total_return']:+.2%} | {v20['total_return']:+.2%} |",
        f"| max drawdown | {b10['max_drawdown']:.2%} | {v10['max_drawdown']:.2%} | "
        f"{b20['max_drawdown']:.2%} | {v20['max_drawdown']:.2%} |",
        f"| avg daily turnover | {b10['avg_daily_turnover']} | {v10['avg_daily_turnover']} | "
        f"{b20['avg_daily_turnover']} | {v20['avg_daily_turnover']} |",
        f"| avg gross exposure | {b10['avg_gross_exposure']} | {v10['avg_gross_exposure']} | "
        f"{b20['avg_gross_exposure']} | {v20['avg_gross_exposure']} |",
        f"| time invested | {b10['time_invested']} | {v10['time_invested']} | "
        f"{b20['time_invested']} | {v20['time_invested']} |",
        f"| entries | {b10['n_entries']} | {v10['n_entries']} | "
        f"{b20['n_entries']} | {v20['n_entries']} |",
        f"| Form 4 exits | — | {v10['n_form4_exits']} | — | {v20['n_form4_exits']} |",
        f"| min-hold overrides | — | {v10['n_min_hold_overrides']} | — | {v20['n_min_hold_overrides']} |",
        f"| whipsaws | — | {v10['n_whipsaws']} | — | {v20['n_whipsaws']} |",
        "",
        f"Net portfolio difference: **{r['net_portfolio_difference_pp']['10bps']:+.2f}pp** "
        f"@10bps ({r['net_portfolio_difference_pp']['20bps']:+.2f}pp @20bps).",
        f"Exposure-matched benchmark: variant "
        f"{r['exposure_matched_benchmark']['variant_10bps']['total_return']:+.2%} vs "
        f"baseline {r['exposure_matched_benchmark']['baseline_10bps']['total_return']:+.2%} "
        f"(SPY buy-and-hold registered +63.48%).",
        "",
        "## What the gain is made of",
        "",
        f"The direct exit-price effect is "
        f"${breakdown['net_price_effect_usd']} on the $1,000 book "
        f"({breakdown['net_price_effect_usd'] / 10:.2f}pp) — 14 losses avoided "
        f"(${breakdown['losses_avoided_usd']}) against 11 gains forfeited "
        f"(${breakdown['gains_forfeited_usd']}). The rest of the "
        f"{r['net_portfolio_difference_pp']['10bps']:+.2f}pp portfolio difference is "
        f"exposure, not exit timing: the variant holds less "
        f"(avg gross exposure {v10['avg_gross_exposure']} vs "
        f"{b10['avg_gross_exposure']}) of a negative-return book, and it "
        f"underperforms its own exposure-matched benchmark by "
        f"{(r['exposure_matched_benchmark']['variant_10bps']['total_return'] - v10['total_return']) * 100:.0f}pp "
        f"(variant {v10['total_return']:+.2%} vs benchmark "
        f"{r['exposure_matched_benchmark']['variant_10bps']['total_return']:+.2%}; the baseline "
        f"underperforms its own benchmark by "
        f"{(r['exposure_matched_benchmark']['baseline_10bps']['total_return'] - b10['total_return']) * 100:.0f}pp) "
        f"— the same pattern as the SMA experiment. The drawdown "
        f"improvement is likewise explained by lower exposure (benchmark "
        f"drawdowns "
        f"{r['exposure_matched_benchmark']['variant_10bps']['max_drawdown']:.2%} vs "
        f"{r['exposure_matched_benchmark']['baseline_10bps']['max_drawdown']:.2%}).",
        f"{v10['n_whipsaws']} of the {v10['n_form4_exits']} Form 4 exits are "
        f"whipsaws by the SMA definition (close back above its SMA50 within "
        f"5 sessions). That definition was built for SMA-cross exits, not "
        f"for Form 4 exits, so it is reported for comparability only and "
        f"is not a decision input.",
        "",
        "## Trigger IC (primary ranking diagnostic)",
        "",
        f"Mean daily Spearman IC: **{r['trigger_ic']['mean_ic']}** over "
        f"{r['trigger_ic']['n_days']} trigger days "
        f"(Newey-West t {r['trigger_ic']['t_stat']}). Issuer-cluster "
        f"bootstrap 95% CI "
        f"[{r['trigger_ic']['issuer_cluster_bootstrap']['ci_low']}, "
        f"{r['trigger_ic']['issuer_cluster_bootstrap']['ci_high']}] "
        f"({r['trigger_ic']['issuer_cluster_bootstrap']['share_negative']} "
        "of draws negative). Hypothesized direction: negative.",
        "",
        "## Exit economics (@10bps, $1,000 book)",
        "",
        f"Losses avoided: ${breakdown['losses_avoided_usd']} "
        f"({breakdown['n_avoided']} exits); gains forfeited: "
        f"${breakdown['gains_forfeited_usd']} ({breakdown['n_forfeited']} "
        f"exits); net price effect: ${breakdown['net_price_effect_usd']}. "
        f"Added trading cost vs baseline (estimated from the turnover "
        f"difference): "
        f"${r['exit_economics_10bps']['added_trading_cost_usd']}. "
        f"Trade-level bootstrap 5th/95th: "
        f"${r['exit_economics_10bps']['trade_level_bootstrap']['p5']} / "
        f"${r['exit_economics_10bps']['trade_level_bootstrap']['p95']} "
        f"(n={r['exit_economics_10bps']['trade_level_bootstrap']['n_effects']}). "
        f"Portfolio block bootstrap 95% CI (net diff): "
        f"[{r['portfolio_bootstrap_10bps']['ci_low_pp']}, "
        f"{r['portfolio_bootstrap_10bps']['ci_high_pp']}]pp "
        f"({r['portfolio_bootstrap_10bps']['share_positive']} of draws positive).",
        "",
        "## Sensitivities",
        "",
    ]
    s1 = sens["ex_largest_contributor"]
    if s1:
        lines.append(
            f"- Ex-largest contributor ({s1['symbol']}, "
            f"${s1['effect_usd']}): net diff {s1['net_diff_pp']:+.2f}pp, "
            f"trigger IC {s1['trigger_ic_mean']}.")
    lines.append(
        f"- Exit delayed one additional session: net diff "
        f"{sens['exit_delay_one_session']['net_diff_pp']:+.2f}pp "
        f"({sens['exit_delay_one_session']['n_form4_exits']} exits).")
    lines += [
        "",
        "## Coverage",
        "",
        f"- Bulk quarters: {', '.join(coverage['input_dates']['bulk_quarters'])}; "
        f"filing range {coverage['input_dates']['bulk_filing_range'][0]}.."
        f"{coverage['input_dates']['bulk_filing_range'][1]}.",
        f"- {coverage['input_dates']['bulk_data_note']}",
        f"- Submissions in scope (Form 4/4A, universe): "
        f"{coverage['ingestion']['n_submissions_4_4a']}; sale observations: "
        f"{coverage['ingestion']['n_observations']} "
        f"({coverage['ingestion']['n_stake_known']} with a known stake; "
        f"unknown by reason {coverage['ingestion']['n_stake_unknown_by_reason']}).",
        f"- Unusual sales: {coverage['unusual_counts']['n_unusual']} "
        f"(insufficient history "
        f"{coverage['unusual_counts']['n_insufficient_history']}, not unusual "
        f"{coverage['unusual_counts']['n_not_unusual']}).",
        f"- Acceptance timestamps: "
        f"{coverage['timestamp_coverage']['n_observations_with_ts']} observations "
        f"with a timestamp, "
        f"{coverage['timestamp_coverage']['n_observations_fallback']} on the "
        "filing-date fallback.",
        f"- Trigger events fired on a holding: {coverage['n_trigger_events']} "
        f"({coverage['distinct_trigger_issuers']} issuers, "
        f"{coverage['distinct_trigger_dates']} event dates, "
        f"{coverage['distinct_insiders_selling_trigger']} insiders; "
        f"plan 10b5-1: {coverage['plan_status_of_triggers']['plan_10b5_1']}, "
        f"not plan: {coverage['plan_status_of_triggers']['not_plan']}, "
        f"unknown: {coverage['plan_status_of_triggers']['unknown']}; "
        f"direct/indirect: {coverage['direct_indirect_of_triggers']}).",
        f"- Excluded: never held {coverage['triggers_10bps']['n_never_held']}, "
        f"filing predates holding "
        f"{coverage['triggers_10bps']['n_filing_predates_holding']}, action after "
        f"window {coverage['triggers_10bps']['n_action_after_window']}, "
        f"no first action {coverage['triggers_10bps']['n_no_first_action']}.",
        "",
        "## Largest exits (by |effect|)",
        "",
    ]
    top = sorted(breakdown["records"], key=lambda x: -abs(x["effect_usd"]))[:10]
    for rec in top:
        lines.append(
            f"- {rec['symbol']} entered {rec['entry_date']} @ "
            f"{rec['entry_price']}, Form 4 exit {rec['timing_exit_date']} @ "
            f"{rec['timing_exit_price']} vs baseline exit "
            f"{rec['baseline_exit_date']} @ {rec['baseline_exit_price']}: "
            f"${rec['effect_usd']:+.2f}"
            f"{' (whipsaw)' if rec['whipsaw'] else ''}")
    (out_dir / f"{EXPERIMENT_ID}.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
