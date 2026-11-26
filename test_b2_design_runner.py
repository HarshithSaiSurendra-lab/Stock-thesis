"""B2 design-runner tests. Simulation + FakeBroker only — no real
broker, no network, no holdout data. Winner is always unset.
"""
import json

from design_runner import DESIGN_IDS, run_all_designs, run_design_simulation, select_symbols
from fake_broker import FakeBroker

SAMPLE_INPUTS = [
    {"date": "2026-12-01", "symbol": "AAPL", "haiku_score": 0.85, "composite_technical": 1.0, "rvol_20": 0.40, "rsi_14": 55.0, "item1a_similarity": 0.97, "price": 200.0, "qty_hint": 10},
    {"date": "2026-12-01", "symbol": "MSFT", "haiku_score": 0.70, "composite_technical": 1.0, "rvol_20": 0.30, "rsi_14": 60.0, "item1a_similarity": 0.95, "price": 400.0, "qty_hint": 5},
    {"date": "2026-12-01", "symbol": "NVDA", "haiku_score": 0.60, "composite_technical": -1.0, "rvol_20": 0.80, "rsi_14": 25.0, "item1a_similarity": 0.60, "price": 150.0, "qty_hint": 8},
    {"date": "2026-12-01", "symbol": "AMZN", "haiku_score": 0.50, "composite_technical": 1.0, "rvol_20": 0.25, "rsi_14": 45.0, "item1a_similarity": 0.99, "price": 180.0, "qty_hint": 10},
    {"date": "2026-12-01", "symbol": "META", "haiku_score": 0.40, "composite_technical": 1.0, "rvol_20": 0.20, "rsi_14": 28.0, "item1a_similarity": 0.90, "price": 300.0, "qty_hint": 6},
    {"date": "2026-12-01", "symbol": "TSLA", "haiku_score": 0.30, "composite_technical": 1.0, "rvol_20": 0.90, "rsi_14": 22.0, "item1a_similarity": 0.88, "price": 250.0, "qty_hint": 4},
    {"date": "2026-12-01", "symbol": "JPM", "haiku_score": -0.20, "composite_technical": 1.0, "rvol_20": 0.15, "rsi_14": 50.0, "item1a_similarity": 0.99, "price": 220.0, "qty_hint": 10},
    {"date": "2026-12-01", "symbol": "XOM", "haiku_score": 0.10, "composite_technical": -1.0, "rvol_20": 0.10, "rsi_14": 65.0, "item1a_similarity": 0.99, "price": 90.0, "qty_hint": 10},
]


def test_all_five_designs_same_inputs_tagged_with_own_design_id(tmp_path):
    out = run_all_designs(SAMPLE_INPUTS, "2026-12-01", tmp_path)
    assert out["winner"] is None
    assert set(out["designs"]) == set(DESIGN_IDS)
    for design_id, summary in out["designs"].items():
        assert summary["winner"] is None
        assert summary["simulation"] is True
        assert summary["design_id"] == design_id
        assert summary["broker_order_count"] == len(summary["decisions"])
        for d in summary["decisions"]:
            assert d["design_id"] == design_id
            assert d["client_order_id"] and d["broker_order_id"]
            assert d["status"] == "SUBMITTED"
    # v2 picks top-5 positive Haiku; v3 vetoes NVDA (composite<0);
    # h3 additionally vetoes NVDA on Item 1A; h7 picks oversold RSI.
    v2_syms = [d["symbol"] for d in out["designs"]["v2"]["decisions"]]
    assert v2_syms == ["AAPL", "MSFT", "NVDA", "AMZN", "META"]
    v3_syms = [d["symbol"] for d in out["designs"]["v3"]["decisions"]]
    assert "NVDA" not in v3_syms
    h7_syms = [d["symbol"] for d in out["designs"]["h7-c1c"]["decisions"]]
    assert set(h7_syms) == {"NVDA", "META", "TSLA"}


def test_v2_vs_v3_same_symbol_different_client_order_id(tmp_path):
    out = run_all_designs(SAMPLE_INPUTS, "2026-12-01", tmp_path)
    v2_aapl = next(d for d in out["designs"]["v2"]["decisions"] if d["symbol"] == "AAPL")
    v3_aapl = next(d for d in out["designs"]["v3"]["decisions"] if d["symbol"] == "AAPL")
    assert v2_aapl["client_order_id"] != v3_aapl["client_order_id"]
    # ... but the same design re-run on the same inputs (same broker
    # AND same store — a fresh broker with an old store would be an
    # inconsistent pairing) reuses its order.
    broker = FakeBroker()
    first = run_design_simulation("v2", SAMPLE_INPUTS, broker, tmp_path / "reuse_v2.db", "2026-12-01")
    second = run_design_simulation("v2", SAMPLE_INPUTS, broker, tmp_path / "reuse_v2.db", "2026-12-01")
    assert second["broker_order_count"] == first["broker_order_count"] == len(first["decisions"])
    assert [d["client_order_id"] for d in second["decisions"]] == [
        d["client_order_id"] for d in first["decisions"]
    ]


def test_same_design_twice_reuses_broker_order(tmp_path):
    broker = FakeBroker()
    store = tmp_path / "v3.db"
    first = run_design_simulation("v3", SAMPLE_INPUTS, broker, store, "2026-12-01")
    second = run_design_simulation("v3", SAMPLE_INPUTS, broker, store, "2026-12-01")
    assert second["broker_order_count"] == first["broker_order_count"]
    assert [d["broker_order_id"] for d in second["decisions"]] == [
        d["broker_order_id"] for d in first["decisions"]
    ]


def test_winner_always_none_and_no_real_broker(tmp_path):
    out = run_all_designs(SAMPLE_INPUTS, "2026-12-01", tmp_path)
    assert out["winner"] is None
    for summary in out["designs"].values():
        assert summary["winner"] is None
        # Decisions carry the dated inputs hash, so a re-run on
        # different inputs is a different intent namespace.
        assert summary["inputs_hash"]


def test_select_symbols_is_pure_and_deterministic():
    assert select_symbols("v2", SAMPLE_INPUTS) == select_symbols("v2", SAMPLE_INPUTS)
    assert select_symbols("exp-ml-ranker-v1", SAMPLE_INPUTS)
