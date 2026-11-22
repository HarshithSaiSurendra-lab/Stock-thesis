# Equibles Earnings-Call Pilot Scoring — Results

Generated 2026-11-22. Machine-readable: `exp-equibles-pilot-scoring.json`.
Runner: `run_equibles_pilot_scoring.py` (repairs: `repair_amgn_pilot.py`).
Raw transcripts: `experiments/equibles-pilot-raw/speakers-*.json`.

## What was scored

21 earnings calls (the coverage doc and brief say 19; the defined set —
ABT/AMGN/AMZN × 4 quarters + ABBV/AMD/AMT × 3 — is 4+4+4+3+3+3 = 21,
and `cells.json` lists 21 cells; all 21 were scored). Q&A text ONLY,
prepared remarks excluded, one Haiku 5.5 call per call
(prompt `equibles-pilot-qa-v1`). Per call: score (−1..+1, expected
directional effect over the next 20 trading days from the Q&A alone),
confidence (0..1), evasion (0..1, management deflection in Q&A),
analyst tone (−1..+1, tone of analysts' questions). Bleak tone change is
the deterministic quarter-over-quarter change in the Q&A score within
each ticker — not a model judgement.

## Headline numbers

- Mean Q&A score **+0.20** (mildly positive), mean confidence 0.62,
  mean evasion 0.49, mean analyst tone −0.03 (roughly neutral).
- Per ticker (mean score): AMD **+0.50**, ABBV +0.40, AMZN +0.40,
  AMGN +0.06, AMT 0.00, ABT **−0.08**.
- Largest QoQ moves (bleak tone change proxy): ABT swings −0.55 / +0.55 /
  −0.55 across its four quarters; AMGN drops −0.50 into FY2025 Q4 (the
  quarter management called the FDA's Tavneos withdrawal request a
  surprise), then +0.35 / +0.25 back.
- Most evasive Q&A: ABT FY2026 Q3 (0.70, guidance cut under questioning);
  most direct: AMZN FY2026 Q2 (0.30). Most skeptical analysts: ABT
  (−0.23 mean tone).

## Spend and requests

- Equibles: 20 new requests in this run (all speakers pages, 1 per call);
  day total 67/100. Re-runs and repairs used cached pages ($0).
- Haiku: $0.0268 for the 21 scored calls; November ledger now
  $10.6702/$15.00 (headroom $4.33), run cap $1.00 never approached.
- Waste (documented, estimated): $0.0291 — the first run's 21 scored
  replies were lost to a response-parsing bug (Anthropic Messages API
  returns a content-block list, not an OpenAI-style choices[] dict), and
  one AMGN FY2025 Q4 reply was truncated at the 500-token cap. Both
  fixed; the waste is estimated from the actual Q&A texts sent and
  recorded in the ledger.

## Honest caveats

- **21 calls is a small pilot, and no return test was done here.** These
  are scores, not a signal: no post-call returns were computed, no IC,
  no backtest. Whether Q&A scores predict 20-day returns is the
  follow-up experiment's question, not this pilot's answer.
- Scores are single-model (Haiku only) and single-pass; no second
  opinion, no debate, no audit sample. Expect model noise — e.g. ABT's
  ±0.55 quarter-to-quarter swings are as likely scoring noise as real
  tone change.
- One call's transcript (AMGN FY2025 Q3) labels no analyst turns; its
  Q&A span was found with the operator fallback rule instead. Q&A spans
  for the other 20 use the first-Analyst-turn rule.
- Haiku 5.5 ran in November 2026 and may know these calls' outcomes
  (knowledge-cutoff contamination) — same caveat as the filing scores.

## Next step

Pair these 21 scores with 20-trading-day post-call returns (vs SPY) and
compute the IC of the Q&A score, the QoQ-delta (bleak tone change)
signal, and the evasion/analyst-tone components. If the IC clears a
pre-registered bar, register a full earnings-call experiment (wider
universe, more quarters); if not, drop the earnings-call leg. Do not
trade on these scores.
