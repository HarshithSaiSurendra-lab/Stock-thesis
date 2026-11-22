#!/usr/bin/env python3
"""GPT-6.1 Sol second-opinion adapter (OpenAI, custom.openai connector).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE
stays 0. This is the plan's Leg 2 second opinion: the same filing
excerpts, the same JSON score contract as the Haiku screen
(llm_sentiment.SYSTEM_PROMPT), scored by an INDEPENDENT model family
(OpenAI gpt-6.1-sol) so agreement can be measured cross-family instead
of within one Claude family (the recorded Phase 2 caveat).

Connector: ~/workspace/custom-connectors/custom.openai/bin/openai_api.py
(call_chat). Auth is via the registered custom.openai credential
(surrogate bearer header); the key is never read, printed, or stored
here. Verified live Nov 22, 2026: GET /v1/models 200 (99 models,
gpt-6.1-sol present); smoke chat 200 (73 tokens). The credit-grants
endpoint returns 403 for secret keys (browser session key only), so
the $5 balance is not directly readable — a successful chat call is
the practical proof credits exist, and EVERY call's token usage is
logged in the spend ledger (.cache/openai_spend.json).

Pricing: gpt-6.1-sol pricing was NOT verified from a price list
(connector doc Limits). Cost accounting uses the repo's recorded
prices in llm_sentiment.MODEL_PRICES (gpt-6.1-sol: $2.00/MTok input,
$10.00/MTok output). If OpenAI's price list disagrees, the ledger
amounts are wrong by that factor — the TOKEN counts are the ground
truth and are logged per call.

Budget: run cap $4.50, monthly cap $5.00 (the OpenAI credit). The
cap check runs BEFORE each call on the worst case (input estimated
at ~2.9 chars/token — conservative for OpenAI tokenization, which
runs closer to ~4 chars/token — plus GPT_MAX_OUTPUT_TOKENS output).
Results are cached on disk by (symbol, document hash, model id,
prompt version, cache tag); a cached call costs $0 and makes no
network call.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / "workspace" / "custom-connectors" / "custom.openai" / "bin"))

from llm_sentiment import (
    CHATGPT_MODEL_ID,
    MAX_EXCERPT_CHARS,
    MODEL_PRICES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    SpendCap,
    _doc_hash,
    _estimate_tokens,
    _parse_score_json,
    _trim_excerpt,
    cost_usd,
)

# The JSON reply is ~100 tokens; 300 is ample headroom and keeps the
# worst-case output cost small ($0.003/call at $10/MTok).
GPT_MAX_OUTPUT_TOKENS = 300
GPT_CACHE_VERSION = "gpt-sentiment-v1"


class GPTProvider:
    """OpenAI second-opinion adapter: one model, one scored excerpt per call.

    Same score contract as AnthropicProvider.score: returns
    {symbol, as_of, role, model_id, score, confidence, rationale,
    sources, tokens_in, tokens_out, cost_usd, cached}. ``call_chat_fn``
    is injectable for tests; by default it is the connector helper's
    call_chat (which sends max_completion_tokens — gpt-6.1-sol rejects
    max_tokens with a 400).
    """

    def __init__(
        self,
        model_id: str = CHATGPT_MODEL_ID,
        run_cap_usd: float = 4.50,
        monthly_cap_usd: float = 5.00,
        cache_dir: str | Path = ".cache/gpt_cache",
        ledger_path: str | Path = ".cache/openai_spend.json",
        call_chat_fn=None,
    ):
        self.model_id = model_id
        self.cap = SpendCap(run_cap_usd, monthly_cap_usd, ledger_path)
        self.cache_dir = Path(cache_dir)
        if call_chat_fn is not None:
            self._call_chat = call_chat_fn
        else:
            from openai_api import call_chat as _call_chat

            self._call_chat = _call_chat

    def _disk_path(self, symbol: str, documents: list[dict], cache_tag: str = "") -> Path:
        key = hashlib.sha256(
            "|".join([symbol, _doc_hash(documents), self.model_id,
                      PROMPT_VERSION, GPT_CACHE_VERSION, cache_tag]).encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"{key}.json"

    def score(self, symbol: str, as_of: str, documents: list[dict], excerpt: str,
              system_prompt: str | None = None, cache_tag: str = "") -> dict:
        """Score one excerpt. ``documents`` must be dated <= ``as_of``."""
        prompt = system_prompt or SYSTEM_PROMPT
        disk_path = self._disk_path(symbol, documents, cache_tag)
        try:
            cached = json.loads(disk_path.read_text())
            cached["cached"] = True
            return cached
        except (OSError, ValueError, KeyError):
            pass

        text = _trim_excerpt(excerpt)
        est_in = _estimate_tokens(prompt) + _estimate_tokens(text)
        price_in, price_out = MODEL_PRICES[self.model_id]
        worst_case = (est_in / 1_000_000 * price_in
                      + GPT_MAX_OUTPUT_TOKENS / 1_000_000 * price_out)
        self.cap.check(worst_case)

        user_text = f"Symbol: {symbol}\nAs of: {as_of}\n\n{text}"
        status, payload = self._call_chat(
            self.model_id, prompt, user_text,
            max_tokens=GPT_MAX_OUTPUT_TOKENS)
        if status != 200:
            raise RuntimeError(
                f"OpenAI chat failed: status {status}: {str(payload)[:300]}")
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"OpenAI reply has no choices[0].message.content: {str(payload)[:300]}") from exc
        parsed = _parse_score_json(content)
        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        cost = cost_usd(self.model_id, tokens_in, tokens_out)
        self.cap.record(cost)

        result = {
            "symbol": symbol,
            "as_of": as_of,
            "role": "second_opinion",
            "model_id": self.model_id,
            "score": parsed["score"],
            "confidence": parsed["confidence"],
            "rationale": parsed["rationale"],
            "sources": [
                {"doc": d.get("doc", ""), "section": d.get("section", ""), "date": d.get("date", "")}
                for d in documents
            ],
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": cost,
            "cached": False,
        }
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            disk_path.write_text(json.dumps(result, indent=2))
        except OSError:
            pass
        return result
