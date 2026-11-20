#!/usr/bin/env python3
"""Phase 2 driver: exp-technical-plus-llm-v1 (registered LLM backtest).

BACKTEST ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.

Pipeline (per the registered amendment exp-technical-plus-llm-v1-amendment-1):
1. Load cached Alpaca bars; build the 4 technical variant panels (same as v1/v2).
2. Load EDGAR point-in-time excerpts (dataset-edgar-point-in-time-v1).
3. Haiku 5.5 screens every in-scope filing dated <= 2026-08-17 (train
   window). Sonnet 5.5 adjudicates the ranked near-threshold subset
   within the $5.00 sub-budget. Fable 5.2 is audit-only and is NOT run
   here; there is no ChatGPT leg (OpenAI top-up never approved).
4. Build LLM contribution panels (holdout decisions use the latest
   scored filing carried forward; no holdout-window filing is scored),
   combine with the technical panels per the registered rule, and
   evaluate base vs LLM on the frozen holdout at 10bps and 2x costs.
5. Diagnostics + gate verdict; write results JSON; print a summary.

Spend guardrails (hard): total Anthropic spend for this run <= $10.00
(Haiku est ~$2.59, Sonnet sub-budget $5.00). The provider's own
run_cap (9.50) / monthly_cap (10.00) are backstops; the driver also
tracks cumulative spend itself and stops before either cap is crossed.
No new cash, no new keys, no purchases, no OpenAI top-up.

Usage:
    cd ~/workspace/stock-thesis
    .venv/bin/python run_llm_backtest.py
"""

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest import BacktestConfig, run_backtest
from config import TradingConfig
from diagnostics import (
    alpha_beta,
    deflated_sharpe,
    exposure_stats,
    information_ratio,
    return_significance,
    total_costs,
    trade_stats,
)
from llm_sentiment import (
    MAX_EXCERPT_CHARS,
    MAX_OUTPUT_TOKENS,
    MODEL_PRICES,
    SYSTEM_PROMPT,
    AnthropicProvider,
    combine_two_scores,
    cost_usd,
)
from llm_smoke_test import _SurrogateClient
from research_runner import (
    build_signal_variants,
    evaluate_variants,
    slice_research_window,
)
from run_real_backtest import _baseline_stats, _load_cached_bars

REPO = Path(__file__).parent
SHARD_DIR = REPO / "data" / "edgar_excerpts"
RESULTS_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1.json"
SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores.json"
# Incrementally-written per-role score files (raw accession -> result).
# Kept separate so the Sonnet pass never overwrites the Haiku scores
# and a resume after an interruption reads back exactly what was saved.
HAIKU_SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json"
SONNET_SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-sonnet.json"


def _load_scores(path: Path) -> dict:
    """Load an incrementally-written scores file (raw accession->result).

    Returns {} when the file is missing or unparsable, so a fresh run
    and a resumed run use the same code path.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

# Registered parameters (amendment-1).
MIN_DECISION_SCORE = 10.25  # live-path only; research selects top-5, no threshold
TARGET_N_POSITIONS = 5
TRAIL_PERCENT = 8.0
# Research window and frozen holdout, identical to v1/v2.
RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63
MAX_POSITIONS = 5
REBALANCE_EVERY = 1
N_SPLITS = 4
INITIAL_CAPITAL = 1000.0
COSTS_BPS = (10, 20)
N_TRIALS_DSR = 18  # 4 base variants + 4 LLM variants + 10 (dataset + scoring choices)

# Spend guardrails (USD). Already spent before THIS (re)run is read from
# the provider's monthly ledger (.cache/llm_spend.json) plus the $0.018034
# Haiku validation (Nov 17), which went through the raw helper, not the
# provider, so it is not in the ledger. Reading the ledger (instead of a
# hardcoded constant) makes a resumed run after an interruption account
# for the spend of the interrupted run instead of understating it.
VALIDATION_OFF_LEDGER_USD = 0.018034
LEDGER_PATH = REPO / ".cache" / "llm_spend.json"
RUN_CAP_USD = 10.00
SONNET_BUDGET_USD = 5.00
PROVIDER_RUN_CAP_USD = 9.50
PROVIDER_MONTHLY_CAP_USD = 10.00


def already_spent_usd() -> float:
    """Anthropic spend incurred before this (re)run, in dollars."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        data = json.loads(LEDGER_PATH.read_text())
        ledger_total = float(data.get(month, 0.0))
    except (OSError, ValueError, KeyError):
        ledger_total = 0.0
    return round(ledger_total + VALIDATION_OFF_LEDGER_USD, 6)
# Filing-date cutoff for scoring: train window only. Filings dated
# after this are holdout-window and are NEVER scored.
TRAIN_END_DATE = "2026-08-17"
API_KEY_PLACEHOLDER = "sk-ant-surrogate"


def estimate_call_cost(excerpt_chars: int, model_id: str) -> float:
    """Worst-case cost of one scoring call, mirroring the provider.

    Input tokens = system prompt + excerpt (trimmed to the provider's
    160k-char cap, ~4 chars/token); output = the provider's 1,000-token
    max. Actual output is usually ~330 tokens, so actual spend runs
    below this estimate.
    """
    est_in = max(1, len(SYSTEM_PROMPT) // 4) + max(1, min(excerpt_chars, MAX_EXCERPT_CHARS) // 4)
    price_in, price_out = MODEL_PRICES[model_id]
    return round(est_in / 1_000_000 * price_in + MAX_OUTPUT_TOKENS / 1_000_000 * price_out, 6)


# ---------------------------------------------------------------------------
# EDGAR filings
# ---------------------------------------------------------------------------
def load_edgar_filings() -> pd.DataFrame:
    """Load all EDGAR excerpt shards into one DataFrame.

    Columns: ticker, filing_date (Timestamp), form, accession,
    excerpt_text, excerpt_chars.
    """
    frames = []
    for shard in sorted(SHARD_DIR.glob("excerpts_*.jsonl")):
        records = [json.loads(line) for line in shard.read_text().splitlines() if line.strip()]
        frames.append(pd.DataFrame.from_records(records))
    filings = pd.concat(frames, ignore_index=True)
    filings["filing_date"] = pd.to_datetime(filings["filing_date"])
    filings["excerpt_chars"] = filings["excerpt_text"].str.len()
    return filings


def in_scope_filings(filings: pd.DataFrame, variants: dict, train_days: pd.DatetimeIndex) -> pd.DataFrame:
    """Filings dated <= TRAIN_END_DATE, current on >= 1 active train day
    for their OWN symbol (any variant's technical score > 0).

    Per-symbol: a filing can only affect a decision on a day when its
    own symbol's technical score is > 0, so a filing that is current
    only on days when its symbol is inactive is never scored. This is
    the amendment's recorded scope (1,166 filings on the real dataset).
    A filing is current from its filing date until the next filing for
    the same symbol. Holdout-window filings (dated > TRAIN_END_DATE)
    are excluded.
    """
    scoped = filings[filings["filing_date"] <= pd.Timestamp(TRAIN_END_DATE)].copy()
    scoped = scoped.sort_values(["ticker", "filing_date"])
    scoped["next_filing_date"] = scoped.groupby("ticker")["filing_date"].shift(-1)
    keep = []
    for symbol, group in scoped.groupby("ticker"):
        panels = [variants[v] for v in variants if symbol in variants[v].columns]
        if not panels:
            continue
        active_mask = np.any(
            [p.loc[train_days, symbol].to_numpy() > 0 for p in panels],
            axis=0,
        )
        active = train_days[active_mask]
        dates = group["filing_date"].to_numpy()
        next_dates = group["next_filing_date"].to_numpy()
        pos = np.searchsorted(active.to_numpy(), dates, side="left")
        for i, p in enumerate(pos):
            if p >= len(active):
                continue
            if pd.isna(next_dates[i]) or active[p] < next_dates[i]:
                keep.append(group.index[i])
    return scoped.loc[keep].reset_index(drop=True)


def relevance_counts(
    filings: pd.DataFrame,
    variants: dict,
    days: pd.DatetimeIndex,
) -> pd.Series:
    """Decision-relevance triple count per filing.

    For each (filing, day, variant) triple: the filing is the current
    filing for its symbol on that day AND the symbol's technical score
    is within +/-1.0 of that day's top-5 inclusion cutoff (i.e. the LLM
    adjustment could change whether the symbol is in the portfolio).
    Only days where the variant score for the symbol is > 0 count.
    Returns a Series indexed by the filings DataFrame index.
    NOTE: v2_regime and v3_regime_trend_quality produce identical
    panels, so their triples coincide; the count is a sum over all
    four variants as registered.
    """
    counts = pd.Series(0, index=filings.index, dtype=int)
    filings_sorted = filings.sort_values(["ticker", "filing_date"])
    by_symbol = {t: g for t, g in filings_sorted.groupby("ticker")}
    for name, panel in variants.items():
        for day in days:
            day_scores = panel.loc[day].dropna()
            if len(day_scores) == 0:
                continue
            cutoff = day_scores.nlargest(TARGET_N_POSITIONS).min()
            near = day_scores[(day_scores > 0) & ((day_scores - cutoff).abs() <= 1.0)]
            for symbol in near.index:
                group = by_symbol.get(symbol)
                if group is None:
                    continue
                current = group[group["filing_date"] <= day]
                if len(current) == 0:
                    continue
                counts.loc[current.index[-1]] += 1
    return counts


def select_sonnet_subset(
    scored: dict,
    counts: pd.Series,
    filings: pd.DataFrame,
    budget_usd: float,
) -> list:
    """Rank near-threshold filings for Sonnet adjudication, in budget.

    Candidates: in-scope filings with a Haiku score and Haiku confidence
    >= 0.6. Ranked by decision-relevance triple count descending,
    tie-break Haiku confidence ascending, then filing_date ascending.
    Taken in rank order while the cumulative estimated Sonnet cost stays
    within budget_usd. Returns a list of filing indexes (into filings).
    """
    candidates = []
    for idx, row in filings.iterrows():
        score = scored.get(row["accession"])
        if score is None or "error" in score:
            continue
        if score["confidence"] < 0.6:
            continue
        est = estimate_call_cost(int(row["excerpt_chars"]), "claude-sonnet-5-5")
        candidates.append(
            (int(counts.get(idx, 0)), float(score["confidence"]), row["filing_date"], idx, est)
        )
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    chosen, spent = [], 0.0
    for triples, conf, fdate, idx, est in candidates:
        if spent + est > budget_usd:
            continue
        chosen.append(idx)
        spent += est
    return chosen


def current_filing_series(
    filings: pd.DataFrame,
    days: pd.DatetimeIndex,
) -> pd.DataFrame:
    """For each symbol and day, the accession of the latest filing <= day.

    Only filings dated <= TRAIN_END_DATE are considered (holdout days
    carry forward the latest pre-holdout filing). NaN where a symbol has
    no filing on or before that day.
    """
    scoped = filings[filings["filing_date"] <= pd.Timestamp(TRAIN_END_DATE)].copy()
    scoped = scoped.sort_values(["ticker", "filing_date"])
    out = pd.DataFrame(index=days, columns=sorted(scoped["ticker"].unique()), dtype=object)
    for symbol, group in scoped.groupby("ticker"):
        dates = group["filing_date"].to_numpy()
        accessions = group["accession"].to_numpy()
        pos = np.searchsorted(dates, days.to_numpy(), side="right") - 1
        col = np.where(pos >= 0, accessions[np.maximum(pos, 0)], np.nan)
        out[symbol] = col
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _score_one(provider, filing: pd.Series, attempt: int = 1):
    """Score one filing with retry-once on transient failure."""
    documents = [{
        "doc": filing["form"],
        "section": "MD&A + Risk Factors",
        "date": filing["filing_date"].strftime("%Y-%m-%d"),
    }]
    try:
        return provider.score(
            symbol=filing["ticker"],
            documents=documents,
            excerpt=filing["excerpt_text"],
            as_of=filing["filing_date"].strftime("%Y-%m-%d"),
        )
    except Exception as exc:  # noqa: BLE001 - retry once, then record failure
        if attempt == 1:
            time.sleep(5)
            return _score_one(provider, filing, attempt=2)
        raise exc


def score_pass(
    provider,
    filings: pd.DataFrame,
    role: str,
    spent_before_usd: float,
    budget_usd: float,
    save_path: Path,
    scores: dict,
) -> dict:
    """Score every filing in `filings` with `provider`; returns a summary.

    `scores` is a dict accession -> result dict, updated in place and
    saved incrementally (every 100 calls) so a crash loses at most 100
    calls (the provider's disk cache makes re-running them $0 anyway).
    Spend is tracked from actual result costs and stops before
    budget_usd is exceeded. A circuit breaker aborts after 10
    consecutive failures.
    """
    spent = spent_before_usd
    n_scored = n_failed = n_cached = 0
    consecutive_failures = 0
    tokens_in = tokens_out = 0
    for i, (_, filing) in enumerate(filings.iterrows()):
        accession = filing["accession"]
        if accession in scores and "error" not in scores[accession]:
            continue
        est = estimate_call_cost(int(filing["excerpt_chars"]), provider.model_id)
        if spent + est > budget_usd:
            print(f"[{role}] budget guard: stopping before {accession} "
                  f"(spent ${spent:.4f} + est ${est:.4f} > ${budget_usd:.2f})",
                  flush=True)
            break
        try:
            result = _score_one(provider, filing)
        except Exception as exc:  # noqa: BLE001 - record and continue
            n_failed += 1
            consecutive_failures += 1
            scores[accession] = {"error": str(exc), "ticker": filing["ticker"],
                                 "filing_date": filing["filing_date"].strftime("%Y-%m-%d")}
            print(f"[{role}] FAILED {accession} ({filing['ticker']}): {exc}", flush=True)
            if consecutive_failures >= 10:
                raise RuntimeError(f"[{role}] circuit breaker: 10 consecutive failures; aborting")
            continue
        consecutive_failures = 0
        scores[accession] = result
        # A cache hit returns the ORIGINAL result dict (with its original
        # cost_usd) but bills $0: the provider did not re-charge. Count
        # it as $0, or a resumed run double-counts the spend.
        is_cached = bool(result.get("cached"))
        cost = 0.0 if is_cached else float(result.get("cost_usd", 0.0))
        spent += cost
        tokens_in += int(result.get("input_tokens", 0))
        tokens_out += int(result.get("output_tokens", 0))
        if is_cached:
            n_cached += 1
        else:
            n_scored += 1
        if (i + 1) % 100 == 0:
            save_path.write_text(json.dumps(scores, indent=2))
            print(f"[{role}] {i + 1}/{len(filings)} filings, spent ${spent:.4f} "
                  f"({n_scored} new, {n_cached} cached, {n_failed} failed)", flush=True)
        time.sleep(0.2)
    save_path.write_text(json.dumps(scores, indent=2))
    return {
        "n_attempted": len(filings),
        "n_scored_new": n_scored,
        "n_cached": n_cached,
        "n_failed": n_failed,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": round(spent - spent_before_usd, 6),
        "spent_after_usd": round(spent, 6),
    }


# ---------------------------------------------------------------------------
# Combination and evaluation
# ---------------------------------------------------------------------------
def build_contribution_panel(
    current: pd.DataFrame,
    haiku_scores: dict,
    sonnet_scores: dict,
) -> pd.DataFrame:
    """Daily LLM contribution per symbol, from the current filing's scores.

    The current filing is the latest train-scored filing <= that day.
    Contribution is 0 unless the filing has BOTH a Haiku and a Sonnet
    score that agree (>= 0.7) with both confidences >= 0.6 and the same
    sign; then it is the mean of the two scores, capped at +/-1.0.
    Also returns a mask of cells where the adjustment was applied.
    """
    contrib = pd.DataFrame(0.0, index=current.index, columns=current.columns)
    applied = pd.DataFrame(False, index=current.index, columns=current.columns)
    for symbol in current.columns:
        for day in current.index:
            accession = current.at[day, symbol]
            if not isinstance(accession, str):
                continue
            h = haiku_scores.get(accession)
            s = sonnet_scores.get(accession)
            if h is None or s is None or "error" in h or "error" in s:
                continue
            out = combine_two_scores(
                technical_score=0.0,
                score_a=float(h["score"]),
                confidence_a=float(h["confidence"]),
                score_b=float(s["score"]),
                confidence_b=float(s["confidence"]),
            )
            if out["applied"]:
                contrib.at[day, symbol] = out["llm_contribution"]
                applied.at[day, symbol] = True
    return contrib, applied


def build_adjusted_panels(variants: dict, contribution: pd.DataFrame) -> dict:
    """Technical panel + LLM contribution, only where the technical score > 0.

    The LLM never creates a trade alone: a symbol with technical score
    0 (or NaN) stays 0 (or NaN). Adjusted = tech + contribution.
    """
    adjusted = {}
    for name, panel in variants.items():
        adj = panel.copy()
        mask = panel > 0
        adj = adj.where(~mask, panel + contribution.reindex_like(panel).fillna(0.0))
        adjusted[name + "_llm"] = adj
    return adjusted


def _trade_count(weights: pd.DataFrame) -> int:
    """Number of position entries (same definition as evaluate_variants)."""
    held = weights.fillna(0.0)
    entries = (held > 0) & (held.shift(1).fillna(0.0) <= 0)
    return int(entries.sum().sum())


def _eval_panels(panels: dict, prices: pd.DataFrame, full_prices: pd.DataFrame,
                 start: str, end: str):
    """Evaluate a dict of panels on one window at 10/20bps (v1 settings).

    Mirrors run_real_backtest's _eval: rows are lists of record dicts
    (same shape as the v1 results JSON), plus no-cost baselines.
    """
    window_prices = prices.loc[start:end]
    window_panels = {k: v.loc[start:end] for k, v in panels.items()}
    full_window = full_prices.loc[start:end]
    results = {}
    for cost_bps in COSTS_BPS:
        result = evaluate_variants(
            window_prices, window_panels, cost_bps=cost_bps,
            max_positions=MAX_POSITIONS, rebalance_every=REBALANCE_EVERY,
            n_splits=N_SPLITS, initial_capital=INITIAL_CAPITAL,
        )
        results[f"cost_{cost_bps}bps"] = result.to_dict(orient="records")
    results["baselines_no_cost"] = _baseline_stats(full_window, "SPY")
    return results


def _eval_lookup(eval_dict: dict, cost_bps: int, variant: str) -> dict:
    """Find one variant's record in an _eval_panels result."""
    for row in eval_dict[f"cost_{cost_bps}bps"]:
        if row["variant"] == variant:
            return row
    raise KeyError(f"{variant} not in cost_{cost_bps}bps results")


def _diagnostics_for(
    variant: str,
    panels: dict,
    prices: pd.DataFrame,
    full_prices: pd.DataFrame,
    start: str,
    end: str,
    cost_bps: int,
) -> dict:
    """Full diagnostics for one variant on one window at one cost level."""
    window_prices = prices.loc[start:end]
    panel = panels[variant].loc[start:end]
    cfg = BacktestConfig(
        cost_bps=cost_bps, long_only=True, max_positions=MAX_POSITIONS,
        rebalance_every=REBALANCE_EVERY, min_hold_days=1,
    )
    out = run_backtest(window_prices, panel, cfg)
    gross_cfg = BacktestConfig(
        cost_bps=0.0, long_only=True, max_positions=MAX_POSITIONS,
        rebalance_every=REBALANCE_EVERY, min_hold_days=1,
    )
    gross = run_backtest(window_prices, panel, gross_cfg)
    total_return = float(out.equity_curve.iloc[-1]) - 1.0
    gross_return = float(gross.equity_curve.iloc[-1]) - 1.0
    returns = out.daily_returns
    spy_returns = full_prices.loc[start:end]["SPY"].pct_change().dropna()
    diag = {
        "total_return": round(total_return, 4),
        "gross_return": round(gross_return, 4),
        "cost_drag_frac": round(gross_return - total_return, 4),
        "sharpe": out.stats["sharpe"],
        "max_drawdown": out.stats["max_drawdown"],
        "trades": _trade_count(out.weights),
        "deflated_sharpe": deflated_sharpe(returns, N_TRIALS_DSR),
        "alpha_beta_vs_spy": alpha_beta(returns, spy_returns),
        "information_ratio_vs_spy": information_ratio(returns, spy_returns),
        "return_significance": return_significance(returns),
        "trade_stats": trade_stats(out.weights, window_prices, cost_bps),
        "total_costs": total_costs(out.turnover, cost_bps, INITIAL_CAPITAL),
        "exposure": exposure_stats(out.weights),
    }
    return diag


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    run_started = datetime.now(timezone.utc).isoformat()
    print("=== Phase 2: exp-technical-plus-llm-v1 ===", flush=True)
    print(f"run started {run_started}", flush=True)
    already_spent = already_spent_usd()
    print(f"spend already incurred before this run: ${already_spent:.6f} "
          f"(ledger + ${VALIDATION_OFF_LEDGER_USD:.6f} off-ledger validation)", flush=True)

    # --- Load bars and technical panels (identical to v1) -------------------
    data = _load_cached_bars()
    if data is None:
        raise RuntimeError("no cached bars; .cache/backtest_data is empty")
    cfg = TradingConfig.from_env()
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, RESEARCH_START, RESEARCH_END)
    # Full close panel (including the SPY benchmark) for baselines and
    # SPY returns; build_signal_variants returns the universe-only panel.
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

    # --- Load EDGAR filings --------------------------------------------------
    filings = load_edgar_filings()
    print(f"EDGAR filings loaded: {len(filings)}", flush=True)

    # --- Scope and relevance (train days only) --------------------------------
    in_scope = in_scope_filings(filings, variants, train_days)
    print(f"in-scope filings (dated <= {TRAIN_END_DATE}, current on >=1 active "
          f"train day for their symbol): {len(in_scope)}", flush=True)
    counts = relevance_counts(in_scope, variants, train_days)
    near_threshold = int((counts > 0).sum())
    print(f"near-threshold filings (relevance > 0): {near_threshold} "
          f"of {len(in_scope)}; total relevance triples: {int(counts.sum())}", flush=True)

    # --- Haiku screen --------------------------------------------------------
    haiku = AnthropicProvider(
        API_KEY_PLACEHOLDER,
        role="screen",
        run_cap_usd=PROVIDER_RUN_CAP_USD,
        monthly_cap_usd=PROVIDER_MONTHLY_CAP_USD,
        client=_SurrogateClient(),
    )
    haiku_scores = _load_scores(HAIKU_SCORES_PATH)
    print(f"resuming: {len(haiku_scores)} Haiku scores already on disk", flush=True)
    haiku_summary = score_pass(
        haiku, in_scope, "haiku",
        spent_before_usd=already_spent,
        budget_usd=RUN_CAP_USD,
        save_path=HAIKU_SCORES_PATH,
        scores=haiku_scores,
    )
    print(f"Haiku pass: {haiku_summary}", flush=True)
    spent = haiku_summary["spent_after_usd"]

    # --- Sonnet adjudication -------------------------------------------------
    sonnet_subset = select_sonnet_subset(
        haiku_scores, counts, in_scope, SONNET_BUDGET_USD,
    )
    print(f"Sonnet subset: {len(sonnet_subset)} filings (ranked by relevance, "
          f"Haiku conf >= 0.6, est cost within ${SONNET_BUDGET_USD:.2f})", flush=True)
    sonnet = AnthropicProvider(
        API_KEY_PLACEHOLDER,
        role="adjudicate",
        run_cap_usd=PROVIDER_RUN_CAP_USD,
        monthly_cap_usd=PROVIDER_MONTHLY_CAP_USD,
        client=_SurrogateClient(),
    )
    sonnet_scores = _load_scores(SONNET_SCORES_PATH)
    print(f"resuming: {len(sonnet_scores)} Sonnet scores already on disk", flush=True)
    sonnet_filings = in_scope.loc[sonnet_subset] if sonnet_subset else in_scope.iloc[0:0]
    sonnet_summary = score_pass(
        sonnet, sonnet_filings, "sonnet",
        spent_before_usd=spent,
        budget_usd=RUN_CAP_USD,
        save_path=SONNET_SCORES_PATH,
        scores=sonnet_scores,
    )
    print(f"Sonnet pass: {sonnet_summary}", flush=True)
    spent = sonnet_summary["spent_after_usd"]
    total_spend = round(spent, 6)
    print(f"TOTAL Anthropic spend this run: ${total_spend - already_spent:.6f} "
          f"(cumulative incl. prior: ${total_spend:.6f})", flush=True)
    if total_spend > RUN_CAP_USD:
        raise RuntimeError(f"spend cap breached: ${total_spend:.4f} > ${RUN_CAP_USD:.2f}")

    # Persist combined scores file.
    SCORES_PATH.write_text(json.dumps({
        "haiku": haiku_scores,
        "sonnet": sonnet_scores,
        "haiku_summary": haiku_summary,
        "sonnet_summary": sonnet_summary,
        "total_spend_usd": total_spend,
    }, indent=2))

    # --- Contribution and adjusted panels ------------------------------------
    current = current_filing_series(in_scope, window_days)
    contribution, applied = build_contribution_panel(current, haiku_scores, sonnet_scores)
    n_applied_cells = int(applied.sum().sum())
    n_active_cells = int(((pd.concat(variants, axis=1) > 0).sum().sum()))
    print(f"LLM contribution applied on {n_applied_cells} (symbol, day) cells "
          f"out of {n_active_cells} active technical cells", flush=True)
    print(f"contribution distribution: mean {contribution[applied].stack().mean():.3f}, "
          f"min {contribution[applied].stack().min():.3f}, "
          f"max {contribution[applied].stack().max():.3f}", flush=True)
    adjusted = build_adjusted_panels(variants, contribution)
    all_panels = {**variants, **adjusted}

    # --- Evaluate base vs LLM on train and holdout ----------------------------
    train_eval = _eval_panels(all_panels, prices, full_prices, train_start, train_end)
    holdout_eval = _eval_panels(all_panels, prices, full_prices, holdout_start_str, holdout_end)
    print("train baselines:", train_eval["baselines_no_cost"], flush=True)
    print("holdout baselines:", holdout_eval["baselines_no_cost"], flush=True)

    # --- Headline-pair diagnostics -------------------------------------------
    diagnostics = {}
    for window, (start, end) in {
        "train": (train_start, train_end),
        "holdout": (holdout_start_str, holdout_end),
    }.items():
        diagnostics[window] = {
            "v4_regime_quality_risk": _diagnostics_for(
                "v4_regime_quality_risk", all_panels, prices, full_prices, start, end, 10),
            "v4_regime_quality_risk_llm": _diagnostics_for(
                "v4_regime_quality_risk_llm", all_panels, prices, full_prices, start, end, 10),
        }

    # --- Gate verdict (headline pair v4 vs v4+LLM) ----------------------------
    def _ret(eval_dict, cost, variant):
        return _eval_lookup(eval_dict, cost, variant)["total_return"]

    sg1 = _ret(holdout_eval, 10, "v4_regime_quality_risk_llm") > _ret(holdout_eval, 10, "v4_regime_quality_risk")
    sg2 = _ret(holdout_eval, 20, "v4_regime_quality_risk_llm") > _ret(holdout_eval, 20, "v4_regime_quality_risk")
    sg3 = _ret(holdout_eval, 10, "v4_regime_quality_risk_llm") > 0
    verdict = "PASS" if (sg1 and sg2 and sg3) else "FAIL"
    gate = {
        "verdict": verdict,
        "headline_pair": "v4_regime_quality_risk vs v4_regime_quality_risk_llm",
        "sub_gates": {
            "SG1_holdout_net10_llm_gt_base": sg1,
            "SG2_holdout_net20_llm_gt_base": sg2,
            "SG3_holdout_net10_llm_gt_zero": sg3,
        },
        "values": {
            "holdout_net10_base": _ret(holdout_eval, 10, "v4_regime_quality_risk"),
            "holdout_net10_llm": _ret(holdout_eval, 10, "v4_regime_quality_risk_llm"),
            "holdout_net20_base": _ret(holdout_eval, 20, "v4_regime_quality_risk"),
            "holdout_net20_llm": _ret(holdout_eval, 20, "v4_regime_quality_risk_llm"),
        },
    }
    # Supporting pairs (not gates).
    supporting = {}
    for base in ("v1_composite", "v2_regime", "v3_regime_trend_quality"):
        supporting[base] = {
            "holdout_net10_base": _ret(holdout_eval, 10, base),
            "holdout_net10_llm": _ret(holdout_eval, 10, base + "_llm"),
            "holdout_net20_base": _ret(holdout_eval, 20, base),
            "holdout_net20_llm": _ret(holdout_eval, 20, base + "_llm"),
        }
    gate["supporting_pairs"] = supporting

    # --- Write results --------------------------------------------------------
    results = {
        "experiment_id": "exp-technical-plus-llm-v1",
        "amendment": "exp-technical-plus-llm-v1-amendment-1",
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
            "scoring_scope": f"filings dated <= {TRAIN_END_DATE} only; holdout "
                              "decisions use the latest scored filing carried forward",
        },
        "scoring": {
            "in_scope_filings": len(in_scope),
            "near_threshold_filings": near_threshold,
            "total_relevance_triples": int(counts.sum()),
            "sonnet_subset_size": len(sonnet_subset),
            "haiku": haiku_summary,
            "sonnet": sonnet_summary,
            "total_spend_usd": total_spend,
            "run_spend_usd": round(total_spend - already_spent, 6),
            "spend_cap_usd": RUN_CAP_USD,
            "sonnet_budget_usd": SONNET_BUDGET_USD,
        },
        "coverage": {
            "applied_cells": n_applied_cells,
            "active_technical_cells": n_active_cells,
            "applied_frac_of_active": round(n_applied_cells / max(n_active_cells, 1), 4),
        },
        "train": train_eval,
        "holdout": holdout_eval,
        "diagnostics": diagnostics,
        "gate": gate,
        "limits": [
            "Backtest only; no paper trading, no live money.",
            "Alpaca IEX bars are raw (unadjusted) prices; not survivorship-free.",
            "Holdout sentiment is stale by construction (latest pre-holdout filing carried forward).",
            "Agreement is between two Claude-family models, not independent-model (Claude + ChatGPT) agreement.",
            "Universe is the current 134-symbol set; results are for that universe only.",
        ],
    }
    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps(gate, indent=2), flush=True)
    print(f"GATE VERDICT: {verdict}", flush=True)
    print(f"results written to {RESULTS_PATH}", flush=True)
    print(f"scores written to {SCORES_PATH}", flush=True)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
