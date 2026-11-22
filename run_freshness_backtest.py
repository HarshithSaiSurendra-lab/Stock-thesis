#!/usr/bin/env python3
"""Freshness test driver: exp-technical-plus-llm-freshness-v1.

BACKTEST ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.

Registered BEFORE scoring (see experiments/registry.jsonl and
experiments/exp-technical-plus-llm-freshness-v1-design.md).

What this run does, and nothing else:
1. Loads cached Alpaca bars; builds the 4 technical variant panels
   (identical to v1/v2/Phase 2).
2. Loads EDGAR point-in-time excerpts (dataset-edgar-point-in-time-v1).
3. Haiku 5.5 screens the 75 in-scope HOLDOUT-window filings
   (2026-08-18..2026-11-13). Sonnet 5.5 adjudicates the top-10 by
   holdout-day relevance within the $0.75 sub-budget. Fable 5.2 is
   audit-only and is NOT run; no ChatGPT leg.
4. Builds the fresh contribution panel from the UNION current-filing
   series (in-scope train filings with Phase 2 scores + in-scope
   holdout filings with fresh scores). Train days are identical to
   Phase 2 by construction; only holdout days can change.
5. Rebuilds the STALE panels from Phase 2's scores (same run, no new
   API spend) and evaluates base vs stale vs fresh on train and the
   frozen holdout at 10bps and 2x costs. Asserts the base and stale
   results reproduce Phase 2's recorded numbers exactly before the
   gate is read — a mismatch aborts the run.
6. Gate verdict (SG1/SG2/SG3, same definitions as Phase 2); writes
   results JSON; prints a summary.

Spend guardrails (hard): $1.00 of NEW spend this run (cumulative cap
$9.888873 = $8.888873 already spent + $1.00). Monthly cap $10.00.
Cost estimates use ~2.9 chars/token (Phase 2 measured rate), NOT
chars/4 — the driver's estimator is patched in before scoring (see
estimate_call_cost_29). No new cash, no new keys, no purchases.

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python run_freshness_backtest.py
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

import run_llm_backtest as p2
from llm_sentiment import (
    MAX_EXCERPT_CHARS,
    MAX_OUTPUT_TOKENS,
    MODEL_PRICES,
    SYSTEM_PROMPT,
    AnthropicProvider,
)
from llm_smoke_test import _SurrogateClient
from research_runner import (
    build_signal_variants,
    slice_research_window,
)
from run_real_backtest import _load_cached_bars
from config import TradingConfig

REPO = Path(__file__).parent
RESULTS_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-freshness-v1.json"
SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-freshness-v1-scores.json"
HAIKU_SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-freshness-v1-scores-haiku.json"
SONNET_SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-freshness-v1-scores-sonnet.json"
P2_RESULTS_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1.json"
P2_HAIKU_SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json"
P2_SONNET_SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-sonnet.json"

# Registered parameters (same as Phase 2 unless noted).
MIN_DECISION_SCORE = 10.25
TARGET_N_POSITIONS = 5
TRAIL_PERCENT = 8.0
RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63
MAX_POSITIONS = 5
REBALANCE_EVERY = 1
N_SPLITS = 4
INITIAL_CAPITAL = 1000.0
COSTS_BPS = (10, 20)
N_TRIALS_DSR = 26  # 18 from Phase 2 + 8 (4 fresh variants x 2 costs)

# Spend guardrails (USD). Already spent is read from the ledger (same
# helper as Phase 2) — $8.888873 incl. the off-ledger validation.
RUN_BUDGET_NEW_USD = 1.00
CUMULATIVE_CAP_USD = round(p2.already_spent_usd() + RUN_BUDGET_NEW_USD, 6)
SONNET_BUDGET_USD = 0.75
PROVIDER_RUN_CAP_USD = 1.00
PROVIDER_MONTHLY_CAP_USD = 10.00
API_KEY_PLACEHOLDER = "sk-ant-surrogate"

# Scoring window for THIS experiment: holdout filings only.
HOLDOUT_START_DATE = "2026-08-18"
HOLDOUT_END_DATE = "2026-11-13"


def estimate_call_cost_29(excerpt_chars: int, model_id: str) -> float:
    """Worst-case cost of one scoring call at ~2.9 chars/token.

    Phase 2 measured mean actual Sonnet input 25,998 tokens vs 19,026
    estimated at chars/4 (ratio 1.366, i.e. ~2.9 chars/token). The
    chars/4 estimate breached the Phase 2 Sonnet sub-budget by $0.5576.
    This estimator uses 2.9 and the provider's 1,000-token max output,
    so subset selection and budget guards cannot understate cost the
    way Phase 2's did.
    """
    est_in = max(1, int(len(SYSTEM_PROMPT) / 2.9)) + max(
        1, int(min(excerpt_chars, MAX_EXCERPT_CHARS) / 2.9)
    )
    price_in, price_out = MODEL_PRICES[model_id]
    return round(est_in / 1_000_000 * price_in + MAX_OUTPUT_TOKENS / 1_000_000 * price_out, 6)


# NOTE: the estimator patch lives inside main(), NOT at module import,
# so importing this driver (e.g. from tests) never changes
# run_llm_backtest's module globals for other callers. main() patches,
# runs, and restores in a finally block.
_PATCHED = {}


def in_scope_holdout_filings(filings: pd.DataFrame, variants: dict,
                             holdout_days: pd.DatetimeIndex) -> pd.DataFrame:
    """Holdout-window filings current on >= 1 active holdout day.

    Mirror of run_llm_backtest.in_scope_filings, applied to the holdout
    window (2026-08-18..2026-11-13) and holdout days instead of the
    train window and train days. A filing is current from its filing
    date until the next filing for the same symbol (from the FULL
    filings frame). Only filings dated in the holdout window are
    returned; train filings are never re-scored here.
    """
    scoped = filings[
        (filings["filing_date"] >= pd.Timestamp(HOLDOUT_START_DATE))
        & (filings["filing_date"] <= pd.Timestamp(HOLDOUT_END_DATE))
    ].copy()
    scoped = scoped.sort_values(["ticker", "filing_date"])
    full = filings.sort_values(["ticker", "filing_date"])
    full_next = full.groupby("ticker")["filing_date"].shift(-1)
    scoped["next_filing_date"] = full_next.reindex(scoped.index)
    keep = []
    for symbol, group in scoped.groupby("ticker"):
        panels = [variants[v] for v in variants if symbol in variants[v].columns]
        if not panels:
            continue
        active_mask = np.any(
            [p.loc[holdout_days, symbol].to_numpy() > 0 for p in panels],
            axis=0,
        )
        active = holdout_days[active_mask]
        dates = group["filing_date"].to_numpy()
        next_dates = group["next_filing_date"].to_numpy()
        pos = np.searchsorted(active.to_numpy(), dates, side="left")
        for i, p_ in enumerate(pos):
            if p_ >= len(active):
                continue
            if pd.isna(next_dates[i]) or active[p_] < next_dates[i]:
                keep.append(group.index[i])
    return scoped.loc[keep].reset_index(drop=True)


def current_filing_series_union(
    filings_union: pd.DataFrame,
    days: pd.DatetimeIndex,
) -> pd.DataFrame:
    """For each symbol and day, the accession of the latest filing <= day.

    `filings_union` is the in-scope train filings (Phase 2) + the
    in-scope holdout filings (this run). Unlike
    run_llm_backtest.current_filing_series, there is NO date cutoff:
    holdout-window filings become current on their filing date. Train
    days are unaffected (no holdout filing is dated before a train
    day), so train panels reproduce Phase 2 exactly.
    """
    scoped = filings_union.sort_values(["ticker", "filing_date"])
    out = pd.DataFrame(index=days, columns=sorted(scoped["ticker"].unique()), dtype=object)
    for symbol, group in scoped.groupby("ticker"):
        dates = group["filing_date"].to_numpy()
        accessions = group["accession"].to_numpy()
        pos = np.searchsorted(dates, days.to_numpy(), side="right") - 1
        col = np.where(pos >= 0, accessions[np.maximum(pos, 0)], np.nan)
        out[symbol] = col
    return out


def _ret(eval_dict, cost, variant):
    return p2._eval_lookup(eval_dict, cost, variant)["total_return"]


def main() -> int:
    """Patch Phase 2's estimator + trial count for this run, then run."""
    _PATCHED["estimate_call_cost"] = p2.estimate_call_cost
    _PATCHED["N_TRIALS_DSR"] = p2.N_TRIALS_DSR
    p2.estimate_call_cost = estimate_call_cost_29
    p2.N_TRIALS_DSR = N_TRIALS_DSR
    try:
        return _run()
    finally:
        p2.estimate_call_cost = _PATCHED["estimate_call_cost"]
        p2.N_TRIALS_DSR = _PATCHED["N_TRIALS_DSR"]


def _run() -> int:
    run_started = datetime.now(timezone.utc).isoformat()
    print("=== Freshness test: exp-technical-plus-llm-freshness-v1 ===", flush=True)
    print(f"run started {run_started}", flush=True)
    already_spent = p2.already_spent_usd()
    print(f"spend already incurred before this run: ${already_spent:.6f}", flush=True)
    print(f"run budget (new spend): ${RUN_BUDGET_NEW_USD:.2f}; "
          f"cumulative cap: ${CUMULATIVE_CAP_USD:.6f}; "
          f"monthly cap: ${PROVIDER_MONTHLY_CAP_USD:.2f}", flush=True)

    # --- Load bars and technical panels (identical to v1/v2/Phase 2) -----
    data = _load_cached_bars()
    if data is None:
        raise RuntimeError("no cached bars; .cache/backtest_data is empty")
    cfg = TradingConfig.from_env()
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, RESEARCH_START, RESEARCH_END)
    full_prices = pd.DataFrame(
        {s: f["close"] for s, f in data.bars.items()}
    ).sort_index().loc[prices.index]
    window_days = prices.index
    holdout_start = window_days[-HOLDOUT_DAYS_N]
    train_start, train_end = str(window_days[0].date()), str(window_days[-HOLDOUT_DAYS_N - 1].date())
    holdout_start_str = str(holdout_start.date())
    holdout_end = str(window_days[-1].date())
    print(f"window {window_days[0].date()}..{window_days[-1].date()} ({len(window_days)} days); "
          f"holdout starts {holdout_start.date()} ({HOLDOUT_DAYS_N} days)", flush=True)
    train_days = window_days[window_days < holdout_start]
    holdout_days = window_days[window_days >= holdout_start]

    # --- Load EDGAR filings; scope the holdout set ------------------------
    filings = p2.load_edgar_filings()
    print(f"EDGAR filings loaded: {len(filings)}", flush=True)
    in_scope_train = p2.in_scope_filings(filings, variants, train_days)
    in_scope_hw = in_scope_holdout_filings(filings, variants, holdout_days)
    print(f"in-scope train filings (Phase 2): {len(in_scope_train)}", flush=True)
    print(f"in-scope holdout filings (this run): {len(in_scope_hw)}", flush=True)
    counts = p2.relevance_counts(in_scope_hw, variants, holdout_days)
    near_threshold = int((counts > 0).sum())
    print(f"near-threshold holdout filings (relevance > 0): {near_threshold} "
          f"of {len(in_scope_hw)}; total relevance triples: {int(counts.sum())}", flush=True)

    # --- Load Phase 2 scores (already paid; $0 to re-read) ----------------
    p2_haiku = p2._load_scores(P2_HAIKU_SCORES_PATH)
    p2_sonnet = p2._load_scores(P2_SONNET_SCORES_PATH)
    print(f"Phase 2 scores loaded: {len(p2_haiku)} Haiku, {len(p2_sonnet)} Sonnet", flush=True)

    # --- Haiku screen: all in-scope holdout filings -----------------------
    haiku = AnthropicProvider(
        API_KEY_PLACEHOLDER,
        role="screen",
        run_cap_usd=PROVIDER_RUN_CAP_USD,
        monthly_cap_usd=PROVIDER_MONTHLY_CAP_USD,
        client=_SurrogateClient(),
    )
    fresh_haiku = p2._load_scores(HAIKU_SCORES_PATH)
    print(f"resuming: {len(fresh_haiku)} fresh Haiku scores already on disk", flush=True)
    haiku_summary = p2.score_pass(
        haiku, in_scope_hw, "haiku",
        spent_before_usd=already_spent,
        budget_usd=CUMULATIVE_CAP_USD,
        save_path=HAIKU_SCORES_PATH,
        scores=fresh_haiku,
    )
    print(f"Haiku pass: {haiku_summary}", flush=True)
    spent = haiku_summary["spent_after_usd"]

    # --- Sonnet adjudication: top-ranked by holdout relevance ------------
    sonnet_subset = p2.select_sonnet_subset(
        fresh_haiku, counts, in_scope_hw, SONNET_BUDGET_USD,
    )
    print(f"Sonnet subset: {len(sonnet_subset)} filings (ranked by holdout "
          f"relevance, Haiku conf >= 0.6, 2.9-char/token est within "
          f"${SONNET_BUDGET_USD:.2f})", flush=True)
    sonnet = AnthropicProvider(
        API_KEY_PLACEHOLDER,
        role="adjudicate",
        run_cap_usd=PROVIDER_RUN_CAP_USD,
        monthly_cap_usd=PROVIDER_MONTHLY_CAP_USD,
        client=_SurrogateClient(),
    )
    fresh_sonnet = p2._load_scores(SONNET_SCORES_PATH)
    print(f"resuming: {len(fresh_sonnet)} fresh Sonnet scores already on disk", flush=True)
    sonnet_filings = in_scope_hw.loc[sonnet_subset] if sonnet_subset else in_scope_hw.iloc[0:0]
    sonnet_summary = p2.score_pass(
        sonnet, sonnet_filings, "sonnet",
        spent_before_usd=spent,
        budget_usd=CUMULATIVE_CAP_USD,
        save_path=SONNET_SCORES_PATH,
        scores=fresh_sonnet,
    )
    print(f"Sonnet pass: {sonnet_summary}", flush=True)
    spent = sonnet_summary["spent_after_usd"]
    total_spend = round(spent, 6)
    run_spend = round(total_spend - already_spent, 6)
    print(f"TOTAL Anthropic spend this run: ${run_spend:.6f} "
          f"(cumulative incl. prior: ${total_spend:.6f})", flush=True)
    if total_spend > CUMULATIVE_CAP_USD:
        raise RuntimeError(f"spend cap breached: ${total_spend:.4f} > ${CUMULATIVE_CAP_USD:.6f}")
    if run_spend > RUN_BUDGET_NEW_USD:
        raise RuntimeError(f"run budget breached: ${run_spend:.4f} > ${RUN_BUDGET_NEW_USD:.2f}")

    # --- Merge scores; build stale and fresh panels ------------------------
    merged_haiku = {**p2_haiku, **fresh_haiku}
    merged_sonnet = {**p2_sonnet, **fresh_sonnet}
    SCORES_PATH.write_text(json.dumps({
        "haiku": merged_haiku,
        "sonnet": merged_sonnet,
        "fresh_haiku": fresh_haiku,
        "fresh_sonnet": fresh_sonnet,
        "haiku_summary": haiku_summary,
        "sonnet_summary": sonnet_summary,
        "total_spend_usd": total_spend,
        "run_spend_usd": run_spend,
    }, indent=2))

    # Stale panels: Phase 2's exact construction (train filings only).
    current_stale = p2.current_filing_series(in_scope_train, window_days)
    contrib_stale, applied_stale = p2.build_contribution_panel(
        current_stale, p2_haiku, p2_sonnet)
    adjusted_stale = p2.build_adjusted_panels(variants, contrib_stale)
    # Fresh panels: union current series + merged scores.
    filings_union = pd.concat([in_scope_train, in_scope_hw], ignore_index=True)
    current_fresh = current_filing_series_union(filings_union, window_days)
    # Train days must be identical (no holdout filing predates a train day).
    if not (current_fresh.loc[train_days].fillna("")
            == current_stale.loc[train_days].fillna("")).all().all():
        raise RuntimeError("union current series differs from stale on TRAIN days; aborting")
    contrib_fresh, applied_fresh = p2.build_contribution_panel(
        current_fresh, merged_haiku, merged_sonnet)
    adjusted_fresh = p2.build_adjusted_panels(variants, contrib_fresh)
    n_applied_fresh = int(applied_fresh.sum().sum())
    n_applied_fresh_ho = int(applied_fresh.loc[holdout_days].sum().sum())
    n_applied_stale_ho = int(applied_stale.loc[holdout_days].sum().sum())
    n_active_cells = int(((pd.concat(variants, axis=1) > 0).sum().sum()))
    print(f"LLM contribution applied (fresh): {n_applied_fresh} cells total, "
          f"{n_applied_fresh_ho} on holdout days (stale: {n_applied_stale_ho} "
          f"on holdout days) out of {n_active_cells} active cells", flush=True)

    # Rename fresh adjusted panels with a _fresh suffix (stale keeps _llm).
    adjusted_fresh = {k.replace("_llm", "_llm_fresh"): v for k, v in adjusted_fresh.items()}
    all_panels = {**variants, **adjusted_stale, **adjusted_fresh}

    # --- Evaluate base vs stale vs fresh on train and holdout -------------
    train_eval = p2._eval_panels(all_panels, prices, full_prices, train_start, train_end)
    holdout_eval = p2._eval_panels(all_panels, prices, full_prices, holdout_start_str, holdout_end)
    print("train baselines:", train_eval["baselines_no_cost"], flush=True)
    print("holdout baselines:", holdout_eval["baselines_no_cost"], flush=True)

    # --- Reproducibility assertions vs Phase 2's recorded numbers --------
    p2_results = json.loads(P2_RESULTS_PATH.read_text())
    tol = 1e-9
    for window, eval_dict in (("train", train_eval), ("holdout", holdout_eval)):
        for cost in COSTS_BPS:
            for row in p2_results[window][f"cost_{cost}bps"]:
                variant = row["variant"]
                got = p2._eval_lookup(eval_dict, cost, variant)["total_return"]
                if abs(got - row["total_return"]) > tol:
                    raise RuntimeError(
                        f"reproduction failure: {window} {variant} @{cost}bps "
                        f"got {got}, Phase 2 recorded {row['total_return']}")
    print("reproduction check PASSED: base + stale train/holdout returns "
          "match Phase 2's recorded numbers exactly", flush=True)

    # --- Headline-pair diagnostics (fresh, 10bps) --------------------------
    diagnostics = {}
    for window, (start, end) in {
        "train": (train_start, train_end),
        "holdout": (holdout_start_str, holdout_end),
    }.items():
        diagnostics[window] = {
            "v4_regime_quality_risk": p2._diagnostics_for(
                "v4_regime_quality_risk", all_panels, prices, full_prices, start, end, 10),
            "v4_regime_quality_risk_llm": p2._diagnostics_for(
                "v4_regime_quality_risk_llm", all_panels, prices, full_prices, start, end, 10),
            "v4_regime_quality_risk_llm_fresh": p2._diagnostics_for(
                "v4_regime_quality_risk_llm_fresh", all_panels, prices, full_prices, start, end, 10),
        }

    # --- Gate verdict (headline pair v4 vs v4+LLM-fresh) --------------------
    sg1 = _ret(holdout_eval, 10, "v4_regime_quality_risk_llm_fresh") > _ret(holdout_eval, 10, "v4_regime_quality_risk")
    sg2 = _ret(holdout_eval, 20, "v4_regime_quality_risk_llm_fresh") > _ret(holdout_eval, 20, "v4_regime_quality_risk")
    sg3 = _ret(holdout_eval, 10, "v4_regime_quality_risk_llm_fresh") > 0
    verdict = "PASS" if (sg1 and sg2 and sg3) else "FAIL"
    gate = {
        "verdict": verdict,
        "headline_pair": "v4_regime_quality_risk vs v4_regime_quality_risk_llm_fresh",
        "sub_gates": {
            "SG1_holdout_net10_fresh_gt_base": sg1,
            "SG2_holdout_net20_fresh_gt_base": sg2,
            "SG3_holdout_net10_fresh_gt_zero": sg3,
        },
        "values": {
            "holdout_net10_base": _ret(holdout_eval, 10, "v4_regime_quality_risk"),
            "holdout_net10_stale": _ret(holdout_eval, 10, "v4_regime_quality_risk_llm"),
            "holdout_net10_fresh": _ret(holdout_eval, 10, "v4_regime_quality_risk_llm_fresh"),
            "holdout_net20_base": _ret(holdout_eval, 20, "v4_regime_quality_risk"),
            "holdout_net20_stale": _ret(holdout_eval, 20, "v4_regime_quality_risk_llm"),
            "holdout_net20_fresh": _ret(holdout_eval, 20, "v4_regime_quality_risk_llm_fresh"),
        },
    }
    supporting = {}
    for base in ("v1_composite", "v2_regime", "v3_regime_trend_quality"):
        supporting[base] = {
            "holdout_net10_base": _ret(holdout_eval, 10, base),
            "holdout_net10_stale": _ret(holdout_eval, 10, base + "_llm"),
            "holdout_net10_fresh": _ret(holdout_eval, 10, base + "_llm_fresh"),
            "holdout_net20_base": _ret(holdout_eval, 20, base),
            "holdout_net20_stale": _ret(holdout_eval, 20, base + "_llm"),
            "holdout_net20_fresh": _ret(holdout_eval, 20, base + "_llm_fresh"),
        }
    gate["supporting_pairs"] = supporting
    fresh_vs_stale = {
        "holdout_net10_delta_pp": round(
            (_ret(holdout_eval, 10, "v4_regime_quality_risk_llm_fresh")
             - _ret(holdout_eval, 10, "v4_regime_quality_risk_llm")) * 100, 2),
        "holdout_net20_delta_pp": round(
            (_ret(holdout_eval, 20, "v4_regime_quality_risk_llm_fresh")
             - _ret(holdout_eval, 20, "v4_regime_quality_risk_llm")) * 100, 2),
        "applied_cells_holdout_fresh": n_applied_fresh_ho,
        "applied_cells_holdout_stale": n_applied_stale_ho,
    }

    # --- Write results ------------------------------------------------------
    results = {
        "experiment_id": "exp-technical-plus-llm-freshness-v1",
        "run_date": run_started[:10],
        "run_started_utc": run_started,
        "meta": {
            "source": "Alpaca IEX, 2024-01-02..2026-11-13, 134 symbols",
            "train_window": f"{train_start}..{train_end}",
            "holdout_window": f"{holdout_start.date()}..{holdout_end} (final {HOLDOUT_DAYS_N} trading days)",
            "parameters": {
                "min_decision_score": MIN_DECISION_SCORE,
                "target_n_positions": TARGET_N_POSITIONS,
                "trail_percent": TRAIL_PERCENT,
                "max_positions": MAX_POSITIONS,
                "rebalance_every": REBALANCE_EVERY,
                "n_splits": N_SPLITS,
                "initial_capital": INITIAL_CAPITAL,
                "costs_bps": list(COSTS_BPS),
                "n_trials_dsr": N_TRIALS_DSR,
            },
            "llm_pipeline": {
                "screen": "claude-haiku-5-5",
                "adjudicate": "claude-sonnet-5-5",
                "audit": "claude-fable-5-2 (not run in this experiment)",
                "chatgpt_leg": "not run (OpenAI top-up never approved)",
            },
            "scoring_scope": "holdout-window filings dated 2026-08-18..2026-11-13, "
                             "in-scope only (75 filings); train filings reuse Phase 2 scores",
            "cost_estimation": "~2.9 chars/token (Phase 2 measured rate), "
                               "worst-case 1000-token output",
        },
        "scoring": {
            "in_scope_holdout_filings": len(in_scope_hw),
            "near_threshold_holdout_filings": near_threshold,
            "total_relevance_triples_holdout": int(counts.sum()),
            "sonnet_subset_size": len(sonnet_subset),
            "haiku": haiku_summary,
            "sonnet": sonnet_summary,
            "total_spend_usd": total_spend,
            "run_spend_usd": run_spend,
            "spend_cap_usd": CUMULATIVE_CAP_USD,
            "run_budget_usd": RUN_BUDGET_NEW_USD,
            "sonnet_budget_usd": SONNET_BUDGET_USD,
        },
        "coverage": {
            "applied_cells_fresh": n_applied_fresh,
            "applied_cells_fresh_holdout": n_applied_fresh_ho,
            "applied_cells_stale_holdout": n_applied_stale_ho,
            "active_technical_cells": n_active_cells,
            "applied_frac_of_active_fresh": round(n_applied_fresh / max(n_active_cells, 1), 4),
        },
        "train": train_eval,
        "holdout": holdout_eval,
        "diagnostics": diagnostics,
        "gate": gate,
        "fresh_vs_stale": fresh_vs_stale,
        "limits": [
            "Backtest only; no paper trading, no live money.",
            "Alpaca IEX bars are raw (unadjusted) prices; not survivorship-free.",
            "Only the top-10 most decision-relevant holdout filings got Sonnet adjudication (budget); the other 65 in-scope holdout filings are Haiku-only and contribute 0.",
            "Agreement is between two Claude-family models, not independent-model (Claude + ChatGPT) agreement.",
            "Universe is the current 134-symbol set; results are for that universe only.",
        ],
    }
    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(gate, indent=2), flush=True)
    print(json.dumps(fresh_vs_stale, indent=2), flush=True)
    print(f"GATE VERDICT: {verdict}", flush=True)
    print(f"results written to {RESULTS_PATH}", flush=True)
    print(f"scores written to {SCORES_PATH}", flush=True)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
