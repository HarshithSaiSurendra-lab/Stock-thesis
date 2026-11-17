"""LLM sentiment layer — OFFLINE STUB ONLY (mock provider).

This module is research-only and must NOT be imported by the order path
(main.py / strategy_runner.py / guardian.py / broker_alpaca.py) until
the P0 evidence gate passes for the combined signal.

It implements the interface drafted in ~/workspace/trading-system/
agentic-design.md (2026-11-16) for the two-agent design:

- Claude Fable 5.2 (``claude-fable-5-2``) — lead agent: reads a 10-K /
  10-Q section or news item and returns a structured -1..+1 score.
- ChatGPT — second-opinion agent: same inputs, separate model, same
  -1..+1 scale. Disagreement is a feature, not noise.

Only a mock provider exists here. It returns fixed, deterministic
scores supplied by the caller, costs $0, and makes no network calls.
Real Anthropic / OpenAI adapters belong behind env-gated adapters only,
with API keys from the environment (never in chat or git) and a spend
cap in code. No model id for ChatGPT is pinned here — pin it at
integration time from the OpenAI model list and record it in every
experiment log.

Combination rule — registered BEFORE testing, not tuned after
(see experiments.py):
- The technical composite score stays the base signal.
- The LLM combined score may add at most +1.0 / -1.0 to the decision
  score, only when both models agree (agreement >= 0.7) and both are
  confident (confidence >= 0.6).
- If the two models disagree in sign, the combined LLM score is 0 and
  the disagreement is logged (returned in the result).
- An LLM score alone never creates a strong_up signal, and never
  overrides Guardian, regime filters, liquidity, or quote checks.
"""
from __future__ import annotations

import hashlib
import json

FABLE_MODEL_ID = "claude-fable-5-2"
PROMPT_VERSION = "llm-sentiment-v1"
MIN_AGREEMENT = 0.7
MIN_CONFIDENCE = 0.6
MAX_LLM_ADJUSTMENT = 1.0

# In-memory cache only: (symbol, document hash, model ids, prompt
# version) -> result. A backtest must be reproducible and must not
# re-bill for the same document. Persist this keying when real
# providers are added; do not cache across a prompt-version change.
_cache: dict[str, dict] = {}


def _doc_hash(documents: list[dict]) -> str:
    payload = json.dumps(documents, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _cache_key(symbol: str, documents: list[dict], model_ids: dict) -> str:
    return "|".join(
        [
            symbol,
            _doc_hash(documents),
            model_ids.get("fable", ""),
            model_ids.get("chatgpt", ""),
            PROMPT_VERSION,
        ]
    )


class MockProvider:
    """Deterministic stand-in for Fable 5.2 + ChatGPT.

    Scores are supplied by the caller (e.g. a test, or a hand-scored
    filing) so the pipeline runs offline with zero API spend. The mock
    never reads the documents to produce a score — that is the real
    providers' job, once they are wired in.
    """

    def __init__(
        self,
        fable_score: float = 0.0,
        fable_confidence: float = 0.8,
        chatgpt_score: float = 0.0,
        chatgpt_confidence: float = 0.8,
        chatgpt_model_id: str = "chatgpt-unpinned-mock",
    ):
        for name, value in (
            ("fable_score", fable_score),
            ("chatgpt_score", chatgpt_score),
        ):
            if not -1.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [-1, 1], got {value}")
        self.fable_score = fable_score
        self.fable_confidence = fable_confidence
        self.chatgpt_score = chatgpt_score
        self.chatgpt_confidence = chatgpt_confidence
        self.chatgpt_model_id = chatgpt_model_id


def llm_score(
    symbol: str,
    as_of: str,
    documents: list[dict],
    provider: MockProvider | None = None,
) -> dict:
    """Score ``symbol`` from point-in-time documents only.

    ``documents`` is a list of {"doc": str, "section": str, "date": str}.
    Callers must pass only documents dated on or before ``as_of`` —
    no future documents. Returns the structured result from the design
    doc, with cost_usd 0.0 for the mock provider.
    """
    provider = provider or MockProvider()
    model_ids = {"fable": FABLE_MODEL_ID, "chatgpt": provider.chatgpt_model_id}
    key = _cache_key(symbol, documents, model_ids)
    if key in _cache:
        result = dict(_cache[key])
        result["cached"] = True
        return result

    fable = provider.fable_score
    chatgpt = provider.chatgpt_score
    agreement = 1.0 - abs(fable - chatgpt) / 2.0
    disagree_in_sign = (fable > 0 > chatgpt) or (chatgpt > 0 > fable)
    combined = 0.0 if disagree_in_sign else (fable + chatgpt) / 2.0
    result = {
        "symbol": symbol,
        "as_of": as_of,
        "fable_score": fable,
        "fable_confidence": provider.fable_confidence,
        "chatgpt_score": chatgpt,
        "chatgpt_confidence": provider.chatgpt_confidence,
        "agreement": round(agreement, 4),
        "combined_score": round(combined, 4),
        "disagree_in_sign": disagree_in_sign,
        "sources": [
            {"doc": d.get("doc", ""), "section": d.get("section", ""), "date": d.get("date", "")}
            for d in documents
        ],
        "model_ids": model_ids,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "cached": False,
    }
    _cache[key] = dict(result)
    return result


def combine_with_technical(technical_score: float, llm_result: dict) -> dict:
    """Add the LLM signal to a technical score under the registered rule.

    Returns {"adjusted_score", "llm_contribution", "applied", "reason"}.
    ``applied`` is False (contribution 0) when the models disagree in
    sign, agree too weakly, or are not confident enough. The contribution
    is capped at +/-1.0. This function never decides strong_up / no_trade
    on its own — the caller keeps that decision, and Guardian still vets
    every order.
    """
    if llm_result.get("disagree_in_sign"):
        return {
            "adjusted_score": technical_score,
            "llm_contribution": 0.0,
            "applied": False,
            "reason": "models disagree in sign; combined LLM score neutralised",
        }
    if llm_result.get("agreement", 0.0) < MIN_AGREEMENT:
        return {
            "adjusted_score": technical_score,
            "llm_contribution": 0.0,
            "applied": False,
            "reason": f"agreement {llm_result.get('agreement')} below {MIN_AGREEMENT}",
        }
    if (
        llm_result.get("fable_confidence", 0.0) < MIN_CONFIDENCE
        or llm_result.get("chatgpt_confidence", 0.0) < MIN_CONFIDENCE
    ):
        return {
            "adjusted_score": technical_score,
            "llm_contribution": 0.0,
            "applied": False,
            "reason": f"a model confidence is below {MIN_CONFIDENCE}",
        }
    contribution = max(
        -MAX_LLM_ADJUSTMENT,
        min(MAX_LLM_ADJUSTMENT, llm_result.get("combined_score", 0.0)),
    )
    return {
        "adjusted_score": technical_score + contribution,
        "llm_contribution": contribution,
        "applied": True,
        "reason": "both models agree and are confident; LLM adds at most +/-1.0",
    }


def clear_cache() -> None:
    """Clear the in-memory LLM result cache (tests / fresh runs only)."""
    _cache.clear()
