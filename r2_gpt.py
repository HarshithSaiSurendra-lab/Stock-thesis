#!/usr/bin/env python3
"""Round-2 GPT leg: GPT-6.1 Sol second opinion on the adjudication subset.

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE
stays 0. Scores the Phase-2 Sonnet adjudication subset (104 train
filings, most decision-relevant first) with gpt-6.1-sol using the SAME
excerpts and the SAME JSON score contract as the Haiku screen, so
agreement can be measured CROSS-FAMILY (Claude vs OpenAI) instead of
within one Claude family (the recorded Phase 2 caveat).

Budget: $5 OpenAI credit; run cap $4.50, monthly cap $5.00. The
subset is sized BEFORE scoring by worst-case accounting (input at
~2.9 chars/token — conservative for OpenAI tokenization, which runs
closer to ~4 chars/token — plus 300 max output tokens at the repo's
recorded gpt-6.1-sol prices $2.00/$10.00 per MTok; pricing was NOT
verified from a price list, so token counts are the ground truth and
are logged per call). The provider's cap check runs before every call;
scoring is resume-safe (disk cache + incremental saves). The $5
balance is not readable via API (credit-grants is 403 for secret
keys); a successful chat call is the practical proof credits exist.

Analysis (diagnostic, not a portfolio experiment — no Sheet rows):
cross-family agreement (Haiku vs GPT, Sonnet vs GPT), pooled rank IC
@20d/@60d for GPT vs Haiku on the same filings, and which side GPT
takes where Haiku and Sonnet disagree in sign.

Run AFTER the H1 debate: python3 r2_gpt.py
Writes experiments/results/exp-r2-gpt-second-opinion-scores.json and
experiments/results/exp-r2-gpt-second-opinion.json.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from gpt_sentiment import GPT_MAX_OUTPUT_TOKENS, GPTProvider
from llm_sentiment import MODEL_PRICES, _estimate_tokens
from r2_debate import pooled_rank_ic
import run_llm_backtest as p2

REPO = Path(__file__).parent
REGISTRY_PATH = REPO / "experiments" / "registry.jsonl"
SONNET_SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-sonnet.json"
HAIKU_SCORES_PATH = REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json"
SCORES_PATH = REPO / "experiments" / "results" / "exp-r2-gpt-second-opinion-scores.json"
RESULTS_PATH = REPO / "experiments" / "results" / "exp-r2-gpt-second-opinion.json"

RUN_CAP_USD = 4.50
MONTHLY_CAP_USD = 5.00
GPT_MODEL = "gpt-6.1-sol"
DECISION_DELTA = 0.02  # same bar shape as the H1 debate (diagnostic only)


def worst_case_filing_usd(excerpt_chars: int) -> float:
    """Worst case for one GPT scoring call (repo-recorded prices)."""
    est_in = _estimate_tokens("x" * min(excerpt_chars, 160_000))
    # system prompt ~700 chars, counted the same way
    est_in += _estimate_tokens("x" * 700)
    price_in, price_out = MODEL_PRICES[GPT_MODEL]
    return (est_in / 1_000_000 * price_in
            + GPT_MAX_OUTPUT_TOKENS / 1_000_000 * price_out)


def agreement(a: pd.Series, b: pd.Series) -> dict:
    """Cross-family agreement between two score series (same filings)."""
    both = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(both) < 5:
        return {"n": int(len(both)), "mean_agreement": None,
                "mean_abs_diff": None, "pearson": None, "sign_agreement": None}
    diff = (both["a"] - both["b"]).abs()
    agree = 1.0 - diff / 2.0
    return {
        "n": int(len(both)),
        "mean_agreement": round(float(agree.mean()), 4),
        "mean_abs_diff": round(float(diff.mean()), 4),
        "pearson": round(float(both["a"].corr(both["b"], method="pearson")), 4),
        "sign_agreement": round(float(((both["a"] > 0) == (both["b"] > 0)).mean()), 4),
    }


def main() -> int:
    print("=== Round-2 GPT leg: gpt-6.1-sol second opinion ===", flush=True)
    sonnet = json.loads(SONNET_SCORES_PATH.read_text())
    haiku = json.loads(HAIKU_SCORES_PATH.read_text())
    subset_accessions = [a for a in sonnet if "error" not in sonnet[a]]
    print(f"adjudication subset: {len(subset_accessions)} filings "
          f"(Phase-2 Sonnet subset, rank order)", flush=True)
    filings = p2.load_edgar_filings()
    by_acc = filings.set_index("accession")
    missing = [a for a in subset_accessions if a not in by_acc.index]
    if missing:
        raise RuntimeError(f"{len(missing)} subset accessions missing from EDGAR shards")
    subset = by_acc.loc[subset_accessions].reset_index()

    # --- Size the subset BEFORE scoring (worst-case accounting).
    # Effective budget: the run cap, but never more than the monthly
    # headroom left in the ledger (a resume after a crash must not
    # re-spend the already-spent amount).
    ledger_path = REPO / ".cache" / "openai_spend.json"
    spent_month = 0.0
    try:
        ledger = json.loads(ledger_path.read_text())
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        spent_month = float(ledger.get(month_key, 0.0))
    except (OSError, ValueError, TypeError):
        spent_month = 0.0
    effective_cap = min(RUN_CAP_USD, MONTHLY_CAP_USD - spent_month)
    print(f"budget: already spent this month ${spent_month:.4f}; "
          f"effective cap ${effective_cap:.2f} (run ${RUN_CAP_USD:.2f}, "
          f"monthly headroom ${MONTHLY_CAP_USD - spent_month:.2f})", flush=True)
    subset = subset.assign(
        worst_case=subset["excerpt_chars"].map(worst_case_filing_usd))
    total_worst = float(subset["worst_case"].sum())
    trimmed = 0
    while total_worst > effective_cap and len(subset) > 1:
        drop_idx = subset["worst_case"].idxmax()
        total_worst -= float(subset.at[drop_idx, "worst_case"])
        subset = subset.drop(index=drop_idx)
        trimmed += 1
    print(f"pre-budget: worst-case total ${total_worst:.4f} for "
          f"{len(subset)} filings (effective cap ${effective_cap:.2f}); "
          f"trimmed {trimmed} longest filings", flush=True)
    if total_worst > effective_cap:
        print("STOP: GPT leg does not fit the budget; no calls made.", flush=True)
        return 2

    # --- Register BEFORE scoring (idempotent: skip on a resume).
    # The strategy text is defined once so the results entry below can
    # reference it on a resume too (where `entry` is never built).
    strategy_text = ("GPT-6.1 Sol second opinion on the Phase-2 Sonnet "
                     "adjudication subset (train filings, most "
                     "decision-relevant first): same excerpts and same JSON "
                     "score contract as the Haiku screen, scored by an "
                     "independent model family (OpenAI) so agreement can "
                     "be measured cross-family.")
    already_registered = False
    try:
        for line in REGISTRY_PATH.read_text().splitlines():
            if json.loads(line).get("experiment_id") == "exp-r2-gpt-second-opinion":
                already_registered = True
                break
    except (OSError, ValueError):
        already_registered = False
    if already_registered:
        print("already registered exp-r2-gpt-second-opinion (resume); "
              "not re-registering", flush=True)
    else:
        entry = {
            "experiment_id": "exp-r2-gpt-second-opinion",
            "strategy": strategy_text,
            "hypothesis": "A second opinion from an independent model family "
                          "agrees with the Claude-family scores where the "
                          "signal is real and disagrees where it is noise; "
                          "cross-family agreement is the missing Leg 2 "
                          "diagnostic from Phase 2 (whose agreement was "
                          "within one Claude family).",
            "holdout_rule": "No holdout use: scoring-quality diagnostic on "
                            "TRAIN filings only. A GPT score that wins here "
                            "would still need a full panel + holdout "
                            "confirmation before use.",
            "horizon": "filing date -> forward return +1..+1+20d and +1..+1+60d (IC diagnostic only)",
            "benchmark": "Haiku 5.5 single-pass scores and Sonnet 5.5 adjudications on the same filings",
            "cost_model": {"worst_case_usd": round(total_worst, 4),
                           "run_cap_usd": RUN_CAP_USD,
                           "monthly_cap_usd": MONTHLY_CAP_USD,
                           "n_filings": int(len(subset)),
                           "n_trimmed_for_budget": trimmed,
                           "prices_usd_per_mtok": {"input": 2.0, "output": 10.0},
                           "prices_note": "gpt-6.1-sol pricing NOT verified from a price list; token counts are ground truth"},
            "design_doc": "experiments/exp-llm-long-horizon-r2-design.md",
            "exit_gate": "Diagnostic, not a portfolio gate. Report: "
                         "cross-family agreement (Haiku vs GPT, Sonnet vs "
                         "GPT), pooled rank IC @20d/@60d for GPT vs Haiku on "
                         "the same filings, and which side GPT takes where "
                         "Haiku and Sonnet disagree in sign. No DROP/SCALE "
                         "verdict on the leg itself.",
            "parameters": {"model": GPT_MODEL, "max_output_tokens": GPT_MAX_OUTPUT_TOKENS,
                           "subset": "Phase-2 Sonnet adjudication subset (104 train filings, rank order)",
                           "cost_estimation": "~2.9 chars/token (conservative for OpenAI), 300-token max output worst case"},
            "limits": ["Backtest only; no paper trading, no live money.",
                       "Pricing not verified from a price list; ledger USD amounts use the repo's recorded (2.0, 10.0).",
                       "Credit balance not readable via API (403); a successful chat call is the credit proof.",
                       "GPT is a second opinion on the adjudication subset only — not a full-panel rescore."],
            "spend": {"openai_credit_usd": 5.00, "run_cap_usd": RUN_CAP_USD,
                      "monthly_cap_usd": MONTHLY_CAP_USD,
                      "smoke_call_note": "73-token smoke call Nov 22 ~02:55 EST (~$0.001 at recorded prices) is not in the ledger; immaterial."},
            "status": "registered",
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        with REGISTRY_PATH.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        print("registered exp-r2-gpt-second-opinion (before scoring)", flush=True)

    # --- Score the subset (resume-safe; cached calls cost $0).
    provider = GPTProvider(run_cap_usd=RUN_CAP_USD, monthly_cap_usd=MONTHLY_CAP_USD)
    scores = {}
    if SCORES_PATH.exists():
        try:
            scores = json.loads(SCORES_PATH.read_text())
        except (OSError, ValueError):
            scores = {}
    print(f"resuming: {len(scores)} GPT scores already on disk", flush=True)
    spent = 0.0
    n_new = n_cached = n_failed = 0
    tokens_in_total = tokens_out_total = 0
    first_probe = None
    for i, (_, filing) in enumerate(subset.iterrows()):
        acc = filing["accession"]
        if acc in scores and "error" not in scores[acc]:
            continue
        docs = [{"doc": filing["form"], "section": "MD&A + Risk Factors",
                 "date": filing["filing_date"].strftime("%Y-%m-%d")}]
        as_of = filing["filing_date"].strftime("%Y-%m-%d")
        try:
            result = provider.score(filing["ticker"], as_of, docs,
                                    filing["excerpt_text"])
        except Exception as exc:  # noqa: BLE001 - record and continue
            n_failed += 1
            scores[acc] = {"error": str(exc), "ticker": filing["ticker"],
                           "filing_date": as_of}
            print(f"[gpt] FAILED {acc} ({filing['ticker']}): {exc}", flush=True)
            if n_failed >= 10:
                raise RuntimeError("GPT circuit breaker: 10 failures; aborting")
            continue
        if first_probe is None and not result.get("cached"):
            first_probe = {
                "accession": acc,
                "excerpt_chars": int(filing["excerpt_chars"]),
                "prompt_tokens": result["tokens_in"],
                "completion_tokens": result["tokens_out"],
                "cost_usd": result["cost_usd"],
                "chars_per_token": round(int(filing["excerpt_chars"]) / max(result["tokens_in"], 1), 2),
            }
        if not result.get("cached"):
            spent += float(result.get("cost_usd", 0.0))
            tokens_in_total += int(result.get("tokens_in", 0))
            tokens_out_total += int(result.get("tokens_out", 0))
            n_new += 1
        else:
            n_cached += 1
        scores[acc] = {
            "ticker": filing["ticker"],
            "filing_date": as_of,
            "form": filing["form"],
            "score": result["score"],
            "confidence": result["confidence"],
            "rationale": result["rationale"],
            "tokens_in": result["tokens_in"],
            "tokens_out": result["tokens_out"],
            "cost_usd": result["cost_usd"],
            "haiku_score": float(haiku[acc]["score"]) if acc in haiku else None,
            "sonnet_score": float(sonnet[acc]["score"]),
        }
        # Save after EVERY filing (not every 25): a service restart
        # must not lose the in-memory scores dict (the provider disk
        # cache makes re-scoring $0, but the dict itself is the record).
        SCORES_PATH.write_text(json.dumps(scores, indent=2))
        if (i + 1) % 25 == 0:
            print(f"[gpt] {i + 1}/{len(subset)} filings, new spend "
                  f"${spent:.4f} ({n_new} new, {n_cached} cached, "
                  f"{n_failed} failed)", flush=True)
        time.sleep(0.2)  # gentle pacing
    SCORES_PATH.write_text(json.dumps(scores, indent=2))
    print(f"[gpt] done: {len(scores)} filings, new spend ${spent:.4f} "
          f"({n_new} new calls, {n_cached} cached, {n_failed} failed); "
          f"tokens in {tokens_in_total}, out {tokens_out_total}", flush=True)
    if first_probe:
        print(f"[gpt] probe (first live filing): {first_probe}", flush=True)

    # --- Analysis: cross-family agreement + pooled rank IC.
    import r2_panels as r2
    d = r2.load_r2_data()
    close = d["prices"].loc[d["train_days"]]
    idx = close.index
    gpt_s, haiku_s, sonnet_s, fwd20, fwd60 = [], [], [], [], []
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
        gpt_s.append(rec["score"])
        haiku_s.append(rec["haiku_score"])
        sonnet_s.append(rec["sonnet_score"])
        fwd20.append(f20)
        fwd60.append(f60)
    gpt_s = pd.Series(gpt_s, dtype=float)
    haiku_s = pd.Series(haiku_s, dtype=float)
    sonnet_s = pd.Series(sonnet_s, dtype=float)
    agree_hg = agreement(haiku_s, gpt_s)
    agree_sg = agreement(sonnet_s, gpt_s)
    ic_gpt_20 = pooled_rank_ic(gpt_s, pd.Series(fwd20, dtype=float))
    ic_haiku_20 = pooled_rank_ic(haiku_s, pd.Series(fwd20, dtype=float))
    ic_gpt_60 = pooled_rank_ic(gpt_s, pd.Series(fwd60, dtype=float))
    ic_haiku_60 = pooled_rank_ic(haiku_s, pd.Series(fwd60, dtype=float))
    # where Haiku and Sonnet disagree in sign, which side does GPT take?
    disagree = (haiku_s > 0) != (sonnet_s > 0)
    sided = {"n_disagree": int(disagree.sum()), "gpt_sides_with_sonnet": None,
             "gpt_sides_with_haiku": None}
    if disagree.sum() >= 5:
        g = gpt_s[disagree]
        s = sonnet_s[disagree]
        h = haiku_s[disagree]
        sided["gpt_sides_with_sonnet"] = int(((g > 0) == (s > 0)).sum())
        sided["gpt_sides_with_haiku"] = int(((g > 0) == (h > 0)).sum())
    print(f"[gpt] agreement Haiku vs GPT: {agree_hg}", flush=True)
    print(f"[gpt] agreement Sonnet vs GPT: {agree_sg}", flush=True)
    print(f"[gpt] IC @20d: GPT {ic_gpt_20['ic']} vs Haiku {ic_haiku_20['ic']}", flush=True)
    print(f"[gpt] IC @60d: GPT {ic_gpt_60['ic']} vs Haiku {ic_haiku_60['ic']}", flush=True)
    print(f"[gpt] sign-disagreement split: {sided}", flush=True)

    results = {
        "experiment_id": "exp-r2-gpt-second-opinion",
        "n_filings": int(len(scores)),
        "n_errors": int(sum(1 for v in scores.values() if "error" in v)),
        "n_trimmed_for_budget": trimmed,
        "spend_usd": round(spent, 6),
        "worst_case_usd": round(total_worst, 4),
        "tokens_in": tokens_in_total,
        "tokens_out": tokens_out_total,
        "first_probe": first_probe,
        "agreement_haiku_vs_gpt": agree_hg,
        "agreement_sonnet_vs_gpt": agree_sg,
        "ic_gpt_20d": ic_gpt_20,
        "ic_haiku_20d": ic_haiku_20,
        "ic_gpt_60d": ic_gpt_60,
        "ic_haiku_60d": ic_haiku_60,
        "sign_disagreement_split": sided,
        "scores_file": str(SCORES_PATH),
    }
    RESULTS_PATH.write_text(json.dumps(results, indent=2))
    result_entry = {
        "experiment_id": "exp-r2-gpt-second-opinion-results",
        "strategy": "RESULTS for exp-r2-gpt-second-opinion. " + strategy_text,
        "results_file": str(RESULTS_PATH),
        "gate": (f"DIAGNOSTIC (no portfolio gate). Cross-family agreement: "
                 f"Haiku vs GPT mean agreement {agree_hg['mean_agreement']} "
                 f"(Pearson {agree_hg['pearson']}, sign agreement "
                 f"{agree_hg['sign_agreement']}); Sonnet vs GPT mean "
                 f"agreement {agree_sg['mean_agreement']} (Pearson "
                 f"{agree_sg['pearson']}). Pooled rank IC @20d: GPT "
                 f"{ic_gpt_20['ic']} vs Haiku {ic_haiku_20['ic']}; @60d: GPT "
                 f"{ic_gpt_60['ic']} vs Haiku {ic_haiku_60['ic']}. "
                 f"Sign-disagreement split: {sided}. Spend ${spent:.4f} new "
                 f"(worst case ${total_worst:.4f}, run cap $4.50)."),
        "spend": {"run_usd": round(spent, 6),
                  "worst_case_usd": round(total_worst, 4),
                  "monthly_cap_usd": MONTHLY_CAP_USD,
                  "tokens_in": tokens_in_total,
                  "tokens_out": tokens_out_total},
        "status": "passed",
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    with REGISTRY_PATH.open("a") as fh:
        fh.write(json.dumps(result_entry, sort_keys=True) + "\n")
    print(f"results -> {RESULTS_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
