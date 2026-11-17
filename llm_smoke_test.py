"""Smoke test for the real Anthropic provider — plumbing only, NOT a signal.

Reads ANTHROPIC_API_KEY from the environment (or from .env, same file
the Alpaca keys live in — never from chat or git). Scores one short
TEST excerpt (labelled test text, not a real filing) three times:
Haiku screen, Sonnet adjudicate, Fable audit — the agreed routing,
on a tiny excerpt so the whole run costs a few cents at most.

Run cap for this script: $1.00 total. Monthly cap: $10.00 (the $10
added 2026-11-17, balance $11.77). If either cap would be exceeded,
the script stops before calling the API. Cached results cost $0.

Usage:  .venv/bin/python llm_smoke_test.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from llm_sentiment import AnthropicProvider, SpendCapExceeded  # noqa: E402

# TEST excerpt only — a short, invented filing paragraph. It exists to
# prove the API key, JSON parsing, cost logging and caps work. It is
# NOT a real filing and its score is NOT a trading signal.
TEST_EXCERPT = (
    "TEST EXCERPT — NOT A REAL FILING. TestCo Inc. reported revenue of "
    "$1.2 billion for the quarter, up 8% year over year, driven by "
    "growth in its cloud segment. Gross margin expanded to 62% from "
    "58% a year ago. Management raised full-year guidance and "
    "announced a $500 million share repurchase program. The company "
    "also disclosed a one-time restructuring charge of $40 million and "
    "noted softer demand in its hardware division."
)
DOCS = [{"doc": "TEST", "section": "Test excerpt", "date": "2026-11-17"}]


def _load_dotenv() -> None:
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    _load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set (env or .env). No calls made.")
        print("Create a key at platform.claude.com > Settings > API keys,")
        print("then add it via the secure vault — never paste it in chat.")
        return 2
    total_cost = 0.0
    for role in ("screen", "adjudicate", "audit"):
        provider = AnthropicProvider(
            api_key, role=role, run_cap_usd=1.0, monthly_cap_usd=10.0
        )
        try:
            result = provider.score("TEST", "2026-11-17", DOCS, TEST_EXCERPT)
        except SpendCapExceeded as exc:
            print(f"[{role}] STOPPED by spend cap: {exc}")
            return 3
        total_cost += result["cost_usd"]
        print(
            f"[{role}] {result['model_id']}: score={result['score']:+.2f} "
            f"confidence={result['confidence']:.2f} "
            f"tokens={result['tokens_in']}/{result['tokens_out']} "
            f"cost=${result['cost_usd']:.4f} cached={result['cached']}"
        )
        print(f"    rationale: {result['rationale']}")
    print(f"Total smoke-test cost: ${total_cost:.4f} (cap $1.00/run, $10.00/month)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
