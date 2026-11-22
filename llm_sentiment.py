"""LLM sentiment layer — mock provider + real Anthropic provider.

This module is research-only and must NOT be imported by the order path
(main.py / strategy_runner.py / guardian.py / broker_alpaca.py) until
the P0 evidence gate passes for the combined signal.

It implements the interface drafted in ~/workspace/trading-system/
agentic-design.md (2026-11-16) for the two-agent design:

- Claude (Anthropic) — lead agent: reads a 10-K / 10-Q section or news
  item and returns a structured -1..+1 score.
- ChatGPT (OpenAI, ``gpt-6.1-sol``) — second-opinion agent: same inputs,
  separate model, same -1..+1 scale. Disagreement is a feature, not noise.
  The OpenAI adapter is NOT wired yet (no OpenAI API credit as of
  2026-11-17); see the funding page for the week-2 plan.

Methodology agreed with the user 2026-11-17 (research verified live on
the official pricing pages, 2026-11-17):
- Fund Anthropic first: $10 added 2026-11-17, balance $11.77.
  OpenAI $5 in week 2, after the Claude pipeline works. $15 month 1.
- Haiku 5.5 screens everything that passes the technical filter.
- Sonnet 5.5 adjudicates only near-threshold / low-confidence /
  position-candidate cases.
- Fable 5.2 audits a 5-10% sample only — it is NOT the daily model
  ($8/$40 per MTok vs Haiku $0.10/$0.50).
- Excerpts are trimmed to ~40k tokens; every result is cached on disk
  by (symbol, document hash, model id, prompt version) so a backtest
  never re-bills for the same filing.
- A spend cap is enforced in code (per-run + monthly ledger), on top
  of any dashboard limit. Keys come from the environment, never chat/git.

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
import os
import re
from pathlib import Path

# Pinned model IDs (verified live on official pricing pages, 2026-11-17).
HAIKU_MODEL_ID = "claude-haiku-5-5"
SONNET_MODEL_ID = "claude-sonnet-5-5"
FABLE_MODEL_ID = "claude-fable-5-2"
OPUS_MODEL_ID = "claude-opus-5-5"
CHATGPT_MODEL_ID = "gpt-6.1-sol"  # OpenAI adapter not wired yet

# $ per million tokens (input / output), standard rates, Nov 2026.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    HAIKU_MODEL_ID: (0.10, 0.50),
    SONNET_MODEL_ID: (2.0, 10.0),
    FABLE_MODEL_ID: (8.0, 40.0),
    OPUS_MODEL_ID: (4.0, 20.0),
    CHATGPT_MODEL_ID: (2.0, 10.0),
}

PROMPT_VERSION = "llm-sentiment-v2"
MIN_AGREEMENT = 0.7
MIN_CONFIDENCE = 0.6
MAX_LLM_ADJUSTMENT = 1.0

# ~40k tokens at ~4 chars/token. A full 10-K must be trimmed to an
# excerpt first — never send a whole filing.
MAX_EXCERPT_CHARS = 160_000
MAX_OUTPUT_TOKENS = 1_000

SYSTEM_PROMPT = (
    "You are a financial analyst scoring a filing excerpt for a "
    "quantitative trading system. Reply with ONLY a JSON object, no "
    "other text: {\"score\": <float -1 to 1>, \"confidence\": <float 0 "
    "to 1>, \"rationale\": \"<one sentence>\"}. Score is the expected "
    "directional effect on the stock over the next 3-12 months from "
    "this excerpt alone: +1 strongly positive, 0 neutral, -1 strongly "
    "negative. Confidence is how clear the evidence in the excerpt is."
)

# In-memory cache only: (symbol, document hash, model ids, prompt
# version) -> result. A backtest must be reproducible and must not
# re-bill for the same document. Persist this keying when real
# providers are added; do not cache across a prompt-version change.
_cache: dict[str, dict] = {}


class SpendCapExceeded(RuntimeError):
    """Raised when a call would exceed the run or monthly spend cap."""


def cost_usd(model_id: str, tokens_in: int, tokens_out: int) -> float:
    """Cost of one call from verified Nov 2026 prices."""
    price_in, price_out = MODEL_PRICES[model_id]
    return round(tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out, 6)


def _estimate_tokens(text: str) -> int:
    # ~2.9 chars/token, NOT 4: Phase 2 (exp-technical-plus-llm-v1) measured
    # mean actual Sonnet input 25,998 tokens vs 19,026 estimated at
    # chars/4 (ratio 1.366) — the chars/4 estimate breached the $5.00
    # Sonnet sub-budget by $0.5576. SEC filing text is denser than 4
    # chars/token; 2.9 is the measured rate. Estimates stay conservative
    # (worst-case output is still MAX_OUTPUT_TOKENS).
    return max(1, int(len(text) / 2.9))


def _trim_excerpt(text: str) -> str:
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    return text[:MAX_EXCERPT_CHARS] + "\n[TRUNCATED to ~40k tokens]"


def _parse_score_json(text: str) -> dict:
    """Parse the model's strict-JSON reply; reject anything else."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"model reply contained no JSON object: {text[:200]!r}")
    data = json.loads(match.group(0))
    score = float(data["score"])
    confidence = float(data["confidence"])
    if not -1.0 <= score <= 1.0:
        raise ValueError(f"score out of [-1, 1]: {score}")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of [0, 1]: {confidence}")
    return {
        "score": score,
        "confidence": confidence,
        "rationale": str(data.get("rationale", ""))[:280],
    }


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


class SpendCap:
    """Per-run + monthly spend cap, persisted in a JSON ledger.

    The ledger lives under .cache/ (git-ignored). It records spend by
    calendar month so a new month starts fresh, and a run can never
    spend past either cap — the check happens BEFORE the API call,
    using the worst-case cost (full excerpt + max output tokens).
    """

    def __init__(
        self,
        run_cap_usd: float = 1.0,
        monthly_cap_usd: float = 10.0,
        ledger_path: str | Path = ".cache/llm_spend.json",
        month: str | None = None,
    ):
        from datetime import date

        self.run_cap_usd = run_cap_usd
        self.monthly_cap_usd = monthly_cap_usd
        self.ledger_path = Path(ledger_path)
        self.month = month or date.today().strftime("%Y-%m")
        self.spent_run_usd = 0.0
        self.spent_month_usd = self._read_month_spend()

    def _read_month_spend(self) -> float:
        try:
            data = json.loads(self.ledger_path.read_text())
            return float(data.get(self.month, 0.0))
        except (OSError, ValueError, TypeError):
            return 0.0

    def check(self, estimated_cost_usd: float) -> None:
        if self.spent_run_usd + estimated_cost_usd > self.run_cap_usd:
            raise SpendCapExceeded(
                f"run cap ${self.run_cap_usd:.2f} would be exceeded "
                f"(spent ${self.spent_run_usd:.4f}, next call ~${estimated_cost_usd:.4f})"
            )
        if self.spent_month_usd + estimated_cost_usd > self.monthly_cap_usd:
            raise SpendCapExceeded(
                f"monthly cap ${self.monthly_cap_usd:.2f} would be exceeded "
                f"(spent ${self.spent_month_usd:.4f} in {self.month}, "
                f"next call ~${estimated_cost_usd:.4f})"
            )

    def record(self, actual_cost_usd: float) -> None:
        self.spent_run_usd = round(self.spent_run_usd + actual_cost_usd, 6)
        self.spent_month_usd = round(self.spent_month_usd + actual_cost_usd, 6)
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            data: dict = {}
            try:
                data = json.loads(self.ledger_path.read_text())
            except (OSError, ValueError):
                data = {}
            data[self.month] = self.spent_month_usd
            self.ledger_path.write_text(json.dumps(data, indent=2))
        except OSError:
            pass  # ledger is bookkeeping; never block a scored result


class AnthropicProvider:
    """Real Anthropic adapter: one model, one scored excerpt per call.

    Role selects the model per the agreed methodology:
    - "screen" -> Haiku 5.5 (everything that passes the technical filter)
    - "adjudicate" -> Sonnet 5.5 (near-threshold / low-confidence / candidates)
    - "audit" -> Fable 5.2 (5-10% sample only, never the daily model)

    Results are cached on disk by (symbol, document hash, model id,
    prompt version, cache tag). A cached call costs $0 and makes no
    network call. The cache tag (default "") keeps debate-role calls
    (bull / bear / adjudicate) separate from single-pass calls.
    """

    ROLE_MODELS = {
        "screen": HAIKU_MODEL_ID,
        "adjudicate": SONNET_MODEL_ID,
        "audit": FABLE_MODEL_ID,
    }

    def __init__(
        self,
        api_key: str,
        role: str = "screen",
        run_cap_usd: float = 1.0,
        monthly_cap_usd: float = 10.0,
        cache_dir: str | Path = ".cache/llm_cache",
        ledger_path: str | Path = ".cache/llm_spend.json",
        client=None,
    ):
        if role not in self.ROLE_MODELS:
            raise ValueError(f"role must be one of {sorted(self.ROLE_MODELS)}, got {role!r}")
        if not api_key or not api_key.startswith("sk-ant-"):
            raise ValueError("Anthropic API key must start with 'sk-ant-'")
        self.api_key = api_key
        self.role = role
        self.model_id = self.ROLE_MODELS[role]
        self.cap = SpendCap(run_cap_usd, monthly_cap_usd, ledger_path)
        self.cache_dir = Path(cache_dir)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self):
        # The stock-thesis venv's proxy env (NO_PROXY with bracketed
        # IPv6 entries like [::1]) makes httpx choke parsing env
        # proxies at client construction. Pass the egress proxy
        # explicitly with trust_env=False so no env proxy is parsed.
        import anthropic

        try:
            import httpx2

            http_client = httpx2.Client(
                proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"),
                trust_env=False,
                timeout=60.0,
            )
            return anthropic.Anthropic(api_key=self.api_key, http_client=http_client)
        except ImportError:
            return anthropic.Anthropic(api_key=self.api_key)

    def _disk_path(self, symbol: str, documents: list[dict], cache_tag: str = "") -> Path:
        key = hashlib.sha256(
            "|".join([symbol, _doc_hash(documents), self.model_id, PROMPT_VERSION,
                      cache_tag]).encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"{key}.json"

    def score(self, symbol: str, as_of: str, documents: list[dict], excerpt: str,
              system_prompt: str | None = None, cache_tag: str = "") -> dict:
        """Score one excerpt. ``documents`` must be dated <= ``as_of``.

        ``system_prompt`` overrides the module SYSTEM_PROMPT for this
        call (e.g. bull/bear/adjudicate debate roles); ``cache_tag``
        keeps those roles' disk-cache entries separate. Both default
        to the standard single-pass behaviour.
        """
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
        worst_case = est_in / 1_000_000 * price_in + MAX_OUTPUT_TOKENS / 1_000_000 * price_out
        self.cap.check(worst_case)

        response = self.client.messages.create(
            model=self.model_id,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=prompt,
            messages=[{"role": "user", "content": f"Symbol: {symbol}\nAs of: {as_of}\n\n{text}"}],
        )
        reply = "".join(block.text for block in response.content if hasattr(block, "text"))
        parsed = _parse_score_json(reply)
        tokens_in = int(response.usage.input_tokens)
        tokens_out = int(response.usage.output_tokens)
        cost = cost_usd(self.model_id, tokens_in, tokens_out)
        self.cap.record(cost)

        result = {
            "symbol": symbol,
            "as_of": as_of,
            "role": self.role,
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


def anthropic_score(
    symbol: str,
    as_of: str,
    documents: list[dict],
    excerpt: str,
    role: str = "screen",
    api_key: str | None = None,
    run_cap_usd: float = 1.0,
    monthly_cap_usd: float = 10.0,
) -> dict:
    """One-call helper: API key from ``ANTHROPIC_API_KEY`` if not given."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    provider = AnthropicProvider(
        key, role=role, run_cap_usd=run_cap_usd, monthly_cap_usd=monthly_cap_usd
    )
    return provider.score(symbol, as_of, documents, excerpt)


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


def combine_two_scores(
    technical_score: float,
    score_a: float,
    confidence_a: float,
    score_b: float,
    confidence_b: float,
) -> dict:
    """Combine TWO model scores with a technical score (amended Phase 2 rule).

    Generic version of :func:`combine_with_technical`: model A and model B
    are any two scorers (in the amended exp-technical-plus-llm-v1, A is
    Haiku 5.5 screen and B is Sonnet 5.5 adjudicate). The rule is identical
    to the registered one: agreement = 1 - |a - b| / 2, combined = mean
    when the signs do not disagree; the adjustment applies only when
    agreement >= 0.7 and both confidences >= 0.6, is capped at +/-1.0,
    and is 0 when the models disagree in sign. Returns the same
    {"adjusted_score", "llm_contribution", "applied", "reason"} shape.
    """
    disagree_in_sign = (score_a > 0 > score_b) or (score_b > 0 > score_a)
    if disagree_in_sign:
        return {
            "adjusted_score": technical_score,
            "llm_contribution": 0.0,
            "applied": False,
            "reason": "models disagree in sign; combined LLM score neutralised",
        }
    agreement = 1.0 - abs(score_a - score_b) / 2.0
    if agreement < MIN_AGREEMENT:
        return {
            "adjusted_score": technical_score,
            "llm_contribution": 0.0,
            "applied": False,
            "reason": f"agreement {agreement:.4f} below {MIN_AGREEMENT}",
        }
    if confidence_a < MIN_CONFIDENCE or confidence_b < MIN_CONFIDENCE:
        return {
            "adjusted_score": technical_score,
            "llm_contribution": 0.0,
            "applied": False,
            "reason": f"a model confidence is below {MIN_CONFIDENCE}",
        }
    combined = (score_a + score_b) / 2.0
    contribution = max(-MAX_LLM_ADJUSTMENT, min(MAX_LLM_ADJUSTMENT, combined))
    return {
        "adjusted_score": technical_score + contribution,
        "llm_contribution": contribution,
        "applied": True,
        "reason": "both models agree and are confident; LLM adds at most +/-1.0",
    }


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
