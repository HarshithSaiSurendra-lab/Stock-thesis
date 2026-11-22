#!/usr/bin/env python3
"""Unit tests for round-2 panel builders and the llm_sentiment debate
extension. Synthetic data only — no API calls, no file writes outside
temporary directories. Run: python3 -m pytest test_r2.py -q
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))

import r2_panels as r2
from backtest import BacktestConfig, run_backtest
from llm_sentiment import AnthropicProvider


def _days(n=30):
    return pd.bdate_range("2025-01-01", periods=n)


def _score_panel(days, symbols, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(0, 1, size=(len(days), len(symbols))),
                        index=days, columns=symbols)


# ----------------------------------------------------------------------
# v2 / v3 selection
# ----------------------------------------------------------------------
def test_v2_selection_top_five_cutoff():
    days = _days(5)
    syms = [f"S{i}" for i in range(10)]
    score = pd.DataFrame(0.0, index=days, columns=syms)
    score.iloc[0] = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    sel = r2.v2_selection(score)
    # top 5 positive scores (10..6) selected; ties at the cutoff included
    assert sel.iloc[0].sum() == 5
    assert list(sel.columns[sel.iloc[0] > 0]) == syms[:5]
    # no positives -> empty
    assert sel.iloc[1].sum() == 0


def test_v3_selection_requires_positive_composite():
    days = _days(3)
    syms = ["A", "B", "C", "D", "E", "F"]
    score = pd.DataFrame(1.0, index=days, columns=syms)
    comp = pd.DataFrame(1.0, index=days, columns=syms)
    comp.loc[days[0], ["A", "B"]] = -1.0
    sel = r2.v3_selection(score, comp)
    assert sel.loc[days[0], "A"] == 0.0
    assert sel.loc[days[0], "B"] == 0.0
    assert sel.loc[days[0], "C"] == 1.0


# ----------------------------------------------------------------------
# Banding
# ----------------------------------------------------------------------
def test_banded_selection_hysteresis():
    days = _days(10)
    syms = ["A", "B", "C", "D", "E", "F", "G", "H"]
    # base selection picks A on day 0 only
    base = pd.DataFrame(0.0, index=days, columns=syms)
    base.iloc[0, 0] = 1.0
    # scores: A starts high, drifts down slowly; others constant
    score = pd.DataFrame(0.5, index=days, columns=syms)
    score["A"] = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    banded = r2.banded_selection(base, score, buffer_pct=50, grid=5)
    # A enters day 0 and is retained through the day-5 grid check
    # (score 5 on day 5 vs median of positives ~0.5 -> still above)
    assert banded.loc[days[0], "A"] == 1.0
    assert banded.loc[days[4], "A"] == 1.0
    assert banded.loc[days[5], "A"] == 1.0
    # no other name ever enters
    assert banded.drop(columns=["A"]).sum().sum() == 0
    # a name below the buffer exits at the next grid day
    score2 = pd.DataFrame(0.5, index=days, columns=syms)
    score2["A"] = [10, 9, 8, 7, 6, 0.1, 0.1, 0.1, 0.1, 0.1]
    banded2 = r2.banded_selection(base, score2, buffer_pct=50, grid=5)
    assert banded2.loc[days[4], "A"] == 1.0  # held between grids
    assert banded2.loc[days[5], "A"] == 0.0  # exits at grid day (0.1 < median)


# ----------------------------------------------------------------------
# Cohorts
# ----------------------------------------------------------------------
def test_cohort_h1_matches_plain_backtest():
    days = _days(60)
    syms = [f"S{i}" for i in range(20)]
    rng = np.random.default_rng(3)
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, 0.01, size=(len(days), len(syms))), axis=0)),
        index=days, columns=syms)
    sel = pd.DataFrame(0.0, index=days, columns=syms)
    sel.iloc[::5, :5] = 1.0
    cohort = r2.cohort_returns(close, sel, horizon=1, costs=(10,),
                               min_hold_days=1, max_positions=5)
    plain = run_backtest(close, sel, BacktestConfig(
        cost_bps=10, long_only=True, max_positions=5,
        rebalance_every=1, min_hold_days=1))
    # H=1 cohort = the same portfolio, up to the ffill reproduction
    plain_total = float((1 + plain.daily_returns).prod() - 1.0)
    assert abs(cohort["cost_10bps"]["total_return"] - plain_total) < 1e-9


def test_cohort_h10_caps_turnover():
    days = _days(120)
    syms = [f"S{i}" for i in range(20)]
    rng = np.random.default_rng(4)
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0, 0.01, size=(len(days), len(syms))), axis=0)),
        index=days, columns=syms)
    sel = pd.DataFrame(0.0, index=days, columns=syms)
    # churn the whole selection every 5 days
    for k in range(0, len(days), 5):
        sel.iloc[k, (k // 5) % 4 * 5:(k // 5) % 4 * 5 + 5] = 1.0
    out = r2.cohort_returns(close, sel, horizon=10, costs=(10,),
                            min_hold_days=1, max_positions=5)
    # structural cap: blended turnover cannot exceed 100%/H = 0.10/day
    assert out["cost_10bps"]["avg_daily_turnover"] <= 0.10 + 1e-9


# ----------------------------------------------------------------------
# Lazy Prices
# ----------------------------------------------------------------------
def test_lazy_prices_same_form_only_and_missing():
    filings = pd.DataFrame([
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-02-01"),
         "form": "10-K", "risk_text": "risk factor alpha beta gamma"},
        {"ticker": "AAA", "filing_date": pd.Timestamp("2024-05-01"),
         "form": "10-Q", "risk_text": "risk factor alpha beta gamma"},
        {"ticker": "AAA", "filing_date": pd.Timestamp("2025-02-01"),
         "form": "10-K", "risk_text": "risk factor alpha beta delta"},
        {"ticker": "AAA", "filing_date": pd.Timestamp("2025-05-01"),
         "form": "10-Q", "risk_text": ""},  # missing risk text
    ])
    sim = r2.lazy_prices_similarity(filings)
    k25 = sim[(sim["form"] == "10-K") & (sim["filing_date"] == "2025-02-01")]
    # 10-K vs prior 10-K: tokens {risk,factor,alpha,beta,gamma} vs
    # {risk,factor,alpha,beta,delta}: Jaccard 4/6
    assert abs(k25["jaccard"].iloc[0] - 4 / 6) < 1e-9
    q25 = sim[(sim["form"] == "10-Q") & (sim["filing_date"] == "2025-05-01")]
    assert q25["jaccard"].iloc[0] == 1.0  # missing text -> no change


def test_lazy_changer_panel_bottom_quintile():
    days = _days(10)
    syms = [f"S{i}" for i in range(10)]
    sim = pd.DataFrame([
        {"ticker": s, "filing_date": pd.Timestamp("2025-01-01"),
         "form": "10-K", "jaccard": 0.1 if i == 0 else 0.9,
         "cosine": 0.1 if i == 0 else 0.9}
        for i, s in enumerate(syms)
    ])
    _, changer = r2.lazy_changer_panel(sim, days, syms)
    # only the 0.1-similarity name is in the bottom quintile
    assert changer.loc[days[0], "S0"] == 1.0
    assert changer.loc[days[0], syms[1:]].sum() == 0


# ----------------------------------------------------------------------
# Mixture
# ----------------------------------------------------------------------
def test_mixture_thresholds():
    days = _days(3)
    syms = [f"S{i}" for i in range(10)]
    score = pd.DataFrame(0.0, index=days, columns=syms)
    score.iloc[0] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    comp = pd.DataFrame(0.0, index=days, columns=syms)
    comp.iloc[0] = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]  # anti-aligned
    mix = r2.mixture_panel(score, comp)
    # ranks are perfectly anti-aligned: mixture is 0.55 for every name,
    # below the 0.9 threshold -> nothing selected
    assert mix.iloc[0].sum() == 0
    # aligned case: top names pass all three thresholds. Ranks are
    # 0.1..1.0, so mixture >= 0.9 admits the top two (ranks 0.9, 1.0)
    comp.iloc[1] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    score.iloc[1] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    mix = r2.mixture_panel(score, comp)
    assert mix.iloc[1, -1] == 1.0  # top name: both ranks 1.0
    assert (mix.iloc[1] > 0).sum() == 2


# ----------------------------------------------------------------------
# Native Ridge / GBDT
# ----------------------------------------------------------------------
def test_ridge_recovers_linear_signal():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(500, 4))
    beta = np.array([1.0, -2.0, 0.5, 0.0])
    y = X @ beta + rng.normal(scale=0.1, size=500)
    pred = r2.ridge_fit_predict(X[:400], y[:400], X[400:])
    corr = np.corrcoef(pred, y[400:])[0, 1]
    assert corr > 0.95


def test_gbdt_fits_nonlinear_and_predicts_finite():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(2000, 3))
    y = (X[:, 0] > 0).astype(float) * 2.0 - (X[:, 1] < -0.5) + rng.normal(scale=0.1, size=2000)
    model = r2.NativeGBDT(n_estimators=50, seed=5).fit(X[:1500], y[:1500])
    pred = model.predict(X[1500:])
    assert np.all(np.isfinite(pred))
    base_err = float(np.mean((y[1500:] - y[:1500].mean()) ** 2))
    model_err = float(np.mean((y[1500:] - pred) ** 2))
    assert model_err < 0.5 * base_err  # beats the constant baseline


def test_gbdt_deterministic_seed():
    rng = np.random.default_rng(2)
    X = rng.normal(size=(800, 2))
    y = X[:, 0] + rng.normal(scale=0.1, size=800)
    p1 = r2.NativeGBDT(n_estimators=10, seed=9).fit(X, y).predict(X)
    p2 = r2.NativeGBDT(n_estimators=10, seed=9).fit(X, y).predict(X)
    assert np.allclose(p1, p2)


# ----------------------------------------------------------------------
# llm_sentiment debate extension (fake client, no network)
# ----------------------------------------------------------------------
class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(10, 5)


class _FakeMessages:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, model, max_tokens, system, messages):
        self.calls.append({"system": system, "messages": messages})
        return _FakeResponse(self.text)


class _FakeClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def test_score_system_prompt_and_cache_tag(tmp_path):
    provider = AnthropicProvider(
        api_key="sk-ant-surrogate", role="screen",
        run_cap_usd=10.0, monthly_cap_usd=100.0,
        cache_dir=tmp_path / "cache",
        ledger_path=tmp_path / "spend.json",
        client=_FakeClient('{"score": 1, "confidence": 0.8, "rationale": "ok"}'))
    docs = [{"accession": "a1", "filing_date": "2025-01-01"}]
    r_bull = provider.score("AAA", "2025-01-02", docs, "great growth",
                            system_prompt="BULL PROMPT", cache_tag="bull")
    r_bear = provider.score("AAA", "2025-01-02", docs, "great growth",
                            system_prompt="BEAR PROMPT", cache_tag="bear")
    # separate cache entries: two API calls, different system prompts
    assert len(provider.client.messages.calls) == 2
    assert provider.client.messages.calls[0]["system"] == "BULL PROMPT"
    assert provider.client.messages.calls[1]["system"] == "BEAR PROMPT"
    # a repeat bull call hits the disk cache (no third call)
    r_bull2 = provider.score("AAA", "2025-01-02", docs, "great growth",
                             system_prompt="BULL PROMPT", cache_tag="bull")
    assert len(provider.client.messages.calls) == 2
    assert r_bull2["cached"] is True
    # default behaviour unchanged: standard prompt, empty tag
    r_std = provider.score("AAA", "2025-01-02", docs, "great growth")
    assert provider.client.messages.calls[2]["system"].startswith(
        "You are a financial analyst scoring")


# ----------------------------------------------------------------------
# GPT second-opinion adapter (fake call_chat, no network)
# ----------------------------------------------------------------------
from gpt_sentiment import GPTProvider, GPT_MAX_OUTPUT_TOKENS
from llm_sentiment import MODEL_PRICES, SpendCapExceeded


class _FakeOpenAIChat:
    """Stub for openai_api.call_chat: returns a canned JSON reply with
    usage. Records every call (model, system, user_text, max_tokens)."""

    def __init__(self, reply='{"score": 0.5, "confidence": 0.7, "rationale": "ok"}',
                 prompt_tokens=1000, completion_tokens=80, status=200):
        self.reply = reply
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.status = status
        self.calls = []

    def __call__(self, model, system, user_text, max_tokens=1000, temperature=None):
        self.calls.append({"model": model, "system": system,
                           "user_text": user_text, "max_tokens": max_tokens})
        payload = {
            "id": "chatcmpl-fake",
            "model": model,
            "choices": [{"message": {"content": self.reply}}],
            "usage": {"prompt_tokens": self.prompt_tokens,
                      "completion_tokens": self.completion_tokens,
                      "total_tokens": self.prompt_tokens + self.completion_tokens},
        }
        return self.status, payload


def test_gpt_score_parses_reply_and_logs_usage(tmp_path):
    chat = _FakeOpenAIChat()
    provider = GPTProvider(run_cap_usd=10.0, monthly_cap_usd=10.0,
                           cache_dir=tmp_path / "gpt_cache",
                           ledger_path=tmp_path / "openai_spend.json",
                           call_chat_fn=chat)
    docs = [{"doc": "10-K", "section": "MD&A + Risk Factors", "date": "2025-01-01"}]
    res = provider.score("AAA", "2025-01-02", docs, "revenue up strongly")
    assert res["score"] == 0.5
    assert res["confidence"] == 0.7
    assert res["model_id"] == "gpt-6.1-sol"
    assert res["role"] == "second_opinion"
    assert res["tokens_in"] == 1000
    assert res["tokens_out"] == 80
    price_in, price_out = MODEL_PRICES["gpt-6.1-sol"]
    assert res["cost_usd"] == round(1000 / 1_000_000 * price_in
                                    + 80 / 1_000_000 * price_out, 6)
    assert res["cached"] is False
    # the helper is called with max_completion_tokens semantics:
    # max_tokens param == GPT_MAX_OUTPUT_TOKENS
    assert chat.calls[0]["max_tokens"] == GPT_MAX_OUTPUT_TOKENS
    # the standard system prompt is used by default
    assert chat.calls[0]["system"].startswith("You are a financial analyst scoring")
    # ledger records the spend
    ledger = json.loads((tmp_path / "openai_spend.json").read_text())
    assert ledger["2026-11"] == res["cost_usd"]


def test_gpt_score_cached_second_call_is_free(tmp_path):
    chat = _FakeOpenAIChat()
    provider = GPTProvider(run_cap_usd=10.0, monthly_cap_usd=10.0,
                           cache_dir=tmp_path / "gpt_cache",
                           ledger_path=tmp_path / "openai_spend.json",
                           call_chat_fn=chat)
    docs = [{"doc": "10-K", "section": "MD&A + Risk Factors", "date": "2025-01-01"}]
    provider.score("AAA", "2025-01-02", docs, "revenue up strongly")
    res2 = provider.score("AAA", "2025-01-02", docs, "revenue up strongly")
    assert len(chat.calls) == 1
    assert res2["cached"] is True
    # a different cache tag is a separate entry (a new call)
    provider.score("AAA", "2025-01-02", docs, "revenue up strongly",
                   cache_tag="debate")
    assert len(chat.calls) == 2


def test_gpt_score_non_200_raises(tmp_path):
    chat = _FakeOpenAIChat(status=400)
    provider = GPTProvider(run_cap_usd=10.0, monthly_cap_usd=10.0,
                           cache_dir=tmp_path / "gpt_cache",
                           ledger_path=tmp_path / "openai_spend.json",
                           call_chat_fn=chat)
    docs = [{"doc": "10-K", "section": "MD&A + Risk Factors", "date": "2025-01-01"}]
    with pytest.raises(RuntimeError, match="status 400"):
        provider.score("AAA", "2025-01-02", docs, "revenue up strongly")


def test_gpt_score_cap_blocks_before_call(tmp_path):
    chat = _FakeOpenAIChat()
    provider = GPTProvider(run_cap_usd=0.0001, monthly_cap_usd=10.0,
                           cache_dir=tmp_path / "gpt_cache",
                           ledger_path=tmp_path / "openai_spend.json",
                           call_chat_fn=chat)
    docs = [{"doc": "10-K", "section": "MD&A + Risk Factors", "date": "2025-01-01"}]
    with pytest.raises(SpendCapExceeded):
        provider.score("AAA", "2025-01-02", docs, "revenue up strongly")
    assert len(chat.calls) == 0  # no network call was made
