"""
design_runner.py — B2 design-selectable DRY-RUN / SIMULATION runner.

SIMULATION ONLY. This is NOT a backtest, NOT evidence, and NOT a
paper-trading run. It exists to exercise the B3 exact-once path with
each candidate design's decisions on the SAME dated inputs, in
December, while the January fresh-validation gate stays frozen and
the winner stays UNSET until the January window closes and the sample
minimums are met.

Structure (adopted improvement 5):
- Decision is a pure function: same dated inputs -> proposed orders,
  with design_id recorded on every decision. No market-data fetching,
  no Alpaca, no holdout data, no training, no fitting.
- Execution is the tested B3 path (OrderManager + FakeBroker only).
- Each design gets its own intent namespace (design_id is part of the
  deterministic client_order_id) and its own FakeBroker sandbox /
  intent store in run_all_designs, so designs cannot collide or share
  positions.

Selection rules below are SIMULATION APPROXIMATIONS of the registered
designs, documented per design. They are not the backtest engine
(r2_panels / run_llm_long_horizon_*.py) and their outputs must not be
read as returns or as a gate verdict.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from fake_broker import FakeBroker
from order_manager import OrderManager, build_intent, compute_inputs_hash

DESIGN_IDS = ["v2", "v3", "h3-lazy-veto", "h7-c1c", "exp-ml-ranker-v1"]

# H3: Item 1A similarity below this = the Risk Factors text changed
# materially, so the lazy veto drops the name (simulation threshold,
# documented — the registered H3 rule is in the R2 design doc).
H3_ITEM1A_SIMILARITY_MIN = 0.85
# H7: oversold RSI threshold for the reversion proxy.
H7_RSI_MAX = 30.0
# exp-ml-ranker-v1 simulation stub weights (NO training, NO fitting):
# rank = 0.6*haiku + 0.3*min(rvol,1) - 0.1*(rsi/100). Stub only.
RANKER_WEIGHTS = {"haiku": 0.6, "rvol": 0.3, "rsi": -0.1}


def select_symbols(design_id: str, inputs: list[dict]) -> list[dict]:
    """Pure decision function: dated inputs -> selected rows for a design."""
    if design_id not in DESIGN_IDS:
        raise ValueError(f"unknown design {design_id!r}; expected one of {DESIGN_IDS}")
    rows = [dict(r) for r in inputs]
    if design_id == "v2":
        # v2: top-5 by Haiku score, score > 0 (weekly rebalance /
        # min-hold 10d are execution notes, recorded in the output).
        picked = [r for r in rows if float(r.get("haiku_score") or 0) > 0]
        picked.sort(key=lambda r: float(r.get("haiku_score") or 0), reverse=True)
        return picked[:5]
    if design_id == "v3":
        # v3: v2 + composite technical > 0 veto.
        picked = [
            r for r in rows
            if float(r.get("haiku_score") or 0) > 0 and float(r.get("composite_technical") or 0) > 0
        ]
        picked.sort(key=lambda r: float(r.get("haiku_score") or 0), reverse=True)
        return picked[:5]
    if design_id == "h3-lazy-veto":
        # h3: v3 + Item 1A similarity veto (drop materially changed names).
        picked = [
            r for r in rows
            if float(r.get("haiku_score") or 0) > 0
            and float(r.get("composite_technical") or 0) > 0
            and float(r.get("item1a_similarity") if r.get("item1a_similarity") is not None else 1.0) >= H3_ITEM1A_SIMILARITY_MIN
        ]
        picked.sort(key=lambda r: float(r.get("haiku_score") or 0), reverse=True)
        return picked[:5]
    if design_id == "h7-c1c":
        # h7: reversion+RSI approximation — oversold RSI (<30), most
        # oversold first. No price-dip series is present in these
        # inputs, so this is a documented approximation, not C1c itself.
        picked = [r for r in rows if float(r.get("rsi_14") or 100) < H7_RSI_MAX]
        picked.sort(key=lambda r: float(r.get("rsi_14") or 100))
        return picked[:5]
    # exp-ml-ranker-v1: regularized-ranker STUB — weighted sum only,
    # no training, no fitting on the inputs.
    def _rank(r: dict) -> float:
        return (
            RANKER_WEIGHTS["haiku"] * float(r.get("haiku_score") or 0)
            + RANKER_WEIGHTS["rvol"] * min(float(r.get("rvol_20") or 0), 1.0)
            + RANKER_WEIGHTS["rsi"] * (float(r.get("rsi_14") or 50) / 100.0)
        )

    picked = [r for r in rows if float(r.get("haiku_score") or 0) > 0]
    picked.sort(key=_rank, reverse=True)
    return picked[:5]


def run_design_simulation(
    design_id: str,
    inputs: list[dict],
    broker: FakeBroker,
    store_path: str | Path,
    trading_day: str,
) -> dict:
    """Run one design on the given dated inputs, via B3 + FakeBroker.

    Returns a JSON-serialisable summary. winner is always None.
    """
    inputs_hash = compute_inputs_hash(inputs)
    manager = OrderManager(store_path)
    try:
        # Startup reconcile before any new order for any symbol.
        manager.reconcile_all_unknowns(
            lookup_fn=broker.get_by_client_order_id,
            place_order_fn=broker.submit,
            grace_seconds=0.0,
            max_polls=3,
            sleep_fn=lambda s: None,
        )
        decisions = []
        for row in select_symbols(design_id, inputs):
            qty = int(row.get("qty_hint") or 10)
            intent = build_intent(
                design_id=design_id,
                trading_day=trading_day,
                symbol=row["symbol"],
                side="buy",
                qty=qty,
                order_type="market",
                inputs_hash=inputs_hash,
            )
            result = manager.submit(
                intent,
                place_order_fn=broker.submit,
                lookup_fn=broker.get_by_client_order_id,
                sleep_fn=lambda s: None,
                grace_seconds=0.0,
            )
            decisions.append(
                {
                    "symbol": row["symbol"],
                    "design_id": design_id,
                    "trading_day": trading_day,
                    "inputs_hash": inputs_hash,
                    "client_order_id": result["intent_id"],
                    "broker_order_id": result.get("broker_order_id"),
                    "status": result["status"],
                    "qty": qty,
                }
            )
    finally:
        manager.close()
    return {
        "simulation": True,
        "design_id": design_id,
        "trading_day": trading_day,
        "inputs_hash": inputs_hash,
        "decisions": decisions,
        "broker_order_count": broker.order_count(),
        "winner": None,
        "winner_note": "unset until January — fresh-validation window closes 2026-12-31 and sample minimums (30 trading days, 40 filings) are met",
    }


def run_all_designs(inputs: list[dict], trading_day: str, store_dir: str | Path) -> dict:
    """Run all five designs on the SAME inputs, each in its own sandbox."""
    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for design_id in DESIGN_IDS:
        broker = FakeBroker()
        results[design_id] = run_design_simulation(
            design_id, inputs, broker, store_dir / f"{design_id}.db", trading_day
        )
    return {"simulation": True, "trading_day": trading_day, "designs": results, "winner": None}


def _load_inputs(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        return data.get("inputs") or data.get("rows") or []
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="B2 design-selectable simulation runner (fake broker only)")
    parser.add_argument("--design", choices=DESIGN_IDS, help="single design to run")
    parser.add_argument("--all-designs", action="store_true", help="run all five designs on the same inputs")
    parser.add_argument("--trading-day", default="2026-12-01")
    parser.add_argument("--inputs", required=True, help="JSON file: list of input rows (or {inputs: [...]})")
    parser.add_argument("--store-dir", default="./.cache/design_sim")
    args = parser.parse_args()
    inputs = _load_inputs(args.inputs)
    if args.all_designs:
        print(json.dumps(run_all_designs(inputs, args.trading_day, args.store_dir), indent=2))
    elif args.design:
        broker = FakeBroker()
        store = Path(args.store_dir) / f"{args.design}.db"
        store.parent.mkdir(parents=True, exist_ok=True)
        print(json.dumps(run_design_simulation(args.design, inputs, broker, store, args.trading_day), indent=2))
    else:
        parser.error("pass --design <id> or --all-designs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
