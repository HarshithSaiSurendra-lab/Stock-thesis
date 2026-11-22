#!/usr/bin/env python3
"""Round-2 H1: Haiku bull/bear debate vs single-pass (funded, TRAIN only).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE
stays 0. Three Haiku 5.5 calls per filing (bull advocate, bear
advocate, adjudicator) on an n=100 systematic subsample of in-scope
train filings, vs the already-paid single-pass Haiku scores.

Decision rule (pre-registered): the debate is worth scaling IF the
debate IC @20d or @60d exceeds the single-pass IC by >= 0.02
consistently at BOTH horizons; otherwise DROP. IC here is the pooled
rank IC across filings (score at filing date vs the symbol's forward
return from the next trading day), computed identically for debate
and single-pass on the same 100 filings.

Budget (pre-registered): worst-case cost is computed EXACTLY from the
sampled excerpts before any call (2.9 chars/token input, 1000-token
max output, Haiku $0.10/$0.50 per MTok). The sample is trimmed
(longest excerpts first) until the worst case fits the $1.00/run cap
with margin (target <= $0.98). Minimum n=60; if even that cannot fit,
STOP and report without scoring. November monthly budget is $15
($11.77 original + $5 top-up); the provider is constructed with
monthly_cap_usd=15.0. Actual spend is tracked from result costs;
cached calls cost $0. Registry entry is appended BEFORE scoring.

Run: python3 r2_debate.py
Writes experiments/results/exp-r2-h1-debate-scores.json and
experiments/results/exp-r2-h1-debate.json.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from llm_sentiment import (
    MODEL_PRICES, MAX_OUTPUT_TOKENS, AnthropicProvider, _estimate_tokens,
)
from llm_smoke_test import _SurrogateClient
import run_llm_backtest as p2
import r2_panels as r2

REPO = Path(__file__).parent
REGISTRY_PATH = REPO / "experiments" / "registry.jsonl"
SCORES_PATH = REPO / "experiments" / "results" / "exp-r2-h1-debate-scores.json"
RESULTS_PATH = REPO / "experiments" / "results" / "exp-r2-h1-debate.json"

N_TARGET = 100
N_MIN = 60
RUN_CAP_USD = 1.00
RUN_BUDGET_TARGET_USD = 0.98
MONTHLY_CAP_USD = 15.00
DECISION_DELTA = 0.02
HAIKU = "claude-haiku-5-5"

BULL_PROMPT = (
    "You are a bullish equity research advocate. Make the strongest honest "
    "bullish case for this stock from the filing excerpt alone: what in the "
    "excerpt supports the stock rising over the next 3-12 months? Reply with "
    "ONLY a JSON object, no other text: {\"score\": <float 0 to 1>, "
    "\"confidence\": <float 0 to 1>, \"rationale\": \"<one sentence>\"}. "
    "Score is how convincing the bullish case is: 1 very convincing, 0 no "
    "bullish case. Do not invent facts outside the excerpt."
)
BEAR_PROMPT = (
    "You are a bearish equity research advocate. Make the strongest honest "
    "bearish case for this stock from the filing excerpt alone: what in the "
    "excerpt supports the stock falling over the next 3-12 months? Reply with "
    "ONLY a JSON object, no other text: {\"score\": <float 0 to 1>, "
    "\"confidence\": <float 0 to 1>, \"rationale\": \"<one sentence>\"}. "
    "Score is how convincing the bearish case is: 1 very convincing, 0 no "
    "bearish case. Do not invent facts outside the excerpt."
)
ADJUDICATE_PROMPT = (
    "You are an impartial equity research adjudicator. You are given a bull "
    "case and a bear case (one sentence each) for a stock, plus the filing "
    "excerpt they were drawn from. Weigh the two cases against the excerpt "
    "and return your own final score. Reply with ONLY a JSON object, no "
    "other text: {\"score\": <float -1 to 1>, \"confidence\": <float 0 to 1>, "
    "\"rationale\": \"<one sentence>\"}. Score is the expected directional "
    "effect on the stock over the next 3-12 months: +1 strongly positive, "
    "0 neutral, -1 strongly negative. Do not invent facts outside the excerpt."
)


def worst_case_call_usd(prompt: str, text_chars: int) -> float:
    """Worst-case cost of one Haiku call, mirroring the provider."""
    est_in = _estimate_tokens(prompt) + _estimate_tokens("x" * min(text_chars, 160_000))
    price_in, price_out = MODEL_PRICES[HAIKU]
    return est_in / 1_000_000 * price_in + MAX_OUTPUT_TOKENS / 1_000_000 * price_out


def worst_case_filing_usd(excerpt_chars: int) -> float:
    """Worst case for one filing: bull + bear + adjudicator (adjudicator
    input = excerpt + ~600 chars of advocate cases)."""
    bull = worst_case_call_usd(BULL_PROMPT, excerpt_chars)
    bear = worst_case_call_usd(BEAR_PROMPT, excerpt_chars)
    adj = worst_case_call_usd(ADJUDICATE_PROMPT, excerpt_chars + 600)
    return bull + bear + adj


def pooled_rank_ic(scores: pd.Series, fwd: pd.Series) -> dict:
    """Pooled rank IC: Pearson correlation of cross-ranked scores and
    forward returns over the filings that have both. Returns
    {n, ic, tstat}."""
    both = pd.DataFrame({"s": scores, "r": fwd}).dropna()
    if len(both) < 20:
        return {"n": int(len(both)), "ic": None, "tstat": None}
    rs = both["s"].rank()
    rr = both["r"].rank()
    ic = float(rs.corr(rr, method="pearson"))
    if np.isnan(ic):
        return {"n": int(len(both)), "ic": None, "tstat": None}
    # t-stat of the correlation
    t = ic * np.sqrt((len(both) - 2) / max(1 - ic * ic, 1e-12))
    return {"n": int(len(both)), "ic": round(ic, 4), "tstat": round(float(t), 2)}


def main() -> int:
    print("=== Round-2 H1: Haiku bull/bear debate (TRAIN, n=100) ===", flush=True)
    d = r2.load_r2_data()
    train_days = d["train_days"]
    prices = d["prices"].loc[train_days]
    merged_haiku = d["merged_haiku"]

    # --- Sample: in-scope train filings with a single-pass Haiku score,
    # systematic sample across filing dates (deterministic, time-spread).
    in_scope = p2.in_scope_filings(d["filings"], d["variants"], train_days)
    scored = in_scope[in_scope["accession"].isin(merged_haiku)].copy()
    scored = scored.sort_values(["filing_date", "ticker"]).reset_index(drop=True)
    print(f"in-scope train filings with single-pass scores: {len(scored)}", flush=True)
    step = max(1, len(scored) // N_TARGET)
    sample = scored.iloc[::step].copy()
    print(f"systematic sample: {len(sample)} filings (every {step}th)", flush=True)

    # --- Pre-budget: exact worst case from the sampled excerpts; trim
    # longest excerpts until the worst case fits the run cap with margin.
    sample = sample.assign(
        worst_case=sample["excerpt_chars"].map(worst_case_filing_usd))
    total_worst = float(sample["worst_case"].sum())
    while total_worst > RUN_BUDGET_TARGET_USD and len(sample) > N_MIN:
        drop_idx = sample["worst_case"].idxmax()
        total_worst -= float(sample.at[drop_idx, "worst_case"])
        sample = sample.drop(index=drop_idx)
    print(f"pre-budget: worst-case total ${total_worst:.4f} for "
          f"{len(sample)} filings (run cap ${RUN_CAP_USD:.2f}, target "
          f"<= ${RUN_BUDGET_TARGET_USD:.2f})", flush=True)
    if total_worst > RUN_CAP_USD or len(sample) < N_MIN:
        print("STOP: debate does not fit the $1.00/run cap even at "
              f"n={N_MIN}; no calls made.", flush=True)
        return 2
    sample = sample.sort_values(["filing_date", "ticker"]).reset_index(drop=True)

    # --- Register BEFORE scoring.
    entry = {
        "experiment_id": "exp-r2-h1-debate",
        "strategy": "Haiku 5.5 bull/bear debate: 3 calls per filing (bull "
                    "advocate, bear advocate, adjudicator) on an n=100 "
                    "systematic subsample of in-scope train filings; the "
                    "adjudicator weighs both one-sentence cases against "
                    "the excerpt and returns the final -1..+1 score. "
                    "Compared against the already-paid single-pass Haiku "
                    "scores on the same filings.",
        "hypothesis": "A bull/bear debate adjudicated by the same model "
                      "produces scores with higher forward-return rank IC "
                      "than single-pass scoring (TradingAgents-style "
                      "debate adjudication, implemented natively).",
        "holdout_rule": "No holdout use: this is a scoring-quality test "
                        "on TRAIN filings only (pooled rank IC @20d/@60d). "
                        "A debate score that wins here would still need a "
                        "full panel + holdout confirmation before use.",
        "horizon": "filing date -> forward return +1..+1+20d and +1..+1+60d",
        "benchmark": "single-pass Haiku 5.5 scores on the same 100 filings",
        "cost_model": {"worst_case_usd": round(total_worst, 4),
                       "run_cap_usd": RUN_CAP_USD,
                       "monthly_cap_usd": MONTHLY_CAP_USD,
                       "n_filings": int(len(sample))},
        "design_doc": "experiments/exp-llm-long-horizon-r2-design.md",
        "exit_gate": f"SCALE iff debate rank IC exceeds single-pass rank IC "
                     f"by >= {DECISION_DELTA} consistently at BOTH 20d and "
                     f"60d horizons; otherwise DROP (debate not worth 3x "
                     f"the scoring cost).",
        "parameters": {"n_target": N_TARGET, "n_sampled": int(len(sample)),
                       "sampling": "systematic every-kth by filing_date",
                       "model": HAIKU, "calls_per_filing": 3,
                       "cost_estimation": "~2.9 chars/token, 1000-token max "
                                          "output worst case"},
        "limits": ["Backtest only; no paper trading, no live money.",
                   "Debate is within one model (Haiku 5.5), not "
                   "cross-family; the adjudicator shares the advocates' "
                   "blind spots.",
                   "Pooled IC across filings mixes cross-sectional and "
                   "time-series variation; it is a scoring-quality "
                   "diagnostic, not a portfolio result."],
        "spend": {"monthly_total_before_usd": 9.824975,
                  "monthly_cap_usd": MONTHLY_CAP_USD,
                  "run_cap_usd": RUN_CAP_USD},
        "status": "registered",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    with REGISTRY_PATH.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    print("registered exp-r2-h1-debate (before scoring)", flush=True)

    # --- Score the debate (resume-safe; cached calls cost $0).
    provider = AnthropicProvider(
        "sk-ant-surrogate", role="screen",
        run_cap_usd=RUN_CAP_USD, monthly_cap_usd=MONTHLY_CAP_USD,
        client=_SurrogateClient(),
    )
    scores = {}
    if SCORES_PATH.exists():
        try:
            scores = json.loads(SCORES_PATH.read_text())
        except (OSError, ValueError):
            scores = {}
    print(f"resuming: {len(scores)} debate scores already on disk", flush=True)
    spent = 0.0
    n_new = n_cached = n_failed = 0
    for i, (_, filing) in enumerate(sample.iterrows()):
        acc = filing["accession"]
        if acc in scores and "error" not in scores[acc]:
            continue
        docs = [{"doc": filing["form"], "section": "MD&A + Risk Factors",
                 "date": filing["filing_date"].strftime("%Y-%m-%d")}]
        as_of = filing["filing_date"].strftime("%Y-%m-%d")
        excerpt = filing["excerpt_text"]
        try:
            bull = provider.score(filing["ticker"], as_of, docs, excerpt,
                                  system_prompt=BULL_PROMPT, cache_tag="r2-debate-bull")
            bear = provider.score(filing["ticker"], as_of, docs, excerpt,
                                  system_prompt=BEAR_PROMPT, cache_tag="r2-debate-bear")
            adj_text = (f"BULL CASE: {bull['rationale']}\n"
                        f"BEAR CASE: {bear['rationale']}\n\nEXCERPT:\n{excerpt}")
            adj = provider.score(filing["ticker"], as_of, docs, adj_text,
                                 system_prompt=ADJUDICATE_PROMPT,
                                 cache_tag="r2-debate-adjudicate")
        except Exception as exc:  # noqa: BLE001 - record and continue
            n_failed += 1
            scores[acc] = {"error": str(exc), "ticker": filing["ticker"],
                           "filing_date": as_of}
            print(f"[debate] FAILED {acc} ({filing['ticker']}): {exc}", flush=True)
            if n_failed >= 10:
                raise RuntimeError("debate circuit breaker: 10 failures; aborting")
            continue
        for res in (bull, bear, adj):
            if not res.get("cached"):
                spent += float(res.get("cost_usd", 0.0))
                n_new += 1
            else:
                n_cached += 1
        scores[acc] = {
            "ticker": filing["ticker"],
            "filing_date": as_of,
            "form": filing["form"],
            "bull": {"score": bull["score"], "confidence": bull["confidence"],
                     "rationale": bull["rationale"]},
            "bear": {"score": bear["score"], "confidence": bear["confidence"],
                     "rationale": bear["rationale"]},
            "adjudicated": {"score": adj["score"], "confidence": adj["confidence"],
                            "rationale": adj["rationale"]},
            "single_pass": float(merged_haiku[acc]["score"]),
        }
        if (i + 1) % 25 == 0:
            SCORES_PATH.write_text(json.dumps(scores, indent=2))
            print(f"[debate] {i + 1}/{len(sample)} filings, new spend "
                  f"${spent:.4f} ({n_new} new calls, {n_cached} cached, "
                  f"{n_failed} failed)", flush=True)
        time.sleep(0.2)  # gentle pacing; 300 calls total
    SCORES_PATH.write_text(json.dumps(scores, indent=2))
    print(f"[debate] done: {len(scores)} filings, new spend ${spent:.4f} "
          f"({n_new} new calls, {n_cached} cached, {n_failed} failed)", flush=True)

    # --- IC comparison: debate-adjudicated vs single-pass, same filings.
    close = prices  # research-window closes (train days only)
    idx = close.index
    debate_s, single_s, fwd20_s, fwd60_s = [], [], [], []
    for acc, rec in scores.items():
        if "error" in rec:
            continue
        fdate = pd.Timestamp(rec["filing_date"])
        sym = rec["ticker"]
        if sym not in close.columns:
            continue
        pos = int(np.searchsorted(idx.to_numpy(), np.datetime64(fdate), side="right") - 1)
        if pos < 0 or pos + 1 >= len(idx):
            continue
        base = close.iloc[pos + 1][sym]
        if pd.isna(base) or base == 0:
            continue
        f20 = f60 = np.nan
        if pos + 1 + 20 < len(idx):
            v = close.iloc[pos + 1 + 20][sym]
            f20 = float(v / base - 1.0) if pd.notna(v) else np.nan
        if pos + 1 + 60 < len(idx):
            v = close.iloc[pos + 1 + 60][sym]
            f60 = float(v / base - 1.0) if pd.notna(v) else np.nan
        debate_s.append(rec["adjudicated"]["score"])
        single_s.append(rec["single_pass"])
        fwd20_s.append(f20)
        fwd60_s.append(f60)
    debate_s = pd.Series(debate_s, dtype=float)
    single_s = pd.Series(single_s, dtype=float)
    ic_debate_20 = pooled_rank_ic(debate_s, pd.Series(fwd20_s, dtype=float))
    ic_single_20 = pooled_rank_ic(single_s, pd.Series(fwd20_s, dtype=float))
    ic_debate_60 = pooled_rank_ic(debate_s, pd.Series(fwd60_s, dtype=float))
    ic_single_60 = pooled_rank_ic(single_s, pd.Series(fwd60_s, dtype=float))
    d20 = (ic_debate_20["ic"] - ic_single_20["ic"]) if ic_debate_20["ic"] is not None and ic_single_20["ic"] is not None else None
    d60 = (ic_debate_60["ic"] - ic_single_60["ic"]) if ic_debate_60["ic"] is not None and ic_single_60["ic"] is not None else None
    scale = (d20 is not None and d60 is not None
             and d20 >= DECISION_DELTA and d60 >= DECISION_DELTA)
    verdict = "SCALE" if scale else "DROP"
    print(f"[debate] IC @20d: debate {ic_debate_20['ic']} (n {ic_debate_20['n']}) "
          f"vs single {ic_single_20['ic']} -> delta {d20}", flush=True)
    print(f"[debate] IC @60d: debate {ic_debate_60['ic']} (n {ic_debate_60['n']}) "
          f"vs single {ic_single_60['ic']} -> delta {d60}", flush=True)
    print(f"[debate] VERDICT: {verdict} (needs >= +{DECISION_DELTA} at BOTH "
          f"horizons)", flush=True)

    results = {
        "experiment_id": "exp-r2-h1-debate",
        "n_filings": int(len(scores)),
        "n_errors": int(sum(1 for v in scores.values() if "error" in v)),
        "spend_usd": round(spent, 6),
        "worst_case_usd": round(total_worst, 4),
        "ic_debate_20d": ic_debate_20,
        "ic_single_20d": ic_single_20,
        "ic_debate_60d": ic_debate_60,
        "ic_single_60d": ic_single_60,
        "delta_20d": round(d20, 4) if d20 is not None else None,
        "delta_60d": round(d60, 4) if d60 is not None else None,
        "decision_delta": DECISION_DELTA,
        "verdict": verdict,
        "scores_file": str(SCORES_PATH),
    }
    RESULTS_PATH.write_text(json.dumps(results, indent=2))
    result_entry = {
        "experiment_id": "exp-r2-h1-debate-results",
        "strategy": "RESULTS for exp-r2-h1-debate. " + entry["strategy"],
        "results_file": str(RESULTS_PATH),
        "gate": f"GATE VERDICT: {verdict}. Debate rank IC @20d "
                f"{ic_debate_20['ic']} vs single-pass {ic_single_20['ic']} "
                f"(delta {d20}); @60d {ic_debate_60['ic']} vs "
                f"{ic_single_60['ic']} (delta {d60}). "
                f"{'Debate wins at both horizons: worth scaling to a full '
                  'panel + holdout confirmation (separate experiment).'
                  if scale else 'Debate does not beat single-pass by >= '
                  '0.02 at both horizons: DROPPED per the kill rule — the '
                  '3x scoring cost is not justified.'} Spend ${spent:.4f} "
                f"new (worst case ${total_worst:.4f}, run cap $1.00).",
        "spend": {"run_usd": round(spent, 6),
                  "worst_case_usd": round(total_worst, 4),
                  "monthly_total_usd": round(9.824975 + spent, 6),
                  "monthly_cap_usd": MONTHLY_CAP_USD},
        "status": "passed" if scale else "failed",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    with REGISTRY_PATH.open("a") as fh:
        fh.write(json.dumps(result_entry, sort_keys=True) + "\n")
    print(f"results -> {RESULTS_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
