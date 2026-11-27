from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import TradingConfig

log = logging.getLogger("reconcile")


def _load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _positions_for_leg(leg) -> dict:
    try:
        return leg.positions() or {}
    except Exception:
        return {}


def _open_orders_for_leg(leg) -> list[dict]:
    """Open orders for a leg, [] if the leg does not expose them.
    A leg without open_orders() (older adapters) behaves as before:
    only filled positions count."""
    getter = getattr(leg, "open_orders", None)
    if getter is None:
        return []
    try:
        return getter() or []
    except Exception:
        return []


def _actual_for_leg(leg) -> dict:
    """What the broker actually holds for a leg: filled positions PLUS
    the unfilled quantity of open orders, netted per symbol (buys +,
    sells -). B13: the strategy records the expected position the
    moment an order is submitted, but positions() only reflects fills
    — a limit order that has not filled yet is an open order, not a
    position. Counting only fills would flag every unfilled order as
    drift and halt the guardian on the first live run. Counting the
    open-order quantity keeps that case green while an open order the
    strategy does NOT expect is still drift (it appears below as an
    unexpected position)."""
    actual: dict[str, dict] = {}
    for symbol, payload in _positions_for_leg(leg).items():
        qty = float(payload.get("qty", 0.0) or 0.0)
        if qty != 0:
            actual[symbol] = {
                "qty": qty,
                "position_qty": qty,
                "open_order_qty": 0.0,
            }
    for order in _open_orders_for_leg(leg):
        remaining = float(order.get("qty") or 0.0) - float(order.get("filled_qty") or 0.0)
        if remaining == 0:
            continue
        signed = remaining if str(order.get("side", "")).lower() == "buy" else -remaining
        symbol = str(order.get("symbol"))
        entry = actual.setdefault(symbol, {"qty": 0.0, "position_qty": 0.0, "open_order_qty": 0.0})
        entry["qty"] = entry["qty"] + signed
        entry["open_order_qty"] = entry["open_order_qty"] + signed
    return {s: v for s, v in actual.items() if round(v["qty"], 6) != 0}


def reconcile(broker, memory, cfg: TradingConfig, guardian=None) -> dict:
    state = _load_state(cfg.paths.strategy_state_path)
    expected = state.get("expected_positions", {})
    actual = {
        "paper": _actual_for_leg(broker.paper),
        "live": _actual_for_leg(broker.live),
    }
    discrepancies: list[str] = []

    for leg_name in ("paper", "live"):
        exp = expected.get(leg_name, {})
        act = actual.get(leg_name, {})
        for symbol, payload in exp.items():
            exp_qty = float(payload.get("qty", 0.0))
            act_qty = float(act.get(symbol, {}).get("qty", 0.0))
            if round(exp_qty, 6) != round(act_qty, 6):
                discrepancies.append(f"{leg_name}:{symbol} expected {exp_qty} actual {act_qty}")
        for symbol in act:
            if symbol not in exp:
                discrepancies.append(f"{leg_name}:{symbol} unexpected open position {act[symbol].get('qty')}")

    ok = not discrepancies
    if discrepancies:
        log.warning("reconcile discrepancies: %s", discrepancies)
        if guardian is not None:
            guardian.manual_halt("reconcile detected position drift")
    return {"ok": ok, "discrepancies": discrepancies, "actual": actual, "expected": expected}

