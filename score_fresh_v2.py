#!/usr/bin/env python3
"""Score the v2 extension filings with Haiku 5.5 (fresh-validation Step 0).

Same JSON score contract as Phase 2 (llm_sentiment.py, prompt version
llm-sentiment-v2, Haiku-only, no Sonnet adjudication). Reuses
run_llm_backtest.score_pass (budget guard, retry-once, incremental
saves, circuit breaker) and the surrogate client from llm_smoke_test
(Secure Vault credential; no key in env/.env).

Caps (repo convention): run cap $1.00 in code, November monthly cap
$15.00 (ledger 2026-11 already $10.670207; headroom $4.33). Filings
are scored in filing-date order so a cap breach stops at the cap with
the shortfall reported, not guessed.

Scores: experiments/results/exp-fresh-validation-v1-scores-haiku.json
(accession -> provider result dict, same shape as the Phase 2 haiku
scores files).

Usage:
    .venv/bin/python score_fresh_v2.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from llm_sentiment import AnthropicProvider  # noqa: E402
from llm_smoke_test import _SurrogateClient  # noqa: E402
import run_llm_backtest as p2  # noqa: E402

REPO = Path(__file__).parent
V2_DIR = REPO / "data" / "edgar_excerpts_v2"
SCORES_PATH = REPO / "experiments" / "results" / "exp-fresh-validation-v1-scores-haiku.json"

RUN_CAP_USD = 1.00
MONTHLY_CAP_USD = 15.00


def load_v2_filings() -> pd.DataFrame:
    frames = []
    for shard in sorted(V2_DIR.glob("excerpts_*.jsonl")):
        records = [json.loads(line) for line in shard.read_text().splitlines() if line.strip()]
        frames.append(pd.DataFrame.from_records(records))
    filings = pd.concat(frames, ignore_index=True)
    filings["filing_date"] = pd.to_datetime(filings["filing_date"])
    filings["excerpt_chars"] = filings["excerpt_text"].str.len()
    # Filing-date order (task rule): a cap breach stops at the cap.
    filings = filings.sort_values(["filing_date", "ticker"]).reset_index(drop=True)
    return filings


def main() -> int:
    filings = load_v2_filings()
    print(f"v2 filings to score: {len(filings)} "
          f"({filings['filing_date'].min().date()}..{filings['filing_date'].max().date()})",
          flush=True)
    scores = p2._load_scores(SCORES_PATH)
    print(f"already scored (resuming): {len(scores)}", flush=True)

    provider = AnthropicProvider(
        "sk-ant-surrogate",
        role="screen",
        run_cap_usd=RUN_CAP_USD,
        monthly_cap_usd=MONTHLY_CAP_USD,
        client=_SurrogateClient(),
    )
    summary = p2.score_pass(
        provider,
        filings,
        role="haiku-v2",
        spent_before_usd=0.0,
        budget_usd=RUN_CAP_USD,
        save_path=SCORES_PATH,
        scores=scores,
    )
    print(json.dumps(summary, indent=2), flush=True)
    n_ok = sum(1 for v in scores.values() if "error" not in v)
    print(f"scores file: {SCORES_PATH} ({n_ok} scored, "
          f"{len(scores) - n_ok} failed)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
