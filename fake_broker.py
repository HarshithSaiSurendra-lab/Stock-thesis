"""
fake_broker.py — test-only fake broker for B3 / B2. No real network,
no Alpaca, no live or paper orders.

Models the failure modes that make exact-once hard:
- accept + drop the response (order stored, TimeoutError raised),
- delayed lookup visibility (first N lookups after acceptance return
  None, then the order appears),
- duplicate client_order_id: a second submit with the same ID returns
  the original order, never creates a second broker order,
- partial fills, including replaying the same fill_id.

Thread-safe (one lock) so two workers can submit concurrently.
"""

from __future__ import annotations

import threading
from typing import Optional


class DuplicateClientOrderIdError(RuntimeError):
    """409-like duplicate client_order_id rejection."""


class FakeBroker:
    def __init__(self, delayed_visibility_polls: int = 0, drop_next_response: bool = False):
        self.delayed_visibility_polls = delayed_visibility_polls
        self.drop_next_response = drop_next_response
        self._lock = threading.Lock()
        self._orders: dict[str, dict] = {}  # client_order_id -> order
        self._by_id: dict[str, dict] = {}  # broker order id -> order
        self._visibility_counts: dict[str, int] = {}
        self._seq = 0
        self._fills: dict[str, dict] = {}  # fill_id -> fill

    def submit(self, order: dict) -> dict:
        coid = order.get("client_order_id")
        if not coid:
            raise ValueError("fake broker requires client_order_id")
        with self._lock:
            if coid in self._orders:
                # Idempotent: return the original, never a second order.
                return dict(self._orders[coid])
            self._seq += 1
            stored = {
                "id": f"fake-{self._seq:04d}",
                "client_order_id": coid,
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "qty": float(order.get("qty") or 0),
                "order_type": order.get("order_type"),
                "limit_price": order.get("limit_price"),
                "status": "submitted",
                "filled_qty": 0.0,
            }
            self._orders[coid] = stored
            self._by_id[stored["id"]] = stored
            drop = self.drop_next_response
            if drop:
                self.drop_next_response = False
            if drop:
                raise TimeoutError("fake broker accepted order but dropped response")
            return dict(stored)

    def submit_raising_duplicate(self, order: dict) -> dict:
        """Variant that raises a 409-like error on a duplicate ID, for
        testing the explicit-rejection path (real Alpaca behaviour)."""
        coid = order.get("client_order_id")
        with self._lock:
            if coid in self._orders:
                raise DuplicateClientOrderIdError(
                    f"409 duplicate client_order_id {coid}"
                )
        return self.submit(order)

    def get_by_client_order_id(self, client_order_id: str) -> Optional[dict]:
        with self._lock:
            order = self._orders.get(client_order_id)
            if order is None:
                return None
            count = self._visibility_counts.get(client_order_id, 0) + 1
            self._visibility_counts[client_order_id] = count
            if count <= self.delayed_visibility_polls:
                return None
            return dict(order)

    def apply_partial_fill(self, client_order_id: str, fill_id: str, qty: float, price: float) -> dict:
        with self._lock:
            order = self._orders[client_order_id]
            if fill_id in self._fills:
                return dict(self._fills[fill_id])
            filled = float(order["filled_qty"]) + float(qty)
            order["filled_qty"] = min(filled, float(order["qty"]))
            order["status"] = "filled" if order["filled_qty"] >= float(order["qty"]) else "partially_filled"
            fill = {
                "fill_id": fill_id,
                "client_order_id": client_order_id,
                "broker_order_id": order["id"],
                "qty": float(qty),
                "cumulative_filled_qty": float(order["filled_qty"]),
                "price": float(price),
            }
            self._fills[fill_id] = fill
            return dict(fill)

    def duplicate_fill_delivery(self, fill: dict) -> dict:
        """Replay the exact same fill (same fill_id) — a duplicate delivery."""
        return dict(fill)

    def list_orders(self) -> list[dict]:
        with self._lock:
            return [dict(o) for o in self._orders.values()]

    def order_count(self) -> int:
        with self._lock:
            return len(self._orders)
