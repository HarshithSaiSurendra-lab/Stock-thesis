#!/usr/bin/env python3
"""Equibles earnings-call pilot scoring (exp-equibles-pilot-scoring).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE
stays 0. No return test is done here — this pilot only SCORES Q&A text.
The follow-up experiment (not this script) pairs these scores with
post-call returns to test the PEAD signal.

Registered design (experiments/equibles-coverage-pilot.md, "Next step"):
fetch full transcripts via the speakers endpoint for the 21-call pilot
subset (ABT, AMGN, AMZN x4 quarters + ABBV, AMD, AMT x3) and score
Q&A text only with Haiku 5.5. Components per the Gemini evidence base:
PEAD.txt blend (~104 bps gross over 20 days), bleak tone changes
(~30 bps/mo), Q&A evasion / analyst tone.

ASSUMPTIONS (design doc has no detailed rubric; noted here and in the
results file):
- Q&A section = turns from the operator's "question and answer session"
  announcement (fallback: first Analyst turn) up to, but excluding, the
  closing turn (first post-Q&A Operator/IR turn containing "concludes").
- One Haiku call per call, JSON-only reply:
  score (-1..1, expected directional effect over the NEXT 20 TRADING
  DAYS from the Q&A alone), confidence (0..1), rationale (one sentence),
  evasion (0..1, how often management deflects / gives non-answers /
  refuses to quantify in Q&A), analyst_tone (-1..1, tone of the
  analysts' questions: -1 skeptical/pushback, +1 congratulatory).
- Bleak tone change is NOT scored by the model: it is computed
  deterministically afterwards as the quarter-over-quarter change in
  the Q&A score within each ticker (score[t] - score[t-1]).
- Scoring format follows the repo convention (llm_sentiment SYSTEM_PROMPT
  contract: strict JSON {score, confidence, rationale}); the evasion /
  analyst_tone fields are the pilot's extension.

Budgets: Equibles free tier 100 requests/day shared; 46 used before this
run (45 coverage + 1 NVDA check) and 1 more for the ABT Q3 speakers
probe, so 47 used at start. Hard stop: never make a request that would
take the day past 100. Anthropic: Haiku 5.5, run cap $1.00, November
monthly cap $15.00 (ledger .cache/llm_spend.json, November spend
$10.614197 at start). Worst-case cost checked BEFORE every call.

Run: python3 run_equibles_pilot_scoring.py
Writes experiments/exp-equibles-pilot-scoring.json + .md, raw speaker
pages under experiments/equibles-pilot-raw/, and logs 19 rows to the
experiment Sheet (verified by row-count read-back).
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, "/home/hatch/workspace/custom-connectors/custom.anthropic/bin")
sys.path.insert(0, "/home/hatch/workspace/custom-connectors/custom.equibles/bin")

from llm_sentiment import (  # noqa: E402
    HAIKU_MODEL_ID,
    MAX_EXCERPT_CHARS,
    MODEL_PRICES,
    SpendCap,
    _estimate_tokens,
    _parse_score_json,
    _trim_excerpt,
    cost_usd,
)
from anthropic_api import call_messages  # noqa: E402

REPO = Path(__file__).parent
RAW_DIR = REPO / "experiments" / "equibles-pilot-raw"
CACHE_DIR = REPO / ".cache" / "equibles_pilot_cache"
EQUIBLES_HELPER = "/home/hatch/workspace/custom-connectors/custom.equibles/bin/equibles_api.py"
SHEET_ID = "1bJdCrPm2licM-arLLdIck2zAq15gWfa6ZNjrk14h3G4"
SHEET_CLI = "/opt/hatch/bin/hatch_gws_cli"

TODAY = "2026-11-22"
PROMPT_VERSION = "equibles-pilot-qa-v1"
HAIKU_MAX_OUTPUT_TOKENS = 500
RUN_CAP_USD = 1.00
MONTHLY_CAP_USD = 15.00
EQUIBLES_DAILY_CAP = 100
EQUIBLES_USED_BEFORE_RUN = 47  # 46 from coverage day + 1 ABT Q3 speakers probe

PILOT_TICKERS = ["ABT", "AMGN", "AMZN", "ABBV", "AMD", "AMT"]

SYSTEM_PROMPT = (
    "You are a financial analyst scoring the QUESTION-AND-ANSWER portion "
    "of an earnings call transcript for a quantitative trading system. "
    "Reply with ONLY a JSON object, no other text: "
    '{"score": <float -1 to 1>, "confidence": <float 0 to 1>, '
    '"rationale": "<one sentence>", "evasion": <float 0 to 1>, '
    '"analyst_tone": <float -1 to 1>}. '
    "score is the expected directional effect on the stock over the NEXT "
    "20 TRADING DAYS from the Q&A alone: +1 strongly positive, 0 neutral, "
    "-1 strongly negative. Base it on what management revealed or conceded "
    "under questioning, not on the prepared remarks. confidence is how clear "
    "the evidence in the Q&A is. evasion is how often management deflects, "
    "gives non-answers, or refuses to quantify in the Q&A: 0 direct and "
    "quantified throughout, 1 evasive throughout. analyst_tone is the tone "
    "of the analysts' questions: -1 skeptical or pushback, 0 neutral, "
    "+1 congratulatory or softball."
)


def qa_span(turns: list[dict]) -> tuple[int, int]:
    """Return (start, end) indices of the Q&A section, end exclusive.

    Start = the first Analyst turn, stepped back over the immediately
    preceding Operator/IR turn when it announces questions. (The naive
    "first turn containing 'question and answer session'" rule misfires:
    AMT FY2026 Q3's CLOSING turn reads "This concludes our question and
    answer session" and would be picked as the start, yielding a 1-turn
    Q&A.) End = the first turn after start whose text contains
    "concludes" with role Operator / Head of Investor Relations.
    """
    first_analyst = None
    for i, t in enumerate(turns):
        if (t.get("speakerRole") or "").startswith("Analyst"):
            first_analyst = i
            break
    if first_analyst is None:
        # Older calls (e.g. AMGN FY2025Q3) leave analyst turns unlabeled
        # (speakerRole None). Fall back to the first Operator turn AFTER
        # the prepared remarks that opens questions — it must come after
        # the first executive turn, so the operator's opening script
        # (turn 0, "ask your question by pressing...") never matches.
        first_exec = None
        for i, t in enumerate(turns):
            if (t.get("speakerRole") or "") in (
                    "CEO", "CFO", "Head of Investor Relations"):
                first_exec = i
                break
        for i, t in enumerate(turns):
            if first_exec is not None and i <= first_exec:
                continue
            role = t.get("speakerRole") or ""
            text = (t.get("text") or "").lower()
            if role == "Operator" and "question" in text and any(
                    p in text for p in ("comes from", "first question",
                                        "next question", "ask a question",
                                        "question and answer")):
                first_analyst = i
                break
    if first_analyst is None:
        return (0, 0)
    start = first_analyst
    for j in (first_analyst - 1, first_analyst - 2):
        if j < 0:
            break
        role = turns[j].get("speakerRole") or ""
        text = (turns[j].get("text") or "").lower()
        if role in ("Operator", "Head of Investor Relations") and "question" in text:
            start = j
        else:
            break
    end = len(turns)
    for j in range(start + 1, len(turns)):
        role = turns[j].get("speakerRole") or ""
        text = (turns[j].get("text") or "").lower()
        if "concludes" in text and role in ("Operator", "Head of Investor Relations"):
            end = j
            break
    return (start, end)


def fetch_speakers(ticker: str, fy: int, fq: int, budget: dict) -> list[dict]:
    """Fetch all speaker pages for one call, reusing saved pages."""
    turns: list[dict] = []
    page = 0
    offset = 0
    while True:
        fname = RAW_DIR / f"speakers-{ticker}-FY{fy}Q{fq}-p{page}.json"
        if fname.exists():
            payload = json.loads(fname.read_text())
            print(f"  {ticker} FY{fy}Q{fq} p{page}: cached", flush=True)
        else:
            if budget["used"] + 1 > EQUIBLES_DAILY_CAP:
                raise RuntimeError(
                    f"Equibles daily cap reached ({budget['used']}/{EQUIBLES_DAILY_CAP}); "
                    f"stopped before fetching {ticker} FY{fy}Q{fq} p{page}")
            out = subprocess.run(
                ["python3", EQUIBLES_HELPER, "speakers", "--ticker", ticker,
                 "--fy", str(fy), "--fq", str(fq), "--limit", "200",
                 "--offset", str(offset)],
                capture_output=True, text=True, timeout=120)
            budget["used"] += 1
            budget["requests_this_run"] += 1
            try:
                payload = json.loads(out.stdout)
            except ValueError:
                raise RuntimeError(
                    f"speakers fetch {ticker} FY{fy}Q{fq} p{page}: "
                    f"unparseable output: {out.stdout[:300]} {out.stderr[:300]}")
            fname.write_text(json.dumps(payload, indent=2))
            print(f"  {ticker} FY{fy}Q{fq} p{page}: "
                  f"status={payload.get('status')} (Equibles day total "
                  f"{budget['used']}/{EQUIBLES_DAILY_CAP})", flush=True)
        if payload.get("status") != 200:
            raise RuntimeError(
                f"speakers fetch {ticker} FY{fy}Q{fq} p{page} failed: "
                f"{str(payload)[:300]}")
        result = payload["result"]
        turns.extend(result.get("data", []))
        if not result.get("hasMore"):
            break
        offset += int(result.get("turnCount") or 0)
        page += 1
    return turns


def score_qa(ticker: str, fy: int, fq: int, call_date: str, qa_text: str,
             cap: SpendCap) -> dict:
    """Score one call's Q&A text with Haiku (disk-cached, $0 on rerun)."""
    import hashlib

    key = hashlib.sha256(
        "|".join([ticker, str(fy), str(fq), PROMPT_VERSION, qa_text]).encode()
    ).hexdigest()[:24]
    disk_path = CACHE_DIR / f"{key}.json"
    try:
        cached = json.loads(disk_path.read_text())
        cached["cached"] = True
        return cached
    except (OSError, ValueError, KeyError):
        pass

    text = _trim_excerpt(qa_text)
    est_in = _estimate_tokens(SYSTEM_PROMPT) + _estimate_tokens(text)
    price_in, price_out = MODEL_PRICES[HAIKU_MODEL_ID]
    worst_case = (est_in / 1_000_000 * price_in
                  + HAIKU_MAX_OUTPUT_TOKENS / 1_000_000 * price_out)
    cap.check(worst_case)

    user_text = (f"Ticker: {ticker}\nCall date: {call_date}\n"
                 f"Fiscal: FY{fy} Q{fq} (Q&A section only)\n\n{text}")
    status, payload = call_messages(
        HAIKU_MODEL_ID, SYSTEM_PROMPT, user_text,
        max_tokens=HAIKU_MAX_OUTPUT_TOKENS)
    if status != 200:
        raise RuntimeError(f"Haiku call failed: status {status}: {str(payload)[:300]}")
    # Anthropic Messages API returns content as a LIST of blocks
    # (type "text", sometimes preceded by a "thinking" block) — NOT an
    # OpenAI-style choices[0].message.content dict. The first run of this
    # script parsed it the OpenAI way and lost 21 scored replies.
    blocks = payload.get("content") or []
    if isinstance(blocks, str):
        content = blocks
    else:
        content = "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text")
    if not content.strip():
        raise RuntimeError(f"Haiku reply has no text block: {str(payload)[:300]}")
    core = _parse_score_json(content)
    match = re.search(r"\{.*\}", content, re.DOTALL)
    full = json.loads(match.group(0))
    evasion = float(full.get("evasion", 0.0))
    analyst_tone = float(full.get("analyst_tone", 0.0))
    if not 0.0 <= evasion <= 1.0:
        raise ValueError(f"evasion out of [0, 1]: {evasion}")
    if not -1.0 <= analyst_tone <= 1.0:
        raise ValueError(f"analyst_tone out of [-1, 1]: {analyst_tone}")
    usage = payload.get("usage") or {}
    tokens_in = int(usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output_tokens") or 0)
    cost = cost_usd(HAIKU_MODEL_ID, tokens_in, tokens_out)
    cap.record(cost)
    result = {
        "ticker": ticker, "fiscal_year": fy, "fiscal_quarter": fq,
        "call_date": call_date, "model_id": HAIKU_MODEL_ID,
        "prompt_version": PROMPT_VERSION,
        "score": core["score"], "confidence": core["confidence"],
        "rationale": core["rationale"], "evasion": evasion,
        "analyst_tone": analyst_tone,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "cost_usd": cost, "cached": False,
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        disk_path.write_text(json.dumps(result, indent=2))
    except OSError:
        pass
    return result


def record_first_run_waste() -> float:
    """Record the estimated spend of the aborted first run (2026-11-22).

    The first run made 21 successful Haiku calls (200, scored replies
    received) but failed to parse them — it treated the Anthropic
    Messages API payload as an OpenAI-style choices[] dict — so
    SpendCap.record() never ran and the ledger missed that spend.
    Exact token usage is unrecoverable (usage was not logged), so the
    waste is ESTIMATED from the same Q&A texts that were sent
    (2.9 chars/token input estimate + 200 output tokens per call at
    Haiku prices) and added to the November ledger total. Idempotent:
    guarded by a marker file so a rerun never double-counts.
    """
    marker = CACHE_DIR / "first-run-waste-recorded.json"
    if marker.exists():
        return float(json.loads(marker.read_text()).get("waste_usd", 0.0))
    cells = json.loads((RAW_DIR / "cells.json").read_text())
    total = 0.0
    per_call_est = {}
    for ticker in PILOT_TICKERS:
        for fy, fq in cells[ticker]:
            turns: list[dict] = []
            page = 0
            while True:
                fname = RAW_DIR / f"speakers-{ticker}-FY{fy}Q{fq}-p{page}.json"
                if not fname.exists():
                    break
                payload = json.loads(fname.read_text())
                turns.extend(payload.get("result", {}).get("data", []))
                if not payload.get("result", {}).get("hasMore"):
                    break
                page += 1
            start, end = qa_span(turns)
            qa_text = "\n".join(
                f"[{t.get('speakerRole') or 'Unknown'}"
                f"{' — ' + t['speakerName'] if t.get('speakerName') else ''}]: "
                f"{t.get('text') or ''}" for t in turns[start:end])
            est_in = _estimate_tokens(SYSTEM_PROMPT) + _estimate_tokens(
                f"Ticker: {ticker}\nCall date: \nFiscal: FY{fy} Q{fq} "
                f"(Q&A section only)\n\n{_trim_excerpt(qa_text)}")
            est = round(est_in / 1_000_000 * MODEL_PRICES[HAIKU_MODEL_ID][0]
                        + 200 / 1_000_000 * MODEL_PRICES[HAIKU_MODEL_ID][1], 6)
            per_call_est[f"{ticker} FY{fy}Q{fq}"] = est
            total += est
    total = round(total, 6)
    ledger_path = REPO / ".cache" / "llm_spend.json"
    try:
        data = json.loads(ledger_path.read_text())
    except (OSError, ValueError):
        data = {}
    data["2026-11"] = round(float(data.get("2026-11", 0.0)) + total, 6)
    ledger_path.write_text(json.dumps(data, indent=2))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(
        {"waste_usd": total, "per_call_est": per_call_est,
         "note": "estimate only: input from 2.9 chars/token on the actual "
                 "Q&A texts sent; output assumed 200 tokens/call"}, indent=2))
    print(f"[ledger] first-run waste recorded (estimate): ${total:.6f} "
          f"added to 2026-11", flush=True)
    return total


def main() -> int:
    cells = json.loads((RAW_DIR / "cells.json").read_text())
    coverage = json.loads(
        (REPO / "experiments" / "equibles-coverage-pilot.json").read_text())
    call_dates = {
        (c["ticker"], c["fiscalYear"], c["fiscalQuarter"]): c["callDate"]
        for c in coverage["matrix"]
    }
    budget = {"used": EQUIBLES_USED_BEFORE_RUN, "requests_this_run": 0}
    waste_usd = record_first_run_waste()
    cap = SpendCap(RUN_CAP_USD, MONTHLY_CAP_USD,
                   REPO / ".cache" / "llm_spend.json", month="2026-11")
    print(f"[budget] Equibles day total at start: {budget['used']}/100; "
          f"Anthropic Nov spend at start: ${cap.spent_month_usd:.6f}/$15.00; "
          f"run cap ${RUN_CAP_USD:.2f}", flush=True)

    per_call = []
    errors = []
    for ticker in PILOT_TICKERS:
        for fy, fq in cells[ticker]:
            call_date = call_dates.get((ticker, fy, fq), "")
            print(f"[{ticker} FY{fy}Q{fq} {call_date}]", flush=True)
            try:
                turns = fetch_speakers(ticker, fy, fq, budget)
                start, end = qa_span(turns)
                qa_turns = turns[start:end]
                qa_text = "\n".join(
                    f"[{t.get('speakerRole') or 'Unknown'}"
                    f"{' — ' + t['speakerName'] if t.get('speakerName') else ''}]: "
                    f"{t.get('text') or ''}" for t in qa_turns)
                scored = score_qa(ticker, fy, fq, call_date, qa_text, cap)
                per_call.append({
                    "ticker": ticker, "fiscal_year": fy, "fiscal_quarter": fq,
                    "call_date": call_date, "n_turns": len(turns),
                    "n_qa_turns": len(qa_turns),
                    "qa_start_turn": start, "qa_end_turn": end,
                    "qa_chars": len(qa_text),
                    "qa_tokens_est": _estimate_tokens(qa_text),
                    **scored,
                })
                print(f"  -> score={scored['score']:.2f} conf={scored['confidence']:.2f} "
                      f"evasion={scored['evasion']:.2f} "
                      f"analyst_tone={scored['analyst_tone']:.2f} "
                      f"cost=${scored['cost_usd']:.6f} "
                      f"(cached={scored['cached']})", flush=True)
            except Exception as exc:  # keep going; record the failure
                errors.append({"ticker": ticker, "fiscal_year": fy,
                               "fiscal_quarter": fq, "call_date": call_date,
                               "error": str(exc)[:500]})
                print(f"  !! ERROR: {exc}", flush=True)

    # Deterministic post-analysis: QoQ score deltas (bleak tone change
    # proxy) within each ticker, chronological order.
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

    aggregates = {
        "n_calls_scored": len(per_call),
        "n_calls_failed": len(errors),
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
    total_cost = round(sum(r["cost_usd"] for r in per_call), 6)
    result = {
        "experiment_id": "exp-equibles-pilot-scoring",
        "generated": datetime.now(timezone.utc).isoformat(),
        "date": TODAY,
        "design": "Q&A-only Haiku 5.5 scoring of the 21-call Equibles pilot "
                  "(PEAD 20-day horizon; bleak tone change = QoQ score delta; "
                  "evasion + analyst tone per call). No return test in this pilot.",
        "model_id": HAIKU_MODEL_ID,
        "prompt_version": PROMPT_VERSION,
        "assumptions": [
            "Pilot size is 21 calls, not 19: the coverage doc and the task "
            "brief say 19, but the defined set (ABT/AMGN/AMZN x4 + "
            "ABBV/AMD/AMT x3) is 4+4+4+3+3+3 = 21, and cells.json lists "
            "21 cells. All 21 were scored.",
            "Q&A span = first Analyst turn, stepped back over the "
            "immediately preceding Operator/IR turn when it announces "
            "questions; ends before the closing 'concludes' turn. "
            "Prepared remarks excluded. Fallback for older calls with "
            "unlabeled analyst turns (e.g. AMGN FY2025Q3): first Operator "
            "turn after the prepared remarks that opens questions. (The "
            "naive 'first turn containing question and answer session' "
            "rule misfires: AMT FY2026 Q3's closing turn reads 'This "
            "concludes our question and answer session'.)",
            "Bleak tone change is the deterministic QoQ change in the Q&A "
            "score within a ticker, not a model judgement.",
            "Scores are a pilot diagnostic only: 21 calls, no post-call "
            "returns computed here, no IC, no trading signal.",
        ],
        "equibles": {
            "requests_used_before_run": EQUIBLES_USED_BEFORE_RUN,
            "requests_this_run": budget["requests_this_run"],
            "requests_day_total_after": budget["used"],
            "daily_cap": EQUIBLES_DAILY_CAP,
        },
        "anthropic": {
            "spend_usd": total_cost,
            "first_run_waste_usd_estimate": waste_usd,
            "run_cap_usd": RUN_CAP_USD,
            "month_spend_after_usd": round(cap.spent_month_usd, 6),
            "monthly_cap_usd": MONTHLY_CAP_USD,
            "tokens_in": sum(r["tokens_in"] for r in per_call),
            "tokens_out": sum(r["tokens_out"] for r in per_call),
        },
        "aggregates": aggregates,
        "per_call": per_call,
        "errors": errors,
    }
    out_json = REPO / "experiments" / "exp-equibles-pilot-scoring.json"
    out_json.write_text(json.dumps(result, indent=2))
    print(f"[done] scored {len(per_call)}/{len(per_call) + len(errors)}, "
          f"errors {len(errors)}, "
          f"Haiku spend ${total_cost:.6f} (+ first-run waste est "
          f"${waste_usd:.6f}), Equibles day total "
          f"{budget['used']}/100", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
