#!/usr/bin/env python3
"""Repair the two bad AMGN entries from exp-equibles-pilot-scoring.

1. AMGN FY2025Q3 was scored on an EMPTY Q&A (the old qa_span found no
   Analyst turn because that older call leaves analyst turns unlabeled).
   Re-score with the fixed span.
2. AMGN FY2025Q4's Haiku reply was truncated at the 500-token cap, so
   _parse_score_json found no closing brace. Retry with max_tokens=800.
   The truncated call was billed but never recorded: its estimated cost
   (input estimate from the actual Q&A text + 500 output tokens, the cap
   it hit) is added to the ledger waste marker.

Rewrites experiments/exp-equibles-pilot-scoring.json in place:
replaces both per-call entries, recomputes aggregates and QoQ deltas,
updates spend/errors. The two repaired scores go through the same
score_qa() path (disk-cached) as the rest of the pilot.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/hatch/workspace/custom-connectors/custom.anthropic/bin")

import run_equibles_pilot_scoring as runner
from llm_sentiment import MODEL_PRICES, SpendCap, _estimate_tokens

REPO = Path(__file__).parent
RAW_DIR = REPO / "experiments" / "equibles-pilot-raw"
OUT = REPO / "experiments" / "exp-equibles-pilot-scoring.json"

CALL_DATES = {(2025, 4): "2026-02-03", (2025, 3): "2025-11-04"}


def qa_text_for(ticker: str, fy: int, fq: int):
    turns = []
    page = 0
    while True:
        fname = RAW_DIR / f"speakers-{ticker}-FY{fy}Q{fq}-p{page}.json"
        payload = json.loads(fname.read_text())
        turns.extend(payload["result"].get("data", []))
        if not payload["result"].get("hasMore"):
            break
        page += 1
    start, end = runner.qa_span(turns)
    text = "\n".join(
        f"[{t.get('speakerRole') or 'Unknown'}"
        f"{' — ' + t['speakerName'] if t.get('speakerName') else ''}]: "
        f"{t.get('text') or ''}" for t in turns[start:end])
    return turns, start, end, text


def main() -> int:
    runner.HAIKU_MAX_OUTPUT_TOKENS = 800
    cap = SpendCap(runner.RUN_CAP_USD, runner.MONTHLY_CAP_USD,
                   REPO / ".cache" / "llm_spend.json", month="2026-11")
    result = json.loads(OUT.read_text())

    # --- waste from the truncated AMGN FY2025Q4 call (run 2) ---
    _, _, _, q4_text = qa_text_for("AMGN", 2025, 4)
    est_in = _estimate_tokens(runner.SYSTEM_PROMPT) + _estimate_tokens(
        f"Ticker: AMGN\nCall date: 2026-02-03\nFiscal: FY2025 Q4 "
        f"(Q&A section only)\n\n{runner._trim_excerpt(q4_text)}")
    waste = round(est_in / 1_000_000 * MODEL_PRICES["claude-haiku-5-5"][0]
                  + 500 / 1_000_000 * MODEL_PRICES["claude-haiku-5-5"][1], 6)
    ledger_path = REPO / ".cache" / "llm_spend.json"
    data = json.loads(ledger_path.read_text())
    data["2026-11"] = round(float(data.get("2026-11", 0.0)) + waste, 6)
    ledger_path.write_text(json.dumps(data, indent=2))
    marker_path = REPO / ".cache" / "equibles_pilot_cache" / "first-run-waste-recorded.json"
    marker = json.loads(marker_path.read_text())
    marker["per_call_est"]["AMGN FY2025Q4 truncated retry (run 2)"] = waste
    marker["waste_usd"] = round(marker["waste_usd"] + waste, 6)
    marker_path.write_text(json.dumps(marker, indent=2))
    print(f"[ledger] truncated Q4 call waste (estimate): ${waste:.6f}", flush=True)

    # --- re-score the two bad calls ---
    repaired = {}
    for fy, fq in [(2025, 3), (2025, 4)]:
        turns, start, end, text = qa_text_for("AMGN", fy, fq)
        scored = runner.score_qa("AMGN", fy, fq, CALL_DATES[(fy, fq)], text, cap)
        repaired[(fy, fq)] = {
            "ticker": "AMGN", "fiscal_year": fy, "fiscal_quarter": fq,
            "call_date": CALL_DATES[(fy, fq)], "n_turns": len(turns),
            "n_qa_turns": len(turns[start:end]),
            "qa_start_turn": start, "qa_end_turn": end,
            "qa_chars": len(text),
            "qa_tokens_est": _estimate_tokens(text),
            **scored,
        }
        print(f"AMGN FY{fy}Q{fq}: qa_turns={end - start} chars={len(text)} "
              f"score={scored['score']:.2f} conf={scored['confidence']:.2f} "
              f"evasion={scored['evasion']:.2f} "
              f"analyst_tone={scored['analyst_tone']:.2f} "
              f"cost=${scored['cost_usd']:.6f}", flush=True)

    # --- rewrite the results file ---
    per_call = [r for r in result["per_call"]
                if not (r["ticker"] == "AMGN" and r["fiscal_year"] == 2025)]
    per_call.append(repaired[(2025, 3)])
    per_call.append(repaired[(2025, 4)])
    per_call.sort(key=lambda r: (r["ticker"], r["call_date"]))
    by_ticker: dict[str, list[dict]] = {}
    for row in per_call:
        by_ticker.setdefault(row["ticker"], []).append(row)
    for ticker, rows in by_ticker.items():
        rows.sort(key=lambda r: r["call_date"])
        prev = None
        for row in rows:
            row["qoq_score_delta"] = (
                None if prev is None else round(row["score"] - prev, 4))
            prev = row["score"]

    def _mean(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    result["per_call"] = per_call
    result["errors"] = []
    result["aggregates"] = {
        "n_calls_scored": len(per_call),
        "n_calls_failed": 0,
        "mean_score": _mean(per_call, "score"),
        "mean_confidence": _mean(per_call, "confidence"),
        "mean_evasion": _mean(per_call, "evasion"),
        "mean_analyst_tone": _mean(per_call, "analyst_tone"),
        "mean_qa_turns": _mean(per_call, "n_qa_turns"),
        "mean_qa_chars": _mean(per_call, "qa_chars"),
        "per_ticker": {
            t: {"n": len(rows), "mean_score": _mean(rows, "score"),
                "mean_evasion": _mean(rows, "evasion"),
                "mean_analyst_tone": _mean(rows, "analyst_tone"),
                "qoq_deltas": [r["qoq_score_delta"] for r in rows
                               if r.get("qoq_score_delta") is not None]}
            for t, rows in sorted(by_ticker.items())
        },
    }
    new_spend = round(sum(r["cost_usd"] for r in per_call), 6)
    result["anthropic"]["spend_usd"] = new_spend
    result["anthropic"]["first_run_waste_usd_estimate"] = round(
        result["anthropic"]["first_run_waste_usd_estimate"] + waste, 6)
    result["anthropic"]["month_spend_after_usd"] = round(
        float(json.loads(ledger_path.read_text())["2026-11"]), 6)
    result["anthropic"]["tokens_in"] = sum(r["tokens_in"] for r in per_call)
    result["anthropic"]["tokens_out"] = sum(r["tokens_out"] for r in per_call)
    result["repair_note"] = (
        "AMGN FY2025Q3 re-scored after the qa_span fix (first run scored an "
        "empty Q&A: 0 turns); AMGN FY2025Q4 re-scored with max_tokens=800 "
        "after the run-2 reply was truncated at the 500-token cap. Both "
        "repairs used the same score_qa() path and prompt version.")
    OUT.write_text(json.dumps(result, indent=2))
    print(f"[done] 21/21 scored; Haiku spend (scored calls) ${new_spend:.6f}; "
          f"ledger 2026-11 now ${result['anthropic']['month_spend_after_usd']:.6f}",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
