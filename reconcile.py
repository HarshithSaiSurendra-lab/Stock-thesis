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


def _done_for_day_orders(manager, lookup_fn=None, account: str = "paper") -> list[dict]:
    """Order-shaped dicts for DONE_FOR_DAY intents (B16 correction).

    The broker's OPEN query does not list done_for_day orders, so
    _open_orders_for_leg never sees them — but the unfilled remainder
    is still a working holding (it can fill tomorrow) and still
    counts against buying power. The intent store is the only source
    for that qty. Each dict has the open_orders shape (id, symbol,
    side, qty, filled_qty, status, limit_price, filled_avg_price,
    client_order_id) so the sizing reservation prices it with the
    same chain and reconcile counts it the same way. filled_qty
    comes from a fresh lookup when one answers, else the intent row
    (the startup reconcile just adopted the latest known state into
    it); limit_price comes from the lookup when present, else the
    intent row; filled_avg_price only from the lookup (the intent
    row does not carry it — the reservation chain falls back to
    last_price / entry_price without it, as for a market order).
    An inconclusive lookup never removes the intent: the row's own
    quantities are the conservative record.

    Only intents for `account` are returned: a paper DONE_FOR_DAY
    intent must never be counted as a live-leg holding (or vice
    versa) — the live leg's reconcile would otherwise see an
    unexpected position for an order it never placed.
    """
    if manager is None:
        return []
    out = []
    for intent in manager.done_for_day_intents():
        if (intent.get("account") or "paper") != account:
            continue
        broker_order = None
        if lookup_fn is not None:
            try:
                broker_order = lookup_fn(intent["intent_id"])
            except Exception:
                broker_order = None
        filled = intent.get("filled_qty")
        limit = intent.get("limit_price")
        filled_avg = None
        if isinstance(broker_order, dict):
            try:
                if broker_order.get("filled_qty") is not None:
                    filled = float(broker_order["filled_qty"])
            except (TypeError, ValueError):
                pass
            try:
                if broker_order.get("limit_price") is not None:
                    limit = float(broker_order["limit_price"])
            except (TypeError, ValueError):
                pass
            try:
                if broker_order.get("filled_avg_price") is not None:
                    filled_avg = float(broker_order["filled_avg_price"])
            except (TypeError, ValueError):
                pass
        try:
            filled_qty = float(filled or 0.0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        try:
            qty = float(intent.get("qty") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty - filled_qty <= 0:
            continue  # fully filled (adopted as FILLED next reconcile); nothing outstanding
        out.append({
            "id": str(intent.get("broker_order_id") or intent.get("intent_id")),
            "client_order_id": str(intent.get("intent_id")),
            "symbol": str(intent.get("symbol")),
            "side": str(intent.get("side", "")).lower(),
            "qty": qty,
            "filled_qty": filled_qty,
            "status": "done_for_day",
            "order_type": str(intent.get("order_type", "")).lower(),
            "limit_price": limit,
            "filled_avg_price": filled_avg,
        })
    return out


def _open_orders_including_done_for_day(leg, manager=None, lookup_fn=None,
                                        account: str = "paper") -> list[dict]:
    """Broker open orders PLUS the DONE_FOR_DAY intents' orders.

    The two sources are disjoint by construction — the OPEN query
    never lists a done_for_day order — but the merge still dedupes
    by broker order id (preferring the broker list, which is
    fresher) so a disagreement between the two views can never
    double-count the same order. Use this wherever an open order's
    unfilled quantity matters: sizing reservations and reconcile.
    Only intents for `account` are merged (see _done_for_day_orders).
    """
    orders = list(_open_orders_for_leg(leg))
    seen = {str(o.get("id")) for o in orders if o.get("id") is not None}
    for extra in _done_for_day_orders(manager, lookup_fn, account=account):
        if str(extra.get("id")) in seen:
            continue
        seen.add(str(extra.get("id")))
        orders.append(extra)
    return orders


def _actual_for_leg(leg, manager=None, lookup_fn=None, account: str = "paper") -> dict:
    """What the broker actually holds for a leg: filled positions PLUS
    the unfilled quantity of open orders, netted per symbol (buys +,
    sells -). B13: the strategy records the expected position the
    moment an order is submitted, but positions() only reflects fills
    — a limit order that has not filled yet is an open order, not a
    position. Counting only fills would flag every unfilled order as
    drift and halt the guardian on the first live run. Counting the
    open-order quantity keeps that case green while an open order the
    strategy does NOT expect is still drift (it appears below as an
    unexpected position). B16 correction: a DONE_FOR_DAY order's
    unfilled quantity counts here too (from the intent store — the
    OPEN query excludes it), or the routine overnight case would
    trip the drift halt. Trailing stops are EXCLUDED: a working
    conditional order is not a holding (its fills ARE counted, via
    positions()), and counting its qty as -remaining would make the
    actual holding negative the moment a stop is attached."""
    actual: dict[str, dict] = {}
    for symbol, payload in _positions_for_leg(leg).items():
        qty = float(payload.get("qty", 0.0) or 0.0)
        if qty != 0:
            actual[symbol] = {
                "qty": qty,
                "position_qty": qty,
                "open_order_qty": 0.0,
            }
    for order in _open_orders_including_done_for_day(leg, manager, lookup_fn,
                                                      account=account):
        if str(order.get("order_type", "")).lower() == "trailing_stop":
            continue
        remaining = float(order.get("qty") or 0.0) - float(order.get("filled_qty") or 0.0)
        if remaining == 0:
            continue
        signed = remaining if str(order.get("side", "")).lower() == "buy" else -remaining
        symbol = str(order.get("symbol"))
        entry = actual.setdefault(symbol, {"qty": 0.0, "position_qty": 0.0, "open_order_qty": 0.0})
        entry["qty"] = entry["qty"] + signed
        entry["open_order_qty"] = entry["open_order_qty"] + signed
    return {s: v for s, v in actual.items() if round(v["qty"], 6) != 0}


def reconcile(broker, memory, cfg: TradingConfig, guardian=None, manager=None) -> dict:
    state = _load_state(cfg.paths.strategy_state_path)
    expected = state.get("expected_positions", {})
    actual = {}
    for leg_name in ("paper", "live"):
        leg = getattr(broker, leg_name)
        # The DONE_FOR_DAY quantities come from the intent store; the
        # lookup for each leg is that leg's own (the intents are
        # paper-leg, but a live DONE_FOR_DAY intent would resolve
        # through the live lookup the same way). The account is the
        # leg's own: a paper intent must never count as a live-leg
        # holding (it would read as an unexpected live position).
        lookup_fn = getattr(leg, "get_by_client_order_id", None)
        actual[leg_name] = _actual_for_leg(leg, manager, lookup_fn, account=leg_name)
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

